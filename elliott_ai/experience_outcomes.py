"""Reviewed historical outcomes attached after deterministic analogue retrieval.

Phase 5B validates and freezes a Phase 5A.4 retrieval result before loading any
post-endpoint record. Reviewed outcomes are contextual historical evidence only;
they cannot alter filtering, comparison, retrieval tiers, ordering, limits,
tie-breaking, selection, or the original retrieval hash.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .correction_state import (
    OUTCOME_SCHEMA_VERSION,
    correction_case_content_hash,
    outcome_content_hash,
    review_content_hash,
    validate_snapshot,
)
from .experience import (
    EXPERIENCE_SCHEMA_VERSION,
    experience_case_content_hash,
    experience_review_content_hash,
    pattern_dna_content_hash,
    validate_experience_eligibility,
)
from .experience_retrieval import (
    ANALOGUE_RETRIEVAL_CALCULATION_VERSION,
    ANALOGUE_RETRIEVAL_SCHEMA_VERSION,
    analogue_retrieval_content_hash,
)
from .fingerprints import FEATURE_SCHEMA_VERSION, fingerprint_content_hash
from .market_data import parse_date


OUTCOME_EVIDENCE_SCHEMA_VERSION = "experience-outcome-evidence-1.0.0"
OUTCOME_EVIDENCE_CALCULATION_VERSION = (
    "experience-outcome-evidence-calc-1.0.0"
)
OUTCOME_EVIDENCE_SPECIFICATION_VERSION = "experience-outcome-spec-1.0.0"
OUTCOME_TAXONOMY_VERSION = "reviewed-outcome-taxonomy-1.0.0"
OBSERVATION_HORIZON_SPECIFICATION_VERSION = (
    "experience-observation-horizon-1.0.0"
)
DEFAULT_HORIZON_ID = "reviewed-resolution-window"
DEFAULT_HORIZON_VERSION = "1.0.0"

OUTCOME_EVIDENCE_STATES = (
    "accepted",
    "accepted_with_limitations",
    "unavailable_no_outcome",
    "unavailable_unreviewed_outcome",
    "unavailable_partially_reviewed_outcome",
    "unavailable_stale_outcome",
    "unavailable_experience_not_accepted",
    "rejected_outcome",
    "invalid_hash",
    "invalid_linkage",
    "source_load_error",
)

STRUCTURAL_OUTCOME_BY_HYPOTHESIS = {
    "correction_complete": "reviewed_endpoint_completion",
    "larger_wave_a_complete": "reviewed_larger_corrective_role",
    "larger_wave_w_complete": "reviewed_larger_corrective_role",
    "triple_three_continuation": "reviewed_continuation",
    "correction_still_in_progress": "reviewed_continuation",
    "unresolved": "structurally_ambiguous",
    "new_motive_wave_1": "reviewed_new_motive_role",
    "corrective_wave_a": "reviewed_corrective_role",
    "local_wave_c_complete": "reviewed_local_terminal_role",
    "local_wave_y_complete": "reviewed_local_terminal_role",
}

REVIEWED_FAMILY_BY_HYPOTHESIS = {
    "correction_complete": "completed_correction",
    "larger_wave_a_complete": "larger_corrective_role_a",
    "larger_wave_w_complete": "larger_combination_role_w",
    "triple_three_continuation": "triple_three_continuation",
    "correction_still_in_progress": "correction_continuation",
    "unresolved": None,
    "new_motive_wave_1": "motive",
    "corrective_wave_a": "corrective_role_a",
    "local_wave_c_complete": "local_terminal_c",
    "local_wave_y_complete": "local_combination_y",
}

ENDPOINT_INTERPRETATION_BY_HYPOTHESIS = {
    "correction_complete": "held_as_reviewed",
    "larger_wave_a_complete": "reclassified_within_larger_structure",
    "larger_wave_w_complete": "reclassified_within_larger_structure",
    "triple_three_continuation": "continued_beyond_endpoint",
    "correction_still_in_progress": "continued_beyond_endpoint",
    "unresolved": "unresolved",
    "new_motive_wave_1": "held_as_reviewed",
    "corrective_wave_a": "held_as_reviewed",
    "local_wave_c_complete": "held_at_local_degree_only",
    "local_wave_y_complete": "held_at_local_degree_only",
}

ACCEPTED_REVIEW_ACTIONS = frozenset({"approved", "revised"})
ACCEPTED_REVIEW_STATUSES = frozenset({"reviewed", "approved", "resolved"})
PARTIAL_REVIEW_STATUSES = frozenset(
    {"partial", "partially_reviewed", "pending", "in_review"}
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_payload(value: Mapping[str, Any]) -> str:
    payload = deepcopy(dict(value))
    payload.pop("content_hash", None)
    return hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()


def outcome_evidence_content_hash(value: Mapping[str, Any]) -> str:
    return _hash_payload(value)


def _timestamp(value: Any, *, field: str) -> datetime:
    parsed = parse_date(value)
    if parsed is None:
        raise ValueError(f"{field} must be a parseable timestamp.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any, *, field: str) -> str:
    return _timestamp(value, field=field).isoformat(timespec="seconds")


def _elapsed(start: Any, end: Any) -> dict[str, Any] | None:
    try:
        start_time = _timestamp(start, field="observation start")
        end_time = _timestamp(end, field="observation end")
    except ValueError:
        return None
    seconds = (end_time - start_time).total_seconds()
    if seconds < 0:
        return None
    return {"clock_seconds": seconds, "clock_days": seconds / 86_400.0}


def outcome_taxonomy() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "taxonomy_version": OUTCOME_TAXONOMY_VERSION,
        "evidence_states": list(OUTCOME_EVIDENCE_STATES),
        "structural_categories": [
            {
                "category": category,
                "meaning": meaning,
            }
            for category, meaning in (
                (
                    "reviewed_endpoint_completion",
                    "The endpoint interpretation was accepted as a completed correction.",
                ),
                (
                    "reviewed_larger_corrective_role",
                    "The endpoint was reviewed as a component of a larger correction.",
                ),
                (
                    "reviewed_continuation",
                    "The correction was reviewed as continuing beyond the endpoint.",
                ),
                (
                    "reviewed_new_motive_role",
                    "The reviewed role was a new motive Wave 1.",
                ),
                (
                    "reviewed_corrective_role",
                    "The reviewed role was corrective Wave A.",
                ),
                (
                    "reviewed_local_terminal_role",
                    "A local C or Y terminal role was accepted without asserting parent completion.",
                ),
                (
                    "structurally_ambiguous",
                    "Human review did not resolve one structural interpretation.",
                ),
            )
        ],
        "hypothesis_mapping": deepcopy(STRUCTURAL_OUTCOME_BY_HYPOTHESIS),
        "directional_aftermath_categories": [
            "continued_in_reviewed_direction",
            "reversed_from_endpoint",
            "ranged",
            "expanded_volatility",
            "contracted_volatility",
            "structurally_unresolved",
        ],
        "directional_policy": (
            "No directional category is inferred unless an immutable source explicitly "
            "states direction. Current records therefore use structurally_unresolved."
        ),
        "label_policy": (
            "Complex outcomes are not reduced to bullish or bearish labels."
        ),
    }
    payload["content_hash"] = _hash_payload(payload)
    return payload


def observation_horizon_specification(
    horizon_id: str = DEFAULT_HORIZON_ID,
    horizon_version: str = DEFAULT_HORIZON_VERSION,
) -> dict[str, Any]:
    if horizon_id != DEFAULT_HORIZON_ID:
        raise ValueError(
            f"Unsupported observation horizon {horizon_id!r}; supported: "
            f"{DEFAULT_HORIZON_ID}."
        )
    if horizon_version != DEFAULT_HORIZON_VERSION:
        raise ValueError(
            f"Unsupported observation horizon version {horizon_version!r}; "
            f"supported: {DEFAULT_HORIZON_VERSION}."
        )
    payload: dict[str, Any] = {
        "specification_version": OBSERVATION_HORIZON_SPECIFICATION_VERSION,
        "horizon_id": DEFAULT_HORIZON_ID,
        "horizon_version": DEFAULT_HORIZON_VERSION,
        "unit": "event_based_utc_time",
        "value": "accepted_reviewed_resolution",
        "start_rule": "Historical endpoint DNA cutoff in UTC.",
        "end_rule": "Accepted Phase 4 outcome resolution cutoff in UTC.",
        "maximum_available_rule": (
            "Latest immutable Phase 4 confirmation-DNA cutoff; outcome review time "
            "does not substitute for missing market observation coverage."
        ),
        "incomplete_data_behavior": (
            "Mark incomplete and retain explicit unavailable measurements."
        ),
        "censoring_behavior": (
            "Right-censored when confirmation lineage ends before the accepted "
            "resolution cutoff."
        ),
        "permitted_timeframes": "all timeframes supported by the stored experience schema",
        "comparison_policy": (
            "Outcome measurements from another horizon must not be compared without "
            "an explicit compatible horizon version."
        ),
        "fixed_bar_horizons": {
            "supported": False,
            "reason": "No immutable post-endpoint candle series is stored with experience cases.",
        },
    }
    payload["content_hash"] = _hash_payload(payload)
    return payload


def outcome_evidence_specification() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "specification_version": OUTCOME_EVIDENCE_SPECIFICATION_VERSION,
        "schema_version": OUTCOME_EVIDENCE_SCHEMA_VERSION,
        "calculation_version": OUTCOME_EVIDENCE_CALCULATION_VERSION,
        "source_requirements": [
            "The historical experience case is active and human-accepted.",
            "The exact Phase 5A.4-selected experience case is used.",
            "Experience, DNA, fingerprint, snapshot, outcome, and review hashes validate.",
            "Outcome and review IDs match the experience source references.",
            "The outcome is the current reviewed revision for the correction case.",
            "Symbol, timeframe, degree, role, feed, provider, and endpoint align.",
            "The resolution cutoff is strictly after the historical endpoint cutoff.",
        ],
        "record_sections": [
            "identity_and_linkage",
            "review_state",
            "observation_window",
            "structural_outcome",
            "price_behavior",
            "momentum_and_volume_aftermath",
            "quality_and_limitations",
            "provenance",
            "source_hashes",
            "issues",
        ],
        "deterministic_formulas": {
            "elapsed_clock_seconds": "UTC(end) - UTC(start)",
            "elapsed_clock_days": "elapsed_clock_seconds / 86400",
            "first_recorded_event": (
                "Minimum observed_at among explicit confirmation-DNA events with status observed."
            ),
        },
        "unsupported_measurements": {
            "price": [
                "maximum_favorable_excursion",
                "maximum_adverse_excursion",
                "signed_movement",
                "absolute_movement",
                "retracement_from_endpoint",
                "extension_from_endpoint",
                "drawdown",
                "time_to_local_extreme",
            ],
            "reason": (
                "The existing immutable experience record has no complete post-endpoint "
                "candle series or terminal outcome price."
            ),
        },
        "retrieval_isolation": (
            "Validate and freeze Phase 5A.4 before invoking any outcome source loader."
        ),
        "aggregation": {
            "implemented": False,
            "reason": "Case-by-case contextual evidence is the Phase 5B default boundary.",
        },
        "interpretation_boundary": (
            "Historical reviewed outcomes are contextual evidence, not a forecast or "
            "current Elliott decision."
        ),
    }
    payload["content_hash"] = _hash_payload(payload)
    return payload


def outcome_specification_manifest() -> dict[str, Any]:
    evidence = outcome_evidence_specification()
    taxonomy = outcome_taxonomy()
    horizon = observation_horizon_specification()
    payload: dict[str, Any] = {
        "outcome_evidence_specification": evidence,
        "outcome_taxonomy": taxonomy,
        "observation_horizon_specification": horizon,
        "hash_generation": {
            "algorithm": "sha256",
            "serialization": "ASCII canonical JSON with sorted keys and compact separators",
            "self_field_excluded": "content_hash",
            "timestamps_added": False,
        },
    }
    payload["content_hash"] = _hash_payload(payload)
    return payload


def _issue(
    code: str,
    category: str,
    reason: str,
    location: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "category": category,
        "reason": reason,
        "location": location,
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _dna_payload(source: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    row = _mapping(source.get(key))
    dna = row.get("dna")
    return dna if isinstance(dna, Mapping) else row


def _source_identity(dna: Mapping[str, Any]) -> Mapping[str, Any]:
    groups = _mapping(dna.get("groups"))
    source_group = _mapping(groups.get("source_identity"))
    values = _mapping(source_group.get("values"))
    nested = values.get("source")
    return nested if isinstance(nested, Mapping) else values


def _case_identity_fields(source: Mapping[str, Any]) -> dict[str, Any]:
    record = _mapping(source.get("experience_case_record"))
    case = _mapping(record.get("experience_case"))
    return {
        "experience_case_id": case.get("experience_case_id")
        or record.get("experience_case_id"),
        "market_episode_id": case.get("market_episode_id")
        or record.get("market_episode_id"),
        "case_version": record.get("case_version"),
        "symbol": case.get("symbol"),
        "timeframe": case.get("timeframe"),
        "elliott_degree": case.get("elliott_degree"),
        "candidate_role": case.get("candidate_role"),
        "parent_pattern_family": case.get("parent_pattern_family"),
        "endpoint_timestamp": case.get("endpoint_timestamp"),
    }


def _validate_retrieval(retrieval: Mapping[str, Any]) -> dict[str, Any]:
    if retrieval.get("schema_version") != ANALOGUE_RETRIEVAL_SCHEMA_VERSION:
        raise ValueError("Unsupported Phase 5A.4 retrieval schema version.")
    if retrieval.get("calculation_version") != ANALOGUE_RETRIEVAL_CALCULATION_VERSION:
        raise ValueError("Unsupported Phase 5A.4 retrieval calculation version.")
    if retrieval.get("content_hash") != analogue_retrieval_content_hash(retrieval):
        raise ValueError("Phase 5A.4 retrieval content hash is invalid.")
    ordered = retrieval.get("ordered_analogue_references") or []
    if not isinstance(ordered, Sequence) or isinstance(ordered, (str, bytes)):
        raise ValueError("Phase 5A.4 ordered analogue references are malformed.")
    identities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(ordered, start=1):
        if not isinstance(item, Mapping):
            raise ValueError("A Phase 5A.4 analogue reference is malformed.")
        case_id = str(
            _mapping(item.get("experience_case_reference")).get(
                "experience_case_id"
            )
            or ""
        )
        if not case_id or case_id in seen:
            raise ValueError("Retrieved experience case IDs must be present and unique.")
        if item.get("presentation_order") != index:
            raise ValueError("Phase 5A.4 presentation order is not sequential.")
        tier = _mapping(item.get("retrieval_tier")).get("tier")
        if tier not in {1, 2, 3}:
            raise ValueError("A retrieved analogue has no valid retrieval tier.")
        seen.add(case_id)
        identities.append(
            {
                "experience_case_id": case_id,
                "presentation_order": index,
                "retrieval_tier": tier,
                "ordering_keys": deepcopy(
                    list(item.get("deterministic_ordering_keys") or [])
                ),
            }
        )
    frozen: dict[str, Any] = {
        "retrieval_schema_version": retrieval.get("schema_version"),
        "retrieval_calculation_version": retrieval.get("calculation_version"),
        "retrieval_content_hash": retrieval.get("content_hash"),
        "current_endpoint_identifier": deepcopy(
            dict(_mapping(retrieval.get("current_endpoint_identifier")))
        ),
        "retrieval_policy": deepcopy(dict(_mapping(retrieval.get("retrieval_policy")))),
        "requested_result_limit": retrieval.get("requested_result_limit"),
        "maximum_comparison_level": retrieval.get("maximum_comparison_level"),
        "selected_analogues": identities,
    }
    frozen["content_hash"] = _hash_payload(frozen)
    return frozen


def _validate_dna_row(
    source: Mapping[str, Any], key: str, kind: str, issues: list[dict[str, Any]]
) -> Mapping[str, Any]:
    row = _mapping(source.get(key))
    dna = _dna_payload(source, key)
    if not dna:
        issues.append(
            _issue(
                f"missing_{kind}_dna",
                "unavailable",
                f"The {kind} Pattern DNA record is unavailable.",
                key,
            )
        )
        return {}
    if dna.get("dna_kind") != kind:
        issues.append(
            _issue(
                f"{kind}_dna_kind_mismatch",
                "linkage",
                "Pattern DNA kind does not match its storage role.",
                f"{key}.dna_kind",
            )
        )
    canonical_hash = pattern_dna_content_hash(dna)
    if dna.get("content_hash") != canonical_hash or (
        row.get("content_hash") is not None
        and row.get("content_hash") != dna.get("content_hash")
    ):
        issues.append(
            _issue(
                f"{kind}_dna_hash_failure",
                "hash",
                "Pattern DNA row or canonical payload hash failed validation.",
                key,
            )
        )
    return dna


def _match(
    issues: list[dict[str, Any]],
    *,
    code: str,
    values: Sequence[Any],
    location: str,
) -> None:
    normalized = {
        str(value).strip().lower().replace("_", "-")
        for value in values
        if value not in (None, "")
    }
    if len(normalized) > 1:
        issues.append(
            _issue(
                code,
                "linkage",
                f"Linked source values disagree: {sorted(normalized)!r}.",
                location,
            )
        )


def _validate_source(
    retrieval_analogue: Mapping[str, Any],
    source: Mapping[str, Any] | None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    if not isinstance(source, Mapping):
        return {
            "state": "unavailable_no_outcome",
            "issues": [
                _issue(
                    "outcome_source_unavailable",
                    "unavailable",
                    "No outcome source package exists for the retrieved experience case.",
                    "outcome_source",
                )
            ],
            "components": {},
        }
    if source.get("load_status") not in {None, "loaded"}:
        return {
            "state": "source_load_error",
            "issues": [
                _issue(
                    str(source.get("load_status")),
                    "unavailable",
                    str(source.get("reason") or "Outcome source loading failed."),
                    "outcome_source",
                )
            ],
            "components": {},
        }

    record = _mapping(source.get("experience_case_record"))
    case = _mapping(record.get("experience_case"))
    status = _mapping(source.get("effective_status"))
    outcome = _mapping(source.get("outcome"))
    outcome_review = _mapping(source.get("outcome_review"))
    correction_case = _mapping(source.get("correction_case"))
    endpoint_snapshot = _mapping(source.get("endpoint_snapshot"))
    fingerprint_record = _mapping(source.get("fingerprint_record"))
    fingerprint = _mapping(fingerprint_record.get("fingerprint"))
    endpoint_dna = _validate_dna_row(source, "endpoint_dna_row", "endpoint", issues)
    confirmation_dna = _validate_dna_row(
        source, "confirmation_dna_row", "confirmation", issues
    )
    outcome_dna = _validate_dna_row(
        source, "resolved_outcome_dna_row", "resolved_outcome", issues
    )

    retrieved_case_id = str(
        _mapping(retrieval_analogue.get("experience_case_reference")).get(
            "experience_case_id"
        )
        or ""
    )
    source_case_id = str(
        case.get("experience_case_id") or record.get("experience_case_id") or ""
    )
    if not record or not case:
        issues.append(
            _issue(
                "experience_case_missing",
                "unavailable",
                "The retrieved experience case record is unavailable.",
                "experience_case_record",
            )
        )
    elif (
        case.get("schema_version") != EXPERIENCE_SCHEMA_VERSION
        or case.get("content_hash") != experience_case_content_hash(case)
        or record.get("content_hash") != case.get("content_hash")
    ):
        issues.append(
            _issue(
                "experience_case_hash_failure",
                "hash",
                "Experience case schema, row hash, or canonical payload hash failed.",
                "experience_case_record",
            )
        )
    if retrieved_case_id != source_case_id:
        issues.append(
            _issue(
                "experience_case_linkage_mismatch",
                "linkage",
                "The loaded outcome belongs to a different experience case.",
                "experience_case_id",
            )
        )
    if status.get("state") != "accepted" or status.get(
        "accepted_pool_eligible"
    ) is not True:
        issues.append(
            _issue(
                "experience_case_not_accepted",
                "not_accepted",
                "Only an active human-accepted experience case may provide outcome evidence.",
                "effective_status",
            )
        )

    experience_reviews = [
        item
        for item in source.get("experience_review_history") or []
        if isinstance(item, Mapping)
    ]
    for review in experience_reviews:
        if review.get("content_hash") != experience_review_content_hash(review):
            issues.append(
                _issue(
                    "experience_review_hash_failure",
                    "hash",
                    "An experience-review hash failed validation.",
                    "experience_review_history",
                )
            )
    accepting_reviews = [
        item
        for item in experience_reviews
        if str(item.get("effective_action") or item.get("action")) == "accept"
    ]
    if not accepting_reviews or accepting_reviews[-1].get("human_confirmed") is not True:
        issues.append(
            _issue(
                "experience_acceptance_not_human_confirmed",
                "not_accepted",
                "Experience acceptance requires an explicit named human confirmation.",
                "experience_review_history",
            )
        )

    if not outcome:
        issues.append(
            _issue(
                "reviewed_outcome_missing",
                "no_outcome",
                "The experience case has no linked outcome payload.",
                "outcome",
            )
        )
    elif (
        outcome.get("schema_version") != OUTCOME_SCHEMA_VERSION
        or outcome.get("content_hash") != outcome_content_hash(outcome)
    ):
        issues.append(
            _issue(
                "outcome_hash_failure",
                "hash",
                "The reviewed outcome schema or canonical hash failed validation.",
                "outcome",
            )
        )
    if not outcome_review:
        issues.append(
            _issue(
                "outcome_unreviewed",
                "unreviewed",
                "The outcome has no explicit review record.",
                "outcome_review",
            )
        )
    elif outcome_review.get("content_hash") != review_content_hash(outcome_review):
        issues.append(
            _issue(
                "outcome_review_hash_failure",
                "hash",
                "The outcome-review canonical hash failed validation.",
                "outcome_review",
            )
        )
    if outcome_review and str(outcome_review.get("review_status") or "") in (
        PARTIAL_REVIEW_STATUSES
    ):
        issues.append(
            _issue(
                "outcome_review_partial",
                "partial_review",
                "The linked outcome review is incomplete and cannot be accepted evidence.",
                "outcome_review",
            )
        )
    elif outcome_review and (
        outcome_review.get("action") not in ACCEPTED_REVIEW_ACTIONS
        or outcome_review.get("review_status") not in ACCEPTED_REVIEW_STATUSES
    ):
        issues.append(
            _issue(
                "outcome_review_rejected",
                "rejected",
                "The linked outcome review is rejected or is not in an accepted review state.",
                "outcome_review",
            )
        )
    if source.get("outcome_is_current") is not True:
        issues.append(
            _issue(
                "outcome_revision_stale",
                "stale",
                "The linked outcome is not the current reviewed correction outcome.",
                "outcome_is_current",
            )
        )
    if source.get("outcome_review_is_latest") is not True:
        issues.append(
            _issue(
                "outcome_review_stale",
                "stale",
                "The linked review is not the latest review for this outcome revision.",
                "outcome_review_is_latest",
            )
        )

    if correction_case and correction_case.get("content_hash") != correction_case_content_hash(
        correction_case
    ):
        issues.append(
            _issue(
                "correction_case_hash_failure",
                "hash",
                "The correction-case canonical hash failed validation.",
                "correction_case",
            )
        )
    snapshot_errors = validate_snapshot(endpoint_snapshot) if endpoint_snapshot else [
        "missing"
    ]
    if snapshot_errors:
        issues.append(
            _issue(
                "endpoint_snapshot_invalid",
                "hash" if endpoint_snapshot else "unavailable",
                "The immutable endpoint snapshot is missing or invalid: "
                + " ".join(snapshot_errors),
                "endpoint_snapshot",
            )
        )
    if fingerprint:
        if (
            fingerprint.get("feature_schema_version") != FEATURE_SCHEMA_VERSION
            or fingerprint.get("content_hash") != fingerprint_content_hash(fingerprint)
            or fingerprint_record.get("content_hash") != fingerprint.get("content_hash")
        ):
            issues.append(
                _issue(
                    "fingerprint_hash_failure",
                    "hash",
                    "The Phase 3 fingerprint schema, row hash, or canonical hash failed.",
                    "fingerprint_record",
                )
            )
    else:
        issues.append(
            _issue(
                "fingerprint_missing",
                "unavailable",
                "The linked Phase 3 fingerprint is unavailable.",
                "fingerprint_record",
            )
        )

    source_refs = _mapping(case.get("source_references"))
    storage_links = {
        "endpoint_snapshot_id": record.get("source_endpoint_snapshot_id"),
        "fingerprint_id": record.get("source_fingerprint_id"),
        "outcome_id": record.get("source_outcome_id"),
        "outcome_review_id": record.get("source_outcome_review_id"),
    }
    expected_links = {
        "endpoint_snapshot_id": endpoint_snapshot.get("snapshot_id"),
        "fingerprint_id": fingerprint_record.get("id"),
        "outcome_id": outcome.get("outcome_id"),
        "outcome_review_id": outcome_review.get("review_id"),
    }
    for name in storage_links:
        if storage_links[name] != expected_links[name]:
            issues.append(
                _issue(
                    f"{name}_storage_link_mismatch",
                    "linkage",
                    "Experience storage does not reference the supplied immutable source.",
                    f"experience_case_record.{name}",
                )
            )
    payload_links = {
        "endpoint_snapshot_id": source_refs.get("endpoint_snapshot_id"),
        "endpoint_snapshot_hash": source_refs.get("endpoint_snapshot_hash"),
        "fingerprint_content_hash": source_refs.get("fingerprint_content_hash"),
        "outcome_id": source_refs.get("outcome_id"),
        "outcome_content_hash": source_refs.get("outcome_content_hash"),
        "outcome_review_id": source_refs.get("outcome_review_id"),
        "outcome_review_hash": source_refs.get("outcome_review_hash"),
    }
    actual_links = {
        "endpoint_snapshot_id": endpoint_snapshot.get("snapshot_id"),
        "endpoint_snapshot_hash": endpoint_snapshot.get("content_hash"),
        "fingerprint_content_hash": fingerprint.get("content_hash"),
        "outcome_id": outcome.get("outcome_id"),
        "outcome_content_hash": outcome.get("content_hash"),
        "outcome_review_id": outcome_review.get("review_id"),
        "outcome_review_hash": outcome_review.get("content_hash"),
    }
    for name in payload_links:
        if payload_links[name] != actual_links[name]:
            issues.append(
                _issue(
                    f"{name}_payload_link_mismatch",
                    "linkage",
                    "Experience payload does not reference the supplied immutable source.",
                    f"experience_case.source_references.{name}",
                )
            )

    retrieval_links = _mapping(retrieval_analogue.get("immutable_input_references"))
    retrieval_expected = {
        "historical_endpoint_dna_hash": endpoint_dna.get("content_hash"),
        "historical_phase3_fingerprint_hash": fingerprint.get("content_hash"),
        "historical_phase4_endpoint_snapshot_hash": endpoint_snapshot.get(
            "content_hash"
        ),
    }
    for name, expected in retrieval_expected.items():
        if retrieval_links.get(name) != expected:
            issues.append(
                _issue(
                    f"retrieval_{name}_mismatch",
                    "linkage",
                    "Outcome source does not match the immutable input used by retrieval.",
                    f"retrieval.{name}",
                )
            )

    if endpoint_dna and endpoint_dna.get("source_fingerprint_hash") != fingerprint.get(
        "content_hash"
    ):
        issues.append(
            _issue(
                "endpoint_dna_fingerprint_mismatch",
                "linkage",
                "Endpoint DNA references a different Phase 3 fingerprint.",
                "endpoint_dna.source_fingerprint_hash",
            )
        )
    if endpoint_dna and endpoint_dna.get(
        "source_endpoint_snapshot_id"
    ) != endpoint_snapshot.get("snapshot_id"):
        issues.append(
            _issue(
                "endpoint_dna_snapshot_mismatch",
                "linkage",
                "Endpoint DNA references a different Phase 4 endpoint snapshot.",
                "endpoint_dna.source_endpoint_snapshot_id",
            )
        )
    if outcome_dna:
        for name, actual in (
            ("source_outcome_id", outcome.get("outcome_id")),
            ("source_outcome_hash", outcome.get("content_hash")),
            ("source_outcome_review_id", outcome_review.get("review_id")),
            ("source_outcome_review_hash", outcome_review.get("content_hash")),
        ):
            if outcome_dna.get(name) != actual:
                issues.append(
                    _issue(
                        f"resolved_outcome_dna_{name}_mismatch",
                        "linkage",
                        "Resolved-outcome DNA references a different reviewed source.",
                        f"resolved_outcome_dna.{name}",
                    )
                )

    if all(
        (correction_case, endpoint_snapshot, fingerprint, outcome, outcome_review)
    ):
        try:
            eligibility = validate_experience_eligibility(
                correction_case=correction_case,
                endpoint_snapshot=endpoint_snapshot,
                fingerprint=fingerprint,
                outcome=outcome,
                outcome_review=outcome_review,
                outcome_is_current=bool(source.get("outcome_is_current")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(
                _issue(
                    "source_eligibility_validation_error",
                    "linkage",
                    str(exc),
                    "validate_experience_eligibility",
                )
            )
            eligibility = {}
        for item in [
            *eligibility.get("rejection_reasons", []),
            *eligibility.get("quarantine_reasons", []),
        ]:
            code = str(item.get("code") or "source_eligibility_failure")
            category = "hash" if "hash" in code else "linkage"
            issues.append(
                _issue(
                    code,
                    category,
                    str(item.get("message") or item.get("reason") or code),
                    "validate_experience_eligibility",
                )
            )

    identity = _mapping(fingerprint.get("identity"))
    fingerprint_source = _mapping(fingerprint.get("source"))
    endpoint_source = _source_identity(endpoint_dna)
    outcome_source = _source_identity(outcome_dna)
    _match(
        issues,
        code="symbol_mismatch",
        values=(
            case.get("symbol"),
            correction_case.get("symbol"),
            endpoint_snapshot.get("symbol"),
            identity.get("symbol"),
            fingerprint_source.get("symbol"),
            endpoint_source.get("symbol"),
            outcome_source.get("symbol"),
        ),
        location="identity.symbol",
    )
    _match(
        issues,
        code="timeframe_mismatch",
        values=(
            case.get("timeframe"),
            correction_case.get("timeframe"),
            endpoint_snapshot.get("timeframe"),
            identity.get("timeframe"),
            fingerprint_source.get("timeframe"),
            endpoint_source.get("timeframe"),
            outcome_source.get("timeframe"),
        ),
        location="identity.timeframe",
    )
    _match(
        issues,
        code="degree_mismatch",
        values=(
            case.get("elliott_degree"),
            correction_case.get("elliott_degree"),
            endpoint_snapshot.get("elliott_degree"),
            identity.get("wave_degree"),
        ),
        location="identity.elliott_degree",
    )
    _match(
        issues,
        code="role_mismatch",
        values=(
            case.get("candidate_role"),
            _mapping(correction_case.get("candidate_endpoint")).get(
                "terminal_label"
            ),
            _mapping(endpoint_snapshot.get("candidate_endpoint")).get(
                "terminal_label"
            ),
            identity.get("wave_label"),
        ),
        location="identity.endpoint_role",
    )
    _match(
        issues,
        code="feed_mismatch",
        values=(
            fingerprint_source.get("feed_identity"),
            endpoint_source.get("feed_identity"),
            outcome_source.get("feed_identity"),
            _mapping(correction_case.get("source_identity")).get("feed_identity"),
            _mapping(endpoint_snapshot.get("source_identity")).get("feed_identity"),
        ),
        location="provenance.feed_identity",
    )
    _match(
        issues,
        code="provenance_mismatch",
        values=(
            fingerprint_source.get("provider"),
            endpoint_source.get("provider"),
            outcome_source.get("provider"),
            _mapping(correction_case.get("source_identity")).get("provider"),
            _mapping(endpoint_snapshot.get("source_identity")).get("provider"),
        ),
        location="provenance.provider",
    )

    endpoint_cutoff = endpoint_dna.get("cutoff") or case.get("endpoint_timestamp")
    resolution_cutoff = outcome.get("resolution_cutoff")
    if endpoint_cutoff and resolution_cutoff:
        try:
            if _timestamp(resolution_cutoff, field="resolution cutoff") <= _timestamp(
                endpoint_cutoff, field="endpoint cutoff"
            ):
                issues.append(
                    _issue(
                        "outcome_not_after_endpoint",
                        "linkage",
                        "The reviewed outcome cutoff must be strictly after the endpoint cutoff.",
                        "outcome.resolution_cutoff",
                    )
                )
        except ValueError as exc:
            issues.append(
                _issue(
                    "outcome_timestamp_invalid",
                    "linkage",
                    str(exc),
                    "outcome.resolution_cutoff",
                )
            )

    reviewer_names = {
        str(value).strip()
        for value in (
            outcome.get("reviewer"),
            outcome_review.get("reviewer"),
        )
        if str(value or "").strip()
    }
    reviewer_disagreement = len(reviewer_names) > 1
    if reviewer_disagreement:
        issues.append(
            _issue(
                "reviewer_disagreement",
                "limitation",
                "Outcome author and outcome-review reviewer identities differ.",
                "review_state.reviewers",
            )
        )

    categories = {str(item.get("category")) for item in issues}
    if "no_outcome" in categories:
        state = "unavailable_no_outcome"
    elif "unreviewed" in categories:
        state = "unavailable_unreviewed_outcome"
    elif "partial_review" in categories:
        state = "unavailable_partially_reviewed_outcome"
    elif "rejected" in categories:
        state = "rejected_outcome"
    elif "stale" in categories:
        state = "unavailable_stale_outcome"
    elif "not_accepted" in categories:
        state = "unavailable_experience_not_accepted"
    elif "hash" in categories:
        state = "invalid_hash"
    elif "linkage" in categories:
        state = "invalid_linkage"
    elif "unavailable" in categories:
        state = "accepted_with_limitations"
    elif "limitation" in categories:
        state = "accepted_with_limitations"
    else:
        state = "accepted"
    return {
        "state": state,
        "issues": issues,
        "components": {
            "record": record,
            "case": case,
            "status": status,
            "experience_reviews": experience_reviews,
            "outcome": outcome,
            "outcome_review": outcome_review,
            "correction_case": correction_case,
            "endpoint_snapshot": endpoint_snapshot,
            "fingerprint_record": fingerprint_record,
            "fingerprint": fingerprint,
            "endpoint_dna": endpoint_dna,
            "confirmation_dna": confirmation_dna,
            "outcome_dna": outcome_dna,
            "reviewer_disagreement": reviewer_disagreement,
        },
    }


def _unavailable_metric(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "value": None, "reason": reason}


def _confirmation_events(confirmation_dna: Mapping[str, Any]) -> Mapping[str, Any]:
    groups = _mapping(confirmation_dna.get("groups"))
    group = _mapping(groups.get("confirmation_events"))
    values = _mapping(group.get("values"))
    return values


def _first_recorded_event(
    confirmation_dna: Mapping[str, Any], endpoint_cutoff: Any
) -> dict[str, Any]:
    candidates: list[tuple[datetime, str, Mapping[str, Any]]] = []
    for name, raw in _confirmation_events(confirmation_dna).items():
        event = _mapping(raw)
        if event.get("status") != "observed" or not event.get("observed_at"):
            continue
        try:
            observed = _timestamp(event["observed_at"], field="event observed_at")
            start = _timestamp(endpoint_cutoff, field="endpoint cutoff")
        except ValueError:
            continue
        if observed >= start:
            candidates.append((observed, str(name), event))
    if not candidates:
        return _unavailable_metric(
            "No explicit observed confirmation event has a valid post-endpoint timestamp."
        )
    observed, name, event = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
    return {
        "status": "available",
        "event_name": name,
        "observed_at": observed.isoformat(timespec="seconds"),
        "elapsed_from_endpoint": _elapsed(endpoint_cutoff, observed),
        "source_snapshot_id": event.get("source_snapshot_id"),
        "source_snapshot_hash": event.get("source_snapshot_hash"),
    }


def _observation_window(
    endpoint_cutoff: Any,
    resolution_cutoff: Any,
    confirmation_dna: Mapping[str, Any],
    horizon: Mapping[str, Any],
) -> dict[str, Any]:
    confirmation_cutoff = confirmation_dna.get("cutoff")
    result = {
        "horizon_id": horizon["horizon_id"],
        "horizon_version": horizon["horizon_version"],
        "horizon_specification_version": horizon["specification_version"],
        "horizon_specification_hash": horizon["content_hash"],
        "endpoint_cutoff": endpoint_cutoff,
        "observation_start": endpoint_cutoff,
        "observation_end": resolution_cutoff,
        "maximum_available_observation_timestamp": confirmation_cutoff,
        "horizon_type": "event_based",
        "elapsed": _elapsed(endpoint_cutoff, resolution_cutoff),
        "completeness_state": "unavailable",
        "censoring_state": "unknown",
        "reason": None,
    }
    if not endpoint_cutoff or not resolution_cutoff:
        result["reason"] = "Endpoint or accepted resolution cutoff is unavailable."
        return result
    try:
        start = _timestamp(endpoint_cutoff, field="endpoint cutoff")
        end = _timestamp(resolution_cutoff, field="resolution cutoff")
    except ValueError as exc:
        result["reason"] = str(exc)
        return result
    if end <= start:
        result["completeness_state"] = "invalid"
        result["censoring_state"] = "unknown"
        result["reason"] = "Resolution cutoff is not strictly after the endpoint cutoff."
        return result
    if not confirmation_cutoff:
        result["completeness_state"] = "incomplete"
        result["censoring_state"] = "right_censored"
        result["reason"] = "No immutable confirmation-lineage cutoff is available."
        return result
    try:
        maximum = _timestamp(
            confirmation_cutoff, field="maximum observation timestamp"
        )
    except ValueError as exc:
        result["completeness_state"] = "incomplete"
        result["censoring_state"] = "right_censored"
        result["reason"] = str(exc)
        return result
    if maximum < end:
        result["completeness_state"] = "incomplete"
        result["censoring_state"] = "right_censored"
        result["reason"] = (
            "Immutable confirmation coverage ends before the reviewed resolution cutoff."
        )
    else:
        result["completeness_state"] = "complete"
        result["censoring_state"] = "not_censored"
        result["reason"] = "Confirmation lineage covers the reviewed resolution window."
    return result


def _explicit_aftermath_group(
    confirmation_dna: Mapping[str, Any], group_name: str
) -> dict[str, Any]:
    group = _mapping(_mapping(confirmation_dna.get("groups")).get(group_name))
    status = str(group.get("status") or "unavailable")
    if status not in {"available", "partial"}:
        return {
            "status": "unavailable",
            "values": None,
            "reason": group.get("unavailable_reason")
            or f"No immutable post-endpoint {group_name} evidence is stored.",
            "source_references": deepcopy(list(group.get("source_references") or [])),
        }
    return {
        "status": status,
        "values": deepcopy(group.get("values")),
        "reason": group.get("incomparable_reason"),
        "source_references": deepcopy(list(group.get("source_references") or [])),
    }


def _source_hashes(components: Mapping[str, Any]) -> dict[str, Any]:
    record = _mapping(components.get("record"))
    case = _mapping(components.get("case"))
    endpoint_dna = _mapping(components.get("endpoint_dna"))
    confirmation_dna = _mapping(components.get("confirmation_dna"))
    outcome_dna = _mapping(components.get("outcome_dna"))
    fingerprint = _mapping(components.get("fingerprint"))
    snapshot = _mapping(components.get("endpoint_snapshot"))
    outcome = _mapping(components.get("outcome"))
    review = _mapping(components.get("outcome_review"))
    return {
        "experience_case": case.get("content_hash") or record.get("content_hash"),
        "endpoint_dna": endpoint_dna.get("content_hash"),
        "confirmation_dna": confirmation_dna.get("content_hash"),
        "resolved_outcome_dna": outcome_dna.get("content_hash"),
        "phase3_fingerprint": fingerprint.get("content_hash"),
        "phase4_endpoint_snapshot": snapshot.get("content_hash"),
        "phase4_outcome": outcome.get("content_hash"),
        "phase4_outcome_review": review.get("content_hash"),
    }


def build_outcome_evidence_record(
    retrieval_analogue: Mapping[str, Any],
    retrieval_hash: str,
    source: Mapping[str, Any] | None,
    *,
    horizon_id: str = DEFAULT_HORIZON_ID,
    horizon_version: str = DEFAULT_HORIZON_VERSION,
    include_outcome_details: bool = False,
) -> dict[str, Any]:
    horizon = observation_horizon_specification(horizon_id, horizon_version)
    taxonomy = outcome_taxonomy()
    validation = _validate_source(retrieval_analogue, source)
    components = validation["components"]
    case_identity = _case_identity_fields(source or {})
    outcome = _mapping(components.get("outcome"))
    review = _mapping(components.get("outcome_review"))
    endpoint_dna = _mapping(components.get("endpoint_dna"))
    confirmation_dna = _mapping(components.get("confirmation_dna"))
    fingerprint = _mapping(components.get("fingerprint"))
    endpoint_snapshot = _mapping(components.get("endpoint_snapshot"))
    experience_reviews = list(components.get("experience_reviews") or [])
    resolved_hypothesis = str(outcome.get("resolved_hypothesis") or "")
    structural_category = STRUCTURAL_OUTCOME_BY_HYPOTHESIS.get(
        resolved_hypothesis, "structurally_ambiguous"
    )
    endpoint_cutoff = endpoint_dna.get("cutoff") or case_identity.get(
        "endpoint_timestamp"
    )
    resolution_cutoff = outcome.get("resolution_cutoff")
    window = _observation_window(
        endpoint_cutoff, resolution_cutoff, confirmation_dna, horizon
    )
    issues = deepcopy(validation["issues"])
    if window["completeness_state"] == "incomplete":
        issues.append(
            _issue(
                "observation_horizon_incomplete",
                "limitation",
                str(window.get("reason")),
                "observation_window",
            )
        )
    state = validation["state"]
    if state == "accepted" and issues:
        state = "accepted_with_limitations"

    retrieval_reference = _mapping(
        retrieval_analogue.get("phase5a3_comparison_reference")
    )
    tier = _mapping(retrieval_analogue.get("retrieval_tier"))
    accepting_review = next(
        (
            item
            for item in reversed(experience_reviews)
            if str(item.get("effective_action") or item.get("action")) == "accept"
        ),
        None,
    )
    no_price_reason = (
        "No immutable post-endpoint candle series or terminal outcome price is stored."
    )
    first_event = _first_recorded_event(confirmation_dna, endpoint_cutoff)
    events = _confirmation_events(confirmation_dna)
    channel_break = deepcopy(events.get("channel_break")) if events else None
    record: dict[str, Any] = {
        "schema_version": OUTCOME_EVIDENCE_SCHEMA_VERSION,
        "calculation_version": OUTCOME_EVIDENCE_CALCULATION_VERSION,
        "outcome_specification_version": OUTCOME_EVIDENCE_SPECIFICATION_VERSION,
        "outcome_taxonomy_version": OUTCOME_TAXONOMY_VERSION,
        "outcome_taxonomy_hash": taxonomy["content_hash"],
        "evidence_state": state,
        "identity_and_linkage": {
            "outcome_id": outcome.get("outcome_id"),
            "experience_case_id": case_identity.get("experience_case_id")
            or _mapping(retrieval_analogue.get("experience_case_reference")).get(
                "experience_case_id"
            ),
            "historical_endpoint_id": endpoint_snapshot.get("snapshot_id"),
            "phase3_fingerprint_reference": {
                "identity_hash": fingerprint.get("identity_hash"),
                "content_hash": fingerprint.get("content_hash"),
            },
            "phase4_endpoint_snapshot_reference": {
                "snapshot_id": endpoint_snapshot.get("snapshot_id"),
                "content_hash": endpoint_snapshot.get("content_hash"),
            },
            "retrieval_result_hash": retrieval_hash,
            "phase5a3_comparison_id": retrieval_reference.get("comparison_id"),
            "retrieval_position": retrieval_analogue.get("presentation_order"),
            "retrieval_tier": tier.get("tier"),
            "outcome_evidence_specification_version": OUTCOME_EVIDENCE_SPECIFICATION_VERSION,
        },
        "review_state": {
            "outcome_reviewed": bool(review),
            "outcome_review_action": review.get("action"),
            "outcome_review_status": review.get("review_status"),
            "reviewer": review.get("reviewer"),
            "review_timestamp": review.get("reviewed_at"),
            "review_notes": review.get("reason"),
            "outcome_revision": outcome.get("revision"),
            "experience_case_version": case_identity.get("case_version"),
            "experience_acceptance_review_id": (
                accepting_review.get("review_id")
                if isinstance(accepting_review, Mapping)
                else None
            ),
            "experience_acceptance_reviewer": (
                accepting_review.get("reviewer")
                if isinstance(accepting_review, Mapping)
                else None
            ),
            "reviewer_disagreement": bool(
                components.get("reviewer_disagreement")
            ),
        },
        "observation_window": window,
        "structural_outcome": {
            "taxonomy_category": structural_category,
            "resolved_hypothesis": resolved_hypothesis or None,
            "confirmed_next_structural_role": resolved_hypothesis or None,
            "reviewed_structure_family": REVIEWED_FAMILY_BY_HYPOTHESIS.get(
                resolved_hypothesis
            ),
            "endpoint_interpretation_status": ENDPOINT_INTERPRETATION_BY_HYPOTHESIS.get(
                resolved_hypothesis, "unresolved"
            ),
            "maximum_confirmed_degree": _unavailable_metric(
                "The reviewed outcome does not store a separately confirmed outcome degree."
            ),
            "final_reviewed_interpretation": outcome.get(
                "final_reviewed_interpretation"
            ),
            "alternative_structure_notes": (
                deepcopy(outcome.get("rejected_alternatives") or [])
                if include_outcome_details
                else {
                    "count": len(outcome.get("rejected_alternatives") or []),
                    "details_included": False,
                }
            ),
            "supporting_future_structure": (
                deepcopy(outcome.get("supporting_future_structure") or [])
                if include_outcome_details
                else {
                    "count": len(outcome.get("supporting_future_structure") or []),
                    "details_included": False,
                }
            ),
            "directional_aftermath": {
                "category": "structurally_unresolved",
                "reason": "Direction is not inferred from a reviewed structural label.",
            },
        },
        "price_behavior": {
            name: _unavailable_metric(no_price_reason)
            for name in (
                "maximum_favorable_excursion",
                "maximum_adverse_excursion",
                "signed_movement",
                "absolute_movement",
                "retracement_from_endpoint",
                "extension_from_endpoint",
                "drawdown",
                "time_to_local_extreme",
                "time_to_invalidation",
            )
        },
        "timing_behavior": {
            "time_to_reviewed_resolution": window.get("elapsed"),
            "first_recorded_structural_event": first_event,
        },
        "momentum_and_volume_aftermath": {
            "rsi": _explicit_aftermath_group(confirmation_dna, "optional_rsi"),
            "momentum": _explicit_aftermath_group(confirmation_dna, "momentum"),
            "volume": _explicit_aftermath_group(confirmation_dna, "volume"),
            "volatility": _explicit_aftermath_group(
                confirmation_dna, "volatility"
            ),
            "channel_break_behavior": (
                {
                    "status": "available",
                    "value": channel_break,
                    "reason": None,
                }
                if channel_break
                else _unavailable_metric(
                    "No explicit channel-break event is stored in confirmation DNA."
                )
            ),
            "trendline_retest_behavior": _unavailable_metric(
                "No explicit trendline-retest event is stored in confirmation DNA."
            ),
        },
        "quality_and_limitations": {
            "data_completeness": (
                "structural_lineage_complete_market_series_unavailable"
                if window.get("completeness_state") == "complete"
                else "incomplete"
            ),
            "feed_continuity": (
                "validated" if not any(item["code"] == "feed_mismatch" for item in issues) else "incompatible"
            ),
            "corporate_action_adjustment_status": "unavailable",
            "missing_intervals": "unavailable_without_post_endpoint_candle_series",
            "unresolved_structure": structural_category == "structurally_ambiguous",
            "reviewer_disagreement": bool(
                components.get("reviewer_disagreement")
            ),
            "warnings": [
                deepcopy(item)
                for item in issues
                if item.get("category") in {"limitation", "unavailable"}
            ],
            "evidence_limitations": [
                no_price_reason,
                "Post-endpoint RSI and volume are shown only when explicitly stored in confirmation DNA.",
                "This is contextual evidence, not a forecast.",
            ],
        },
        "provenance": {
            "historical_endpoint_source": deepcopy(
                dict(_source_identity(endpoint_dna))
            ),
            "historical_outcome_source": deepcopy(
                dict(_source_identity(_mapping(components.get("outcome_dna"))))
            ),
            "case_identity": case_identity,
            "source_policy": (
                "Only immutable stored records linked to the already-retrieved case were read."
            ),
        },
        "source_hashes": _source_hashes(components),
        "issues": issues,
        "contextual_evidence_notice": (
            "Historical reviewed outcome after this historical endpoint, within the "
            "stated observation horizon. This is contextual evidence, not a forecast."
        ),
    }
    record["content_hash"] = _hash_payload(record)
    return record


def _retrieval_presentation(
    analogue: Mapping[str, Any], *, include_retrieval_details: bool
) -> dict[str, Any]:
    if include_retrieval_details:
        return deepcopy(dict(analogue))
    tier = _mapping(analogue.get("retrieval_tier"))
    filter_reference = _mapping(analogue.get("phase5a2_filter_decision_reference"))
    return {
        "experience_case_reference": deepcopy(
            dict(_mapping(analogue.get("experience_case_reference")))
        ),
        "presentation_order": analogue.get("presentation_order"),
        "retrieval_tier": deepcopy(dict(tier)),
        "why_retrieved": tier.get("reason"),
        "comparison_level": filter_reference.get("comparison_level"),
        "compatibility_class": filter_reference.get("compatibility_class"),
        "comparison_summary": deepcopy(
            dict(_mapping(analogue.get("comparison_summary")))
        ),
        "ordering_explanation": deepcopy(
            dict(_mapping(analogue.get("ordering_explanation")))
        ),
        "immutable_input_references": deepcopy(
            dict(_mapping(analogue.get("immutable_input_references")))
        ),
        "warnings": deepcopy(list(analogue.get("warnings") or [])),
        "retrieval_details_included": False,
    }


def attach_reviewed_outcome_evidence(
    retrieval_result: Mapping[str, Any],
    outcome_loader: Callable[[str], Mapping[str, Any] | None],
    *,
    horizon_id: str = DEFAULT_HORIZON_ID,
    horizon_version: str = DEFAULT_HORIZON_VERSION,
    include_retrieval_details: bool = False,
    include_outcome_details: bool = False,
) -> dict[str, Any]:
    """Freeze retrieval, then load outcomes only for the selected case IDs."""
    original = deepcopy(dict(retrieval_result))
    frozen = _validate_retrieval(original)
    horizon = observation_horizon_specification(horizon_id, horizon_version)
    specification = outcome_evidence_specification()
    taxonomy = outcome_taxonomy()
    execution_trace = ["phase5a4_retrieval_validated_and_frozen"]
    records: list[dict[str, Any]] = []
    loaded_ids: list[str] = []
    ordered = list(original.get("ordered_analogue_references") or [])
    for analogue in ordered:
        case_id = str(
            _mapping(analogue.get("experience_case_reference")).get(
                "experience_case_id"
            )
        )
        try:
            source = outcome_loader(case_id)
        except Exception as exc:  # Loader failures become explicit evidence states.
            source = {
                "load_status": "outcome_source_load_error",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        loaded_ids.append(case_id)
        execution_trace.append(f"outcome_source_loaded:{case_id}")
        evidence = build_outcome_evidence_record(
            analogue,
            str(frozen["retrieval_content_hash"]),
            source,
            horizon_id=horizon_id,
            horizon_version=horizon_version,
            include_outcome_details=include_outcome_details,
        )
        records.append(
            {
                "retrieval_evidence": _retrieval_presentation(
                    analogue,
                    include_retrieval_details=include_retrieval_details,
                ),
                "historical_reviewed_outcome": evidence,
            }
        )

    if retrieval_result.get("content_hash") != analogue_retrieval_content_hash(
        retrieval_result
    ):
        raise ValueError("Phase 5A.4 retrieval changed while outcomes were loading.")
    selected_ids = [
        str(item["experience_case_id"]) for item in frozen["selected_analogues"]
    ]
    if loaded_ids != selected_ids:
        raise ValueError("Outcome loader calls do not match the frozen retrieval selection.")
    accepted_count = sum(
        item["historical_reviewed_outcome"]["evidence_state"]
        in {"accepted", "accepted_with_limitations"}
        for item in records
    )
    unavailable_count = len(records) - accepted_count
    if not records:
        status = "unavailable_no_retrieved_analogues"
    elif accepted_count and unavailable_count:
        status = "completed_mixed_outcome_availability"
    elif accepted_count:
        status = "completed"
    else:
        status = "completed_no_accepted_outcome_evidence"
    payload: dict[str, Any] = {
        "schema_version": OUTCOME_EVIDENCE_SCHEMA_VERSION,
        "calculation_version": OUTCOME_EVIDENCE_CALCULATION_VERSION,
        "current_endpoint_identifier": deepcopy(
            dict(_mapping(original.get("current_endpoint_identifier")))
        ),
        "frozen_phase5a4_retrieval": frozen,
        "original_phase5a4_retrieval": original,
        "outcome_evidence_specification": {
            "version": specification["specification_version"],
            "content_hash": specification["content_hash"],
        },
        "outcome_taxonomy": {
            "version": taxonomy["taxonomy_version"],
            "content_hash": taxonomy["content_hash"],
        },
        "observation_horizon": {
            "horizon_id": horizon["horizon_id"],
            "horizon_version": horizon["horizon_version"],
            "specification_version": horizon["specification_version"],
            "content_hash": horizon["content_hash"],
        },
        "presentation_options": {
            "include_retrieval_details": bool(include_retrieval_details),
            "include_outcome_details": bool(include_outcome_details),
            "include_descriptive_summary": False,
        },
        "analogue_evidence": records,
        "counts": {
            "retrieved_analogues": len(records),
            "accepted_outcomes": accepted_count,
            "unavailable_or_invalid_outcomes": unavailable_count,
        },
        "status": status,
        "retrieval_outcome_isolation": {
            "retrieval_validated_before_outcome_load": True,
            "retrieval_frozen_before_outcome_load": True,
            "retrieval_hash_before_outcomes": frozen["retrieval_content_hash"],
            "retrieval_hash_after_outcomes": original["content_hash"],
            "retrieval_hash_unchanged": (
                frozen["retrieval_content_hash"] == original["content_hash"]
            ),
            "ordered_selected_case_ids": selected_ids,
            "outcome_loader_case_ids": loaded_ids,
            "only_selected_cases_loaded": loaded_ids == selected_ids,
            "retrieval_order_preserved": True,
            "retrieval_tiers_preserved": True,
            "execution_trace": execution_trace,
        },
        "interpretation_boundary": (
            "Historical reviewed outcomes are attached after retrieval and are contextual "
            "evidence only."
        ),
    }
    payload["content_hash"] = _hash_payload(payload)
    return payload


def inspect_outcome_source(
    source: Mapping[str, Any] | None,
    *,
    horizon_id: str = DEFAULT_HORIZON_ID,
    horizon_version: str = DEFAULT_HORIZON_VERSION,
    include_outcome_details: bool = False,
) -> dict[str, Any]:
    """Validate one source without treating it as a retrieved current-case analogue."""
    identity = _case_identity_fields(source or {})
    endpoint_dna = _dna_payload(source or {}, "endpoint_dna_row")
    fingerprint = _mapping(
        _mapping((source or {}).get("fingerprint_record")).get("fingerprint")
    )
    snapshot = _mapping((source or {}).get("endpoint_snapshot"))
    pseudo = {
        "experience_case_reference": {
            "experience_case_id": identity.get("experience_case_id"),
            "market_episode_id": identity.get("market_episode_id"),
            "case_version": identity.get("case_version"),
        },
        "presentation_order": None,
        "retrieval_tier": {"tier": None, "label": "not_retrieved_source_audit"},
        "phase5a3_comparison_reference": {},
        "immutable_input_references": {
            "historical_endpoint_dna_hash": endpoint_dna.get("content_hash"),
            "historical_phase3_fingerprint_hash": fingerprint.get("content_hash"),
            "historical_phase4_endpoint_snapshot_hash": snapshot.get("content_hash"),
        },
    }
    evidence = build_outcome_evidence_record(
        pseudo,
        "not_applicable_source_audit",
        source,
        horizon_id=horizon_id,
        horizon_version=horizon_version,
        include_outcome_details=include_outcome_details,
    )
    payload: dict[str, Any] = {
        "mode": "source_audit_not_retrieval",
        "experience_case_id": identity.get("experience_case_id"),
        "historical_reviewed_outcome": evidence,
        "policy": (
            "This command validates one source. It does not add the case to any "
            "current-endpoint retrieval result."
        ),
    }
    payload["content_hash"] = _hash_payload(payload)
    return payload


def _display(value: Any, limit: int = 100) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        text = f"{value:.8g}"
    elif isinstance(value, (str, int, bool)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_outcome_specification(manifest: Mapping[str, Any]) -> str:
    evidence = _mapping(manifest.get("outcome_evidence_specification"))
    taxonomy = _mapping(manifest.get("outcome_taxonomy"))
    horizon = _mapping(manifest.get("observation_horizon_specification"))
    lines = [
        "# Historical Outcome Evidence Specification",
        "",
        f"Evidence specification: {evidence.get('specification_version')}",
        f"Taxonomy: {taxonomy.get('taxonomy_version')}",
        f"Observation horizon: {horizon.get('horizon_id')} version {horizon.get('horizon_version')}",
        "Execution order: freeze Phase 5A.4, then load reviewed outcomes.",
        "Context: historical reviewed evidence only; not a forecast.",
        "",
        "## Structural Categories",
    ]
    for item in taxonomy.get("structural_categories") or []:
        lines.append(f"- {item.get('category')}: {item.get('meaning')}")
    lines.extend(
        [
            "",
            "## Horizon",
            f"Start: {horizon.get('start_rule')}",
            f"End: {horizon.get('end_rule')}",
            f"Censoring: {horizon.get('censoring_behavior')}",
            "",
            f"Manifest hash: {manifest.get('content_hash')}",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_outcome_source_audit(
    result: Mapping[str, Any],
    *,
    explain: bool = False,
    show_provenance: bool = False,
    show_hashes: bool = False,
    include_outcome_details: bool = False,
) -> str:
    outcome = _mapping(result.get("historical_reviewed_outcome"))
    review = _mapping(outcome.get("review_state"))
    window = _mapping(outcome.get("observation_window"))
    structural = _mapping(outcome.get("structural_outcome"))
    quality = _mapping(outcome.get("quality_and_limitations"))
    lines = [
        "# Historical Reviewed Outcome Audit",
        "",
        f"Experience case: {result.get('experience_case_id') or 'unknown'}",
        f"Evidence state: {outcome.get('evidence_state')}",
        "Mode: source audit only; this case was not added to a retrieval result.",
        "Notice: this is contextual evidence, not a forecast.",
        "",
        "## Review",
        f"Action/status: {review.get('outcome_review_action')} / {review.get('outcome_review_status')}",
        f"Reviewer: {review.get('reviewer')}",
        f"Review timestamp: {review.get('review_timestamp')}",
        "",
        "## Observation Horizon",
        f"Horizon: {window.get('horizon_id')} version {window.get('horizon_version')}",
        f"Window: {window.get('observation_start')} to {window.get('observation_end')}",
        f"Completeness: {window.get('completeness_state')}",
        f"Censoring: {window.get('censoring_state')}",
        "",
        "## Structural Outcome",
        f"Category: {structural.get('taxonomy_category')}",
        f"Reviewed hypothesis: {structural.get('resolved_hypothesis')}",
        f"Interpretation: {structural.get('final_reviewed_interpretation')}",
        f"Data completeness: {quality.get('data_completeness')}",
    ]
    if explain:
        lines.extend(
            [
                "",
                "## Limitations",
                "Issues: " + _display(outcome.get("issues"), 800),
                "Measured aftermath: "
                + _display(
                    {
                        "timing": outcome.get("timing_behavior"),
                        "price": outcome.get("price_behavior"),
                        "momentum_volume": outcome.get(
                            "momentum_and_volume_aftermath"
                        ),
                    },
                    1200,
                ),
            ]
        )
    if include_outcome_details:
        lines.append(
            "Outcome details: "
            + _display(
                {
                    "alternatives": structural.get("alternative_structure_notes"),
                    "supporting_structure": structural.get(
                        "supporting_future_structure"
                    ),
                },
                1000,
            )
        )
    if show_provenance:
        lines.extend(
            [
                "",
                "## Provenance",
                _display(outcome.get("provenance"), 1200),
            ]
        )
    if show_hashes:
        lines.extend(
            [
                "",
                "## Hashes",
                f"Outcome evidence hash: {outcome.get('content_hash')}",
                f"Source audit hash: {result.get('content_hash')}",
                "Source hashes: " + _display(outcome.get("source_hashes"), 1200),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_experience_evidence(
    result: Mapping[str, Any],
    *,
    explain: bool = False,
    show_provenance: bool = False,
    show_hashes: bool = False,
    include_retrieval_details: bool = False,
    include_outcome_details: bool = False,
) -> str:
    lines = [
        "# Retrieved Analogues With Historical Reviewed Outcomes",
        "",
        f"Status: {result.get('status')}",
        f"Retrieved analogues: {_mapping(result.get('counts')).get('retrieved_analogues', 0)}",
        f"Accepted historical outcomes: {_mapping(result.get('counts')).get('accepted_outcomes', 0)}",
        "Execution: retrieval was validated and frozen before outcome loading.",
        "Notice: this is contextual evidence, not a forecast.",
    ]
    records = result.get("analogue_evidence") or []
    if not records:
        lines.extend(["", "No retrieved analogue is available for outcome attachment."])
    for item in records:
        retrieval = _mapping(item.get("retrieval_evidence"))
        outcome = _mapping(item.get("historical_reviewed_outcome"))
        identity = _mapping(outcome.get("identity_and_linkage"))
        tier = _mapping(retrieval.get("retrieval_tier"))
        structural = _mapping(outcome.get("structural_outcome"))
        review = _mapping(outcome.get("review_state"))
        window = _mapping(outcome.get("observation_window"))
        lines.extend(
            [
                "",
                f"## Order {identity.get('retrieval_position')}: {identity.get('experience_case_id') or 'unknown'}",
                "",
                "### Retrieval Evidence",
                f"Tier: {tier.get('label') or tier.get('tier')}",
                f"Why retrieved: {retrieval.get('why_retrieved') or tier.get('reason')}",
                f"Comparison level: {retrieval.get('comparison_level')}",
                "",
                "### Historical Reviewed Outcome",
                f"Evidence state: {outcome.get('evidence_state')}",
                f"Review: {review.get('outcome_review_action')} / {review.get('outcome_review_status')}",
                f"Reviewer: {review.get('reviewer')}",
                f"Review timestamp: {review.get('review_timestamp')}",
                f"Observation horizon: {window.get('horizon_id')} ({window.get('completeness_state')}, {window.get('censoring_state')})",
                f"Structural category: {structural.get('taxonomy_category')}",
                f"Reviewed hypothesis: {structural.get('resolved_hypothesis')}",
                f"Reviewed interpretation: {structural.get('final_reviewed_interpretation')}",
                f"Data completeness: {_mapping(outcome.get('quality_and_limitations')).get('data_completeness')}",
            ]
        )
        if explain:
            lines.append("Issues: " + _display(outcome.get("issues"), 600))
            lines.append(
                "Measured aftermath: "
                + _display(
                    {
                        "timing": outcome.get("timing_behavior"),
                        "price": outcome.get("price_behavior"),
                        "momentum_volume": outcome.get(
                            "momentum_and_volume_aftermath"
                        ),
                    },
                    800,
                )
            )
        if include_retrieval_details:
            lines.append("Retrieval details: " + _display(retrieval, 800))
        if include_outcome_details:
            lines.append(
                "Outcome details: "
                + _display(
                    {
                        "alternatives": structural.get("alternative_structure_notes"),
                        "supporting_structure": structural.get(
                            "supporting_future_structure"
                        ),
                    },
                    800,
                )
            )
        if show_provenance:
            lines.append("Provenance: " + _display(outcome.get("provenance"), 800))
        if show_hashes:
            lines.append("Outcome evidence hash: " + str(outcome.get("content_hash")))
            lines.append("Source hashes: " + _display(outcome.get("source_hashes"), 800))
    if show_hashes:
        frozen = _mapping(result.get("frozen_phase5a4_retrieval"))
        lines.extend(
            [
                "",
                f"Frozen retrieval hash: {frozen.get('retrieval_content_hash')}",
                f"Combined evidence hash: {result.get('content_hash')}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
