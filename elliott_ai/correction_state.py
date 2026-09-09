"""Immutable, cutoff-safe correction hypotheses and reviewed outcomes.

This module deliberately does not choose an Elliott count. It records which
structurally valid interpretations were available at each cutoff and advances
their observable state as later candles and structure become available.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .correction_semantics import structure_family
from .evidence import make_evidence_item, validate_evidence_item
from .market_data import parse_date


CORRECTION_STATE_SCHEMA_VERSION = "correction-state-1.0.0"
CORRECTION_STATE_CALCULATION_VERSION = "correction-state-calc-1.0.0"
OUTCOME_SCHEMA_VERSION = "correction-outcome-1.0.0"

EVIDENCE_TIMING_CLASSES = frozenset(
    {
        "available_at_endpoint",
        "available_after_endpoint",
        "available_at_resolution",
        "human_review_only",
    }
)

HYPOTHESIS_IDS = frozenset(
    {
        "correction_complete",
        "larger_wave_a_complete",
        "larger_wave_w_complete",
        "triple_three_continuation",
        "correction_still_in_progress",
        "unresolved",
        "new_motive_wave_1",
        "corrective_wave_a",
        "local_wave_c_complete",
        "local_wave_y_complete",
    }
)

STATE_STAGES = (
    "structural_terminal_candidate",
    "structurally_complete",
    "terminal_evidence_supportive",
    "reversal_displacement_observed",
    "five_wave_move_away_candidate",
    "corrective_retracement_candidate",
    "origin_hold_observed",
    "next_motive_leg_candidate",
    "structurally_confirmed",
    "invalidated",
    "unresolved",
    "superseded",
)
STATE_STAGE_SET = frozenset(STATE_STAGES)
_PROGRESS_ORDER = {
    stage: index
    for index, stage in enumerate(
        (
            "unresolved",
            "structural_terminal_candidate",
            "structurally_complete",
            "terminal_evidence_supportive",
            "reversal_displacement_observed",
            "five_wave_move_away_candidate",
            "corrective_retracement_candidate",
            "origin_hold_observed",
            "next_motive_leg_candidate",
            "structurally_confirmed",
        )
    )
}

_DEFAULT_EVIDENCE_NEEDED: dict[str, list[str]] = {
    "correction_complete": [
        "Displacement and a local structure break away from the endpoint.",
        "A corrective retracement that protects the candidate endpoint.",
    ],
    "larger_wave_a_complete": [
        "A three-wave B and the later motive C required by the larger correction.",
    ],
    "larger_wave_w_complete": [
        "A corrective X and a later corrective Y at the proposed larger degree.",
    ],
    "triple_three_continuation": [
        "A corrective X2 followed by a valid corrective Z family.",
    ],
    "correction_still_in_progress": [
        "Additional child structure showing the terminal label was premature.",
    ],
    "unresolved": [
        "More price structure at a compatible lower timeframe.",
    ],
    "new_motive_wave_1": [
        "A corrective Wave 2 that holds the origin, followed by motive acceleration.",
    ],
    "corrective_wave_a": [
        "A corrective Wave B and subsequent Wave C behavior.",
    ],
    "local_wave_c_complete": [
        "Post-terminal displacement; local C completion alone does not confirm the parent correction.",
    ],
    "local_wave_y_complete": [
        "Post-terminal displacement or later X2 structure to determine the role of local Y.",
    ],
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any, *, field: str = "timestamp") -> datetime:
    if isinstance(value, Mapping):
        value = value.get("timestamp") or value.get("date") or value.get("time")
    parsed = parse_date(value)
    if parsed is None:
        raise ValueError(f"{field} must be a parseable timestamp.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any, *, field: str = "timestamp") -> str:
    return _timestamp(value, field=field).isoformat(timespec="seconds")


def _not_after(left: Any, right: Any) -> bool:
    return _timestamp(left) <= _timestamp(right)


def _event_time(value: Mapping[str, Any], fallback: str) -> str:
    raw = value.get("observed_at") or value.get("timestamp") or fallback
    return _iso(raw, field="event timestamp")


def _source_identity(source: Mapping[str, Any] | None, symbol: str, timeframe: str) -> dict[str, Any]:
    raw = dict(source or {})
    provider = str(raw.get("provider") or "unknown")
    venue = str(raw.get("venue") or raw.get("exchange") or "unknown")
    market_type = str(raw.get("market_type") or "unknown")
    resolved_symbol = str(raw.get("symbol") or raw.get("resolved_symbol") or symbol)
    feed_identity = str(
        raw.get("feed_identity")
        or "|".join((provider, venue, resolved_symbol, market_type, timeframe))
    )
    return {
        "provider": provider,
        "venue": venue,
        "symbol": resolved_symbol,
        "market_type": market_type,
        "timeframe": str(raw.get("timeframe") or timeframe),
        "feed_identity": feed_identity,
    }


def _normalize_endpoint(endpoint: Mapping[str, Any]) -> dict[str, Any]:
    price = _number(endpoint.get("price"))
    if price is None:
        raise ValueError("candidate_endpoint.price must be finite.")
    timestamp = _iso(
        endpoint.get("timestamp") or endpoint.get("date"),
        field="candidate endpoint timestamp",
    )
    terminal_direction = str(endpoint.get("terminal_direction") or "unknown").lower()
    reversal_direction = str(endpoint.get("expected_reversal_direction") or "").lower()
    if reversal_direction not in {"up", "down"}:
        reversal_direction = (
            "up" if terminal_direction == "down" else "down" if terminal_direction == "up" else "unknown"
        )
    protected = endpoint.get("protected_origin")
    protected_origin = dict(protected) if isinstance(protected, Mapping) else {}
    protected_price = _number(protected_origin.get("price"))
    if protected_price is None:
        protected_price = price
    protected_origin["price"] = protected_price
    protected_origin["timestamp"] = _iso(
        protected_origin.get("timestamp") or protected_origin.get("date") or timestamp,
        field="protected origin timestamp",
    )
    return {
        "timestamp": timestamp,
        "price": price,
        "terminal_label": str(endpoint.get("terminal_label") or "").upper(),
        "terminal_direction": terminal_direction,
        "expected_reversal_direction": reversal_direction,
        "protected_origin": protected_origin,
    }


def correction_case_content_hash(case: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(case))
    payload.pop("content_hash", None)
    payload.pop("case_id", None)
    return _hash(payload)


def snapshot_content_hash(snapshot: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(snapshot))
    payload.pop("content_hash", None)
    return _hash(payload)


def outcome_content_hash(outcome: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(outcome))
    payload.pop("content_hash", None)
    payload.pop("outcome_id", None)
    return _hash(payload)


def review_content_hash(review: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(review))
    payload.pop("content_hash", None)
    payload.pop("review_id", None)
    return _hash(payload)


def with_evidence_timing(
    evidence_item: Mapping[str, Any],
    *,
    timing_class: str,
    available_at: Any,
    evidence_reference: str | None = None,
) -> dict[str, Any]:
    """Return a timed copy of a Phase 3 evidence item."""
    errors = validate_evidence_item(evidence_item)
    if errors:
        raise ValueError("Invalid Phase 3 evidence item: " + " ".join(errors))
    timing = str(timing_class)
    if timing not in EVIDENCE_TIMING_CLASSES:
        raise ValueError(f"Unsupported evidence timing class: {timing_class!r}")
    result = deepcopy(dict(evidence_item))
    result["phase4_timing"] = {
        "timing_class": timing,
        "available_at": _iso(available_at, field="evidence availability timestamp"),
    }
    reference_payload = deepcopy(result)
    result["evidence_reference"] = evidence_reference or f"evidence_{_hash(reference_payload)[:24]}"
    return result


def _timed_evidence(
    item: Mapping[str, Any],
    *,
    default_timing: str,
    default_available_at: str,
) -> dict[str, Any]:
    timing = item.get("phase4_timing")
    if isinstance(timing, Mapping):
        result = deepcopy(dict(item))
        timing_class = str(timing.get("timing_class") or "")
        if timing_class not in EVIDENCE_TIMING_CLASSES:
            raise ValueError(f"Unsupported evidence timing class: {timing_class!r}")
        result["phase4_timing"] = {
            "timing_class": timing_class,
            "available_at": _iso(timing.get("available_at"), field="evidence availability timestamp"),
        }
        errors = validate_evidence_item(result)
        if errors:
            raise ValueError("Invalid Phase 3 evidence item: " + " ".join(errors))
        if not result.get("evidence_reference"):
            reference_payload = deepcopy(result)
            reference_payload.pop("evidence_reference", None)
            result["evidence_reference"] = f"evidence_{_hash(reference_payload)[:24]}"
        return result
    return with_evidence_timing(
        item,
        timing_class=default_timing,
        available_at=default_available_at,
    )


def _evidence_available(
    item: Mapping[str, Any],
    cutoff: str,
    *,
    reviewed_snapshot: bool = False,
) -> bool:
    timing = item.get("phase4_timing")
    if not isinstance(timing, Mapping):
        return False
    timing_class = str(timing.get("timing_class") or "")
    if timing_class in {"available_at_resolution", "human_review_only"} and not reviewed_snapshot:
        return False
    return _not_after(timing.get("available_at"), cutoff)


def _evidence_buckets(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    catalog: list[dict[str, Any]] = []
    references = {
        "supporting": [],
        "contradictory": [],
        "unavailable": [],
        "invalidating": [],
        "neutral": [],
    }
    seen: set[str] = set()
    for raw in items:
        item = deepcopy(dict(raw))
        reference = str(item.get("evidence_reference") or "")
        if not reference or reference in seen:
            continue
        seen.add(reference)
        catalog.append(item)
        status = str(item.get("status") or "neutral")
        if status == "supportive":
            references["supporting"].append(reference)
        elif status == "contradictory":
            references["contradictory"].append(reference)
        elif status in {"unavailable", "incomparable"}:
            references["unavailable"].append(reference)
        elif status == "invalidating":
            references["invalidating"].append(reference)
        else:
            references["neutral"].append(reference)
    return {"catalog": catalog, "references": references}


def _fingerprint_reference(raw: Mapping[str, Any], cutoff: str) -> dict[str, Any] | None:
    content_hash = str(raw.get("content_hash") or "")
    if not content_hash:
        raise ValueError("Every fingerprint reference requires its original content_hash.")
    fingerprint_cutoff = raw.get("cutoff")
    if isinstance(fingerprint_cutoff, Mapping):
        fingerprint_cutoff = fingerprint_cutoff.get("timestamp")
    if fingerprint_cutoff and not _not_after(fingerprint_cutoff, cutoff):
        return None
    identity = raw.get("identity") if isinstance(raw.get("identity"), Mapping) else {}
    return {
        "content_hash": content_hash,
        "identity_hash": raw.get("identity_hash"),
        "wave_id": identity.get("wave_id") or raw.get("wave_id"),
        "feature_schema_version": raw.get("feature_schema_version"),
        "calculation_version": raw.get("calculation_version"),
        "cutoff": _iso(fingerprint_cutoff, field="fingerprint cutoff") if fingerprint_cutoff else None,
    }


def _fingerprint_references(
    items: Iterable[Mapping[str, Any]], cutoff: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        reference = _fingerprint_reference(item, cutoff)
        if reference is None or reference["content_hash"] in seen:
            continue
        seen.add(reference["content_hash"])
        result.append(reference)
    return result


def derive_structurally_valid_hypotheses(
    *,
    parent_pattern_family: str,
    terminal_label: str,
    candidate_hypotheses: Iterable[str | Mapping[str, Any]] | None = None,
) -> list[str]:
    """Derive only context-relevant candidates, or filter supplied valid ones."""
    if candidate_hypotheses is not None:
        result: list[str] = []
        for raw in candidate_hypotheses:
            if isinstance(raw, Mapping):
                if raw.get("structurally_valid") is not True:
                    continue
                hypothesis_id = str(raw.get("hypothesis_id") or "")
            else:
                hypothesis_id = str(raw)
            if hypothesis_id not in HYPOTHESIS_IDS:
                raise ValueError(f"Unsupported correction hypothesis: {hypothesis_id!r}")
            if hypothesis_id not in result:
                result.append(hypothesis_id)
        if not result:
            raise ValueError("At least one structurally valid hypothesis is required.")
        return result

    family_text = str(parent_pattern_family).strip().lower().replace("_", "-")
    family = structure_family(parent_pattern_family)
    label = str(terminal_label).strip().upper()
    if label == "Y" or "double-three" in family_text or "w-x-y" in family_text:
        return [
            "local_wave_y_complete",
            "correction_complete",
            "larger_wave_w_complete",
            "larger_wave_a_complete",
            "triple_three_continuation",
            "correction_still_in_progress",
            "unresolved",
        ]
    if label == "Z" or "triple-three" in family_text:
        return [
            "correction_complete",
            "larger_wave_w_complete",
            "larger_wave_a_complete",
            "correction_still_in_progress",
            "unresolved",
        ]
    if label == "C" and family in {"zigzag", "flat"}:
        return [
            "local_wave_c_complete",
            "correction_complete",
            "larger_wave_w_complete",
            "larger_wave_a_complete",
            "correction_still_in_progress",
            "unresolved",
        ]
    if label in {"1", "A"} and family in {"impulse", "diagonal", "other"}:
        return ["new_motive_wave_1", "corrective_wave_a", "unresolved"]
    return ["correction_still_in_progress", "unresolved"]


def create_correction_case(
    *,
    symbol: str,
    timeframe: str,
    candidate_endpoint: Mapping[str, Any],
    elliott_degree: str,
    parent_pattern_family: str,
    source_identity: Mapping[str, Any] | None = None,
    source_run_id: int | None = None,
    source_resolution_id: int | None = None,
    structurally_valid_hypotheses: Iterable[str | Mapping[str, Any]] | None = None,
    explicit_price_invalidation_levels: Mapping[str, Any] | None = None,
    created_at: Any | None = None,
) -> dict[str, Any]:
    """Create deterministic case metadata without inventing historical outcomes."""
    if not str(symbol).strip() or not str(timeframe).strip():
        raise ValueError("symbol and timeframe are required.")
    endpoint = _normalize_endpoint(candidate_endpoint)
    hypotheses = derive_structurally_valid_hypotheses(
        parent_pattern_family=parent_pattern_family,
        terminal_label=endpoint["terminal_label"],
        candidate_hypotheses=structurally_valid_hypotheses,
    )
    case: dict[str, Any] = {
        "schema_version": CORRECTION_STATE_SCHEMA_VERSION,
        "calculation_version": CORRECTION_STATE_CALCULATION_VERSION,
        "source_run_id": source_run_id,
        "source_resolution_id": source_resolution_id,
        "source_identity": _source_identity(source_identity, str(symbol), str(timeframe)),
        "symbol": str(symbol),
        "timeframe": str(timeframe),
        "candidate_endpoint": endpoint,
        "elliott_degree": str(elliott_degree),
        "parent_pattern_family": str(parent_pattern_family),
        "initial_hypotheses": hypotheses,
        "explicit_price_invalidation_levels": dict(explicit_price_invalidation_levels or {}),
        "created_at": _iso(created_at or endpoint["timestamp"], field="case creation timestamp"),
    }
    content_hash = correction_case_content_hash(case)
    case["case_id"] = f"correction_{content_hash[:24]}"
    case["content_hash"] = correction_case_content_hash(case)
    return case


def _initial_stage(hypothesis_id: str, terminal_label: str) -> str:
    if hypothesis_id == "unresolved":
        return "unresolved"
    if hypothesis_id in {"local_wave_c_complete", "local_wave_y_complete"}:
        return "structurally_complete"
    if hypothesis_id in {"new_motive_wave_1", "corrective_wave_a"}:
        return "five_wave_move_away_candidate"
    if terminal_label == "Z" and hypothesis_id == "correction_complete":
        return "structurally_complete"
    return "structural_terminal_candidate"


def _state_record(hypothesis_id: str, stage: str, *, created_at: str) -> dict[str, Any]:
    return {
        "hypothesis_id": hypothesis_id,
        "state": stage,
        "status": "active" if stage not in {"invalidated", "superseded"} else stage,
        "provisional": stage != "structurally_confirmed",
        "created_at": created_at,
        "last_changed_at": created_at,
        "evidence_needed_next": deepcopy(_DEFAULT_EVIDENCE_NEEDED[hypothesis_id]),
        "structural_invalidations": [],
    }


def _transition(
    *,
    case_id: str,
    snapshot_id: str,
    hypothesis_id: str,
    previous_state: str,
    new_state: str,
    triggering: Sequence[str],
    contradictory: Sequence[str],
    unavailable: Sequence[str],
    structural_rule: str | None,
    price_invalidation_level: Any,
    cutoff: str,
    source_run_id: int | None,
    timestamp: str,
    provisional: bool,
    reason: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "case_id": case_id,
        "snapshot_id": snapshot_id,
        "hypothesis_id": hypothesis_id,
        "previous_state": previous_state,
        "new_state": new_state,
        "triggering_evidence_references": list(triggering),
        "contradictory_evidence_references": list(contradictory),
        "unavailable_evidence_references": list(unavailable),
        "hard_structural_rule": structural_rule,
        "price_invalidation_level": price_invalidation_level,
        "analysis_cutoff": cutoff,
        "source_run_id": source_run_id,
        "calculation_version": CORRECTION_STATE_CALCULATION_VERSION,
        "transition_timestamp": timestamp,
        "provisional": bool(provisional),
        "confirmed": not bool(provisional),
        "reason": reason,
    }
    payload["transition_id"] = f"transition_{_hash(payload)[:24]}"
    return payload


def _aggregate_state(states: Iterable[Mapping[str, Any]]) -> str:
    active = [
        str(item.get("state"))
        for item in states
        if str(item.get("status")) == "active"
    ]
    if not active:
        return "unresolved"
    return max(active, key=lambda stage: _PROGRESS_ORDER.get(stage, -1))


def _finalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    identity_payload = {
        "case_id": snapshot["case_id"],
        "parent_snapshot_id": snapshot.get("parent_snapshot_id"),
        "cutoff": snapshot["cutoff"],
        "candidate_endpoint": snapshot["candidate_endpoint"],
        "hypothesis_states": snapshot["hypothesis_states"],
        "evidence_catalog": snapshot["evidence_catalog"],
        "evidence_references": snapshot["evidence_references"],
        "fingerprint_references": snapshot["fingerprint_references"],
        "post_terminal_events": snapshot["post_terminal_events"],
    }
    snapshot["snapshot_id"] = f"snapshot_{_hash(identity_payload)[:24]}"
    for item in snapshot.get("transitions", []):
        item["snapshot_id"] = snapshot["snapshot_id"]
        transition_payload = deepcopy(item)
        transition_payload.pop("transition_id", None)
        item["transition_id"] = f"transition_{_hash(transition_payload)[:24]}"
    snapshot["content_hash"] = snapshot_content_hash(snapshot)
    return snapshot


def create_initial_snapshot(
    case: Mapping[str, Any],
    *,
    evidence_items: Iterable[Mapping[str, Any]] = (),
    fingerprint_references: Iterable[Mapping[str, Any]] = (),
    cutoff: Any | None = None,
    transition_timestamp: Any | None = None,
) -> dict[str, Any]:
    """Create the endpoint snapshot containing endpoint-time evidence only."""
    case_copy = deepcopy(dict(case))
    if case_copy.get("content_hash") != correction_case_content_hash(case_copy):
        raise ValueError("Correction case content_hash does not match its canonical payload.")
    endpoint = case_copy["candidate_endpoint"]
    snapshot_cutoff = _iso(cutoff or endpoint["timestamp"], field="snapshot cutoff")
    if not _not_after(endpoint["timestamp"], snapshot_cutoff):
        raise ValueError("Initial snapshot cutoff cannot precede the candidate endpoint.")
    timestamp = _iso(transition_timestamp or snapshot_cutoff, field="transition timestamp")
    timed = [
        _timed_evidence(
            item,
            default_timing="available_at_endpoint",
            default_available_at=endpoint["timestamp"],
        )
        for item in evidence_items
    ]
    available = [item for item in timed if _evidence_available(item, snapshot_cutoff)]
    buckets = _evidence_buckets(available)
    states = [
        _state_record(
            hypothesis_id,
            _initial_stage(hypothesis_id, endpoint["terminal_label"]),
            created_at=timestamp,
        )
        for hypothesis_id in case_copy["initial_hypotheses"]
    ]
    snapshot: dict[str, Any] = {
        "schema_version": CORRECTION_STATE_SCHEMA_VERSION,
        "calculation_version": CORRECTION_STATE_CALCULATION_VERSION,
        "case_id": case_copy["case_id"],
        "parent_snapshot_id": None,
        "source_run_id": case_copy.get("source_run_id"),
        "source_resolution_id": case_copy.get("source_resolution_id"),
        "source_identity": case_copy["source_identity"],
        "symbol": case_copy["symbol"],
        "timeframe": case_copy["timeframe"],
        "cutoff": snapshot_cutoff,
        "candidate_endpoint": endpoint,
        "elliott_degree": case_copy["elliott_degree"],
        "parent_pattern_family": case_copy["parent_pattern_family"],
        "current_state": _aggregate_state(states),
        "provisional": True,
        "reviewed": False,
        "active_hypotheses": [item["hypothesis_id"] for item in states],
        "invalidated_hypotheses": [],
        "hypothesis_states": states,
        "structural_invalidations": [],
        "evidence_catalog": buckets["catalog"],
        "evidence_references": buckets["references"],
        "fingerprint_references": _fingerprint_references(
            fingerprint_references, snapshot_cutoff
        ),
        "post_terminal_events": {},
        "origin_and_invalidation_levels": {
            "protected_origin": endpoint["protected_origin"],
            "explicit_levels": case_copy.get("explicit_price_invalidation_levels", {}),
        },
        "timeline": {
            "candidate_endpoint_time": endpoint["timestamp"],
            "first_structural_completion_time": (
                endpoint["timestamp"]
                if any(item["state"] == "structurally_complete" for item in states)
                else None
            ),
            "first_displacement_confirmation_time": None,
            "first_five_away_candidate_time": (
                endpoint["timestamp"]
                if any(item["state"] == "five_wave_move_away_candidate" for item in states)
                else None
            ),
            "retracement_confirmation_time": None,
            "final_reviewed_resolution_time": None,
        },
        "transitions": [],
        "created_at": timestamp,
    }
    snapshot = _finalize_snapshot(snapshot)
    for state in snapshot["hypothesis_states"]:
        snapshot["transitions"].append(
            _transition(
                case_id=snapshot["case_id"],
                snapshot_id=snapshot["snapshot_id"],
                hypothesis_id=state["hypothesis_id"],
                previous_state="unresolved",
                new_state=state["state"],
                triggering=buckets["references"]["supporting"],
                contradictory=buckets["references"]["contradictory"],
                unavailable=buckets["references"]["unavailable"],
                structural_rule="Phase 1 child-family structure is declared structurally valid.",
                price_invalidation_level=case_copy.get(
                    "explicit_price_invalidation_levels", {}
                ).get(state["hypothesis_id"]),
                cutoff=snapshot_cutoff,
                source_run_id=case_copy.get("source_run_id"),
                timestamp=timestamp,
                provisional=True,
                reason="Structurally valid hypothesis created at the endpoint cutoff.",
            )
        )
    snapshot["content_hash"] = snapshot_content_hash(snapshot)
    return snapshot


def _prepare_candles(candles: Iterable[Mapping[str, Any]], cutoff: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    cutoff_time = _timestamp(cutoff)
    for index, raw in enumerate(candles):
        date_raw = raw.get("parsed_date") or raw.get("date") or raw.get("time") or raw.get("timestamp")
        parsed = parse_date(date_raw)
        close = _number(raw.get("close"))
        high = _number(raw.get("high"))
        low = _number(raw.get("low"))
        if parsed is None or close is None or high is None or low is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        if parsed > cutoff_time:
            continue
        result.append(
            {
                "timestamp": parsed.isoformat(timespec="seconds"),
                "close": close,
                "high": high,
                "low": low,
                "source_position": raw.get("source_position", index),
            }
        )
    result.sort(key=lambda row: row["timestamp"])
    return result


def _context_event(
    context: Mapping[str, Any],
    keys: Sequence[str],
    cutoff: str,
    *,
    unavailable_reason: str,
) -> dict[str, Any]:
    raw: Any = None
    for key in keys:
        if key in context:
            raw = context[key]
            break
    if not isinstance(raw, Mapping):
        return {
            "status": "unavailable",
            "observed_at": None,
            "provisional": True,
            "reason": unavailable_reason,
        }
    event_time = _event_time(raw, cutoff)
    if not _not_after(event_time, cutoff):
        return {
            "status": "unavailable",
            "observed_at": None,
            "provisional": True,
            "reason": "The supplied event occurs after this snapshot cutoff.",
        }
    status = str(raw.get("status") or ("observed" if raw.get("observed") is True else "candidate"))
    return {
        "status": status,
        "observed_at": event_time,
        "provisional": bool(raw.get("provisional", status != "observed")),
        "reason": str(raw.get("reason") or "Explicit structural observation supplied."),
        "details": deepcopy(dict(raw)),
    }


def _pivot_price(raw: Any) -> float | None:
    return _number(raw.get("price")) if isinstance(raw, Mapping) else _number(raw)


def _pivot_timestamp(raw: Any) -> Any:
    if not isinstance(raw, Mapping):
        return None
    return raw.get("timestamp") or raw.get("date") or raw.get("time")


def _five_wave_event(context: Mapping[str, Any], cutoff: str) -> dict[str, Any]:
    raw = context.get("five_wave_move_away") or context.get("five_wave_structure")
    if not isinstance(raw, Mapping):
        return {
            "status": "unavailable",
            "observed_at": None,
            "provisional": True,
            "reason": "Compatible lower-timeframe pivots or five child waves were not supplied.",
        }
    if str(raw.get("status")) == "unavailable":
        return {
            "status": "unavailable",
            "observed_at": None,
            "provisional": True,
            "reason": str(raw.get("reason") or "Lower-timeframe structure is unavailable."),
        }
    pivots = list(raw.get("pivots") or [])
    children = [item for item in raw.get("children", []) if isinstance(item, Mapping)]
    has_pivots = len(pivots) >= 6 and all(_pivot_price(item) is not None for item in pivots[:6])
    positions = {
        str(item.get("sequence_position") or item.get("wave") or item.get("label"))
        .replace("(", "")
        .replace(")", "")
        for item in children
    }
    anchored_children = [
        item
        for item in children
        if isinstance(item.get("start"), Mapping)
        and isinstance(item.get("end"), Mapping)
        and _number(item["start"].get("price")) is not None
        and _number(item["end"].get("price")) is not None
        and (item["start"].get("timestamp") or item["start"].get("date"))
        and (item["end"].get("timestamp") or item["end"].get("date"))
    ]
    has_children = {"1", "2", "3", "4", "5"}.issubset(positions) and len(
        anchored_children
    ) >= 5
    if not has_pivots and not has_children:
        return {
            "status": "candidate",
            "observed_at": None,
            "provisional": True,
            "reason": "A directional move was supplied without six pivots or five actual child waves.",
            "details": {"structure_verified": False},
        }
    timestamps = [_pivot_timestamp(item) for item in pivots[:6] if _pivot_timestamp(item)]
    child_times = [
        (item.get("end") or {}).get("date")
        for item in children
        if isinstance(item.get("end"), Mapping) and (item.get("end") or {}).get("date")
    ]
    times = timestamps or child_times
    if any(not _not_after(value, cutoff) for value in times):
        return {
            "status": "candidate",
            "observed_at": None,
            "provisional": True,
            "reason": "The proposed five-wave structure is not complete at this cutoff.",
            "details": {"structure_verified": False},
        }
    hard_valid = raw.get("hard_rules_valid")
    reason = "Five actual child waves were supplied."
    if has_pivots:
        prices = [float(_pivot_price(item)) for item in pivots[:6]]
        direction = str(raw.get("direction") or ("up" if prices[-1] > prices[0] else "down"))
        sign = 1.0 if direction == "up" else -1.0
        adjusted = [price * sign for price in prices]
        lengths = [
            adjusted[1] - adjusted[0],
            adjusted[3] - adjusted[2],
            adjusted[5] - adjusted[4],
        ]
        family = structure_family(raw.get("pattern_family") or raw.get("structure") or "impulse")
        pivot_rules = (
            adjusted[1] > adjusted[0]
            and adjusted[2] > adjusted[0]
            and adjusted[3] > adjusted[1]
            and adjusted[3] > adjusted[2]
            and adjusted[4] < adjusted[3]
            and min(lengths) != lengths[1]
        )
        if family != "diagonal":
            pivot_rules = pivot_rules and adjusted[4] > adjusted[1]
        if hard_valid is False or not pivot_rules:
            return {
                "status": "not_observed",
                "observed_at": times[-1] if times else cutoff,
                "provisional": True,
                "reason": "Supplied pivots fail one or more hard motive-structure checks.",
            }
        reason = "Six aligned pivots pass the supplied motive-family hard checks."
    elif hard_valid is not True:
        return {
            "status": "candidate",
            "observed_at": times[-1] if times else cutoff,
            "provisional": True,
            "reason": "Five child labels exist, but hard-rule validation was not supplied.",
            "details": {"structure_verified": False},
        }
    observed_at = _iso(times[-1], field="five-wave completion timestamp") if times else cutoff
    return {
        "status": "candidate",
        "observed_at": observed_at,
        "provisional": True,
        "reason": reason + " This remains Wave 1 versus Wave A ambiguous.",
        "details": {
            "pivot_count": len(pivots),
            "child_count": len(children),
            "structure_verified": True,
        },
    }


def _retracement_event(context: Mapping[str, Any], cutoff: str) -> dict[str, Any]:
    raw = context.get("retracement") or context.get("retracement_structure")
    if not isinstance(raw, Mapping):
        return {
            "status": "unavailable",
            "observed_at": None,
            "provisional": True,
            "reason": "No post-five-wave retracement structure was supplied.",
        }
    observed_at = _event_time(raw, cutoff)
    if not _not_after(observed_at, cutoff):
        return {
            "status": "unavailable",
            "observed_at": None,
            "provisional": True,
            "reason": "The supplied retracement occurs after this cutoff.",
        }
    family = structure_family(raw.get("pattern_family") or raw.get("structure"))
    child_count = int(raw.get("child_count") or len(raw.get("children") or []))
    actual_structure = child_count >= 3 or len(raw.get("pivots") or []) >= 4
    corrective = raw.get("is_corrective") is True or family in {
        "zigzag",
        "flat",
        "triangle",
        "combination",
    }
    if corrective and actual_structure:
        return {
            "status": "observed",
            "observed_at": observed_at,
            "provisional": bool(raw.get("provisional", True)),
            "reason": "A supplied corrective family has actual child or pivot structure.",
            "details": deepcopy(dict(raw)),
        }
    return {
        "status": "candidate",
        "observed_at": observed_at,
        "provisional": True,
        "reason": "Retracement exists, but corrective child structure is not yet verified.",
        "details": deepcopy(dict(raw)),
    }


def _subsequent_motive_event(context: Mapping[str, Any], cutoff: str) -> dict[str, Any]:
    raw = context.get("subsequent_motive_candidate") or context.get("next_motive_leg")
    if not isinstance(raw, Mapping):
        return {
            "status": "unavailable",
            "observed_at": None,
            "provisional": True,
            "reason": "No subsequent motive pivots or child structure were supplied.",
            "details": {"structure_verified": False},
        }
    pivots = list(raw.get("pivots") or [])
    children = [item for item in raw.get("children", []) if isinstance(item, Mapping)]
    actual_pivots = len(pivots) >= 4 and all(
        _pivot_price(item) is not None for item in pivots[:4]
    )
    positions = {
        str(item.get("sequence_position") or item.get("wave") or item.get("label"))
        .replace("(", "")
        .replace(")", "")
        for item in children
    }
    actual_children = {"1", "2", "3"}.issubset(positions)
    event_time = _event_time(raw, cutoff)
    times = [_pivot_timestamp(item) for item in pivots if _pivot_timestamp(item)]
    if not _not_after(event_time, cutoff) or any(
        not _not_after(value, cutoff) for value in times
    ):
        return {
            "status": "candidate",
            "observed_at": None,
            "provisional": True,
            "reason": "The subsequent motive candidate is incomplete at this cutoff.",
            "details": {"structure_verified": False},
        }
    if raw.get("hard_rules_valid") is not True or not (
        actual_pivots or actual_children
    ):
        return {
            "status": "candidate",
            "observed_at": event_time,
            "provisional": True,
            "reason": "Motive acceleration is suspected, but actual validated pivots or children are unavailable.",
            "details": {"structure_verified": False},
        }
    return {
        "status": "observed",
        "observed_at": event_time,
        "provisional": True,
        "reason": str(
            raw.get("reason")
            or "A validated subsequent motive candidate developed while the origin held."
        ),
        "details": {
            "pivot_count": len(pivots),
            "child_count": len(children),
            "structure_verified": True,
        },
    }
def _continuation_event(context: Mapping[str, Any], cutoff: str) -> dict[str, Any]:
    raw = context.get("x2_z_continuation") or context.get("continuation_structure")
    if not isinstance(raw, Mapping):
        return {
            "status": "not_observed",
            "observed_at": None,
            "provisional": True,
            "reason": "No X2-Z child structure has been supplied at this cutoff.",
        }
    positions = {
        str(item.get("sequence_position") or item.get("wave") or item.get("label"))
        for item in raw.get("children", [])
        if isinstance(item, Mapping)
    }
    positions.update(str(item) for item in raw.get("positions", []))
    observed_at = _event_time(raw, cutoff)
    if not _not_after(observed_at, cutoff):
        return {
            "status": "not_observed",
            "observed_at": None,
            "provisional": True,
            "reason": "The proposed X2-Z continuation occurs after this cutoff.",
        }
    if "X2" not in positions:
        return {
            "status": "candidate",
            "observed_at": observed_at,
            "provisional": True,
            "reason": "Continuation is suspected, but a distinct X2 child is absent.",
        }
    return {
        "status": "observed",
        "observed_at": observed_at,
        "provisional": "Z" not in positions,
        "reason": (
            "A distinct X2 and Z structure is present."
            if "Z" in positions
            else "A distinct X2 is present; Z remains in progress or unresolved."
        ),
        "details": {"positions": sorted(positions)},
    }


def detect_post_terminal_events(
    *,
    candidate_endpoint: Mapping[str, Any],
    newly_available_candles: Iterable[Mapping[str, Any]],
    cutoff: Any,
    structural_context: Mapping[str, Any] | None = None,
    explicit_price_invalidation_levels: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect only events observable at ``cutoff``; never infer child waves from indicators."""
    endpoint = _normalize_endpoint(candidate_endpoint)
    cutoff_iso = _iso(cutoff, field="post-terminal cutoff")
    context = dict(structural_context or {})
    candles = [
        row
        for row in _prepare_candles(newly_available_candles, cutoff_iso)
        if _timestamp(row["timestamp"]) > _timestamp(endpoint["timestamp"])
    ]
    reversal_direction = str(
        context.get("expected_reversal_direction")
        or endpoint["expected_reversal_direction"]
    )
    threshold = _number(context.get("displacement_threshold_pct"))
    threshold = threshold if threshold is not None and threshold > 0 else 2.0
    displacement = {
        "status": "unavailable",
        "observed_at": None,
        "provisional": True,
        "reason": "No post-endpoint candles are available.",
    }
    if candles and reversal_direction in {"up", "down"}:
        target = endpoint["price"] * (
            1.0 + threshold / 100.0 if reversal_direction == "up" else 1.0 - threshold / 100.0
        )
        crossing = next(
            (
                row
                for row in candles
                if (row["close"] >= target if reversal_direction == "up" else row["close"] <= target)
            ),
            None,
        )
        displacement = {
            "status": "observed" if crossing else "not_observed",
            "observed_at": crossing["timestamp"] if crossing else None,
            "provisional": True,
            "reason": (
                f"Close displaced at least {threshold:g}% away from the endpoint."
                if crossing
                else f"No close has displaced {threshold:g}% away from the endpoint."
            ),
            "threshold_price": target,
        }

    protected_price = float(endpoint["protected_origin"]["price"])
    return_crossing = None
    if reversal_direction == "up":
        return_crossing = next((row for row in candles if row["low"] < protected_price), None)
    elif reversal_direction == "down":
        return_crossing = next((row for row in candles if row["high"] > protected_price), None)
    origin_return = {
        "status": "observed" if return_crossing else "not_observed" if candles else "unavailable",
        "observed_at": return_crossing["timestamp"] if return_crossing else None,
        "provisional": True,
        "reason": (
            "Price crossed the protected terminal origin."
            if return_crossing
            else "The protected terminal origin remains intact at this cutoff."
            if candles
            else "No post-endpoint candle can test the protected origin."
        ),
        "protected_price": protected_price,
    }
    origin_hold = {
        "status": "observed" if candles and not return_crossing else "not_observed" if return_crossing else "unavailable",
        "observed_at": candles[-1]["timestamp"] if candles and not return_crossing else None,
        "provisional": True,
        "reason": (
            "The candidate endpoint remains protected through this cutoff."
            if candles and not return_crossing
            else "Origin hold failed."
            if return_crossing
            else "Origin hold cannot yet be assessed."
        ),
    }
    explicit_events: list[dict[str, Any]] = []
    for hypothesis_id, raw_level in dict(
        explicit_price_invalidation_levels or {}
    ).items():
        level_definition = dict(raw_level) if isinstance(raw_level, Mapping) else {}
        price = _number(
            level_definition.get("price") if level_definition else raw_level
        )
        if price is None:
            explicit_events.append(
                {
                    "hypothesis_id": str(hypothesis_id),
                    "status": "unavailable",
                    "observed_at": None,
                    "price": None,
                    "invalidated_when": None,
                    "reason": "The explicit invalidation level is missing or non-finite.",
                }
            )
            continue
        invalidated_when = str(
            level_definition.get("invalidated_when")
            or level_definition.get("direction")
            or ("below" if reversal_direction == "up" else "above")
        ).lower()
        if invalidated_when not in {"below", "above"}:
            explicit_events.append(
                {
                    "hypothesis_id": str(hypothesis_id),
                    "status": "unavailable",
                    "observed_at": None,
                    "price": price,
                    "invalidated_when": invalidated_when,
                    "reason": "Explicit invalidation direction must be below or above.",
                }
            )
            continue
        crossing = next(
            (
                row
                for row in candles
                if (row["low"] < price if invalidated_when == "below" else row["high"] > price)
            ),
            None,
        )
        explicit_events.append(
            {
                "hypothesis_id": str(hypothesis_id),
                "status": "observed" if crossing else "not_observed" if candles else "unavailable",
                "observed_at": crossing["timestamp"] if crossing else None,
                "price": price,
                "invalidated_when": invalidated_when,
                "reason": (
                    f"Price crossed the explicit {invalidated_when} {price:g} invalidation level."
                    if crossing
                    else "The explicit price invalidation level remains intact at this cutoff."
                    if candles
                    else "No post-endpoint candles are available to test this level."
                ),
            }
        )
    return {
        "calculation_version": CORRECTION_STATE_CALCULATION_VERSION,
        "cutoff": cutoff_iso,
        "post_endpoint_candle_count": len(candles),
        "structural_completion": _context_event(
            context,
            ("structural_completion",),
            cutoff_iso,
            unavailable_reason="No additional structural-completion observation was supplied.",
        ),
        "displacement_away": displacement,
        "channel_break": _context_event(
            context,
            ("channel_break",),
            cutoff_iso,
            unavailable_reason="A relevant channel and break observation were not supplied.",
        ),
        "local_structure_break": _context_event(
            context,
            ("local_structure_break",),
            cutoff_iso,
            unavailable_reason="A local structural pivot and break were not supplied.",
        ),
        "five_wave_move_away": _five_wave_event(context, cutoff_iso),
        "retracement": _retracement_event(context, cutoff_iso),
        "origin_hold": origin_hold,
        "origin_return": origin_return,
        "explicit_price_invalidations": explicit_events,
        "subsequent_motive_candidate": _subsequent_motive_event(
            context, cutoff_iso
        ),
        "x2_z_continuation": _continuation_event(context, cutoff_iso),
    }


