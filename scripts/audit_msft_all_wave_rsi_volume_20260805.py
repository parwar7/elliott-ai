from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from scripts.audit_msft_cycle_iv_rsi_volume_20260725 import (
    Boundary,
    add_rsi_and_volume,
    load_snapshot,
    motive_evidence,
    parse_timestamp,
    segment_metrics,
)
from scripts.audit_tsla_count_20260721 import impulse_rules


SNAPSHOT_DATE = "20260805"
REGULAR_TIMEFRAMES = (
    "monthly",
    "weekly",
    "daily",
    "4h",
    "1h",
    "30m",
    "15m",
    "5m",
)
EXTENDED_TIMEFRAMES = ("4h", "1h", "30m", "15m", "5m")


@dataclass(frozen=True)
class SequenceSpec:
    sequence_id: str
    title: str
    degree: str
    family: str
    timeframe: str
    session: str
    labels: tuple[str, ...]
    boundaries: tuple[Boundary, ...]
    comparison_timeframes: tuple[str, ...]
    notes: str = ""


def b(timestamp: str, price: float) -> Boundary:
    return Boundary(timestamp, price)


MOTIVE_SEQUENCES: tuple[SequenceSpec, ...] = (
    SequenceSpec(
        "cycle_i",
        "Cycle I",
        "Cycle",
        "impulse",
        "weekly",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("1986-03-10T14:30:00+00:00", 0.0885),
            b("1987-10-05T13:30:00+00:00", 0.5504),
            b("1987-10-26T14:30:00+00:00", 0.2587),
            b("1995-07-17T13:30:00+00:00", 6.8281),
            b("1995-10-09T13:30:00+00:00", 5.0234),
            b("1999-12-27T14:30:00+00:00", 59.9688),
        ),
        ("monthly", "weekly", "daily"),
        "The monthly bar cannot order the October 1987 high and low; weekly and daily data supply the ordering.",
    ),
    SequenceSpec(
        "cycle_ii_a",
        "Cycle II - Primary A",
        "Primary",
        "impulse",
        "daily",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("1999-12-30T14:30:00+00:00", 59.9688),
            b("2000-02-28T14:30:00+00:00", 44.063),
            b("2000-03-24T14:30:00+00:00", 57.50),
            b("2000-12-21T14:30:00+00:00", 20.125),
            b("2001-06-28T13:30:00+00:00", 38.075),
            b("2002-07-24T13:30:00+00:00", 20.705),
        ),
        ("monthly", "weekly", "daily"),
        "Wave 5 is truncated above the Wave 3 low, which is permitted but uncommon.",
    ),
    SequenceSpec(
        "cycle_ii_c",
        "Cycle II - Primary C",
        "Primary",
        "impulse",
        "daily",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2007-11-02T13:30:00+00:00", 37.50),
            b("2008-03-03T14:30:00+00:00", 26.87),
            b("2008-04-24T13:30:00+00:00", 32.10),
            b("2008-11-20T14:30:00+00:00", 17.50),
            b("2008-12-08T14:30:00+00:00", 21.25),
            b("2009-03-06T14:30:00+00:00", 14.87),
        ),
        ("monthly", "weekly", "daily"),
    ),
    SequenceSpec(
        "cycle_iii",
        "Cycle III",
        "Cycle",
        "impulse",
        "monthly",
        "regular",
        ("I", "II", "III", "IV", "V"),
        (
            b("2009-03-02T14:30:00+00:00", 14.87),
            b("2010-04-01T13:30:00+00:00", 31.58),
            b("2010-07-01T13:30:00+00:00", 22.73),
            b("2021-11-01T13:30:00+00:00", 349.67),
            b("2022-11-01T13:30:00+00:00", 213.431),
            b("2025-07-01T13:30:00+00:00", 555.45),
        ),
        ("monthly", "weekly", "daily"),
    ),
    SequenceSpec(
        "cycle_iii_primary_3",
        "Cycle III - Primary 3",
        "Primary",
        "impulse",
        "daily",
        "regular",
        ("(1)", "(2)", "(3)", "(4)", "(5)"),
        (
            b("2010-07-01T13:30:00+00:00", 22.73),
            b("2012-03-16T13:30:00+00:00", 32.95),
            b("2012-12-05T14:30:00+00:00", 26.26),
            b("2020-02-11T14:30:00+00:00", 190.70),
            b("2020-03-23T13:30:00+00:00", 132.52),
            b("2021-11-22T14:30:00+00:00", 349.67),
        ),
        ("monthly", "weekly", "daily", "4h"),
    ),
    SequenceSpec(
        "primary_3_intermediate_3",
        "Primary 3 - Intermediate (3)",
        "Intermediate",
        "impulse",
        "daily",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2012-12-05T14:30:00+00:00", 26.26),
            b("2014-11-14T14:30:00+00:00", 50.045),
            b("2015-08-24T13:30:00+00:00", 39.72),
            b("2018-10-03T13:30:00+00:00", 116.18),
            b("2018-12-26T14:30:00+00:00", 93.96),
            b("2020-02-11T14:30:00+00:00", 190.70),
        ),
        ("monthly", "weekly", "daily", "4h"),
    ),
    SequenceSpec(
        "primary_3_intermediate_5",
        "Primary 3 - Intermediate (5)",
        "Intermediate",
        "impulse",
        "daily",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2020-03-23T13:30:00+00:00", 132.52),
            b("2020-09-02T13:30:00+00:00", 232.86),
            b("2020-09-18T13:30:00+00:00", 196.25),
            b("2021-08-20T13:30:00+00:00", 305.84),
            b("2021-10-04T13:30:00+00:00", 280.25),
            b("2021-11-22T14:30:00+00:00", 349.67),
        ),
        ("monthly", "weekly", "daily", "4h"),
    ),
    SequenceSpec(
        "cycle_iii_primary_5",
        "Cycle III - Primary 5",
        "Primary",
        "impulse",
        "daily",
        "regular",
        ("(1)", "(2)", "(3)", "(4)", "(5)"),
        (
            b("2022-11-04T13:30:00+00:00", 213.431),
            b("2022-12-13T14:30:00+00:00", 263.915),
            b("2023-01-06T14:30:00+00:00", 219.35),
            b("2024-07-05T13:30:00+00:00", 468.35),
            b("2025-04-07T13:30:00+00:00", 344.79),
            b("2025-07-31T13:30:00+00:00", 555.45),
        ),
        ("monthly", "weekly", "daily", "4h", "1h"),
    ),
    SequenceSpec(
        "primary_5_intermediate_3",
        "Primary 5 - Intermediate (3)",
        "Intermediate",
        "impulse",
        "daily",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2023-01-06T14:30:00+00:00", 219.35),
            b("2023-02-08T14:30:00+00:00", 276.75),
            b("2023-03-02T14:30:00+00:00", 245.63),
            b("2023-07-18T13:30:00+00:00", 366.77),
            b("2023-09-28T13:30:00+00:00", 309.45),
            b("2024-07-05T13:30:00+00:00", 468.35),
        ),
        ("weekly", "daily", "4h", "1h"),
    ),
    SequenceSpec(
        "primary_5_intermediate_5",
        "Primary 5 - Intermediate (5)",
        "Intermediate",
        "impulse",
        "1h",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2025-04-07T13:30:00+00:00", 344.79),
            b("2025-04-14T19:30:00+00:00", 394.66),
            b("2025-04-21T13:30:00+00:00", 355.70),
            b("2025-05-01T14:30:00+00:00", 437.00),
            b("2025-05-01T17:30:00+00:00", 424.94),
            b("2025-07-31T13:30:00+00:00", 555.45),
        ),
        ("weekly", "daily", "4h", "1h", "30m"),
    ),
    SequenceSpec(
        "terminal_minor_5",
        "Intermediate (5) - Minor 5",
        "Minor",
        "impulse",
        "1h",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2025-05-01T17:30:00+00:00", 424.94),
            b("2025-05-22T13:30:00+00:00", 460.25),
            b("2025-05-23T13:30:00+00:00", 448.92),
            b("2025-06-30T13:30:00+00:00", 500.79),
            b("2025-07-02T13:30:00+00:00", 488.66),
            b("2025-07-31T13:30:00+00:00", 555.45),
        ),
        ("daily", "4h", "1h", "30m"),
    ),
    SequenceSpec(
        "cycle_iv_outer_a_alternate",
        "Cycle IV - direct Primary A alternate",
        "Primary",
        "impulse",
        "1h",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2025-07-31T13:30:00+00:00", 555.45),
            b("2025-09-05T18:30:00+00:00", 492.38),
            b("2025-10-28T13:30:00+00:00", 553.50),
            b("2026-01-21T18:30:00+00:00", 438.67),
            b("2026-01-28T14:30:00+00:00", 483.52),
            b("2026-03-30T18:30:00+00:00", 356.26),
        ),
        ("weekly", "daily", "4h", "1h", "30m"),
        "This is the direct-five alternate to the preferred 5-3-5 W interpretation.",
    ),
    SequenceSpec(
        "cycle_iv_w_a",
        "Cycle IV W - A",
        "Intermediate",
        "impulse",
        "30m",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2025-07-31T13:30:00+00:00", 555.45),
            b("2025-08-01T17:00:00+00:00", 520.88),
            b("2025-08-04T17:30:00+00:00", 538.25),
            b("2025-08-26T18:30:00+00:00", 498.51),
            b("2025-08-28T19:30:00+00:00", 510.90),
            b("2025-09-05T19:00:00+00:00", 492.38),
        ),
        ("daily", "4h", "1h", "30m"),
    ),
    SequenceSpec(
        "cycle_iv_w_c",
        "Cycle IV W - C",
        "Intermediate",
        "impulse",
        "1h",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2025-10-28T13:30:00+00:00", 553.50),
            b("2025-11-25T14:30:00+00:00", 464.89),
            b("2025-12-02T17:30:00+00:00", 493.44),
            b("2026-02-05T20:30:00+00:00", 392.33),
            b("2026-02-10T14:30:00+00:00", 423.68),
            b("2026-03-30T18:30:00+00:00", 356.26),
        ),
        ("weekly", "daily", "4h", "1h", "30m"),
    ),
    SequenceSpec(
        "cycle_iv_x_a",
        "Cycle IV X - A",
        "Minor",
        "impulse",
        "30m",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2026-03-30T18:30:00+00:00", 356.85),
            b("2026-04-08T13:30:00+00:00", 385.00),
            b("2026-04-09T14:30:00+00:00", 367.06),
            b("2026-04-17T15:00:00+00:00", 431.58),
            b("2026-04-20T15:00:00+00:00", 416.32),
            b("2026-04-22T19:30:00+00:00", 434.00),
        ),
        ("daily", "4h", "1h", "30m", "15m"),
    ),
    SequenceSpec(
        "cycle_iv_y_a",
        "Cycle IV Y - A",
        "Minor",
        "impulse",
        "5m",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2026-06-01T14:00:00+00:00", 466.33),
            b("2026-06-01T17:50:00+00:00", 458.27),
            b("2026-06-01T19:35:00+00:00", 464.15),
            b("2026-06-03T14:00:00+00:00", 430.85),
            b("2026-06-03T14:20:00+00:00", 433.19),
            b("2026-06-03T16:35:00+00:00", 424.26),
        ),
        ("1h", "30m", "15m", "5m"),
        "The June 2-3 gap makes this lower-timeframe five plausible but less secure than the surrounding pivots.",
    ),
    SequenceSpec(
        "cycle_iv_y_c",
        "Cycle IV Y - C",
        "Minor",
        "impulse",
        "30m",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2026-06-04T13:30:00+00:00", 436.10),
            b("2026-06-12T13:30:00+00:00", 382.27),
            b("2026-06-15T16:30:00+00:00", 401.75),
            b("2026-06-22T19:30:00+00:00", 367.07),
            b("2026-06-24T13:30:00+00:00", 378.88),
            b("2026-06-25T17:30:00+00:00", 349.20),
        ),
        ("daily", "4h", "1h", "30m", "15m"),
    ),
    SequenceSpec(
        "cycle_iv_x2_a",
        "Cycle IV X2 candidate - A",
        "Minor",
        "impulse",
        "5m",
        "regular",
        ("1", "2", "3", "4", "5"),
        (
            b("2026-06-25T17:45:00+00:00", 349.20),
            b("2026-06-29T13:45:00+00:00", 380.50),
            b("2026-06-29T19:30:00+00:00", 366.82),
            b("2026-07-02T19:45:00+00:00", 392.39),
            b("2026-07-06T13:30:00+00:00", 381.23),
            b("2026-07-07T14:15:00+00:00", 395.57),
        ),
        ("daily", "4h", "1h", "30m", "15m", "5m"),
    ),
    SequenceSpec(
        "cycle_iv_x2_c",
        "Cycle IV X2 candidate - C",
        "Minor",
        "impulse",
        "15m",
        "extended",
        ("1", "2", "3", "4", "5"),
        (
            b("2026-07-29T13:45:00+00:00", 388.75),
            b("2026-07-29T20:00:00+00:00", 409.48),
            b("2026-07-29T20:15:00+00:00", 391.73),
            b("2026-07-30T18:00:00+00:00", 458.68),
            b("2026-07-30T22:30:00+00:00", 445.13),
            b("2026-08-04T15:15:00+00:00", 499.33),
        ),
        ("4h", "1h", "30m", "15m"),
        "Extended-hours data is required for Waves 1, 2 and 4. Its RSI and volume are not mixed with regular-session metrics.",
    ),
    SequenceSpec(
        "cycle_iv_x2_c_wave_5",
        "X2-C - terminal Wave 5",
        "Minute",
        "impulse",
        "15m",
        "extended",
        ("i", "ii", "iii", "iv", "v"),
        (
            b("2026-07-30T22:30:00+00:00", 445.13),
            b("2026-07-31T14:00:00+00:00", 462.95),
            b("2026-07-31T15:00:00+00:00", 450.68),
            b("2026-08-03T18:45:00+00:00", 491.64),
            b("2026-08-04T13:00:00+00:00", 475.53),
            b("2026-08-04T15:15:00+00:00", 499.33),
        ),
        ("1h", "30m", "15m"),
    ),
)


