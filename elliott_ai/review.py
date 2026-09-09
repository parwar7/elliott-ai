"""Structured human review records for analysis runs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


REVIEW_STATUSES = ("draft", "reviewed")
CORRECTION_ACTIONS = ("add", "replace", "reject", "confirm")
LESSON_SCOPES = ("symbol_specific", "general_rule_candidate")


def build_correction_template(run: dict[str, Any]) -> dict[str, Any]:
    """Build an editable review template while preserving the source hypothesis."""
    response = run.get("response", {})
    symbol = str(run.get("symbol") or response.get("symbol") or "")
    return {
        "review_status": "draft",
        "title": f"{symbol} review of run {run.get('id', '')}".strip(),
        "summary": "",
        "source_preferred_count": deepcopy(response.get("preferred_count", [])),
        "wave_corrections": [],
        "rejected_labels": [],
        "general_lessons": [],
        "corrected_hierarchy": [],
        "reviewer_note": "",
    }


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_review(value: Any) -> list[str]:
    """Validate a correction document without treating it as canonical knowledge."""
    if not isinstance(value, dict):
        return ["Correction document must be a JSON object."]

    errors: list[str] = []
    status = value.get("review_status")
    if status not in REVIEW_STATUSES:
        errors.append("review_status must be draft or reviewed.")

    list_fields = (
        "source_preferred_count",
        "wave_corrections",
        "rejected_labels",
        "general_lessons",
        "corrected_hierarchy",
    )
    for field in list_fields:
        if not isinstance(value.get(field, []), list):
            errors.append(f"{field} must be an array.")

    corrections = value.get("wave_corrections", [])
    if isinstance(corrections, list):
        for index, correction in enumerate(corrections, start=1):
            prefix = f"wave_corrections[{index}]"
            if not isinstance(correction, dict):
                errors.append(f"{prefix} must be an object.")
                continue
            action = correction.get("action")
            if action not in CORRECTION_ACTIONS:
                errors.append(
                    f"{prefix}.action must be one of {', '.join(CORRECTION_ACTIONS)}."
                )
            if action in {"replace", "reject", "confirm"} and not _nonempty_text(
                correction.get("target")
            ):
                errors.append(f"{prefix}.target is required for action {action!r}.")
            if not _nonempty_text(correction.get("reason")):
                errors.append(f"{prefix}.reason is required.")
            replacement = correction.get("replacement")
            if action in {"add", "replace"} and not isinstance(replacement, dict):
                errors.append(f"{prefix}.replacement must be an object for action {action!r}.")

    rejected = value.get("rejected_labels", [])
    if isinstance(rejected, list):
        for index, item in enumerate(rejected, start=1):
            prefix = f"rejected_labels[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object.")
                continue
            if not _nonempty_text(item.get("label")):
                errors.append(f"{prefix}.label is required.")
            if not _nonempty_text(item.get("reason")):
                errors.append(f"{prefix}.reason is required.")

    lessons = value.get("general_lessons", [])
    if isinstance(lessons, list):
        for index, item in enumerate(lessons, start=1):
            prefix = f"general_lessons[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object.")
                continue
            if item.get("scope") not in LESSON_SCOPES:
                errors.append(
                    f"{prefix}.scope must be symbol_specific or general_rule_candidate."
                )
            if not _nonempty_text(item.get("lesson")):
                errors.append(f"{prefix}.lesson is required.")
            if not _nonempty_text(item.get("basis")):
                errors.append(f"{prefix}.basis is required.")

    if status == "reviewed":
        if not _nonempty_text(value.get("summary")):
            errors.append("A reviewed correction requires a non-empty summary.")
        actionable = any(
            isinstance(value.get(field), list) and bool(value[field])
            for field in (
                "wave_corrections",
                "rejected_labels",
                "general_lessons",
                "corrected_hierarchy",
            )
        )
        if not actionable:
            errors.append(
                "A reviewed correction requires at least one correction, rejection, lesson, "
                "or corrected hierarchy entry."
            )
    return errors