def _later_stage(current: str, candidate: str) -> str:
    if current in {"invalidated", "superseded", "structurally_confirmed"}:
        return current
    if candidate in {"invalidated", "superseded", "structurally_confirmed"}:
        return candidate
    return candidate if _PROGRESS_ORDER.get(candidate, -1) > _PROGRESS_ORDER.get(current, -1) else current


def _event_observed(events: Mapping[str, Any], key: str) -> bool:
    value = events.get(key)
    return isinstance(value, Mapping) and str(value.get("status")) == "observed"


def _event_candidate(events: Mapping[str, Any], key: str) -> bool:
    value = events.get(key)
    return isinstance(value, Mapping) and str(value.get("status")) in {"observed", "candidate"}


def _verified_event_candidate(events: Mapping[str, Any], key: str) -> bool:
    value = events.get(key)
    details = value.get("details") if isinstance(value, Mapping) else None
    return (
        _event_candidate(events, key)
        and isinstance(details, Mapping)
        and details.get("structure_verified") is True
    )


def _invalidation_targets(item: Mapping[str, Any]) -> list[str]:
    direct = item.get("hypothesis_id")
    provenance = item.get("provenance") if isinstance(item.get("provenance"), Mapping) else {}
    raw = provenance.get("target_hypotheses") or ([direct] if direct else [])
    return [str(value) for value in raw if str(value) in HYPOTHESIS_IDS]


