from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from elliott_ai.adaptive_market_data import NativeOHLCVBundle
from elliott_ai.adaptive_recount import (
    AdaptiveRecountBudget,
    AdaptiveRecountCoordinator,
    adaptive_child_policies,
    create_adaptive_recount_plan,
)
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import (
    CandidateGraphRole,
    NativeCandle,
)
from elliott_ai.lower_timeframe_subdivision_verifier import (
    SubdivisionVerificationStatus,
)
from elliott_ai.timeframes import (
    ProviderCapabilitySet,
    ProviderTimeframeCapability,
)


UTC = timezone.utc
STAMP = "2026-08-31T12:00:00+00:00"
START = "2026-01-01T14:30:00+00:00"
CUTOFF = "2026-01-10T14:30:00+00:00"
FEED = "fixture-consolidated-regular"


def _capabilities(*timeframes: str) -> ProviderCapabilitySet:
    return ProviderCapabilitySet.create(
        provider="offline-fixture",
        capabilities=tuple(
            ProviderTimeframeCapability.create(
                canonical_name=timeframe,
                provider_alias=f"fixture-{timeframe}",
                native_available=True,
                derived=False,
                provider="offline-fixture",
                session_policy="nasdaq_regular_session",
                adjustment_policy="split_adjusted_dividend_unadjusted",
            )
            for timeframe in timeframes
        ),
        discovery_status="offline_fixture",
        source_interface="scripts.test_adaptive_proof_resolution_e2e",
        discovered_at_utc=STAMP,
        limitations=(),
    )


def _bundle(
    manifest,
    capabilities,
    candles,
    *,
    feed: str = FEED,
    provider: str | None = None,
    source_interface: str | None = None,
    provider_symbol: str = "TEST",
    exchange: str = "NASDAQ",
    mic: str | None = None,
    session: str | None = None,
    adjustment: str | None = None,
):
    candles = tuple(candles)
    return NativeOHLCVBundle.create_for_manifest(
        manifest,
        bundle_id=f"fixture-{manifest.canonical_timeframe}-{manifest.request_id}",
        source_request_id=manifest.request_id,
        source_request_hash=manifest.content_hash,
        provider_capability_set_hash=capabilities.content_hash,
        canonical_symbol=manifest.symbol,
        provider_symbol=provider_symbol,
        exchange=exchange,
        mic=mic,
        provider=provider or manifest.provider,
        source_interface=source_interface or manifest.source_interface,
        feed_identity=feed,
        session=session or manifest.session_policy,
        timezone="America/New_York",
        price_adjustment=adjustment or manifest.adjustment_policy,
        dividend_adjustment="unadjusted",
        volume_adjustment="split_adjusted",
        price_basis="split-adjusted dividend-unadjusted OHLCV",
        timeframe=manifest.canonical_timeframe,
        provider_interval_alias=manifest.provider_interval_alias,
        native=True,
        derived=False,
        requested_start_utc=manifest.requested_start_utc,
        requested_end_utc=manifest.requested_end_utc,
        actual_first_candle_utc=candles[0].timestamp_utc,
        actual_final_completed_candle_utc=candles[-1].timestamp_utc,
        acquisition_timestamp_utc=STAMP,
        analysis_cutoff_utc=manifest.analysis_cutoff_utc,
        timestamp_semantics="bar_open_utc_with_provider_interval",
        provider_bar_alignment=(
            capabilities.available(manifest.canonical_timeframe).provider_bar_alignment.value
        ),
        completion_verification_method="offline_fixture_completed_flag",
        incomplete_candles_excluded=True,
        source_response_hash=canonical_sha256(
            [item.to_dict() for item in candles]
        ),
        candles=candles,
    )


def _root_candles() -> tuple[NativeCandle, ...]:
    start = datetime.fromisoformat(START)
    rows = []
    for index in range(55):
        if index <= 6:
            base = 130.0 - (5.0 * index)
        elif index <= 36:
            base = 100.0 + (99.0 * (index - 6) / 30.0)
        else:
            base = 199.0 - (19.0 * (index - 36) / 18.0)
        rows.append(
            NativeCandle.create(
                candle_id=f"root-4h-{index:03d}",
                timestamp_utc=(start + timedelta(hours=4 * index)).isoformat(),
                open=base,
                high=base + 1.0,
                low=base - 1.0,
                close=base,
                volume=1000.0 + index,
            )
        )
    return tuple(rows)


