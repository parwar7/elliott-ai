"""Versioned, review-gated Elliott experience cases and Pattern DNA.

Phase 5A.1 is deliberately a storage and projection layer.  It reuses an
immutable Phase 3 fingerprint for endpoint facts and Phase 4 snapshot lineage
for later confirmation facts.  It does not calculate indicators, perform
similarity retrieval, assign predictive weights, or resolve an outcome.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .correction_semantics import combination_variant, structure_family
from .correction_state import (
    CORRECTION_STATE_CALCULATION_VERSION,
    CORRECTION_STATE_SCHEMA_VERSION,
    OUTCOME_SCHEMA_VERSION,
    correction_case_content_hash,
    outcome_content_hash,
    review_content_hash as outcome_review_content_hash,
    snapshot_content_hash,
    validate_snapshot,
)
from .fingerprints import (
    CALCULATION_VERSION as FINGERPRINT_CALCULATION_VERSION,
    FEATURE_SCHEMA_VERSION,
    fingerprint_content_hash,
)
from .market_data import parse_date


EXPERIENCE_SCHEMA_VERSION = "elliott-experience-1.0.0"
EXPERIENCE_CALCULATION_VERSION = "elliott-experience-calc-1.0.0"
PATTERN_DNA_SCHEMA_VERSION = "pattern-dna-1.0.0"
PATTERN_DNA_CALCULATION_VERSION = "pattern-dna-projection-1.0.0"
MARKET_EPISODE_IDENTITY_VERSION = "market-episode-identity-1.0.0"
EXPERIENCE_CASE_IDENTITY_VERSION = "experience-case-identity-1.0.0"
EXPERIENCE_TAG_SCHEMA_VERSION = "experience-tag-1.0.0"

EXPERIENCE_STATES = frozenset(
    {
        "assembled_candidate",
        "pending_experience_review",
        "accepted",
        "rejected",
        "quarantined",
        "superseded",
    }
)
QUALITY_STATUSES = frozenset({"high", "medium", "low", "quarantined"})
EXPERIENCE_REVIEW_ACTIONS = frozenset(
    {
        "accept",
        "reject",
        "quarantine",
        "revise",
        "assign_quality",
        "add_rationale",
    }
)
DIRECT_REVIEW_ACTIONS = frozenset({"accept", "reject", "quarantine"})
TAG_ACTIONS = frozenset({"add", "remove"})
DNA_KINDS = ("endpoint", "confirmation", "resolved_outcome")

SUPPORTED_SOURCE_VERSIONS = {
    "fingerprint_schema": frozenset({FEATURE_SCHEMA_VERSION}),
    "fingerprint_calculation": frozenset({FINGERPRINT_CALCULATION_VERSION}),
    "snapshot_schema": frozenset({CORRECTION_STATE_SCHEMA_VERSION}),
    "snapshot_calculation": frozenset({CORRECTION_STATE_CALCULATION_VERSION}),
    "outcome_schema": frozenset({OUTCOME_SCHEMA_VERSION}),
}

DNA_GROUP_NAMES = (
    "source_identity",
    "structural_context",
    "price_shape",
    "duration",
    "pivot_path",
    "fibonacci_relationships",
    "channel_behaviour",
    "momentum",
    "volume",
    "volatility",
    "optional_rsi",
    "market_context",
    "availability_and_comparability",
    "evidence_references",
)

CONFIRMATION_EVENT_SOURCES = {
    "displacement": "displacement_away",
    "channel_break": "channel_break",
    "local_structure_break": "local_structure_break",
    "five_away_candidate": "five_wave_move_away",
    "corrective_retracement_candidate": "retracement",
    "origin_hold": "origin_hold",
    "origin_invalidation": "origin_return",
    "next_motive_candidate": "subsequent_motive_candidate",
    "x2_z_continuation": "x2_z_continuation",
}

# This map is documentation as data.  It prevents a later implementation from
# quietly creating a second indicator calculation path inside Pattern DNA.
DNA_FIELD_SOURCE_MAP: dict[str, Any] = {
    "source_identity": ["fingerprint.source", "fingerprint.identity"],
    "structural_context": ["fingerprint.identity", "fingerprint.structural"],
    "price_shape": ["fingerprint.price", "fingerprint.price.acceleration"],
    "duration": [
        "fingerprint.structural.duration_candles",
        "fingerprint.structural.duration_clock_seconds",
        "fingerprint.structural.duration_clock_days",
    ],
    "pivot_path": [
        "fingerprint.price.start_price",
        "fingerprint.price.end_price",
        "fingerprint.price.start_pivot_alignment",
        "fingerprint.price.end_pivot_alignment",
        "fingerprint.structural.pivot_count",
    ],
    "fibonacci_relationships": [
        "fingerprint.structural.retracement_ratio",
        "fingerprint.structural.extension_ratio",
        "fingerprint.structural.overlap_ratio",
    ],
    "channel_behaviour": ["fingerprint.price.channel"],
    "momentum": ["fingerprint.indicators.ewo", "fingerprint.indicators.macd"],
    "volume": ["fingerprint.indicators.volume"],
    "volatility": ["fingerprint.indicators.volatility"],
    "optional_rsi": ["fingerprint.indicators.rsi"],
    "market_context": ["fingerprint.source"],
    "availability_and_comparability": [
        "fingerprint.data_quality",
        "fingerprint.identity.active_feature_flags",
        "fingerprint.indicators.*.availability",
    ],
    "evidence_references": ["fingerprint.evidence_items"],
    "confirmation_events": [
        "phase4.snapshot_lineage[*].post_terminal_events",
        "phase4.snapshot_lineage[*].transitions",
    ],
    "resolved_outcome": ["phase4.reviewed_outcome", "phase4.outcome_review"],
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


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iso(value: Any, *, field: str) -> str:
    parsed = parse_date(value)
    if parsed is None:
        raise ValueError(f"{field} must be a valid timestamp.")
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


def _timestamp_distance_seconds(left: Any, right: Any) -> float | None:
    first = parse_date(left)
    second = parse_date(right)
    if first is None or second is None:
        return None
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    if second.tzinfo is None:
        second = second.replace(tzinfo=timezone.utc)
    return abs((first.astimezone(timezone.utc) - second.astimezone(timezone.utc)).total_seconds())


def _not_after(left: Any, right: Any) -> bool:
    distance = _timestamp_distance_seconds(left, right)
    if distance is None:
        return False
    first = parse_date(left)
    second = parse_date(right)
    assert first is not None and second is not None
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    if second.tzinfo is None:
        second = second.replace(tzinfo=timezone.utc)
    return first.astimezone(timezone.utc) <= second.astimezone(timezone.utc)


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_timeframe(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _normalize_degree(value: Any) -> str:
    return _normalize_text(value).replace("degree", "").strip()


def _normalize_role(value: Any) -> str:
    return str(value or "").strip().replace("(", "").replace(")", "").upper()


def _normalize_family(value: Any) -> str:
    family = structure_family(value)
    if family != "combination":
        return family
    return "triple-three" if combination_variant(value) == "triple" else "double-three"


def _source(value: Mapping[str, Any] | None) -> dict[str, str]:
    raw = dict(value or {})
    return {
        "provider": _normalize_text(raw.get("provider")),
        "venue": _normalize_text(raw.get("venue") or raw.get("exchange")),
        "symbol": _normalize_symbol(raw.get("symbol") or raw.get("resolved_symbol")),
        "market_type": _normalize_text(raw.get("market_type")),
        "timeframe": _normalize_timeframe(raw.get("timeframe")),
        "feed_identity": str(raw.get("feed_identity") or "").strip(),
    }


def market_episode_identity(case: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable identity tuple for one real market episode.

    The reviewed outcome, snapshot ID, fingerprint version, experience review,
    and price are intentionally absent.  Revisions of those records therefore
    remain versions of one observation rather than synthetic new observations.
    """
    endpoint = case.get("candidate_endpoint")
    if not isinstance(endpoint, Mapping):
        raise ValueError("Correction case has no candidate_endpoint mapping.")
    source = _source(case.get("source_identity") if isinstance(case.get("source_identity"), Mapping) else None)
    payload = {
        "identity_version": MARKET_EPISODE_IDENTITY_VERSION,
        "provider": source["provider"],
        "venue": source["venue"],
        "feed_identity": source["feed_identity"],
        "symbol": _normalize_symbol(case.get("symbol") or source["symbol"]),
        "timeframe": _normalize_timeframe(case.get("timeframe") or source["timeframe"]),
        "elliott_degree": _normalize_degree(case.get("elliott_degree")),
        "endpoint_timestamp": _iso(endpoint.get("timestamp") or endpoint.get("date"), field="endpoint timestamp"),
        "candidate_role": _normalize_role(endpoint.get("terminal_label")),
        "parent_pattern_family": _normalize_family(case.get("parent_pattern_family")),
    }
    digest = _hash(payload)
    return {
        "market_episode_id": f"market_episode_{digest[:32]}",
        "identity_hash": digest,
        "identity_fields": payload,
        "formula": (
            "SHA256(canonical_json(identity_version, provider, venue, feed_identity, "
            "symbol, timeframe, elliott_degree, endpoint_timestamp, candidate_role, "
            "parent_pattern_family))"
        ),
    }


