from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NY = ZoneInfo("America/New_York")
SOURCE_TIMEFRAMES = ("15m", "1h", "4h")
DERIVED_TIMEFRAMES = {"30m": 30, "45m": 45, "2h": 120, "3h": 180}
ALL_TIMEFRAMES = ("15m", "30m", "45m", "1h", "2h", "3h", "4h")
TIMEFRAME_MINUTES = {
    "15m": 15,
    "30m": 30,
    "45m": 45,
    "1h": 60,
    "2h": 120,
    "3h": 180,
    "4h": 240,
}
QUARANTINED_VOLUME_DATES = frozenset({"2026-08-25"})

PIVOTS = (
    {
        "pivot_id": "current_b_high",
        "timestamp": "2026-08-13T13:30:00+00:00",
        "price": 9.32,
        "kind": "high",
    },
    {
        "pivot_id": "wave_i_low",
        "timestamp": "2026-08-13T15:30:00+00:00",
        "price": 8.765,
        "kind": "low",
    },
    {
        "pivot_id": "wave_ii_high",
        "timestamp": "2026-08-13T18:45:00+00:00",
        "price": 9.195,
        "kind": "high",
    },
    {
        "pivot_id": "wave_iii_low",
        "timestamp": "2026-08-20T19:15:00+00:00",
        "price": 7.755,
        "kind": "low",
    },
    {
        "pivot_id": "wave_iv_high",
        "timestamp": "2026-08-21T13:30:00+00:00",
        "price": 8.015,
        "kind": "high",
    },
    {
        "pivot_id": "wave_v_low_candidate",
        "timestamp": "2026-08-25T14:30:00+00:00",
        "price": 7.58,
        "kind": "low",
    },
)

LEGS = (
    ("i", "current_b_high", "wave_i_low", "down"),
    ("ii", "wave_i_low", "wave_ii_high", "up"),
    ("iii", "wave_ii_high", "wave_iii_low", "down"),
    ("iv", "wave_iii_low", "wave_iv_high", "up"),
    ("v_candidate", "wave_iv_high", "wave_v_low_candidate", "down"),
)


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_payload_hash(payload: dict[str, Any], *, source: str) -> None:
    expected = str(payload.get("content_hash") or "")
    unhashed = dict(payload)
    unhashed.pop("content_hash", None)
    actual = canonical_hash(unhashed)
    if expected != actual:
        raise ValueError(f"Content-hash mismatch in {source}.")


