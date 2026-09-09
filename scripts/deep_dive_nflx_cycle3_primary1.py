"""Deep subdivision audit of NFLX Cycle III, Primary 1."""

from __future__ import annotations

import json
from pathlib import Path

from recount_nflx_book_first_20260718 import ROOT, move, validate_impulse


def correction(start: float, a: float, b: float, c: float) -> dict[str, object]:
    a_length = abs(start - a)
    c_length = abs(b - c)
    return {
        "path": [start, a, b, c],
        "b_retracement_pct": round(abs(b - a) / a_length * 100, 2),
        "c_to_a": round(c_length / a_length, 3),
        "classification": "probable zigzag geometry; lower-timeframe 5-3-5 proof unavailable",
        "status": "Unproven Internal Structure below weekly resolution",
    }


def main() -> None:
    structures = {
        "primary_1": {
            "degree": "Primary",
            "path": [16.27, 48.50, 35.21, 106.45, 85.39, 134.12],
            "validation": validate_impulse([16.27, 48.50, 35.21, 106.45, 85.39, 134.12]),
        },
        "intermediate_1": {
            "degree": "Intermediate",
            "path": [16.27, 25.20, 21.17, 37.94, 28.53, 48.50],
            "validation": validate_impulse([16.27, 25.20, 21.17, 37.94, 28.53, 48.50]),
        },
        "intermediate_2": correction(48.50, 39.82, 45.35, 35.21),
        "intermediate_3": {
            "degree": "Intermediate",
            "path": [35.21, 63.80, 54.20, 93.53, 82.35, 106.45],
            "validation": validate_impulse([35.21, 63.80, 54.20, 93.53, 82.35, 106.45]),
        },
        "intermediate_3_minor_3": {
            "degree": "Minor",
            "path": [54.20, 69.75, 60.84, 90.80, 85.89, 93.53],
            "validation": validate_impulse([54.20, 69.75, 60.84, 90.80, 85.89, 93.53]),
        },
        "intermediate_4": correction(106.45, 85.81, 99.87, 85.39),
        "intermediate_5": {
            "degree": "Intermediate",
            "path": [85.39, 95.14, 90.67, 126.28, 118.06, 134.12],
            "validation": validate_impulse([85.39, 95.14, 90.67, 126.28, 118.06, 134.12]),
        },
        "intermediate_5_minor_3": {
            "degree": "Minor",
            "path": [90.67, 115.94, 110.29, 121.18, 117.63, 126.28],
            "validation": validate_impulse([90.67, 115.94, 110.29, 121.18, 117.63, 126.28]),
        },
    }
    for name, structure in structures.items():
        if "validation" in structure:
            assert structure["validation"]["valid"], name

    primary_lengths = {
        "intermediate_1": move(16.27, 48.50),
        "intermediate_3": move(35.21, 106.45),
        "intermediate_5": move(85.39, 134.12),
    }
    result = {
        "symbol": "NASDAQ:NFLX",
        "parent": "Cycle III Primary 1",
        "parent_path": ["2022-05-09", 16.27, "2025-06-30", 134.12],
        "method": "Locked-parent deep dive using weekly data for 2022-2025 and daily data from 2025-03-20 onward",
        "structures": structures,
        "actionary_lengths": primary_lengths,
        "data_limit": "Minute counts before 2025-03-20 are not promoted because daily/intraday source bars are unavailable in the current export.",
        "confidence": {
            "primary_1_impulse": "high",
            "intermediate_1_price_structure": "high",
            "intermediate_2_exact_family": "medium-low",
            "intermediate_3_price_structure": "high",
            "intermediate_4_exact_family": "medium-low",
            "intermediate_5_price_structure": "high",
            "minute_counts_before_2025_03_20": "not claimed",
        },
    }
    (ROOT / "nflx_cycle3_primary1_deep_dive_2026-07-19.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )

    report = """# NFLX Cycle III Primary 1 Deep Dive

**Locked parent:** $16.27 (9 May 2022) to $134.12 (30 June 2025)  
**Status:** Completed five-wave Primary impulse

## Intermediate Degree

| Wave | Path | Classification |
|---|---:|---|
| (1) | $16.27 -> $48.50 | Impulse |
| (2) | $48.50 -> $35.21 | Probable zigzag |
| (3) | $35.21 -> $106.45 | Impulse |
| (4) | $106.45 -> $85.39 | Probable zigzag |
| (5) | $85.39 -> $134.12 | Impulse |

All mandatory Primary-impulse rules pass. Intermediate (3) travels beyond (1), is not shortest, and Intermediate (4) remains above the $48.50 Wave-(1) high.

## Intermediate (1)

| Minor | Path |
|---|---:|
| 1 | $16.27 -> $25.20 |
| 2 | $25.20 -> $21.17 |
| 3 | $21.17 -> $37.94 |
| 4 | $37.94 -> $28.53 |
| 5 | $28.53 -> $48.50 |

This passes all impulse rules. Minor 4 remains above the $25.20 Minor-1 high and Minor 3 is not shortest.

## Intermediate (2)

Probable zigzag geometry:

**A:** $48.50 -> $39.82  
**B:** $39.82 -> $45.35  
**C:** $45.35 -> $35.21

B retraced 63.71% of A and C measured 1.168 x A. The geometry is strong, but the exact 5-3-5 internals remain `Unproven Internal Structure` because the current export has no daily bars for 2023.

## Intermediate (3)

| Minor | Path |
|---|---:|
| 1 | $35.21 -> $63.80 |
| 2 | $63.80 -> $54.20 |
| 3 | $54.20 -> $93.53 |
| 4 | $93.53 -> $82.35 |
| 5 | $82.35 -> $106.45 |

Minor 3 is the extended center. Its visible Minute structure is:

`$54.20 -> $69.75 -> $60.84 -> $90.80 -> $85.89 -> $93.53`

This nested five passes the price rules. Deeper Minute labeling outside this strongest segment is not promoted without daily source bars.

## Intermediate (4)

Probable zigzag geometry:

**A:** $106.45 -> $85.81  
**B:** $85.81 -> $99.87  
**C:** $99.87 -> $85.39

B retraced 68.12% of A and C measured 0.702 x A. As with Intermediate (2), the family is probable rather than proven below weekly resolution.

## Intermediate (5)

| Minor | Path |
|---|---:|
| 1 | $85.39 -> $95.14 |
| 2 | $95.14 -> $90.67 |
| 3 | $90.67 -> $126.28 |
| 4 | $126.28 -> $118.06 |
| 5 | $118.06 -> $134.12 |

Minor 3 is extended. Daily bars expose its Minute sequence:

`$90.67 -> $115.94 -> $110.29 -> $121.18 -> $117.63 -> $126.28`

The nested sequence is valid: Minute iv remains above Minute i and Minute iii is not shortest by percentage movement.

## Verdict

Primary 1 of Cycle III is a strong completed impulse. The most defensible map reaches Minor degree across the whole parent and Minute degree inside the clearest extended thirds. Intermediate (2) and (4) have strong zigzag geometry, but their exact 5-3-5 internals should remain provisional until daily data for those historical windows is exported.
"""
    (ROOT / "NFLX_Cycle_III_Primary_1_Deep_Dive_2026-07-19.md").write_text(
        report, encoding="utf-8"
    )
    print(json.dumps({"report": "NFLX_Cycle_III_Primary_1_Deep_Dive_2026-07-19.md", "json": "nflx_cycle3_primary1_deep_dive_2026-07-19.json"}))


if __name__ == "__main__":
    main()