def _post_terminal_evidence_items(
    events: Mapping[str, Any], *, cutoff: str
) -> list[dict[str, Any]]:
    """Represent deterministic event detectors in the unified evidence contract."""
    evidence_types = {
        "structural_completion": "structural",
        "displacement_away": "structural",
        "channel_break": "channel",
        "local_structure_break": "structural",
        "five_wave_move_away": "structural",
        "retracement": "structural",
        "origin_hold": "structural",
        "origin_return": "explicit_price_invalidation",
        "subsequent_motive_candidate": "structural",
        "x2_z_continuation": "structural",
    }
    result: list[dict[str, Any]] = []
    for event_name, evidence_type in evidence_types.items():
        event = events.get(event_name)
        if not isinstance(event, Mapping):
            continue
        event_status = str(event.get("status") or "unavailable")
        hard_invalidation = event_name == "origin_return" and event_status == "observed"
        if hard_invalidation:
            status = "invalidating"
        elif event_status == "observed":
            status = "supportive"
        elif event_status == "unavailable":
            status = "unavailable"
        else:
            status = "neutral"
        provenance: dict[str, Any] = {
            "detector": event_name,
            "detector_status": event_status,
            "event_cutoff": cutoff,
        }
        if hard_invalidation:
            provenance["target_hypotheses"] = ["new_motive_wave_1"]
        item = make_evidence_item(
            evidence_type=evidence_type,
            feature_name=f"correction_state.{event_name}",
            observed_value={
                "status": event_status,
                "observed_at": event.get("observed_at"),
            },
            status=status,
            reason=str(event.get("reason") or "Post-terminal event detector result."),
            comparison_or_expected_condition=event.get("threshold_price")
            or event.get("protected_price"),
            source_wave_or_pivot=event.get("details"),
            hard_rule=hard_invalidation,
            data_quality_status=(
                "unavailable" if event_status == "unavailable" else "available"
            ),
            provenance=provenance,
            calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        )
        result.append(
            with_evidence_timing(
                item,
                timing_class="available_after_endpoint",
                available_at=event.get("observed_at") or cutoff,
            )
        )
    for event in events.get("explicit_price_invalidations", []):
        if not isinstance(event, Mapping):
            continue
        event_status = str(event.get("status") or "unavailable")
        crossed = event_status == "observed"
        item = make_evidence_item(
            evidence_type="explicit_price_invalidation",
            feature_name="correction_state.explicit_price_invalidation",
            observed_value={
                "status": event_status,
                "price": event.get("price"),
                "invalidated_when": event.get("invalidated_when"),
            },
            status=(
                "invalidating"
                if crossed
                else "unavailable"
                if event_status == "unavailable"
                else "neutral"
            ),
            reason=str(event.get("reason") or "Explicit price level detector result."),
            comparison_or_expected_condition=event.get("price"),
            hard_rule=crossed,
            data_quality_status=(
                "unavailable" if event_status == "unavailable" else "available"
            ),
            provenance={
                "detector": "explicit_price_invalidation",
                "target_hypotheses": [event.get("hypothesis_id")],
                "event_cutoff": cutoff,
            },
            calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        )
        result.append(
            with_evidence_timing(
                item,
                timing_class="available_after_endpoint",
                available_at=event.get("observed_at") or cutoff,
            )
        )
    return result