CORRECTION_SEQUENCES: tuple[SequenceSpec, ...] = (
    SequenceSpec(
        "cycle_ii_zigzag",
        "Cycle II",
        "Cycle",
        "zigzag 5-3-5",
        "weekly",
        "regular",
        ("A", "B", "C"),
        (
            b("1999-12-27T14:30:00+00:00", 59.9688),
            b("2002-07-22T13:30:00+00:00", 20.705),
            b("2007-10-29T13:30:00+00:00", 37.50),
            b("2009-03-02T14:30:00+00:00", 14.87),
        ),
        ("monthly", "weekly", "daily"),
    ),
    SequenceSpec(
        "cycle_iii_primary_4_wxy",
        "Cycle III - Primary 4",
        "Primary",
        "W-X-Y",
        "daily",
        "regular",
        ("W", "X", "Y"),
        (
            b("2021-11-22T14:30:00+00:00", 349.67),
            b("2022-06-14T13:30:00+00:00", 241.51),
            b("2022-08-15T13:30:00+00:00", 294.18),
            b("2022-11-04T13:30:00+00:00", 213.431),
        ),
        ("monthly", "weekly", "daily", "4h"),
    ),
    SequenceSpec(
        "cycle_iii_primary_4_w",
        "Primary 4 - W",
        "Intermediate",
        "zigzag 5-3-5",
        "daily",
        "regular",
        ("A", "B", "C"),
        (
            b("2021-11-22T14:30:00+00:00", 349.67),
            b("2022-03-08T14:30:00+00:00", 270.00),
            b("2022-03-30T13:30:00+00:00", 315.95),
            b("2022-06-14T13:30:00+00:00", 241.51),
        ),
        ("weekly", "daily", "4h"),
    ),
    SequenceSpec(
        "cycle_iii_primary_4_x",
        "Primary 4 - X",
        "Intermediate",
        "zigzag candidate",
        "daily",
        "regular",
        ("A", "B", "C"),
        (
            b("2022-06-14T13:30:00+00:00", 241.51),
            b("2022-07-07T13:30:00+00:00", 269.055),
            b("2022-07-14T13:30:00+00:00", 245.94),
            b("2022-08-15T13:30:00+00:00", 294.18),
        ),
        ("weekly", "daily", "4h"),
    ),
    SequenceSpec(
        "cycle_iii_primary_4_y",
        "Primary 4 - Y",
        "Intermediate",
        "zigzag 5-3-5",
        "daily",
        "regular",
        ("A", "B", "C"),
        (
            b("2022-08-15T13:30:00+00:00", 294.18),
            b("2022-09-06T13:30:00+00:00", 251.94),
            b("2022-09-12T13:30:00+00:00", 267.45),
            b("2022-11-04T13:30:00+00:00", 213.431),
        ),
        ("weekly", "daily", "4h"),
    ),
    SequenceSpec(
        "primary_5_intermediate_4_wxy",
        "Cycle III Primary 5 - Intermediate (4)",
        "Intermediate",
        "W-X-Y",
        "daily",
        "regular",
        ("W", "X", "Y"),
        (
            b("2024-07-05T13:30:00+00:00", 468.35),
            b("2024-08-05T13:30:00+00:00", 385.58),
            b("2024-12-12T14:30:00+00:00", 456.165),
            b("2025-04-07T13:30:00+00:00", 344.79),
        ),
        ("weekly", "daily", "4h", "1h"),
        "The parent W-X-Y footprint is clear; not every lower-degree component is uniquely resolved.",
    ),
    SequenceSpec(
        "cycle_iv_w",
        "Cycle IV - W",
        "Primary",
        "zigzag 5-3-5",
        "1h",
        "regular",
        ("A", "B", "C"),
        (
            b("2025-07-31T13:30:00+00:00", 555.45),
            b("2025-09-05T19:00:00+00:00", 492.38),
            b("2025-10-28T13:30:00+00:00", 553.50),
            b("2026-03-30T18:30:00+00:00", 356.26),
        ),
        ("weekly", "daily", "4h", "1h", "30m"),
    ),
    SequenceSpec(
        "cycle_iv_x",
        "Cycle IV - X",
        "Primary",
        "zigzag with ending-diagonal C candidate",
        "1h",
        "regular",
        ("A", "B", "C"),
        (
            b("2026-03-30T18:30:00+00:00", 356.26),
            b("2026-04-22T19:30:00+00:00", 434.00),
            b("2026-04-30T16:30:00+00:00", 398.01),
            b("2026-06-01T13:30:00+00:00", 466.33),
        ),
        ("weekly", "daily", "4h", "1h", "30m"),
    ),
    SequenceSpec(
        "cycle_iv_y",
        "Cycle IV - Y",
        "Primary",
        "zigzag 5-3-5",
        "1h",
        "regular",
        ("A", "B", "C"),
        (
            b("2026-06-01T13:30:00+00:00", 466.33),
            b("2026-06-03T16:30:00+00:00", 424.26),
            b("2026-06-04T13:30:00+00:00", 436.10),
            b("2026-06-25T17:30:00+00:00", 349.20),
        ),
        ("weekly", "daily", "4h", "1h", "30m", "15m"),
        "The A-wave five is price-valid on 5-minute data but carries a gap-order warning.",
    ),
    SequenceSpec(
        "cycle_iv_x2_candidate",
        "Cycle IV - X2 candidate",
        "Primary",
        "zigzag 5-3-5",
        "15m",
        "extended",
        ("A", "B", "C"),
        (
            b("2026-06-25T17:30:00+00:00", 349.20),
            b("2026-07-07T14:15:00+00:00", 395.57),
            b("2026-07-29T13:45:00+00:00", 388.75),
            b("2026-08-04T15:15:00+00:00", 499.33),
        ),
        ("weekly", "daily", "4h", "1h", "30m", "15m"),
        "The daily high is 499.4399 and the extended 15-minute high is 499.33; extended-session RSI and volume are kept separate from regular-session metrics.",
    ),
    SequenceSpec(
        "cycle_iv_x2_b",
        "X2 candidate - B",
        "Minor",
        "W-X-Y-X2-Z triple-three candidate",
        "15m",
        "regular",
        ("W", "X", "Y", "X2", "Z"),
        (
            b("2026-07-07T14:15:00+00:00", 395.57),
            b("2026-07-09T13:30:00+00:00", 373.36),
            b("2026-07-16T19:00:00+00:00", 405.975),
            b("2026-07-23T15:00:00+00:00", 377.39),
            b("2026-07-28T15:30:00+00:00", 400.32),
            b("2026-07-29T13:45:00+00:00", 388.75),
        ),
        ("daily", "4h", "1h", "30m", "15m"),
        "The corrective character is clear; the exact triple-three family remains a working hypothesis.",
    ),
    SequenceSpec(
        "cycle_iv_wxyx2_parent",
        "Cycle IV - W-X-Y-X2-Z candidate",
        "Cycle",
        "W-X-Y-X2-Z, Z not started",
        "weekly",
        "regular",
        ("W", "X", "Y", "X2"),
        (
            b("2025-07-28T13:30:00+00:00", 555.45),
            b("2026-03-30T13:30:00+00:00", 356.26),
            b("2026-06-01T13:30:00+00:00", 466.33),
            b("2026-06-22T13:30:00+00:00", 349.20),
            b("2026-08-03T13:30:00+00:00", 499.4399),
        ),
        ("weekly", "daily", "4h", "1h"),
        "X2 and the future Z are not confirmed. The rally can instead be the start of Cycle V.",
    ),
)