def experience_case_identity(
    *,
    market_episode_id: str,
    endpoint_snapshot: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
    outcome: Mapping[str, Any],
    outcome_review: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a deterministic version identity for an experience case."""
    payload = {
        "identity_version": EXPERIENCE_CASE_IDENTITY_VERSION,
        "market_episode_id": market_episode_id,
        "endpoint_snapshot_id": endpoint_snapshot.get("snapshot_id"),
        "endpoint_snapshot_hash": endpoint_snapshot.get("content_hash"),
        "fingerprint_identity_hash": fingerprint.get("identity_hash"),
        "fingerprint_content_hash": fingerprint.get("content_hash"),
        "outcome_id": outcome.get("outcome_id"),
        "outcome_content_hash": outcome.get("content_hash"),
        "outcome_revision": outcome.get("revision"),
        "outcome_review_id": outcome_review.get("review_id"),
        "outcome_review_hash": outcome_review.get("content_hash"),
        "experience_schema_version": EXPERIENCE_SCHEMA_VERSION,
        "pattern_dna_schema_version": PATTERN_DNA_SCHEMA_VERSION,
        "pattern_dna_calculation_version": PATTERN_DNA_CALCULATION_VERSION,
    }
    digest = _hash(payload)
    return {
        "experience_case_id": f"experience_case_{digest[:32]}",
        "identity_hash": digest,
        "identity_fields": payload,
        "formula": (
            "SHA256(canonical_json(identity_version, market_episode_id, endpoint snapshot "
            "ID/hash, fingerprint identity/content hashes, reviewed outcome ID/hash/revision, "
            "outcome-review ID/hash, experience and DNA versions))"
        ),
    }


def experience_case_content_hash(case: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(case))
    payload.pop("content_hash", None)
    return _hash(payload)


def pattern_dna_content_hash(dna: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(dna))
    payload.pop("content_hash", None)
    payload.pop("dna_id", None)
    return _hash(payload)


def experience_review_content_hash(review: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(review))
    payload.pop("content_hash", None)
    payload.pop("review_id", None)
    return _hash(payload)


def tag_content_hash(tag: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(tag))
    payload.pop("content_hash", None)
    payload.pop("tag_id", None)
    return _hash(payload)


def tag_assignment_content_hash(assignment: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(assignment))
    payload.pop("content_hash", None)
    payload.pop("assignment_id", None)
    return _hash(payload)


def _reason(code: str, message: str, disposition: str) -> dict[str, str]:
    return {"code": code, "message": message, "disposition": disposition}


def _hash_validations(
    case: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
    outcome: Mapping[str, Any],
    review: Mapping[str, Any],
) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    checks = (
        (
            "correction_case_hash_mismatch",
            case.get("content_hash"),
            correction_case_content_hash(case),
            "Correction-case source hash does not match its immutable payload.",
        ),
        (
            "endpoint_snapshot_hash_mismatch",
            snapshot.get("content_hash"),
            snapshot_content_hash(snapshot),
            "Endpoint-snapshot source hash does not match its immutable payload.",
        ),
        (
            "fingerprint_hash_mismatch",
            fingerprint.get("content_hash"),
            fingerprint_content_hash(fingerprint),
            "Fingerprint source hash does not match its immutable payload.",
        ),
        (
            "outcome_hash_mismatch",
            outcome.get("content_hash"),
            outcome_content_hash(outcome),
            "Reviewed-outcome source hash does not match its immutable payload.",
        ),
        (
            "outcome_review_hash_mismatch",
            review.get("content_hash"),
            outcome_review_content_hash(review),
            "Outcome-review source hash does not match its immutable payload.",
        ),
    )
    for code, supplied, expected, message in checks:
        if not supplied or supplied != expected:
            reasons.append(_reason(code, message, "rejected"))
    return reasons


def _match(
    reasons: list[dict[str, str]],
    *,
    code: str,
    values: Sequence[Any],
    normalize: Any,
    label: str,
) -> None:
    normalized = [normalize(value) for value in values]
    if any(not value for value in normalized) or len(set(normalized)) != 1:
        reasons.append(
            _reason(
                code,
                f"{label} mismatch: {normalized!r}.",
                "rejected",
            )
        )


def validate_experience_eligibility(
    *,
    correction_case: Mapping[str, Any],
    endpoint_snapshot: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
    outcome: Mapping[str, Any],
    outcome_review: Mapping[str, Any],
    endpoint_alignment_tolerance_seconds: float = 0.0,
    outcome_is_current: bool = True,
) -> dict[str, Any]:
    """Validate all mandatory Phase 5A.1 source contracts without repairing them."""
    reasons: list[dict[str, str]] = []
    if endpoint_alignment_tolerance_seconds < 0:
        raise ValueError("Endpoint alignment tolerance cannot be negative.")

    reasons.extend(
        _hash_validations(
            correction_case,
            endpoint_snapshot,
            fingerprint,
            outcome,
            outcome_review,
        )
    )
    snapshot_errors = validate_snapshot(endpoint_snapshot)
    if snapshot_errors:
        reasons.append(
            _reason(
                "invalid_endpoint_snapshot",
                " ".join(snapshot_errors),
                "rejected",
            )
        )
    if endpoint_snapshot.get("parent_snapshot_id") is not None:
        reasons.append(
            _reason(
                "endpoint_snapshot_not_original",
                "The endpoint snapshot must be the immutable root snapshot.",
                "rejected",
            )
        )
    if endpoint_snapshot.get("reviewed") is True:
        reasons.append(
            _reason(
                "endpoint_snapshot_is_review_snapshot",
                "A reviewed future snapshot cannot be used as endpoint evidence.",
                "rejected",
            )
        )

    case_id = str(correction_case.get("case_id") or "")
    if not case_id or any(
        str(value or "") != case_id
        for value in (
            endpoint_snapshot.get("case_id"),
            outcome.get("case_id"),
            outcome_review.get("case_id"),
        )
    ):
        reasons.append(
            _reason(
                "correction_case_reference_mismatch",
                "Snapshot, outcome, review, and correction case must share one case_id.",
                "rejected",
            )
        )
    if str(outcome_review.get("outcome_id") or "") != str(outcome.get("outcome_id") or ""):
        reasons.append(
            _reason(
                "outcome_review_reference_mismatch",
                "The outcome review does not reference the supplied outcome.",
                "rejected",
            )
        )
    if not outcome_is_current:
        reasons.append(
            _reason(
                "stale_outcome_review",
                "The supplied reviewed outcome is not the latest non-stale outcome revision.",
                "rejected",
            )
        )
    if str(outcome_review.get("action")) not in {"approved", "revised"} or str(
        outcome_review.get("review_status")
    ) not in {"reviewed", "approved", "resolved"}:
        reasons.append(
            _reason(
                "outcome_not_currently_approved",
                "A current approved or revised Phase 4 outcome review is mandatory.",
                "rejected",
            )
        )

    fingerprint_hash = str(fingerprint.get("content_hash") or "")
    endpoint_references = {
        str(item.get("content_hash") or "")
        for item in endpoint_snapshot.get("fingerprint_references", [])
        if isinstance(item, Mapping)
    }
    if not fingerprint_hash or fingerprint_hash not in endpoint_references:
        reasons.append(
            _reason(
                "fingerprint_not_referenced_at_endpoint",
                "The original Phase 3 fingerprint hash is absent from the endpoint snapshot.",
                "rejected",
            )
        )

    identity = fingerprint.get("identity") if isinstance(fingerprint.get("identity"), Mapping) else {}
    fp_source = fingerprint.get("source") if isinstance(fingerprint.get("source"), Mapping) else {}
    case_source = correction_case.get("source_identity") if isinstance(correction_case.get("source_identity"), Mapping) else {}
    snapshot_source = endpoint_snapshot.get("source_identity") if isinstance(endpoint_snapshot.get("source_identity"), Mapping) else {}
    _match(
        reasons,
        code="symbol_mismatch",
        values=(correction_case.get("symbol"), endpoint_snapshot.get("symbol"), identity.get("symbol"), fp_source.get("symbol")),
        normalize=_normalize_symbol,
        label="Symbol",
    )
    _match(
        reasons,
        code="timeframe_mismatch",
        values=(correction_case.get("timeframe"), endpoint_snapshot.get("timeframe"), identity.get("timeframe"), fp_source.get("timeframe")),
        normalize=_normalize_timeframe,
        label="Timeframe",
    )
    _match(
        reasons,
        code="degree_mismatch",
        values=(correction_case.get("elliott_degree"), endpoint_snapshot.get("elliott_degree"), identity.get("wave_degree")),
        normalize=_normalize_degree,
        label="Elliott degree",
    )
    _match(
        reasons,
        code="candidate_role_mismatch",
        values=(
            (correction_case.get("candidate_endpoint") or {}).get("terminal_label"),
            (endpoint_snapshot.get("candidate_endpoint") or {}).get("terminal_label"),
            identity.get("wave_label"),
        ),
        normalize=_normalize_role,
        label="Candidate role",
    )
    _match(
        reasons,
        code="parent_family_mismatch",
        values=(correction_case.get("parent_pattern_family"), endpoint_snapshot.get("parent_pattern_family"), identity.get("parent_pattern_family")),
        normalize=_normalize_family,
        label="Parent pattern family",
    )
    _match(
        reasons,
        code="provider_mismatch",
        values=(case_source.get("provider"), snapshot_source.get("provider"), fp_source.get("provider")),
        normalize=_normalize_text,
        label="Provider",
    )
    _match(
        reasons,
        code="feed_identity_mismatch",
        values=(case_source.get("feed_identity"), snapshot_source.get("feed_identity"), fp_source.get("feed_identity")),
        normalize=lambda value: str(value or "").strip(),
        label="Feed identity",
    )

    endpoint_timestamp = (correction_case.get("candidate_endpoint") or {}).get("timestamp")
    timestamp_values = (
        endpoint_timestamp,
        (endpoint_snapshot.get("candidate_endpoint") or {}).get("timestamp"),
        identity.get("end_timestamp"),
    )
    endpoint_alignment: list[dict[str, Any]] = []
    for source_name, value in zip(("case", "snapshot", "fingerprint"), timestamp_values):
        distance = _timestamp_distance_seconds(endpoint_timestamp, value)
        endpoint_alignment.append(
            {"source": source_name, "timestamp": value, "distance_seconds": distance}
        )
        if distance is None or distance > endpoint_alignment_tolerance_seconds:
            reasons.append(
                _reason(
                    "endpoint_timestamp_mismatch",
                    f"{source_name} endpoint differs by {distance!r} seconds; explicit tolerance is {endpoint_alignment_tolerance_seconds:g}.",
                    "rejected",
                )
            )

    versions = (
        ("unsupported_fingerprint_schema", fingerprint.get("feature_schema_version"), SUPPORTED_SOURCE_VERSIONS["fingerprint_schema"]),
        ("unsupported_fingerprint_calculation", fingerprint.get("calculation_version"), SUPPORTED_SOURCE_VERSIONS["fingerprint_calculation"]),
        ("unsupported_snapshot_schema", endpoint_snapshot.get("schema_version"), SUPPORTED_SOURCE_VERSIONS["snapshot_schema"]),
        ("unsupported_snapshot_calculation", endpoint_snapshot.get("calculation_version"), SUPPORTED_SOURCE_VERSIONS["snapshot_calculation"]),
        ("unsupported_outcome_schema", outcome.get("schema_version"), SUPPORTED_SOURCE_VERSIONS["outcome_schema"]),
    )
    for code, actual, supported in versions:
        if actual not in supported:
            reasons.append(
                _reason(
                    code,
                    f"Unsupported source version {actual!r}; supported versions are {sorted(supported)!r}.",
                    "quarantined",
                )
            )

    fingerprint_cutoff = fingerprint.get("cutoff") if isinstance(fingerprint.get("cutoff"), Mapping) else {}
    fp_cutoff = fingerprint_cutoff.get("timestamp")
    snapshot_cutoff = endpoint_snapshot.get("cutoff")
    if not fp_cutoff or not snapshot_cutoff or not _not_after(fp_cutoff, snapshot_cutoff):
        reasons.append(
            _reason(
                "cutoff_leakage",
                "Fingerprint cutoff occurs after the immutable endpoint snapshot cutoff.",
                "quarantined",
            )
        )
    if fp_cutoff and endpoint_timestamp and not _not_after(fp_cutoff, endpoint_timestamp):
        reasons.append(
            _reason(
                "cutoff_leakage",
                "Endpoint fingerprint includes information later than the candidate endpoint.",
                "quarantined",
            )
        )
    last_candle = fingerprint_cutoff.get("last_source_candle_timestamp")
    if last_candle and fp_cutoff and not _not_after(last_candle, fp_cutoff):
        reasons.append(
            _reason(
                "source_candle_cutoff_leakage",
                "Fingerprint source candle occurs after its declared cutoff.",
                "quarantined",
            )
        )
    endpoint_catalog = endpoint_snapshot.get("evidence_catalog", [])
    for item in endpoint_catalog:
        if not isinstance(item, Mapping):
            continue
        available_at = item.get("available_at")
        timing = str(item.get("timing_class") or "")
        if timing not in {"available_at_endpoint", ""} or (
            available_at and endpoint_timestamp and not _not_after(available_at, endpoint_timestamp)
        ):
            reasons.append(
                _reason(
                    "endpoint_evidence_cutoff_leakage",
                    "Endpoint snapshot contains evidence that was not available at the endpoint.",
                    "quarantined",
                )
            )
            break

    disposition = "eligible"
    if any(item["disposition"] == "rejected" for item in reasons):
        disposition = "rejected"
    elif any(item["disposition"] == "quarantined" for item in reasons):
        disposition = "quarantined"
    return {
        "status": disposition,
        "eligible": disposition == "eligible",
        "rejection_reasons": [item for item in reasons if item["disposition"] == "rejected"],
        "quarantine_reasons": [item for item in reasons if item["disposition"] == "quarantined"],
        "endpoint_alignment": {
            "explicit_tolerance_seconds": float(endpoint_alignment_tolerance_seconds),
            "observations": endpoint_alignment,
        },
        "source_versions": {
            "fingerprint_schema": fingerprint.get("feature_schema_version"),
            "fingerprint_calculation": fingerprint.get("calculation_version"),
            "snapshot_schema": endpoint_snapshot.get("schema_version"),
            "snapshot_calculation": endpoint_snapshot.get("calculation_version"),
            "outcome_schema": outcome.get("schema_version"),
        },
    }


def _group(
    *,
    status: str,
    values: Any,
    units: Mapping[str, Any] | None,
    source_references: Iterable[Mapping[str, Any]],
    calculation_version: str,
    cutoff: str | None,
    unavailable_reason: str | None = None,
    incomparable_reason: str | None = None,
) -> dict[str, Any]:
    if status not in {"available", "partial", "unavailable", "incomparable"}:
        raise ValueError(f"Unsupported DNA feature-group status: {status!r}")
    return {
        "status": status,
        "values": deepcopy(values) if values is not None else None,
        "units": dict(units or {}),
        "source_references": [deepcopy(dict(item)) for item in source_references],
        "calculation_version": calculation_version,
        "cutoff": cutoff,
        "unavailable_reason": unavailable_reason,
        "incomparable_reason": incomparable_reason,
    }


def _fingerprint_reference(fingerprint: Mapping[str, Any], *paths: str) -> list[dict[str, Any]]:
    return [
        {
            "source_type": "phase3_wave_fingerprint",
            "content_hash": fingerprint.get("content_hash"),
            "identity_hash": fingerprint.get("identity_hash"),
            "field_paths": list(paths),
        }
    ]


def _indicator_group(
    fingerprint: Mapping[str, Any],
    name: str,
    cutoff: str | None,
) -> dict[str, Any]:
    indicators = fingerprint.get("indicators") if isinstance(fingerprint.get("indicators"), Mapping) else {}
    value = indicators.get(name)
    reference = _fingerprint_reference(fingerprint, f"indicators.{name}")
    if not isinstance(value, Mapping):
        label = "Optional RSI" if name == "rsi" else name.upper()
        return _group(
            status="unavailable",
            values=None,
            units={},
            source_references=reference,
            calculation_version=str(fingerprint.get("calculation_version") or "unknown"),
            cutoff=cutoff,
            unavailable_reason=f"{label} was not available in the source fingerprint.",
        )
    availability = str(value.get("availability") or "available")
    status = "available" if availability == "available" else "unavailable"
    return _group(
        status=status,
        values=value if status == "available" else None,
        units={"slope_per_bar": "indicator units per candle"},
        source_references=reference,
        calculation_version=str(fingerprint.get("calculation_version") or "unknown"),
        cutoff=cutoff,
        unavailable_reason=(
            str(value.get("unavailable_reason") or f"{name.upper()} source block is unavailable.")
            if status == "unavailable"
            else None
        ),
        incomparable_reason=(
            str(value.get("incomparable_reason"))
            if value.get("comparability") == "incomparable"
            else None
        ),
    )


def build_endpoint_dna(
    *,
    experience_case_id: str,
    market_episode_id: str,
    endpoint_snapshot: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a Phase 3 fingerprint without introducing later evidence."""
    cutoff_block = fingerprint.get("cutoff") if isinstance(fingerprint.get("cutoff"), Mapping) else {}
    cutoff = cutoff_block.get("timestamp")
    identity = fingerprint.get("identity") if isinstance(fingerprint.get("identity"), Mapping) else {}
    source = fingerprint.get("source") if isinstance(fingerprint.get("source"), Mapping) else {}
    price = fingerprint.get("price") if isinstance(fingerprint.get("price"), Mapping) else {}
    structural = fingerprint.get("structural") if isinstance(fingerprint.get("structural"), Mapping) else {}
    data_quality = fingerprint.get("data_quality") if isinstance(fingerprint.get("data_quality"), Mapping) else {}
    calculation = str(fingerprint.get("calculation_version") or "unknown")
    endpoint_ref = _fingerprint_reference(fingerprint)
    indicators = fingerprint.get("indicators") if isinstance(fingerprint.get("indicators"), Mapping) else {}
    momentum_values = {
        "ewo": deepcopy(indicators.get("ewo")) if isinstance(indicators.get("ewo"), Mapping) else None,
        "macd": deepcopy(indicators.get("macd")) if isinstance(indicators.get("macd"), Mapping) else None,
    }
    momentum_status = "available" if any(value is not None for value in momentum_values.values()) else "unavailable"
    groups: dict[str, Any] = {
        "source_identity": _group(
            status="available",
            values={"source": source, "identity": identity},
            units={},
            source_references=_fingerprint_reference(fingerprint, "source", "identity"),
            calculation_version=calculation,
            cutoff=cutoff,
        ),
        "structural_context": _group(
            status="available",
            values={
                "wave_id": identity.get("wave_id"),
                "candidate_role": identity.get("wave_label"),
                "elliott_degree": identity.get("wave_degree"),
                "parent_wave_id": identity.get("parent_wave_id"),
                "parent_pattern_family": identity.get("parent_pattern_family"),
                "internal_structure_family": structural.get("internal_structure_family"),
                "internal_child_count": structural.get("internal_child_count"),
                "child_structure_status": structural.get("child_structure_status"),
                "completion_status_at_cutoff": structural.get("completion_status_at_cutoff"),
            },
            units={},
            source_references=_fingerprint_reference(fingerprint, "identity", "structural"),
            calculation_version=calculation,
            cutoff=cutoff,
        ),
        "price_shape": _group(
            status="available",
            values={
                key: deepcopy(price.get(key))
                for key in (
                    "start_price",
                    "end_price",
                    "signed_change",
                    "absolute_change",
                    "percentage_change",
                    "log_return",
                    "direction",
                    "highest_high",
                    "lowest_low",
                    "price_range",
                    "normalized_range_pct",
                    "directional_efficiency",
                    "acceleration",
                )
            },
            units={
                "start_price": "quote currency",
                "end_price": "quote currency",
                "percentage_change": "percent",
                "normalized_range_pct": "percent",
            },
            source_references=_fingerprint_reference(fingerprint, "price"),
            calculation_version=calculation,
            cutoff=cutoff,
        ),
        "duration": _group(
            status="available" if structural.get("duration_candles") is not None else "unavailable",
            values={
                "candles": structural.get("duration_candles"),
                "clock_seconds": structural.get("duration_clock_seconds"),
                "clock_days": structural.get("duration_clock_days"),
            } if structural.get("duration_candles") is not None else None,
            units={"candles": "candles", "clock_seconds": "seconds", "clock_days": "days"},
            source_references=_fingerprint_reference(fingerprint, "structural.duration_*"),
            calculation_version=calculation,
            cutoff=cutoff,
            unavailable_reason="Duration was unavailable in the source fingerprint." if structural.get("duration_candles") is None else None,
        ),
        "pivot_path": _group(
            status="partial",
            values={
                "start": {"timestamp": identity.get("start_timestamp"), "price": price.get("start_price")},
                "end": {"timestamp": identity.get("end_timestamp"), "price": price.get("end_price")},
                "pivot_count": structural.get("pivot_count"),
                "start_alignment": price.get("start_pivot_alignment"),
                "end_alignment": price.get("end_pivot_alignment"),
                "full_internal_pivot_path": None,
            },
            units={"price": "quote currency", "pivot_count": "pivots"},
            source_references=_fingerprint_reference(fingerprint, "price.*pivot*", "structural.pivot_count"),
            calculation_version=calculation,
            cutoff=cutoff,
            unavailable_reason="The Phase 3 fingerprint stores endpoint anchors and pivot count, not a reconstructed full pivot path.",
        ),
        "fibonacci_relationships": _group(
            status="available" if any(isinstance(structural.get(key), Mapping) and structural[key].get("status") == "available" for key in ("retracement_ratio", "extension_ratio", "overlap_ratio")) else "unavailable",
            values={
                "retracement": deepcopy(structural.get("retracement_ratio")),
                "extension": deepcopy(structural.get("extension_ratio")),
                "overlap": deepcopy(structural.get("overlap_ratio")),
            } if any(isinstance(structural.get(key), Mapping) and structural[key].get("status") == "available" for key in ("retracement_ratio", "extension_ratio", "overlap_ratio")) else None,
            units={"retracement": "ratio", "extension": "ratio", "overlap": "ratio"},
            source_references=_fingerprint_reference(fingerprint, "structural.*_ratio"),
            calculation_version=calculation,
            cutoff=cutoff,
            unavailable_reason="No valid comparable reference wave was supplied to the fingerprint." if not any(isinstance(structural.get(key), Mapping) and structural[key].get("status") == "available" for key in ("retracement_ratio", "extension_ratio", "overlap_ratio")) else None,
        ),
        "channel_behaviour": _group(
            status="available" if isinstance(price.get("channel"), Mapping) and price["channel"].get("availability") == "available" else "unavailable",
            values=deepcopy(price.get("channel")) if isinstance(price.get("channel"), Mapping) and price["channel"].get("availability") == "available" else None,
            units={"slope_per_bar": "price units per candle", "terminal_deviation_sigma": "standard deviations"},
            source_references=_fingerprint_reference(fingerprint, "price.channel"),
            calculation_version=calculation,
            cutoff=cutoff,
            unavailable_reason="Channel behaviour was unavailable in the source fingerprint." if not isinstance(price.get("channel"), Mapping) or price["channel"].get("availability") != "available" else None,
        ),
        "momentum": _group(
            status=momentum_status,
            values=momentum_values if momentum_status == "available" else None,
            units={"ewo": "price units", "macd": "price units"},
            source_references=_fingerprint_reference(fingerprint, "indicators.ewo", "indicators.macd"),
            calculation_version=calculation,
            cutoff=cutoff,
            unavailable_reason="Neither EWO nor MACD was available in the source fingerprint." if momentum_status == "unavailable" else None,
        ),
        "volume": _indicator_group(fingerprint, "volume", cutoff),
        "volatility": _indicator_group(fingerprint, "volatility", cutoff),
        "optional_rsi": _indicator_group(fingerprint, "rsi", cutoff),
        "market_context": _group(
            status="partial",
            values={
                "market_type": source.get("market_type"),
                "quote_currency": source.get("quote_currency"),
                "session": source.get("session"),
                "venue": source.get("venue"),
                "broader_regime": None,
            },
            units={},
            source_references=_fingerprint_reference(fingerprint, "source"),
            calculation_version=calculation,
            cutoff=cutoff,
            unavailable_reason="The fingerprint contains feed context but no separately resolved broader market regime.",
        ),
        "availability_and_comparability": _group(
            status="available",
            values={
                "data_quality": data_quality,
                "active_feature_flags": identity.get("active_feature_flags"),
                "indicator_availability": {
                    name: value.get("availability") if isinstance(value, Mapping) else "unavailable"
                    for name, value in (
                        ("ewo", indicators.get("ewo")),
                        ("macd", indicators.get("macd")),
                        ("volume", indicators.get("volume")),
                        ("volatility", indicators.get("volatility")),
                        ("rsi", indicators.get("rsi")),
                    )
                },
            },
            units={},
            source_references=_fingerprint_reference(fingerprint, "data_quality", "identity.active_feature_flags", "indicators"),
            calculation_version=calculation,
            cutoff=cutoff,
        ),
        "evidence_references": _group(
            status="available" if fingerprint.get("evidence_items") else "unavailable",
            values={"items": deepcopy(fingerprint.get("evidence_items"))} if fingerprint.get("evidence_items") else None,
            units={},
            source_references=_fingerprint_reference(fingerprint, "evidence_items"),
            calculation_version=calculation,
            cutoff=cutoff,
            unavailable_reason="No evidence items were stored in the source fingerprint." if not fingerprint.get("evidence_items") else None,
        ),
    }
    payload: dict[str, Any] = {
        "schema_version": PATTERN_DNA_SCHEMA_VERSION,
        "calculation_version": PATTERN_DNA_CALCULATION_VERSION,
        "dna_kind": "endpoint",
        "experience_case_id": experience_case_id,
        "market_episode_id": market_episode_id,
        "cutoff": cutoff,
        "source_fingerprint_hash": fingerprint.get("content_hash"),
        "source_endpoint_snapshot_id": endpoint_snapshot.get("snapshot_id"),
        "source_endpoint_snapshot_hash": endpoint_snapshot.get("content_hash"),
        "groups": groups,
        "source_field_map": {
            name: deepcopy(DNA_FIELD_SOURCE_MAP[name]) for name in DNA_GROUP_NAMES
        },
        "policy": "Endpoint DNA is a projection of the original cutoff-safe Phase 3 fingerprint only.",
    }
    payload["content_hash"] = pattern_dna_content_hash(payload)
    payload["dna_id"] = f"pattern_dna_{payload['content_hash'][:32]}"
    payload["content_hash"] = pattern_dna_content_hash(payload)
    return payload


def _event_delay(
    endpoint_timestamp: Any,
    available_cutoff: Any,
    observed_at: Any,
    candle_count: Any,
) -> dict[str, Any]:
    target = observed_at or available_cutoff
    first = parse_date(endpoint_timestamp)
    second = parse_date(target)
    seconds: float | None = None
    if first is not None and second is not None:
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        if second.tzinfo is None:
            second = second.replace(tzinfo=timezone.utc)
        seconds = (second.astimezone(timezone.utc) - first.astimezone(timezone.utc)).total_seconds()
    return {
        "candles": int(candle_count) if isinstance(candle_count, int) and not isinstance(candle_count, bool) else None,
        "clock_seconds": seconds,
        "clock_days": seconds / 86_400.0 if seconds is not None else None,
    }


def _first_confirmation_events(
    snapshot_lineage: Sequence[Mapping[str, Any]], endpoint_timestamp: str
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for output_name, source_name in CONFIRMATION_EVENT_SOURCES.items():
        selected: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
        for snapshot in snapshot_lineage:
            events = snapshot.get("post_terminal_events")
            if not isinstance(events, Mapping):
                continue
            event = events.get(source_name)
            if not isinstance(event, Mapping):
                continue
            status = str(event.get("status") or "unavailable")
            if status not in {"unavailable", "not_observed"}:
                selected = (snapshot, event)
                break
            if selected is None and status == "not_observed":
                selected = (snapshot, event)
        if selected is None:
            result[output_name] = {
                "status": "unavailable",
                "observed_at": None,
                "earliest_available_cutoff": None,
                "delay_from_endpoint": {"candles": None, "clock_seconds": None, "clock_days": None},
                "source_snapshot_id": None,
                "source_event_name": source_name,
                "source_transition_ids": [],
                "verification_status": None,
                "details": None,
                "reason": "No explicit Phase 4 event exists in the supplied snapshot lineage.",
            }
            continue
        snapshot, event = selected
        events = snapshot.get("post_terminal_events") if isinstance(snapshot.get("post_terminal_events"), Mapping) else {}
        details = event.get("details") if isinstance(event.get("details"), Mapping) else None
        verification: Any = None
        if source_name == "five_wave_move_away":
            verification = details.get("structure_verified") if details else None
        elif source_name == "retracement":
            verification = str(event.get("status") or "unavailable")
        result[output_name] = {
            "status": str(event.get("status") or "unavailable"),
            "observed_at": event.get("observed_at"),
            "earliest_available_cutoff": snapshot.get("cutoff"),
            "delay_from_endpoint": _event_delay(
                endpoint_timestamp,
                snapshot.get("cutoff"),
                event.get("observed_at"),
                events.get("post_endpoint_candle_count"),
            ),
            "source_snapshot_id": snapshot.get("snapshot_id"),
            "source_snapshot_hash": snapshot.get("content_hash"),
            "source_event_name": source_name,
            "source_transition_ids": [
                item.get("transition_id")
                for item in snapshot.get("transitions", [])
                if isinstance(item, Mapping) and item.get("transition_id")
            ],
            "verification_status": verification,
            "details": deepcopy(details),
            "reason": event.get("reason"),
        }
    return result


def _empty_groups(
    *, cutoff: str | None, calculation_version: str, source_reference: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        name: _group(
            status="unavailable",
            values=None,
            units={},
            source_references=[source_reference],
            calculation_version=calculation_version,
            cutoff=cutoff,
            unavailable_reason=f"{name.replace('_', ' ').title()} is not represented by this DNA timing layer.",
        )
        for name in DNA_GROUP_NAMES
    }


def build_confirmation_dna(
    *,
    experience_case_id: str,
    market_episode_id: str,
    endpoint_snapshot: Mapping[str, Any],
    snapshot_lineage: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project explicitly recorded Phase 4 events with first-availability timing."""
    endpoint_timestamp = str((endpoint_snapshot.get("candidate_endpoint") or {}).get("timestamp") or "")
    ordered = sorted(
        (deepcopy(dict(item)) for item in snapshot_lineage),
        key=lambda item: str(item.get("cutoff") or ""),
    )
    cutoff = str(ordered[-1].get("cutoff")) if ordered else str(endpoint_snapshot.get("cutoff") or "")
    lineage_refs = [
        {
            "source_type": "phase4_hypothesis_snapshot",
            "snapshot_id": item.get("snapshot_id"),
            "content_hash": item.get("content_hash"),
            "cutoff": item.get("cutoff"),
        }
        for item in ordered
    ]
    source_ref = {
        "source_type": "phase4_snapshot_lineage",
        "snapshot_ids": [item.get("snapshot_id") for item in ordered],
    }
    groups = _empty_groups(
        cutoff=cutoff,
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        source_reference=source_ref,
    )
    events = _first_confirmation_events(ordered, endpoint_timestamp)
    source_identity = endpoint_snapshot.get("source_identity") if isinstance(endpoint_snapshot.get("source_identity"), Mapping) else None
    groups["source_identity"] = _group(
        status="available" if source_identity else "unavailable",
        values=source_identity,
        units={},
        source_references=lineage_refs,
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
        unavailable_reason="Phase 4 lineage has no source identity." if not source_identity else None,
    )
    groups["structural_context"] = _group(
        status="available" if ordered else "unavailable",
        values={
            "active_hypotheses_at_last_cutoff": ordered[-1].get("active_hypotheses") if ordered else None,
            "invalidated_hypotheses_at_last_cutoff": ordered[-1].get("invalidated_hypotheses") if ordered else None,
            "structural_invalidations": ordered[-1].get("structural_invalidations") if ordered else None,
        } if ordered else None,
        units={},
        source_references=lineage_refs,
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
        unavailable_reason="No Phase 4 snapshot lineage was supplied." if not ordered else None,
    )
    groups["duration"] = _group(
        status="available" if ordered else "unavailable",
        values={
            "endpoint_to_last_snapshot": _event_delay(endpoint_timestamp, cutoff, cutoff, None),
            "per_event": {name: event["delay_from_endpoint"] for name, event in events.items()},
        } if ordered else None,
        units={"candles": "candles", "clock_seconds": "seconds", "clock_days": "days"},
        source_references=lineage_refs,
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
        unavailable_reason="No Phase 4 snapshot lineage was supplied." if not ordered else None,
    )
    groups["channel_behaviour"] = _group(
        status="available" if events["channel_break"]["status"] not in {"unavailable", "not_observed"} else "unavailable",
        values={"channel_break": events["channel_break"]} if events["channel_break"]["status"] not in {"unavailable", "not_observed"} else None,
        units={},
        source_references=lineage_refs,
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
        unavailable_reason="No explicit channel-break event was available." if events["channel_break"]["status"] in {"unavailable", "not_observed"} else None,
    )
    groups["fibonacci_relationships"] = _group(
        status="partial" if events["corrective_retracement_candidate"]["status"] not in {"unavailable", "not_observed"} else "unavailable",
        values={"retracement_event": events["corrective_retracement_candidate"]} if events["corrective_retracement_candidate"]["status"] not in {"unavailable", "not_observed"} else None,
        units={"retracement": "ratio when supplied by Phase 4 event details"},
        source_references=lineage_refs,
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
        unavailable_reason="No explicit retracement event was available." if events["corrective_retracement_candidate"]["status"] in {"unavailable", "not_observed"} else "Only values explicitly stored by Phase 4 are retained.",
    )
    groups["availability_and_comparability"] = _group(
        status="available",
        values={
            "event_statuses": {name: item["status"] for name, item in events.items()},
            "inferred_from_outcome_text": False,
        },
        units={},
        source_references=lineage_refs,
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
    )
    groups["evidence_references"] = _group(
        status="available" if lineage_refs else "unavailable",
        values={
            "snapshots": lineage_refs,
            "transition_ids": [
                transition.get("transition_id")
                for item in ordered
                for transition in item.get("transitions", [])
                if isinstance(transition, Mapping) and transition.get("transition_id")
            ],
        } if lineage_refs else None,
        units={},
        source_references=lineage_refs,
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
        unavailable_reason="No Phase 4 lineage references exist." if not lineage_refs else None,
    )
    groups["confirmation_events"] = _group(
        status="available" if any(item["status"] not in {"unavailable", "not_observed"} for item in events.values()) else "unavailable",
        values=events,
        units={"delay.candles": "candles", "delay.clock_seconds": "seconds"},
        source_references=lineage_refs,
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
        unavailable_reason="No positive or candidate confirmation event was explicitly recorded." if not any(item["status"] not in {"unavailable", "not_observed"} for item in events.values()) else None,
    )
    payload: dict[str, Any] = {
        "schema_version": PATTERN_DNA_SCHEMA_VERSION,
        "calculation_version": PATTERN_DNA_CALCULATION_VERSION,
        "dna_kind": "confirmation",
        "experience_case_id": experience_case_id,
        "market_episode_id": market_episode_id,
        "cutoff": cutoff,
        "source_endpoint_snapshot_id": endpoint_snapshot.get("snapshot_id"),
        "source_snapshot_hashes": [item.get("content_hash") for item in ordered],
        "groups": groups,
        "policy": "Only explicit Phase 4 events and transitions are projected; final outcome prose is not parsed for events.",
    }
    payload["content_hash"] = pattern_dna_content_hash(payload)
    payload["dna_id"] = f"pattern_dna_{payload['content_hash'][:32]}"
    payload["content_hash"] = pattern_dna_content_hash(payload)
    return payload


def build_resolved_outcome_dna(
    *,
    experience_case_id: str,
    market_episode_id: str,
    endpoint_snapshot: Mapping[str, Any],
    outcome: Mapping[str, Any],
    outcome_review: Mapping[str, Any],
) -> dict[str, Any]:
    """Project the reviewed result without rewriting endpoint or confirmation DNA."""
    cutoff = str(outcome.get("resolution_cutoff") or outcome_review.get("reviewed_at") or "")
    source_ref = {
        "source_type": "phase4_reviewed_outcome",
        "outcome_id": outcome.get("outcome_id"),
        "outcome_hash": outcome.get("content_hash"),
        "outcome_review_id": outcome_review.get("review_id"),
        "outcome_review_hash": outcome_review.get("content_hash"),
    }
    groups = _empty_groups(
        cutoff=cutoff,
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        source_reference=source_ref,
    )
    groups["source_identity"] = _group(
        status="available",
        values=deepcopy(endpoint_snapshot.get("source_identity")),
        units={},
        source_references=[source_ref],
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
    )
    groups["structural_context"] = _group(
        status="available",
        values={
            "resolved_hypothesis": outcome.get("resolved_hypothesis"),
            "final_reviewed_interpretation": outcome.get("final_reviewed_interpretation"),
            "rejected_alternatives": deepcopy(outcome.get("rejected_alternatives") or []),
            "supporting_future_structure": deepcopy(outcome.get("supporting_future_structure") or []),
            "notes": outcome.get("notes"),
        },
        units={},
        source_references=[source_ref],
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
    )
    groups["duration"] = _group(
        status="available",
        values={
            "endpoint_to_resolution": _event_delay(
                (endpoint_snapshot.get("candidate_endpoint") or {}).get("timestamp"),
                cutoff,
                cutoff,
                None,
            )
        },
        units={"clock_seconds": "seconds", "clock_days": "days"},
        source_references=[source_ref],
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
    )
    groups["availability_and_comparability"] = _group(
        status="available",
        values={
            "review_status": outcome.get("review_status"),
            "review_action": outcome_review.get("action"),
            "human_resolution_confidence": deepcopy(outcome.get("resolution_confidence")),
            "probability_calibrated": False,
        },
        units={},
        source_references=[source_ref],
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
    )
    groups["evidence_references"] = _group(
        status="available",
        values={"reviewed_outcome": source_ref},
        units={},
        source_references=[source_ref],
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
    )
    groups["resolved_outcome"] = _group(
        status="available",
        values={
            "outcome": {
                "outcome_id": outcome.get("outcome_id"),
                "revision": outcome.get("revision"),
                "resolved_hypothesis": outcome.get("resolved_hypothesis"),
                "final_reviewed_interpretation": outcome.get("final_reviewed_interpretation"),
                "resolution_cutoff": outcome.get("resolution_cutoff"),
                "rejected_alternatives": deepcopy(outcome.get("rejected_alternatives") or []),
                "supporting_future_structure": deepcopy(outcome.get("supporting_future_structure") or []),
            },
            "review": {
                "review_id": outcome_review.get("review_id"),
                "action": outcome_review.get("action"),
                "reviewer": outcome_review.get("reviewer"),
                "review_status": outcome_review.get("review_status"),
                "reason": outcome_review.get("reason"),
                "reviewed_at": outcome_review.get("reviewed_at"),
            },
        },
        units={},
        source_references=[source_ref],
        calculation_version=CORRECTION_STATE_CALCULATION_VERSION,
        cutoff=cutoff,
    )
    payload: dict[str, Any] = {
        "schema_version": PATTERN_DNA_SCHEMA_VERSION,
        "calculation_version": PATTERN_DNA_CALCULATION_VERSION,
        "dna_kind": "resolved_outcome",
        "experience_case_id": experience_case_id,
        "market_episode_id": market_episode_id,
        "cutoff": cutoff,
        "source_outcome_id": outcome.get("outcome_id"),
        "source_outcome_hash": outcome.get("content_hash"),
        "source_outcome_review_id": outcome_review.get("review_id"),
        "source_outcome_review_hash": outcome_review.get("content_hash"),
        "groups": groups,
        "policy": "Outcome DNA records human-reviewed history and never modifies earlier DNA records.",
    }
    payload["content_hash"] = pattern_dna_content_hash(payload)
    payload["dna_id"] = f"pattern_dna_{payload['content_hash'][:32]}"
    payload["content_hash"] = pattern_dna_content_hash(payload)
    return payload


def assess_experience_quality(
    *,
    eligibility: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
    endpoint_snapshot: Mapping[str, Any],
    outcome: Mapping[str, Any],
    outcome_review: Mapping[str, Any],
) -> dict[str, Any]:
    """Assign a transparent, reviewable quality status without a score."""
    identity = fingerprint.get("identity") if isinstance(fingerprint.get("identity"), Mapping) else {}
    data_quality = fingerprint.get("data_quality") if isinstance(fingerprint.get("data_quality"), Mapping) else {}
    indicators = fingerprint.get("indicators") if isinstance(fingerprint.get("indicators"), Mapping) else {}
    endpoint = endpoint_snapshot.get("candidate_endpoint") if isinstance(endpoint_snapshot.get("candidate_endpoint"), Mapping) else {}
    structural_anchor_complete = all(
        value not in (None, "")
        for value in (
            endpoint.get("timestamp"),
            endpoint.get("price"),
            endpoint.get("terminal_label"),
            endpoint_snapshot.get("elliott_degree"),
            endpoint_snapshot.get("parent_pattern_family"),
            identity.get("start_timestamp"),
            identity.get("end_timestamp"),
        )
    )
    fingerprint_status = str(data_quality.get("status") or identity.get("data_quality_status") or "unknown")
    fingerprint_complete = fingerprint_status == "sufficient"
    feed_consistent = not any(
        item.get("code") in {"provider_mismatch", "feed_identity_mismatch", "symbol_mismatch"}
        for item in [
            *eligibility.get("rejection_reasons", []),
            *eligibility.get("quarantine_reasons", []),
        ]
    )
    timeframe_complete = all(
        _normalize_timeframe(value) not in {"", "unknown"}
        for value in (endpoint_snapshot.get("timeframe"), identity.get("timeframe"))
    )
    review_agreement = (
        str(outcome.get("reviewer") or "") == str(outcome_review.get("reviewer") or "")
        and str(outcome_review.get("action")) in {"approved", "revised"}
    )
    cutoff_safe = not any(
        "cutoff" in str(item.get("code"))
        for item in eligibility.get("quarantine_reasons", [])
    )
    optional_gaps = [
        name
        for name in ("rsi", "ewo", "macd", "volume", "volatility")
        if not isinstance(indicators.get(name), Mapping)
        or indicators[name].get("availability") != "available"
    ]
    checks = {
        "structural_anchor_completeness": structural_anchor_complete,
        "fingerprint_completeness": fingerprint_complete,
        "fingerprint_data_quality_status": fingerprint_status,
        "feed_consistency": feed_consistent,
        "timeframe_completeness": timeframe_complete,
        "review_agreement": review_agreement,
        "cutoff_safety": cutoff_safe,
        "optional_feature_gaps": optional_gaps,
    }
    if not eligibility.get("eligible") or not feed_consistent or not cutoff_safe:
        quality = "quarantined"
    elif all(
        (
            structural_anchor_complete,
            fingerprint_complete,
            timeframe_complete,
            review_agreement,
        )
    ):
        quality = "high"
    elif structural_anchor_complete and timeframe_complete:
        quality = "medium"
    else:
        quality = "low"
    reasons = [
        {
            "criterion": key,
            "result": value,
            "quality_effect": (
                "mandatory_safety"
                if key in {"feed_consistency", "cutoff_safety"}
                else "informational_only"
                if key == "optional_feature_gaps"
                else "core_completeness"
            ),
        }
        for key, value in checks.items()
    ]
    return {
        "status": quality,
        "assignment_method": "phase5a1_rule_based_reviewable",
        "reasons": reasons,
        "predictive_score": None,
        "similarity_weight": None,
        "profitability_used": False,
        "market_return_used": False,
    }


def assemble_experience_candidate(
    *,
    correction_case: Mapping[str, Any],
    endpoint_snapshot: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
    outcome: Mapping[str, Any],
    outcome_review: Mapping[str, Any],
    snapshot_lineage: Sequence[Mapping[str, Any]],
    endpoint_alignment_tolerance_seconds: float = 0.0,
    outcome_is_current: bool = True,
) -> dict[str, Any]:
    """Assemble a candidate and its three immutable DNA records."""
    eligibility = validate_experience_eligibility(
        correction_case=correction_case,
        endpoint_snapshot=endpoint_snapshot,
        fingerprint=fingerprint,
        outcome=outcome,
        outcome_review=outcome_review,
        endpoint_alignment_tolerance_seconds=endpoint_alignment_tolerance_seconds,
        outcome_is_current=outcome_is_current,
    )
    episode = market_episode_identity(correction_case)
    if not eligibility["eligible"]:
        return {
            "state": eligibility["status"],
            "market_episode_id": episode["market_episode_id"],
            "episode_identity": episode,
            "eligibility": eligibility,
            "experience_case": None,
            "pattern_dna": None,
        }
    case_identity = experience_case_identity(
        market_episode_id=episode["market_episode_id"],
        endpoint_snapshot=endpoint_snapshot,
        fingerprint=fingerprint,
        outcome=outcome,
        outcome_review=outcome_review,
    )
    quality = assess_experience_quality(
        eligibility=eligibility,
        fingerprint=fingerprint,
        endpoint_snapshot=endpoint_snapshot,
        outcome=outcome,
        outcome_review=outcome_review,
    )
    case_payload: dict[str, Any] = {
        "schema_version": EXPERIENCE_SCHEMA_VERSION,
        "calculation_version": EXPERIENCE_CALCULATION_VERSION,
        "experience_case_id": case_identity["experience_case_id"],
        "market_episode_id": episode["market_episode_id"],
        "assembly_state": "assembled_candidate",
        "episode_identity": episode,
        "case_identity": case_identity,
        "source_references": {
            "correction_case_id": correction_case.get("case_id"),
            "correction_case_hash": correction_case.get("content_hash"),
            "endpoint_snapshot_id": endpoint_snapshot.get("snapshot_id"),
            "endpoint_snapshot_hash": endpoint_snapshot.get("content_hash"),
            "fingerprint_content_hash": fingerprint.get("content_hash"),
            "fingerprint_identity_hash": fingerprint.get("identity_hash"),
            "outcome_id": outcome.get("outcome_id"),
            "outcome_content_hash": outcome.get("content_hash"),
            "outcome_revision": outcome.get("revision"),
            "outcome_review_id": outcome_review.get("review_id"),
            "outcome_review_hash": outcome_review.get("content_hash"),
        },
        "symbol": correction_case.get("symbol"),
        "timeframe": correction_case.get("timeframe"),
        "elliott_degree": correction_case.get("elliott_degree"),
        "candidate_role": (correction_case.get("candidate_endpoint") or {}).get("terminal_label"),
        "parent_pattern_family": correction_case.get("parent_pattern_family"),
        "endpoint_timestamp": (correction_case.get("candidate_endpoint") or {}).get("timestamp"),
        "eligibility": deepcopy(eligibility),
        "proposed_quality": quality,
        "created_from_reviewed_at": outcome_review.get("reviewed_at"),
        "policy": "A valid Phase 4 outcome remains pending until a named human explicitly accepts this experience version.",
    }
    case_payload["content_hash"] = experience_case_content_hash(case_payload)
    endpoint_dna = build_endpoint_dna(
        experience_case_id=case_identity["experience_case_id"],
        market_episode_id=episode["market_episode_id"],
        endpoint_snapshot=endpoint_snapshot,
        fingerprint=fingerprint,
    )
    confirmation_dna = build_confirmation_dna(
        experience_case_id=case_identity["experience_case_id"],
        market_episode_id=episode["market_episode_id"],
        endpoint_snapshot=endpoint_snapshot,
        snapshot_lineage=snapshot_lineage,
    )
    outcome_dna = build_resolved_outcome_dna(
        experience_case_id=case_identity["experience_case_id"],
        market_episode_id=episode["market_episode_id"],
        endpoint_snapshot=endpoint_snapshot,
        outcome=outcome,
        outcome_review=outcome_review,
    )
    return {
        "state": "assembled_candidate",
        "market_episode_id": episode["market_episode_id"],
        "episode_identity": episode,
        "eligibility": eligibility,
        "experience_case": case_payload,
        "pattern_dna": {
            "endpoint": endpoint_dna,
            "confirmation": confirmation_dna,
            "resolved_outcome": outcome_dna,
        },
    }


def build_experience_review(
    *,
    experience_case_id: str,
    action: str,
    reviewer: str,
    rationale: str,
    reviewed_at: Any,
    quality_status: str | None = None,
    parent_review_id: str | None = None,
    source_outcome_review_id: str | None = None,
    human_confirmed: bool = False,
    effective_action: str | None = None,
) -> dict[str, Any]:
    if action not in EXPERIENCE_REVIEW_ACTIONS:
        raise ValueError(f"Unsupported experience review action: {action!r}")
    if not str(reviewer).strip():
        raise ValueError("A named reviewer identity is required.")
    if not str(rationale).strip():
        raise ValueError("Experience review rationale is required.")
    resolved_action = effective_action if action == "revise" else action
    if resolved_action in {"accept", "reject", "quarantine"} and resolved_action not in DIRECT_REVIEW_ACTIONS:
        raise ValueError("Invalid effective review action.")
    if action == "revise" and effective_action not in DIRECT_REVIEW_ACTIONS:
        raise ValueError("A revised review requires an effective accept, reject, or quarantine action.")
    if resolved_action == "accept" and not human_confirmed:
        raise ValueError("Acceptance requires an explicit named human confirmation.")
    if quality_status is not None and quality_status not in QUALITY_STATUSES:
        raise ValueError(f"Unsupported experience quality status: {quality_status!r}")
    if resolved_action == "quarantine":
        quality_status = "quarantined"
    payload: dict[str, Any] = {
        "schema_version": EXPERIENCE_SCHEMA_VERSION,
        "calculation_version": EXPERIENCE_CALCULATION_VERSION,
        "experience_case_id": experience_case_id,
        "action": action,
        "effective_action": resolved_action,
        "reviewer": str(reviewer).strip(),
        "review_origin": "human" if human_confirmed else "manual_or_system_nonaccepting",
        "human_confirmed": bool(human_confirmed),
        "quality_status": quality_status,
        "rationale": str(rationale).strip(),
        "reviewed_at": _iso(reviewed_at, field="experience review timestamp"),
        "parent_review_id": parent_review_id,
        "supersedes_review_id": parent_review_id if action == "revise" else None,
        "source_outcome_review_id": source_outcome_review_id,
    }
    payload["content_hash"] = experience_review_content_hash(payload)
    payload["review_id"] = f"experience_review_{payload['content_hash'][:32]}"
    payload["content_hash"] = experience_review_content_hash(payload)
    return payload


def build_experience_tag(tag: str) -> dict[str, Any]:
    raw = str(tag or "").strip().lower()
    if ":" not in raw:
        raise ValueError("Experience tags must use namespace:value form.")
    namespace, value = raw.split(":", 1)
    if not re.fullmatch(r"[a-z][a-z0-9-]*", namespace) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_.-]*", value
    ):
        raise ValueError("Experience tag namespace or value is invalid.")
    payload: dict[str, Any] = {
        "schema_version": EXPERIENCE_TAG_SCHEMA_VERSION,
        "namespace": namespace,
        "value": value,
        "tag": f"{namespace}:{value}",
    }
    payload["content_hash"] = tag_content_hash(payload)
    payload["tag_id"] = f"experience_tag_{payload['content_hash'][:32]}"
    payload["content_hash"] = tag_content_hash(payload)
    return payload


def build_tag_assignment(
    *,
    experience_case_id: str,
    tag: Mapping[str, Any],
    action: str,
    actor: str,
    rationale: str,
    assigned_at: Any,
    parent_assignment_id: str | None = None,
    revision: int | None = None,
) -> dict[str, Any]:
    if action not in TAG_ACTIONS:
        raise ValueError("Tag action must be add or remove.")
    if not str(actor).strip():
        raise ValueError("A tag actor is required.")
    payload: dict[str, Any] = {
        "schema_version": EXPERIENCE_TAG_SCHEMA_VERSION,
        "experience_case_id": experience_case_id,
        "tag_id": tag.get("tag_id"),
        "tag": tag.get("tag"),
        "action": action,
        "actor": str(actor).strip(),
        "rationale": str(rationale or "").strip(),
        "assigned_at": _iso(assigned_at, field="tag assignment timestamp"),
        "parent_assignment_id": parent_assignment_id,
        "revision": revision,
    }
    payload["content_hash"] = tag_assignment_content_hash(payload)
    payload["assignment_id"] = f"experience_tag_action_{payload['content_hash'][:32]}"
    payload["content_hash"] = tag_assignment_content_hash(payload)
    return payload
