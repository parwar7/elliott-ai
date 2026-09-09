from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elliott_ai.degrees import validate_degree_hierarchy_rules
from scripts.audit_tmc_count_20260726 import (
    SNAPSHOT_DATE,
    Boundary,
    load_snapshot,
    motive_by_timeframe,
    ratio,
)
from scripts.audit_tsla_count_20260721 import impulse_rules


def b(timestamp: str, price: float) -> Boundary:
    return Boundary(timestamp, price)


# Intraday endpoints use the BATS:TMC 15-minute snapshot. The daily series reports
# 4.75 for 17 November while the intraday series reports 4.76.
W_A = [
    b("2025-10-13T14:00:00+00:00", 11.350),
    b("2025-10-15T17:00:00+00:00", 9.310),
    b("2025-10-16T13:30:00+00:00", 9.860),
    b("2025-10-17T15:30:00+00:00", 8.010),
    b("2025-10-20T13:30:00+00:00", 8.575),
    b("2025-10-22T16:30:00+00:00", 6.975),
]
W_B = [
    W_A[-1],
    b("2025-10-23T16:15:00+00:00", 7.390),
    b("2025-10-23T19:30:00+00:00", 7.125),
    b("2025-10-24T13:30:00+00:00", 7.540),
]
W_C = [
    W_B[-1],
    b("2025-10-27T13:30:00+00:00", 6.650),
    b("2025-10-30T16:45:00+00:00", 7.290),
    b("2025-11-07T14:30:00+00:00", 5.345),
    b("2025-11-10T17:45:00+00:00", 6.130),
    b("2025-11-17T19:30:00+00:00", 4.760),
]

X_W = [
    W_C[-1],
    b("2025-11-20T14:45:00+00:00", 5.980),
    b("2025-11-21T15:30:00+00:00", 4.880),
    b("2025-12-08T16:30:00+00:00", 8.140),
]
X_W_C = [
    X_W[2],
    b("2025-11-28T17:15:00+00:00", 7.060),
    b("2025-12-01T15:30:00+00:00", 6.260),
    b("2025-12-03T20:45:00+00:00", 7.740),
    b("2025-12-04T14:30:00+00:00", 7.310),
    X_W[-1],
]
X_X = [
    X_W[-1],
    b("2025-12-15T16:45:00+00:00", 6.290),
    b("2025-12-22T14:30:00+00:00", 8.130),
    b("2025-12-31T20:30:00+00:00", 6.105),
]
X_X_C = [
    X_X[2],
    b("2025-12-23T17:45:00+00:00", 7.125),
    b("2025-12-24T17:30:00+00:00", 7.745),
    b("2025-12-26T20:30:00+00:00", 6.610),
    b("2025-12-29T14:30:00+00:00", 6.820),
    X_X[-1],
]
X_Y = [
    X_X[-1],
    b("2026-01-14T19:45:00+00:00", 8.135),
    b("2026-01-20T15:00:00+00:00", 7.000),
    b("2026-01-22T14:30:00+00:00", 10.050),
]
X_Y_A = [
    X_Y[0],
    b("2026-01-02T14:45:00+00:00", 6.600),
    b("2026-01-02T15:00:00+00:00", 6.380),
    b("2026-01-07T16:45:00+00:00", 7.805),
    b("2026-01-09T20:15:00+00:00", 6.890),
    X_Y[1],
]

Y_W = [
    X_Y[-1],
    b("2026-02-05T20:45:00+00:00", 5.630),
    b("2026-02-26T16:00:00+00:00", 6.670),
    b("2026-03-30T13:45:00+00:00", 3.930),
]
Y_W_A = [
    Y_W[0],
    b("2026-01-27T14:45:00+00:00", 7.450),
    b("2026-01-28T19:45:00+00:00", 8.560),
    b("2026-01-30T18:30:00+00:00", 6.600),
    b("2026-02-02T14:30:00+00:00", 7.250),
    Y_W[1],
]
Y_W_B = [
    Y_W[1],
    b("2026-02-09T20:30:00+00:00", 6.670),
    b("2026-02-17T15:00:00+00:00", 5.585),
    Y_W[2],
]
Y_W_B_C = [
    Y_W_B[2],
    b("2026-02-19T20:45:00+00:00", 6.110),
    b("2026-02-23T14:30:00+00:00", 5.760),
    b("2026-02-25T16:30:00+00:00", 6.515),
    b("2026-02-26T14:30:00+00:00", 6.280),
    Y_W_B[-1],
]
Y_W_C = [
    Y_W[2],
    b("2026-03-06T14:45:00+00:00", 5.540),
    b("2026-03-10T17:30:00+00:00", 6.535),
    b("2026-03-20T19:45:00+00:00", 4.890),
    b("2026-03-23T13:30:00+00:00", 5.165),
    Y_W[-1],
]

