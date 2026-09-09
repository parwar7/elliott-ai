from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.audit_ssys_intraday_multitimeframe_20260826 import (
    PIVOTS,
    ROOT,
    derive_session_aligned,
    leg_evidence,
)


NY = ZoneInfo("America/New_York")


def complete_session(trading_date: date) -> list[dict[str, object]]:
    local = datetime.combine(trading_date, datetime.min.time(), NY).replace(
        hour=9, minute=30
    )
    rows: list[dict[str, object]] = []
    for index in range(26):
        start = local + timedelta(minutes=15 * index)
        price = 10.0 + index * 0.01
        rows.append(
            {
                "date": start.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "open": price,
                "high": price + 0.05,
                "low": price - 0.05,
                "close": price + 0.01,
                "volume": 1000.0 + index,
            }
        )
    return rows


class SessionAlignedAuditTests(unittest.TestCase):
    def test_all_requested_derived_intervals_tile_a_complete_session(self) -> None:
        rows = complete_session(date(2026, 8, 13))
        expected = {
            "30m": (30, 13, 2, False),
            "45m": (45, 9, 2, True),
            "2h": (120, 4, 2, True),
            "3h": (180, 3, 2, True),
        }
        for timeframe, (minutes, count, final_sources, shortened) in expected.items():
            with self.subTest(timeframe=timeframe):
                derived, issues = derive_session_aligned(
                    rows, timeframe=timeframe, minutes=minutes
                )
                self.assertEqual(issues, [])
                self.assertEqual(len(derived), count)
                self.assertEqual(derived[-1]["terminal_shortened_bucket"], shortened)
                self.assertEqual(derived[-1]["source_row_count"], final_sources)

    def test_missing_source_interval_excludes_the_session(self) -> None:
        rows = complete_session(date(2026, 8, 13))
        del rows[7]
        derived, issues = derive_session_aligned(rows, timeframe="30m", minutes=30)
        self.assertEqual(derived, [])
        self.assertEqual(issues[0]["reason"], "incomplete_or_misaligned_source_session")

    def test_broad_bar_cannot_claim_an_internal_wave_it_cannot_resolve(self) -> None:
        row = {
            "date": "2026-08-13T13:30:00+00:00",
            "bucket_end": "2026-08-13T16:30:00+00:00",
            "open": 9.0,
            "high": 9.32,
            "low": 8.765,
            "close": 8.9,
            "volume": 100000.0,
            "rsi14": 50.0,
            "ewo": 0.0,
            "volume_ratio20": 1.0,
        }
        start = next(pivot for pivot in PIVOTS if pivot["pivot_id"] == "current_b_high")
        end = next(pivot for pivot in PIVOTS if pivot["pivot_id"] == "wave_i_low")
        evidence = leg_evidence([row], "3h", start, end, "down")
        self.assertEqual(evidence["availability"], "incomparable")
        self.assertEqual(evidence["reason"], "both_pivots_share_one_timeframe_candle")

    def test_overlay_auto_mode_shows_active_labels_from_15m_through_4h(self) -> None:
        source = (
            ROOT
            / "market_scans"
            / "2026-08-25_ssys"
            / "SSYS_TOP_DOWN_ELLIOTT_OVERLAY_2026-08-25.pine"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'chartSeconds >= timeframe.in_seconds("15") and chartSeconds <= timeframe.in_seconds("240")',
            source,
        )
        self.assertIn("bool showCurrentInternals = all or current or showIntradayMap", source)
        self.assertIn("bool showActiveFive = all or current or showIntradayMap", source)

    def test_overlay_draws_only_explicit_candidate_pattern_boundaries(self) -> None:
        source = (
            ROOT
            / "market_scans"
            / "2026-08-25_ssys"
            / "SSYS_TOP_DOWN_ELLIOTT_OVERLAY_2026-08-25.pine"
        ).read_text(encoding="utf-8")
        self.assertIn("max_lines_count=80", source)
        self.assertIn('showPatternLines = input.bool(true, "Pattern lines")', source)
        self.assertIn("clearPatternLines()", source)
        self.assertIn(
            "string patternStyle = confirmed ? line.style_solid : line.style_dashed",
            source,
        )
        self.assertEqual(source.count("addDiagonalBoundaries("), 3)
        self.assertIn("2021, 5, 10", source)
        self.assertIn("2026, 6, 9", source)


if __name__ == "__main__":
    unittest.main()
