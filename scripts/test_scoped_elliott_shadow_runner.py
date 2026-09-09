"""Focused offline tests for the opt-in scoped Elliott shadow runner."""

from __future__ import annotations

import unittest
from typing import Any, Mapping, Sequence

from elliott_ai.complete_history_shadow_runner import (
    CompleteHistoryBundle,
    CompleteHistoryDataset,
)
from elliott_ai.forecast_records import DatasetCutoff, DatasetHashScope, canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import (
    NativeCandle,
    NativeOHLCVWindow,
    PivotPriceField,
    WindowCoverageRole,
)
from elliott_ai.lower_timeframe_recursive_proof import RecursiveProofHypothesisRole
from elliott_ai.lower_timeframe_subdivision_verifier import SubdivisionVerificationStatus
from elliott_ai.scoped_elliott_shadow_runner import (
    ScopedCoverageStatus,
    ScopedElliottShadowError,
    ScopedElliottShadowApproval,
    ScopedElliottShadowReasonCode,
    ScopedElliottShadowRunner,
    ScopedSegmentPreparationStatus,
    build_scoped_elliott_shadow_preflight_from_bundle,
)


UTC_CUTOFF = "2023-05-01T00:00:00Z"
SCOPE_START = "2022-11-01T00:00:00Z"
SCOPE_PRICE = 83.34


def _sha(value: Any) -> str:
    return canonical_sha256(value)


def _candle(candle_id: str, timestamp: str, low: float, high: float) -> NativeCandle:
    return NativeCandle.create(
        candle_id=candle_id,
        timestamp_utc=timestamp,
        open=(low + high) / 2,
        high=high,
        low=low,
        close=(low + high) / 2,
        volume=1_000.0,
    )


MONTHLY_ROWS = (
    _candle("monthly-before-scope", "2022-10-01T00:00:00Z", 100.0, 110.0),
    _candle("monthly-0", "2022-11-01T00:00:00Z", 83.34, 90.0),
    _candle("monthly-1", "2022-12-01T00:00:00Z", 90.0, 120.0),
    _candle("monthly-2", "2023-01-01T00:00:00Z", 110.0, 130.0),
    _candle("monthly-3", "2023-02-01T00:00:00Z", 100.0, 140.0),
    _candle("monthly-4", "2023-03-01T00:00:00Z", 115.0, 135.0),
    _candle("monthly-5", "2023-04-01T00:00:00Z", 110.0, 130.0),
)

WEEKLY_ROWS = (
    # The fixture uses a source candle that brackets the selected Monthly
    # pivot exactly, so the tests exercise boundary lineage rather than an
    # artificial coverage gap.
    _candle("weekly-0", "2022-11-01T00:00:00Z", 83.34, 90.0),
    _candle("weekly-1", "2022-11-14T00:00:00Z", 85.0, 110.0),
    _candle("weekly-2", "2022-11-21T00:00:00Z", 90.0, 100.0),
    _candle("weekly-3", "2022-12-05T00:00:00Z", 105.0, 120.0),
    _candle("weekly-4", "2022-12-12T00:00:00Z", 100.0, 115.0),
    _candle("weekly-5", "2023-01-09T00:00:00Z", 110.0, 130.0),
    _candle("weekly-6", "2023-01-16T00:00:00Z", 105.0, 125.0),
    _candle("weekly-7", "2023-02-06T00:00:00Z", 115.0, 140.0),
    _candle("weekly-8", "2023-03-06T00:00:00Z", 115.0, 135.0),
    _candle("weekly-9", "2023-04-03T00:00:00Z", 110.0, 130.0),
)


def _cutoff(timeframe: str, *, bar_count: int) -> DatasetCutoff:
    document_hash = _sha({"fixture": "scoped-shadow", "timeframe": timeframe, "bar_count": bar_count})
    return DatasetCutoff(
        dataset_id=f"fixture-{timeframe}",
        source_reference=f"fixture://{timeframe}",
        timeframe=timeframe,
        cutoff_utc=UTC_CUTOFF,
        dataset_hash=document_hash,
        hash_scope=DatasetHashScope.SOURCE_DOCUMENT,
        provider="fixture_provider",
        feed_identity="fixture-compatible-feed",
        requested_symbol="GOOGL",
        resolved_symbol="GOOGL",
        exchange="NASDAQ",
        session="regular",
        timezone="UTC",
        adjustment={"splits": True, "dividends": False},
        price_basis="split_adjusted_dividend_unadjusted_ohlcv",
        completed_candles_only=True,
        source_document_hash=document_hash,
        captured_at_utc=UTC_CUTOFF,
        bar_count=bar_count,
    )


