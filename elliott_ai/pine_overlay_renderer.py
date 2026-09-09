"""Deterministic Pine v6 rendering for immutable chart-annotation plans."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .chart_annotations import (
    AnnotationStatus,
    ChartAnnotationPlan,
    ChartLabel,
    ChartPattern,
    HypothesisRole,
    LabelPlacement,
    PatternGeometry,
    annotation_content_hash,
    chart_annotation_plan_content_hash,
)


PINE_OVERLAY_RENDER_SCHEMA_VERSION = "pine-overlay-render-1.0.0"


def _pine_string(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _pine_identifier(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)


def _pine_color(value: str) -> str:
    red = int(value[1:3], 16)
    green = int(value[3:5], 16)
    blue = int(value[5:7], 16)
    return f"color.rgb({red}, {green}, {blue})"


def _pine_float(value: float) -> str:
    rendered = format(value, ".15g")
    return rendered if "." in rendered or "e" in rendered.lower() else rendered + ".0"


def _pine_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    return (
        f'timestamp("UTC", {parsed.year}, {parsed.month}, {parsed.day}, '
        f"{parsed.hour}, {parsed.minute})"
    )


def status_safe_label_text(label: ChartLabel) -> str:
    """Prevent candidate or unproven labels from visually claiming certainty."""

    if label.status is AnnotationStatus.VERIFIED or "?" in label.text:
        return label.text
    return label.text + "?"


def _hypothesis_condition(role: HypothesisRole) -> str:
    if role is HypothesisRole.PRIMARY:
        return "showPrimary"
    if role is HypothesisRole.ALTERNATIVE:
        return "showAlternative"
    return "true"


def _pattern_calls(
    pattern: ChartPattern,
    *,
    pivot_by_id: Mapping[str, Any],
    layer_condition: str,
) -> list[str]:
    pivots = [pivot_by_id[pivot_id] for pivot_id in pattern.pivot_ids]
    target = pivots[-1]
    color = _pine_color(pattern.line_color)
    verified = "true" if pattern.status is AnnotationStatus.VERIFIED else "false"
    condition = (
        f"showPatternLines and {layer_condition} and "
        f"{_hypothesis_condition(pattern.hypothesis_role)}"
    )
    lines = [f"    if {condition}"]
    if pattern.geometry is PatternGeometry.ALTERNATING_BOUNDARIES:
        first, second, third, fourth = pivots[:4]
        lines.extend(
            [
                "        addProjectedBoundary("
                + ", ".join(
                    (
                        _pine_timestamp(first.timestamp),
                        _pine_float(first.price),
                        _pine_timestamp(third.timestamp),
                        _pine_float(third.price),
                        _pine_timestamp(target.timestamp),
                        color,
                        verified,
                        str(pattern.line_width),
                    )
                )
                + ")",
                "        addProjectedBoundary("
                + ", ".join(
                    (
                        _pine_timestamp(second.timestamp),
                        _pine_float(second.price),
                        _pine_timestamp(fourth.timestamp),
                        _pine_float(fourth.price),
                        _pine_timestamp(target.timestamp),
                        color,
                        verified,
                        str(pattern.line_width),
                    )
                )
                + ")",
            ]
        )
        return lines

    first, second, third, fourth = pivots[:4]
    if pattern.geometry is PatternGeometry.ONE_THREE_PARALLEL_TWO:
        reference_first, reference_second, anchor = first, third, second
    else:
        reference_first, reference_second, anchor = second, fourth, first
    lines.extend(
        [
            "        addProjectedBoundary("
            + ", ".join(
                (
                    _pine_timestamp(reference_first.timestamp),
                    _pine_float(reference_first.price),
                    _pine_timestamp(reference_second.timestamp),
                    _pine_float(reference_second.price),
                    _pine_timestamp(target.timestamp),
                    color,
                    verified,
                    str(pattern.line_width),
                )
            )
            + ")",
            "        addParallelBoundary("
            + ", ".join(
                (
                    _pine_timestamp(reference_first.timestamp),
                    _pine_float(reference_first.price),
                    _pine_timestamp(reference_second.timestamp),
                    _pine_float(reference_second.price),
                    _pine_timestamp(anchor.timestamp),
                    _pine_float(anchor.price),
                    _pine_timestamp(target.timestamp),
                    color,
                    verified,
                    str(pattern.line_width),
                )
            )
            + ")",
        ]
    )
    return lines


def render_pine_overlay_source(plan: ChartAnnotationPlan) -> str:
    """Render a plan without discovering, relabelling, or verifying waves."""

    if not isinstance(plan, ChartAnnotationPlan):
        raise TypeError("plan must be a ChartAnnotationPlan.")
    if plan.content_hash != chart_annotation_plan_content_hash(plan):
        raise ValueError("The chart annotation plan hash is invalid.")

    has_alternative = any(
        hypothesis.role is HypothesisRole.ALTERNATIVE
        for hypothesis in plan.hypotheses
    )
    detail_views = sorted({layer.detail_view for layer in plan.layers})
    detail_options = ["Auto", *detail_views, "All"]
    pivot_by_id = {pivot.pivot_id: pivot for pivot in plan.pivots}
    layer_by_id = {layer.layer_id: layer for layer in plan.layers}
    layer_variables = {
        layer.layer_id: "layerColor_" + _pine_identifier(layer.layer_id)
        for layer in plan.layers
    }
    layer_conditions = {
        layer.layer_id: "showLayer_" + _pine_identifier(layer.layer_id)
        for layer in plan.layers
    }
    maximum_labels = min(500, max(50, len(plan.labels) + 10))
    maximum_lines = min(500, max(20, len(plan.patterns) * 2 + 10))

    lines = [
        "//@version=6",
        (
            f'indicator("{_pine_string(plan.title)}", overlay=true, '
            f"max_labels_count={maximum_labels}, max_lines_count={maximum_lines})"
        ),
        "",
        f"// Annotation plan: {plan.plan_id}",
        f"// Plan SHA-256: {plan.content_hash}",
        f"// Source: {plan.source.provider} | {plan.source.feed} | {plan.source.symbol}",
        f"// Decision-time cutoff: {plan.source.cutoff}",
        "// This renderer displays supplied annotations; it does not verify Elliott structure.",
        "",
        (
            'detailView = input.string("Auto", "Detail", options=['
            + ", ".join(f'"{_pine_string(item)}"' for item in detail_options)
            + "])"
        ),
    ]
    if has_alternative:
        lines.append(
            'countView = input.string("Primary", "Count", '
            'options=["Primary", "Alternative", "Both"])'
        )
    lines.extend(
        [
            'showPatternLines = input.bool(true, "Pattern lines")',
            "",
        ]
    )
    for layer in plan.layers:
        lines.append(
            f"{layer_variables[layer.layer_id]} = {_pine_color(layer.label_color)}"
        )
    lines.extend(
        [
            "",
            "var labels = array.new<label>()",
            "var patternLines = array.new<line>()",
            "float recentPriceRange = ta.highest(high, 250) - ta.lowest(low, 250)",
            "",
            "clearLabels() =>",
            "    while array.size(labels) > 0",
            "        label.delete(array.pop(labels))",
            "",
            "clearPatternLines() =>",
            "    while array.size(patternLines) > 0",
            "        line.delete(array.pop(patternLines))",
            "",
            "labelGap(float pivotPrice) =>",
            "    float priceMagnitude = math.max(math.abs(pivotPrice), syminfo.mintick)",
            "    float rangeGap = nz(recentPriceRange, priceMagnitude * 0.10) * 0.02",
            "    float desiredGap = math.max(priceMagnitude * 0.018, rangeGap)",
            "    math.max(syminfo.mintick * 8.0, math.min(desiredGap, priceMagnitude * 0.08))",
            "",
            "addPivot(int labelTime, float pivotPrice, string labelText, color textColor, bool above, int stack, string textSize) =>",
            "    float distance = labelGap(pivotPrice) * (stack + 1)",
            "    float labelPrice = above ? pivotPrice + distance : pivotPrice - distance",
            "    label id = label.new(labelTime, labelPrice, labelText, xloc=xloc.bar_time, yloc=yloc.price, style=label.style_none, textcolor=textColor, size=textSize, textalign=text.align_center)",
            "    array.push(labels, id)",
            "",
            "projectPrice(int firstTime, float firstPrice, int secondTime, float secondPrice, int targetTime) =>",
            "    float elapsed = math.max(float(secondTime - firstTime), 1.0)",
            "    firstPrice + (secondPrice - firstPrice) * float(targetTime - firstTime) / elapsed",
            "",
            "addProjectedBoundary(int firstTime, float firstPrice, int secondTime, float secondPrice, int targetTime, color patternColor, bool verified, int width) =>",
            "    float targetPrice = projectPrice(firstTime, firstPrice, secondTime, secondPrice, targetTime)",
            "    string patternStyle = verified ? line.style_solid : line.style_dashed",
            "    line id = line.new(firstTime, firstPrice, targetTime, targetPrice, xloc=xloc.bar_time, extend=extend.none, color=patternColor, style=patternStyle, width=width)",
            "    array.push(patternLines, id)",
            "",
            "addParallelBoundary(int referenceFirstTime, float referenceFirstPrice, int referenceSecondTime, float referenceSecondPrice, int anchorTime, float anchorPrice, int targetTime, color patternColor, bool verified, int width) =>",
            "    float referenceElapsed = math.max(float(referenceSecondTime - referenceFirstTime), 1.0)",
            "    float slope = (referenceSecondPrice - referenceFirstPrice) / referenceElapsed",
            "    float targetPrice = anchorPrice + slope * float(targetTime - anchorTime)",
            "    string patternStyle = verified ? line.style_solid : line.style_dashed",
            "    line id = line.new(anchorTime, anchorPrice, targetTime, targetPrice, xloc=xloc.bar_time, extend=extend.none, color=patternColor, style=patternStyle, width=width)",
            "    array.push(patternLines, id)",
            "",
            "float chartSeconds = timeframe.in_seconds()",
            'bool autoView = detailView == "Auto"',
            'bool allView = detailView == "All"',
        ]
    )
    if has_alternative:
        lines.extend(
            [
                'bool showPrimary = countView == "Primary" or countView == "Both"',
                'bool showAlternative = countView == "Alternative" or countView == "Both"',
            ]
        )
    else:
        lines.extend(["bool showPrimary = true", "bool showAlternative = false"])
    for layer in plan.layers:
        lines.append(
            f"bool {layer_conditions[layer.layer_id]} = allView or "
            f'detailView == "{_pine_string(layer.detail_view)}" or '
            f'(autoView and chartSeconds >= timeframe.in_seconds("{layer.min_timeframe}") '
            f'and chartSeconds <= timeframe.in_seconds("{layer.max_timeframe}"))'
        )
    lines.extend(["", "if barstate.islast", "    clearLabels()", "    clearPatternLines()"])

    for label in plan.labels:
        pivot = pivot_by_id[label.pivot_id]
        layer = layer_by_id[label.layer_id]
        condition = (
            f"{layer_conditions[layer.layer_id]} and "
            f"{_hypothesis_condition(label.hypothesis_role)}"
        )
        label_text = _pine_string(status_safe_label_text(label))
        above = "true" if label.placement is LabelPlacement.ABOVE else "false"
        lines.extend(
            [
                f"    if {condition}",
                "        addPivot("
                + ", ".join(
                    (
                        _pine_timestamp(pivot.timestamp),
                        _pine_float(pivot.price),
                        f'"{label_text}"',
                        layer_variables[layer.layer_id],
                        above,
                        str(label.stack_order),
                        f"size.{label.size.value}",
                    )
                )
                + ")",
            ]
        )

    for pattern in plan.patterns:
        lines.extend(
            _pattern_calls(
                pattern,
                pivot_by_id=pivot_by_id,
                layer_condition=layer_conditions[pattern.layer_id],
            )
        )
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PineOverlayRenderResult:
    plan_id: str
    plan_content_hash: str
    pine_source: str
    pine_source_sha256: str
    content_hash: str = ""
    schema_version: str = PINE_OVERLAY_RENDER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PINE_OVERLAY_RENDER_SCHEMA_VERSION:
            raise ValueError("Unsupported Pine overlay render schema version.")
        if not self.plan_id:
            raise ValueError("plan_id is required.")
        if (
            len(self.plan_content_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.plan_content_hash)
        ):
            raise ValueError("plan_content_hash must be SHA-256.")
        calculated_source_hash = hashlib.sha256(self.pine_source.encode("utf-8")).hexdigest()
        if self.pine_source_sha256 != calculated_source_hash:
            raise ValueError("pine_source_sha256 does not match the rendered Pine source.")
        if self.content_hash and self.content_hash != pine_overlay_render_content_hash(self):
            raise ValueError("PineOverlayRenderResult content_hash does not match its payload.")

    @classmethod
    def create(cls, plan: ChartAnnotationPlan) -> "PineOverlayRenderResult":
        source = render_pine_overlay_source(plan)
        seed = cls(
            plan_id=plan.plan_id,
            plan_content_hash=plan.content_hash,
            pine_source=source,
            pine_source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            content_hash="",
        )
        return replace(seed, content_hash=pine_overlay_render_content_hash(seed))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "plan_content_hash": self.plan_content_hash,
            "pine_source": self.pine_source,
            "pine_source_sha256": self.pine_source_sha256,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PineOverlayRenderResult":
        expected = {
            "schema_version",
            "plan_id",
            "plan_content_hash",
            "pine_source",
            "pine_source_sha256",
            "content_hash",
        }
        if set(value) != expected:
            raise ValueError("PineOverlayRenderResult keys do not match its schema.")
        return cls(**dict(value))


def pine_overlay_render_content_hash(
    value: PineOverlayRenderResult | Mapping[str, Any],
) -> str:
    payload = value.to_dict() if isinstance(value, PineOverlayRenderResult) else dict(value)
    payload.pop("content_hash", None)
    return annotation_content_hash(payload)


def write_pine_overlay(
    result: PineOverlayRenderResult,
    output_path: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write deterministic Pine source without silent replacement."""

    if not isinstance(result, PineOverlayRenderResult):
        raise TypeError("result must be a PineOverlayRenderResult.")
    if result.content_hash != pine_overlay_render_content_hash(result):
        raise ValueError("The Pine overlay render result hash is invalid.")
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = (result.pine_source + "\n").encode("utf-8")
    if destination.exists():
        if destination.read_bytes() == expected:
            return destination
        if not overwrite:
            raise FileExistsError(f"Refusing to overwrite {destination}.")

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".pine_overlay_",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temporary_path, destination)
            temporary_path = None
        else:
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                if destination.read_bytes() != expected:
                    raise
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination


__all__ = [
    "PINE_OVERLAY_RENDER_SCHEMA_VERSION",
    "PineOverlayRenderResult",
    "pine_overlay_render_content_hash",
    "render_pine_overlay_source",
    "status_safe_label_text",
    "write_pine_overlay",
]
