"""Status-safe human report for an adaptive Elliott recount trace."""

from __future__ import annotations

from typing import Any, Mapping

from .adaptive_recount import (
    AdaptiveRecountTrace,
    AdaptiveRootHypothesis,
    CandidateScreenStatus,
    RootGenerationStage,
    RootStageFailureCode,
    RootStageValidationFailure,
    adaptive_parent_reconstruction_decision,
    adaptive_hypothesis_signature,
    effective_adaptive_proof_status,
)
from .lower_timeframe_candidate_generator import CandidateGraphRole


def _price(value: float) -> str:
    return f"{value:,.6f}".rstrip("0").rstrip(".")


_ROOT_STAGE_LABELS = {
    RootGenerationStage.PRICE_SEGMENTATION: "Stage A",
    RootGenerationStage.STRUCTURAL_GROUPING: "Stage B1",
    RootGenerationStage.FAMILY_CLASSIFICATION: "Stage B2",
}


def _role_failures(
    trace: AdaptiveRecountTrace,
    role: CandidateGraphRole,
) -> tuple[RootStageValidationFailure, ...]:
    return tuple(item for item in trace.root_stage_failures if item.role is role)


def _role_provider_was_called(
    trace: AdaptiveRecountTrace,
    role: CandidateGraphRole,
    hypothesis: AdaptiveRootHypothesis | None,
) -> bool:
    if hypothesis is not None:
        return True
    return any(
        item.returned_structured_output_hash is not None
        for item in _role_failures(trace, role)
    )


def _root_contract_failure_lines(
    trace: AdaptiveRecountTrace,
    role: CandidateGraphRole,
) -> list[str]:
    failures = _role_failures(trace, role)
    if not failures:
        return [
            "**ROOT GENERATION CONTRACT FAILURE**",
            f"{role.value.title()} root generation did not produce a typed hypothesis. "
            "No Elliott conclusion was reached.",
        ]
    last = failures[-1]
    stage = _ROOT_STAGE_LABELS[last.stage]
    if last.error_code is RootStageFailureCode.ROOT_STAGE_ATTEMPT_BUDGET_EXHAUSTED:
        reason = "the global approved root-stage call budget was exhausted"
    else:
        reason = (
            "a model-generated structured response could not satisfy the deterministic "
            f"contract (`{last.error_code.value}`)"
        )
    return [
        "**ROOT GENERATION CONTRACT FAILURE**",
        f"{role.value.title()} {stage} exhausted because {reason}. "
        "No Elliott conclusion was reached.",
        f"**Attempts recorded for this role:** {len(failures)}.",
    ]