def _dataset(timeframe: str, candles: Sequence[NativeCandle]) -> CompleteHistoryDataset:
    cutoff = _cutoff(timeframe, bar_count=len(candles))
    window = NativeOHLCVWindow.create(
        window_id=f"fixture-{timeframe}-window",
        dataset_cutoff=cutoff,
        timeframe=timeframe,
        candles=tuple(candles),
        coverage_role=WindowCoverageRole.REQUIRED_GENERATION,
        is_native=True,
    )
    return CompleteHistoryDataset.create(
        timeframe=timeframe,
        source_path=f"fixture/{timeframe}.json",
        source_file_sha256=_sha({"source": timeframe}),
        canonical_content_sha256=_sha({"canonical": timeframe}),
        raw_feed_identity=f"fixture|NASDAQ|GOOGL|{timeframe}|splits|regular",
        proof_dataset_cutoff=cutoff,
        coverage_start_utc=candles[0].timestamp_utc,
        coverage_end_utc=candles[-1].timestamp_utc,
        source_window=window,
    )


def _bundle(*, weekly_rows: Sequence[NativeCandle] = WEEKLY_ROWS) -> CompleteHistoryBundle:
    datasets = (
        _dataset("monthly", MONTHLY_ROWS),
        _dataset("weekly", weekly_rows),
        _dataset("daily", WEEKLY_ROWS),
        _dataset("4h", WEEKLY_ROWS),
        _dataset("1h", WEEKLY_ROWS[5:]),
        _dataset("15m", WEEKLY_ROWS[7:]),
    )
    return CompleteHistoryBundle.create(
        bundle_id="fixture-scoped-shadow-bundle",
        bundle_directory="fixture/scoped-shadow",
        shared_cutoff_utc=UTC_CUTOFF,
        manifest_file_sha256=_sha({"manifest": "fixture"}),
        manifest_canonical_content_sha256=_sha({"manifest-canonical": "fixture"}),
        coverage_map_content_sha256=_sha({"coverage": "fixture"}),
        identity_bridge_content_sha256=_sha({"bridge": "fixture"}),
        datasets=datasets,
        warnings=(),
    )


def _invalidation(source_pivot_id: str, threshold: float, *, direction: str = "below") -> dict[str, Any]:
    return {
        "invalidation_id": f"fixture-invalid-{source_pivot_id}",
        "threshold_price": threshold,
        "direction": direction,
        "evaluation_basis": "intrabar_touch_or_breach",
        "source_pivot_id": source_pivot_id,
    }