def load_custom(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for source in payload["bars"]:
        row = dict(source)
        row["timestamp"] = parse_timestamp(row["date"])
        for field in ("open", "high", "low", "close", "volume"):
            row[field] = float(row[field])
        rows.append(row)
    rows.sort(key=lambda row: row["timestamp"])
    return payload["metadata"], add_rsi_and_volume(rows)


def load_datasets(snapshot_date: str) -> dict[str, dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]]:
    regular: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for timeframe in REGULAR_TIMEFRAMES:
        if timeframe == "5m":
            regular[timeframe] = load_custom(ROOT / f"tv_msft_5m_{snapshot_date}.json")
        else:
            regular[timeframe] = load_snapshot(timeframe, snapshot_date)

    extended = {
        timeframe: load_custom(
            ROOT / f"tv_msft_{timeframe}_extended_{snapshot_date}.json"
        )
        for timeframe in EXTENDED_TIMEFRAMES
    }
    return {"regular": regular, "extended": extended}


def finite(value: Any) -> bool:
    return value is not None and math.isfinite(float(value))


def evidence_state(condition: bool | None, absent: str = "contradictory") -> str:
    if condition is None:
        return "unavailable"
    if condition:
        return "supportive"
    return absent


def timeframe_motive_metrics(
    rows: list[dict[str, Any]], boundaries: tuple[Boundary, ...]
) -> dict[str, Any]:
    start_at = parse_timestamp(boundaries[0].timestamp)
    end_at = parse_timestamp(boundaries[-1].timestamp)
    if start_at < rows[0]["timestamp"] or end_at > rows[-1]["timestamp"]:
        return {"availability": "unavailable"}
    waves = [
        segment_metrics(rows, start, end)
        for start, end in zip(boundaries[:-1], boundaries[1:])
    ]
    if any(wave.get("availability") != "available" for wave in waves):
        return {"availability": "unavailable"}
    evidence = motive_evidence(waves)
    return {
        "availability": "available",
        "minimum_wave_bars": min(wave["bars"] for wave in waves),
        "waves": waves,
        "evidence": evidence,
    }