Y_X_W = [
    Y_W[-1],
    b("2026-04-01T13:30:00+00:00", 4.885),
    b("2026-04-13T13:45:00+00:00", 4.240),
    b("2026-04-22T18:45:00+00:00", 5.820),
]
Y_X_X = [
    Y_X_W[-1],
    b("2026-04-27T13:30:00+00:00", 5.020),
    b("2026-04-27T19:30:00+00:00", 5.280),
    b("2026-04-29T14:00:00+00:00", 4.875),
]
Y_X_Y = [
    Y_X_X[-1],
    b("2026-05-06T19:45:00+00:00", 6.215),
    b("2026-05-19T14:15:00+00:00", 4.850),
    b("2026-06-02T15:15:00+00:00", 6.640),
]
Y_X_Y_C = [
    Y_X_Y[2],
    b("2026-05-26T14:15:00+00:00", 5.725),
    b("2026-05-27T13:45:00+00:00", 5.430),
    b("2026-05-28T16:15:00+00:00", 6.585),
    b("2026-05-29T14:15:00+00:00", 5.850),
    Y_X_Y[-1],
]

Y_Y = [
    Y_X_Y[-1],
    b("2026-06-09T16:30:00+00:00", 4.785),
    b("2026-06-12T14:45:00+00:00", 5.815),
    b("2026-07-17T13:30:00+00:00", 3.570),
]
Y_Y_A = [
    Y_Y[0],
    b("2026-06-03T16:30:00+00:00", 6.040),
    b("2026-06-03T19:30:00+00:00", 6.280),
    b("2026-06-05T18:45:00+00:00", 5.080),
    b("2026-06-08T13:30:00+00:00", 5.280),
    Y_Y[1],
]
Y_Y_B = [
    Y_Y[1],
    b("2026-06-10T13:45:00+00:00", 5.045),
    b("2026-06-10T19:15:00+00:00", 4.805),
    Y_Y[2],
]
Y_Y_C = [
    Y_Y[2],
    b("2026-06-18T16:45:00+00:00", 5.000),
    b("2026-06-22T14:00:00+00:00", 5.215),
    b("2026-07-08T15:15:00+00:00", 3.845),
    b("2026-07-10T17:15:00+00:00", 4.295),
    Y_Y[-1],
]

MOTIVE_SEQUENCES = {
    "W.A": W_A,
    "W.C": W_C,
    "X.w.C": X_W_C,
    "X.x.C": X_X_C,
    "X.y.A": X_Y_A,
    "Y.w.A": Y_W_A,
    "Y.w.B.C": Y_W_B_C,
    "Y.w.C": Y_W_C,
    "Y.x.Y.C": Y_X_Y_C,
    "Y.y.A": Y_Y_A,
    "Y.y.C": Y_Y_C,
}


def point(boundary: Boundary) -> dict[str, Any]:
    return {"timestamp": boundary.timestamp, "price": boundary.price}


def path(boundaries: list[Boundary]) -> list[dict[str, Any]]:
    return [point(item) for item in boundaries]


def wave_node(
    wave_id: str,
    structure: str,
    start: Boundary,
    end: Boundary,
    *,
    position: str | None = None,
    children: list[str] | None = None,
    verified: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "wave_id": wave_id,
        "structure": structure,
        "direction": "up" if end.price > start.price else "down",
        "start": {"date": start.timestamp, "price": start.price},
        "end": {"date": end.timestamp, "price": end.price},
        "child_structure_status": "verified" if verified else "candidate",
    }
    if position is not None:
        result["sequence_position"] = position
    if children:
        result["child_wave_ids"] = children
    return result


def correction_semantics_audit() -> list[str]:
    nodes = [
        wave_node(
            "I4",
            "double-three W-X-Y",
            W_A[0],
            Y_Y[-1],
            children=["I4.W", "I4.X", "I4.Y"],
            verified=True,
        ),
        wave_node("I4.W", "zigzag", W_A[0], W_C[-1], position="W"),
        wave_node("I4.X", "double-three W-X-Y", X_W[0], X_Y[-1], position="X"),
        wave_node("I4.Y", "double-three W-X-Y", Y_W[0], Y_Y[-1], position="Y"),
    ]
    return validate_degree_hierarchy_rules({"degree_hierarchy": nodes})


def sequence_report(
    boundaries: list[Boundary],
    snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
) -> dict[str, Any]:
    return {
        "boundaries": path(boundaries),
        "hard_price_rules": impulse_rules([item.price for item in boundaries]),
        "rsi_volume": motive_by_timeframe(boundaries, snapshots),
    }


