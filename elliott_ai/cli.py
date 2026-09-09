"""Command-line interface for the Elliott AI agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent import AnalysisRequest, ElliottAgent
from .adaptive_recount_cli import (
    execute_adaptive_recount_file_command,
    execute_adaptive_recount_finalize,
    register_adaptive_recount_commands,
)
from .config import Settings
from .correction_state import build_reviewed_outcome
from .experience_analogue import (
    COMPARISON_SPECIFICATION_VERSION,
    render_analogue_comparisons,
)
from .experience_retrieval import (
    DEFAULT_RESULT_LIMIT,
    DEFAULT_RETRIEVAL_POLICY_ID,
    DEFAULT_RETRIEVAL_POLICY_VERSION,
    render_analogue_retrieval,
    render_retrieval_policy,
)
from .experience_outcomes import (
    DEFAULT_HORIZON_ID,
    DEFAULT_HORIZON_VERSION,
    render_experience_evidence,
    render_outcome_source_audit,
    render_outcome_specification,
)
from .experience_workflow import (
    OUTCOME_REVIEW_DECISIONS,
    STRUCTURAL_REVIEW_DECISIONS,
    WORKFLOW_STATES,
    render_workflow,
)
from .indicators import SUPPORTED_CANDLE_FEATURES, with_optional_rsi
from .knowledge import KnowledgeStore
from .phase11_cli import (
    execute_phase11_shadow_command,
    register_phase11_shadow_commands,
)
from .providers import ProviderError, create_provider
from .reporting import (
    default_report_name,
    render_correction_lineage_report,
    write_degree_report,
)
from .review import build_correction_template
from .wave_metrics import calculate_resolution_analytics


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=True, indent=2))


def _load_json_mapping(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Review file {path} must contain one JSON object.")
    return value


def _emit_formatted(
    value: Any,
    *,
    output_format: str,
    output_path: str | None,
    workspace: Path,
    text_renderer: Any,
) -> None:
    rendered = (
        json.dumps(value, ensure_ascii=True, indent=2) + "\n"
        if output_format == "json"
        else text_renderer(value)
    )
    if output_path:
        output = Path(output_path)
        if not output.is_absolute():
            output = workspace / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


def _render_workflow_value(value: Any, *, explain: bool = False) -> str:
    if isinstance(value, dict):
        return render_workflow(value, explain=explain)
    if isinstance(value, list):
        lines = ["# Historical Experience Workflow Cases", ""]
        if not value:
            lines.append("No matching workflow cases are available.")
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{index}. {item.get('workflow_case_id') or item.get('source_endpoint_snapshot_id')}: "
                f"{item.get('state') or item.get('candidate_status')}"
            )
            if item.get("source_endpoint_snapshot_id"):
                lines.append(
                    f"   Source endpoint: {item.get('source_endpoint_snapshot_id')}"
                )
            if item.get("source_pair_hash"):
                lines.append(f"   Source-pair hash: {item.get('source_pair_hash')}")
            if explain and item.get("reason"):
                lines.append(f"   Reason: {item.get('reason')}")
        return "\n".join(lines) + "\n"
    return str(value) + "\n"


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.from_environment(
        workspace=Path(args.workspace) if args.workspace else None,
        database_path=Path(args.database) if args.database else None,
        provider=getattr(args, "provider", None),
        model=getattr(args, "model", None),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m elliott_ai",
        description="Evidence-bound Elliott Wave AI over the existing local brain.",
    )
    parser.add_argument("--workspace", help="Workspace containing the Elliott brain.")
    parser.add_argument("--database", help="SQLite retrieval/memory database path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("index", help="Rebuild the local knowledge index.")
    subparsers.add_parser("stats", help="Show knowledge and memory statistics.")

    search = subparsers.add_parser("search", help="Search the local Elliott brain.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--include-superseded", action="store_true")

    analyze = subparsers.add_parser("analyze", help="Build evidence and call a model provider.")
    analyze.add_argument("question")
    analyze.add_argument("--symbol", default="")
    analyze.add_argument("--timeframe", action="append", default=[])
    analyze.add_argument("--ohlcv", action="append", default=[])
    analyze.add_argument(
        "--context",
        action="append",
        default=[],
        help=(
            "Optional JSON context file. Repeat for benchmark, breadth, yields, "
            "options, fundamentals, news, or order-book data."
        ),
    )
    analyze.add_argument(
        "--feature",
        action="append",
        choices=tuple(sorted(SUPPORTED_CANDLE_FEATURES)),
        default=[],
        help=(
            "Candle calculation to enable. Repeat to test layers individually; "
            "omit to enable the default analytical features."
        ),
    )
    analyze.add_argument(
        "--enable-rsi",
        action="store_true",
        help="Explicitly add optional Wilder RSI(14) evidence to the selected features.",
    )
    analyze.add_argument("--waves", help="JSON file containing completed wave rows.")
    analyze.add_argument(
        "--wave-candles", help="OHLCV file whose timeframe matches the wave rows."
    )
    analyze.add_argument("--top-k", type=int, default=10)
    analyze.add_argument(
        "--provider", choices=("packet", "openai", "ollama"), default=None
    )
    analyze.add_argument("--model")
    analyze.add_argument(
        "--blind",
        action="store_true",
        help="Exclude all prior symbol counts and retrieve rules only.",
    )
    analyze.add_argument("--no-auto-data", action="store_true")
    analyze.add_argument("--output", help="Optional JSON result file.")

    refine = subparsers.add_parser(
        "refine", help="Recursively validate a prior run with focused 100-140 bar windows."
    )
    refine.add_argument("run_id", type=int)
    refine.add_argument("--question")
    refine.add_argument(
        "--provider", choices=("packet", "openai", "ollama"), default=None
    )
    refine.add_argument("--model")
    refine.add_argument("--output", help="Optional JSON result file.")

    correct = subparsers.add_parser(
        "correct-run", help="Create or store a structured human correction for a run."
    )
    correct.add_argument("run_id", type=int)
    correct_mode = correct.add_mutually_exclusive_group(required=True)
    correct_mode.add_argument(
        "--template", help="Write an editable correction template to this JSON file."
    )
    correct_mode.add_argument(
        "--file", help="Validate and store this completed correction JSON file."
    )

    resolve = subparsers.add_parser(
        "resolve-degrees",
        help="Assign and validate the strict standard degree hierarchy for a run.",
    )
    resolve.add_argument("run_id", type=int)
    resolve.add_argument("--question")
    resolve.add_argument(
        "--provider", choices=("packet", "openai", "ollama"), default=None
    )
    resolve.add_argument("--model")
    resolve.add_argument("--output", help="Optional JSON result file.")

    report = subparsers.add_parser(
        "report", help="Render the latest strict degree resolution as clean Markdown."
    )
    report.add_argument("run_id", type=int)
    report.add_argument("--resolution-id", type=int)
    report.add_argument("--output", help="Markdown output file.")

    add_source = subparsers.add_parser(
        "add-source", help="Register extracted book, transcript, or research text."
    )
    add_source.add_argument("path")
    add_source.add_argument("--kind", default="research")
    add_source.add_argument(
        "--status", choices=("candidate", "active", "canonical"), default="candidate"
    )

    runs = subparsers.add_parser("runs", help="List saved analysis runs.")
    runs.add_argument("--limit", type=int, default=20)

    show_run = subparsers.add_parser("show-run", help="Show one saved run.")
    show_run.add_argument("run_id", type=int)

    accept = subparsers.add_parser(
        "accept", help="Promote a reviewed run into approved case memory."
    )
    accept.add_argument("run_id", type=int)
    accept.add_argument("--title", required=True)
    accept.add_argument("--note", default="")
    accept.add_argument("--resolution-id", type=int)

    correction_cases = subparsers.add_parser(
        "correction-cases", help="List unresolved or reviewed correction cases."
    )
    correction_cases.add_argument(
        "--status", choices=("unresolved", "resolved", "all"), default="unresolved"
    )
    correction_cases.add_argument("--limit", type=int, default=100)

    correction_lineage = subparsers.add_parser(
        "correction-lineage", help="View the complete immutable snapshot lineage."
    )
    correction_lineage.add_argument("case_id")

    correction_evidence = subparsers.add_parser(
        "correction-evidence", help="View evidence available at one snapshot cutoff."
    )
    correction_evidence.add_argument("snapshot_id")

    correction_report = subparsers.add_parser(
        "correction-report", help="Write a clean correction-state Markdown report."
    )
    correction_report.add_argument("case_id")
    correction_report.add_argument("--output")

    import_case = subparsers.add_parser(
        "import-correction-case", help="Persist a correction case JSON and optional endpoint snapshot."
    )
    import_case.add_argument("file")

    import_snapshot = subparsers.add_parser(
        "import-correction-snapshot", help="Append one immutable correction snapshot JSON."
    )
    import_snapshot.add_argument("file")

    for command, help_text in (
        ("resolve-correction", "Assign an explicitly reviewed historical outcome."),
        ("revise-correction", "Revise a reviewed outcome without deleting prior history."),
    ):
        outcome_parser = subparsers.add_parser(command, help=help_text)
        outcome_parser.add_argument("case_id")
        outcome_parser.add_argument("--snapshot", required=True)
        outcome_parser.add_argument("--file", required=True)
        outcome_parser.add_argument("--reviewer", required=True)
        outcome_parser.add_argument("--resolution-cutoff", required=True)
        outcome_parser.add_argument("--confidence", default="not_stated")

    reject_correction = subparsers.add_parser(
        "reject-correction", help="Reject a proposed or prior outcome while preserving it."
    )
    reject_correction.add_argument("outcome_id")
    reject_correction.add_argument("--reviewer", required=True)
    reject_correction.add_argument("--reason", required=True)

    export_corrections = subparsers.add_parser(
        "export-corrections", help="Export explicitly reviewed correction cases."
    )
    export_corrections.add_argument("--output", required=True)

    experience = subparsers.add_parser(
        "experience",
        help="Manage reviewed experiences, structural filtering, and analogue comparison.",
    )
    experience_subparsers = experience.add_subparsers(
        dest="experience_command", required=True
    )
    experience_candidates = experience_subparsers.add_parser(
        "candidates", help="List explicitly eligible reviewed Phase 4 outcomes."
    )
    experience_candidates.add_argument("--include-ineligible", action="store_true")
    experience_candidates.add_argument("--limit", type=int, default=100)

    workflow_candidates = experience_subparsers.add_parser(
        "workflow-candidates",
        help="Discover immutable Phase 3/4 endpoint pairs without reading outcomes.",
    )
    workflow_candidates.add_argument("--include-invalid", action="store_true")
    workflow_candidates.add_argument("--limit", type=int, default=100)
    workflow_candidates.add_argument("--format", choices=("json", "text"), default="json")
    workflow_candidates.add_argument("--output")
    workflow_candidates.add_argument("--explain", action="store_true")
    workflow_candidates.add_argument("--show-provenance", action="store_true")
    workflow_candidates.add_argument("--show-hashes", action="store_true")

    workflow_spec = experience_subparsers.add_parser(
        "workflow-spec",
        help="Show the versioned population and human-review state machine.",
    )
    workflow_spec.add_argument("--format", choices=("json", "text"), default="json")
    workflow_spec.add_argument("--output")

    workflow_draft = experience_subparsers.add_parser(
        "draft", help="Assemble one immutable workflow draft from Phase 3/4 sources."
    )
    workflow_draft.add_argument("source_reference")
    workflow_draft.add_argument("--fingerprint-id", type=int)
    workflow_draft.add_argument("--fingerprint-hash")
    workflow_draft.add_argument("--supersedes")
    workflow_draft.add_argument("--actor", default="experience-workflow-service")
    workflow_draft.add_argument("--recorded-at")
    workflow_draft.add_argument("--format", choices=("json", "text"), default="json")
    workflow_draft.add_argument("--output")
    workflow_draft.add_argument("--explain", action="store_true")
    workflow_draft.add_argument("--show-provenance", action="store_true")
    workflow_draft.add_argument("--show-hashes", action="store_true")

    workflow_inspect = experience_subparsers.add_parser(
        "workflow-inspect", help="Inspect one workflow draft and its immutable event chain."
    )
    workflow_inspect.add_argument("workflow_case_id")
    workflow_inspect.add_argument("--format", choices=("json", "text"), default="json")
    workflow_inspect.add_argument("--output")
    workflow_inspect.add_argument("--explain", action="store_true")
    workflow_inspect.add_argument("--show-provenance", action="store_true")
    workflow_inspect.add_argument("--show-hashes", action="store_true")

    workflow_validate = experience_subparsers.add_parser(
        "validate", help="Revalidate immutable workflow sources without accepting them."
    )
    workflow_validate.add_argument("workflow_case_id")
    workflow_validate.add_argument("--actor", default="experience-workflow-validator")
    workflow_validate.add_argument("--recorded-at")
    workflow_validate.add_argument("--format", choices=("json", "text"), default="json")
    workflow_validate.add_argument("--output")
    workflow_validate.add_argument("--explain", action="store_true")

    workflow_structural_review = experience_subparsers.add_parser(
        "review-structure", help="Append one explicit human structural-review event."
    )
    workflow_structural_review.add_argument("workflow_case_id")
    workflow_structural_review.add_argument(
        "--decision", choices=tuple(sorted(STRUCTURAL_REVIEW_DECISIONS)), required=True
    )
    workflow_structural_review.add_argument("--review-file", required=True)
    workflow_structural_review.add_argument("--reviewer", required=True)
    workflow_structural_review.add_argument("--reviewed-at")
    workflow_structural_review.add_argument("--format", choices=("json", "text"), default="json")
    workflow_structural_review.add_argument("--output")
    workflow_structural_review.add_argument("--explain", action="store_true")
    workflow_structural_review.add_argument("--show-provenance", action="store_true")
    workflow_structural_review.add_argument("--show-hashes", action="store_true")

    workflow_outcome_review = experience_subparsers.add_parser(
        "review-outcome", help="Append one explicit human reviewed-outcome event."
    )
    workflow_outcome_review.add_argument("workflow_case_id")
    workflow_outcome_review.add_argument(
        "--decision", choices=tuple(sorted(OUTCOME_REVIEW_DECISIONS)), required=True
    )
    workflow_outcome_review.add_argument("--review-file", required=True)
    workflow_outcome_review.add_argument("--reviewer", required=True)
    workflow_outcome_review.add_argument("--reviewed-at")
    workflow_outcome_review.add_argument("--horizon", default=DEFAULT_HORIZON_ID)
    workflow_outcome_review.add_argument(
        "--horizon-version", default=DEFAULT_HORIZON_VERSION
    )
    workflow_outcome_review.add_argument("--format", choices=("json", "text"), default="json")
    workflow_outcome_review.add_argument("--output")
    workflow_outcome_review.add_argument("--explain", action="store_true")
    workflow_outcome_review.add_argument("--show-provenance", action="store_true")
    workflow_outcome_review.add_argument("--show-hashes", action="store_true")

    workflow_acceptance_status = experience_subparsers.add_parser(
        "acceptance-status", help="Show every final-acceptance prerequisite."
    )
    workflow_acceptance_status.add_argument("workflow_case_id")
    workflow_acceptance_status.add_argument("--format", choices=("json", "text"), default="json")
    workflow_acceptance_status.add_argument("--output")
    workflow_acceptance_status.add_argument("--explain", action="store_true")

    workflow_accept = experience_subparsers.add_parser(
        "accept", help="Explicitly accept one fully reviewed workflow case."
    )
    workflow_accept.add_argument("workflow_case_id")
    workflow_accept.add_argument("--reviewer", required=True)
    workflow_accept.add_argument("--notes", required=True)
    workflow_accept.add_argument("--reviewed-at")
    workflow_accept.add_argument("--format", choices=("json", "text"), default="json")
    workflow_accept.add_argument("--output")
    workflow_accept.add_argument("--explain", action="store_true")

    workflow_reject = experience_subparsers.add_parser(
        "reject", help="Append an explicit human final-rejection event."
    )
    workflow_reject.add_argument("workflow_case_id")
    workflow_reject.add_argument("--reviewer", required=True)
    workflow_reject.add_argument("--reason", required=True)
    workflow_reject.add_argument("--notes", required=True)
    workflow_reject.add_argument("--reviewed-at")
    workflow_reject.add_argument("--format", choices=("json", "text"), default="json")
    workflow_reject.add_argument("--output")
    workflow_reject.add_argument("--explain", action="store_true")

    workflow_withdraw = experience_subparsers.add_parser(
        "withdraw", help="Append an explicit human withdrawal event."
    )
    workflow_withdraw.add_argument("workflow_case_id")
    workflow_withdraw.add_argument("--reviewer", required=True)
    workflow_withdraw.add_argument("--notes", required=True)
    workflow_withdraw.add_argument("--reviewed-at")
    workflow_withdraw.add_argument("--format", choices=("json", "text"), default="json")
    workflow_withdraw.add_argument("--output")
    workflow_withdraw.add_argument("--explain", action="store_true")

    workflow_supersede = experience_subparsers.add_parser(
        "supersede", help="Explicitly supersede one workflow case with a linked replacement."
    )
    workflow_supersede.add_argument("workflow_case_id")
    workflow_supersede.add_argument("--replacement", required=True)
    workflow_supersede.add_argument("--actor", required=True)
    workflow_supersede.add_argument("--recorded-at")
    workflow_supersede.add_argument("--format", choices=("json", "text"), default="json")
    workflow_supersede.add_argument("--output")
    workflow_supersede.add_argument("--explain", action="store_true")

    workflow_pool = experience_subparsers.add_parser(
        "pool", help="List workflow cases explicitly accepted into the existing pool."
    )
    workflow_pool.add_argument("--state", choices=("all", *WORKFLOW_STATES), default="accepted")
    workflow_pool.add_argument("--limit", type=int, default=100)
    workflow_pool.add_argument("--format", choices=("json", "text"), default="json")
    workflow_pool.add_argument("--output")
    workflow_pool.add_argument("--explain", action="store_true")

    experience_create = experience_subparsers.add_parser(
        "create", help="Assemble and persist one pending experience candidate."
    )
    experience_create.add_argument("correction_case_id")
    experience_create.add_argument("--fingerprint-hash")
    experience_create.add_argument(
        "--endpoint-tolerance-seconds", type=float, default=0.0
    )

    experience_inspect = experience_subparsers.add_parser(
        "inspect", help="Inspect one case, its review lineage, DNA, quality, and tags."
    )
    experience_inspect.add_argument("experience_case_id")

    experience_review = experience_subparsers.add_parser(
        "review", help="Accept, reject, or quarantine a pending experience version."
    )
    experience_review.add_argument("experience_case_id")
    experience_review.add_argument(
        "--action", choices=("accept", "reject", "quarantine"), required=True
    )
    experience_review.add_argument("--reviewer", required=True)
    experience_review.add_argument("--rationale", required=True)
    experience_review.add_argument(
        "--quality", choices=("high", "medium", "low", "quarantined")
    )
    experience_review.add_argument(
        "--human-confirmed",
        action="store_true",
        help="Required for acceptance; confirms this is a named human decision.",
    )

    experience_revise = experience_subparsers.add_parser(
        "revise-review", help="Append a revision that supersedes the current review."
    )
    experience_revise.add_argument("experience_case_id")
    experience_revise.add_argument("--parent-review", required=True)
    experience_revise.add_argument(
        "--action", choices=("accept", "reject", "quarantine"), required=True
    )
    experience_revise.add_argument("--reviewer", required=True)
    experience_revise.add_argument("--rationale", required=True)
    experience_revise.add_argument(
        "--quality", choices=("high", "medium", "low", "quarantined")
    )
    experience_revise.add_argument("--human-confirmed", action="store_true")

    experience_quality = experience_subparsers.add_parser(
        "quality", help="Append a reviewable quality assignment."
    )
    experience_quality.add_argument("experience_case_id")
    experience_quality.add_argument(
        "--status", choices=("high", "medium", "low", "quarantined"), required=True
    )
    experience_quality.add_argument("--reviewer", required=True)
    experience_quality.add_argument("--rationale", required=True)

    experience_tag = experience_subparsers.add_parser(
        "tag", help="Append a namespaced tag add or removal action."
    )
    experience_tag.add_argument("experience_case_id")
    experience_tag.add_argument("--tag", required=True)
    experience_tag.add_argument("--action", choices=("add", "remove"), required=True)
    experience_tag.add_argument("--actor", required=True)
    experience_tag.add_argument("--rationale", default="")

    experience_dna = experience_subparsers.add_parser(
        "dna", help="Show endpoint, confirmation, or resolved-outcome DNA."
    )
    experience_dna.add_argument("experience_case_id")
    experience_dna.add_argument(
        "--kind", choices=("endpoint", "confirmation", "resolved_outcome")
    )

    experience_filter = experience_subparsers.add_parser(
        "filter",
        help="Filter reviewed experiences by endpoint structure without ranking them.",
    )
    experience_filter.add_argument("current_experience_case_id")
    experience_filter.add_argument("--same-degree-only", action="store_true")
    experience_filter.add_argument(
        "--no-adjacent-degree",
        action="store_true",
        help="Disable the default adjacent-degree search level.",
    )
    experience_filter.add_argument("--allow-far-degree", action="store_true")
    experience_filter.add_argument("--allow-cross-market", action="store_true")
    experience_filter.add_argument("--allow-context-family", action="store_true")

    experience_matrix = experience_subparsers.add_parser(
        "matrix", help="Show the exhaustive versioned structural compatibility matrix."
    )

    experience_compare = experience_subparsers.add_parser(
        "compare",
        help="Compare endpoint DNA for Phase 5A.2-eligible experiences without ranking.",
    )
    experience_compare.add_argument("current_experience_case_id")
    experience_compare.add_argument(
        "--candidate",
        action="append",
        default=[],
        help="Historical experience case ID to consider. Repeat as needed; omit for all.",
    )
    experience_compare.add_argument(
        "--level",
        type=int,
        choices=(1, 2, 3, 4),
        default=4,
        help="Maximum progressive comparison level to include.",
    )
    experience_compare.add_argument(
        "--spec-version", default=COMPARISON_SPECIFICATION_VERSION
    )
    experience_compare.add_argument(
        "--format", choices=("json", "text"), default="json"
    )
    experience_compare.add_argument("--output")
    experience_compare.add_argument("--explain", action="store_true")
    experience_compare.add_argument("--show-provenance", action="store_true")
    experience_compare.add_argument("--show-hashes", action="store_true")
    experience_compare.add_argument("--same-degree-only", action="store_true")
    experience_compare.add_argument("--no-adjacent-degree", action="store_true")
    experience_compare.add_argument("--allow-far-degree", action="store_true")
    experience_compare.add_argument("--allow-cross-market", action="store_true")
    experience_compare.add_argument("--allow-context-family", action="store_true")

    experience_spec = experience_subparsers.add_parser(
        "comparison-spec",
        help="Show the versioned Phase 5A.3 endpoint comparison specification.",
    )
    experience_spec.add_argument(
        "--spec-version", default=COMPARISON_SPECIFICATION_VERSION
    )

    experience_retrieve = experience_subparsers.add_parser(
        "retrieve",
        help="Order eligible endpoint analogues using a versioned deterministic policy.",
    )
    experience_retrieve.add_argument("current_experience_case_id")
    experience_retrieve.add_argument(
        "--candidate",
        action="append",
        default=[],
        help="Historical experience case ID to consider. Repeat as needed; omit for all.",
    )
    experience_retrieve.add_argument(
        "--policy", default=DEFAULT_RETRIEVAL_POLICY_ID
    )
    experience_retrieve.add_argument(
        "--policy-version", default=DEFAULT_RETRIEVAL_POLICY_VERSION
    )
    experience_retrieve.add_argument(
        "--comparison-level",
        "--level",
        dest="comparison_level",
        type=int,
        choices=(1, 2, 3, 4),
        default=4,
        help="Maximum progressive comparison level to include.",
    )
    experience_retrieve.add_argument(
        "--spec-version", default=COMPARISON_SPECIFICATION_VERSION
    )
    experience_retrieve.add_argument("--limit", type=int, default=DEFAULT_RESULT_LIMIT)
    experience_retrieve.add_argument(
        "--format", choices=("json", "text"), default="json"
    )
    experience_retrieve.add_argument("--output")
    experience_retrieve.add_argument("--explain", action="store_true")
    experience_retrieve.add_argument("--show-provenance", action="store_true")
    experience_retrieve.add_argument("--show-hashes", action="store_true")
    experience_retrieve.add_argument("--show-ordering-keys", action="store_true")
    experience_retrieve.add_argument(
        "--include-comparison-details", action="store_true"
    )
    experience_retrieve.add_argument("--same-degree-only", action="store_true")
    experience_retrieve.add_argument("--no-adjacent-degree", action="store_true")
    experience_retrieve.add_argument("--allow-far-degree", action="store_true")
    experience_retrieve.add_argument("--allow-cross-market", action="store_true")
    experience_retrieve.add_argument("--allow-context-family", action="store_true")

    experience_retrieval_policy = experience_subparsers.add_parser(
        "retrieval-policy",
        help="Show the versioned deterministic analogue-retrieval policy.",
    )
    experience_retrieval_policy.add_argument(
        "--policy", default=DEFAULT_RETRIEVAL_POLICY_ID
    )
    experience_retrieval_policy.add_argument(
        "--policy-version", default=DEFAULT_RETRIEVAL_POLICY_VERSION
    )
    experience_retrieval_policy.add_argument(
        "--format", choices=("json", "text"), default="json"
    )
    experience_retrieval_policy.add_argument("--output")

    experience_outcome_spec = experience_subparsers.add_parser(
        "outcome-spec",
        help="Show the versioned Phase 5B outcome evidence, taxonomy, and horizon specification.",
    )
    experience_outcome_spec.add_argument(
        "--format", choices=("json", "text"), default="json"
    )
    experience_outcome_spec.add_argument("--output")

    experience_outcomes = experience_subparsers.add_parser(
        "outcomes",
        help="Audit the reviewed historical outcome linked to one experience case.",
    )
    experience_outcomes.add_argument("experience_case_id")
    experience_outcomes.add_argument("--horizon", default=DEFAULT_HORIZON_ID)
    experience_outcomes.add_argument(
        "--horizon-version", default=DEFAULT_HORIZON_VERSION
    )
    experience_outcomes.add_argument(
        "--format", choices=("json", "text"), default="json"
    )
    experience_outcomes.add_argument("--output")
    experience_outcomes.add_argument("--explain", action="store_true")
    experience_outcomes.add_argument("--show-provenance", action="store_true")
    experience_outcomes.add_argument("--show-hashes", action="store_true")
    experience_outcomes.add_argument(
        "--include-outcome-details", action="store_true"
    )

    experience_evidence = experience_subparsers.add_parser(
        "evidence",
        help="Freeze analogue retrieval, then attach reviewed historical outcomes.",
    )
    experience_evidence.add_argument("current_experience_case_id")
    experience_evidence.add_argument(
        "--candidate",
        action="append",
        default=[],
        help="Historical experience case ID to consider. Repeat as needed; omit for all.",
    )
    experience_evidence.add_argument(
        "--policy", default=DEFAULT_RETRIEVAL_POLICY_ID
    )
    experience_evidence.add_argument(
        "--policy-version", default=DEFAULT_RETRIEVAL_POLICY_VERSION
    )
    experience_evidence.add_argument(
        "--comparison-level",
        "--level",
        dest="comparison_level",
        type=int,
        choices=(1, 2, 3, 4),
        default=4,
    )
    experience_evidence.add_argument(
        "--spec-version", default=COMPARISON_SPECIFICATION_VERSION
    )
    experience_evidence.add_argument("--limit", type=int, default=DEFAULT_RESULT_LIMIT)
    experience_evidence.add_argument("--horizon", default=DEFAULT_HORIZON_ID)
    experience_evidence.add_argument(
        "--horizon-version", default=DEFAULT_HORIZON_VERSION
    )
    experience_evidence.add_argument(
        "--format", choices=("json", "text"), default="json"
    )
    experience_evidence.add_argument("--output")
    experience_evidence.add_argument("--explain", action="store_true")
    experience_evidence.add_argument("--show-provenance", action="store_true")
    experience_evidence.add_argument("--show-hashes", action="store_true")
    experience_evidence.add_argument(
        "--include-retrieval-details", action="store_true"
    )
    experience_evidence.add_argument(
        "--include-outcome-details", action="store_true"
    )
    experience_evidence.add_argument("--same-degree-only", action="store_true")
    experience_evidence.add_argument("--no-adjacent-degree", action="store_true")
    experience_evidence.add_argument("--allow-far-degree", action="store_true")
    experience_evidence.add_argument("--allow-cross-market", action="store_true")
    experience_evidence.add_argument("--allow-context-family", action="store_true")

    experience_export = experience_subparsers.add_parser(
        "export", help="Export active experience versions and their review-gated state."
    )
    experience_export.add_argument("--output", required=True)
    experience_export.add_argument("--accepted-only", action="store_true")
    experience_export.add_argument("--include-superseded", action="store_true")
    register_adaptive_recount_commands(subparsers)
    register_phase11_shadow_commands(subparsers)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = _settings(args)
        if args.command == "adaptive-recount" and args.adaptive_command != "finalize":
            result = execute_adaptive_recount_file_command(args, settings=settings)
            _print_json(result)
            return
        store = KnowledgeStore(settings.database_path)

        if args.command == "adaptive-recount":
            result = execute_adaptive_recount_finalize(
                args,
                settings=settings,
                store=store,
            )
            _print_json(result)
            return

        if args.command == "phase11-shadow":
            try:
                result = execute_phase11_shadow_command(
                    args,
                    settings=settings,
                    store=store,
                )
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
            _print_json(result)
            return

        if args.command == "index":
            _print_json({**store.index_workspace(settings.workspace), **store.stats()})
            return
        if args.command == "stats":
            _print_json(store.stats())
            return
        if args.command == "search":
            if store.stats()["documents"] == 0:
                store.index_workspace(settings.workspace)
            hits = store.search(
                args.query,
                limit=args.limit,
                include_superseded=args.include_superseded,
            )
            _print_json([hit.to_dict() for hit in hits])
            return
        if args.command == "add-source":
            source_id = store.register_source(
                Path(args.path), kind=args.kind, status=args.status
            )
            index_stats = store.index_workspace(settings.workspace)
            _print_json({"source_id": source_id, **index_stats})
            return
        if args.command == "runs":
            _print_json(store.list_runs(args.limit))
            return
        if args.command == "show-run":
            run = store.get_run(args.run_id)
            if run is None:
                raise ValueError(f"Analysis run {args.run_id} does not exist.")
            run["latest_review"] = store.get_latest_review(args.run_id)
            run["degree_resolutions"] = store.list_degree_resolutions(args.run_id)
            _print_json(run)
            return
        if args.command == "experience":
            if args.experience_command == "workflow-candidates":
                candidates = store.discover_experience_workflow_candidates(
                    include_invalid=args.include_invalid,
                    limit=args.limit,
                )
                _emit_formatted(
                    candidates,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command == "workflow-spec":
                specification = store.experience_workflow_specification()
                _emit_formatted(
                    specification,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(value),
                )
                return
            if args.experience_command == "draft":
                drafted = store.create_experience_workflow_draft(
                    args.source_reference,
                    fingerprint_id=args.fingerprint_id,
                    fingerprint_content_hash_value=args.fingerprint_hash,
                    supersedes_workflow_case_id=args.supersedes,
                    actor_reference=args.actor,
                    recorded_at=args.recorded_at,
                )
                _emit_formatted(
                    drafted,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command == "workflow-inspect":
                inspected = store.inspect_experience_workflow_case(
                    args.workflow_case_id
                )
                _emit_formatted(
                    inspected,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command == "validate":
                validated = store.validate_experience_workflow_case(
                    args.workflow_case_id,
                    actor_reference=args.actor,
                    recorded_at=args.recorded_at,
                )
                _emit_formatted(
                    validated,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command in {"review-structure", "review-outcome"}:
                review_path = Path(args.review_file)
                if not review_path.is_absolute():
                    review_path = settings.workspace / review_path
                review_payload = _load_json_mapping(review_path)
                if args.experience_command == "review-structure":
                    reviewed = store.review_experience_workflow_structure(
                        args.workflow_case_id,
                        decision=args.decision,
                        reviewer=args.reviewer,
                        review=review_payload,
                        reviewed_at=args.reviewed_at,
                    )
                else:
                    reviewed = store.review_experience_workflow_outcome(
                        args.workflow_case_id,
                        decision=args.decision,
                        reviewer=args.reviewer,
                        review=review_payload,
                        horizon_id=args.horizon,
                        horizon_version=args.horizon_version,
                        reviewed_at=args.reviewed_at,
                    )
                _emit_formatted(
                    reviewed,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command == "acceptance-status":
                status = store.experience_workflow_acceptance_status(
                    args.workflow_case_id
                )
                _emit_formatted(
                    status,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command == "accept":
                accepted = store.accept_experience_workflow_case(
                    args.workflow_case_id,
                    reviewer=args.reviewer,
                    rationale=args.notes,
                    reviewed_at=args.reviewed_at,
                )
                _emit_formatted(
                    accepted,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command == "reject":
                rejected = store.reject_experience_workflow_case(
                    args.workflow_case_id,
                    reviewer=args.reviewer,
                    reason_code=args.reason,
                    notes=args.notes,
                    reviewed_at=args.reviewed_at,
                )
                _emit_formatted(
                    rejected,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command == "withdraw":
                withdrawn = store.withdraw_experience_workflow_case(
                    args.workflow_case_id,
                    reviewer=args.reviewer,
                    notes=args.notes,
                    reviewed_at=args.reviewed_at,
                )
                _emit_formatted(
                    withdrawn,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command == "supersede":
                superseded = store.supersede_experience_workflow_case(
                    args.workflow_case_id,
                    replacement_workflow_case_id=args.replacement,
                    actor_reference=args.actor,
                    recorded_at=args.recorded_at,
                )
                _emit_formatted(
                    superseded,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command == "pool":
                pooled = store.list_experience_workflow_cases(
                    state=args.state,
                    limit=args.limit,
                )
                _emit_formatted(
                    pooled,
                    output_format=args.format,
                    output_path=args.output,
                    workspace=settings.workspace,
                    text_renderer=lambda value: _render_workflow_value(
                        value, explain=args.explain
                    ),
                )
                return
            if args.experience_command == "candidates":
                _print_json(
                    store.list_experience_candidates(
                        include_ineligible=args.include_ineligible,
                        limit=args.limit,
                    )
                )
                return
            if args.experience_command == "create":
                _print_json(
                    store.create_experience_case(
                        args.correction_case_id,
                        fingerprint_content_hash=args.fingerprint_hash,
                        endpoint_alignment_tolerance_seconds=args.endpoint_tolerance_seconds,
                    )
                )
                return
            if args.experience_command == "inspect":
                _print_json(store.inspect_experience_case(args.experience_case_id))
                return
            if args.experience_command == "review":
                _print_json(
                    store.review_experience_case(
                        args.experience_case_id,
                        action=args.action,
                        reviewer=args.reviewer,
                        rationale=args.rationale,
                        quality_status=args.quality,
                        human_confirmed=args.human_confirmed,
                    )
                )
                return
            if args.experience_command == "revise-review":
                _print_json(
                    store.revise_experience_review(
                        args.experience_case_id,
                        parent_review_id=args.parent_review,
                        effective_action=args.action,
                        reviewer=args.reviewer,
                        rationale=args.rationale,
                        quality_status=args.quality,
                        human_confirmed=args.human_confirmed,
                    )
                )
                return
            if args.experience_command == "quality":
                _print_json(
                    store.assign_experience_quality(
                        args.experience_case_id,
                        quality_status=args.status,
                        reviewer=args.reviewer,
                        rationale=args.rationale,
                    )
                )
                return
            if args.experience_command == "tag":
                _print_json(
                    store.tag_experience_case(
                        args.experience_case_id,
                        tag=args.tag,
                        action=args.action,
                        actor=args.actor,
                        rationale=args.rationale,
                    )
                )
                return
            if args.experience_command == "dna":
                dna = store.get_pattern_dna(
                    args.experience_case_id, dna_kind=args.kind
                )
                if dna is None:
                    raise ValueError(
                        f"No matching Pattern DNA exists for {args.experience_case_id}."
                    )
                _print_json(dna)
                return
            if args.experience_command == "filter":
                _print_json(
                    store.filter_experience_cases(
                        args.current_experience_case_id,
                        config={
                            "same_degree_only": args.same_degree_only,
                            "allow_adjacent_degree": not args.no_adjacent_degree,
                            "allow_far_degree": args.allow_far_degree,
                            "allow_cross_market": args.allow_cross_market,
                            "allow_context_family": args.allow_context_family,
                        },
                    )
                )
                return
            if args.experience_command == "matrix":
                _print_json(store.experience_compatibility_matrix())
                return
            if args.experience_command == "comparison-spec":
                _print_json(
                    store.experience_comparison_specification(args.spec_version)
                )
                return
            if args.experience_command == "retrieval-policy":
                policy = store.experience_retrieval_policy(
                    args.policy, args.policy_version
                )
                rendered = (
                    json.dumps(policy, ensure_ascii=True, indent=2) + "\n"
                    if args.format == "json"
                    else render_retrieval_policy(policy)
                )
                if args.output:
                    output = Path(args.output)
                    if not output.is_absolute():
                        output = settings.workspace / output
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(rendered, encoding="utf-8")
                print(rendered, end="")
                return
            if args.experience_command == "outcome-spec":
                manifest = store.experience_outcome_specification()
                rendered = (
                    json.dumps(manifest, ensure_ascii=True, indent=2) + "\n"
                    if args.format == "json"
                    else render_outcome_specification(manifest)
                )
                if args.output:
                    output = Path(args.output)
                    if not output.is_absolute():
                        output = settings.workspace / output
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(rendered, encoding="utf-8")
                print(rendered, end="")
                return
            if args.experience_command == "outcomes":
                audited = store.inspect_experience_outcome(
                    args.experience_case_id,
                    horizon_id=args.horizon,
                    horizon_version=args.horizon_version,
                    include_outcome_details=args.include_outcome_details,
                )
                rendered = (
                    json.dumps(audited, ensure_ascii=True, indent=2) + "\n"
                    if args.format == "json"
                    else render_outcome_source_audit(
                        audited,
                        explain=args.explain,
                        show_provenance=args.show_provenance,
                        show_hashes=args.show_hashes,
                        include_outcome_details=args.include_outcome_details,
                    )
                )
                if args.output:
                    output = Path(args.output)
                    if not output.is_absolute():
                        output = settings.workspace / output
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(rendered, encoding="utf-8")
                print(rendered, end="")
                return
            if args.experience_command == "evidence":
                evidence = store.build_experience_evidence(
                    args.current_experience_case_id,
                    candidate_case_ids=(args.candidate or None),
                    maximum_comparison_level=args.comparison_level,
                    comparison_specification_version=args.spec_version,
                    policy_id=args.policy,
                    policy_version=args.policy_version,
                    limit=args.limit,
                    filter_config={
                        "same_degree_only": args.same_degree_only,
                        "allow_adjacent_degree": not args.no_adjacent_degree,
                        "allow_far_degree": args.allow_far_degree,
                        "allow_cross_market": args.allow_cross_market,
                        "allow_context_family": args.allow_context_family,
                    },
                    horizon_id=args.horizon,
                    horizon_version=args.horizon_version,
                    include_retrieval_details=args.include_retrieval_details,
                    include_outcome_details=args.include_outcome_details,
                )
                rendered = (
                    json.dumps(evidence, ensure_ascii=True, indent=2) + "\n"
                    if args.format == "json"
                    else render_experience_evidence(
                        evidence,
                        explain=args.explain,
                        show_provenance=args.show_provenance,
                        show_hashes=args.show_hashes,
                        include_retrieval_details=args.include_retrieval_details,
                        include_outcome_details=args.include_outcome_details,
                    )
                )
                if args.output:
                    output = Path(args.output)
                    if not output.is_absolute():
                        output = settings.workspace / output
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(rendered, encoding="utf-8")
                print(rendered, end="")
                return
            if args.experience_command == "retrieve":
                retrieved = store.retrieve_experience_analogues(
                    args.current_experience_case_id,
                    candidate_case_ids=(args.candidate or None),
                    maximum_comparison_level=args.comparison_level,
                    comparison_specification_version=args.spec_version,
                    policy_id=args.policy,
                    policy_version=args.policy_version,
                    limit=args.limit,
                    filter_config={
                        "same_degree_only": args.same_degree_only,
                        "allow_adjacent_degree": not args.no_adjacent_degree,
                        "allow_far_degree": args.allow_far_degree,
                        "allow_cross_market": args.allow_cross_market,
                        "allow_context_family": args.allow_context_family,
                    },
                )
                rendered = (
                    json.dumps(retrieved, ensure_ascii=True, indent=2) + "\n"
                    if args.format == "json"
                    else render_analogue_retrieval(
                        retrieved,
                        explain=args.explain,
                        show_provenance=args.show_provenance,
                        show_hashes=args.show_hashes,
                        show_ordering_keys=args.show_ordering_keys,
                        include_comparison_details=args.include_comparison_details,
                    )
                )
                if args.output:
                    output = Path(args.output)
                    if not output.is_absolute():
                        output = settings.workspace / output
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(rendered, encoding="utf-8")
                print(rendered, end="")
                return
            if args.experience_command == "compare":
                compared = store.compare_experience_cases(
                    args.current_experience_case_id,
                    candidate_case_ids=(args.candidate or None),
                    maximum_comparison_level=args.level,
                    specification_version=args.spec_version,
                    filter_config={
                        "same_degree_only": args.same_degree_only,
                        "allow_adjacent_degree": not args.no_adjacent_degree,
                        "allow_far_degree": args.allow_far_degree,
                        "allow_cross_market": args.allow_cross_market,
                        "allow_context_family": args.allow_context_family,
                    },
                )
                rendered = (
                    json.dumps(compared, ensure_ascii=True, indent=2) + "\n"
                    if args.format == "json"
                    else render_analogue_comparisons(
                        compared,
                        explain=args.explain,
                        show_provenance=args.show_provenance,
                        show_hashes=args.show_hashes,
                    )
                )
                if args.output:
                    output = Path(args.output)
                    if not output.is_absolute():
                        output = settings.workspace / output
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(rendered, encoding="utf-8")
                print(rendered, end="")
                return
            if args.experience_command == "export":
                output = Path(args.output)
                if not output.is_absolute():
                    output = settings.workspace / output
                output.parent.mkdir(parents=True, exist_ok=True)
                exported = store.export_experience_cases(
                    accepted_only=args.accepted_only,
                    include_superseded=args.include_superseded,
                )
                output.write_text(
                    json.dumps(exported, ensure_ascii=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                _print_json(
                    {
                        "export_file": str(output.resolve()),
                        "episode_count": exported["episode_count"],
                        "case_count": exported["case_count"],
                        "accepted_only": exported["accepted_only"],
                        "include_superseded_for_audit": exported[
                            "include_superseded_for_audit"
                        ],
                    }
                )
                return
        if args.command == "correction-cases":
            _print_json(
                store.list_correction_cases(status=args.status, limit=args.limit)
            )
            return
        if args.command == "correction-lineage":
            lineage = store.get_correction_lineage(args.case_id)
            if lineage is None:
                raise ValueError(f"Correction case {args.case_id} does not exist.")
            _print_json(lineage)
            return
        if args.command == "correction-evidence":
            evidence = store.get_snapshot_evidence(args.snapshot_id)
            if evidence is None:
                raise ValueError(f"Snapshot {args.snapshot_id} does not exist.")
            _print_json(evidence)
            return
        if args.command == "correction-report":
            lineage = store.get_correction_lineage(args.case_id)
            if lineage is None:
                raise ValueError(f"Correction case {args.case_id} does not exist.")
            output = Path(
                args.output or f"{args.case_id}_CORRECTION_STATE_REPORT.md"
            )
            if not output.is_absolute():
                output = settings.workspace / output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                render_correction_lineage_report(lineage), encoding="utf-8"
            )
            _print_json(
                {
                    "case_id": args.case_id,
                    "report_file": str(output.resolve()),
                    "snapshot_count": len(lineage.get("snapshots", [])),
                    "reviewed": lineage.get("current_reviewed_outcome") is not None,
                }
            )
            return
        if args.command == "import-correction-case":
            source = Path(args.file)
            if not source.is_absolute():
                source = settings.workspace / source
            payload = json.loads(source.read_text(encoding="utf-8"))
            case = payload.get("case") if isinstance(payload.get("case"), dict) else payload
            initial_snapshot = payload.get("initial_snapshot")
            saved = store.save_correction_case(
                case,
                initial_snapshot=(
                    initial_snapshot if isinstance(initial_snapshot, dict) else None
                ),
            )
            _print_json({**saved, "source_file": str(source.resolve())})
            return
        if args.command == "import-correction-snapshot":
            source = Path(args.file)
            if not source.is_absolute():
                source = settings.workspace / source
            snapshot = json.loads(source.read_text(encoding="utf-8"))
            saved = store.save_hypothesis_snapshot(snapshot)
            _print_json({**saved, "source_file": str(source.resolve())})
            return
        if args.command in {"resolve-correction", "revise-correction"}:
            snapshot = store.get_hypothesis_snapshot(args.snapshot)
            if snapshot is None or snapshot.get("case_id") != args.case_id:
                raise ValueError("Selected snapshot does not belong to this correction case.")
            source = Path(args.file)
            if not source.is_absolute():
                source = settings.workspace / source
            payload = json.loads(source.read_text(encoding="utf-8"))
            outcome = build_reviewed_outcome(
                snapshot,
                resolved_hypothesis=str(payload.get("resolved_hypothesis") or ""),
                final_reviewed_interpretation=str(
                    payload.get("final_reviewed_interpretation") or ""
                ),
                resolution_cutoff=args.resolution_cutoff,
                reviewer=args.reviewer,
                review_status="reviewed",
                resolution_confidence=args.confidence,
                supporting_future_structure=payload.get(
                    "supporting_future_structure", []
                ),
                rejected_alternatives=payload.get("rejected_alternatives", []),
                notes=str(payload.get("notes") or ""),
            )
            saved = (
                store.revise_outcome_resolution(outcome)
                if args.command == "revise-correction"
                else store.save_reviewed_outcome(outcome)
            )
            _print_json({**saved, "source_file": str(source.resolve())})
            return
        if args.command == "reject-correction":
            _print_json(
                store.reject_outcome_resolution(
                    args.outcome_id,
                    reviewer=args.reviewer,
                    reason=args.reason,
                )
            )
            return
        if args.command == "export-corrections":
            output = Path(args.output)
            if not output.is_absolute():
                output = settings.workspace / output
            output.parent.mkdir(parents=True, exist_ok=True)
            exported = store.export_reviewed_correction_cases()
            output.write_text(
                json.dumps(exported, ensure_ascii=True, indent=2) + "\n",
                encoding="utf-8",
            )
            _print_json(
                {
                    "export_file": str(output.resolve()),
                    "case_count": exported["case_count"],
                }
            )
            return
        if args.command == "correct-run":
            run = store.get_run(args.run_id)
            if run is None:
                raise ValueError(f"Analysis run {args.run_id} does not exist.")
            if args.template:
                output = Path(args.template)
                if not output.is_absolute():
                    output = settings.workspace / output
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(build_correction_template(run), ensure_ascii=True, indent=2)
                    + "\n",
                    encoding="utf-8",
                )
                _print_json(
                    {
                        "run_id": args.run_id,
                        "correction_template": str(output.resolve()),
                        "stored": False,
                    }
                )
                return
            correction_path = Path(args.file)
            if not correction_path.is_absolute():
                correction_path = settings.workspace / correction_path
            review = json.loads(correction_path.read_text(encoding="utf-8"))
            saved = store.save_review(args.run_id, review)
            _print_json({**saved, "source_file": str(correction_path.resolve())})
            return
        if args.command == "report":
            run = store.get_run(args.run_id)
            if run is None:
                raise ValueError(f"Analysis run {args.run_id} does not exist.")
            resolution = (
                store.get_degree_resolution(args.resolution_id)
                if args.resolution_id is not None
                else store.get_latest_degree_resolution(args.run_id)
            )
            if resolution is None or resolution.get("run_id") != args.run_id:
                raise ValueError(
                    "No matching degree resolution exists. Run resolve-degrees first."
                )
            readiness = resolution.setdefault("readiness", {})
            if (
                not readiness.get("analytical_metrics")
                and resolution.get("response", {}).get("degree_hierarchy")
            ):
                raw_paths = run.get("request", {}).get("market_data_paths", [])
                market_paths = []
                for raw_path in raw_paths:
                    path = Path(raw_path)
                    resolved_path = (
                        path.resolve()
                        if path.is_absolute()
                        else (settings.workspace / path).resolve()
                    )
                    if resolved_path.exists():
                        market_paths.append(resolved_path)
                features = (
                    resolution.get("request", {}).get("features")
                    or run.get("request", {}).get("features")
                )
                readiness["analytical_metrics"] = calculate_resolution_analytics(
                    resolution["response"],
                    market_paths,
                    workspace=settings.workspace,
                    features=features,
                    origin_run_id=args.run_id,
                )
            output = Path(
                args.output
                or default_report_name(str(run.get("symbol", "")), args.run_id)
            )
            if not output.is_absolute():
                output = settings.workspace / output
            report_path = write_degree_report(run, resolution, output)
            _print_json(
                {
                    "run_id": args.run_id,
                    "resolution_id": resolution["id"],
                    "report_file": str(report_path),
                    "final_report_ready": bool(
                        resolution.get("readiness", {}).get("final_report_ready")
                    ),
                    "blockers": resolution.get("readiness", {}).get("blockers", []),
                    "analytical_metrics_included": bool(
                        resolution.get("readiness", {}).get("analytical_metrics")
                    ),
                }
            )
            return
        if args.command == "accept":
            case_id = store.accept_run(
                args.run_id,
                title=args.title,
                user_note=args.note,
                resolution_id=args.resolution_id,
            )
            index_stats = store.index_workspace(settings.workspace)
            _print_json({"accepted_case_id": case_id, **index_stats})
            return
        if args.command == "analyze":
            provider = create_provider(settings)
            agent = ElliottAgent(
                workspace=settings.workspace,
                store=store,
                provider=provider,
            )
            requested_features = with_optional_rsi(
                tuple(args.feature),
                enabled=bool(args.enable_rsi or settings.rsi_enabled),
            )
            request = AnalysisRequest(
                question=args.question,
                symbol=args.symbol,
                timeframes=tuple(args.timeframe),
                market_data_paths=tuple(args.ohlcv),
                context_paths=tuple(args.context),
                features=requested_features,
                wave_path=args.waves,
                wave_candle_path=args.wave_candles,
                top_k=args.top_k,
                auto_discover_market_data=not args.no_auto_data,
                blind=args.blind,
            )
            result = agent.analyze(request)
            if args.output:
                output = Path(args.output)
                if not output.is_absolute():
                    output = settings.workspace / output
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(result, ensure_ascii=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                result["output_file"] = str(output.resolve())
            _print_json(result)
            return
        if args.command == "refine":
            provider = create_provider(settings)
            agent = ElliottAgent(
                workspace=settings.workspace,
                store=store,
                provider=provider,
            )
            result = agent.refine(args.run_id, args.question)
            if args.output:
                output = Path(args.output)
                if not output.is_absolute():
                    output = settings.workspace / output
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(result, ensure_ascii=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                result["output_file"] = str(output.resolve())
            _print_json(result)
            return
        if args.command == "resolve-degrees":
            provider = create_provider(settings)
            agent = ElliottAgent(
                workspace=settings.workspace,
                store=store,
                provider=provider,
            )
            result = agent.resolve_degrees(args.run_id, args.question)
            if args.output:
                output = Path(args.output)
                if not output.is_absolute():
                    output = settings.workspace / output
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(result, ensure_ascii=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                result["output_file"] = str(output.resolve())
            _print_json(result)
            return
        parser.error(f"Unknown command: {args.command}")
    except (OSError, ValueError, KeyError, ProviderError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