def build_motive_result(
    spec: SequenceSpec,
    datasets: dict[str, dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]],
) -> dict[str, Any]:
    session_data = datasets[spec.session]
    primary = timeframe_motive_metrics(
        session_data[spec.timeframe][1], spec.boundaries
    )
    hard_rules = impulse_rules([boundary.price for boundary in spec.boundaries])
    comparisons: dict[str, Any] = {}
    for timeframe in spec.comparison_timeframes:
        if timeframe not in session_data:
            comparisons[timeframe] = {"availability": "unavailable"}
            continue
        comparisons[timeframe] = timeframe_motive_metrics(
            session_data[timeframe][1], spec.boundaries
        )

    evidence = primary.get("evidence", {})
    evidence_contract = {
        "wave_3_rsi_vs_wave_1": evidence_state(
            evidence.get("wave_3_rsi_stronger_than_wave_1")
        ),
        "wave_3_relative_volume_vs_wave_1": (
            "supportive"
            if evidence.get("wave_3_average_relative_volume_vs_wave_1", 0) >= 1.0
            else "contradictory"
            if finite(evidence.get("wave_3_average_relative_volume_vs_wave_1"))
            else "unavailable"
        ),
        "wave_2_volume_reset": evidence_state(
            evidence.get("wave_2_volume_dry_up_vs_wave_1")
        ),
        "wave_4_volume_reset": evidence_state(
            evidence.get("wave_4_volume_dry_up_vs_wave_3")
        ),
        "wave_5_rsi_divergence": evidence_state(
            evidence.get("wave_5_aligned_endpoint_rsi_divergence_vs_wave_3"),
            absent="neutral",
        ),
        "wave_5_volume_divergence": evidence_state(
            evidence.get("wave_5_average_relative_volume_dry_up_vs_wave_3"),
            absent="neutral",
        ),
    }
    hard_valid = bool(
        hard_rules["wave_3_not_shortest_arithmetic"]
        and hard_rules["wave_3_not_shortest_log"]
        and hard_rules["wave_4_no_overlap"]
    )
    return {
        "sequence_id": spec.sequence_id,
        "title": spec.title,
        "degree": spec.degree,
        "family": spec.family,
        "timeframe": spec.timeframe,
        "session": spec.session,
        "labels": list(spec.labels),
        "boundaries": [boundary.__dict__ for boundary in spec.boundaries],
        "hard_price_rules": hard_rules,
        "hard_price_status": "valid" if hard_valid else "invalid",
        "primary_metrics": primary,
        "comparison_by_timeframe": comparisons,
        "evidence_contract": evidence_contract,
        "notes": spec.notes,
    }