def verify_snapshot(snapshot: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_path = snapshot / "SSYS_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_payload_hash(manifest, source=manifest_path.name)
    entries = {str(entry["timeframe"]): dict(entry) for entry in manifest["files"]}
    payloads: dict[str, dict[str, Any]] = {}
    for timeframe in SOURCE_TIMEFRAMES:
        entry = entries[timeframe]
        path = snapshot / str(entry["path"])
        if file_hash(path) != entry["sha256"]:
            raise ValueError(f"SHA-256 mismatch in {path.name}.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        validate_payload_hash(payload, source=path.name)
        metadata = payload["metadata"]
        expected = {
            "provider": "twelve_data",
            "provider_symbol": "SSYS",
            "exchange": "NASDAQ",
            "exchange_timezone": "America/New_York",
            "session": "regular",
            "adjustment": "splits",
            "dividend_adjustment": "none",
            "completed_candles_only": True,
        }
        for key, value in expected.items():
            if metadata.get(key) != value:
                raise ValueError(f"Incompatible {timeframe} metadata field {key}.")
        payloads[timeframe] = payload
    return manifest, payloads


def source_row_hash(row: dict[str, Any]) -> str:
    fields = {key: row[key] for key in ("date", "open", "high", "low", "close", "volume")}
    return canonical_hash(fields)


def session_starts(trading_date: date) -> tuple[datetime, ...]:
    start = datetime.combine(trading_date, time(9, 30), NY)
    close = datetime.combine(trading_date, time(16, 0), NY)
    result: list[datetime] = []
    current = start
    while current < close:
        result.append(current)
        current += timedelta(minutes=15)
    return tuple(result)


def enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from elliott_ai.indicators import calculate_ewo, calculate_macd, calculate_wilder_rsi

    closes = [float(row["close"]) for row in rows]
    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    rsi = calculate_wilder_rsi(closes)["values"]
    ewo = calculate_ewo(highs, lows)["values"]
    macd = calculate_macd(closes)
    for index, row in enumerate(rows):
        history = rows[max(0, index - 19) : index + 1]
        average_volume = sum(float(item["volume"]) for item in history) / len(history)
        row["rsi14"] = rsi[index]
        row["ewo"] = ewo[index]
        row["macd"] = macd["line"][index]
        row["macd_signal"] = macd["signal"][index]
        row["volume_ratio20"] = (
            float(row["volume"]) / average_volume if average_volume else None
        )
    return rows


def derive_session_aligned(
    source_rows: Iterable[dict[str, Any]], *, timeframe: str, minutes: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for source in source_rows:
        local_date = parse_timestamp(source["date"]).astimezone(NY).date()
        grouped[local_date].append(dict(source))

    derived: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    for trading_date in sorted(grouped):
        rows = sorted(grouped[trading_date], key=lambda row: row["date"])
        actual_starts = tuple(parse_timestamp(row["date"]).astimezone(NY) for row in rows)
        expected_starts = session_starts(trading_date)
        if actual_starts != expected_starts:
            exclusions.append(
                {
                    "trading_date": trading_date.isoformat(),
                    "reason": "incomplete_or_misaligned_source_session",
                    "expected_15m_rows": len(expected_starts),
                    "actual_15m_rows": len(actual_starts),
                }
            )
            continue

        session_start = expected_starts[0]
        session_close = datetime.combine(trading_date, time(16, 0), NY)
        bucket_start = session_start
        while bucket_start < session_close:
            bucket_end = min(bucket_start + timedelta(minutes=minutes), session_close)
            members = [
                row
                for row in rows
                if bucket_start
                <= parse_timestamp(row["date"]).astimezone(NY)
                < bucket_end
            ]
            expected_count = int((bucket_end - bucket_start).total_seconds() // 900)
            if len(members) != expected_count:
                exclusions.append(
                    {
                        "trading_date": trading_date.isoformat(),
                        "reason": "missing_source_interval_inside_bucket",
                        "bucket_start": bucket_start.astimezone(timezone.utc).isoformat(),
                        "bucket_end": bucket_end.astimezone(timezone.utc).isoformat(),
                        "expected_15m_rows": expected_count,
                        "actual_15m_rows": len(members),
                    }
                )
                break
            derived.append(
                {
                    "date": bucket_start.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "bucket_end": bucket_end.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "open": float(members[0]["open"]),
                    "high": max(float(row["high"]) for row in members),
                    "low": min(float(row["low"]) for row in members),
                    "close": float(members[-1]["close"]),
                    "volume": sum(float(row["volume"]) for row in members),
                    "source_row_count": len(members),
                    "source_row_hashes": [source_row_hash(row) for row in members],
                    "terminal_shortened_bucket": (
                        bucket_end == session_close
                        and (bucket_end - bucket_start) < timedelta(minutes=minutes)
                    ),
                }
            )
            bucket_start = bucket_end
    derived.sort(key=lambda row: row["date"])
    return enrich(derived), exclusions


def bar_end(row: dict[str, Any], timeframe: str) -> datetime:
    if row.get("bucket_end"):
        return parse_timestamp(row["bucket_end"])
    start = parse_timestamp(row["date"])
    local = start.astimezone(NY)
    close = datetime.combine(local.date(), time(16, 0), NY).astimezone(timezone.utc)
    return min(start + timedelta(minutes=TIMEFRAME_MINUTES[timeframe]), close)


def containing_index(rows: list[dict[str, Any]], timeframe: str, timestamp: str) -> int | None:
    target = parse_timestamp(timestamp)
    for index, row in enumerate(rows):
        start = parse_timestamp(row["date"])
        if start <= target < bar_end(row, timeframe):
            return index
    return None


def numeric_summary(values: Iterable[float]) -> float | None:
    numbers = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return sum(numbers) / len(numbers) if numbers else None


def leg_evidence(
    rows: list[dict[str, Any]],
    timeframe: str,
    start_pivot: dict[str, Any],
    end_pivot: dict[str, Any],
    direction: str,
) -> dict[str, Any]:
    start_index = containing_index(rows, timeframe, start_pivot["timestamp"])
    end_index = containing_index(rows, timeframe, end_pivot["timestamp"])
    if start_index is None or end_index is None:
        return {
            "availability": "not_covered",
            "reason": "pivot_outside_complete_timeframe_coverage",
        }
    start_row = rows[start_index]
    end_row = rows[end_index]
    alignment = {
        "start": (
            "exact_bar_start"
            if parse_timestamp(start_row["date"]) == parse_timestamp(start_pivot["timestamp"])
            else "contained_within_bar"
        ),
        "end": (
            "exact_bar_start"
            if parse_timestamp(end_row["date"]) == parse_timestamp(end_pivot["timestamp"])
            else "contained_within_bar"
        ),
    }
    if start_index == end_index:
        return {
            "availability": "incomparable",
            "reason": "both_pivots_share_one_timeframe_candle",
            "shared_bar_start": start_row["date"],
            "shared_bar_end": bar_end(start_row, timeframe).isoformat(timespec="seconds"),
            "pivot_alignment": alignment,
        }

    selected = rows[min(start_index, end_index) : max(start_index, end_index) + 1]
    indicator_rows = [row for row in selected if row.get("rsi14") is not None]
    volume_rows = [
        row
        for row in selected
        if parse_timestamp(row["date"]).astimezone(NY).date().isoformat()
        not in QUARANTINED_VOLUME_DATES
    ]
    extreme = min if direction == "down" else max
    rsi_values = [float(row["rsi14"]) for row in indicator_rows]
    ewo_values = [float(row["ewo"]) for row in indicator_rows if row.get("ewo") is not None]
    return {
        "availability": "available",
        "bars": len(selected),
        "start_bar": start_row["date"],
        "end_bar": end_row["date"],
        "pivot_alignment": alignment,
        "rsi_start": start_row.get("rsi14"),
        "rsi_end": end_row.get("rsi14"),
        "rsi_directional_extreme": extreme(rsi_values) if rsi_values else None,
        "ewo_start": start_row.get("ewo"),
        "ewo_end": end_row.get("ewo"),
        "ewo_directional_extreme": extreme(ewo_values) if ewo_values else None,
        "cumulative_volume": sum(float(row["volume"]) for row in volume_rows),
        "average_volume_per_bar": numeric_summary(row["volume"] for row in volume_rows),
        "average_volume_ratio20": numeric_summary(
            row.get("volume_ratio20") for row in volume_rows
        ),
        "volume_rows_used": len(volume_rows),
        "volume_rows_quarantined": len(selected) - len(volume_rows),
    }


def compare_metric(
    first: dict[str, Any],
    second: dict[str, Any],
    field: str,
    *,
    second_should_be: str,
) -> dict[str, Any]:
    if first.get("availability") != "available" or second.get("availability") != "available":
        states = {first.get("availability"), second.get("availability")}
        return {
            "status": "incomparable" if "incomparable" in states else "unavailable",
            "reason": "one_or_both_legs_not_comparable",
        }
    first_value = first.get(field)
    second_value = second.get(field)
    if first_value is None or second_value is None:
        return {"status": "unavailable", "reason": f"{field}_missing"}
    relation_holds = (
        float(second_value) < float(first_value)
        if second_should_be == "lower"
        else float(second_value) > float(first_value)
    )
    return {
        "status": "supportive" if relation_holds else "contradictory",
        "first": float(first_value),
        "second": float(second_value),
        "expected_second": second_should_be,
    }


def timeframe_assessment(rows: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    pivot_map = {pivot["pivot_id"]: dict(pivot) for pivot in PIVOTS}
    legs: dict[str, dict[str, Any]] = {}
    for label, start_id, end_id, direction in LEGS:
        legs[label] = leg_evidence(
            rows, timeframe, pivot_map[start_id], pivot_map[end_id], direction
        )

    comparisons = {
        "wave_iii_vs_i_rsi_strength": compare_metric(
            legs["i"], legs["iii"], "rsi_directional_extreme", second_should_be="lower"
        ),
        "wave_iii_vs_i_ewo_strength": compare_metric(
            legs["i"], legs["iii"], "ewo_directional_extreme", second_should_be="lower"
        ),
        "wave_iii_vs_i_relative_volume": compare_metric(
            legs["i"], legs["iii"], "average_volume_ratio20", second_should_be="higher"
        ),
        "wave_ii_volume_reset": compare_metric(
            legs["i"], legs["ii"], "average_volume_ratio20", second_should_be="lower"
        ),
        "wave_iv_volume_reset": compare_metric(
            legs["iii"], legs["iv"], "average_volume_ratio20", second_should_be="lower"
        ),
        "wave_v_vs_iii_rsi_divergence": compare_metric(
            legs["iii"], legs["v_candidate"], "rsi_end", second_should_be="higher"
        ),
        "wave_v_vs_iii_ewo_divergence": compare_metric(
            legs["iii"], legs["v_candidate"], "ewo_end", second_should_be="higher"
        ),
        "wave_v_vs_iii_relative_volume": compare_metric(
            legs["iii"], legs["v_candidate"], "average_volume_ratio20", second_should_be="lower"
        ),
    }
    statuses = [item["status"] for item in comparisons.values()]
    if not any(status in {"supportive", "contradictory"} for status in statuses):
        overall = "unavailable_or_incomparable"
    elif "supportive" in statuses and "contradictory" in statuses:
        overall = "mixed_soft_evidence"
    elif "contradictory" in statuses:
        overall = "contradictory_soft_evidence"
    else:
        overall = "supportive_soft_evidence"
    return {
        "timeframe": timeframe,
        "coverage": {
            "first": rows[0]["date"],
            "last": rows[-1]["date"],
            "bars": len(rows),
        },
        "resolution_capability": (
            "broader_context_only"
            if legs["i"].get("availability") == "incomparable"
            else "current_subwave_comparable"
        ),
        "legs": legs,
        "comparisons": comparisons,
        "overall_soft_evidence": overall,
    }


def write_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "path": path.name,
        "size": path.stat().st_size,
        "sha256": file_hash(path),
        "content_hash": payload["content_hash"],
    }


def run(snapshot: Path, output_root: Path) -> dict[str, Any]:
    manifest, payloads = verify_snapshot(snapshot)
    captured_at = datetime.now(timezone.utc)
    output_directory = output_root / (
        f"multitimeframe_confirmation_{captured_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    output_directory.mkdir(parents=True, exist_ok=False)

    series: dict[str, list[dict[str, Any]]] = {
        timeframe: [dict(row) for row in payloads[timeframe]["candles"]]
        for timeframe in SOURCE_TIMEFRAMES
    }
    derived_files: list[dict[str, Any]] = []
    derivation_issues: dict[str, list[dict[str, Any]]] = {}
    source_15m_hash = str(payloads["15m"]["content_hash"])
    for timeframe, minutes in DERIVED_TIMEFRAMES.items():
        rows, issues = derive_session_aligned(
            series["15m"], timeframe=timeframe, minutes=minutes
        )
        series[timeframe] = rows
        derivation_issues[timeframe] = issues
        payload: dict[str, Any] = {
            "metadata": {
                "provider": "twelve_data",
                "provider_symbol": "SSYS",
                "exchange": "NASDAQ",
                "exchange_timezone": "America/New_York",
                "timeframe": timeframe,
                "source_timeframe": "15m",
                "source_content_hash": source_15m_hash,
                "source_kind": "deterministic_session_aligned_aggregation",
                "aggregation_policy": "nasdaq-regular-session-max-duration-v1",
                "maximum_bucket_minutes": minutes,
                "terminal_shortened_buckets_allowed": True,
                "session": "regular",
                "adjustment": "splits",
                "dividend_adjustment": "none",
                "completed_candles_only": True,
                "captured_at": captured_at.isoformat(timespec="seconds"),
                "applicable_cutoff": rows[-1]["date"],
                "excluded_incomplete_sessions": issues,
            },
            "candles": rows,
        }
        payload["content_hash"] = canonical_hash(payload)
        derived_files.append(
            write_json(output_directory / f"SSYS_{timeframe}_derived_closed.json", payload)
        )

    assessments = {
        timeframe: timeframe_assessment(series[timeframe], timeframe)
        for timeframe in ALL_TIMEFRAMES
    }
    audit: dict[str, Any] = {
        "schema_version": "ssys-intraday-multitimeframe-confirmation-1.0.0",
        "symbol": "NASDAQ:SSYS",
        "source_snapshot": str(snapshot.resolve()),
        "source_manifest_sha256": file_hash(snapshot / "SSYS_manifest.json"),
        "source_manifest_content_hash": manifest["content_hash"],
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "policy": {
            "price_structure_first": True,
            "indicators_are_soft_evidence_only": True,
            "same_provider_and_timeframe_comparisons_only": True,
            "derived_timeframes": {
                timeframe: "complete session-aligned native 15m aggregation"
                for timeframe in DERIVED_TIMEFRAMES
            },
            "quarantined_volume_dates": sorted(QUARANTINED_VOLUME_DATES),
            "no_probability_or_automatic_verification": True,
        },
        "pivots": list(PIVOTS),
        "assessments": assessments,
        "derivation_issues": derivation_issues,
        "interpretation_boundary": (
            "RSI, volume and EWO may support or contradict the candidate count, "
            "but cannot create pivots, select an Elliott family or verify a wave."
        ),
    }
    audit["content_hash"] = canonical_hash(audit)
    audit_file = write_json(
        output_directory / "SSYS_intraday_multitimeframe_confirmation.json", audit
    )
    result_manifest: dict[str, Any] = {
        "schema_version": "ssys-intraday-multitimeframe-manifest-1.0.0",
        "symbol": "NASDAQ:SSYS",
        "source_snapshot": str(snapshot.resolve()),
        "source_manifest_sha256": file_hash(snapshot / "SSYS_manifest.json"),
        "source_manifest_content_hash": manifest["content_hash"],
        "created_at": captured_at.isoformat(timespec="seconds"),
        "derived_files": derived_files,
        "audit_file": audit_file,
    }
    result_manifest["content_hash"] = canonical_hash(result_manifest)
    manifest_file = write_json(
        output_directory / "SSYS_intraday_multitimeframe_manifest.json",
        result_manifest,
    )
    return {
        "output_directory": str(output_directory.resolve()),
        "audit_file": audit_file,
        "manifest_file": manifest_file,
        "assessment_summary": {
            timeframe: assessments[timeframe]["overall_soft_evidence"]
            for timeframe in ALL_TIMEFRAMES
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=ROOT
        / "market_scans"
        / "2026-08-25_ssys"
        / "SSYS_20260826T020129Z",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "market_scans" / "2026-08-25_ssys",
    )
    args = parser.parse_args()
    print(json.dumps(run(args.snapshot.resolve(), args.output_root.resolve()), indent=2))


if __name__ == "__main__":
    main()
