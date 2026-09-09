from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import unittest

from elliott_ai.forecast_records import DatasetCutoff, DatasetHashScope, canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import (
    CandidateChildGraph,
    CandidateGraphRole,
    CandidateInvalidation,
    CandidateProofScope,
    GeneratedChildWave,
    InvalidationDirection,
    InvalidationEvaluationBasis,
    NativeCandle,
    NativeOHLCVWindow,
    PivotPriceField,
    TypedPivot,
    WindowCoverageRole,
    build_native_pivot_catalog,
)
from elliott_ai.lower_timeframe_recursive_proof import (
    ProofCoverageAttestation,
    ProofWaveSnapshot,
    RECURSIVE_PROOF_TIMEFRAME_LADDER,
    RecursiveMultiTimeframeProofVerifier,
    RecursiveProofBundle,
    RecursiveProofBundlePair,
    RecursiveProofHypothesisRole,
    RecursiveProofNode,
    RecursiveProofPolicy,
    RecursiveProofReasonCode,
    monthly_to_4h_recursive_proof_policy,
)
from elliott_ai.lower_timeframe_subdivision_verifier import SubdivisionVerificationStatus


UTC = timezone.utc
ANALYSIS_CUTOFF = "2035-01-01T00:00:00+00:00"
ANALYSIS_HASH = canonical_sha256({"fixture": "recursive-proof-analysis"})
RESOLUTION_HASH = canonical_sha256({"fixture": "recursive-proof-resolution"})
_DEGREES = ("Cycle", "Primary", "Intermediate", "Minor")


def _dataset(timeframe: str, *, name: str, bar_count: int) -> DatasetCutoff:
    return DatasetCutoff(
        dataset_id=f"recursive-{name}-{timeframe}",
        source_reference=f"offline://recursive/{name}/{timeframe}",
        timeframe=timeframe,
        cutoff_utc=ANALYSIS_CUTOFF,
        dataset_hash=canonical_sha256({"fixture": "recursive", "name": name, "timeframe": timeframe, "bars": bar_count}),
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
        bar_count=bar_count,
    )


def _window(
    *,
    timeframe: str,
    points: tuple[tuple[datetime, float], ...],
    name: str,
) -> tuple[NativeOHLCVWindow, tuple[TypedPivot, ...]]:
    candles = tuple(
        NativeCandle.create(
            candle_id=f"{name}:{index}",
            timestamp_utc=timestamp.isoformat(timespec="seconds"),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=1000.0 + index,
        )
        for index, (timestamp, price) in enumerate(points)
    )
    window = NativeOHLCVWindow.create(
        window_id=f"window-{name}",
        dataset_cutoff=_dataset(timeframe, name=name, bar_count=len(candles)),
        timeframe=timeframe,
        candles=candles,
        coverage_role=WindowCoverageRole.REQUIRED_GENERATION,
        is_native=True,
    )
    return window, build_native_pivot_catalog(window, pivot_window=1)


def _pivot_for_point(
    catalog: tuple[TypedPivot, ...],
    window: NativeOHLCVWindow,
    prices: tuple[float, ...],
    index: int,
) -> TypedPivot:
    if index == 0:
        field = PivotPriceField.LOW if prices[1] > prices[0] else PivotPriceField.HIGH
    elif index == len(prices) - 1:
        field = PivotPriceField.HIGH if prices[index] > prices[index - 1] else PivotPriceField.LOW
    else:
        field = PivotPriceField.HIGH if prices[index] > prices[index - 1] and prices[index] > prices[index + 1] else PivotPriceField.LOW
    timestamp = window.candles[index].timestamp_utc
    return next(item for item in catalog if item.timestamp_utc == timestamp and item.price_field is field)


def _coverage(
    window: NativeOHLCVWindow,
    *,
    timeframe: str,
    start: TypedPivot,
    end: TypedPivot,
    complete: bool = True,
    missing_interval_ids: tuple[str, ...] = (),
) -> ProofCoverageAttestation:
    return ProofCoverageAttestation.create(
        attestation_id=f"coverage-{window.window_id}-{start.pivot_id}-{end.pivot_id}",
        proof_timeframe=timeframe,
        dataset_id=window.dataset_cutoff.dataset_id,
        source_window_hash=window.content_hash,
        interval_start_utc=start.timestamp_utc,
        interval_end_utc=end.timestamp_utc,
        coverage_complete=complete,
        missing_interval_ids=missing_interval_ids,
        coverage_method="offline_fixture_complete_native_rows",
    )