def advance_correction_snapshot(
    previous_snapshot: Mapping[str, Any],
    *,
    cutoff: Any,
    newly_available_candles: Iterable[Mapping[str, Any]] = (),
    evidence_items: Iterable[Mapping[str, Any]] = (),
    fingerprint_references: Iterable[Mapping[str, Any]] = (),
    structural_context: Mapping[str, Any] | None = None,
    structural_invalidations: Iterable[Mapping[str, Any]] = (),
    added_hypotheses: Iterable[str | Mapping[str, Any]] = (),
    transition_timestamp: Any | None = None,
) -> dict[str, Any]:
    """Create a child snapshot; the supplied parent is never mutated."""
    previous = deepcopy(dict(previous_snapshot))
    if previous.get("content_hash") != snapshot_content_hash(previous):
        raise ValueError("Previous snapshot content_hash does not match its canonical payload.")
    cutoff_iso = _iso(cutoff, field="snapshot cutoff")
    if not _not_after(previous["cutoff"], cutoff_iso):
        raise ValueError("A child snapshot cutoff cannot precede its parent cutoff.")
    timestamp = _iso(transition_timestamp or cutoff_iso, field="transition timestamp")
    timed_new = [
        _timed_evidence(
            item,
            default_timing="available_after_endpoint",
            default_available_at=cutoff_iso,
        )
        for item in evidence_items
    ]
    events = detect_post_terminal_events(
        candidate_endpoint=previous["candidate_endpoint"],
        newly_available_candles=newly_available_candles,
        cutoff=cutoff_iso,
        structural_context=structural_context,
        explicit_price_invalidation_levels=previous.get(
            "origin_and_invalidation_levels", {}
        ).get("explicit_levels", {}),
    )
    event_evidence = _post_terminal_evidence_items(events, cutoff=cutoff_iso)
    available_new = [
        item
        for item in [*timed_new, *event_evidence]
        if _evidence_available(item, cutoff_iso)
    ]
    old_catalog = [
        item
        for item in previous.get("evidence_catalog", [])
        if _evidence_available(item, cutoff_iso)
    ]
    buckets = _evidence_buckets([*old_catalog, *available_new])
    new_refs = _evidence_buckets(available_new)["references"]
    states = {
        str(item["hypothesis_id"]): deepcopy(dict(item))
        for item in previous.get("hypothesis_states", [])
    }
    for raw in added_hypotheses:
        if isinstance(raw, Mapping):
            if raw.get("structurally_valid") is not True:
                continue
            hypothesis_id = str(raw.get("hypothesis_id") or "")
        else:
            hypothesis_id = str(raw)
        if hypothesis_id not in HYPOTHESIS_IDS:
            raise ValueError(f"Unsupported correction hypothesis: {hypothesis_id!r}")
        states.setdefault(hypothesis_id, _state_record(hypothesis_id, "unresolved", created_at=timestamp))

    if _verified_event_candidate(events, "five_wave_move_away"):
        for hypothesis_id in ("new_motive_wave_1", "corrective_wave_a"):
            states.setdefault(
                hypothesis_id,
                _state_record(
                    hypothesis_id,
                    "five_wave_move_away_candidate",
                    created_at=timestamp,
                ),
            )
    if _event_observed(events, "x2_z_continuation"):
        states.setdefault(
            "triple_three_continuation",
            _state_record(
                "triple_three_continuation",
                "structurally_confirmed",
                created_at=timestamp,
            ),
        )

    accepted_invalidations: list[dict[str, Any]] = []
    ignored_invalidations: list[dict[str, Any]] = []
    for raw in structural_invalidations:
        item = deepcopy(dict(raw))
        basis = str(item.get("basis") or "").lower()
        hypothesis_id = str(item.get("hypothesis_id") or "")
        if (
            hypothesis_id in states
            and basis
            in {
                "hard_rule",
                "hard_structural_rule",
                "structural_rule",
                "explicit_price_invalidation",
                "explicit_invalidation_level",
                "later_structure_impossible",
            }
            and str(item.get("reason") or "").strip()
        ):
            accepted_invalidations.append(item)
        else:
            ignored_invalidations.append(item)
    for item in available_new:
        if item.get("status") != "invalidating":
            continue
        for hypothesis_id in _invalidation_targets(item):
            accepted_invalidations.append(
                {
                    "hypothesis_id": hypothesis_id,
                    "basis": item.get("evidence_type"),
                    "reason": item.get("reason"),
                    "evidence_reference": item.get("evidence_reference"),
                    "hard_structural_rule": item.get("feature_name"),
                    "price_invalidation_level": item.get(
                        "comparison_or_expected_condition"
                    ),
                }
            )
    if _event_observed(events, "x2_z_continuation") and "correction_complete" in states:
        accepted_invalidations.append(
            {
                "hypothesis_id": "correction_complete",
                "basis": "later_structure_impossible",
                "reason": "A distinct X2 continuation means the prior local Y was not the entire correction.",
                "hard_structural_rule": "W-X-Y-X2-Z sequence",
            }
        )

    invalidation_by_id = {
        str(item["hypothesis_id"]): item for item in accepted_invalidations
    }
    supportive_indicator = bool(new_refs["supporting"])
    transitions: list[dict[str, Any]] = []
    prior_ids = {
        str(item.get("hypothesis_id"))
        for item in previous.get("hypothesis_states", [])
    }
    for hypothesis_id, state in states.items():
        old_state = str(state.get("state") or "unresolved")
        candidate_state = old_state
        reason = "Evidence was updated without structurally resolving this hypothesis."
        structural_rule: str | None = None
        invalidation = invalidation_by_id.get(hypothesis_id)
        if invalidation is not None:
            candidate_state = "invalidated"
            reason = str(invalidation["reason"])
            structural_rule = str(
                invalidation.get("hard_structural_rule") or invalidation.get("basis")
            )
            state.setdefault("structural_invalidations", []).append(invalidation)
        elif _event_observed(events, "x2_z_continuation") and hypothesis_id in {
            "triple_three_continuation",
            "correction_still_in_progress",
        }:
            candidate_state = "structurally_confirmed"
            reason = events["x2_z_continuation"]["reason"]
            structural_rule = "Distinct X2 child structure is present."
        else:
            candidate_state = old_state
            if (
                _event_observed(events, "structural_completion")
                and hypothesis_id
                not in {
                    "triple_three_continuation",
                    "correction_still_in_progress",
                    "unresolved",
                    "new_motive_wave_1",
                    "corrective_wave_a",
                }
            ):
                candidate_state = _later_stage(
                    candidate_state, "structurally_complete"
                )
                reason = events["structural_completion"]["reason"]
            if supportive_indicator and old_state in {
                "structural_terminal_candidate",
                "structurally_complete",
            }:
                candidate_state = _later_stage(
                    candidate_state, "terminal_evidence_supportive"
                )
                reason = "Non-structural evidence supports the endpoint but does not confirm it."
            if (
                _event_observed(events, "displacement_away")
                or _event_observed(events, "channel_break")
                or _event_observed(events, "local_structure_break")
            ) and hypothesis_id in {
                "correction_complete",
                "larger_wave_a_complete",
                "larger_wave_w_complete",
                "local_wave_c_complete",
                "local_wave_y_complete",
            }:
                candidate_state = _later_stage(
                    candidate_state, "reversal_displacement_observed"
                )
                reason = "Post-terminal price displacement or structural break was observed."
            if _verified_event_candidate(events, "five_wave_move_away") and hypothesis_id in {
                "new_motive_wave_1",
                "corrective_wave_a",
                "correction_complete",
            }:
                candidate_state = _later_stage(
                    candidate_state, "five_wave_move_away_candidate"
                )
                reason = events["five_wave_move_away"]["reason"]
            if (
                _event_observed(events, "retracement")
                and _PROGRESS_ORDER.get(candidate_state, -1)
                >= _PROGRESS_ORDER["five_wave_move_away_candidate"]
                and hypothesis_id
                in {
                    "new_motive_wave_1",
                    "corrective_wave_a",
                    "correction_complete",
                }
            ):
                candidate_state = _later_stage(
                    candidate_state, "corrective_retracement_candidate"
                )
                reason = events["retracement"]["reason"]
            if (
                _event_observed(events, "origin_hold")
                and _PROGRESS_ORDER.get(candidate_state, -1)
                >= _PROGRESS_ORDER["corrective_retracement_candidate"]
                and hypothesis_id
                in {
                    "new_motive_wave_1",
                    "corrective_wave_a",
                    "correction_complete",
                }
            ):
                candidate_state = _later_stage(candidate_state, "origin_hold_observed")
                reason = events["origin_hold"]["reason"]
            if (
                _event_observed(events, "subsequent_motive_candidate")
                and _PROGRESS_ORDER.get(candidate_state, -1)
                >= _PROGRESS_ORDER["origin_hold_observed"]
                and hypothesis_id == "new_motive_wave_1"
            ):
                candidate_state = _later_stage(
                    candidate_state, "next_motive_leg_candidate"
                )
                reason = "A subsequent motive candidate supports, but does not finally prove, the Wave 1 interpretation."

        new_state = _later_stage(old_state, candidate_state)
        if hypothesis_id not in prior_ids and new_state == "unresolved" and _verified_event_candidate(events, "five_wave_move_away"):
            new_state = "five_wave_move_away_candidate"
        state["state"] = new_state
        state["status"] = "active" if new_state not in {"invalidated", "superseded"} else new_state
        state["provisional"] = new_state != "structurally_confirmed"
        if new_state != old_state or hypothesis_id not in prior_ids:
            state["last_changed_at"] = timestamp
        material = (
            new_state != old_state
            or hypothesis_id not in prior_ids
            or any(new_refs.values())
        )
        if material:
            transitions.append(
                _transition(
                    case_id=previous["case_id"],
                    snapshot_id="pending",
                    hypothesis_id=hypothesis_id,
                    previous_state=old_state,
                    new_state=new_state,
                    triggering=[
                        *new_refs["supporting"],
                        *new_refs["invalidating"],
                    ],
                    contradictory=new_refs["contradictory"],
                    unavailable=new_refs["unavailable"],
                    structural_rule=structural_rule,
                    price_invalidation_level=(invalidation or {}).get("price_invalidation_level"),
                    cutoff=cutoff_iso,
                    source_run_id=previous.get("source_run_id"),
                    timestamp=timestamp,
                    provisional=new_state != "structurally_confirmed",
                    reason=reason,
                )
            )

    ordered_states = sorted(states.values(), key=lambda item: item["hypothesis_id"])
    old_fingerprints = previous.get("fingerprint_references", [])
    fingerprints = _fingerprint_references(
        [*old_fingerprints, *fingerprint_references], cutoff_iso
    )
    timeline = deepcopy(previous.get("timeline", {}))
    if timeline.get("first_structural_completion_time") is None and _event_observed(
        events, "structural_completion"
    ):
        timeline["first_structural_completion_time"] = events[
            "structural_completion"
        ]["observed_at"]
    if timeline.get("first_displacement_confirmation_time") is None and _event_observed(events, "displacement_away"):
        timeline["first_displacement_confirmation_time"] = events["displacement_away"]["observed_at"]
    if timeline.get("first_five_away_candidate_time") is None and _verified_event_candidate(events, "five_wave_move_away"):
        timeline["first_five_away_candidate_time"] = events["five_wave_move_away"].get("observed_at") or cutoff_iso
    if timeline.get("retracement_confirmation_time") is None and _event_observed(events, "retracement"):
        timeline["retracement_confirmation_time"] = events["retracement"]["observed_at"]
    snapshot: dict[str, Any] = {
        "schema_version": CORRECTION_STATE_SCHEMA_VERSION,
        "calculation_version": CORRECTION_STATE_CALCULATION_VERSION,
        "case_id": previous["case_id"],
        "parent_snapshot_id": previous["snapshot_id"],
        "source_run_id": previous.get("source_run_id"),
        "source_resolution_id": previous.get("source_resolution_id"),
        "source_identity": deepcopy(previous["source_identity"]),
        "symbol": previous["symbol"],
        "timeframe": previous["timeframe"],
        "cutoff": cutoff_iso,
        "candidate_endpoint": deepcopy(previous["candidate_endpoint"]),
        "elliott_degree": previous["elliott_degree"],
        "parent_pattern_family": previous["parent_pattern_family"],
        "current_state": _aggregate_state(ordered_states),
        "provisional": not any(item["state"] == "structurally_confirmed" for item in ordered_states),
        "reviewed": False,
        "active_hypotheses": [
            item["hypothesis_id"] for item in ordered_states if item["status"] == "active"
        ],
        "invalidated_hypotheses": [
            item["hypothesis_id"] for item in ordered_states if item["status"] == "invalidated"
        ],
        "hypothesis_states": ordered_states,
        "structural_invalidations": [
            *deepcopy(previous.get("structural_invalidations", [])),
            *accepted_invalidations,
        ],
        "ignored_nonstructural_invalidations": ignored_invalidations,
        "evidence_catalog": buckets["catalog"],
        "evidence_references": buckets["references"],
        "fingerprint_references": fingerprints,
        "post_terminal_events": events,
        "origin_and_invalidation_levels": deepcopy(
            previous.get("origin_and_invalidation_levels", {})
        ),
        "timeline": timeline,
        "transitions": transitions,
        "created_at": timestamp,
    }
    return _finalize_snapshot(snapshot)


