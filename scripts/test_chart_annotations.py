"""Focused tests for immutable chart plans and deterministic Pine rendering."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from elliott_ai.chart_annotations import (
    AnnotationStatus,
    ChartAnnotationPlan,
    ChartHypothesis,
    ChartLabelDraft,
    ChartLayer,
    ChartPattern,
    ChartPivot,
    ChartSourceMetadata,
    HypothesisRole,
    LabelPlacement,
    LabelSize,
    PatternGeometry,
    PatternKind,
    PivotKind,
    WaveDegree,
    chart_annotation_plan_content_hash,
)
from elliott_ai.pine_overlay_renderer import (
    PineOverlayRenderResult,
    render_pine_overlay_source,
    status_safe_label_text,
    write_pine_overlay,
)
from scripts.render_chart_annotation_plan import main as render_main


DATA_HASH = "a" * 64
VERIFICATION_HASH = "b" * 64


def source_metadata(*, cutoff: str = "2026-08-25T20:00:00Z") -> ChartSourceMetadata:
    return ChartSourceMetadata(
        symbol="NASDAQ:SSYS",
        exchange="NASDAQ",
        provider="twelve_data",
        feed="NASDAQ regular session",
        session="regular",
        timezone="America/New_York",
        adjustment="split-adjusted, dividend-unadjusted",
        price_basis="adjusted OHLC with corresponding volume",
        cutoff=cutoff,
        dataset_hashes={"15m": DATA_HASH},
    )


def pivots() -> tuple[ChartPivot, ...]:
    values = (
        ("a1", "2026-06-09T16:45:00Z", 8.31, PivotKind.LOW),
        ("a2", "2026-06-15T13:30:00Z", 9.35, PivotKind.HIGH),
        ("a3", "2026-06-29T14:15:00Z", 7.985, PivotKind.LOW),
        ("a4", "2026-07-09T18:15:00Z", 8.95, PivotKind.HIGH),
        ("a5", "2026-07-29T16:15:00Z", 7.59, PivotKind.LOW),
    )
    return tuple(
        ChartPivot(
            pivot_id=pivot_id,
            timestamp=timestamp,
            price=price,
            kind=kind,
            source_timeframe="15m",
            source_dataset_hash=DATA_HASH,
        )
        for pivot_id, timestamp, price, kind in values
    )


def layers() -> tuple[ChartLayer, ...]:
    return (
        ChartLayer(
            layer_id="current_minor",
            display_name="Current minor structure",
            detail_view="Current",
            min_timeframe="15",
            max_timeframe="240",
            label_color="#D97706",
        ),
    )


def hypotheses() -> tuple[ChartHypothesis, ...]:
    return (
        ChartHypothesis(
            hypothesis_id="primary_count",
            display_name="Primary",
            role=HypothesisRole.PRIMARY,
        ),
        ChartHypothesis(
            hypothesis_id="alternative_count",
            display_name="Alternative",
            role=HypothesisRole.ALTERNATIVE,
        ),
    )


def label_drafts() -> tuple[ChartLabelDraft, ...]:
    drafts: list[ChartLabelDraft] = []
    for index, pivot_id in enumerate(("a1", "a2", "a3", "a4", "a5"), 1):
        drafts.append(
            ChartLabelDraft(
                label_id=f"primary_wave_{index}",
                pivot_id=pivot_id,
                layer_id="current_minor",
                text=str(index),
                degree=WaveDegree.MINUTE,
                placement=(
                    LabelPlacement.BELOW if index % 2 else LabelPlacement.ABOVE
                ),
                status=AnnotationStatus.CANDIDATE,
                hypothesis_role=HypothesisRole.PRIMARY,
                size=LabelSize.TINY,
                stack_group="active_a_low" if index == 5 else None,
            )
        )
    drafts.extend(
        (
            ChartLabelDraft(
                label_id="primary_parent_a",
                pivot_id="a5",
                layer_id="current_minor",
                text="A",
                degree=WaveDegree.INTERMEDIATE,
                placement=LabelPlacement.BELOW,
                status=AnnotationStatus.UNPROVEN,
                hypothesis_role=HypothesisRole.PRIMARY,
                stack_group="active_a_low",
            ),
            ChartLabelDraft(
                label_id="alternative_parent_w",
                pivot_id="a5",
                layer_id="current_minor",
                text="W",
                degree=WaveDegree.INTERMEDIATE,
                placement=LabelPlacement.BELOW,
                status=AnnotationStatus.UNPROVEN,
                hypothesis_role=HypothesisRole.ALTERNATIVE,
                stack_group="active_a_low",
            ),
        )
    )
    return tuple(drafts)


def diagonal_pattern(
    *,
    status: AnnotationStatus = AnnotationStatus.CANDIDATE,
) -> ChartPattern:
    kwargs: dict[str, object] = {}
    if status is AnnotationStatus.VERIFIED:
        kwargs = {
            "verification_authority": "deterministic",
            "verification_reference": "deterministic://subdivision-verifier/result-1",
            "verification_hash": VERIFICATION_HASH,
        }
    return ChartPattern(
        pattern_id="active_a_diagonal",
        layer_id="current_minor",
        kind=PatternKind.DIAGONAL,
        geometry=PatternGeometry.ALTERNATING_BOUNDARIES,
        pivot_ids=("a1", "a2", "a3", "a4", "a5"),
        status=status,
        hypothesis_role=HypothesisRole.PRIMARY,
        line_color="#475569",
        **kwargs,
    )


def build_plan(
    *, patterns: tuple[ChartPattern, ...] | None = None
) -> ChartAnnotationPlan:
    return ChartAnnotationPlan.create(
        plan_id="ssys_active_annotation_test",
        title="SSYS Deterministic Annotation Test",
        source=source_metadata(),
        hypotheses=hypotheses(),
        layers=layers(),
        pivots=pivots(),
        label_drafts=label_drafts(),
        patterns=(diagonal_pattern(),) if patterns is None else patterns,
    )


class ChartAnnotationContractTests(unittest.TestCase):
    def test_contract_is_immutable_hashed_and_round_trips(self) -> None:
        plan = build_plan()
        self.assertEqual(plan.content_hash, chart_annotation_plan_content_hash(plan))
        restored = ChartAnnotationPlan.from_dict(plan.to_dict())
        self.assertEqual(restored, plan)
        with self.assertRaises(FrozenInstanceError):
            plan.title = "changed"  # type: ignore[misc]

    def test_tampering_is_detected(self) -> None:
        payload = build_plan().to_dict()
        payload["labels"][0]["text"] = "tampered"
        with self.assertRaisesRegex(ValueError, "content_hash"):
            ChartAnnotationPlan.from_dict(payload)

    def test_future_pivot_and_unbound_dataset_hash_are_rejected(self) -> None:
        future = ChartPivot(
            pivot_id="future",
            timestamp="2026-08-26T13:30:00Z",
            price=7.8,
            kind=PivotKind.HIGH,
            source_timeframe="15m",
            source_dataset_hash=DATA_HASH,
        )
        with self.assertRaisesRegex(ValueError, "after the plan cutoff"):
            ChartAnnotationPlan.create(
                plan_id="future_test",
                title="Future test",
                source=source_metadata(),
                hypotheses=hypotheses(),
                layers=layers(),
                pivots=(*pivots(), future),
                label_drafts=(),
            )

        wrong_hash_pivot = ChartPivot(
            pivot_id="wrong_hash",
            timestamp="2026-08-20T13:30:00Z",
            price=8.0,
            kind=PivotKind.LOW,
            source_timeframe="15m",
            source_dataset_hash="c" * 64,
        )
        with self.assertRaisesRegex(ValueError, "source dataset hashes"):
            ChartAnnotationPlan.create(
                plan_id="hash_test",
                title="Hash test",
                source=source_metadata(),
                hypotheses=hypotheses(),
                layers=layers(),
                pivots=(*pivots(), wrong_hash_pivot),
                label_drafts=(),
            )

    def test_unknown_label_and_pattern_references_are_rejected(self) -> None:
        bad_label = ChartLabelDraft(
            label_id="bad_label",
            pivot_id="missing",
            layer_id="current_minor",
            text="1",
            degree=WaveDegree.MINUTE,
            placement=LabelPlacement.BELOW,
            status=AnnotationStatus.CANDIDATE,
        )
        with self.assertRaisesRegex(ValueError, "unknown pivot"):
            ChartAnnotationPlan.create(
                plan_id="bad_label_plan",
                title="Bad label plan",
                source=source_metadata(),
                hypotheses=hypotheses(),
                layers=layers(),
                pivots=pivots(),
                label_drafts=(bad_label,),
            )

        bad_pattern_payload = diagonal_pattern().to_dict()
        bad_pattern_payload["pivot_ids"][-1] = "missing"
        bad_pattern = ChartPattern.from_dict(bad_pattern_payload)
        with self.assertRaisesRegex(ValueError, "unknown pivot"):
            ChartAnnotationPlan.create(
                plan_id="bad_pattern_plan",
                title="Bad pattern plan",
                source=source_metadata(),
                hypotheses=hypotheses(),
                layers=layers(),
                pivots=pivots(),
                label_drafts=(),
                patterns=(bad_pattern,),
            )

    def test_auto_stacking_places_lower_degree_nearer_the_candle(self) -> None:
        plan = build_plan()
        stacked = {
            label.label_id: label.stack_order
            for label in plan.labels
            if label.stack_group == "active_a_low"
        }
        self.assertEqual(stacked["primary_wave_5"], 0)
        self.assertEqual(stacked["alternative_parent_w"], 1)
        self.assertEqual(stacked["primary_parent_a"], 2)

    def test_duplicate_explicit_stack_order_is_rejected(self) -> None:
        first = ChartLabelDraft(
            label_id="first",
            pivot_id="a5",
            layer_id="current_minor",
            text="1",
            degree=WaveDegree.MINUTE,
            placement=LabelPlacement.BELOW,
            status=AnnotationStatus.CANDIDATE,
            stack_group="same_candle",
            stack_order=0,
        )
        second = ChartLabelDraft(
            label_id="second",
            pivot_id="a5",
            layer_id="current_minor",
            text="A",
            degree=WaveDegree.INTERMEDIATE,
            placement=LabelPlacement.BELOW,
            status=AnnotationStatus.CANDIDATE,
            stack_group="same_candle",
            stack_order=0,
        )
        with self.assertRaisesRegex(ValueError, "Duplicate explicit stack order"):
            ChartAnnotationPlan.create(
                plan_id="duplicate_stack",
                title="Duplicate stack",
                source=source_metadata(),
                hypotheses=hypotheses(),
                layers=layers(),
                pivots=pivots(),
                label_drafts=(first, second),
            )

    def test_nonverified_text_cannot_claim_verified_or_confirmed(self) -> None:
        for text in ("Wave 5 verified", "Confirmed diagonal"):
            with self.subTest(text=text), self.assertRaisesRegex(
                ValueError, "cannot claim"
            ):
                ChartLabelDraft(
                    label_id="unsafe_label",
                    pivot_id="a5",
                    layer_id="current_minor",
                    text=text,
                    degree=WaveDegree.MINUTE,
                    placement=LabelPlacement.BELOW,
                    status=AnnotationStatus.UNPROVEN,
                )

    def test_verified_pattern_requires_deterministic_verification_lineage(self) -> None:
        with self.assertRaisesRegex(ValueError, "verification_authority"):
            ChartPattern(
                pattern_id="unsafe_verified",
                layer_id="current_minor",
                kind=PatternKind.DIAGONAL,
                geometry=PatternGeometry.ALTERNATING_BOUNDARIES,
                pivot_ids=("a1", "a2", "a3", "a4", "a5"),
                status=AnnotationStatus.VERIFIED,
                hypothesis_role=HypothesisRole.PRIMARY,
                line_color="#475569",
            )
        verified = diagonal_pattern(status=AnnotationStatus.VERIFIED)
        self.assertEqual(verified.verification_authority, "deterministic")

    def test_pattern_shape_and_order_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly five"):
            ChartPattern(
                pattern_id="short_diagonal",
                layer_id="current_minor",
                kind=PatternKind.DIAGONAL,
                geometry=PatternGeometry.ALTERNATING_BOUNDARIES,
                pivot_ids=("a1", "a2", "a3", "a4"),
                status=AnnotationStatus.CANDIDATE,
                hypothesis_role=HypothesisRole.PRIMARY,
                line_color="#475569",
            )
        reversed_pattern = ChartPattern(
            pattern_id="reversed_diagonal",
            layer_id="current_minor",
            kind=PatternKind.DIAGONAL,
            geometry=PatternGeometry.ALTERNATING_BOUNDARIES,
            pivot_ids=("a5", "a4", "a3", "a2", "a1"),
            status=AnnotationStatus.CANDIDATE,
            hypothesis_role=HypothesisRole.PRIMARY,
            line_color="#475569",
        )
        with self.assertRaisesRegex(ValueError, "strictly time-ordered"):
            build_plan(patterns=(reversed_pattern,))

    def test_source_timezone_and_plan_schema_are_strict(self) -> None:
        payload = source_metadata().to_dict()
        payload["timezone"] = "Not/A_Timezone"
        with self.assertRaisesRegex(ValueError, "IANA timezone"):
            ChartSourceMetadata.from_dict(payload)
        plan_payload = build_plan().to_dict()
        plan_payload["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "unknown"):
            ChartAnnotationPlan.from_dict(plan_payload)


class PineOverlayRendererTests(unittest.TestCase):
    def test_render_is_deterministic_and_keeps_hypotheses_separate(self) -> None:
        plan = build_plan()
        first = PineOverlayRenderResult.create(plan)
        second = PineOverlayRenderResult.create(plan)
        self.assertEqual(first, second)
        self.assertEqual(PineOverlayRenderResult.from_dict(first.to_dict()), first)
        self.assertIn('countView = input.string("Primary", "Count"', first.pine_source)
        self.assertIn("bool showPrimary", first.pine_source)
        self.assertIn("bool showAlternative", first.pine_source)

    def test_candidates_are_question_marked_and_pattern_lines_are_dashed(self) -> None:
        source = render_pine_overlay_source(build_plan())
        self.assertIn('"1?"', source)
        self.assertIn('"A?"', source)
        self.assertIn('"W?"', source)
        self.assertIn(
            "string patternStyle = verified ? line.style_solid : line.style_dashed",
            source,
        )
        self.assertIn("addProjectedBoundary", source)
        self.assertNotIn("report_prose", source)

    def test_intraday_layer_remains_visible_from_15_minutes_through_4_hours(self) -> None:
        source = render_pine_overlay_source(build_plan())
        self.assertIn('chartSeconds >= timeframe.in_seconds("15")', source)
        self.assertIn('chartSeconds <= timeframe.in_seconds("240")', source)

    def test_verified_pattern_renders_solid_capability_without_model_authority(self) -> None:
        plan = build_plan(patterns=(diagonal_pattern(status=AnnotationStatus.VERIFIED),))
        source = render_pine_overlay_source(plan)
        pattern_lines = [line for line in source.splitlines() if "addProjectedBoundary(" in line]
        self.assertTrue(any(", true, 2)" in line for line in pattern_lines))

    def test_channel_uses_explicit_parallel_geometry(self) -> None:
        channel = ChartPattern(
            pattern_id="candidate_channel",
            layer_id="current_minor",
            kind=PatternKind.CHANNEL,
            geometry=PatternGeometry.ONE_THREE_PARALLEL_TWO,
            pivot_ids=("a1", "a2", "a3", "a4", "a5"),
            status=AnnotationStatus.CANDIDATE,
            hypothesis_role=HypothesisRole.ALTERNATIVE,
            line_color="#475569",
        )
        source = render_pine_overlay_source(build_plan(patterns=(channel,)))
        self.assertIn("addParallelBoundary", source)
        self.assertIn("showAlternative", source)

    def test_only_patterns_create_lines_not_ordinary_wave_labels(self) -> None:
        without_patterns = render_pine_overlay_source(build_plan(patterns=()))
        calls = [
            line.strip()
            for line in without_patterns.splitlines()
            if (
                line.strip().startswith("addProjectedBoundary(")
                or line.strip().startswith("addParallelBoundary(")
            )
            and not line.strip().endswith("=>")
        ]
        self.assertEqual(calls, [])
        self.assertGreater(without_patterns.count("addPivot("), 1)

    def test_status_safe_label_text_does_not_duplicate_question_mark(self) -> None:
        label = next(item for item in build_plan().labels if item.label_id == "primary_wave_1")
        self.assertEqual(status_safe_label_text(label), "1?")
        payload = label.to_dict()
        payload["text"] = "1?"
        from elliott_ai.chart_annotations import ChartLabel

        self.assertEqual(status_safe_label_text(ChartLabel.from_dict(payload)), "1?")

    def test_atomic_writer_refuses_different_overwrite(self) -> None:
        result = PineOverlayRenderResult.create(build_plan())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "overlay.pine"
            self.assertEqual(write_pine_overlay(result, output), output)
            self.assertEqual(write_pine_overlay(result, output), output)
            output.write_text("different", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                write_pine_overlay(result, output)
            write_pine_overlay(result, output, overwrite=True)
            self.assertEqual(output.read_text(encoding="utf-8"), result.pine_source + "\n")

    def test_standalone_render_script_validates_and_writes(self) -> None:
        plan = build_plan()
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            output_path = Path(directory) / "overlay.pine"
            plan_path.write_text(
                json.dumps(plan.to_dict(), ensure_ascii=True, sort_keys=True),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = render_main((str(plan_path), str(output_path)))
            self.assertEqual(exit_code, 0)
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["plan_content_hash"], plan.content_hash)
            self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()