def _scenario_lines(
    trace: AdaptiveRecountTrace,
    hypothesis: AdaptiveRootHypothesis | None,
    *,
    role: CandidateGraphRole,
    screen_status: CandidateScreenStatus | None,
    hard_errors: tuple[str, ...],
    unresolved: tuple[str, ...],
) -> list[str]:
    title = "SCENARIO 1 - PRIMARY" if role is CandidateGraphRole.PRIMARY else "SCENARIO 2 - ALTERNATIVE"
    lines = [f"## {title}", ""]
    if hypothesis is None:
        lines.extend(_root_contract_failure_lines(trace, role))
        lines.append("")
        return lines
    roots = tuple(item for item in hypothesis.nodes if item.parent_wave_id is None)
    effective_root_statuses = tuple(
        (
            item.wave_id,
            effective_adaptive_proof_status(
                trace,
                role=role,
                parent_wave_id=item.wave_id,
            ),
        )
        for item in roots
    )
    lines.extend(
        [
            f"**Candidate ID:** `{hypothesis.hypothesis_id}`",
            f"**Initial candidate screen:** `{screen_status.value if screen_status else 'unavailable'}`",
            "**Effective top-level proof:** "
            + (
                "; ".join(f"`{wave_id}={status.value}`" for wave_id, status in effective_root_statuses)
                if effective_root_statuses
                else "`unavailable`"
            ),
            f"**Candidate hash:** `{hypothesis.content_hash}`",
            "",
            "| Degree / wave | Family | State | Price path | Timeframe | Deterministic proof |",
            "|---|---|---|---:|---|---|",
        ]
    )
    if hypothesis.analysis_scope is not None and hypothesis.segmentation is not None:
        lines.extend(
            [
                "### Non-Elliott Analysis Scope",
                "",
                f"Container: {hypothesis.analysis_scope.window_start.timestamp_utc} -> "
                f"{hypothesis.analysis_scope.as_of_observation.timestamp_utc}; this is not a wave node.",
                "",
                "| Segment | Kind | State | Structural grouping / classification | Boundary path |",
                "|---|---|---|---|---|",
            ]
        )
        classifications = {
            item.segment_id: item for item in hypothesis.segment_classifications
        }
        groups_by_segment = {
            segment_id: tuple(
                group
                for group in (
                    hypothesis.root_grouping.groups
                    if hypothesis.root_grouping is not None
                    else ()
                )
                if segment_id in group.source_segment_ids
            )
            for segment_id in (
                item.segment_id for item in hypothesis.segmentation.segments
            )
        }
        classification_by_group = {
            item.group_id: item for item in hypothesis.group_classifications
        }
        for segment in hypothesis.segmentation.segments:
            classification = classifications.get(segment.segment_id)
            groups = groups_by_segment.get(segment.segment_id, ())
            if classification is not None:
                grouping_text = classification.classification.value
            elif groups:
                labels = []
                for group in groups:
                    group_classification = classification_by_group.get(group.group_id)
                    labels.append(
                        f"{group.group_id} / {group_classification.classification.value}"
                        if group_classification is not None
                        else f"{group.group_id} / candidate"
                    )
                grouping_text = "; ".join(labels)
            else:
                grouping_text = "explicitly unresolved"
            lines.append(
                f"| `{segment.segment_id}` | "
                f"{segment.segment_kind.value if segment.segment_kind is not None else 'legacy_untyped'} | "
                f"{segment.completion_state.value} | {grouping_text} | "
                f"{segment.start_boundary.timestamp_utc} / {_price(segment.start_boundary.price)} -> "
                f"{segment.end_boundary.timestamp_utc} / {_price(segment.end_boundary.price)} |"
            )
        lines.append("")
        if hypothesis.root_grouping is not None:
            lines.append(
                "Grouped wave candidates may span several connected Stage-A segments; "
                "overlapping groups are alternative proposals, while selected root nodes "
                "must remain non-overlapping and candidate-only until recursive proof."
            )
            lines.append("")
    for node in hypothesis.nodes:
        proof = effective_adaptive_proof_status(
            trace,
            role=role,
            parent_wave_id=node.wave_id,
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    f"{node.degree} {node.sequence_position} (`{node.wave_id}`)",
                    node.declared_family,
                    node.completion_state,
                    f"{node.start_pivot.timestamp_utc} / {_price(node.start_pivot.price)} -> "
                    f"{node.end_pivot.timestamp_utc} / {_price(node.end_pivot.price)}",
                    node.timeframe,
                    proof.value,
                )
            )
            + " |"
        )
    lines.append("")
    if screen_status is CandidateScreenStatus.NO_CANDIDATE:
        lines.append("**NO MARKET CANDIDATE**")
        lines.append(
            "No Elliott candidate was forced from the available Stage-A swings; this is distinct from an unproven candidate."
        )
        lines.append("")
    elif screen_status is CandidateScreenStatus.REJECTED:
        lines.append("**CANDIDATE HARD-RULE REJECTION**")
        lines.append(
            "The root stages returned a validly constructed candidate, but deterministic Elliott screening rejected it."
        )
        lines.append("")
    if any(status.value == "not_covered" for _, status in effective_root_statuses):
        lines.append("**MISSING DATA / NOT COVERED**")
        lines.append(
            "Required compatible lower-timeframe evidence is unavailable for at least one root; no structural rejection is implied."
        )
        lines.append("")
    active = next((item for item in hypothesis.nodes if item.wave_id == hypothesis.active_wave_id), None)
    lines.append(
        "**Current active wave:** "
        + (
            f"{active.degree} {active.sequence_position} (`{active.wave_id}`), candidate-only until completed and proven."
            if active is not None
            else "No active node was declared."
        )
    )
    lines.append(
        "**Confirmation conditions:** "
        + ("; ".join(hypothesis.confirmation_conditions) or "No model-authored confirmation condition was accepted.")
    )
    invalidations = [
        f"{item.wave_id}: {item.invalidation.direction.value} {_price(item.invalidation.threshold_price)} "
        f"on {item.invalidation.evaluation_basis.value}"
        for item in hypothesis.nodes
        if item.invalidation is not None
        if item.parent_wave_id is None or item.wave_id == hypothesis.active_wave_id
    ]
    lines.append("**Invalidation conditions:** " + ("; ".join(invalidations) or "Unavailable."))
    lines.append(
        "**Unresolved structure / next evidence:** "
        + ("; ".join((*hypothesis.unresolved_questions, *unresolved)) or "No unresolved item was recorded at this layer.")
    )
    if hard_errors:
        lines.append("**Hard-rule failures:** " + "; ".join(hard_errors))
    for root in roots:
        decision = adaptive_parent_reconstruction_decision(
            trace,
            role=role,
            parent_wave_id=root.wave_id,
        )
        lines.append(
            f"**Child reconstruction `{root.wave_id}`:** {decision.search_outcome.value}; "
            f"{decision.attempts_used}/{decision.maximum_attempts} attempts used."
        )
    if hypothesis.candidate_evidence:
        lines.append(
            "**Candidate-scoped soft evidence:** "
            + "; ".join(
                f"{item.segment_id} ({item.timeframe}, {item.duration_bars} bars): "
                + ", ".join(item.active_features)
                for item in hypothesis.candidate_evidence
            )
            + ". Indicators did not create pivots or verification status."
        )
    else:
        lines.append(
            "**Candidate-scoped soft evidence:** unavailable in this legacy trace; missing indicators do not invalidate structure."
        )
    lines.append(
        "**Fibonacci / channel / duration evidence:** soft post-boundary evidence only; unavailable states remain explicit."
    )
    lines.append("")
    return lines


