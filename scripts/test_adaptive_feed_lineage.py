from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from elliott_ai.adaptive_market_data import (
    BundleValidationStatus,
    DataComparabilityStatus,
    DataComparisonPurpose,
    NativeOHLCVBundle,
    compare_bundle_identity,
    validate_bundle_for_manifest,
)
from elliott_ai.adaptive_recount import (
    AdaptiveRecountBudget,
    AdaptiveRecountCoordinator,
    create_adaptive_recount_plan,
)
from elliott_ai.lower_timeframe_candidate_generator import CandidateGraphRole
from elliott_ai.live_market_data import TwelveDataClient
from elliott_ai.timeframes import (
    MarketSessionMode,
    ProviderBarAlignment,
    ProviderCapabilitySet,
    ProviderTimeframeCapability,
)
from scripts.test_adaptive_proof_resolution_e2e import (
    CUTOFF,
    STAMP,
    START,
    StagedRootProvider,
    _bundle,
    _child_candles,
    _root_candles,
)


SOURCE_INTERFACE = "elliott_ai.live_market_data.TwelveDataClient"


def _capabilities() -> ProviderCapabilitySet:
    return ProviderCapabilitySet.create(
        provider="twelve_data",
        capabilities=tuple(
            ProviderTimeframeCapability.create(
                canonical_name=timeframe,
                provider_alias=provider_alias,
                native_available=True,
                derived=False,
                provider="twelve_data",
                session_policy="regular_session_prepost_false",
                adjustment_policy="split_adjusted_dividend_unadjusted",
                market_session_mode=MarketSessionMode.REGULAR_SESSION_EQUITY,
                market_calendar_id="XNAS",
                market_calendar_source="elliott_ai.xnas_rules",
                market_calendar_version="1.0.0",
                market_timezone="America/New_York",
                session_start_local="09:30",
                session_end_local="16:00",
                session_minutes=390,
                provider_bar_alignment=(
                    ProviderBarAlignment.SESSION_OPEN_ALIGNED_WITH_SHORT_FINAL
                ),
                shortened_final_bar_emitted=True,
            )
            for timeframe, provider_alias in (
                ("4h", "4h"),
                ("1h", "1h"),
                ("15m", "15min"),
            )
        ),
        discovery_status="offline_fixture",
        source_interface=SOURCE_INTERFACE,
        discovered_at_utc=STAMP,
        limitations=(),
    )


def _replacement_bundle(bundle: NativeOHLCVBundle, **changes) -> NativeOHLCVBundle:
    raw = bundle.to_dict()
    for name in (
        "content_hash",
        "file_hash",
        "feed_family",
        "stream_identity",
    ):
        raw.pop(name, None)
    raw.update(changes)
    raw["content_hash"] = ""
    raw["file_hash"] = ""
    return NativeOHLCVBundle.create(**raw)


class AdaptiveFeedLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capabilities = _capabilities()
        plan = create_adaptive_recount_plan(
            symbol="NASDAQ:TEST",
            analysis_start_utc=START,
            context_start_utc=START,
            analysis_cutoff_utc=CUTOFF,
            market_data_provider="twelve_data",
            provider_capabilities=self.capabilities,
            created_at_utc=STAMP,
            budgets=AdaptiveRecountBudget(
                terminal_degree="Intermediate",
                maximum_bars=5000,
            ),
        )
        coordinator = AdaptiveRecountCoordinator()
        trace = coordinator.plan_initial_data(
            coordinator.initial_trace(plan),
            requested_timeframe="4h",
            created_at_utc=STAMP,
            source_interface=SOURCE_INTERFACE,
        )
        root_manifest = trace.data_manifests[-1]
        root_bundle = _bundle(
            root_manifest,
            self.capabilities,
            _root_candles(),
            feed="twelve_data|NASDAQ|TEST|4h|splits|regular",
            provider_symbol="TEST",
            exchange="NASDAQ",
            mic="XNAS",
        )
        trace = coordinator.ingest_bundle(trace, root_bundle, updated_at_utc=STAMP)
        trace = coordinator.generate_initial_candidates(
            trace,
            (root_bundle,),
            provider=StagedRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        parent = trace.root_pair.primary.nodes[0]
        trace = coordinator.plan_parent_subdivision(
            trace,
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id=parent.wave_id,
            bundles=(root_bundle,),
            requested_timeframe="1h",
            created_at_utc=STAMP,
            source_interface=SOURCE_INTERFACE,
        )
        self.coordinator = coordinator
        self.trace = trace
        self.parent = parent
        self.root_bundle = root_bundle
        self.manifest_1h = trace.data_manifests[-1]
        self.child_candles = _child_candles(
            parent.start_pivot.timestamp_utc,
            parent.start_pivot.price,
            parent.end_pivot.price,
        )

    def _child_bundle(self, **overrides) -> NativeOHLCVBundle:
        values = {
            "feed": "twelve_data|NASDAQ|TEST|1h|splits|regular",
            "provider_symbol": "TEST",
            "exchange": "NASDAQ",
            "mic": "XNAS",
        }
        values.update(overrides)
        return _bundle(
            self.manifest_1h,
            self.capabilities,
            self.child_candles,
            **values,
        )

    def test_four_hour_parent_and_one_hour_child_share_family_not_stream(self) -> None:
        child = self._child_bundle()
        result = validate_bundle_for_manifest(
            self.manifest_1h,
            child,
            self.capabilities,
        )
        self.assertEqual(result.status, BundleValidationStatus.VALID)
        self.assertEqual(
            self.root_bundle.feed_family.feed_family_hash,
            child.feed_family.feed_family_hash,
        )
        self.assertNotEqual(
            self.root_bundle.stream_identity.stream_hash,
            child.stream_identity.stream_hash,
        )
        self.assertEqual(
            self.manifest_1h.expected_child_stream_identity.stream_hash,
            child.stream_identity.stream_hash,
        )
        self.assertEqual(
            child.proof_lineage.parent_bundle_hash,
            self.root_bundle.content_hash,
        )

    def test_false_parent_stream_label_on_child_is_rejected(self) -> None:
        child = self._child_bundle(
            feed="twelve_data|NASDAQ|TEST|4h|splits|regular"
        )
        result = validate_bundle_for_manifest(
            self.manifest_1h,
            child,
            self.capabilities,
        )
        self.assertEqual(result.status, BundleValidationStatus.INVALID)
        self.assertIn("child_stream_identity_mismatch", result.errors)

    def test_family_mismatches_are_rejected_with_precise_codes(self) -> None:
        cases = (
            ({"provider": "another_provider"}, "provider_mismatch"),
            ({"provider_symbol": "OTHER"}, "provider_symbol_mismatch"),
            ({"exchange": "NYSE", "mic": "XNYS"}, "exchange_mismatch"),
            ({"mic": "XNYS"}, "mic_mismatch"),
            ({"session": "extended_session"}, "session_mismatch"),
            ({"adjustment": "unadjusted"}, "adjustment_mismatch"),
            ({"source_interface": "another.provider.product"}, "provider_product_mismatch"),
        )
        for overrides, reason in cases:
            with self.subTest(overrides=overrides):
                child = self._child_bundle(**overrides)
                result = validate_bundle_for_manifest(
                    self.manifest_1h,
                    child,
                    self.capabilities,
                )
                self.assertEqual(result.status, BundleValidationStatus.INVALID)
                self.assertIn(reason, result.errors)
                self.assertIn("feed_family_mismatch", result.errors)

    def test_separately_authorized_fifteen_minute_stream_is_isolated(self) -> None:
        trace = self.coordinator.plan_parent_subdivision(
            self.trace,
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id=self.parent.wave_id,
            bundles=(self.root_bundle,),
            requested_timeframe="15m",
            created_at_utc="2026-08-31T12:01:00+00:00",
            source_interface=SOURCE_INTERFACE,
        )
        manifest_15m = trace.data_manifests[-1]
        child_15m = _bundle(
            manifest_15m,
            self.capabilities,
            self.child_candles,
            feed="twelve_data|NASDAQ|TEST|15m|splits|regular",
            provider_symbol="TEST",
            exchange="NASDAQ",
            mic="XNAS",
        )
        self.assertEqual(
            validate_bundle_for_manifest(
                manifest_15m,
                child_15m,
                self.capabilities,
            ).status,
            BundleValidationStatus.VALID,
        )
        wrong_manifest_stream = _replacement_bundle(
            child_15m,
            source_request_id=self.manifest_1h.request_id,
            source_request_hash=self.manifest_1h.content_hash,
            proof_lineage=self._child_bundle().proof_lineage,
        )
        wrong_result = validate_bundle_for_manifest(
            self.manifest_1h,
            wrong_manifest_stream,
            self.capabilities,
        )
        self.assertEqual(wrong_result.status, BundleValidationStatus.INVALID)
        self.assertIn("expected_child_interval_mismatch", wrong_result.errors)
        self.assertIn("child_stream_identity_mismatch", wrong_result.errors)

    def test_indicator_and_volume_comparability_remain_strict(self) -> None:
        child = self._child_bundle()
        matching = _replacement_bundle(
            child,
            bundle_id="matching-1h-stream",
        )
        different_label = _replacement_bundle(
            child,
            bundle_id="different-label-1h-stream",
            feed_identity="another-product|NASDAQ|TEST|1h|splits|regular",
        )
        self.assertEqual(
            compare_bundle_identity(
                child,
                matching,
                purpose=DataComparisonPurpose.SAME_TIMEFRAME_VOLUME,
            ).status,
            DataComparabilityStatus.COMPARABLE,
        )
        strict_volume = compare_bundle_identity(
            child,
            different_label,
            purpose=DataComparisonPurpose.SAME_TIMEFRAME_VOLUME,
        )
        strict_indicator = compare_bundle_identity(
            child,
            different_label,
            purpose=DataComparisonPurpose.SAME_WAVE_INDICATOR,
        )
        self.assertEqual(strict_volume.status, DataComparabilityStatus.INCOMPARABLE)
        self.assertEqual(strict_indicator.status, DataComparabilityStatus.INCOMPARABLE)
        self.assertIn("legacy_feed_identity", strict_volume.incomparable_fields)
        self.assertEqual(
            compare_bundle_identity(
                self.root_bundle,
                child,
                purpose=DataComparisonPurpose.STRUCTURAL_PRICE,
            ).status,
            DataComparabilityStatus.COMPARABLE,
        )
        self.assertEqual(
            compare_bundle_identity(
                self.root_bundle,
                child,
                purpose=DataComparisonPurpose.CROSS_TIMEFRAME_VOLUME,
            ).status,
            DataComparabilityStatus.INCOMPARABLE,
        )
        self.assertEqual(
            compare_bundle_identity(
                self.root_bundle,
                child,
                purpose=DataComparisonPurpose.CROSS_TIMEFRAME_INDICATOR,
            ).status,
            DataComparabilityStatus.INCOMPARABLE,
        )

    def test_historical_rklb_bundle_and_trace_bytes_are_unchanged(self) -> None:
        root = Path(__file__).resolve().parents[1]
        bundle_path = root / (
            "adaptive_recount_runs/RKLB_fresh_20260831T115147Z/"
            "tf_request_ea85fafc5da539cad6b222a3653776b3_4h_7c94da9333d1f310636e.json"
        )
        trace_path = root / (
            "adaptive_recount_runs/RKLB_fresh_20260831T115147Z/"
            "adaptive_trace_34cf93676bda6abcd2fa30b3f37c3217_ecf9e826f890df88dbc5.json"
        )
        self.assertEqual(
            sha256(bundle_path.read_bytes()).hexdigest(),
            "f48d5b75341eb4676f7409cc6efd92de9c26270d5f036991b338bb7396a7cf88",
        )
        self.assertEqual(
            sha256(trace_path.read_bytes()).hexdigest(),
            "b59abbc20e6224200d3538eb86183cf76440cdc56b2bf5cbb8e73bc5689a8a6c",
        )
        raw = json.loads(bundle_path.read_text(encoding="utf-8"))
        loaded = NativeOHLCVBundle.from_dict(raw)
        self.assertEqual(loaded.to_dict(), raw)

    def test_twelve_data_adapter_emits_family_and_stream_metadata_without_secret(self) -> None:
        client = TwelveDataClient("fixture-secret", maximum_retries=0)

        def fixture_series(_symbol, timeframe):
            provider_interval = "1week" if timeframe == "weekly" else "1day"
            return {
                "timeframe": timeframe,
                "provider_interval": provider_interval,
                "metadata": {
                    "symbol": "TEST",
                    "exchange": "NASDAQ",
                    "mic_code": "XNAS",
                    "exchange_timezone": "America/New_York",
                },
                "values": [
                    {
                        "datetime": "2026-08-28",
                        "open": "10",
                        "high": "11",
                        "low": "9",
                        "close": "10.5",
                        "volume": "1000",
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as directory, patch.object(
            client,
            "fetch_time_series",
            side_effect=fixture_series,
        ):
            result = client.fetch_bundle(
                "NASDAQ:TEST",
                Path(directory),
                timeframes=("weekly", "daily"),
            )
            documents = [
                json.loads(Path(path).read_text(encoding="utf-8"))
                for path in result.paths
            ]
        families = {
            item["metadata"]["feed_family_hash"] for item in documents
        }
        streams = {item["metadata"]["stream_hash"] for item in documents}
        self.assertEqual(len(families), 1)
        self.assertEqual(len(streams), 2)
        self.assertTrue(
            all("feed_family" in item["metadata"] for item in documents)
        )
        self.assertTrue(
            all("stream_identity" in item["metadata"] for item in documents)
        )
        self.assertNotIn(
            "fixture-secret",
            json.dumps(documents, sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()