def validate_snapshot(snapshot: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    required = {
        "snapshot_id",
        "case_id",
        "parent_snapshot_id",
        "source_identity",
        "cutoff",
        "candidate_endpoint",
        "hypothesis_states",
        "evidence_catalog",
        "fingerprint_references",
        "schema_version",
        "calculation_version",
        "content_hash",
    }
    errors.extend(f"Missing snapshot field: {field}." for field in sorted(required - set(snapshot)))
    if errors:
        return errors
    if snapshot.get("content_hash") != snapshot_content_hash(snapshot):
        errors.append("Snapshot content_hash does not match its canonical payload.")
    if not _not_after(snapshot["candidate_endpoint"]["timestamp"], snapshot["cutoff"]):
        errors.append("Snapshot cutoff precedes its candidate endpoint.")
    for item in snapshot.get("evidence_catalog", []):
        if not _evidence_available(item, snapshot["cutoff"], reviewed_snapshot=bool(snapshot.get("reviewed"))):
            errors.append("Snapshot contains evidence unavailable at its declared cutoff or review stage.")
    for state in snapshot.get("hypothesis_states", []):
        if state.get("hypothesis_id") not in HYPOTHESIS_IDS:
            errors.append(f"Unsupported hypothesis ID: {state.get('hypothesis_id')!r}.")
        if state.get("state") not in STATE_STAGE_SET:
            errors.append(f"Unsupported hypothesis state: {state.get('state')!r}.")
    return errors


def build_reviewed_outcome(
    selected_snapshot: Mapping[str, Any],
    *,
    resolved_hypothesis: str,
    final_reviewed_interpretation: str,
    resolution_cutoff: Any,
    reviewer: str,
    review_status: str = "reviewed",
    resolution_confidence: str = "not_stated",
    supporting_future_structure: Iterable[Any] = (),
    rejected_alternatives: Iterable[Mapping[str, Any]] = (),
    notes: str = "",
) -> dict[str, Any]:
    """Build a human outcome record separate from current degree resolution."""
    snapshot = deepcopy(dict(selected_snapshot))
    errors = validate_snapshot(snapshot)
    if errors:
        raise ValueError("Invalid selected snapshot: " + " ".join(errors))
    selected_state = next(
        (
            item
            for item in snapshot["hypothesis_states"]
            if str(item.get("hypothesis_id")) == resolved_hypothesis
        ),
        None,
    )
    if selected_state is None:
        raise ValueError("resolved_hypothesis was not present at the selected snapshot.")
    if selected_state.get("status") == "invalidated":
        raise ValueError("A structurally invalidated hypothesis cannot be selected as the outcome.")
    if not str(reviewer).strip() or not str(final_reviewed_interpretation).strip():
        raise ValueError("reviewer and final_reviewed_interpretation are required.")
    resolution_time = _iso(resolution_cutoff, field="resolution cutoff")
    if not _not_after(snapshot["cutoff"], resolution_time):
        raise ValueError("Resolution cutoff cannot precede the selected snapshot cutoff.")
    rejected: list[dict[str, Any]] = []
    for raw in rejected_alternatives:
        hypothesis_id = str(raw.get("hypothesis_id") or "")
        reason = str(raw.get("reason") or "").strip()
        if hypothesis_id and reason:
            rejected.append({"hypothesis_id": hypothesis_id, "reason": reason})
    outcome: dict[str, Any] = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "case_id": snapshot["case_id"],
        "selected_snapshot_id": snapshot["snapshot_id"],
        "final_reviewed_interpretation": str(final_reviewed_interpretation),
        "resolved_hypothesis": resolved_hypothesis,
        "resolution_cutoff": resolution_time,
        "reviewer": str(reviewer),
        "review_status": str(review_status),
        "resolution_confidence": {
            "value": str(resolution_confidence),
            "kind": "human_review_field_not_model_probability",
        },
        "supporting_future_structure": list(deepcopy(list(supporting_future_structure))),
        "rejected_alternatives": rejected,
        "notes": str(notes),
        "calculation_version": CORRECTION_STATE_CALCULATION_VERSION,
    }
    outcome["content_hash"] = outcome_content_hash(outcome)
    return outcome