def _comparison_lines(trace: AdaptiveRecountTrace) -> list[str]:
    lines = ["## SCENARIO COMPARISON", ""]
    pair = trace.root_pair
    if pair is None or pair.primary is None or pair.alternative is None:
        missing_roles = []
        if pair is None or pair.primary is None:
            missing_roles.append("Primary")
        if pair is None or pair.alternative is None:
            missing_roles.append("Alternative")
        return [
            *lines,
            "A two-count comparison was not run because root generation did not produce "
            + " and ".join(missing_roles)
            + ". This is a generation-contract state, not a market conclusion.",
            "",
        ]
    primary_by_id = {item.wave_id: item for item in pair.primary.nodes}
    alternative_by_id = {item.wave_id: item for item in pair.alternative.nodes}
    structural_differences: list[str] = []
    for wave_id in sorted(set(primary_by_id) | set(alternative_by_id)):
        left = primary_by_id.get(wave_id)
        right = alternative_by_id.get(wave_id)
        if left is None or right is None:
            structural_differences.append(
                f"`{wave_id}` appears only in the {'Alternative' if left is None else 'Primary'}."
            )
            continue
        left_shape = (
            left.degree,
            left.sequence_position,
            left.declared_family,
            left.completion_state,
            left.start_pivot.pivot_id,
            left.end_pivot.pivot_id,
        )
        right_shape = (
            right.degree,
            right.sequence_position,
            right.declared_family,
            right.completion_state,
            right.start_pivot.pivot_id,
            right.end_pivot.pivot_id,
        )
        if left_shape != right_shape:
            structural_differences.append(f"`{wave_id}` has different degree, family, state, or boundaries.")
    lines.extend(
        [
            "**Exact structural difference:** "
            + (" ".join(structural_differences) if structural_differences else "The graph signatures differ through their ordered structural content."),
            "**Five versus three discriminator:** require a connected lower-degree motive five for a motive claim; a corrective three or valid corrective family keeps the corrective interpretation alive.",
            "**Pivot / channel discriminator:** use each candidate's explicit confirmation and invalidation conditions; a channel break is supporting evidence, not proof by itself.",
            "**Indicator discriminator:** same-feed RSI, volume, EWO, and MACD may support or weaken a price-valid count but cannot create pivots or invalidate it alone.",
            "**Alternative promotion:** promote only after the Primary violates a hard rule or explicit invalidation and the Alternative's required child structure is deterministically supported.",
            "**Distinctness check:** `"
            + adaptive_hypothesis_signature(pair.primary)
            + "` versus `"
            + adaptive_hypothesis_signature(pair.alternative)
            + "`.",
            "",
        ]
    )
    return lines