def correction_metrics(
    rows: list[dict[str, Any]], spec: SequenceSpec
) -> dict[str, Any]:
    legs = [
        {
            "label": label,
            **segment_metrics(rows, start, end),
        }
        for label, start, end in zip(
            spec.labels, spec.boundaries[:-1], spec.boundaries[1:]
        )
    ]
    if any(leg.get("availability") != "available" for leg in legs):
        return {"availability": "unavailable", "legs": legs}

    connector_indices = [
        index for index, label in enumerate(spec.labels) if label in {"B", "X", "X2"}
    ]
    connector_evidence: list[dict[str, Any]] = []
    for index in connector_indices:
        connector = legs[index]
        prior = legs[index - 1] if index else None
        volume_ratio = (
            connector["average_relative_volume_50"]
            / prior["average_relative_volume_50"]
            if prior
            and finite(connector.get("average_relative_volume_50"))
            and finite(prior.get("average_relative_volume_50"))
            else None
        )
        connector_evidence.append(
            {
                "label": connector["label"],
                "relative_volume_ratio_to_prior_leg": (
                    round(volume_ratio, 6) if volume_ratio is not None else None
                ),
                "volume_reset_status": (
                    "supportive"
                    if volume_ratio is not None and volume_ratio < 1.0
                    else "contradictory"
                    if volume_ratio is not None
                    else "unavailable"
                ),
                "rsi_crossed_or_touched_50": (
                    connector["rsi_minimum"] <= 50.0 <= connector["rsi_maximum"]
                    if finite(connector.get("rsi_minimum"))
                    and finite(connector.get("rsi_maximum"))
                    else None
                ),
            }
        )

    first = legs[0]
    last = legs[-1]
    terminal_divergence: bool | None = None
    if (
        first["direction"] == last["direction"]
        and finite(first.get("rsi_at_end_bar_close"))
        and finite(last.get("rsi_at_end_bar_close"))
    ):
        if first["direction"] == "down" and last["end_price"] < first["end_price"]:
            terminal_divergence = (
                last["rsi_at_end_bar_close"] > first["rsi_at_end_bar_close"]
            )
        elif first["direction"] == "up" and last["end_price"] > first["end_price"]:
            terminal_divergence = (
                last["rsi_at_end_bar_close"] < first["rsi_at_end_bar_close"]
            )
    return {
        "availability": "available",
        "legs": legs,
        "connector_evidence": connector_evidence,
        "terminal_aligned_rsi_divergence": terminal_divergence,
        "terminal_divergence_status": evidence_state(
            terminal_divergence, absent="neutral"
        ),
    }