def create_review_snapshot(
    previous_snapshot: Mapping[str, Any],
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    """Append a reviewed child snapshot without rewriting decision-time evidence."""
    previous = deepcopy(dict(previous_snapshot))
    errors = validate_snapshot(previous)
    if errors:
        raise ValueError("Invalid previous snapshot: " + " ".join(errors))
    if str(outcome.get("case_id")) != previous["case_id"]:
        raise ValueError("Outcome and snapshot must belong to the same correction case.")
    selected = str(outcome.get("resolved_hypothesis") or "")
    resolution_time = _iso(outcome.get("resolution_cutoff"), field="resolution cutoff")
    review_evidence = make_evidence_item(
        evidence_type="structural",
        feature_name="human_reviewed_future_outcome",
        observed_value=selected,
        status="neutral",
        reason="A human reviewer resolved the later historical outcome.",
        source_wave_or_pivot=previous["candidate_endpoint"],
        provenance={"reviewer": outcome.get("reviewer")},
    )
    timed_review = with_evidence_timing(
        review_evidence,
        timing_class="human_review_only",
        available_at=resolution_time,
    )
    buckets = _evidence_buckets([*previous.get("evidence_catalog", []), timed_review])
    states = deepcopy(previous["hypothesis_states"])
    transitions: list[dict[str, Any]] = []
    rejected_ids = {
        str(item.get("hypothesis_id"))
        for item in outcome.get("rejected_alternatives", [])
    }
    for state in states:
        hypothesis_id = str(state["hypothesis_id"])
        old_state = str(state["state"])
        if hypothesis_id == selected:
            new_state = "structurally_confirmed"
            reason = "Selected by explicit human review of future structure."
        elif hypothesis_id in rejected_ids:
            new_state = "superseded"
            reason = next(
                str(item.get("reason"))
                for item in outcome.get("rejected_alternatives", [])
                if str(item.get("hypothesis_id")) == hypothesis_id
            )
        else:
            new_state = old_state
            reason = "The reviewer did not resolve this alternative in the supplied record."
        state["state"] = new_state
        state["status"] = "active" if new_state not in {"invalidated", "superseded"} else new_state
        state["provisional"] = new_state != "structurally_confirmed"
        if new_state != old_state:
            state["last_changed_at"] = resolution_time
            transitions.append(
                _transition(
                    case_id=previous["case_id"],
                    snapshot_id="pending",
                    hypothesis_id=hypothesis_id,
                    previous_state=old_state,
                    new_state=new_state,
                    triggering=[timed_review["evidence_reference"]],
                    contradictory=[],
                    unavailable=[],
                    structural_rule=None,
                    price_invalidation_level=None,
                    cutoff=resolution_time,
                    source_run_id=previous.get("source_run_id"),
                    timestamp=resolution_time,
                    provisional=False,
                    reason=reason,
                )
            )
    timeline = deepcopy(previous["timeline"])
    timeline["final_reviewed_resolution_time"] = resolution_time
    reviewed: dict[str, Any] = {
        **{
            key: deepcopy(previous[key])
            for key in (
                "source_run_id",
                "source_resolution_id",
                "source_identity",
                "symbol",
                "timeframe",
                "candidate_endpoint",
                "elliott_degree",
                "parent_pattern_family",
                "origin_and_invalidation_levels",
            )
        },
        "schema_version": CORRECTION_STATE_SCHEMA_VERSION,
        "calculation_version": CORRECTION_STATE_CALCULATION_VERSION,
        "case_id": previous["case_id"],
        "parent_snapshot_id": previous["snapshot_id"],
        "cutoff": resolution_time,
        "current_state": "structurally_confirmed",
        "provisional": False,
        "reviewed": True,
        "active_hypotheses": [
            item["hypothesis_id"] for item in states if item["status"] == "active"
        ],
        "invalidated_hypotheses": [
            item["hypothesis_id"] for item in states if item["status"] == "invalidated"
        ],
        "hypothesis_states": states,
        "structural_invalidations": deepcopy(previous.get("structural_invalidations", [])),
        "evidence_catalog": buckets["catalog"],
        "evidence_references": buckets["references"],
        "fingerprint_references": deepcopy(previous.get("fingerprint_references", [])),
        "post_terminal_events": deepcopy(previous.get("post_terminal_events", {})),
        "timeline": timeline,
        "reviewed_outcome": {
            "selected_snapshot_id": outcome.get("selected_snapshot_id"),
            "resolved_hypothesis": selected,
            "reviewer": outcome.get("reviewer"),
            "review_status": outcome.get("review_status"),
        },
        "transitions": transitions,
        "created_at": resolution_time,
    }
    return _finalize_snapshot(reviewed)


def finalize_outcome_revision(outcome: Mapping[str, Any], revision: int) -> dict[str, Any]:
    result = deepcopy(dict(outcome))
    if revision < 1:
        raise ValueError("Outcome revision must be positive.")
    result["revision"] = int(revision)
    result.pop("outcome_id", None)
    result.pop("content_hash", None)
    result["content_hash"] = outcome_content_hash(result)
    result["outcome_id"] = f"outcome_{result['content_hash'][:24]}"
    result["content_hash"] = outcome_content_hash(result)
    return result


def build_outcome_review(
    outcome: Mapping[str, Any],
    *,
    action: str,
    reviewer: str,
    review_status: str,
    reason: str = "",
    reviewed_at: Any | None = None,
) -> dict[str, Any]:
    if action not in {"approved", "rejected", "revised"}:
        raise ValueError("Outcome review action must be approved, rejected, or revised.")
    if not str(reviewer).strip():
        raise ValueError("Outcome reviewer is required.")
    timestamp = _iso(
        reviewed_at or outcome.get("resolution_cutoff"), field="outcome review timestamp"
    )
    review: dict[str, Any] = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "outcome_id": outcome.get("outcome_id"),
        "case_id": outcome.get("case_id"),
        "action": action,
        "reviewer": str(reviewer),
        "review_status": str(review_status),
        "reason": str(reason),
        "reviewed_at": timestamp,
    }
    review["content_hash"] = review_content_hash(review)
    review["review_id"] = f"outcome_review_{review['content_hash'][:24]}"
    review["content_hash"] = review_content_hash(review)
    return review