def render_adaptive_recount_report(
    trace: AdaptiveRecountTrace | Mapping[str, Any],
) -> str:
    if not isinstance(trace, AdaptiveRecountTrace):
        trace = AdaptiveRecountTrace.from_dict(trace)
    plan = trace.plan
    timeframes = tuple(dict.fromkeys(item.canonical_timeframe for item in trace.data_manifests))
    lines = [
        f"# Adaptive Elliott Recount - {plan.symbol}",
        "",
        "## DATA AND SCOPE",
        "",
        f"**Analysis window:** {plan.analysis_start_utc} -> {plan.analysis_cutoff_utc}",
        f"**Context begins:** {plan.context_start_utc}",
        f"**Actual cutoff:** {plan.analysis_cutoff_utc}",
        f"**Mode:** strict blind, `{plan.analysis_profile}` profile",
        f"**Provider capability source:** {plan.market_data_provider} / {plan.provider_capabilities.source_interface}",
        "**Completed-candle policy:** native, completed candles only; incomplete candles cannot prove a completed wave.",
        "**Timeframes examined/requested:** " + (", ".join(timeframes) if timeframes else "none yet"),
        f"**Workflow status:** `{trace.status.value}` (`{trace.stop_reason.value}`)",
        f"**Trace hash:** `{trace.content_hash}`",
        "",
    ]
    if trace.planner_decisions:
        lines.extend(["### Dynamic Requests", "", "| Timeframe | Structural question | Reason | Result |", "|---|---|---|---|"])
        requests = {item.request_id: item for item in trace.planner_requests}
        for decision in trace.planner_decisions:
            request = requests[decision.request_id]
            lines.append(
                f"| {decision.selected_timeframe or 'none'} | {request.structural_question.value} | "
                f"{request.reason_code} | {decision.status.value}: {decision.stop_reason} |"
            )
        lines.append("")
    limitations = [
        *trace.warnings,
        *(
            error
            for validation in trace.bundle_validations
            for error in (*validation.errors, *validation.warnings)
        ),
    ]
    lines.append("**Missing or incompatible data:** " + ("; ".join(dict.fromkeys(limitations)) if limitations else "none recorded"))
    lines.append("")
    pair = trace.root_pair
    primary_hypothesis = pair.primary if pair else None
    alternative_hypothesis = pair.alternative if pair else None
    lines.extend(
        [
            "### Root Generation Audit",
            "",
            "**Primary provider called:** "
            + (
                "yes"
                if _role_provider_was_called(
                    trace,
                    CandidateGraphRole.PRIMARY,
                    primary_hypothesis,
                )
                else "no"
            ),
            "**Alternative provider called:** "
            + (
                "yes"
                if _role_provider_was_called(
                    trace,
                    CandidateGraphRole.ALTERNATIVE,
                    alternative_hypothesis,
                )
                else "no"
            ),
            f"**Root-stage provider calls recorded:** {trace.root_stage_call_count}",
            "",
        ]
    )
    lines.extend(
        _scenario_lines(
            trace,
            primary_hypothesis,
            role=CandidateGraphRole.PRIMARY,
            screen_status=(
                pair.primary_screen.status
                if pair is not None and pair.primary_screen is not None
                else None
            ),
            hard_errors=(
                pair.primary_screen.hard_rule_errors
                if pair is not None and pair.primary_screen is not None
                else ()
            ),
            unresolved=(
                pair.primary_screen.unresolved_requirements
                if pair is not None and pair.primary_screen is not None
                else ()
            ),
        )
    )
    lines.extend(
        _scenario_lines(
            trace,
            alternative_hypothesis,
            role=CandidateGraphRole.ALTERNATIVE,
            screen_status=pair.alternative_screen.status if pair and pair.alternative_screen else None,
            hard_errors=pair.alternative_screen.hard_rule_errors if pair and pair.alternative_screen else (),
            unresolved=pair.alternative_screen.unresolved_requirements if pair and pair.alternative_screen else (),
        )
    )
    lines.extend(_comparison_lines(trace))
    lines.extend(
        [
            "## STATUS NOTE",
            "",
            "Only deterministic verifier results are described as verified. Model-generated labels and prose remain candidate evidence. This report is technical structure context, not a trade instruction or probability estimate.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = ["render_adaptive_recount_report"]
