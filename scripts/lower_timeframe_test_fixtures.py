"""Shared offline fixtures for lower-timeframe shadow-only tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from elliott_ai.forecast_records import DatasetCutoff, DatasetHashScope, canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import (
    CandidateGraphRole,
    CandidateChildGraph,
    CandidateInvalidation,
    CandidateProofScope,
    GeneratedChildWave,
    InvalidationDirection,
    InvalidationEvaluationBasis,
    LowerTimeframeCandidateGenerationRequest,
    NativeCandle,
    NativeOHLCVScope,
    NativeOHLCVWindow,
    ParentCandidateSnapshot,
    PivotPriceField,
    TypedPivot,
    WindowCoverageRole,
    build_native_pivot_catalog,
    googl_intermediate_to_minute_policy,
)


UTC = timezone.utc
ANALYSIS_HASH = canonical_sha256({"source": "analysis fixture"})
RESOLUTION_HASH = canonical_sha256({"source": "resolution fixture"})


def make_window(
    *,
    timeframe: str = "daily",
    native: bool = True,
    row_count: int = 8,
) -> NativeOHLCVWindow:
    start = datetime(2025, 1, 2, 20, 0, tzinfo=UTC)
    interval = timedelta(days=1) if timeframe == "daily" else timedelta(hours=4)
    rows = (
        (100.0, 105.0, 100.0, 103.0),
        (115.0, 120.0, 115.0, 118.0),
        (112.0, 115.0, 110.0, 113.0),
        (140.0, 150.0, 140.0, 145.0),
        (132.0, 135.0, 130.0, 133.0),
        (160.0, 180.0, 160.0, 175.0),
        (170.0, 175.0, 165.0, 168.0),
        (172.0, 190.0, 170.0, 180.0),
    )
    if not 6 <= row_count <= len(rows):
        raise ValueError("row_count must preserve the six pivot fixture rows.")
    candles = tuple(
        NativeCandle.create(
            candle_id=f"{timeframe}-{index}",
            timestamp_utc=(start + (interval * index)).isoformat(timespec="seconds"),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=1000.0 + index,
        )
        for index, (open_price, high, low, close) in enumerate(rows[:row_count])
    )
    cutoff = candles[-1].timestamp_utc
    dataset = DatasetCutoff(
        dataset_id=f"fixture-{timeframe}",
        source_reference=f"fixture-{timeframe}.json",
        timeframe=timeframe,
        cutoff_utc=cutoff,
        dataset_hash=canonical_sha256({"dataset": timeframe, "rows": len(candles)}),
        hash_scope=DatasetHashScope.STORED_DATASET_SUMMARY,
        provider="offline_fixture",
        feed_identity="fixture_regular_session",
        requested_symbol="NASDAQ:GOOGL",
        resolved_symbol="GOOGL",
        exchange="NASDAQ",
        session="regular",
        timezone="America/New_York",
        adjustment={"splits_adjusted": True, "dividends_adjusted": False},
        price_basis="split_adjusted_dividend_unadjusted_ohlc",
        completed_candles_only=True,
        bar_count=len(candles),
    )
    return NativeOHLCVWindow.create(
        window_id=f"fixture-window-{timeframe}",
        dataset_cutoff=dataset,
        timeframe=timeframe,
        candles=candles,
        coverage_role=WindowCoverageRole.REQUIRED_GENERATION,
        is_native=native,
    )


def pivot_for(
    catalog: tuple[TypedPivot, ...],
    *,
    window: NativeOHLCVWindow,
    index: int,
    field: PivotPriceField,
) -> TypedPivot:
    timestamp = window.candles[index].timestamp_utc
    return next(
        pivot
        for pivot in catalog
        if pivot.timestamp_utc == timestamp and pivot.price_field is field
    )


def make_generation_request(
    *,
    parent_degree: str = "Intermediate",
    target_child_degree: str = "Minor",
    timeframe: str = "daily",
    generation_depth: int = 0,
    end_is_date_only: bool = False,
    native: bool = True,
    row_count: int = 8,
) -> tuple[LowerTimeframeCandidateGenerationRequest, tuple[TypedPivot, ...]]:
    window = make_window(timeframe=timeframe, native=native, row_count=row_count)
    catalog = build_native_pivot_catalog(window, pivot_window=1) if native else ()
    if not catalog:
        # Non-native windows are deliberately only used in direct verifier tests.
        native_window = make_window(timeframe=timeframe, native=True, row_count=row_count)
        catalog = build_native_pivot_catalog(native_window, pivot_window=1)
    pivot_window = make_window(timeframe=timeframe, native=True, row_count=row_count)
    start = pivot_for(catalog, window=pivot_window, index=0, field=PivotPriceField.LOW)
    end = pivot_for(catalog, window=pivot_window, index=5, field=PivotPriceField.HIGH)
    parent = ParentCandidateSnapshot.create(
        parent_candidate_id=f"parent-{parent_degree}-{timeframe}",
        source_wave_id="source-parent",
        source_analysis_run_id=21,
        source_analysis_run_hash=ANALYSIS_HASH,
        source_degree_resolution_id=6,
        source_degree_resolution_hash=RESOLUTION_HASH,
        degree=parent_degree,
        timeframe=timeframe,
        direction="up",
        declared_family="impulse",
        completion_state="completed",
        start_pivot=start,
        end_pivot=end,
        invalidation=CandidateInvalidation(
            invalidation_id="parent-origin",
            threshold_price=50.0,
            direction=InvalidationDirection.BELOW,
            evaluation_basis=InvalidationEvaluationBasis.INTRABAR_TOUCH_OR_BREACH,
            source_pivot_id=start.pivot_id,
        ),
        dataset_cutoff=window.dataset_cutoff,
        generation_depth=generation_depth,
        end_is_date_only=end_is_date_only,
    )
    request = LowerTimeframeCandidateGenerationRequest.create(
        request_id=f"generation-{parent_degree}-{target_child_degree}-{timeframe}",
        parent_candidate=parent,
        analysis_cutoff_utc=window.dataset_cutoff.cutoff_utc,
        target_child_degree=target_child_degree,
        target_timeframe=timeframe,
        native_ohlcv_scope=NativeOHLCVScope.create(required_window=window),
        pivot_catalog=catalog,
        proof_scope=CandidateProofScope(
            target_timeframe=timeframe,
            verified_to_timeframe=None,
            terminal_degree="Minute",
            coverage_limitations=(),
        ),
        generation_policy=googl_intermediate_to_minute_policy(),
        shadow_mode=True,
        created_at_utc=window.dataset_cutoff.cutoff_utc,
    )
    return request, catalog


def raw_graph(
    request: LowerTimeframeCandidateGenerationRequest,
    catalog: tuple[TypedPivot, ...],
    *,
    role: CandidateGraphRole,
    family: str = "impulse",
    malformed_boundary: bool = False,
) -> dict[str, Any]:
    window = request.native_ohlcv_scope.required_window
    pivot = {
        "p0": pivot_for(catalog, window=window, index=0, field=PivotPriceField.LOW),
        "p1": pivot_for(catalog, window=window, index=1, field=PivotPriceField.HIGH),
        "p2": pivot_for(catalog, window=window, index=2, field=PivotPriceField.LOW),
        "p3": pivot_for(catalog, window=window, index=3, field=PivotPriceField.HIGH),
        "p4": pivot_for(catalog, window=window, index=4, field=PivotPriceField.LOW),
        "p5": pivot_for(catalog, window=window, index=5, field=PivotPriceField.HIGH),
    }
    template = (
        (
            ("1", "up", "impulse", "p0", "p1"),
            ("2", "down", "zigzag", "p1", "p2"),
            ("3", "up", "impulse", "p2", "p3"),
            ("4", "down", "zigzag", "p3", "p4"),
            ("5", "up", "impulse", "p4", "p5"),
        )
        if family in {"impulse", "diagonal"}
        else (
            ("A", "up", "impulse", "p0", "p1"),
            ("B", "down", "zigzag", "p1", "p2"),
            ("C", "up", "impulse", "p2", "p5"),
        )
    )
    children = []
    for index, (position, direction, child_family, start_name, end_name) in enumerate(template, start=1):
        if malformed_boundary and index == 1:
            end_name = "p2"
        children.append(
            {
                "child_id": f"{role.value}-{position}",
                "sequence_position": position,
                "direction": direction,
                "declared_family": child_family,
                "start_pivot_id": pivot[start_name].pivot_id,
                "end_pivot_id": pivot[end_name].pivot_id,
                "invalidation": {
                    "invalidation_id": f"{role.value}-{position}-invalidation",
                    "threshold_price": 50.0 if direction == "up" else 250.0,
                    "direction": "below" if direction == "up" else "above",
                    "evaluation_basis": "intrabar_touch_or_breach",
                    "source_pivot_id": pivot["p0"].pivot_id,
                },
            }
        )
    return {
        "graph_id": f"{role.value}-{family}-graph",
        "declared_family": family,
        "proof_scope": {
            "terminal_degree": "Minute",
            "verified_to_timeframe": None,
            "coverage_limitations": [],
        },
        "children": children,
    }


class FakeGraphProvider:
    name = "fake_structured_provider"
    model = "fake-graph-model"

    def __init__(self, outputs: Mapping[CandidateGraphRole, Mapping[str, Any]]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[CandidateGraphRole, Mapping[str, Any]]] = []

    def generate_child_graph(
        self,
        *,
        role: CandidateGraphRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((role, packet))
        assert "primary_graph" not in packet
        assert "alternative_graph" not in packet
        assert output_schema["type"] == "object"
        return self.outputs[role]


def generated_wave(
    request: LowerTimeframeCandidateGenerationRequest,
    catalog: tuple[TypedPivot, ...],
    *,
    role: CandidateGraphRole,
    family: str = "impulse",
    malformed_boundary: bool = False,
) -> tuple[GeneratedChildWave, ...]:
    raw = raw_graph(
        request,
        catalog,
        role=role,
        family=family,
        malformed_boundary=malformed_boundary,
    )
    window = request.native_ohlcv_scope.required_window
    by_id = {item.pivot_id: item for item in catalog}
    return tuple(
        GeneratedChildWave(
            child_id=item["child_id"],
            parent_candidate_id=request.parent_candidate.parent_candidate_id,
            degree=request.target_child_degree,
            timeframe=request.target_timeframe,
            sequence_position=item["sequence_position"],
            direction=item["direction"],
            declared_family=item["declared_family"],
            start_pivot=by_id[item["start_pivot_id"]],
            end_pivot=by_id[item["end_pivot_id"]],
            invalidation=CandidateInvalidation(**item["invalidation"]),
        )
        for item in raw["children"]
    )


def direct_graph(
    request: LowerTimeframeCandidateGenerationRequest,
    catalog: tuple[TypedPivot, ...],
    *,
    role: CandidateGraphRole,
    family: str = "impulse",
    malformed_boundary: bool = False,
) -> CandidateChildGraph:
    raw = raw_graph(
        request,
        catalog,
        role=role,
        family=family,
        malformed_boundary=malformed_boundary,
    )
    return CandidateChildGraph.create(
        graph_id=raw["graph_id"],
        role=role,
        request_id=request.request_id,
        request_content_hash=request.content_hash,
        parent_candidate_id=request.parent_candidate.parent_candidate_id,
        target_child_degree=request.target_child_degree,
        target_timeframe=request.target_timeframe,
        declared_family=family,
        children=generated_wave(
            request,
            catalog,
            role=role,
            family=family,
            malformed_boundary=malformed_boundary,
        ),
        proof_scope=request.proof_scope,
        provider="fixture_direct_graph",
        model=None,
        generated_at_utc=request.created_at_utc,
        policy_version=request.generation_policy.policy_id,
    )


def direct_family_graph(
    request: LowerTimeframeCandidateGenerationRequest,
    catalog: tuple[TypedPivot, ...],
    *,
    role: CandidateGraphRole,
    family: str,
    positions: tuple[str, ...],
    child_families: tuple[str, ...],
) -> CandidateChildGraph:
    """Build a catalog-only graph for family-semantic verifier fixtures."""
    if len(positions) != len(child_families):
        raise ValueError("positions and child_families must have the same length.")
    window = request.native_ohlcv_scope.required_window
    pivot_sequence = (
        pivot_for(catalog, window=window, index=0, field=PivotPriceField.LOW),
        pivot_for(catalog, window=window, index=1, field=PivotPriceField.HIGH),
        pivot_for(catalog, window=window, index=2, field=PivotPriceField.LOW),
        pivot_for(catalog, window=window, index=3, field=PivotPriceField.HIGH),
        pivot_for(catalog, window=window, index=4, field=PivotPriceField.LOW),
        pivot_for(catalog, window=window, index=5, field=PivotPriceField.HIGH),
    )
    if len(positions) == 3:
        pivot_sequence = (
            pivot_sequence[0],
            pivot_sequence[1],
            pivot_sequence[2],
            pivot_sequence[5],
        )
    children = []
    for index, (position, child_family) in enumerate(zip(positions, child_families), start=1):
        start = pivot_sequence[index - 1]
        end = pivot_sequence[index]
        direction = "up" if end.price > start.price else "down"
        children.append(
            GeneratedChildWave(
                child_id=f"{role.value}-{family}-{position}",
                parent_candidate_id=request.parent_candidate.parent_candidate_id,
                degree=request.target_child_degree,
                timeframe=request.target_timeframe,
                sequence_position=position,
                direction=direction,
                declared_family=child_family,
                start_pivot=start,
                end_pivot=end,
                invalidation=CandidateInvalidation(
                    invalidation_id=f"{role.value}-{family}-{position}-invalidation",
                    threshold_price=50.0 if direction == "up" else 250.0,
                    direction=InvalidationDirection.BELOW if direction == "up" else InvalidationDirection.ABOVE,
                    evaluation_basis=InvalidationEvaluationBasis.INTRABAR_TOUCH_OR_BREACH,
                    source_pivot_id=pivot_sequence[0].pivot_id,
                ),
            )
        )
    return CandidateChildGraph.create(
        graph_id=f"{role.value}-{family}-family-graph",
        role=role,
        request_id=request.request_id,
        request_content_hash=request.content_hash,
        parent_candidate_id=request.parent_candidate.parent_candidate_id,
        target_child_degree=request.target_child_degree,
        target_timeframe=request.target_timeframe,
        declared_family=family,
        children=tuple(children),
        proof_scope=request.proof_scope,
        provider="fixture_direct_graph",
        model=None,
        generated_at_utc=request.created_at_utc,
        policy_version=request.generation_policy.policy_id,
    )
