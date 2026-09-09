"""Apply strict Elliott Wave volume checks to JSON rows or a pandas DataFrame."""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


WAVE_3_LOWEST_WARNING = "Warning: Wave 3 weakest in supporting volume evidence"
WAVE_5_DIVERGENCE = (
    "Valid: Wave 5 Volume Divergence Confirmed (Reversal Imminent)"
)
WEAK_WAVE_3 = "Warning: Weak Wave 3"
HIGH_PULLBACK_VOLUME = "Warning: High selling volume on pullback"
HEALTHY_CORRECTION = "Passed: Healthy Correction"
NO_WARNING = "Passed: No volume warning detected"
INSUFFICIENT_DATA = "Insufficient Data"

DEFAULT_VOLUME_FIELDS = (
    "average_candle_volume",
    "average_volume",
    "total_volume",
)


def _wave_number(value: Any) -> int | None:
    """Normalize labels such as 3, 'Wave 3', '(3)', and '((3))'."""
    if isinstance(value, int) and 1 <= value <= 5:
        return value
    match = re.search(r"([1-5])", str(value))
    return int(match.group(1)) if match else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _choose_volume_field(
    waves: Mapping[int, Mapping[str, Any]], explicit_field: str | None
) -> str | None:
    candidates = (explicit_field,) if explicit_field else DEFAULT_VOLUME_FIELDS
    for field in candidates:
        if field and all(
            _number(waves[number].get(field)) is not None for number in waves
        ):
            return field
    return None


def _sequence_key(row: Mapping[str, Any], sequence_fields: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row.get(field) for field in sequence_fields)


def _evaluate_sequence(
    sequence_rows: Sequence[Mapping[str, Any]], volume_metric: str | None
) -> tuple[str, str, str | None]:
    waves: dict[int, Mapping[str, Any]] = {}
    duplicates: set[int] = set()
    for row in sequence_rows:
        number = _wave_number(row.get("wave_label"))
        if number is None:
            continue
        if number in waves:
            duplicates.add(number)
        waves[number] = row

    if duplicates:
        labels = ", ".join(str(number) for number in sorted(duplicates))
        return INSUFFICIENT_DATA, f"Duplicate wave rows found for: {labels}.", None

    missing = [number for number in range(1, 6) if number not in waves]
    if not waves:
        return INSUFFICIENT_DATA, "No Wave 1-5 rows were found.", None

    field = _choose_volume_field(waves, volume_metric)
    if field is None:
        requested = volume_metric or ", ".join(DEFAULT_VOLUME_FIELDS)
        return (
            INSUFFICIENT_DATA,
            f"No single comparable volume metric is populated for Waves 1-5: {requested}.",
            None,
        )

    volumes = {number: _number(row.get(field)) for number, row in waves.items()}
    if any(volume is None or volume < 0 for volume in volumes.values()):
        return INSUFFICIENT_DATA, f"Invalid or negative values in {field}.", field

    notes: list[str] = [f"Compared all waves using {field}."]
    warnings: list[str] = []

    if missing:
        notes.append("Missing future or unavailable wave rows: " + ", ".join(map(str, missing)) + ".")

    evaluated_checks = 0
    weak_wave_3 = False
    wave_3_is_lowest = False
    if 1 in volumes and 3 in volumes:
        evaluated_checks += 1
        weak_wave_3 = volumes[3] < volumes[1]
        if weak_wave_3:
            warnings.append(WEAK_WAVE_3)
            notes.append("Wave 3 volume is lower than Wave 1 volume.")
        else:
            notes.append("Wave 3 volume is not lower than Wave 1 volume.")
    else:
        notes.append("Wave 3 strength requires completed Waves 1 and 3.")

    if 1 in volumes and 3 in volumes and 5 in volumes:
        evaluated_checks += 1
        wave_3_is_lowest = volumes[3] < volumes[1] and volumes[3] < volumes[5]

    wave_2_check = 1 in volumes and 2 in volumes
    wave_4_check = 3 in volumes and 4 in volumes
    evaluated_checks += int(wave_2_check) + int(wave_4_check)
    healthy_correction = (
        wave_2_check
        and wave_4_check
        and volumes[2] < volumes[1]
        and volumes[4] < volumes[3]
    )
    high_pullback = (
        (wave_2_check and volumes[2] > volumes[1])
        or (wave_4_check and volumes[4] > volumes[3])
    )
    if healthy_correction:
        notes.append("Waves 2 and 4 both have lower volume than their preceding impulses.")
    elif high_pullback:
        warnings.append(HIGH_PULLBACK_VOLUME)
        elevated = []
        if wave_2_check and volumes[2] > volumes[1]:
            elevated.append("Wave 2 > Wave 1")
        if wave_4_check and volumes[4] > volumes[3]:
            elevated.append("Wave 4 > Wave 3")
        notes.append("High pullback volume: " + ", ".join(elevated) + ".")
    elif wave_2_check and wave_4_check:
        notes.append("Correction volume includes equality, so the strict dry-up test did not pass.")
    else:
        notes.append("Healthy-correction status requires both completed Waves 2 and 4.")

    wave_3_peak = _number(waves[3].get("end_price")) if 3 in waves else None
    wave_5_peak = _number(waves[5].get("end_price")) if 5 in waves else None
    can_check_divergence = 3 in volumes and 5 in volumes
    evaluated_checks += int(can_check_divergence)
    divergence = can_check_divergence and (
        wave_3_peak is not None
        and wave_5_peak is not None
        and wave_5_peak > wave_3_peak
        and volumes[5] < volumes[3]
    )
    if divergence:
        notes.append(
            "Wave 5 ended above Wave 3 on strictly lower volume; reversal risk is elevated, "
            "but timing still requires completed structure and price confirmation."
        )
    elif not can_check_divergence or wave_3_peak is None or wave_5_peak is None:
        notes.append("Wave 3/5 end_price is missing, so volume divergence was not evaluated.")
    else:
        notes.append("Wave 5 price/volume divergence is not present.")

    if wave_3_is_lowest:
        status = WAVE_3_LOWEST_WARNING
        notes.append(
            "Wave 3 has strictly the lowest volume of Waves 1, 3, and 5. "
            "This weakens the count but does not violate a classical Elliott price rule."
        )
    elif divergence:
        status = WAVE_5_DIVERGENCE
    elif WEAK_WAVE_3 in warnings:
        status = WEAK_WAVE_3
    elif HIGH_PULLBACK_VOLUME in warnings:
        status = HIGH_PULLBACK_VOLUME
    elif healthy_correction:
        status = HEALTHY_CORRECTION
    elif missing or evaluated_checks == 0:
        status = INSUFFICIENT_DATA
    else:
        status = NO_WARNING

    return status, " ".join(notes), field