def _invalidation(start: TypedPivot, direction: str, *, suffix: str) -> CandidateInvalidation:
    return CandidateInvalidation(
        invalidation_id=f"invalidation-{suffix}",
        threshold_price=-1_000_000.0 if direction == "up" else 1_000_000.0,
        direction=(InvalidationDirection.BELOW if direction == "up" else InvalidationDirection.ABOVE),
        evaluation_basis=InvalidationEvaluationBasis.INTRABAR_TOUCH_OR_BREACH,
        source_pivot_id=start.pivot_id,
    )


def _child_layout(wave: ProofWaveSnapshot) -> tuple[tuple[str, ...], tuple[str, ...], tuple[float, ...]]:
    start = wave.start_pivot.price
    distance = wave.end_pivot.price - start
    if wave.declared_family == "impulse":
        return (
            ("1", "2", "3", "4", "5"),
            ("impulse", "zigzag", "impulse", "zigzag", "impulse"),
            (start, start + (0.25 * distance), start + (0.125 * distance), start + (0.75 * distance), start + (0.55 * distance), wave.end_pivot.price),
        )
    if wave.declared_family == "zigzag":
        return (
            ("A", "B", "C"),
            ("impulse", "zigzag", "impulse"),
            (start, start + (0.35 * distance), start + (0.15 * distance), wave.end_pivot.price),
        )
    raise ValueError("The recursive fixture only needs impulse and zigzag families.")


def _points_between(start: datetime, end: datetime, prices: tuple[float, ...]) -> tuple[tuple[datetime, float], ...]:
    span = end - start
    return tuple((start + (span * (index / (len(prices) - 1))), price) for index, price in enumerate(prices))


def _make_node(
    *,
    role: RecursiveProofHypothesisRole,
    policy: RecursiveProofPolicy,
    level: int,
    wave: ProofWaveSnapshot,
    native_window: NativeOHLCVWindow,
    native_catalog: tuple[TypedPivot, ...],
    native_coverage: ProofCoverageAttestation,
    name: str,
    nodes: list[RecursiveProofNode],
) -> RecursiveProofNode:
    timeframe = RECURSIVE_PROOF_TIMEFRAME_LADDER[level]
    if level == len(RECURSIVE_PROOF_TIMEFRAME_LADDER) - 1:
        node = RecursiveProofNode.create(
            node_id=f"{role.value}-{name}",
            wave=wave,
            proof_timeframe=timeframe,
            native_window=native_window,
            native_pivot_catalog=native_catalog,
            coverage=native_coverage,
        )
        nodes.append(node)
        return node

    positions, child_families, prices = _child_layout(wave)
    child_timeframe = RECURSIVE_PROOF_TIMEFRAME_LADDER[level + 1]
    points = _points_between(
        datetime.fromisoformat(wave.start_pivot.timestamp_utc),
        datetime.fromisoformat(wave.end_pivot.timestamp_utc),
        prices,
    )
    child_window, child_catalog = _window(
        timeframe=child_timeframe,
        points=points,
        name=f"{role.value}-{name}-{child_timeframe}",
    )
    pivots = tuple(_pivot_for_point(child_catalog, child_window, prices, index) for index in range(len(prices)))
    child_degree = _DEGREES[level + 1]
    graph_children = tuple(
        GeneratedChildWave(
            child_id=f"{role.value}-{name}-{position}",
            parent_candidate_id=wave.wave_id,
            degree=child_degree,
            timeframe=child_timeframe,
            sequence_position=position,
            direction="up" if pivots[index + 1].price > pivots[index].price else "down",
            declared_family=child_families[index],
            start_pivot=pivots[index],
            end_pivot=pivots[index + 1],
            invalidation=_invalidation(
                pivots[index],
                "up" if pivots[index + 1].price > pivots[index].price else "down",
                suffix=f"{role.value}-{name}-{position}",
            ),
        )
        for index, position in enumerate(positions)
    )
    request_id = f"request-{role.value}-{name}"
    graph = CandidateChildGraph.create(
        graph_id=f"graph-{role.value}-{name}",
        role=CandidateGraphRole(role.value),
        request_id=request_id,
        request_content_hash=canonical_sha256({"request_id": request_id, "parent": wave.content_hash}),
        parent_candidate_id=wave.wave_id,
        target_child_degree=child_degree,
        target_timeframe=child_timeframe,
        declared_family=wave.declared_family,
        children=graph_children,
        proof_scope=CandidateProofScope(
            target_timeframe=child_timeframe,
            verified_to_timeframe=None,
            terminal_degree="Minor",
            coverage_limitations=(),
        ),
        provider="offline_candidate_fixture",
        model=None,
        generated_at_utc=ANALYSIS_CUTOFF,
        policy_version="offline-recursive-fixture-v1",
    )
    child_nodes: list[RecursiveProofNode] = []
    for index, graph_child in enumerate(graph.children):
        child_wave = ProofWaveSnapshot.create(
            wave_id=graph_child.child_id,
            degree=graph_child.degree,
            declared_family=graph_child.declared_family,
            direction=graph_child.direction,
            completion_state="completed",
            start_pivot=graph_child.start_pivot,
            end_pivot=graph_child.end_pivot,
            invalidation=graph_child.invalidation,
            source_reference_hash=graph.content_hash,
            source_kind="offline_candidate_graph",
        )
        child_nodes.append(
            _make_node(
                role=role,
                policy=policy,
                level=level + 1,
                wave=child_wave,
                native_window=child_window,
                native_catalog=child_catalog,
                native_coverage=_coverage(
                    child_window,
                    timeframe=child_timeframe,
                    start=child_wave.start_pivot,
                    end=child_wave.end_pivot,
                ),
                name=f"{name}-{positions[index]}",
                nodes=nodes,
            )
        )
    node = RecursiveProofNode.create(
        node_id=f"{role.value}-{name}",
        wave=wave,
        proof_timeframe=timeframe,
        native_window=native_window,
        native_pivot_catalog=native_catalog,
        coverage=native_coverage,
        child_graph=graph,
        child_window=child_window,
        child_pivot_catalog=child_catalog,
        child_coverage=_coverage(
            child_window,
            timeframe=child_timeframe,
            start=wave.start_pivot,
            end=wave.end_pivot,
        ),
        child_node_ids=tuple(item.node_id for item in child_nodes),
    )
    nodes.append(node)
    return node