def build_audit() -> dict[str, Any]:
    snapshots = {
        timeframe: load_snapshot(timeframe)
        for timeframe in ("weekly", "daily", "4h", "1h", "30m", "15m")
    }
    motive = {
        name: sequence_report(boundaries, snapshots)
        for name, boundaries in MOTIVE_SEQUENCES.items()
    }

    completed_abc_diagonal = [10.05, 5.63, 6.67, 3.93, 6.64, 3.57]
    completed_abc_rules = impulse_rules(completed_abc_diagonal)
    completed_abc_valid = (
        completed_abc_rules["wave_3_not_shortest_arithmetic"]
        and completed_abc_rules["wave_3_not_shortest_log"]
    )

    return {
        "analysis_id": "tmc-correction-depth-20260726-v1",
        "symbol": "BATS:TMC",
        "data_cutoff": snapshots["daily"][1][-1]["timestamp"].isoformat(),
        "method": {
            "price_first": True,
            "indicators": "Wilder RSI(14) and same-feed relative volume are confirmation only",
            "scale": "arithmetic wave ratios; logarithmic motive-length rule cross-check",
            "uncertainty_policy": "Preserve structurally valid alternatives and mark unresolved internals.",
        },
        "feed_note": {
            "daily_2025_11_17_low": 4.75,
            "intraday_2025_11_17_low": 4.76,
            "handling": "The outer daily endpoint remains 4.75; intraday subdivisions use 4.76. They are not silently substituted.",
        },
        "preferred_tree": {
            "Intermediate_4": {
                "family": "double-three W-X-Y",
                "status": "price-complete candidate; reversal not confirmed",
                "W": {
                    "family": "zigzag 5-3-5",
                    "A": {"family": "impulse", "path": path(W_A)},
                    "B": {"family": "zigzag candidate", "path": path(W_B)},
                    "C": {"family": "impulse", "path": path(W_C)},
                    "ratios": {
                        "B_retracement_of_A": ratio(
                            W_B[-1].price - W_B[0].price,
                            W_A[0].price - W_A[-1].price,
                        ),
                        "C_to_A": ratio(
                            W_C[0].price - W_C[-1].price,
                            W_A[0].price - W_A[-1].price,
                        ),
                    },
                },
                "X": {
                    "family": "double-three w-x-y",
                    "w": {"family": "zigzag", "path": path(X_W)},
                    "x": {"family": "regular-flat candidate", "path": path(X_X)},
                    "y": {
                        "family": "zigzag; terminal C diagonal/impulse unresolved",
                        "path": path(X_Y),
                    },
                },
                "Y": {
                    "family": "double-three w-x-y",
                    "w": {
                        "family": "zigzag 5-3-5",
                        "A": {"family": "impulse", "path": path(Y_W_A)},
                        "B": {
                            "family": "flat 3-3-5 candidate",
                            "path": path(Y_W_B),
                        },
                        "C": {"family": "impulse", "path": path(Y_W_C)},
                    },
                    "x": {
                        "family": "double-three W-X-Y",
                        "W": {"family": "zigzag candidate", "path": path(Y_X_W)},
                        "X": {"family": "zigzag candidate", "path": path(Y_X_X)},
                        "Y": {
                            "family": "expanded-flat candidate",
                            "path": path(Y_X_Y),
                        },
                    },
                    "y": {
                        "family": "zigzag 5-3-5",
                        "A": {"family": "impulse", "path": path(Y_Y_A)},
                        "B": {"family": "zigzag", "path": path(Y_Y_B)},
                        "C": {"family": "impulse", "path": path(Y_Y_C)},
                    },
                },
            }
        },
        "motive_verification": motive,
        "hierarchy_validation": {
            "errors": correction_semantics_audit(),
            "note": "The top-level family contract validates. Candidate internals remain explicitly unverified where exact subwaves are unavailable.",
        },
        "alternate_outer_ABC": {
            "status": (
                "completed form structurally invalid on selected diagonal pivots"
                if not completed_abc_valid
                else "structurally valid completed alternate"
            ),
            "supporting_evidence": [
                "The 11.35 to 4.75/4.76 decline also admits a motive five-wave interpretation.",
                "The 4.75/4.76 to 10.05 recovery is corrective.",
            ],
            "contradictory_evidence": [
                "A completed C ending diagonal using 10.05-5.63-6.67-3.93-6.64-3.57 makes its third actionary wave the shortest.",
                "The 10.05 to 3.57 decline has a clearer completed W-X-Y corrective footprint than a completed motive C footprint.",
            ],
            "completed_C_diagonal_test": {
                "pivots": completed_abc_diagonal,
                "hard_price_rules": completed_abc_rules,
            },
            "still_valid_unresolved_form": (
                "A larger ABC remains possible only if C is still developing and the current low is not its completed fifth wave."
            ),
            "evidence_needed": [
                "A confirmed reversal above 4.785 and then 6.64 supports completed W-X-Y.",
                "Failure below 3.57 without a durable reversal keeps extension and active-C alternatives open.",
            ],
        },
        "current_state": {
            "working_endpoint": 3.57,
            "status": "termination candidate",
            "confirmation_levels": [4.06, 4.295, 4.785, 6.64],
            "invalidation_levels": {
                "immediate_reversal_sequence": 3.57,
                "standard_Primary_I_impulse": 3.20,
            },
        },
    }


def main() -> None:
    output = ROOT / "tmc_correction_depth_20260726.json"
    result = build_audit()
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "analysis_id": result["analysis_id"],
                "hierarchy_errors": len(result["hierarchy_validation"]["errors"]),
                "output": str(output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