def _verify_rows(
    rows: Iterable[Mapping[str, Any]],
    sequence_fields: Sequence[str],
    volume_metric: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    updated = [copy.deepcopy(dict(row)) for row in rows]
    groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    for index, row in enumerate(updated):
        groups[_sequence_key(row, sequence_fields)].append(index)

    status_counts: Counter[str] = Counter()
    metric_counts: Counter[str] = Counter()
    for indices in groups.values():
        status, notes, metric = _evaluate_sequence(
            [updated[index] for index in indices], volume_metric
        )
        status_counts[status] += 1
        if metric:
            metric_counts[metric] += 1
        for index in indices:
            updated[index]["volume_confirmation_status"] = status
            updated[index]["volume_notes"] = notes

    summary = {
        "total_rows": len(updated),
        "total_sequences": len(groups),
        "status_counts": dict(sorted(status_counts.items())),
        "volume_metric_counts": dict(sorted(metric_counts.items())),
    }
    if not updated:
        summary["message"] = "No active wave rows were available for volume verification."
    return updated, summary


def add_volume_confirmation(
    dataset: Any,
    *,
    sequence_fields: Sequence[str] = ("sequence_id",),
    volume_metric: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Add volume status/notes to list rows or a pandas-compatible DataFrame.

    The returned object has the same broad type as ``dataset``. A DataFrame is
    copied before columns are assigned; list input returns a new list of dicts.
    """
    if hasattr(dataset, "to_dict") and hasattr(dataset, "copy"):
        rows = dataset.to_dict(orient="records")
        updated_rows, summary = _verify_rows(rows, sequence_fields, volume_metric)
        result = dataset.copy()
        result["volume_confirmation_status"] = [
            row["volume_confirmation_status"] for row in updated_rows
        ]
        result["volume_notes"] = [row["volume_notes"] for row in updated_rows]
        return result, summary

    return _verify_rows(dataset, sequence_fields, volume_metric)


def apply_to_database(
    database_path: Path,
    report_path: Path,
    *,
    volume_metric: str | None = None,
) -> dict[str, Any]:
    database = json.loads(database_path.read_text(encoding="utf-8"))
    records = database.get("11_ActiveDatabaseRecords", [])
    if not isinstance(records, list):
        raise TypeError("11_ActiveDatabaseRecords must be a JSON array.")

    updated_records, summary = add_volume_confirmation(
        records, volume_metric=volume_metric
    )
    database["11_ActiveDatabaseRecords"] = updated_records
    database_path.write_text(json.dumps(database, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path, default=Path("AI_BRAIN_MASTER_RULES.json")
    )
    parser.add_argument(
        "--report", type=Path, default=Path("volume_verification_report.json")
    )
    parser.add_argument("--volume-metric", default=None)
    args = parser.parse_args()

    summary = apply_to_database(
        args.database, args.report, volume_metric=args.volume_metric
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