def build_bundle(
    *,
    role: RecursiveProofHypothesisRole = RecursiveProofHypothesisRole.PRIMARY,
    root_family: str = "impulse",
    policy: RecursiveProofPolicy | None = None,
) -> RecursiveProofBundle:
    policy = policy or monthly_to_4h_recursive_proof_policy()
    start = datetime(2000, 1, 1, tzinfo=UTC)
    end = datetime(2030, 1, 1, tzinfo=UTC)
    root_prices = (100.0, 300.0)
    root_window, root_catalog = _window(
        timeframe="monthly",
        points=((start, root_prices[0]), (end, root_prices[1])),
        name=f"{role.value}-root-monthly",
    )
    start_pivot = _pivot_for_point(root_catalog, root_window, root_prices, 0)
    end_pivot = _pivot_for_point(root_catalog, root_window, root_prices, 1)
    root_wave = ProofWaveSnapshot.create(
        wave_id=f"{role.value}-root-wave",
        degree="Cycle",
        declared_family=root_family,
        direction="up",
        completion_state="completed",
        start_pivot=start_pivot,
        end_pivot=end_pivot,
        invalidation=_invalidation(start_pivot, "up", suffix=f"{role.value}-root"),
        source_reference_hash=canonical_sha256({"source": role.value, "root": root_family}),
        source_kind="offline_frozen_parent",
    )
    nodes: list[RecursiveProofNode] = []
    root = _make_node(
        role=role,
        policy=policy,
        level=0,
        wave=root_wave,
        native_window=root_window,
        native_catalog=root_catalog,
        native_coverage=_coverage(root_window, timeframe="monthly", start=start_pivot, end=end_pivot),
        name="root",
        nodes=nodes,
    )
    return RecursiveProofBundle.create(
        bundle_id=f"bundle-{role.value}-{root_family}",
        role=role,
        source_analysis_run_id=21,
        source_analysis_run_hash=ANALYSIS_HASH,
        source_degree_resolution_id=6,
        source_degree_resolution_hash=RESOLUTION_HASH,
        analysis_cutoff_utc=ANALYSIS_CUTOFF,
        root_node_id=root.node_id,
        nodes=tuple(nodes),
        policy=policy,
        shadow_mode=True,
        created_at_utc=ANALYSIS_CUTOFF,
    )