def build_correction_result(
    spec: SequenceSpec,
    datasets: dict[str, dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]],
) -> dict[str, Any]:
    session_data = datasets[spec.session]
    primary = correction_metrics(session_data[spec.timeframe][1], spec)
    comparisons: dict[str, Any] = {}
    for timeframe in spec.comparison_timeframes:
        if timeframe not in session_data:
            comparisons[timeframe] = {"availability": "unavailable"}
            continue
        rows = session_data[timeframe][1]
        if (
            parse_timestamp(spec.boundaries[0].timestamp) < rows[0]["timestamp"]
            or parse_timestamp(spec.boundaries[-1].timestamp) > rows[-1]["timestamp"]
        ):
            comparisons[timeframe] = {"availability": "unavailable"}
        else:
            comparisons[timeframe] = correction_metrics(rows, spec)
    return {
        "sequence_id": spec.sequence_id,
        "title": spec.title,
        "degree": spec.degree,
        "family": spec.family,
        "timeframe": spec.timeframe,
        "session": spec.session,
        "labels": list(spec.labels),
        "boundaries": [boundary.__dict__ for boundary in spec.boundaries],
        "primary_metrics": primary,
        "comparison_by_timeframe": comparisons,
        "notes": spec.notes,
    }


def stable_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_audit(snapshot_date: str) -> dict[str, Any]:
    datasets = load_datasets(snapshot_date)
    motive = [build_motive_result(spec, datasets) for spec in MOTIVE_SEQUENCES]
    corrections = [
        build_correction_result(spec, datasets) for spec in CORRECTION_SEQUENCES
    ]
    data_sources = {
        session: {
            timeframe: metadata
            for timeframe, (metadata, _rows) in timeframe_data.items()
        }
        for session, timeframe_data in datasets.items()
    }
    audit: dict[str, Any] = {
        "audit_id": f"msft-all-wave-rsi-volume-{snapshot_date}-v1",
        "symbol": "BATS:MSFT",
        "listed_exchange": "NASDAQ",
        "data_cutoff": "2026-08-04 regular close; extended data captured through 2026-08-05 09:00 UTC",
        "indicator_policy": {
            "rsi": "canonical Wilder RSI(14)",
            "volume": "average relative volume versus trailing 50 bars",
            "hard_rule": "RSI and volume never invalidate a price-valid Elliott count",
            "comparison_rule": "Only matching feed, timeframe, and session metrics are compared",
        },
        "data_sources": data_sources,
        "motive_sequences": motive,
        "correction_sequences": corrections,
        "current_ambiguity": {
            "shared_structure": (
                "$349.20->$395.57 is a valid five, $395.57->$388.75 is corrective, "
                "and $388.75->$499.33 is a valid five."
            ),
            "bearish_candidate": (
                "The rally is X2 of a still-active Cycle IV W-X-Y-X2-Z correction; "
                "a future Z decline is not confirmed."
            ),
            "bullish_candidate": (
                "Cycle IV ended at $349.20 and the same 5-3-5 footprint is the early "
                "1-2-3 development of Cycle V."
            ),
            "indicator_conclusion": (
                "The current terminal high is not confirmed: the aligned RSI at the "
                "candidate fifth-wave high exceeds the Wave 3 pivot RSI, and the nested "
                "terminal fifth also lacks RSI/volume exhaustion."
            ),
            "distinguishing_evidence": [
                "A sustained reversal below $475.53 and then $450.68-$445.13 would support X2 completion.",
                "A motive advance through $504-$511 and $526 would support continued bullish extension.",
                "A break below $349.20 rejects Cycle V beginning there and strongly supports the future Z branch.",
                "A break above $555.45 is the strongest price confirmation that Cycle V is active.",
            ],
        },
        "limitations": [
            "Cboe One volume is not consolidated Nasdaq volume.",
            "Intraday history is unavailable for the earliest waves, so early lower-degree confirmation is incomparable rather than assumed.",
            "Several event gaps prevent exact intrabar ordering; these are explicitly warned rather than silently filled.",
            "The daily high of 499.4399 and extended 15-minute high of 499.33 differ slightly and are not silently merged.",
            "A textbook indicator tendency can support or contradict a count but cannot replace structural validation.",
        ],
    }
    audit["content_hash"] = stable_hash(audit)
    return audit


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def motive_row(result: dict[str, Any]) -> str:
    evidence = result["primary_metrics"].get("evidence", {})
    hard = "PASS" if result["hard_price_status"] == "valid" else "FAIL"
    dry_2 = evidence.get("wave_2_volume_dry_up_vs_wave_1")
    dry_4 = evidence.get("wave_4_volume_dry_up_vs_wave_3")
    return "| " + " | ".join(
        (
            result["title"],
            result["timeframe"] + (" ext" if result["session"] == "extended" else ""),
            hard,
            fmt(evidence.get("wave_3_rsi_stronger_than_wave_1")),
            fmt(evidence.get("wave_3_average_relative_volume_vs_wave_1"), 3),
            f"{fmt(dry_2)}/{fmt(dry_4)}",
            fmt(evidence.get("wave_5_aligned_endpoint_rsi_divergence_vs_wave_3")),
            fmt(evidence.get("wave_5_relative_volume_ratio_to_wave_3"), 3),
        )
    ) + " |"


