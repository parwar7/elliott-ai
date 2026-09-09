"""Deterministic, human-gated historical experience population workflow.

This module sits in front of the existing Phase 5A.1 experience pool. It can
discover and validate immutable Phase 3/4 endpoint sources, but only explicit
human review events can advance a workflow to final acceptance. It performs no
prediction, probability calculation, wave resolution, or trade decision.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .correction_semantics import combination_variant, structure_family
from .correction_state import (
    CORRECTION_STATE_CALCULATION_VERSION,
    CORRECTION_STATE_SCHEMA_VERSION,
    correction_case_content_hash,
    review_content_hash as outcome_review_content_hash,
    snapshot_content_hash,
    validate_snapshot,
)
from .experience import market_episode_identity
from .experience_comparison import ROLE_CLASS_VERSION, classify_structural_role
from .experience_outcomes import (
    DEFAULT_HORIZON_ID,
    DEFAULT_HORIZON_VERSION,
    OUTCOME_TAXONOMY_VERSION,
    STRUCTURAL_OUTCOME_BY_HYPOTHESIS,
    observation_horizon_specification,
    outcome_taxonomy,
)
from .fingerprints import (
    CALCULATION_VERSION as FINGERPRINT_CALCULATION_VERSION,
    FEATURE_SCHEMA_VERSION,
    fingerprint_content_hash,
)
from .market_data import parse_date


WORKFLOW_SPEC_VERSION = "historical-experience-workflow-1.0.0"
WORKFLOW_CASE_SCHEMA_VERSION = "historical-experience-workflow-case-1.0.0"
WORKFLOW_CASE_CALCULATION_VERSION = (
    "historical-experience-workflow-case-calc-1.0.0"
)
WORKFLOW_EVENT_SCHEMA_VERSION = "historical-experience-workflow-event-1.0.0"
SOURCE_PAIR_IDENTITY_VERSION = "experience-workflow-source-pair-1.0.0"
MATERIAL_EPISODE_IDENTITY_VERSION = (
    "experience-workflow-material-episode-1.0.0"
)
STRUCTURAL_REVIEW_VERSION = "experience-structural-review-1.0.0"
OUTCOME_REVIEW_VERSION = "experience-outcome-review-workflow-1.0.0"
FINAL_ACCEPTANCE_VERSION = "experience-final-acceptance-1.0.0"

WORKFLOW_STATES = (
    "discovered",
    "draft",
    "assembled",
    "validation_failed",
    "ready_for_structural_review",
    "structural_review_in_progress",
    "structural_review_accepted",
    "structural_review_rejected",
    "structural_review_needs_revision",
    "structural_review_ambiguous",
    "ready_for_outcome_review",
    "outcome_review_in_progress",
    "outcome_review_accepted",
    "outcome_review_rejected",
    "outcome_review_needs_revision",
    "outcome_review_ambiguous",
    "ready_for_final_acceptance",
    "accepted",
    "rejected",
    "withdrawn",
    "superseded",
)

EVENT_KINDS = (
    "candidate_discovery",
    "draft_creation",
    "draft_assembly",
    "source_validation",
    "stage_transition",
    "structural_review",
    "outcome_review",
    "review_disagreement",
    "disagreement_resolution",
    "final_acceptance",
    "final_rejection",
    "withdrawal",
    "supersession",
)

HUMAN_EVENT_KINDS = frozenset(
    {
        "structural_review",
        "outcome_review",
        "review_disagreement",
        "disagreement_resolution",
        "final_acceptance",
        "final_rejection",
        "withdrawal",
    }
)

TERMINAL_STATES = frozenset({"rejected", "withdrawn", "superseded"})

ALLOWED_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"discovered"}),
    "discovered": frozenset({"draft", "rejected", "withdrawn", "superseded"}),
    "draft": frozenset({"assembled", "validation_failed", "rejected", "withdrawn", "superseded"}),
    "assembled": frozenset({"validation_failed", "ready_for_structural_review", "rejected", "withdrawn", "superseded"}),
    "validation_failed": frozenset({"assembled", "rejected", "withdrawn", "superseded"}),
    "ready_for_structural_review": frozenset({"structural_review_in_progress", "rejected", "withdrawn", "superseded"}),
    "structural_review_in_progress": frozenset(
        {
            "structural_review_accepted",
            "structural_review_rejected",
            "structural_review_needs_revision",
            "structural_review_ambiguous",
        }
    ),
    "structural_review_accepted": frozenset(
        {
            "ready_for_outcome_review",
            "structural_review_in_progress",
            "structural_review_ambiguous",
            "rejected",
            "withdrawn",
            "superseded",
        }
    ),
    "structural_review_rejected": frozenset(
        {"structural_review_in_progress", "structural_review_ambiguous", "rejected", "withdrawn", "superseded"}
    ),
    "structural_review_needs_revision": frozenset(
        {"structural_review_in_progress", "structural_review_ambiguous", "rejected", "withdrawn", "superseded"}
    ),
    "structural_review_ambiguous": frozenset(
        {"structural_review_in_progress", "structural_review_accepted", "structural_review_rejected", "structural_review_needs_revision", "rejected", "withdrawn", "superseded"}
    ),
    "ready_for_outcome_review": frozenset(
        {"outcome_review_in_progress", "structural_review_in_progress", "structural_review_ambiguous", "rejected", "withdrawn", "superseded"}
    ),
    "outcome_review_in_progress": frozenset(
        {
            "outcome_review_accepted",
            "outcome_review_rejected",
            "outcome_review_needs_revision",
            "outcome_review_ambiguous",
        }
    ),
    "outcome_review_accepted": frozenset(
        {
            "ready_for_final_acceptance",
            "outcome_review_in_progress",
            "outcome_review_ambiguous",
            "structural_review_in_progress",
            "structural_review_ambiguous",
            "rejected",
            "withdrawn",
            "superseded",
        }
    ),
    "outcome_review_rejected": frozenset(
        {"outcome_review_in_progress", "outcome_review_ambiguous", "rejected", "withdrawn", "superseded"}
    ),
    "outcome_review_needs_revision": frozenset(
        {"outcome_review_in_progress", "outcome_review_ambiguous", "rejected", "withdrawn", "superseded"}
    ),
    "outcome_review_ambiguous": frozenset(
        {"outcome_review_in_progress", "outcome_review_accepted", "outcome_review_rejected", "outcome_review_needs_revision", "rejected", "withdrawn", "superseded"}
    ),
    "ready_for_final_acceptance": frozenset(
        {
            "accepted",
            "rejected",
            "structural_review_in_progress",
            "structural_review_ambiguous",
            "outcome_review_in_progress",
            "outcome_review_ambiguous",
            "withdrawn",
            "superseded",
        }
    ),
    "accepted": frozenset({"superseded"}),
    "rejected": frozenset({"superseded"}),
    "withdrawn": frozenset({"superseded"}),
    "superseded": frozenset(),
}

STRUCTURAL_REVIEW_DECISIONS = frozenset(
    {"accepted", "rejected", "needs_revision", "structurally_ambiguous"}
)
OUTCOME_REVIEW_DECISIONS = frozenset(
    {"accepted", "rejected", "needs_revision", "structurally_ambiguous"}
)

STRUCTURAL_DECISION_STATE = {
    "accepted": "structural_review_accepted",
    "rejected": "structural_review_rejected",
    "needs_revision": "structural_review_needs_revision",
    "structurally_ambiguous": "structural_review_ambiguous",
}
OUTCOME_DECISION_STATE = {
    "accepted": "outcome_review_accepted",
    "rejected": "outcome_review_rejected",
    "needs_revision": "outcome_review_needs_revision",
    "structurally_ambiguous": "outcome_review_ambiguous",
}

STRUCTURAL_REVIEW_REQUIRED_FIELDS = (
    "structural_role_confirmation",
    "degree_confirmation",
    "endpoint_confirmation",
    "pattern_family_confirmation",
    "rule_validation_notes",
    "fibonacci_notes",
    "rsi_notes",
    "volume_notes",
    "channel_notes",
    "multi_timeframe_notes",
    "ambiguity_notes",
    "alternative_counts",
    "rejection_reason_codes",
)

OUTCOME_REVIEW_REQUIRED_FIELDS = (
    "historical_endpoint_linkage",
    "structural_aftermath",
    "confirmation_invalidation_behavior",
    "unresolved_state",
    "data_completeness",
    "feed_continuity",
    "reviewer_limitations",
    "notes",
    "rejection_reason_codes",
)


EXPERIENCE_WORKFLOW_MIGRATION_SQL = (
    """
    CREATE TABLE IF NOT EXISTS experience_workflow_cases (
        workflow_case_id TEXT PRIMARY KEY NOT NULL,
        market_episode_id TEXT NOT NULL
            CHECK (length(trim(market_episode_id)) > 0),
        workflow_version INTEGER NOT NULL
            CHECK (workflow_version >= 1),
        supersedes_workflow_case_id TEXT
            REFERENCES experience_workflow_cases(workflow_case_id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        source_correction_case_id TEXT NOT NULL
            REFERENCES correction_cases(case_id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        source_endpoint_snapshot_id TEXT NOT NULL
            REFERENCES hypothesis_snapshots(snapshot_id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        source_fingerprint_id INTEGER NOT NULL
            REFERENCES wave_fingerprints(id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        source_pair_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(source_pair_hash) = 64
                AND source_pair_hash NOT GLOB '*[^0-9a-f]*'
            ),
        material_episode_hash TEXT NOT NULL
            CHECK (
                length(material_episode_hash) = 64
                AND material_episode_hash NOT GLOB '*[^0-9a-f]*'
            ),
        schema_version TEXT NOT NULL
            CHECK (length(trim(schema_version)) > 0),
        calculation_version TEXT NOT NULL
            CHECK (length(trim(calculation_version)) > 0),
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        draft_json TEXT NOT NULL
            CHECK (length(trim(draft_json)) > 1),
        created_at TEXT NOT NULL
            CHECK (length(trim(created_at)) > 0),
        UNIQUE (market_episode_id, workflow_version),
        UNIQUE (source_endpoint_snapshot_id, source_fingerprint_id),
        CHECK (
            supersedes_workflow_case_id IS NULL
            OR supersedes_workflow_case_id <> workflow_case_id
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_workflow_events (
        event_id TEXT PRIMARY KEY NOT NULL,
        workflow_case_id TEXT NOT NULL
            REFERENCES experience_workflow_cases(workflow_case_id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        parent_event_id TEXT
            REFERENCES experience_workflow_events(event_id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        sequence_number INTEGER NOT NULL
            CHECK (sequence_number >= 1),
        event_kind TEXT NOT NULL
            CHECK (event_kind IN (
                'candidate_discovery',
                'draft_creation',
                'draft_assembly',
                'source_validation',
                'stage_transition',
                'structural_review',
                'outcome_review',
                'review_disagreement',
                'disagreement_resolution',
                'final_acceptance',
                'final_rejection',
                'withdrawal',
                'supersession'
            )),
        from_state TEXT
            CHECK (from_state IS NULL OR from_state IN (
                'discovered', 'draft', 'assembled', 'validation_failed',
                'ready_for_structural_review', 'structural_review_in_progress',
                'structural_review_accepted', 'structural_review_rejected',
                'structural_review_needs_revision', 'structural_review_ambiguous',
                'ready_for_outcome_review', 'outcome_review_in_progress',
                'outcome_review_accepted', 'outcome_review_rejected',
                'outcome_review_needs_revision', 'outcome_review_ambiguous',
                'ready_for_final_acceptance', 'accepted', 'rejected',
                'withdrawn', 'superseded'
            )),
        to_state TEXT NOT NULL
            CHECK (to_state IN (
                'discovered', 'draft', 'assembled', 'validation_failed',
                'ready_for_structural_review', 'structural_review_in_progress',
                'structural_review_accepted', 'structural_review_rejected',
                'structural_review_needs_revision', 'structural_review_ambiguous',
                'ready_for_outcome_review', 'outcome_review_in_progress',
                'outcome_review_accepted', 'outcome_review_rejected',
                'outcome_review_needs_revision', 'outcome_review_ambiguous',
                'ready_for_final_acceptance', 'accepted', 'rejected',
                'withdrawn', 'superseded'
            )),
        decision TEXT
            CHECK (decision IS NULL OR length(trim(decision)) > 0),
        actor_reference TEXT NOT NULL
            CHECK (length(trim(actor_reference)) > 0),
        reviewer_reference TEXT
            CHECK (
                reviewer_reference IS NULL
                OR length(trim(reviewer_reference)) > 0
            ),
        human_confirmed INTEGER NOT NULL DEFAULT 0
            CHECK (human_confirmed IN (0, 1)),
        source_outcome_id TEXT
            REFERENCES resolved_outcomes(outcome_id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        source_outcome_review_id TEXT
            REFERENCES outcome_reviews(review_id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        result_experience_case_id TEXT
            REFERENCES experience_cases(experience_case_id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        result_experience_review_id TEXT
            REFERENCES experience_reviews(review_id)
            ON UPDATE NO ACTION ON DELETE RESTRICT,
        workflow_spec_version TEXT NOT NULL
            CHECK (length(trim(workflow_spec_version)) > 0),
        event_schema_version TEXT NOT NULL
            CHECK (length(trim(event_schema_version)) > 0),
        content_hash TEXT NOT NULL UNIQUE
            CHECK (
                length(content_hash) = 64
                AND content_hash NOT GLOB '*[^0-9a-f]*'
            ),
        event_json TEXT NOT NULL
            CHECK (length(trim(event_json)) > 1),
        created_at TEXT NOT NULL
            CHECK (length(trim(created_at)) > 0),
        UNIQUE (workflow_case_id, sequence_number),
        UNIQUE (parent_event_id),
        CHECK (parent_event_id IS NULL OR parent_event_id <> event_id),
        CHECK (
            (sequence_number = 1
             AND parent_event_id IS NULL
             AND from_state IS NULL
             AND event_kind = 'candidate_discovery'
             AND to_state = 'discovered')
            OR
            (sequence_number > 1
             AND parent_event_id IS NOT NULL
             AND from_state IS NOT NULL)
        ),
        CHECK (
            event_kind <> 'candidate_discovery'
            OR sequence_number = 1
        ),
        CHECK (
            event_kind NOT IN (
                'structural_review', 'outcome_review',
                'review_disagreement', 'disagreement_resolution',
                'final_acceptance', 'final_rejection', 'withdrawal'
            )
            OR (
                reviewer_reference IS NOT NULL
                AND human_confirmed = 1
            )
        ),
        CHECK (
            (source_outcome_id IS NULL AND source_outcome_review_id IS NULL)
            OR
            (source_outcome_id IS NOT NULL AND source_outcome_review_id IS NOT NULL)
        ),
        CHECK (
            (result_experience_case_id IS NULL
             AND result_experience_review_id IS NULL)
            OR
            (result_experience_case_id IS NOT NULL
             AND result_experience_review_id IS NOT NULL)
        ),
        CHECK (
            result_experience_case_id IS NULL
            OR event_kind = 'final_acceptance'
        ),
        CHECK (
            to_state <> 'accepted'
            OR (
                event_kind = 'final_acceptance'
                AND decision = 'accepted'
                AND human_confirmed = 1
                AND reviewer_reference IS NOT NULL
                AND source_outcome_id IS NOT NULL
                AND source_outcome_review_id IS NOT NULL
                AND result_experience_case_id IS NOT NULL
                AND result_experience_review_id IS NOT NULL
            )
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS experience_workflow_cases_episode_idx ON experience_workflow_cases(market_episode_id, workflow_version)",
    "CREATE INDEX IF NOT EXISTS experience_workflow_cases_material_idx ON experience_workflow_cases(material_episode_hash, workflow_case_id)",
    "CREATE INDEX IF NOT EXISTS experience_workflow_cases_sources_idx ON experience_workflow_cases(source_correction_case_id, source_endpoint_snapshot_id, source_fingerprint_id)",
    "CREATE INDEX IF NOT EXISTS experience_workflow_events_case_idx ON experience_workflow_events(workflow_case_id, sequence_number)",
    "CREATE INDEX IF NOT EXISTS experience_workflow_events_kind_idx ON experience_workflow_events(event_kind, created_at)",
    "CREATE INDEX IF NOT EXISTS experience_workflow_events_state_idx ON experience_workflow_events(to_state, created_at)",
    "CREATE INDEX IF NOT EXISTS experience_workflow_events_outcome_idx ON experience_workflow_events(source_outcome_id, source_outcome_review_id)",
    "CREATE INDEX IF NOT EXISTS experience_workflow_events_result_idx ON experience_workflow_events(result_experience_case_id, result_experience_review_id)",
)

WORKFLOW_TABLES = frozenset(
    {"experience_workflow_cases", "experience_workflow_events"}
)
WORKFLOW_INDEXES = frozenset(
    {
        "experience_workflow_cases_episode_idx",
        "experience_workflow_cases_material_idx",
        "experience_workflow_cases_sources_idx",
        "experience_workflow_events_case_idx",
        "experience_workflow_events_kind_idx",
        "experience_workflow_events_state_idx",
        "experience_workflow_events_outcome_idx",
        "experience_workflow_events_result_idx",
    }
)


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


def _iso(value: Any, *, field: str) -> str:
    parsed = parse_date(value)
    if parsed is None:
        raise ValueError(f"{field} must be a parseable timestamp.")
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _timeframe(value: Any) -> str:
    return "".join(str(value or "").strip().lower().split())


def _degree(value: Any) -> str:
    return _normalized(value).replace(" degree", "").strip()


def _role(value: Any) -> str:
    return str(value or "").strip().replace("(", "").replace(")", "").upper()


def _family(value: Any) -> str:
    family = structure_family(value)
    if family != "combination":
        return family.replace("-", "_")
    return "triple_three" if combination_variant(value) == "triple" else "double_three"


def _source(value: Mapping[str, Any] | None) -> dict[str, str]:
    raw = dict(value or {})
    return {
        "provider": _normalized(raw.get("provider")),
        "venue": _normalized(raw.get("venue") or raw.get("exchange")),
        "symbol": _symbol(raw.get("symbol") or raw.get("resolved_symbol")),
        "market_type": _normalized(raw.get("market_type")),
        "timeframe": _timeframe(raw.get("timeframe")),
        "feed_identity": str(raw.get("feed_identity") or "").strip(),
    }


def _timestamp(value: Any) -> datetime | None:
    parsed = parse_date(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _not_after(left: Any, right: Any) -> bool:
    first = _timestamp(left)
    second = _timestamp(right)
    if first is None or second is None:
        return False
    return first.astimezone(timezone.utc) <= second.astimezone(timezone.utc)


def workflow_case_content_hash(value: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(value))
    payload.pop("content_hash", None)
    return _hash(payload)


def workflow_event_content_hash(value: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(value))
    payload.pop("content_hash", None)
    payload.pop("event_id", None)
    return _hash(payload)


def review_block_content_hash(value: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(value))
    payload.pop("review_hash", None)
    return _hash(payload)


def source_pair_identity(
    *,
    endpoint_snapshot_id: str,
    endpoint_snapshot_hash: str,
    fingerprint_id: int,
    fingerprint_hash: str,
) -> dict[str, Any]:
    payload = {
        "identity_version": SOURCE_PAIR_IDENTITY_VERSION,
        "source_endpoint_snapshot_id": str(endpoint_snapshot_id),
        "source_endpoint_snapshot_hash": str(endpoint_snapshot_hash),
        "source_fingerprint_id": int(fingerprint_id),
        "source_fingerprint_hash": str(fingerprint_hash),
    }
    payload["source_pair_hash"] = _hash(payload)
    return payload


def material_episode_identity(
    correction_case: Mapping[str, Any], endpoint_snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    endpoint = correction_case.get("candidate_endpoint")
    endpoint = endpoint if isinstance(endpoint, Mapping) else {}
    source = _source(
        correction_case.get("source_identity")
        if isinstance(correction_case.get("source_identity"), Mapping)
        else None
    )
    payload = {
        "identity_version": MATERIAL_EPISODE_IDENTITY_VERSION,
        "provider": source["provider"],
        "venue": source["venue"],
        "feed_identity": source["feed_identity"],
        "symbol": _symbol(correction_case.get("symbol")),
        "timeframe": _timeframe(correction_case.get("timeframe")),
        "elliott_degree": _degree(correction_case.get("elliott_degree")),
        "endpoint_timestamp": str(endpoint.get("timestamp") or ""),
        "candidate_role": _role(endpoint.get("terminal_label")),
        "parent_pattern_family": _family(
            correction_case.get("parent_pattern_family")
        ),
        "endpoint_snapshot_cutoff": str(endpoint_snapshot.get("cutoff") or ""),
    }
    payload["material_episode_hash"] = _hash(payload)
    return payload


def _check(
    checks: list[dict[str, Any]],
    check_id: str,
    passed: bool,
    message: str,
    *,
    hard: bool = True,
) -> None:
    checks.append(
        {
            "check_id": check_id,
            "status": "passed" if passed else "failed",
            "hard_exclusion": bool(hard and not passed),
            "message": message,
        }
    )


def validate_workflow_sources(
    *,
    correction_case: Mapping[str, Any],
    endpoint_snapshot: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
    stored_correction_case_hash: str,
    stored_endpoint_snapshot_hash: str,
    stored_fingerprint_hash: str,
    source_fingerprint_id: int,
) -> dict[str, Any]:
    """Validate immutable source linkage without using a reviewed outcome."""
    checks: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []

    try:
        case_hash = correction_case_content_hash(correction_case)
    except (TypeError, ValueError):
        case_hash = ""
    _check(
        checks,
        "correction_case_hash",
        bool(
            correction_case.get("content_hash") == case_hash
            and stored_correction_case_hash == case_hash
        ),
        "Correction-case row, JSON payload, and canonical hash must agree.",
    )

    try:
        snapshot_hash = snapshot_content_hash(endpoint_snapshot)
        snapshot_errors = validate_snapshot(endpoint_snapshot)
    except (KeyError, TypeError, ValueError):
        snapshot_hash = ""
        snapshot_errors = ["Snapshot payload could not be validated."]
    _check(
        checks,
        "endpoint_snapshot_hash",
        bool(
            not snapshot_errors
            and endpoint_snapshot.get("content_hash") == snapshot_hash
            and stored_endpoint_snapshot_hash == snapshot_hash
        ),
        "Endpoint-snapshot row, JSON payload, and canonical hash must agree.",
    )

    try:
        fingerprint_hash = fingerprint_content_hash(fingerprint)
    except (TypeError, ValueError):
        fingerprint_hash = ""
    _check(
        checks,
        "fingerprint_hash",
        bool(
            fingerprint.get("content_hash") == fingerprint_hash
            and stored_fingerprint_hash == fingerprint_hash
        ),
        "Fingerprint row, JSON payload, and canonical hash must agree.",
    )

    case_id = str(correction_case.get("case_id") or "")
    _check(
        checks,
        "endpoint_is_root_snapshot",
        endpoint_snapshot.get("parent_snapshot_id") is None,
        "The workflow endpoint must be the immutable root Phase 4 snapshot.",
    )
    _check(
        checks,
        "snapshot_case_linkage",
        bool(case_id and endpoint_snapshot.get("case_id") == case_id),
        "The endpoint snapshot must reference the same correction case.",
    )

    fingerprint_refs = endpoint_snapshot.get("fingerprint_references")
    fingerprint_refs = fingerprint_refs if isinstance(fingerprint_refs, list) else []
    linked = any(
        isinstance(item, Mapping)
        and item.get("content_hash") == stored_fingerprint_hash
        for item in fingerprint_refs
    )
    _check(
        checks,
        "snapshot_fingerprint_linkage",
        linked,
        "The endpoint snapshot must contain the original fingerprint content hash.",
    )

    case_endpoint = correction_case.get("candidate_endpoint")
    case_endpoint = case_endpoint if isinstance(case_endpoint, Mapping) else {}
    snapshot_endpoint = endpoint_snapshot.get("candidate_endpoint")
    snapshot_endpoint = snapshot_endpoint if isinstance(snapshot_endpoint, Mapping) else {}
    identity = fingerprint.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    structural = fingerprint.get("structural")
    structural = structural if isinstance(structural, Mapping) else {}

    symbol_values = (
        _symbol(correction_case.get("symbol")),
        _symbol(endpoint_snapshot.get("symbol")),
        _symbol(identity.get("symbol")),
    )
    _check(
        checks,
        "symbol_match",
        all(symbol_values) and len(set(symbol_values)) == 1,
        "Correction case, endpoint snapshot, and fingerprint symbols must match.",
    )
    timeframe_values = (
        _timeframe(correction_case.get("timeframe")),
        _timeframe(endpoint_snapshot.get("timeframe")),
        _timeframe(identity.get("timeframe")),
    )
    _check(
        checks,
        "timeframe_match",
        all(timeframe_values) and len(set(timeframe_values)) == 1,
        "Correction case, endpoint snapshot, and fingerprint timeframes must match.",
    )
    degree_values = (
        _degree(correction_case.get("elliott_degree")),
        _degree(endpoint_snapshot.get("elliott_degree")),
        _degree(identity.get("wave_degree")),
    )
    _check(
        checks,
        "degree_match",
        all(degree_values) and len(set(degree_values)) == 1,
        "Correction case, endpoint snapshot, and fingerprint degrees must match.",
    )
    _check(
        checks,
        "endpoint_match",
        bool(
            case_endpoint.get("timestamp")
            and case_endpoint.get("timestamp") == snapshot_endpoint.get("timestamp")
            and case_endpoint.get("timestamp") == identity.get("end_timestamp")
        ),
        "The candidate endpoint must align with the snapshot and fingerprint end pivot.",
    )
    _check(
        checks,
        "role_match",
        bool(
            _role(case_endpoint.get("terminal_label"))
            and _role(case_endpoint.get("terminal_label"))
            == _role(snapshot_endpoint.get("terminal_label"))
            == _role(identity.get("wave_label"))
        ),
        "The endpoint structural position must match across all sources.",
    )
    _check(
        checks,
        "family_match",
        bool(
            _family(correction_case.get("parent_pattern_family"))
            and _family(correction_case.get("parent_pattern_family"))
            == _family(endpoint_snapshot.get("parent_pattern_family"))
            == _family(identity.get("parent_pattern_family"))
        ),
        "The parent pattern family must match across all sources.",
    )

    case_source = _source(
        correction_case.get("source_identity")
        if isinstance(correction_case.get("source_identity"), Mapping)
        else None
    )
    snapshot_source = _source(
        endpoint_snapshot.get("source_identity")
        if isinstance(endpoint_snapshot.get("source_identity"), Mapping)
        else None
    )
    fingerprint_source = _source(
        fingerprint.get("source")
        if isinstance(fingerprint.get("source"), Mapping)
        else None
    )
    for field in (
        "provider",
        "venue",
        "symbol",
        "market_type",
        "timeframe",
        "feed_identity",
    ):
        values = (
            case_source.get(field, ""),
            snapshot_source.get(field, ""),
            fingerprint_source.get(field, ""),
        )
        _check(
            checks,
            f"source_{field}_match",
            all(values) and len(set(values)) == 1,
            f"Source {field} must match across correction case, snapshot, and fingerprint.",
        )

    fingerprint_cutoff = fingerprint.get("cutoff")
    fingerprint_cutoff = (
        fingerprint_cutoff if isinstance(fingerprint_cutoff, Mapping) else {}
    )
    _check(
        checks,
        "case_endpoint_not_after_snapshot_cutoff",
        _not_after(case_endpoint.get("timestamp"), endpoint_snapshot.get("cutoff")),
        "The endpoint timestamp must not occur after the snapshot cutoff.",
    )
    _check(
        checks,
        "fingerprint_not_after_snapshot_cutoff",
        _not_after(fingerprint_cutoff.get("timestamp"), endpoint_snapshot.get("cutoff")),
        "The Phase 3 fingerprint cutoff must not occur after the endpoint snapshot cutoff.",
    )
    _check(
        checks,
        "fingerprint_candles_not_after_cutoff",
        _not_after(
            fingerprint_cutoff.get("last_source_candle_timestamp"),
            fingerprint_cutoff.get("timestamp"),
        ),
        "The last fingerprint candle must not occur after its declared cutoff.",
    )

    _check(
        checks,
        "correction_schema_supported",
        correction_case.get("schema_version") == CORRECTION_STATE_SCHEMA_VERSION
        and correction_case.get("calculation_version")
        == CORRECTION_STATE_CALCULATION_VERSION,
        "The correction case must use the supported Phase 4 schema and calculation version.",
    )
    _check(
        checks,
        "snapshot_schema_supported",
        endpoint_snapshot.get("schema_version") == CORRECTION_STATE_SCHEMA_VERSION
        and endpoint_snapshot.get("calculation_version")
        == CORRECTION_STATE_CALCULATION_VERSION,
        "The endpoint snapshot must use the supported Phase 4 schema and calculation version.",
    )
    _check(
        checks,
        "fingerprint_schema_supported",
        fingerprint.get("feature_schema_version") == FEATURE_SCHEMA_VERSION
        and fingerprint.get("calculation_version")
        == FINGERPRINT_CALCULATION_VERSION,
        "The fingerprint must use the supported Phase 3 schema and calculation version.",
    )

    missing_optional = []
    for field, value in (
        ("endpoint_price", case_endpoint.get("price")),
        ("internal_structure_family", structural.get("internal_structure_family")),
        ("data_quality_status", (fingerprint.get("data_quality") or {}).get("status") if isinstance(fingerprint.get("data_quality"), Mapping) else None),
    ):
        if value in (None, ""):
            missing_optional.append(field)
            warnings.append(
                {
                    "code": f"missing_{field}",
                    "message": f"Optional draft field {field} is unavailable.",
                }
            )

    hard_exclusions = [
        item for item in checks if item["status"] == "failed" and item["hard_exclusion"]
    ]
    return {
        "status": "passed" if not hard_exclusions else "failed",
        "valid": not hard_exclusions,
        "checks": checks,
        "hard_exclusions": hard_exclusions,
        "warnings": warnings,
        "missing_optional_fields": missing_optional,
        "source_fingerprint_id": int(source_fingerprint_id),
        "policy": (
            "Validation uses immutable decision-time Phase 3/4 sources only. Passing "
            "validation never accepts a case."
        ),
    }


def assemble_workflow_draft(
    *,
    correction_case: Mapping[str, Any],
    endpoint_snapshot: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
    stored_correction_case_hash: str,
    stored_endpoint_snapshot_hash: str,
    stored_fingerprint_hash: str,
    source_fingerprint_id: int,
    workflow_version: int,
    supersedes_workflow_case_id: str | None = None,
    duplicate_workflow_case_ids: Iterable[str] = (),
) -> dict[str, Any]:
    validation = validate_workflow_sources(
        correction_case=correction_case,
        endpoint_snapshot=endpoint_snapshot,
        fingerprint=fingerprint,
        stored_correction_case_hash=stored_correction_case_hash,
        stored_endpoint_snapshot_hash=stored_endpoint_snapshot_hash,
        stored_fingerprint_hash=stored_fingerprint_hash,
        source_fingerprint_id=source_fingerprint_id,
    )
    pair = source_pair_identity(
        endpoint_snapshot_id=str(endpoint_snapshot.get("snapshot_id") or ""),
        endpoint_snapshot_hash=stored_endpoint_snapshot_hash,
        fingerprint_id=source_fingerprint_id,
        fingerprint_hash=stored_fingerprint_hash,
    )
    material = material_episode_identity(correction_case, endpoint_snapshot)
    episode = market_episode_identity(correction_case)
    endpoint = correction_case.get("candidate_endpoint")
    endpoint = endpoint if isinstance(endpoint, Mapping) else {}
    identity = fingerprint.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    structural = fingerprint.get("structural")
    structural = structural if isinstance(structural, Mapping) else {}
    source = _source(
        correction_case.get("source_identity")
        if isinstance(correction_case.get("source_identity"), Mapping)
        else None
    )
    role_classification = classify_structural_role(
        parent_family=correction_case.get("parent_pattern_family"),
        position=endpoint.get("terminal_label"),
        internal_family=structural.get("internal_structure_family"),
    )
    workflow_case_id = f"experience_workflow_{pair['source_pair_hash'][:32]}"
    payload: dict[str, Any] = {
        "schema_version": WORKFLOW_CASE_SCHEMA_VERSION,
        "calculation_version": WORKFLOW_CASE_CALCULATION_VERSION,
        "workflow_spec_version": WORKFLOW_SPEC_VERSION,
        "workflow_case_id": workflow_case_id,
        "market_episode_id": episode["market_episode_id"],
        "workflow_version": int(workflow_version),
        "supersedes_workflow_case_id": supersedes_workflow_case_id,
        "source_pair_identity": pair,
        "source_pair_hash": pair["source_pair_hash"],
        "material_episode_identity": material,
        "material_episode_hash": material["material_episode_hash"],
        "source_references": {
            "correction_case_id": correction_case.get("case_id"),
            "correction_case_hash": stored_correction_case_hash,
            "endpoint_snapshot_id": endpoint_snapshot.get("snapshot_id"),
            "endpoint_snapshot_hash": stored_endpoint_snapshot_hash,
            "fingerprint_id": int(source_fingerprint_id),
            "fingerprint_identity_hash": fingerprint.get("identity_hash"),
            "fingerprint_content_hash": stored_fingerprint_hash,
        },
        "identity": {
            "symbol": correction_case.get("symbol"),
            "timeframe": correction_case.get("timeframe"),
            "elliott_degree": correction_case.get("elliott_degree"),
            "endpoint_timestamp": endpoint.get("timestamp"),
            "endpoint_price": endpoint.get("price"),
            "structural_role": endpoint.get("terminal_label"),
            "role_class": role_classification.get("role_class"),
            "role_class_status": role_classification.get("status"),
            "role_class_version": ROLE_CLASS_VERSION,
            "parent_pattern_family": correction_case.get("parent_pattern_family"),
            "internal_structure_family": structural.get("internal_structure_family"),
            "feed": source,
            "fingerprint_wave_id": identity.get("wave_id"),
        },
        "cutoffs": {
            "candidate_endpoint": endpoint.get("timestamp"),
            "endpoint_snapshot": endpoint_snapshot.get("cutoff"),
            "fingerprint": (fingerprint.get("cutoff") or {}).get("timestamp")
            if isinstance(fingerprint.get("cutoff"), Mapping)
            else None,
        },
        "validation": validation,
        "missing_field_warnings": deepcopy(validation["warnings"]),
        "duplicate_detection": {
            "exact_source_pair_duplicate": False,
            "materially_identical_workflow_case_ids": sorted(
                {str(item) for item in duplicate_workflow_case_ids if str(item)}
            ),
            "policy": (
                "Duplicates are reported and never deleted, merged, or automatically accepted."
            ),
        },
        "outcome_reference": None,
        "review_state": "not_started",
        "policy": (
            "This draft records source evidence only. Structural correctness, outcome "
            "acceptance, and final pool admission require separate human events."
        ),
    }
    payload["content_hash"] = workflow_case_content_hash(payload)
    return payload


def _validate_review_fields(
    review: Mapping[str, Any], required: Sequence[str], *, review_name: str
) -> None:
    missing = [field for field in required if field not in review]
    if missing:
        raise ValueError(
            f"{review_name} is missing required fields: {', '.join(missing)}."
        )


def build_structural_review_block(
    review: Mapping[str, Any],
    *,
    decision: str,
    reviewer: str,
    reviewed_at: Any,
) -> dict[str, Any]:
    if decision not in STRUCTURAL_REVIEW_DECISIONS:
        raise ValueError(f"Unsupported structural review decision: {decision!r}.")
    if not str(reviewer).strip():
        raise ValueError("A named structural reviewer is required.")
    _validate_review_fields(
        review, STRUCTURAL_REVIEW_REQUIRED_FIELDS, review_name="Structural review"
    )
    payload = {
        "review_version": STRUCTURAL_REVIEW_VERSION,
        "reviewer_reference": str(reviewer).strip(),
        "reviewed_at": _iso(reviewed_at, field="structural review timestamp"),
        "decision": decision,
        **{field: deepcopy(review.get(field)) for field in STRUCTURAL_REVIEW_REQUIRED_FIELDS},
        "resolves_event_ids": sorted(
            {str(item) for item in review.get("resolves_event_ids", []) if str(item)}
        ),
        "review_notes": str(review.get("review_notes") or "").strip(),
        "policy": (
            "Indicators are review evidence only and cannot invalidate an otherwise "
            "structurally valid Elliott count."
        ),
    }
    if not isinstance(payload["alternative_counts"], list):
        raise ValueError("Structural review alternative_counts must be a list.")
    if not isinstance(payload["rejection_reason_codes"], list):
        raise ValueError("Structural review rejection_reason_codes must be a list.")
    if decision == "accepted":
        confirmations = (
            "structural_role_confirmation",
            "degree_confirmation",
            "endpoint_confirmation",
            "pattern_family_confirmation",
        )
        if any(str(payload[field]).strip().lower() != "confirmed" for field in confirmations):
            raise ValueError(
                "Structural acceptance requires explicit confirmation of role, degree, endpoint, and family."
            )
        if not str(payload["rule_validation_notes"]).strip():
            raise ValueError("Structural acceptance requires rule-validation notes.")
    if decision == "rejected" and not payload["rejection_reason_codes"]:
        raise ValueError("Structural rejection requires at least one reason code.")
    if decision == "structurally_ambiguous" and not (
        str(payload["ambiguity_notes"]).strip() and payload["alternative_counts"]
    ):
        raise ValueError(
            "Structural ambiguity requires ambiguity notes and at least one alternate count."
        )
    payload["review_hash"] = review_block_content_hash(payload)
    return payload


def build_outcome_review_block(
    review: Mapping[str, Any],
    *,
    decision: str,
    reviewer: str,
    reviewed_at: Any,
    resolved_hypothesis: str,
    horizon_id: str = DEFAULT_HORIZON_ID,
    horizon_version: str = DEFAULT_HORIZON_VERSION,
) -> dict[str, Any]:
    if decision not in OUTCOME_REVIEW_DECISIONS:
        raise ValueError(f"Unsupported outcome review decision: {decision!r}.")
    if not str(reviewer).strip():
        raise ValueError("A named outcome reviewer is required.")
    _validate_review_fields(
        review, OUTCOME_REVIEW_REQUIRED_FIELDS, review_name="Outcome review"
    )
    horizon = observation_horizon_specification(horizon_id, horizon_version)
    taxonomy = outcome_taxonomy()
    categories = {
        str(item.get("category"))
        for item in taxonomy.get("structural_categories", [])
        if isinstance(item, Mapping)
    }
    expected_category = STRUCTURAL_OUTCOME_BY_HYPOTHESIS.get(resolved_hypothesis)
    payload = {
        "review_version": OUTCOME_REVIEW_VERSION,
        "outcome_taxonomy_version": OUTCOME_TAXONOMY_VERSION,
        "outcome_taxonomy_hash": taxonomy["content_hash"],
        "observation_horizon_id": horizon["horizon_id"],
        "observation_horizon_version": horizon["horizon_version"],
        "observation_horizon_specification_version": horizon[
            "specification_version"
        ],
        "observation_horizon_hash": horizon["content_hash"],
        "reviewer_reference": str(reviewer).strip(),
        "reviewed_at": _iso(reviewed_at, field="outcome review timestamp"),
        "decision": decision,
        "resolved_hypothesis": resolved_hypothesis,
        "expected_taxonomy_category": expected_category,
        **{field: deepcopy(review.get(field)) for field in OUTCOME_REVIEW_REQUIRED_FIELDS},
        "resolves_event_ids": sorted(
            {str(item) for item in review.get("resolves_event_ids", []) if str(item)}
        ),
        "policy": (
            "The outcome review records what happened historically. It does not "
            "forecast the current market or affect analogue ordering."
        ),
    }
    if payload["structural_aftermath"] not in categories:
        raise ValueError("Outcome review structural_aftermath is outside the Phase 5B taxonomy.")
    if not isinstance(payload["unresolved_state"], bool):
        raise ValueError("Outcome review unresolved_state must be boolean.")
    if not isinstance(payload["rejection_reason_codes"], list):
        raise ValueError("Outcome review rejection_reason_codes must be a list.")
    if decision == "accepted":
        if str(payload["historical_endpoint_linkage"]).strip().lower() != "confirmed":
            raise ValueError("Outcome acceptance requires confirmed endpoint linkage.")
        if expected_category is None or payload["structural_aftermath"] != expected_category:
            raise ValueError(
                "Outcome acceptance must use the Phase 5B category mapped from the reviewed Phase 4 hypothesis."
            )
    if decision == "rejected" and not payload["rejection_reason_codes"]:
        raise ValueError("Outcome rejection requires at least one reason code.")
    if decision == "structurally_ambiguous" and not payload["unresolved_state"]:
        raise ValueError("An ambiguous outcome review must explicitly remain unresolved.")
    payload["review_hash"] = review_block_content_hash(payload)
    return payload


def build_workflow_event(
    *,
    workflow_case_id: str,
    sequence_number: int,
    parent_event_id: str | None,
    event_kind: str,
    from_state: str | None,
    to_state: str,
    actor_reference: str,
    recorded_at: Any,
    decision: str | None = None,
    reviewer_reference: str | None = None,
    human_confirmed: bool = False,
    source_outcome_id: str | None = None,
    source_outcome_review_id: str | None = None,
    result_experience_case_id: str | None = None,
    result_experience_review_id: str | None = None,
    event_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if event_kind not in EVENT_KINDS:
        raise ValueError(f"Unsupported workflow event kind: {event_kind!r}.")
    if from_state not in ALLOWED_TRANSITIONS or to_state not in ALLOWED_TRANSITIONS.get(
        from_state, frozenset()
    ):
        raise ValueError(
            f"Illegal workflow transition: {from_state!r} -> {to_state!r}."
        )
    if sequence_number == 1:
        if not (
            parent_event_id is None
            and from_state is None
            and event_kind == "candidate_discovery"
            and to_state == "discovered"
        ):
            raise ValueError("The first workflow event must discover the case.")
    elif not parent_event_id or from_state is None:
        raise ValueError("Every event after discovery requires a parent and from_state.")
    if not str(actor_reference).strip():
        raise ValueError("A workflow actor reference is required.")
    if event_kind in HUMAN_EVENT_KINDS and not (
        human_confirmed and str(reviewer_reference or "").strip()
    ):
        raise ValueError(f"{event_kind} requires a named human confirmation.")
    if (source_outcome_id is None) != (source_outcome_review_id is None):
        raise ValueError("Outcome and outcome-review references must be supplied together.")
    if (result_experience_case_id is None) != (result_experience_review_id is None):
        raise ValueError("Final experience case and review references must be supplied together.")
    if to_state == "accepted" and not all(
        (
            event_kind == "final_acceptance",
            decision == "accepted",
            human_confirmed,
            reviewer_reference,
            source_outcome_id,
            source_outcome_review_id,
            result_experience_case_id,
            result_experience_review_id,
        )
    ):
        raise ValueError("Final acceptance is missing mandatory human or source links.")
    payload: dict[str, Any] = {
        "event_schema_version": WORKFLOW_EVENT_SCHEMA_VERSION,
        "workflow_spec_version": WORKFLOW_SPEC_VERSION,
        "workflow_case_id": str(workflow_case_id),
        "sequence_number": int(sequence_number),
        "parent_event_id": parent_event_id,
        "event_kind": event_kind,
        "from_state": from_state,
        "to_state": to_state,
        "decision": decision,
        "actor_reference": str(actor_reference).strip(),
        "reviewer_reference": (
            str(reviewer_reference).strip() if reviewer_reference else None
        ),
        "human_confirmed": bool(human_confirmed),
        "source_outcome_id": source_outcome_id,
        "source_outcome_review_id": source_outcome_review_id,
        "result_experience_case_id": result_experience_case_id,
        "result_experience_review_id": result_experience_review_id,
        "event_data": deepcopy(dict(event_data or {})),
        "recorded_at": _iso(recorded_at, field="workflow event timestamp"),
    }
    payload["content_hash"] = workflow_event_content_hash(payload)
    payload["event_id"] = f"experience_workflow_event_{payload['content_hash'][:32]}"
    payload["content_hash"] = workflow_event_content_hash(payload)
    return payload


def validate_event_chain(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    seen_ids: set[str] = set()
    unresolved_disagreements: set[str] = set()
    for index, event in enumerate(events, start=1):
        event_id = str(event.get("event_id") or "")
        if event.get("content_hash") != workflow_event_content_hash(event):
            issues.append({"code": "invalid_event_hash", "event_id": event_id})
        if event.get("workflow_spec_version") != WORKFLOW_SPEC_VERSION or event.get(
            "event_schema_version"
        ) != WORKFLOW_EVENT_SCHEMA_VERSION:
            issues.append({"code": "unsupported_event_version", "event_id": event_id})
        if event_id in seen_ids:
            issues.append({"code": "duplicate_event_id", "event_id": event_id})
        seen_ids.add(event_id)
        if event.get("sequence_number") != index:
            issues.append(
                {
                    "code": "noncontiguous_sequence",
                    "event_id": event_id,
                    "expected": index,
                    "actual": event.get("sequence_number"),
                }
            )
        expected_parent = previous.get("event_id") if previous else None
        expected_from = previous.get("to_state") if previous else None
        if event.get("parent_event_id") != expected_parent:
            issues.append({"code": "broken_parent_chain", "event_id": event_id})
        if event.get("from_state") != expected_from:
            issues.append({"code": "broken_state_chain", "event_id": event_id})
        if event.get("to_state") not in ALLOWED_TRANSITIONS.get(
            event.get("from_state"), frozenset()
        ):
            issues.append({"code": "illegal_transition", "event_id": event_id})
        event_data = event.get("event_data")
        event_data = event_data if isinstance(event_data, Mapping) else {}
        if event.get("event_kind") == "review_disagreement" or (
            event.get("event_kind") in {"structural_review", "outcome_review"}
            and event.get("to_state")
            in {"structural_review_ambiguous", "outcome_review_ambiguous"}
        ):
            unresolved_disagreements.add(event_id)
        if event.get("event_kind") == "disagreement_resolution":
            review = event_data.get("review")
            review = review if isinstance(review, Mapping) else {}
            referenced = {str(item) for item in review.get("resolves_event_ids", [])}
            missing = referenced - unresolved_disagreements
            if not referenced or missing:
                issues.append(
                    {
                        "code": "invalid_disagreement_resolution",
                        "event_id": event_id,
                        "unresolved_references": sorted(missing),
                    }
                )
            unresolved_disagreements.difference_update(referenced)
        previous = event
    return {
        "valid": not issues,
        "current_state": events[-1].get("to_state") if events and not issues else None,
        "event_count": len(events),
        "issues": issues,
        "unresolved_disagreement_event_ids": sorted(unresolved_disagreements),
        "final_acceptance_blocked": bool(issues or unresolved_disagreements),
    }


def workflow_specification() -> dict[str, Any]:
    taxonomy = outcome_taxonomy()
    horizon = observation_horizon_specification()
    payload: dict[str, Any] = {
        "workflow_spec_version": WORKFLOW_SPEC_VERSION,
        "case_schema_version": WORKFLOW_CASE_SCHEMA_VERSION,
        "case_calculation_version": WORKFLOW_CASE_CALCULATION_VERSION,
        "event_schema_version": WORKFLOW_EVENT_SCHEMA_VERSION,
        "structural_review_version": STRUCTURAL_REVIEW_VERSION,
        "outcome_review_version": OUTCOME_REVIEW_VERSION,
        "final_acceptance_version": FINAL_ACCEPTANCE_VERSION,
        "states": list(WORKFLOW_STATES),
        "event_kinds": list(EVENT_KINDS),
        "transitions": {
            "initial": sorted(ALLOWED_TRANSITIONS[None]),
            **{
                str(state): sorted(targets)
                for state, targets in ALLOWED_TRANSITIONS.items()
                if state is not None
            },
        },
        "structural_review": {
            "decisions": sorted(STRUCTURAL_REVIEW_DECISIONS),
            "required_fields": list(STRUCTURAL_REVIEW_REQUIRED_FIELDS),
            "acceptance_rule": (
                "Role, degree, endpoint, and family must be explicitly confirmed."
            ),
        },
        "outcome_review": {
            "decisions": sorted(OUTCOME_REVIEW_DECISIONS),
            "required_fields": list(OUTCOME_REVIEW_REQUIRED_FIELDS),
            "taxonomy_version": taxonomy["taxonomy_version"],
            "taxonomy_hash": taxonomy["content_hash"],
            "horizon_id": horizon["horizon_id"],
            "horizon_version": horizon["horizon_version"],
            "horizon_hash": horizon["content_hash"],
        },
        "reviewer_policy": {
            "same_named_human_permitted_across_stages": True,
            "separate_event_required_per_stage": True,
            "majority_voting": False,
            "senior_or_adjudication_authority": False,
            "unresolved_disagreement_blocks_final_acceptance": True,
            "resolution_rule": (
                "A later named human event must reference every conflicting review event."
            ),
        },
        "acceptance_policy": {
            "only_final_state_entering_pool": "accepted",
            "explicit_human_action_required": True,
            "technical_validation_never_accepts": True,
            "outcome_valence_never_accepts_or_rejects": True,
            "legacy_case_and_review_created_atomically": True,
        },
        "excluded_capabilities": [
            "machine_learning",
            "prediction",
            "probabilities",
            "automatic_wave_resolution",
            "analogue_voting",
            "trade_decisions",
            "outcome_aware_ranking",
            "post_endpoint_candle_storage",
            "post_endpoint_indicator_measurements",
        ],
    }
    payload["content_hash"] = _hash(payload)
    return payload


def render_workflow(value: Mapping[str, Any], *, explain: bool = False) -> str:
    """Render workflow data without predictive language."""
    lines = ["# Historical Experience Review Workflow", ""]
    if "workflow_spec_version" in value and "states" in value:
        lines.extend(
            [
                f"Specification: {value.get('workflow_spec_version')}",
                f"Content hash: {value.get('content_hash')}",
                "",
                "Only the final `accepted` state enters the existing experience pool.",
                "Technical validation and historical outcome direction never accept a case.",
            ]
        )
        return "\n".join(lines) + "\n"
    case = value.get("workflow_case")
    case = case if isinstance(case, Mapping) else value
    status = value.get("status")
    status = status if isinstance(status, Mapping) else {}
    lines.extend(
        [
            f"Workflow case: {case.get('workflow_case_id', 'unavailable')}",
            f"Current state: {status.get('current_state', value.get('current_state', 'unavailable'))}",
            f"Symbol: {(case.get('identity') or {}).get('symbol') if isinstance(case.get('identity'), Mapping) else 'unavailable'}",
            f"Source validation: {(case.get('validation') or {}).get('status') if isinstance(case.get('validation'), Mapping) else 'unavailable'}",
            f"Final acceptance blocked: {status.get('final_acceptance_blocked', True)}",
        ]
    )
    if explain:
        lines.extend(
            [
                "",
                "This workflow preserves immutable source and review history. Passing checks is not acceptance.",
            ]
        )
    events = value.get("events")
    if isinstance(events, list):
        lines.extend(["", "## Event History"])
        for event in events:
            if not isinstance(event, Mapping):
                continue
            lines.append(
                f"{event.get('sequence_number')}. {event.get('event_kind')}: "
                f"{event.get('from_state')} -> {event.get('to_state')}"
            )
    return "\n".join(lines) + "\n"