def rebuild_bundle(bundle: RecursiveProofBundle, *, nodes: tuple[RecursiveProofNode, ...] | None = None, policy: RecursiveProofPolicy | None = None) -> RecursiveProofBundle:
    return RecursiveProofBundle.create(
        bundle_id=f"{bundle.bundle_id}-rebuild-{canonical_sha256({'nodes': [item.content_hash for item in (nodes or bundle.nodes)], 'policy': (policy or bundle.policy).content_hash})[:10]}",
        role=bundle.role,
        source_analysis_run_id=bundle.source_analysis_run_id,
        source_analysis_run_hash=bundle.source_analysis_run_hash,
        source_degree_resolution_id=bundle.source_degree_resolution_id,
        source_degree_resolution_hash=bundle.source_degree_resolution_hash,
        analysis_cutoff_utc=bundle.analysis_cutoff_utc,
        root_node_id=bundle.root_node_id,
        nodes=nodes or bundle.nodes,
        policy=policy or bundle.policy,
        shadow_mode=True,
        created_at_utc=bundle.created_at_utc,
    )


class RecursiveMultiTimeframeProofTests(unittest.TestCase):
    def test_full_monthly_weekly_daily_4h_tree_verifies_bottom_up(self) -> None:
        bundle = build_bundle()

        result = RecursiveMultiTimeframeProofVerifier().verify(bundle, shadow_mode=True)

        self.assertGreater(len(bundle.nodes), 100)
        self.assertEqual(bundle.policy.timeframe_ladder, ("monthly", "weekly", "daily", "4h"))
        self.assertEqual(result.status, SubdivisionVerificationStatus.VERIFIED)
        by_timeframe = {timeframe: [] for timeframe in RECURSIVE_PROOF_TIMEFRAME_LADDER}
        for item in result.node_results:
            by_timeframe[item.proof_timeframe].append(item)
            self.assertEqual(item.status, SubdivisionVerificationStatus.VERIFIED)
            self.assertEqual(item.verified_to_timeframe, item.proof_timeframe)
        self.assertEqual(by_timeframe["4h"][0].reason_codes, (RecursiveProofReasonCode.TERMINAL_4H_VERIFIED,))
        self.assertTrue(by_timeframe["daily"])
        self.assertTrue(by_timeframe["weekly"])
        self.assertTrue(by_timeframe["monthly"])

    def test_proof_timeframe_is_independent_of_elliott_degree(self) -> None:
        bundle = build_bundle()
        root = next(item for item in bundle.nodes if item.node_id == bundle.root_node_id)
        weekly = next(item for item in bundle.nodes if item.proof_timeframe == "weekly")

        self.assertEqual(root.wave.degree, "Cycle")
        self.assertEqual(root.proof_timeframe, "monthly")
        self.assertEqual(weekly.wave.degree, "Primary")
        self.assertEqual(weekly.proof_timeframe, "weekly")
        self.assertEqual(RecursiveMultiTimeframeProofVerifier().verify(bundle, shadow_mode=True).status, SubdivisionVerificationStatus.VERIFIED)

    def test_recursive_corrective_zigzag_tree_uses_the_same_bottom_up_ladder(self) -> None:
        bundle = build_bundle(root_family="zigzag")

        result = RecursiveMultiTimeframeProofVerifier().verify(bundle, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.VERIFIED)
        self.assertEqual(next(item for item in bundle.nodes if item.node_id == bundle.root_node_id).wave.declared_family, "zigzag")

    def test_monthly_structure_without_required_weekly_descendants_is_unproven(self) -> None:
        bundle = build_bundle()
        root = next(item for item in bundle.nodes if item.node_id == bundle.root_node_id)
        monthly_only = RecursiveProofNode.create(
            node_id=root.node_id,
            wave=root.wave,
            proof_timeframe="monthly",
            native_window=root.native_window,
            native_pivot_catalog=root.native_pivot_catalog,
            coverage=root.coverage,
        )
        altered = rebuild_bundle(
            bundle,
            nodes=tuple(monthly_only if item.node_id == root.node_id else item for item in bundle.nodes),
        )

        result = RecursiveMultiTimeframeProofVerifier().verify(altered, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.UNPROVEN)
        self.assertIn(RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING, result.reason_codes)

    def test_missing_weekly_graph_with_explicit_coverage_gap_is_not_covered(self) -> None:
        bundle = build_bundle()
        root = next(item for item in bundle.nodes if item.node_id == bundle.root_node_id)
        assert root.child_window is not None
        unavailable_root = RecursiveProofNode.create(
            node_id=root.node_id,
            wave=root.wave,
            proof_timeframe=root.proof_timeframe,
            native_window=root.native_window,
            native_pivot_catalog=root.native_pivot_catalog,
            coverage=root.coverage,
            child_window=root.child_window,
            child_pivot_catalog=root.child_pivot_catalog,
            child_coverage=_coverage(
                root.child_window,
                timeframe="weekly",
                start=root.wave.start_pivot,
                end=root.wave.end_pivot,
                complete=False,
                missing_interval_ids=("fixture-weekly-history-gap",),
            ),
        )
        altered = rebuild_bundle(
            bundle,
            nodes=tuple(unavailable_root if item.node_id == root.node_id else item for item in bundle.nodes),
        )

        result = RecursiveMultiTimeframeProofVerifier().verify(altered, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.NOT_COVERED)
        self.assertIn(RecursiveProofReasonCode.COVERAGE_INCOMPLETE, result.reason_codes)

    def test_primary_and_alternative_bundles_are_isolated(self) -> None:
        primary = build_bundle(role=RecursiveProofHypothesisRole.PRIMARY)
        alternative = build_bundle(role=RecursiveProofHypothesisRole.ALTERNATIVE)
        missing_leaf = next(item for item in alternative.nodes if item.proof_timeframe == "4h")
        altered_nodes = tuple(item for item in alternative.nodes if item.node_id != missing_leaf.node_id)
        alternative = rebuild_bundle(alternative, nodes=altered_nodes)
        pair = RecursiveProofBundlePair.create(
            pair_id="pair-primary-alternative-isolation",
            primary_bundle=primary,
            alternative_bundle=alternative,
            created_at_utc=ANALYSIS_CUTOFF,
        )

        result = RecursiveMultiTimeframeProofVerifier().verify_pair(pair, shadow_mode=True)

        self.assertEqual(result.primary_result.status, SubdivisionVerificationStatus.VERIFIED)
        self.assertEqual(result.alternative_result.status, SubdivisionVerificationStatus.UNPROVEN)
        self.assertIn(RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING, result.alternative_result.reason_codes)
        self.assertNotEqual(result.primary_result.content_hash, result.alternative_result.content_hash)

    def test_missing_compatible_native_history_propagates_not_covered(self) -> None:
        bundle = build_bundle()
        leaf = next(item for item in bundle.nodes if item.proof_timeframe == "4h")
        missing_leaf = RecursiveProofNode.create(
            node_id=leaf.node_id,
            wave=leaf.wave,
            proof_timeframe=leaf.proof_timeframe,
            native_window=None,
            native_pivot_catalog=(),
            coverage=None,
        )
        altered = rebuild_bundle(
            bundle,
            nodes=tuple(missing_leaf if item.node_id == leaf.node_id else item for item in bundle.nodes),
        )

        result = RecursiveMultiTimeframeProofVerifier().verify(altered, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.NOT_COVERED)
        self.assertIn(RecursiveProofReasonCode.REQUIRED_NATIVE_WINDOW_MISSING, result.reason_codes)

    def test_missing_coverage_attestation_is_not_silently_treated_as_complete(self) -> None:
        bundle = build_bundle()
        leaf = next(item for item in bundle.nodes if item.proof_timeframe == "4h")
        unproven_leaf = RecursiveProofNode.create(
            node_id=leaf.node_id,
            wave=leaf.wave,
            proof_timeframe=leaf.proof_timeframe,
            native_window=leaf.native_window,
            native_pivot_catalog=leaf.native_pivot_catalog,
            coverage=None,
        )
        altered = rebuild_bundle(
            bundle,
            nodes=tuple(unproven_leaf if item.node_id == leaf.node_id else item for item in bundle.nodes),
        )

        result = RecursiveMultiTimeframeProofVerifier().verify(altered, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.NOT_COVERED)
        self.assertIn(RecursiveProofReasonCode.REQUIRED_COVERAGE_ATTESTATION_MISSING, result.reason_codes)

    def test_missing_descendant_and_skipped_ladder_step_are_unproven(self) -> None:
        bundle = build_bundle()
        root = next(item for item in bundle.nodes if item.node_id == bundle.root_node_id)
        graph = root.child_graph
        assert graph is not None
        skipped_children = tuple(replace(item, timeframe="daily") for item in graph.children)
        skipped_graph = CandidateChildGraph.create(
            graph_id="root-skipped-graph",
            role=graph.role,
            request_id=graph.request_id,
            request_content_hash=graph.request_content_hash,
            parent_candidate_id=graph.parent_candidate_id,
            target_child_degree=graph.target_child_degree,
            target_timeframe="daily",
            declared_family=graph.declared_family,
            children=skipped_children,
            proof_scope=CandidateProofScope(
                target_timeframe="daily",
                verified_to_timeframe=None,
                terminal_degree=graph.proof_scope.terminal_degree,
                coverage_limitations=(),
            ),
            provider=graph.provider,
            model=graph.model,
            generated_at_utc=graph.generated_at_utc,
            policy_version=graph.policy_version,
        )
        skipped_root = RecursiveProofNode.create(
            node_id=root.node_id,
            wave=root.wave,
            proof_timeframe=root.proof_timeframe,
            native_window=root.native_window,
            native_pivot_catalog=root.native_pivot_catalog,
            coverage=root.coverage,
            child_graph=skipped_graph,
            child_window=root.child_window,
            child_pivot_catalog=root.child_pivot_catalog,
            child_coverage=root.child_coverage,
            child_node_ids=root.child_node_ids,
        )
        altered = rebuild_bundle(
            bundle,
            nodes=tuple(skipped_root if item.node_id == root.node_id else item for item in bundle.nodes),
        )

        result = RecursiveMultiTimeframeProofVerifier().verify(altered, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.UNPROVEN)
        self.assertIn(RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED, result.reason_codes)

    def test_node_limit_is_per_hypothesis_and_never_verifies(self) -> None:
        valid = build_bundle()
        capped_policy = RecursiveProofPolicy.create(
            policy_id="monthly-weekly-daily-native-4h-cap-test",
            timeframe_ladder=RECURSIVE_PROOF_TIMEFRAME_LADDER,
            maximum_nodes_per_hypothesis=len(valid.nodes) - 1,
            minimum_native_bars=2,
        )
        capped = rebuild_bundle(valid, policy=capped_policy)

        verifier = RecursiveMultiTimeframeProofVerifier()
        valid_result = verifier.verify(valid, shadow_mode=True)
        capped_result = verifier.verify(capped, shadow_mode=True)

        self.assertEqual(valid_result.status, SubdivisionVerificationStatus.VERIFIED)
        self.assertEqual(capped_result.status, SubdivisionVerificationStatus.UNPROVEN)
        self.assertIn(RecursiveProofReasonCode.NODE_LIMIT_EXHAUSTED, capped_result.reason_codes)

    def test_hard_child_direction_rule_propagates_inconsistency(self) -> None:
        bundle = build_bundle()
        daily = next(item for item in bundle.nodes if item.proof_timeframe == "daily")
        graph = daily.child_graph
        assert graph is not None
        bad_second = replace(graph.children[1], direction="up")
        bad_graph = CandidateChildGraph.create(
            graph_id="daily-bad-direction",
            role=graph.role,
            request_id=graph.request_id,
            request_content_hash=graph.request_content_hash,
            parent_candidate_id=graph.parent_candidate_id,
            target_child_degree=graph.target_child_degree,
            target_timeframe=graph.target_timeframe,
            declared_family=graph.declared_family,
            children=(graph.children[0], bad_second, *graph.children[2:]),
            proof_scope=graph.proof_scope,
            provider=graph.provider,
            model=graph.model,
            generated_at_utc=graph.generated_at_utc,
            policy_version=graph.policy_version,
        )
        bad_daily = RecursiveProofNode.create(
            node_id=daily.node_id,
            wave=daily.wave,
            proof_timeframe=daily.proof_timeframe,
            native_window=daily.native_window,
            native_pivot_catalog=daily.native_pivot_catalog,
            coverage=daily.coverage,
            child_graph=bad_graph,
            child_window=daily.child_window,
            child_pivot_catalog=daily.child_pivot_catalog,
            child_coverage=daily.child_coverage,
            child_node_ids=daily.child_node_ids,
        )
        altered = rebuild_bundle(
            bundle,
            nodes=tuple(bad_daily if item.node_id == daily.node_id else item for item in bundle.nodes),
        )

        result = RecursiveMultiTimeframeProofVerifier().verify(altered, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(RecursiveProofReasonCode.DIRECTION_SEQUENCE_INVALID, result.reason_codes)

    def test_disconnected_child_boundaries_are_inconsistent_at_the_parent(self) -> None:
        bundle = build_bundle()
        root = next(item for item in bundle.nodes if item.node_id == bundle.root_node_id)
        graph = root.child_graph
        assert graph is not None
        disconnected_second = replace(graph.children[1], start_pivot=graph.children[0].start_pivot)
        bad_graph = CandidateChildGraph.create(
            graph_id="root-disconnected-boundaries",
            role=graph.role,
            request_id=graph.request_id,
            request_content_hash=graph.request_content_hash,
            parent_candidate_id=graph.parent_candidate_id,
            target_child_degree=graph.target_child_degree,
            target_timeframe=graph.target_timeframe,
            declared_family=graph.declared_family,
            children=(graph.children[0], disconnected_second, *graph.children[2:]),
            proof_scope=graph.proof_scope,
            provider=graph.provider,
            model=graph.model,
            generated_at_utc=graph.generated_at_utc,
            policy_version=graph.policy_version,
        )
        broken_root = RecursiveProofNode.create(
            node_id=root.node_id,
            wave=root.wave,
            proof_timeframe=root.proof_timeframe,
            native_window=root.native_window,
            native_pivot_catalog=root.native_pivot_catalog,
            coverage=root.coverage,
            child_graph=bad_graph,
            child_window=root.child_window,
            child_pivot_catalog=root.child_pivot_catalog,
            child_coverage=root.child_coverage,
            child_node_ids=root.child_node_ids,
        )
        altered = rebuild_bundle(
            bundle,
            nodes=tuple(broken_root if item.node_id == root.node_id else item for item in bundle.nodes),
        )

        result = RecursiveMultiTimeframeProofVerifier().verify(altered, shadow_mode=True)

        self.assertEqual(result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(RecursiveProofReasonCode.CHILD_PIVOT_CHAIN_DISCONTINUOUS, result.reason_codes)

    def test_candidate_graph_cannot_carry_or_override_a_verification_status(self) -> None:
        bundle = build_bundle()
        root = next(item for item in bundle.nodes if item.node_id == bundle.root_node_id)
        assert root.child_graph is not None
        raw = root.child_graph.to_dict()
        raw["proof_scope"]["verified_to_timeframe"] = "4h"

        with self.assertRaises(ValueError):
            CandidateChildGraph.from_dict(raw)
        self.assertNotIn("status", ProofWaveSnapshot.from_dict(root.wave.to_dict()).to_dict())
        self.assertNotIn("provider", inspect.signature(RecursiveMultiTimeframeProofVerifier.verify).parameters)

    def test_hash_round_trip_and_tamper_detection_are_deterministic(self) -> None:
        bundle = build_bundle()
        restored = RecursiveProofBundle.from_dict(bundle.to_dict())
        tampered = bundle.to_dict()
        tampered["analysis_cutoff_utc"] = "2035-01-02T00:00:00+00:00"

        self.assertEqual(restored, bundle)
        with self.assertRaises(ValueError):
            RecursiveProofBundle.from_dict(tampered)

    def test_shadow_mode_is_required_at_bundle_and_call_boundaries(self) -> None:
        bundle = build_bundle()

        result = RecursiveMultiTimeframeProofVerifier().verify(bundle, shadow_mode=False)

        self.assertEqual(result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(RecursiveProofReasonCode.SHADOW_MODE_REQUIRED, result.reason_codes)


if __name__ == "__main__":
    unittest.main()
