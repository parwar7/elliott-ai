"""Clean Markdown rendering for strict degree-resolution records."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .degrees import degree_sort_key
from .schema import STANDARD_DEGREES


def _text(value: Any, fallback: str = "Not available") -> str:
    text = str(value).strip() if value is not None else ""
    return text if text else fallback


def _cell(value: Any) -> str:
    return _text(value).replace("|", "\\|").replace("\n", " ")


def _price(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "?"
    return f"${value:,.4f}".rstrip("0").rstrip(".")


def _anchor(value: Any) -> str:
    if not isinstance(value, dict):
        return "Not available"
    date = _text(value.get("date"), "?").replace("T", " ")
    return f"{date} @ {_price(value.get('price'))}"


def _percent(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "0%"
    return f"{value:.0%}"


def _number(value: Any, digits: int = 3) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "N/A"
    return f"{float(value):,.{digits}f}".rstrip("0").rstrip(".")


def _duration(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "N/A"
    hours = float(value)
    if hours < 1:
        return f"{hours * 60:.0f} min"
    if hours < 48:
        return f"{hours:.1f} h"
    if hours < 24 * 90:
        return f"{hours / 24:.1f} d"
    if hours < 24 * 365.25 * 2:
        return f"{hours / (24 * 30.4375):.1f} mo"
    return f"{hours / (24 * 365.25):.1f} y"


def _context_summary(item: dict[str, Any]) -> str:
    context_type = item.get("context_type")
    summary = item.get("summary", {})
    if not isinstance(summary, dict):
        return "Malformed context summary"
    if summary.get("status") != "calculated":
        return _text(summary.get("reason"), "Not calculated")
    if context_type == "benchmark":
        relative = summary.get("relative_strength", {})
        return (
            f"return {_number(summary.get('total_return_pct'))}%; "
            f"asset/benchmark correlation {_number(relative.get('asset_benchmark_return_correlation'))}"
        )
    if context_type == "breadth":
        latest = summary.get("latest", {})
        return (
            f"{_text(summary.get('breadth_regime'))}; latest A-D net "
            f"{_number(latest.get('advance_decline_net'))}"
        )
    if context_type == "yields":
        latest = summary.get("latest", {})
        return (
            f"2Y {_number(latest.get('yield_2y'))}; 10Y {_number(latest.get('yield_10y'))}; "
            f"10Y-2Y {_number(latest.get('spread_10y_2y'))}"
        )
    if context_type == "options":
        return (
            f"put/call volume {_number(summary.get('put_call_volume_ratio'))}; "
            f"put/call OI {_number(summary.get('put_call_open_interest_ratio'))}; "
            f"unsigned gross gamma {_number(summary.get('gross_unsigned_gamma_exposure'))}"
        )
    if context_type == "fundamentals":
        metrics = summary.get("metrics", {})
        return (
            f"period {_text(summary.get('latest_period'))}; revenue {_number(metrics.get('revenue'))}; "
            f"sequential revenue growth {_number(metrics.get('revenue_sequential_growth_pct'))}%"
        )
    if context_type == "news":
        return (
            f"{summary.get('item_count', 0)} supplied items; supplied sentiment average "
            f"{_number(summary.get('average_supplied_sentiment'))}"
        )
    if context_type == "order_book":
        return (
            f"spread {_number(summary.get('spread_bps'))} bps; depth imbalance "
            f"{_number(summary.get('depth_imbalance'))}"
        )
    return json.dumps(summary, ensure_ascii=True, separators=(",", ":"))[:240]


def _node_label(node: dict[str, Any]) -> str:
    return f"{_text(node.get('degree'))} {_text(node.get('wave'))}"


_SUBDIVISION_STATES = {"verified", "unproven", "not_covered", "inconsistent"}
_UNTRUSTED_VERIFICATION_WORD = re.compile(r"\b(?:verified|verify|proven|confirmed)\b", re.IGNORECASE)


def _node_subdivision_state(node: dict[str, Any]) -> str:
    """Return the structured status that is allowed to control report wording."""
    # A recursive proof covers the whole mandatory lower-timeframe ladder, so
    # it takes precedence over a legacy single-graph verification field.
    explicit = node.get("recursive_multi_timeframe_proof_status")
    if explicit is None:
        explicit = node.get("subdivision_verification_status")
    if explicit is None:
        explicit = node.get("deterministic_subdivision_status")
    text = str(explicit).strip().lower() if explicit is not None else ""
    if text in _SUBDIVISION_STATES:
        return text
    if (
        node.get("child_structure_status") == "verified"
        and node.get("degree_status") in {"confirmed", "validated"}
        and node.get("completion_status") == "completed"
    ):
        return "verified"
    return "unproven"


def _safe_node_structure(node: dict[str, Any]) -> str:
    state = _node_subdivision_state(node)
    if state == "verified":
        return _text(node.get("structure"))
    if state == "not_covered":
        return "Candidate; required lower-timeframe coverage is not available."
    if state == "inconsistent":
        return "Candidate is inconsistent with a deterministic structural rule."
    return "Candidate; deterministic subdivision remains unproven."


def _proof_evidence_label(node: dict[str, Any]) -> str:
    """Render an explicit provenance label for derived proof evidence only.

    The active report path has no implicit data-kind inference.  A caller that
    supplies session-aligned reconstructed evidence must say so explicitly,
    which prevents ``derived_4h`` from being presented as provider-native 4h.
    """

    evidence_kind = str(
        node.get("recursive_multi_timeframe_proof_evidence_kind", "")
    ).strip().lower()
    if evidence_kind == "derived_4h":
        return " Proof evidence: `derived_4h` session-aligned aggregation."
    return ""


def _safe_unverified_prose(value: Any, *, state: str) -> str:
    """Do not let a raw model phrase become a verification assertion."""
    text = _text(value)
    if state != "verified" and _UNTRUSTED_VERIFICATION_WORD.search(text):
        return "Raw source narrative omitted because deterministic subdivision is not verified."
    return text


def _alternate_subdivision_state(item: dict[str, Any]) -> str:
    explicit = item.get("recursive_multi_timeframe_proof_status")
    if explicit is None:
        explicit = item.get("subdivision_verification_status")
    text = str(explicit).strip().lower() if explicit is not None else ""
    return text if text in _SUBDIVISION_STATES else "unproven"


def _safe_alternate_description(item: dict[str, Any]) -> str:
    state = _alternate_subdivision_state(item)
    if state == "verified":
        return _text(item.get("description"))
    if state == "not_covered":
        return "Alternate candidate retained; required lower-timeframe coverage is not available."
    if state == "inconsistent":
        return "Alternate candidate is inconsistent with a deterministic structural rule."
    return "Alternate candidate retained; deterministic subdivision remains unproven."


def _tree_lines(nodes: list[dict[str, Any]]) -> list[str]:
    node_by_id = {node.get("wave_id"): node for node in nodes if node.get("wave_id")}
    children: dict[str | None, list[dict[str, Any]]] = {}
    for node in nodes:
        parent_id = node.get("parent_wave_id")
        key = parent_id if parent_id in node_by_id else None
        children.setdefault(key, []).append(node)
    for items in children.values():
        items.sort(key=degree_sort_key)

    lines: list[str] = []
    visited: set[str] = set()

    def visit(node: dict[str, Any], depth: int) -> None:
        wave_id = str(node.get("wave_id"))
        if wave_id in visited:
            return
        visited.add(wave_id)
        status = _text(node.get("degree_status"))
        path = f"{_price(node.get('start', {}).get('price'))} -> {_price(node.get('end', {}).get('price'))}"
        lines.append(
            f"{'  ' * depth}- **{_node_label(node)}**: {path}; "
            f"{_safe_node_structure(node)}; {status} ({_percent(node.get('degree_confidence'))})"
            f"{_proof_evidence_label(node)}"
        )
        for child in children.get(wave_id, []):
            visit(child, depth + 1)

    for root in children.get(None, []):
        visit(root, 0)
    for node in nodes:
        if str(node.get("wave_id")) not in visited:
            visit(node, 0)
    return lines


def _correction_evidence_lines(
    snapshot: dict[str, Any], category: str
) -> list[str]:
    references = set(snapshot.get("evidence_references", {}).get(category, []))
    items = [
        item
        for item in snapshot.get("evidence_catalog", [])
        if item.get("evidence_reference") in references
    ]
    if not items:
        return ["- None recorded at this cutoff."]
    return [
        f"- `{_text(item.get('evidence_reference'))}`: "
        f"{_text(item.get('feature_name'))} - {_text(item.get('reason'))} "
        f"({_text((item.get('phase4_timing') or {}).get('timing_class'))})"
        for item in items
    ]


def render_correction_state_section(
    snapshot: dict[str, Any],
    reviewed_outcome: dict[str, Any] | None = None,
) -> str:
    """Render one cutoff-bound correction snapshot without implying hindsight."""
    endpoint = snapshot.get("candidate_endpoint", {})
    states = {
        str(item.get("hypothesis_id")): item
        for item in snapshot.get("hypothesis_states", [])
        if isinstance(item, dict)
    }
    latest_transition = (
        snapshot.get("transitions", [])[-1]
        if snapshot.get("transitions")
        else None
    )
    timeline = snapshot.get("timeline", {})
    lines = [
        "## Correction State",
        "",
        f"**Candidate endpoint:** {_text(endpoint.get('timestamp'))} @ {_price(endpoint.get('price'))}  ",
        f"**Snapshot cutoff:** {_text(snapshot.get('cutoff'))}  ",
        f"**Current stage:** `{_text(snapshot.get('current_state'))}`  ",
        f"**Status:** {'Reviewed' if snapshot.get('reviewed') else 'Provisional and unresolved'}  ",
        f"**Case:** `{_text(snapshot.get('case_id'))}`  ",
        f"**Snapshot:** `{_text(snapshot.get('snapshot_id'))}`",
        "",
        "### Decision Timeline",
        "",
        "| Milestone | First knowable time |",
        "|---|---|",
        f"| Candidate endpoint | {_text(timeline.get('candidate_endpoint_time'))} |",
        f"| Structural completion | {_text(timeline.get('first_structural_completion_time'))} |",
        f"| Displacement confirmation | {_text(timeline.get('first_displacement_confirmation_time'))} |",
        f"| Five-away candidate | {_text(timeline.get('first_five_away_candidate_time'))} |",
        f"| Retracement confirmation | {_text(timeline.get('retracement_confirmation_time'))} |",
        f"| Final human review | {_text(timeline.get('final_reviewed_resolution_time'))} |",
        "",
        "### Active Hypotheses",
        "",
    ]
    active = snapshot.get("active_hypotheses", [])
    if active:
        for hypothesis_id in active:
            state = states.get(str(hypothesis_id), {})
            lines.append(
                f"- `{hypothesis_id}`: `{_text(state.get('state'))}`"
                + (" (provisional)" if state.get("provisional") else " (confirmed)")
            )
    else:
        lines.append("- No active hypothesis is recorded.")
    lines.extend(["", "### Invalidated Or Superseded Hypotheses", ""])
    inactive = [
        item
        for item in states.values()
        if item.get("status") in {"invalidated", "superseded"}
    ]
    if inactive:
        lines.extend(
            f"- `{item.get('hypothesis_id')}`: `{item.get('state')}`"
            for item in inactive
        )
    else:
        lines.append("- None.")
    lines.extend(["", "### Latest Transition", ""])
    if latest_transition:
        lines.extend(
            [
                f"- Hypothesis: `{_text(latest_transition.get('hypothesis_id'))}`",
                f"- State: `{_text(latest_transition.get('previous_state'))}` -> `{_text(latest_transition.get('new_state'))}`",
                f"- Time: {_text(latest_transition.get('transition_timestamp'))}",
                f"- Reason: {_text(latest_transition.get('reason'))}",
            ]
        )
    else:
        lines.append("- No state-changing transition was recorded in this snapshot.")
    for heading, category in (
        ("Supporting Evidence", "supporting"),
        ("Contradictory Evidence", "contradictory"),
        ("Unavailable Or Incomparable Evidence", "unavailable"),
        ("Hard Invalidation Evidence", "invalidating"),
    ):
        lines.extend(["", f"### {heading}", ""])
        lines.extend(_correction_evidence_lines(snapshot, category))
    needed = []
    for hypothesis_id in active:
        for item in states.get(str(hypothesis_id), {}).get("evidence_needed_next", []):
            if item not in needed:
                needed.append(item)
    lines.extend(["", "### Evidence Needed Next", ""])
    lines.extend(f"- {item}" for item in needed)
    if not needed:
        lines.append("- No additional requirement is recorded.")
    levels = snapshot.get("origin_and_invalidation_levels", {})
    lines.extend(
        [
            "",
            "### Origin And Invalidation Levels",
            "",
            f"- Protected origin: {_anchor(levels.get('protected_origin'))}",
            f"- Explicit levels: `{json.dumps(levels.get('explicit_levels', {}), ensure_ascii=True, sort_keys=True)}`",
        ]
    )
    if reviewed_outcome:
        lines.extend(
            [
                "",
                "### Reviewed Outcome",
                "",
                f"- Resolved hypothesis: `{_text(reviewed_outcome.get('resolved_hypothesis'))}`",
                f"- Interpretation: {_text(reviewed_outcome.get('final_reviewed_interpretation'))}",
                f"- Resolution cutoff: {_text(reviewed_outcome.get('resolution_cutoff'))}",
                f"- Reviewer: {_text(reviewed_outcome.get('reviewer'))}",
                "- This reviewed outcome was not available at the endpoint snapshot.",
            ]
        )
    return "\n".join(lines)


def render_correction_lineage_report(lineage: dict[str, Any]) -> str:
    """Render a complete immutable lineage with a compact snapshot audit table."""
    case = lineage.get("case", {})
    snapshots = lineage.get("snapshots", [])
    current_outcome = lineage.get("current_reviewed_outcome")
    lines = [
        f"# {_text(case.get('symbol'), 'Unknown')} Correction State Report",
        "",
        f"**Case:** `{_text(case.get('case_id'))}`  ",
        f"**Pattern family:** {_text(case.get('parent_pattern_family'))}  ",
        f"**Degree:** {_text(case.get('elliott_degree'))}  ",
        f"**Timeframe:** {_text(case.get('timeframe'))}",
        "",
        "## Immutable Snapshot Lineage",
        "",
        "| # | Cutoff | Snapshot | Parent | Stage | Provisional | Reviewed |",
        "|---:|---|---|---|---|---|---|",
    ]
    for index, snapshot in enumerate(snapshots, start=1):
        lines.append(
            f"| {index} | {_cell(snapshot.get('cutoff'))} | "
            f"`{_cell(snapshot.get('snapshot_id'))}` | "
            f"`{_cell(snapshot.get('parent_snapshot_id'),)}` | "
            f"`{_cell(snapshot.get('current_state'))}` | "
            f"{'Yes' if snapshot.get('provisional') else 'No'} | "
            f"{'Yes' if snapshot.get('reviewed') else 'No'} |"
        )
    if not snapshots:
        lines.append("| - | - | - | - | unresolved | Yes | No |")
    if snapshots:
        lines.extend(
            [
                "",
                render_correction_state_section(snapshots[-1], current_outcome),
            ]
        )
    return "\n".join(lines) + "\n"


def render_degree_report(
    run: dict[str, Any], resolution: dict[str, Any]
) -> str:
    """Return a readable top-down report without hiding unresolved evidence."""
    response = resolution.get("response", {})
    readiness = resolution.get("readiness", {})
    nodes = sorted(
        [item for item in response.get("degree_hierarchy", []) if isinstance(item, dict)],
        key=degree_sort_key,
    )
    final_ready = readiness.get("final_report_ready") is True
    report_status = "FINAL REVIEWED HIERARCHY" if final_ready else "PROVISIONAL DEGREE REPORT"
    symbol = _text(response.get("symbol") or run.get("symbol"), "Unknown symbol")
    lines = [
        f"# {symbol} Elliott Wave Degree Report",
        "",
        f"**Report status:** {report_status}",
        f"**Data cutoff:** {_text(response.get('data_cutoff'))}",
        f"**Source run:** {run.get('id')}  ",
        f"**Degree resolution:** {resolution.get('id')}  ",
        f"**Scale:** {_text(response.get('scale_mode'))}",
        "",
        "## Executive Verdict",
        "",
        _text(response.get("scope")),
        "",
        "| Confidence | Score |",
        "|---|---:|",
        f"| Price structure | {_percent(response.get('confidence', {}).get('structure'))} |",
        f"| Exact degree | {_percent(response.get('confidence', {}).get('degree'))} |",
        f"| Active wave | {_percent(response.get('confidence', {}).get('active_wave'))} |",
        "",
        "## Top-Down Hierarchy",
        "",
    ]
    if nodes:
        lines.extend(_tree_lines(nodes))
    else:
        lines.append("No wave hierarchy has been resolved yet.")

    for degree in STANDARD_DEGREES:
        degree_nodes = [node for node in nodes if node.get("degree") == degree]
        if not degree_nodes:
            continue
        lines.extend(
            [
                "",
                f"## {degree} Degree",
                "",
                "| Wave | Parent | Price and date path | Structure | State | Degree confidence | Children |",
                "|---|---|---|---|---|---:|---|",
            ]
        )
        for node in degree_nodes:
            parent = next(
                (item for item in nodes if item.get("wave_id") == node.get("parent_wave_id")),
                None,
            )
            path = f"{_anchor(node.get('start'))} -> {_anchor(node.get('end'))}"
            children = (
                f"{node.get('child_structure_status')} ({len(node.get('child_wave_ids', []))})"
            )
            lines.append(
                "| "
                + " | ".join(
                    (
                        _cell(node.get("wave")),
                        _cell(_node_label(parent) if parent else "Root"),
                        _cell(path),
                        _cell(_safe_node_structure(node)),
                        _cell(
                            f"{node.get('completion_status')} / {node.get('degree_status')}"
                        ),
                        _percent(node.get("degree_confidence")),
                        _cell(children),
                    )
                )
                + " |"
            )
        for node in degree_nodes:
            confirmations = node.get("confirmations", [])
            concerns = node.get("concerns", [])
            if not confirmations and not concerns and not node.get("invalidation"):
                continue
            lines.extend(["", f"### {_node_label(node)} Evidence"])
            subdivision_state = _node_subdivision_state(node)
            if subdivision_state != "verified":
                reason_codes = node.get("subdivision_verification_reason_codes") or node.get(
                    "verification_reason_codes"
                )
                lines.append(f"- Deterministic structure state: `{subdivision_state}`.")
                if isinstance(reason_codes, list) and reason_codes:
                    lines.append("- Deterministic reason codes: " + ", ".join(_text(item) for item in reason_codes))
                lines.append("- Raw source narrative is retained in immutable source data and omitted here.")
            else:
                for item in confirmations:
                    lines.append(f"- Confirmation: {_text(item)}")
                for item in concerns:
                    lines.append(f"- Concern: {_text(item)}")
                if node.get("invalidation"):
                    lines.append(f"- Invalidation: {_text(node.get('invalidation'))}")

    active = response.get("active_position", {})
    lines.extend(
        [
            "",
            "## Current Position",
            "",
            _text(active.get("summary")),
            "",
            f"**Active wave IDs:** {', '.join(active.get('wave_ids', [])) or 'Not assigned'}  ",
            f"**Confirmation:** {_text(active.get('confirmation'))}  ",
            f"**Invalidation:** {_text(active.get('invalidation'))}",
        ]
    )

    verification = readiness.get("deterministic_impulse_verification", [])
    lines.extend(["", "## Deterministic Verification", ""])
    if verification:
        lines.extend(
            [
                "| Parent | Timeframe | Result | Same-timeframe source |",
                "|---|---|---|---|",
            ]
        )
        for item in verification:
            lines.append(
                f"| {_cell(item.get('parent_wave_id'))} | {_cell(item.get('timeframe'))} | "
                f"{_cell(item.get('status'))} | {_cell(item.get('source'))} |"
            )
            if item.get("notes"):
                lines.append(f"\n{_text(item.get('notes'))}")
    else:
        lines.append("No completed verified 1-2-3-4-5 parent was available for EWO/volume aggregation.")

    analytics = readiness.get("analytical_metrics", {})
    lines.append(
        "\n**Active candle features:** "
        + ", ".join(analytics.get("active_candle_features", []))
        if analytics.get("active_candle_features")
        else "\n**Active candle features:** Not recorded"
    )
    wave_features = [
        item
        for item in analytics.get("wave_features", [])
        if isinstance(item, dict) and item.get("status") == "calculated"
    ]
    rsi_active = "rsi" in analytics.get("active_candle_features", [])
    lines.extend(["", "## Per-Wave Indicator Measurements", ""])
    if wave_features:
        if rsi_active:
            lines.extend(
                [
                    "| Wave ID | Degree | TF | Bars | Avg volume / 50 | EWO peak % | MACD histogram end | ATR avg % | RSI start/end | RSI min/max | RSI evidence |",
                    "|---|---|---:|---:|---:|---:|---:|---:|---|---|---|",
                ]
            )
        else:
            lines.extend(
                [
                    "| Wave ID | Degree | TF | Bars | Avg volume / 50 | EWO peak % | MACD histogram end | ATR avg % |",
                    "|---|---|---:|---:|---:|---:|---:|---:|",
                ]
            )
        for item in wave_features:
            base_row = (
                f"| {_cell(item.get('wave_id'))} | {_cell(item.get('degree'))} | "
                f"{_cell(item.get('timeframe'))} | {item.get('bar_count', 0)} | "
                f"{_number(item.get('volume', {}).get('average_ratio_to_50_period'))} | "
                f"{_number(item.get('ewo', {}).get('normalized_absolute_peak_pct'))} | "
                f"{_number(item.get('macd', {}).get('histogram_endpoint'))} | "
                f"{_number(item.get('volatility', {}).get('atr_pct_average'))} |"
            )
            if rsi_active:
                rsi = item.get("rsi", {})
                base_row = (
                    base_row[:-1]
                    + f" {_number(rsi.get('value_at_wave_start'))} / {_number(rsi.get('value_at_wave_end'))} |"
                    + f" {_number(rsi.get('minimum'))} / {_number(rsi.get('maximum'))} |"
                    + f" {_cell(rsi.get('evidence', {}).get('status') or rsi.get('availability'))} |"
                )
            lines.append(base_row)
        if rsi_active:
            lines.append(
                "\nRSI unavailable or incomparable statuses are informational and do not reduce readiness or invalidate a count."
            )
        unavailable_count = len(analytics.get("wave_features", [])) - len(wave_features)
        if unavailable_count:
            lines.append(
                f"\n{unavailable_count} wave rows could not be measured because their assigned timeframe data was unavailable."
            )
    else:
        lines.append("No resolved wave had a matching unchanged candle feed for indicator aggregation.")

    fingerprint_analysis = analytics.get("wave_fingerprint_analysis", {})
    fingerprints = [
        item
        for item in fingerprint_analysis.get("fingerprints", [])
        if isinstance(item, dict) and item.get("status") == "calculated"
    ]
    if fingerprints:
        fingerprint_rsi_active = any(
            item.get("identity", {}).get("active_feature_flags", {}).get("rsi") is True
            for item in fingerprints
        )
        lines.extend(
            [
                "",
                "## Versioned Wave Fingerprints",
                "",
                f"**Schema:** {_text(fingerprint_analysis.get('feature_schema_version'))}  ",
                f"**Calculation:** {_text(fingerprint_analysis.get('calculation_version'))}",
                "",
            ]
        )
        if fingerprint_rsi_active:
            lines.extend(
                [
                    "| Wave | Role context | Cutoff | Bars | Direction | Efficiency | Volume / baseline | EWO peak | MACD histogram | ATR state | RSI end | Evidence |",
                    "|---|---|---|---:|---|---:|---:|---:|---:|---|---:|---|",
                ]
            )
        else:
            lines.extend(
                [
                    "| Wave | Role context | Cutoff | Bars | Direction | Efficiency | Volume / baseline | EWO peak | MACD histogram | ATR state | Evidence |",
                    "|---|---|---|---:|---|---:|---:|---:|---:|---|---|",
                ]
            )
        for fingerprint in fingerprints:
            identity = fingerprint.get("identity", {})
            indicators = fingerprint.get("indicators", {})
            statuses = sorted(
                {
                    str(item.get("status"))
                    for item in fingerprint.get("evidence_items", [])
                    if isinstance(item, dict) and item.get("status")
                }
            )
            context = (
                f"{identity.get('parent_pattern_family', 'unknown')} "
                f"{identity.get('wave_label', 'unknown')}"
            )
            row = (
                f"| {_cell(identity.get('wave_id'))} | {_cell(context)} | "
                f"{_cell(fingerprint.get('cutoff', {}).get('timestamp'))} | "
                f"{fingerprint.get('structural', {}).get('duration_candles', 0)} | "
                f"{_cell(fingerprint.get('price', {}).get('direction'))} | "
                f"{_number(fingerprint.get('price', {}).get('directional_efficiency'))} | "
                f"{_number(indicators.get('volume', {}).get('average_ratio_to_baseline'))} | "
                f"{_number(indicators.get('ewo', {}).get('absolute_peak'))} | "
                f"{_number(indicators.get('macd', {}).get('histogram_end'))} | "
                f"{_cell(indicators.get('volatility', {}).get('volatility_state'))} |"
            )
            if fingerprint_rsi_active:
                row = row[:-1] + f" {_number(indicators.get('rsi', {}).get('end_value'))} |"
            lines.append(row[:-1] + f" {_cell(', '.join(statuses) or 'none')} |")

        comparisons = [
            item
            for item in fingerprint_analysis.get("comparisons", [])
            if isinstance(item, dict)
        ]
        if comparisons:
            lines.extend(
                [
                    "",
                    "### Fingerprint Comparisons",
                    "",
                    "| Current | Reference | Relationship | Status | Price ratio | EWO ratio | Volume ratio | Unavailable / incomparable |",
                    "|---|---|---|---|---:|---:|---:|---|",
                ]
            )
            for comparison in comparisons:
                reasons = [
                    str(item.get("reason"))
                    for item in comparison.get("incomparable_fields", [])
                    if isinstance(item, dict) and item.get("reason")
                ] + [str(item) for item in comparison.get("unavailable_reasons", [])]
                ratios = comparison.get("normalized_ratios", {})
                lines.append(
                    f"| {_cell(comparison.get('current_wave_id'))} | "
                    f"{_cell(comparison.get('reference_wave_id'))} | "
                    f"{_cell(comparison.get('relationship'))} | "
                    f"{_cell(comparison.get('status'))} | "
                    f"{_number(ratios.get('price_absolute_change'))} | "
                    f"{_number(ratios.get('ewo_absolute_peak'))} | "
                    f"{_number(ratios.get('volume_normalized'))} | "
                    f"{_cell('; '.join(dict.fromkeys(reasons)) or 'None')} |"
                )
        unavailable_fingerprints = fingerprint_analysis.get(
            "unavailable_fingerprints", []
        )
        if unavailable_fingerprints:
            lines.append(
                f"\n{len(unavailable_fingerprints)} candidate wave fingerprints were unavailable because no cutoff-safe matching data segment existed."
            )
        lines.append(
            "\nFingerprints and soft role templates describe supplied candidates only; they do not relabel waves or create probabilities."
        )

    fibonacci = analytics.get("fibonacci_duration_alternation", [])
    lines.extend(["", "## Fibonacci, Duration And Alternation", ""])
    if fibonacci:
        lines.extend(
            [
                "| Parent | Family | Ratio scale | Measurement | Actual | Nearest Fibonacci | Error |",
                "|---|---|---|---|---:|---:|---:|",
            ]
        )
        for sequence in fibonacci:
            for name, match in sequence.get("fibonacci_matches", {}).items():
                match = match or {}
                lines.append(
                    f"| {_cell(sequence.get('parent_wave_id'))} | {_cell(sequence.get('family'))} | "
                    f"{_cell(sequence.get('ratio_scale'))} | {_cell(str(name).replace('_', ' '))} | "
                    f"{_number(match.get('actual'))} | {_number(match.get('nearest_level'))} | "
                    f"{_number(match.get('relative_error_pct'))}% |"
                )
            alternation = sequence.get("alternation")
            if isinstance(alternation, dict):
                lines.append(
                    f"\n**{_text(sequence.get('parent_wave_id'))} alternation:** "
                    f"Wave 2 {_text(alternation.get('wave_2_structure'))}; Wave 4 "
                    f"{_text(alternation.get('wave_4_structure'))}; structure alternates: "
                    f"{_text(alternation.get('structure_alternates'))}; W4/W2 duration ratio "
                    f"{_number(alternation.get('duration_ratio_wave_4_to_wave_2'))}."
                )
    else:
        lines.append("No complete impulse, zigzag, flat, or W-X-Y child sequence was available for ratio calculation.")

    reference_sequences = analytics.get("fibonacci_reference_levels", [])
    lines.extend(["", "## Fibonacci Reference Levels", ""])
    if reference_sequences:
        lines.append(
            "These are count-validation projections on the disclosed scale, not trade targets."
        )
        lines.extend(
            [
                "",
                "| Parent | Reference | Scale | Levels |",
                "|---|---|---|---|",
            ]
        )
        for sequence in reference_sequences:
            scale = sequence.get("selected_scale", "arithmetic")
            for reference in sequence.get("references", []):
                all_levels = reference.get("levels", {})
                selected = all_levels.get(scale) or all_levels.get("arithmetic") or {}
                level_text = "; ".join(
                    f"{ratio}: {_number(price)}"
                    for ratio, price in selected.items()
                )
                lines.append(
                    f"| {_cell(sequence.get('parent_wave_id'))} | {_cell(reference.get('name'))} | "
                    f"{_cell(scale)} | {_cell(level_text)} |"
                )
    else:
        lines.append("No child leg had enough exact anchors for Fibonacci reference levels.")

    duration_checks = analytics.get("duration_prior_checks", [])
    lines.extend(["", "## Degree Duration Diagnostics", ""])
    if duration_checks:
        lines.extend(
            [
                "| Wave ID | Degree | Position | Observed | Soft prior | Result | Child-sequence check |",
                "|---|---|---:|---:|---|---|---|",
            ]
        )
        for item in duration_checks:
            wave_check = item.get("wave_duration", {})
            child_check = item.get("child_sequence_duration", {})
            child_text = (
                f"{child_check.get('child_degree')}: {child_check.get('status')} "
                f"({child_check.get('label', 'no prior')})"
                if child_check
                else "N/A"
            )
            lines.append(
                f"| {_cell(item.get('wave_id'))} | {_cell(item.get('degree'))} | "
                f"{_cell(item.get('sequence_position'))} | {_duration(wave_check.get('actual_hours'))} | "
                f"{_cell(wave_check.get('label'))} | {_cell(wave_check.get('status'))} | "
                f"{_cell(child_text)} |"
            )
        lines.append(
            "\nDuration ranges are soft anomaly flags only. Elapsed time includes closures and cannot assign or invalidate degree."
        )
    else:
        lines.append("No duration checks were available.")

    channels = analytics.get("elliott_channels", [])
    lines.extend(["", "## Elliott Channel Comparison", ""])
    if channels:
        lines.extend(
            [
                "| Parent | Preferred scale | Arithmetic combined deviation | Log combined deviation |",
                "|---|---|---:|---:|",
            ]
        )
        for item in channels:
            lines.append(
                f"| {_cell(item.get('parent_wave_id'))} | {_cell(item.get('preferred_channel_scale'))} | "
                f"{_number((item.get('arithmetic') or {}).get('combined_absolute_deviation_pct'))}% | "
                f"{_number((item.get('logarithmic') or {}).get('combined_absolute_deviation_pct'))}% |"
            )
    else:
        lines.append("No complete five-wave child sequence was available for channel comparison.")

    market_evidence = run.get("evidence", {}).get("market_data", [])
    scale_rows = []
    for item in market_evidence:
        comparison = item.get("technical_features", {}).get("scale_comparison", {})
        if comparison.get("status") == "calculated":
            scale_rows.append((item, comparison))
    lines.extend(["", "## Price-Scale Diagnostics", ""])
    if scale_rows:
        lines.extend(
            [
                "| Dataset | TF | Window | Preferred | Arithmetic MAPE | Log MAPE |",
                "|---|---:|---:|---|---:|---:|",
            ]
        )
        for item, comparison in scale_rows:
            lines.append(
                f"| {_cell(item.get('source'))} | {_cell(item.get('timeframe'))} | "
                f"{comparison.get('window_bars', 0)} | {_cell(comparison.get('preferred_scale'))} | "
                f"{_number(comparison.get('arithmetic', {}).get('mean_absolute_percentage_error'))} | "
                f"{_number(comparison.get('logarithmic', {}).get('mean_absolute_percentage_error'))} |"
            )
    else:
        lines.append("No scale-regression diagnostics were stored in the source run.")

    contexts = run.get("evidence", {}).get("context_data", [])
    lines.extend(["", "## Optional Analytical Context", ""])
    if contexts:
        lines.extend(
            [
                "| Evidence | Type | Status | Calculated summary |",
                "|---|---|---|---|",
            ]
        )
        for item in contexts:
            summary = item.get("summary", {})
            lines.append(
                f"| {_cell(item.get('evidence_id'))} | {_cell(item.get('context_type'))} | "
                f"{_cell(summary.get('status'))} | {_cell(_context_summary(item))} |"
            )
    else:
        lines.append(
            "No benchmark, breadth, yield, options, fundamentals, news, or order-book file was supplied. "
            "The absence is explicit; no context value was inferred."
        )

    alternatives = response.get("alternate_counts", [])
    lines.extend(["", "## Alternate Counts", ""])
    if alternatives:
        for index, item in enumerate(alternatives, start=1):
            alternate_state = _alternate_subdivision_state(item)
            lines.append(f"{index}. {_safe_alternate_description(item)}")
            if alternate_state != "verified":
                lines.append(f"   Deterministic structure state: `{alternate_state}`.")
            if item.get("status"):
                lines.append(f"   Status: {_text(item.get('status'))}")
            lines.append(f"   Trigger: {_safe_unverified_prose(item.get('trigger'), state=alternate_state)}")
            lines.append(f"   Invalidation: {_safe_unverified_prose(item.get('invalidation'), state=alternate_state)}")
            evidence_sections = (
                ("Supporting evidence", "supporting_evidence"),
                ("Contradictory evidence", "contradictory_evidence"),
                (
                    "Unavailable or incomparable evidence",
                    "unavailable_or_incomparable_evidence",
                ),
                ("Evidence needed for confirmation", "evidence_needed_for_confirmation"),
            )
            for label, field in evidence_sections:
                values = item.get(field)
                if isinstance(values, list) and values:
                    lines.append(
                        f"   {label}: "
                        + "; ".join(
                            _safe_unverified_prose(value, state=alternate_state)
                            for value in values
                        )
                    )
    else:
        lines.append("No evidence-supported alternate was supplied.")

    lines.extend(["", "## Invalidation Levels", ""])
    invalidations = response.get("invalidation_levels", [])
    lines.extend(f"- {_text(item)}" for item in invalidations)
    if not invalidations:
        lines.append("- Not established.")

    lines.extend(["", "## Final-Readiness Audit", ""])
    lines.append(
        f"**Final report ready:** {'Yes' if final_ready else 'No'}  "
    )
    lines.append(
        f"**Cross-timeframe reconciliation:** {_text(readiness.get('reconciliation_status'))}  "
    )
    lines.append(
        f"**Completed waves:** {readiness.get('completed_wave_count', 0)}  "
    )
    lines.append(
        f"**Impulse sequences checked:** {readiness.get('impulse_sequences_checked', 0)}"
    )
    blockers = readiness.get("blockers", [])
    warnings = readiness.get("warnings", [])
    unresolved = response.get("unresolved_items", [])
    if blockers:
        lines.extend(["", "### Blockers"])
        lines.extend(f"- {_text(item)}" for item in blockers)
    if warnings:
        lines.extend(["", "### Warnings"])
        lines.extend(f"- {_text(item)}" for item in warnings)
    if unresolved:
        lines.extend(["", "### Unresolved Structure"])
        lines.extend(f"- {_text(item)}" for item in unresolved)

    correction_state = resolution.get("correction_state") or readiness.get(
        "correction_state"
    )
    if isinstance(correction_state, dict):
        snapshot = correction_state.get("latest_snapshot") or correction_state.get(
            "snapshot"
        )
        if not isinstance(snapshot, dict) and correction_state.get("snapshot_id"):
            snapshot = correction_state
        if isinstance(snapshot, dict):
            lines.extend(
                [
                    "",
                    render_correction_state_section(
                        snapshot,
                        correction_state.get("current_reviewed_outcome"),
                    ),
                ]
            )

    evidence = run.get("evidence", {})
    freshness = evidence.get("data_freshness", {})
    lines.extend(
        [
            "",
            "## Data Provenance",
            "",
            f"- Source mode: {_text(freshness.get('source_mode'))}",
            f"- Freshness: {_text(freshness.get('overall_status'))}",
            f"- Resolution provider: {_text(resolution.get('provider'))}",
            f"- Resolution model: {_text(resolution.get('model'))}",
            "",
            "## Risk Note",
            "",
            _text(response.get("risk_note")),
            "",
        ]
    )
    return "\n".join(lines)


def default_report_name(symbol: str, run_id: int) -> str:
    safe_symbol = re.sub(r"[^A-Za-z0-9]+", "_", symbol).strip("_") or "SYMBOL"
    return f"{safe_symbol}_CLEAN_WAVE_DEGREE_REPORT_RUN_{run_id}.md"


def write_degree_report(
    run: dict[str, Any], resolution: dict[str, Any], output: Path
) -> Path:
    resolved = Path(output).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(render_degree_report(run, resolution), encoding="utf-8")
    return resolved