class _FakeProvider:
    name = "openai"
    model = "gpt-5.6-terra"

    def __init__(self, *, wrong_start: bool = False, identical_maps: bool = False) -> None:
        self.wrong_start = wrong_start
        self.identical_maps = identical_maps
        self.calls: list[tuple[str, str]] = []
        self.packets: list[Mapping[str, Any]] = []

    @staticmethod
    def _pivot(catalog: Sequence[Mapping[str, Any]], *, timestamp: str, price: float, field: str) -> Mapping[str, Any]:
        normalized_timestamp = timestamp.replace("Z", "+00:00")
        return next(
            item
            for item in catalog
            if item["timestamp_utc"].replace("Z", "+00:00") == normalized_timestamp
            and abs(item["price"] - price) < 1e-9
            and item["price_field"] == field
        )

    def generate_monthly_map(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append(("monthly", role.value))
        self.packets.append(packet)
        catalog = packet["pivot_catalog"]
        start = self._pivot(catalog, timestamp=SCOPE_START, price=SCOPE_PRICE, field="low")
        if self.wrong_start:
            start = self._pivot(catalog, timestamp=SCOPE_START, price=90.0, field="high")
        completed_end = self._pivot(catalog, timestamp="2023-02-01T00:00:00+00:00", price=140.0, field="high")
        active_end = self._pivot(catalog, timestamp="2023-04-01T00:00:00+00:00", price=110.0, field="low")
        family = "zigzag" if self.identical_maps or role is RecursiveProofHypothesisRole.PRIMARY else "flat"
        return {
            "map_id": f"{role.value}-map",
            "outer_parent": {
                "parent_id": f"{role.value}-outer",
                "degree": "Cycle",
                "declared_family": "impulse",
                "direction": "up",
                "completion_state": "active",
                "start_pivot_id": start["pivot_id"],
                "end_pivot_id": active_end["pivot_id"],
                "invalidation": _invalidation(start["pivot_id"], start["price"]),
            },
            "origin_control": None,
            "as_of_monthly_candle_id": packet["as_of_monthly_candle"]["candle_id"],
            "segments": [
                {
                    "segment_id": f"{role.value}-completed",
                    "degree": "Primary",
                    "declared_family": family,
                    "direction": "up",
                    "completion_state": "completed",
                    "start_pivot_id": start["pivot_id"],
                    "end_pivot_id": completed_end["pivot_id"],
                    "invalidation": _invalidation(start["pivot_id"], start["price"]),
                },
                {
                    "segment_id": f"{role.value}-active",
                    "degree": "Primary",
                    "declared_family": "zigzag",
                    "direction": "down",
                    "completion_state": "active",
                    "start_pivot_id": completed_end["pivot_id"],
                    "end_pivot_id": active_end["pivot_id"],
                    "invalidation": _invalidation(completed_end["pivot_id"], completed_end["price"], direction="above"),
                },
            ],
        }

    def generate_weekly_child_graph_batch(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append(("weekly", role.value))
        self.packets.append(packet)
        entries = []
        for request in packet["segment_requests"]:
            catalog = request["pivot_catalog"]
            start_id = request["boundary_lineage"][0]["child_pivot_id"]
            end_id = request["boundary_lineage"][1]["child_pivot_id"]
            high_110 = self._pivot(catalog, timestamp="2022-11-14T00:00:00+00:00", price=110.0, field="high")
            low_100 = self._pivot(catalog, timestamp="2022-12-12T00:00:00+00:00", price=100.0, field="low")
            by_id = {item["pivot_id"]: item for item in catalog}
            start = by_id[start_id]
            end = by_id[end_id]
            family = request["parent_wave"]["declared_family"]
            first_family = "zigzag" if family == "flat" else "impulse"
            children = [
                ("A", "up", first_family, start, high_110),
                ("B", "down", "zigzag", high_110, low_100),
                ("C", "up", "impulse", low_100, end),
            ]
            entries.append(
                {
                    "segment_id": request["segment_id"],
                    "graph": {
                        "graph_id": f"{role.value}-{request['segment_id']}-weekly",
                        "declared_family": family,
                        "boundary_lineage": request["boundary_lineage"],
                        "children": [
                            {
                                "child_id": f"{role.value}-{request['segment_id']}-{position}",
                                "sequence_position": position,
                                "direction": direction,
                                "declared_family": child_family,
                                "start_pivot_id": child_start["pivot_id"],
                                "end_pivot_id": child_end["pivot_id"],
                                "invalidation": _invalidation(child_start["pivot_id"], child_start["price"]),
                            }
                            for position, direction, child_family, child_start, child_end in children
                        ],
                    },
                }
            )
        return {"batch_id": f"{role.value}-batch", "graphs": entries}


class ScopedElliottShadowRunnerTests(unittest.TestCase):
    def preflight(self, *, weekly_rows: Sequence[NativeCandle] = WEEKLY_ROWS):
        return build_scoped_elliott_shadow_preflight_from_bundle(
            _bundle(weekly_rows=weekly_rows),
            start_timestamp_utc=SCOPE_START,
            start_price=SCOPE_PRICE,
            start_price_field=PivotPriceField.LOW,
            created_at_utc=UTC_CUTOFF,
        )

    def approval(self, preflight):
        return ScopedElliottShadowRunner.approval_template(
            preflight,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            provider="openai",
            model="gpt-5.6-terra",
        )

    def test_scope_is_catalog_backed_hashed_and_binds_the_selected_pivot(self) -> None:
        first = self.preflight()
        second = self.preflight()

        self.assertEqual(first.scope.source_selected_start_pivot.timestamp_utc, "2022-11-01T00:00:00+00:00")
        self.assertEqual(first.scope.source_selected_start_pivot.price, SCOPE_PRICE)
        self.assertEqual(first.scope.source_selected_start_pivot.price_field, PivotPriceField.LOW)
        self.assertEqual(first.scope.monthly_structural_start_utc, SCOPE_START.replace("Z", "+00:00"))
        self.assertEqual(first.monthly_window.candles[0].timestamp_utc, SCOPE_START.replace("Z", "+00:00"))
        self.assertEqual(first.scope.content_hash, second.scope.content_hash)
        self.assertEqual(first.plan.content_hash, second.plan.content_hash)
        self.assertEqual(first.plan.total_reserved_model_calls, 4)
        self.assertEqual(self.approval(first).scope_content_hash, first.scope.content_hash)
        self.assertEqual(
            self.approval(first).selected_start_pivot_content_hash,
            first.scope.scoped_selected_start_pivot.content_hash,
        )
        self.assertNotEqual(
            first.scope.source_selected_start_pivot.source_window_hash,
            first.scope.scoped_monthly_window_hash,
        )

    def test_invalid_start_pivot_is_rejected_without_provider_or_database_access(self) -> None:
        with self.assertRaises(ScopedElliottShadowError) as raised:
            build_scoped_elliott_shadow_preflight_from_bundle(
                _bundle(),
                start_timestamp_utc=SCOPE_START,
                start_price=83.35,
                start_price_field=PivotPriceField.LOW,
                created_at_utc=UTC_CUTOFF,
            )
        self.assertEqual(raised.exception.code, ScopedElliottShadowReasonCode.SCOPE_START_PIVOT_NOT_CATALOG_BACKED)

    def test_scope_uses_only_selected_history_and_optional_intraday_is_not_a_blocker(self) -> None:
        preflight = self.preflight()
        packet = __import__("elliott_ai.scoped_elliott_shadow_runner", fromlist=["_monthly_map_packet"])._monthly_map_packet(
            preflight,
            role=RecursiveProofHypothesisRole.PRIMARY,
        )
        timestamps = [item["timestamp_utc"] for item in packet["pivot_catalog"]]
        coverage = {item.timeframe: item for item in preflight.scope.coverage}

        self.assertTrue(all(timestamp >= preflight.scope.monthly_structural_start_utc for timestamp in timestamps))
        self.assertEqual(coverage["monthly"].status, ScopedCoverageStatus.COMPLETE)
        self.assertEqual(coverage["weekly"].status, ScopedCoverageStatus.COMPLETE)
        self.assertEqual(coverage["daily"].status, ScopedCoverageStatus.COMPLETE)
        self.assertEqual(coverage["4h"].status, ScopedCoverageStatus.COMPLETE)
        self.assertEqual(coverage["1h"].status, ScopedCoverageStatus.PARTIAL_OPTIONAL)
        self.assertEqual(coverage["15m"].status, ScopedCoverageStatus.PARTIAL_OPTIONAL)
        self.assertTrue(packet["constraints"]["blind"])
        self.assertTrue(packet["constraints"]["no_prior_symbol_counts_or_outcomes"])
        self.assertNotIn("forecast", packet)

    def test_model_calls_are_disabled_by_default(self) -> None:
        preflight = self.preflight()
        provider = _FakeProvider()
        with self.assertRaises(ScopedElliottShadowError) as raised:
            ScopedElliottShadowRunner().execute(
                preflight,
                provider=provider,
                approval=self.approval(preflight),
                allow_model_calls=False,
            )
        self.assertEqual(raised.exception.code, ScopedElliottShadowReasonCode.MODEL_CALL_NOT_AUTHORIZED)
        self.assertEqual(provider.calls, [])

    def test_scoped_stage_one_runs_primary_and_alternative_with_one_weekly_batch_each(self) -> None:
        preflight = self.preflight()
        provider = _FakeProvider()
        execution = ScopedElliottShadowRunner().execute(
            preflight,
            provider=provider,
            approval=self.approval(preflight),
            allow_model_calls=True,
            created_at_utc=UTC_CUTOFF,
        )

        self.assertEqual(provider.calls, [("monthly", "primary"), ("monthly", "alternative"), ("weekly", "primary"), ("weekly", "alternative")])
        self.assertEqual(execution.attempted_provider_calls, 4)
        self.assertEqual(execution.accepted_provider_calls, 4)
        self.assertEqual(execution.rejected_provider_calls, 0)
        self.assertEqual(len(execution.weekly_batches), 2)
        self.assertTrue(all(item.status is ScopedSegmentPreparationStatus.ELIGIBLE for item in execution.segment_preparations))
        self.assertTrue(all(item.proof_result.status is not SubdivisionVerificationStatus.VERIFIED for item in execution.segment_proof_results))
        self.assertTrue(all("verified" not in child.candidate_state for batch in execution.weekly_batches for graph in batch.graphs for child in graph.children))

    def test_missing_weekly_coverage_is_not_covered_and_does_not_block_candidate_maps(self) -> None:
        preflight = self.preflight(weekly_rows=WEEKLY_ROWS[3:])
        provider = _FakeProvider()
        execution = ScopedElliottShadowRunner().execute(
            preflight,
            provider=provider,
            approval=self.approval(preflight),
            allow_model_calls=True,
            created_at_utc=UTC_CUTOFF,
        )

        self.assertEqual(provider.calls, [("monthly", "primary"), ("monthly", "alternative")])
        self.assertEqual(execution.attempted_provider_calls, 2)
        self.assertEqual(execution.accepted_provider_calls, 2)
        self.assertEqual(execution.rejected_provider_calls, 0)
        self.assertEqual(execution.weekly_batches, ())
        self.assertTrue(all(item.status is ScopedSegmentPreparationStatus.NOT_COVERED for item in execution.segment_preparations))
        self.assertTrue(all(item.proof_result.status is SubdivisionVerificationStatus.NOT_COVERED for item in execution.segment_proof_results))

    def test_wrong_model_start_pivot_fails_deterministically_before_weekly_calls(self) -> None:
        preflight = self.preflight()
        provider = _FakeProvider(wrong_start=True)
        with self.assertRaises(ScopedElliottShadowError) as raised:
            ScopedElliottShadowRunner().execute(
                preflight,
                provider=provider,
                approval=self.approval(preflight),
                allow_model_calls=True,
                created_at_utc=UTC_CUTOFF,
            )
        self.assertEqual(raised.exception.code, ScopedElliottShadowReasonCode.MONTHLY_MAP_SCOPE_MISMATCH)
        self.assertEqual(provider.calls, [("monthly", "primary")])
        self.assertEqual(raised.exception.attempted_provider_calls, 1)
        self.assertEqual(raised.exception.accepted_provider_calls, 0)
        self.assertEqual(raised.exception.rejected_provider_calls, 1)

    def test_identical_primary_and_alternative_maps_are_rejected_before_weekly_calls(self) -> None:
        preflight = self.preflight()
        provider = _FakeProvider(identical_maps=True)
        with self.assertRaises(ScopedElliottShadowError) as raised:
            ScopedElliottShadowRunner().execute(
                preflight,
                provider=provider,
                approval=self.approval(preflight),
                allow_model_calls=True,
                created_at_utc=UTC_CUTOFF,
            )
        self.assertEqual(raised.exception.code, ScopedElliottShadowReasonCode.MONTHLY_MAP_PAIR_NOT_DISTINCT)
        self.assertEqual(provider.calls, [("monthly", "primary"), ("monthly", "alternative")])
        self.assertEqual(raised.exception.attempted_provider_calls, 2)
        self.assertEqual(raised.exception.accepted_provider_calls, 2)
        self.assertEqual(raised.exception.rejected_provider_calls, 0)

    def test_approval_requires_direct_scope_binding(self) -> None:
        preflight = self.preflight()
        approval = self.approval(preflight)
        altered = type(approval)(
            **{
                **approval.to_dict(),
                "scope_content_hash": "0" * 64,
                "content_hash": "",
            }
        )
        provider = _FakeProvider()
        with self.assertRaises(ScopedElliottShadowError) as raised:
            ScopedElliottShadowRunner().execute(
                preflight,
                provider=provider,
                approval=altered,
                allow_model_calls=True,
            )
        self.assertEqual(raised.exception.code, ScopedElliottShadowReasonCode.APPROVAL_HASH_INVALID)
        self.assertEqual(provider.calls, [])

    def test_approval_cannot_bind_a_different_selected_start_pivot(self) -> None:
        preflight = self.preflight()
        approval = self.approval(preflight)
        altered = ScopedElliottShadowApproval.create(
            **{
                **approval.to_dict(),
                "selected_start_pivot_content_hash": "0" * 64,
                "content_hash": "",
            }
        )
        provider = _FakeProvider()
        with self.assertRaises(ScopedElliottShadowError) as raised:
            ScopedElliottShadowRunner().execute(
                preflight,
                provider=provider,
                approval=altered,
                allow_model_calls=True,
            )
        self.assertEqual(raised.exception.code, ScopedElliottShadowReasonCode.APPROVAL_BINDING_MISMATCH)
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
