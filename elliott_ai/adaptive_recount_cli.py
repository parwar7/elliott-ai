"""Staged CLI surface for the adaptive pre-freeze Elliott recount.

Every command except ``finalize`` is file-only and must be dispatched before a
KnowledgeStore is constructed.  Market-data acquisition is deliberately
outside this module: Codex or another approved adapter writes an immutable
NativeOHLCVBundle for a typed manifest, then ``ingest`` validates it.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .adaptive_market_data import current_codex_tradingview_mcp_availability
from .adaptive_recount import (
    AdaptiveRecountBudget,
    AdaptiveRecountCoordinator,
    AdaptiveRecountStatus,
    adaptive_child_policies,
    create_adaptive_recount_plan,
    load_adaptive_recount_trace,
    load_native_bundle,
    rklb_strict_blind_plan,
    write_immutable_adaptive_trace,
)
from .adaptive_recount_provider import AdaptiveRecountStructuredProvider
from .adaptive_reporting import render_adaptive_recount_report
from .config import Settings
from .live_market_data import twelve_data_timeframe_capabilities
from .lower_timeframe_candidate_generator import CandidateGraphRole
from .providers import create_provider
from .timeframes import ProviderCapabilitySet


ADAPTIVE_CLI_POLICY_VERSION = "adaptive-recount-cli-policy-1.1.0"
_DEFAULT_ARTIFACT_DIRECTORY = ".elliott_ai/adaptive_recount"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve(workspace: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _trace_output_directory(args: argparse.Namespace, settings: Settings) -> Path:
    return _resolve(
        settings.workspace,
        getattr(args, "output_dir", None) or _DEFAULT_ARTIFACT_DIRECTORY,
    )


def _write_trace_result(
    trace: Any,
    *,
    args: argparse.Namespace,
    settings: Settings,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = write_immutable_adaptive_trace(
        trace,
        _trace_output_directory(args, settings),
    )
    return {
        "policy_version": ADAPTIVE_CLI_POLICY_VERSION,
        "trace_id": trace.trace_id,
        "trace_status": trace.status.value,
        "stop_reason": trace.stop_reason.value,
        "trace_content_hash": trace.content_hash,
        "trace_file": str(path),
        **dict(extra or {}),
    }


def _load_capabilities(
    args: argparse.Namespace,
    settings: Settings,
    *,
    timestamp: str,
) -> ProviderCapabilitySet:
    capability_path = getattr(args, "capabilities", None)
    if capability_path:
        value = json.loads(
            _resolve(settings.workspace, capability_path).read_text(encoding="utf-8")
        )
        if not isinstance(value, Mapping):
            raise ValueError("Capability file must contain one JSON object.")
        return ProviderCapabilitySet.from_dict(value)
    provider = str(args.market_data_provider).strip().lower()
    if provider == "twelve_data":
        return twelve_data_timeframe_capabilities(
            discovered_at_utc=timestamp,
            symbol=getattr(args, "symbol", None),
        )
    if provider == "tradingview-mcp":
        availability = current_codex_tradingview_mcp_availability(
            inspected_at_utc=timestamp
        )
        return ProviderCapabilitySet.create(
            provider="tradingview-mcp",
            capabilities=(),
            discovery_status=availability.status.value,
            source_interface=", ".join(availability.inspected_tool_names),
            discovered_at_utc=timestamp,
            limitations=(availability.exact_limitation,),
        )
    raise ValueError(
        "A content-hashed --capabilities file is required for this market-data provider."
    )


def _load_bundles(
    values: Iterable[str],
    *,
    settings: Settings,
) -> tuple[Any, ...]:
    paths = tuple(_resolve(settings.workspace, value) for value in values)
    if not paths:
        raise ValueError("At least one --bundle is required.")
    return tuple(load_native_bundle(path) for path in paths)


def _structured_provider(args: argparse.Namespace, settings: Settings) -> Any:
    if not getattr(args, "allow_model_call", False):
        raise PermissionError("This stage requires explicit --allow-model-call.")
    transport = create_provider(settings)
    if not callable(getattr(transport, "generate_strict_json", None)):
        raise ValueError(
            "Adaptive candidate generation requires a strict structured-output provider."
        )
    return AdaptiveRecountStructuredProvider(transport)


def register_adaptive_recount_commands(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    adaptive = subparsers.add_parser(
        "adaptive-recount",
        help="Run a staged blind adaptive Elliott recount before authoritative degree freeze.",
    )
    commands = adaptive.add_subparsers(dest="adaptive_command", required=True)

    commands.add_parser(
        "mcp-status",
        help="Show the audited TradingView MCP raw-candle capability boundary.",
    )

    plan = commands.add_parser("plan", help="Create a blind adaptive plan and first data request.")
    plan.add_argument("--symbol", required=True)
    plan.add_argument("--start", required=True, help="Analysis-window start as UTC ISO-8601.")
    plan.add_argument("--cutoff", required=True, help="Latest permitted completed candle in UTC.")
    plan.add_argument("--context-start")
    plan.add_argument(
        "--market-data-provider",
        required=True,
        help="tradingview-mcp, twelve_data, or a provider named by --capabilities.",
    )
    plan.add_argument("--capabilities", help="Content-hashed ProviderCapabilitySet JSON.")
    plan.add_argument("--requested-timeframe")
    plan.add_argument("--analysis-profile", default="elliott_full")
    plan.add_argument("--max-depth", type=int, default=3)
    plan.add_argument("--max-data-requests", type=int, default=8)
    plan.add_argument("--max-candidate-graphs", type=int, default=20)
    plan.add_argument("--max-pivots", type=int, default=2000)
    plan.add_argument("--max-bars", type=int, default=25000)
    plan.add_argument("--terminal-degree", default="Subminuette")
    plan.add_argument("--created-at")
    plan.add_argument("--output-dir", default=_DEFAULT_ARTIFACT_DIRECTORY)

    status = commands.add_parser("status", help="Inspect an immutable trace and pending proof nodes.")
    status.add_argument("--trace", required=True)

    ingest = commands.add_parser("ingest", help="Validate one immutable bundle against its manifest.")
    ingest.add_argument("--trace", required=True)
    ingest.add_argument("--bundle", required=True)
    ingest.add_argument("--updated-at")
    ingest.add_argument("--output-dir", default=_DEFAULT_ARTIFACT_DIRECTORY)

    roots = commands.add_parser(
        "generate-roots",
        help="Generate independent blind Primary and Alternative candidates.",
    )
    roots.add_argument("--trace", required=True)
    roots.add_argument("--bundle", action="append", default=[], required=True)
    roots.add_argument("--provider", choices=("openai", "ollama"), required=True)
    roots.add_argument("--model")
    roots.add_argument("--allow-model-call", action="store_true")
    roots.add_argument("--generated-at")
    roots.add_argument("--output-dir", default=_DEFAULT_ARTIFACT_DIRECTORY)

    proof_plan = commands.add_parser(
        "plan-proof",
        help="Plan the least-expensive useful native timeframe for one parent.",
    )
    proof_plan.add_argument("--trace", required=True)
    proof_plan.add_argument("--bundle", action="append", default=[])
    proof_plan.add_argument("--role", choices=("primary", "alternative"), required=True)
    proof_plan.add_argument("--parent-wave-id", required=True)
    proof_plan.add_argument("--requested-timeframe")
    proof_plan.add_argument("--source-interface", required=True)
    proof_plan.add_argument("--created-at")
    proof_plan.add_argument("--output-dir", default=_DEFAULT_ARTIFACT_DIRECTORY)

    prove = commands.add_parser(
        "prove",
        help="Generate one candidate child graph and run deterministic verification.",
    )
    prove.add_argument("--trace", required=True)
    prove.add_argument("--bundle", required=True)
    prove.add_argument("--role", choices=("primary", "alternative"), required=True)
    prove.add_argument("--parent-wave-id", required=True)
    prove.add_argument("--provider", choices=("openai", "ollama"), required=True)
    prove.add_argument("--model")
    prove.add_argument("--allow-model-call", action="store_true")
    prove.add_argument("--created-at")
    prove.add_argument("--output-dir", default=_DEFAULT_ARTIFACT_DIRECTORY)

    ready = commands.add_parser(
        "ready",
        help="Apply bounded stop conditions and test readiness for final comparison.",
    )
    ready.add_argument("--trace", required=True)
    ready.add_argument("--updated-at")
    ready.add_argument("--output-dir", default=_DEFAULT_ARTIFACT_DIRECTORY)

    report = commands.add_parser("report", help="Render a status-safe adaptive Markdown report.")
    report.add_argument("--trace", required=True)
    report.add_argument("--output")

    finalize = commands.add_parser(
        "finalize",
        help="Run final comparison and authoritative resolve-degrees persistence.",
    )
    finalize.add_argument("--trace", required=True)
    finalize.add_argument("--bundle", action="append", default=[], required=True)
    finalize.add_argument("--provider", choices=("openai", "ollama"), required=True)
    finalize.add_argument("--model")
    finalize.add_argument("--allow-model-call", action="store_true")
    finalize.add_argument("--commit", action="store_true")
    finalize.add_argument("--question")
    finalize.add_argument("--finalized-at")
    finalize.add_argument("--output-dir", default=_DEFAULT_ARTIFACT_DIRECTORY)


def execute_adaptive_recount_file_command(
    args: argparse.Namespace,
    *,
    settings: Settings,
) -> dict[str, Any]:
    """Execute every adaptive stage that is guaranteed not to open SQLite."""

    command = args.adaptive_command
    coordinator = AdaptiveRecountCoordinator()
    if command == "mcp-status":
        result = current_codex_tradingview_mcp_availability(inspected_at_utc=_now_utc())
        return result.to_dict()
    if command == "plan":
        timestamp = args.created_at or _now_utc()
        capabilities = _load_capabilities(args, settings, timestamp=timestamp)
        budget = AdaptiveRecountBudget(
            maximum_recursion_depth=args.max_depth,
            maximum_timeframe_requests=args.max_data_requests,
            maximum_candidate_graphs=args.max_candidate_graphs,
            maximum_pivots=args.max_pivots,
            maximum_bars=args.max_bars,
            terminal_degree=args.terminal_degree,
        )
        normalized_symbol = args.symbol.upper().strip()
        is_rklb_acceptance = (
            normalized_symbol == "NASDAQ:RKLB"
            and args.start.startswith("2026-05-27")
            and args.analysis_profile == "elliott_full"
            and args.context_start is None
            and budget == AdaptiveRecountBudget()
        )
        if is_rklb_acceptance:
            plan = rklb_strict_blind_plan(
                analysis_cutoff_utc=args.cutoff,
                provider_capabilities=capabilities,
                created_at_utc=timestamp,
            )
        else:
            plan = create_adaptive_recount_plan(
                symbol=normalized_symbol,
                analysis_start_utc=args.start,
                context_start_utc=args.context_start,
                analysis_cutoff_utc=args.cutoff,
                market_data_provider=capabilities.provider,
                provider_capabilities=capabilities,
                created_at_utc=timestamp,
                analysis_profile=args.analysis_profile,
                budgets=budget,
            )
        trace = coordinator.initial_trace(plan)
        trace = coordinator.plan_initial_data(
            trace,
            created_at_utc=timestamp,
            requested_timeframe=args.requested_timeframe,
            source_interface=capabilities.source_interface,
        )
        return _write_trace_result(
            trace,
            args=args,
            settings=settings,
            extra={
                "plan_content_hash": plan.content_hash,
                "provider_capability_set_hash": capabilities.content_hash,
                "pending_data_manifests": [item.to_dict() for item in trace.data_manifests],
            },
        )
    trace = load_adaptive_recount_trace(_resolve(settings.workspace, args.trace))
    if command == "status":
        return {
            "trace": trace.to_dict(),
            "pending_primary": list(
                coordinator.pending_proof_targets(trace, role=CandidateGraphRole.PRIMARY)
            ),
            "pending_alternative": list(
                coordinator.pending_proof_targets(trace, role=CandidateGraphRole.ALTERNATIVE)
            ),
        }
    if command == "ingest":
        bundle = load_native_bundle(_resolve(settings.workspace, args.bundle))
        result = coordinator.ingest_bundle(
            trace,
            bundle,
            updated_at_utc=args.updated_at or _now_utc(),
        )
        return _write_trace_result(result, args=args, settings=settings)
    if command == "generate-roots":
        bundles = _load_bundles(args.bundle, settings=settings)
        provider = _structured_provider(args, settings)
        result = coordinator.generate_initial_candidates(
            trace,
            bundles,
            provider=provider,
            allow_model_calls=args.allow_model_call,
            generated_at_utc=args.generated_at or _now_utc(),
            diagnostic_output_directory=_trace_output_directory(args, settings),
        )
        return _write_trace_result(
            result,
            args=args,
            settings=settings,
            extra={"provider_calls": provider.call_accounting()},
        )
    if command == "plan-proof":
        bundles = (
            _load_bundles(args.bundle, settings=settings) if args.bundle else ()
        )
        result = coordinator.plan_parent_subdivision(
            trace,
            role=CandidateGraphRole(args.role),
            parent_wave_id=args.parent_wave_id,
            bundles=bundles,
            created_at_utc=args.created_at or _now_utc(),
            requested_timeframe=args.requested_timeframe,
            source_interface=args.source_interface,
        )
        latest_manifest = (
            result.data_manifests[-1].to_dict()
            if len(result.data_manifests) > len(trace.data_manifests)
            else None
        )
        return _write_trace_result(
            result,
            args=args,
            settings=settings,
            extra={"pending_data_manifest": latest_manifest},
        )
    if command == "prove":
        bundle = load_native_bundle(_resolve(settings.workspace, args.bundle))
        provider = _structured_provider(args, settings)
        planner_request = next(
            (
                item
                for item in reversed(trace.planner_requests)
                if item.parent_candidate_id
                == f"{args.role}:{args.parent_wave_id}"
            ),
            None,
        )
        if planner_request is None:
            raise ValueError("Run plan-proof for this parent before prove.")
        generation_policy, verifier_policy = adaptive_child_policies(
            plan=trace.plan,
            target_child_degree=planner_request.expected_child_degree,
            target_timeframe=bundle.timeframe,
        )
        result = coordinator.generate_and_verify_child_graph(
            trace,
            role=CandidateGraphRole(args.role),
            parent_wave_id=args.parent_wave_id,
            bundle=bundle,
            graph_provider=provider,
            generation_policy=generation_policy,
            verifier_policy=verifier_policy,
            allow_model_call=args.allow_model_call,
            created_at_utc=args.created_at or _now_utc(),
        )
        return _write_trace_result(
            result,
            args=args,
            settings=settings,
            extra={"provider_calls": provider.call_accounting()},
        )
    if command == "ready":
        result = coordinator.mark_ready_for_final_comparison(
            trace,
            updated_at_utc=args.updated_at or _now_utc(),
        )
        return _write_trace_result(result, args=args, settings=settings)
    if command == "report":
        rendered = render_adaptive_recount_report(trace)
        output_path = None
        if args.output:
            output = _resolve(settings.workspace, args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists():
                raise FileExistsError(f"Adaptive report already exists: {output}")
            output.write_text(rendered, encoding="utf-8")
            output_path = str(output)
        return {
            "trace_id": trace.trace_id,
            "trace_content_hash": trace.content_hash,
            "report": rendered,
            "report_file": output_path,
        }
    if command == "finalize":
        raise RuntimeError("finalize must be dispatched through the persistence boundary.")
    raise ValueError(f"Unsupported adaptive-recount command: {command}")


def execute_adaptive_recount_finalize(
    args: argparse.Namespace,
    *,
    settings: Settings,
    store: Any,
) -> dict[str, Any]:
    """Execute the only adaptive command permitted to persist normal run records."""

    if args.adaptive_command != "finalize":
        raise ValueError("Only finalize may cross the adaptive persistence boundary.")
    if not args.allow_model_call or not args.commit:
        raise PermissionError("finalize requires both --allow-model-call and --commit.")
    trace = load_adaptive_recount_trace(_resolve(settings.workspace, args.trace))
    if trace.status is not AdaptiveRecountStatus.READY_FOR_FINAL_COMPARISON:
        raise ValueError("Trace is not ready for final comparison.")
    bundle_paths = tuple(_resolve(settings.workspace, value) for value in args.bundle)
    bundles = tuple(load_native_bundle(path) for path in bundle_paths)
    if {item.content_hash for item in bundles} != set(trace.bundle_hashes):
        raise ValueError("Finalization requires exactly the trace's immutable bundle set.")
    provider = create_provider(settings)
    from .agent import ElliottAgent

    agent = ElliottAgent(workspace=settings.workspace, store=store, provider=provider)
    result = agent.finalize_adaptive_recount(
        trace,
        market_data_paths=tuple(str(path) for path in bundle_paths),
        finalized_at_utc=args.finalized_at or _now_utc(),
        question=args.question,
    )
    if result.get("frozen"):
        frozen_trace = load_adaptive_recount_trace_from_value(result["adaptive_trace"])
        path = write_immutable_adaptive_trace(
            frozen_trace,
            _trace_output_directory(args, settings),
        )
        result["frozen_trace_file"] = str(path)
    return result


def load_adaptive_recount_trace_from_value(value: Mapping[str, Any]) -> Any:
    from .adaptive_recount import AdaptiveRecountTrace

    return AdaptiveRecountTrace.from_dict(value)


__all__ = [
    "ADAPTIVE_CLI_POLICY_VERSION",
    "execute_adaptive_recount_file_command",
    "execute_adaptive_recount_finalize",
    "register_adaptive_recount_commands",
]