def _child_candles(start_utc: str, start_price: float, end_price: float):
    start = datetime.fromisoformat(start_utc)
    anchor_indexes = (0, 24, 48, 72, 96, 120)
    anchor_prices = (
        start_price + 1.0,
        start_price + 25.0,
        start_price + 12.0,
        start_price + 71.0,
        start_price + 52.0,
        end_price - 1.0,
    )
    rows = []
    for index in range(121):
        segment = min(index // 24, 4)
        left_index = anchor_indexes[segment]
        right_index = anchor_indexes[segment + 1]
        fraction = (index - left_index) / (right_index - left_index)
        base = anchor_prices[segment] + (
            (anchor_prices[segment + 1] - anchor_prices[segment]) * fraction
        )
        high = base + 0.5
        low = base - 0.5
        if index == 0:
            low = start_price
        elif index == 24:
            high = start_price + 25.0
        elif index == 48:
            low = start_price + 11.0
        elif index == 72:
            high = start_price + 72.0
        elif index == 96:
            low = start_price + 51.0
        elif index == 120:
            high = end_price
        rows.append(
            NativeCandle.create(
                candle_id=f"child-1h-{index:03d}",
                timestamp_utc=(start + timedelta(hours=index)).isoformat(),
                open=base,
                high=high,
                low=low,
                close=base,
                volume=2000.0 + index,
            )
        )
    return tuple(rows)


class StagedRootProvider:
    name = "offline-staged-root-provider"
    model = "fixture-only"

    def __init__(self) -> None:
        self.calls = []

    def generate_root_segmentation(self, *, role, packet, output_schema):
        self.calls.append(("stage_a", role))
        boundaries = packet["available_boundaries"]
        low = min(
            (
                item
                for item in boundaries
                if item.get("kind") == "catalog_pivot"
                and item.get("extremum_type") == "low"
            ),
            key=lambda item: item["price"],
        )
        high = max(
            (
                item
                for item in boundaries
                if item.get("kind") == "catalog_pivot"
                and item.get("extremum_type") == "high"
                and item["boundary_ordinal"] > low["boundary_ordinal"]
            ),
            key=lambda item: item["price"],
        )
        return {
            "segmentation_id": f"{role.value}-segmentation",
            "steps": [
                {
                    "end_boundary_id": low["boundary_id"],
                    "segment_kind": "boundary_context",
                },
                {
                    "end_boundary_id": high["boundary_id"],
                    "segment_kind": "atomic_swing",
                },
                {
                    "end_boundary_id": boundaries[-1]["boundary_id"],
                    "segment_kind": "unresolved_interval",
                },
            ],
        }

    def generate_root_grouping(self, *, role, packet, output_schema):
        self.calls.append(("stage_b1", role))
        segment = packet["segmentation"]["segments"][1]
        return {
            "grouping_id": f"{role.value}-grouping",
            "groups": [
                {
                    "group_id": f"{role.value}-group",
                    "source_segment_ids": [segment["segment_id"]],
                    "start_boundary_id": segment["start_boundary"]["boundary_id"],
                    "end_boundary_id": segment["end_boundary"]["boundary_id"],
                    "completion_state": "closed",
                }
            ],
            "unresolved_segment_ids": [
                item["segment_id"]
                for item in packet["segmentation"]["segments"]
                if item["segment_id"] != segment["segment_id"]
            ],
        }

    def classify_root_groups(self, *, role, packet, output_schema):
        self.calls.append(("stage_b2", role))
        group = packet["grouping"]["groups"][0]
        family = "impulse" if role is CandidateGraphRole.PRIMARY else "zigzag"
        return {
            "hypothesis_id": f"{role.value}-hypothesis",
            "root_timeframe": "4h",
            "nodes": [
                {
                    "wave_id": f"{role.value}-parent",
                    "parent_wave_id": None,
                    "degree": "Primary",
                    "sequence_position": "1" if family == "impulse" else "A",
                    "timeframe": "4h",
                    "direction": "up",
                    "declared_family": family,
                    "completion_state": "completed",
                    "start_pivot_id": group["start_boundary"]["source_pivot_id"],
                    "end_pivot_id": group["end_boundary"]["source_pivot_id"],
                    "invalidation": None,
                    "child_wave_ids": [],
                    "source_group_id": group["group_id"],
                    "source_segment_ids": group["source_segment_ids"],
                    "diagonal_declaration": None,
                }
            ],
            "group_classifications": [
                {
                    "group_id": group["group_id"],
                    "classification": family,
                    "wave_id": f"{role.value}-parent",
                    "evidence_ids": [],
                }
            ],
            "active_wave_id": None,
            "confirmation_conditions": ["Require deterministic child proof."],
            "unresolved_questions": ["Child anatomy is not yet proven."],
        }


class ImpulseChildProvider:
    name = "offline-child-graph-provider"
    model = "fixture-only"

    def __init__(self) -> None:
        self.calls = []

    def generate_child_graph(self, *, role, packet, output_schema):
        self.calls.append((role, packet))
        parent = packet["parent_candidate"]
        pivots = packet["pivot_catalog"]

        def pivot(price, pivot_type):
            return next(
                item
                for item in pivots
                if item["pivot_type"] == pivot_type
                and abs(item["price"] - price) < 1e-9
            )

        start_price = parent["start_pivot"]["price"]
        end_price = parent["end_pivot"]["price"]
        selected = (
            pivot(start_price, "low"),
            pivot(start_price + 25.0, "high"),
            pivot(start_price + 11.0, "low"),
            pivot(start_price + 72.0, "high"),
            pivot(start_price + 51.0, "low"),
            pivot(end_price, "high"),
        )
        child_specs = (
            ("1", "up", "impulse"),
            ("2", "down", "zigzag"),
            ("3", "up", "impulse"),
            ("4", "down", "zigzag"),
            ("5", "up", "impulse"),
        )
        children = []
        for index, (position, direction, family) in enumerate(child_specs):
            children.append(
                {
                    "child_id": f"primary-intermediate-{position}",
                    "sequence_position": position,
                    "direction": direction,
                    "declared_family": family,
                    "start_pivot_id": selected[index]["pivot_id"],
                    "end_pivot_id": selected[index + 1]["pivot_id"],
                    "invalidation": {
                        "invalidation_id": f"child-{position}-invalidation",
                        "threshold_price": 0.01 if direction == "up" else 1000.0,
                        "direction": "below" if direction == "up" else "above",
                        "evaluation_basis": "intrabar_touch_or_breach",
                        "source_pivot_id": selected[index]["pivot_id"],
                    },
                }
            )
        return {
            "graph_id": "primary-intermediate-impulse",
            "declared_family": "impulse",
            "diagonal_declaration": None,
            "proof_scope": {
                "terminal_degree": "Intermediate",
                "verified_to_timeframe": None,
                "coverage_limitations": [],
            },
            "children": children,
        }


class AdaptiveProofResolutionEndToEndTests(unittest.TestCase):
    def _frozen_root(self):
        capabilities = _capabilities("4h", "1h", "15m")
        plan = create_adaptive_recount_plan(
            symbol="NASDAQ:TEST",
            analysis_start_utc=START,
            context_start_utc=START,
            analysis_cutoff_utc=CUTOFF,
            market_data_provider=capabilities.provider,
            provider_capabilities=capabilities,
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
            source_interface=capabilities.source_interface,
        )
        root_bundle = _bundle(
            trace.data_manifests[-1],
            capabilities,
            _root_candles(),
        )
        trace = coordinator.ingest_bundle(trace, root_bundle, updated_at_utc=STAMP)
        trace = coordinator.generate_initial_candidates(
            trace,
            (root_bundle,),
            provider=StagedRootProvider(),
            allow_model_calls=True,
            generated_at_utc=STAMP,
        )
        return coordinator, trace, root_bundle

    def test_staged_four_hour_parent_reaches_one_hour_intermediate_verifier(self) -> None:
        coordinator, trace, root_bundle = self._frozen_root()
        parent = trace.root_pair.primary.nodes[0]
        trace = coordinator.plan_parent_subdivision(
            trace,
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id=parent.wave_id,
            bundles=(root_bundle,),
            created_at_utc=STAMP,
            source_interface=trace.plan.provider_capabilities.source_interface,
        )
        decision = trace.planner_decisions[-1]
        manifest = trace.data_manifests[-1]

        self.assertEqual(decision.selected_timeframe, "1h")
        self.assertEqual(decision.proof_feasibility.parent_degree, "Primary")
        self.assertEqual(
            decision.proof_feasibility.expected_child_degree,
            "Intermediate",
        )
        self.assertEqual(manifest.canonical_timeframe, "1h")
        self.assertEqual(manifest.expected_child_degree, "Intermediate")
        self.assertIsNone(manifest.required_feed_identity)
        self.assertEqual(
            manifest.parent_feed_family.feed_family_hash,
            manifest.expected_child_feed_family.feed_family_hash,
        )
        self.assertNotEqual(
            manifest.parent_stream_identity.stream_hash,
            manifest.expected_child_stream_identity.stream_hash,
        )

        child_bundle = _bundle(
            manifest,
            trace.plan.provider_capabilities,
            _child_candles(
                parent.start_pivot.timestamp_utc,
                parent.start_pivot.price,
                parent.end_pivot.price,
            ),
        )
        trace = coordinator.ingest_bundle(trace, child_bundle, updated_at_utc=STAMP)
        generation_policy, verifier_policy = adaptive_child_policies(
            plan=trace.plan,
            target_child_degree="Intermediate",
            target_timeframe="1h",
        )
        trace = coordinator.generate_and_verify_child_graph(
            trace,
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id=parent.wave_id,
            bundle=child_bundle,
            graph_provider=ImpulseChildProvider(),
            generation_policy=generation_policy,
            verifier_policy=verifier_policy,
            allow_model_call=True,
            created_at_utc=STAMP,
        )

        proof = trace.child_proofs[-1]
        self.assertEqual(proof.candidate_graph.target_child_degree, "Intermediate")
        self.assertEqual(proof.candidate_graph.target_timeframe, "1h")
        self.assertTrue(
            all(child.degree == "Intermediate" for child in proof.candidate_graph.children)
        )
        self.assertEqual(
            proof.deterministic_result.status,
            SubdivisionVerificationStatus.VERIFIED,
        )

    def test_metadata_mismatches_fail_before_child_provider_call(self) -> None:
        coordinator, trace, root_bundle = self._frozen_root()
        parent = trace.root_pair.primary.nodes[0]
        trace = coordinator.plan_parent_subdivision(
            trace,
            role=CandidateGraphRole.PRIMARY,
            parent_wave_id=parent.wave_id,
            bundles=(root_bundle,),
            created_at_utc=STAMP,
            source_interface=trace.plan.provider_capabilities.source_interface,
        )
        manifest = trace.data_manifests[-1]
        mismatches = (
            {"source_interface": "different-provider-product"},
            {"provider": "different-provider"},
            {"session": "different-session"},
            {"adjustment": "different-adjustment"},
        )
        for overrides in mismatches:
            with self.subTest(overrides=overrides):
                child_bundle = _bundle(
                    manifest,
                    trace.plan.provider_capabilities,
                    _child_candles(
                        parent.start_pivot.timestamp_utc,
                        parent.start_pivot.price,
                        parent.end_pivot.price,
                    ),
                    **overrides,
                )
                mismatch_trace = coordinator.ingest_bundle(
                    trace,
                    child_bundle,
                    updated_at_utc=STAMP,
                )
                provider = ImpulseChildProvider()
                generation_policy, verifier_policy = adaptive_child_policies(
                    plan=mismatch_trace.plan,
                    target_child_degree="Intermediate",
                    target_timeframe="1h",
                )

                with self.assertRaisesRegex(ValueError, "data preflight failure"):
                    coordinator.generate_and_verify_child_graph(
                        mismatch_trace,
                        role=CandidateGraphRole.PRIMARY,
                        parent_wave_id=parent.wave_id,
                        bundle=child_bundle,
                        graph_provider=provider,
                        generation_policy=generation_policy,
                        verifier_policy=verifier_policy,
                        allow_model_call=True,
                        created_at_utc=STAMP,
                    )
                self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