def correction_row(result: dict[str, Any]) -> str:
    metrics = result["primary_metrics"]
    connectors = metrics.get("connector_evidence", [])
    connector_text = ", ".join(
        f"{item['label']} V={fmt(item['relative_volume_ratio_to_prior_leg'], 2)} "
        f"reset={item['volume_reset_status']}"
        for item in connectors
    ) or "none"
    return "| " + " | ".join(
        (
            result["title"],
            result["family"],
            result["timeframe"],
            connector_text,
            metrics.get("terminal_divergence_status", "unavailable"),
        )
    ) + " |"


def leg_table(result: dict[str, Any]) -> list[str]:
    lines = [
        "| Leg | Path | Bars | RSI end | RSI extreme | Avg rel. volume |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for leg in result["primary_metrics"].get("legs", []):
        lines.append(
            "| "
            + " | ".join(
                (
                    leg["label"],
                    f"${leg['start_price']:.3f} -> ${leg['end_price']:.3f}",
                    str(leg["bars"]),
                    fmt(leg.get("rsi_at_end_bar_close")),
                    fmt(leg.get("rsi_extreme_in_wave_direction")),
                    fmt(leg.get("average_relative_volume_50"), 3),
                )
            )
            + " |"
        )
    return lines


def render_report(audit: dict[str, Any]) -> str:
    motives = {item["sequence_id"]: item for item in audit["motive_sequences"]}
    corrections = {
        item["sequence_id"]: item for item in audit["correction_sequences"]
    }
    lines = [
        "# MSFT All-Wave RSI and Volume Confirmation Audit",
        "",
        "**Symbol:** BATS:MSFT / Nasdaq listing",
        "",
        f"**Cutoff:** {audit['data_cutoff']}",
        "",
        "**Indicators:** Canonical Wilder RSI(14) and same-feed relative volume only",
        "",
        "## Method",
        "",
        "Price structure and Elliott hard rules are evaluated first. RSI and volume are then tested at the parent timeframe and every available lower timeframe. Indicator observations are supportive, contradictory, neutral, unavailable or incomparable; they never hard-invalidate a price-valid structure.",
        "",
        "Cumulative volume is not used to compare unequal-duration waves. The principal volume measure is average bar volume divided by its trailing 50-bar average. Regular and extended sessions are audited separately.",
        "",
        "## Revised Current Hierarchy",
        "",
        "1. Cycle III remains complete at `$555.45`.",
        "2. Cycle IV can be counted as completed `W-X-Y` at `$349.20` or as an active `W-X-Y-X2-Z` in which `X2` is rising.",
        "3. The rally from `$349.20` is a clean shared structure: five to `$395.57`, a complex correction to `$388.75`, and five to `$499.33/$499.44`.",
        "4. That shared structure can be corrective `X2` or the early `1-2-3` of Cycle V. RSI and volume cannot determine the letter.",
        "5. `$499.44` is a candidate pivot, not a confirmed terminal top, because aligned RSI did not diverge and the nested final fifth did not show volume exhaustion.",
        "",
        "## Motive-Wave Audit",
        "",
        "`V3/V1` and `V5/V3` use average relative volume. `V2/V4 dry` reports whether each correction had lower relative volume than the preceding motive leg.",
        "",
        "| Sequence | Main TF | Price rules | RSI W3 stronger | V3/V1 | V2/V4 dry | RSI W5 div. | V5/V3 |",
        "|---|---|---|---|---:|---|---|---:|",
    ]
    lines.extend(motive_row(item) for item in audit["motive_sequences"])
    lines.extend(
        [
            "",
            "Interpretation: a `no` in an indicator column is contradictory or neutral evidence, not structural invalidation. The exact cross-timeframe values remain in the JSON evidence file.",
            "",
            "## Corrective-Family Audit",
            "",
            "| Sequence | Family | Main TF | Connector evidence | Terminal RSI divergence |",
            "|---|---|---|---|---|",
        ]
    )
    lines.extend(correction_row(item) for item in audit["correction_sequences"])
    lines.extend(
        [
            "",
            "## Current Parent Legs",
            "",
            "### Cycle IV W-X-Y-X2-Z Candidate",
            "",
        ]
    )
    lines.extend(leg_table(corrections["cycle_iv_wxyx2_parent"]))
    lines.extend(
        [
            "",
            "Weekly RSI fell hardest during W, reset during X, weakened less during Y, and reached a stronger high during the current X2 candidate. This supports a large corrective sequence but does not confirm that X2 has ended.",
            "",
            "### Current X2 Candidate: 5-3-5",
            "",
        ]
    )
    lines.extend(leg_table(corrections["cycle_iv_x2_candidate"]))

    current_c = motives["cycle_iv_x2_c"]
    current_c_evidence = current_c["primary_metrics"]["evidence"]
    nested = motives["cycle_iv_x2_c_wave_5"]
    nested_evidence = nested["primary_metrics"]["evidence"]
    lines.extend(
        [
            "",
            "### X2-C Motive Detail",
            "",
            f"- Hard price rules: **{current_c['hard_price_status'].upper()}** on arithmetic and logarithmic measurements.",
            f"- Wave 3 RSI stronger than Wave 1: **{fmt(current_c_evidence['wave_3_rsi_stronger_than_wave_1'])}**.",
            f"- Wave 3 relative volume / Wave 1: **{fmt(current_c_evidence['wave_3_average_relative_volume_vs_wave_1'], 3)}**.",
            f"- Wave 5 aligned RSI divergence: **{fmt(current_c_evidence['wave_5_aligned_endpoint_rsi_divergence_vs_wave_3'])}**.",
            f"- Wave 5 relative volume / Wave 3: **{fmt(current_c_evidence['wave_5_relative_volume_ratio_to_wave_3'], 3)}**.",
            f"- Nested terminal Wave 5 aligned RSI divergence: **{fmt(nested_evidence['wave_5_aligned_endpoint_rsi_divergence_vs_wave_3'])}**.",
            f"- Nested terminal Wave 5 relative volume / its Wave 3: **{fmt(nested_evidence['wave_5_relative_volume_ratio_to_wave_3'], 3)}**.",
            "",
            "The larger C wave has lower average relative volume in Wave 5 than Wave 3, but RSI made a higher high. Inside that Wave 5, both RSI and relative volume expanded rather than exhausted. The evidence therefore confirms a valid impulse but does **not** confirm its termination at `$499.33/$499.44`.",
            "",
            "## Competing Counts",
            "",
            "| Candidate | Structural status | RSI/volume reading | What distinguishes it |",
            "|---|---|---|---|",
            "| Active Cycle IV W-X-Y-X2-Z | Structurally valid; Z has not started | Current X2 is a strong 5-3-5 without terminal RSI confirmation | Reversal below `$475.53`, then `$450.68-$445.13`, followed by a five-wave decline |",
            "| Cycle IV completed as W-X-Y at `$349.20`; Cycle V active | Structurally valid | The same five-correction-five footprint is compatible with early 1-2-3 nesting | Extension through `$504-$511`, then `$526`; strongest confirmation above `$555.45` |",
            "| Cycle IV completed as A-B-C at `$349.20`; Cycle V active | Price-valid alternate | First decline also passes an outer-five count, but its internal 5-3-5 W signature is cleaner | Future motive subdivision and break above `$555.45` |",
            "",
            "## Final Verdict",
            "",
            "The completed Cycle I, Cycle II and Cycle III structures remain price-valid after the RSI/volume audit. Their indicator confirmation is mixed at several lower degrees, as expected; none is rejected merely because a textbook divergence is absent.",
            "",
            "For Cycle IV, the earlier simple active-C interpretation is no longer valid after price exceeded `$466.33`. The clean bearish continuation hypothesis is now `W-X-Y-X2-Z`, while the clean bullish hypothesis is that `W-X-Y` completed at `$349.20` and Cycle V began there.",
            "",
            "The current rally is structurally valid and strongly supported by momentum, but its ending is unconfirmed. RSI and volume presently argue against declaring `$499.44` a finished terminal wave without reversal evidence.",
            "",
            "## Limitations",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in audit["limitations"])
    lines.extend(
        [
            "",
            f"**Evidence content hash:** `{audit['content_hash']}`",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-date", default=SNAPSHOT_DATE)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT / "msft_all_wave_rsi_volume_audit_20260805.json",
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        default=ROOT / "MSFT_ALL_WAVE_RSI_VOLUME_AUDIT_2026-08-05.md",
    )
    args = parser.parse_args()

    audit = build_audit(args.snapshot_date)
    args.json_output.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    args.report_output.write_text(render_report(audit), encoding="utf-8")
    print(
        json.dumps(
            {
                "audit_id": audit["audit_id"],
                "motive_sequences": len(audit["motive_sequences"]),
                "correction_sequences": len(audit["correction_sequences"]),
                "json_output": str(args.json_output),
                "report_output": str(args.report_output),
                "content_hash": audit["content_hash"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
