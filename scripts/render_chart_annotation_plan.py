"""Render an immutable ChartAnnotationPlan into deterministic Pine v6 source."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from elliott_ai.chart_annotations import ChartAnnotationPlan  # noqa: E402
from elliott_ai.pine_overlay_renderer import (  # noqa: E402
    PineOverlayRenderResult,
    write_pine_overlay,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a frozen chart-annotation plan and render it as Pine v6. "
            "This command does not analyze or verify Elliott waves."
        )
    )
    parser.add_argument("plan", type=Path, help="Path to ChartAnnotationPlan JSON.")
    parser.add_argument("output", type=Path, help="Destination .pine file.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace a different existing output file atomically.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.plan.read_text(encoding="utf-8"))
        plan = ChartAnnotationPlan.from_dict(raw)
        result = PineOverlayRenderResult.create(plan)
        output = write_pine_overlay(result, args.output, overwrite=args.overwrite)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "status": "rendered",
                "plan_id": plan.plan_id,
                "plan_content_hash": plan.content_hash,
                "render_content_hash": result.content_hash,
                "pine_source_sha256": result.pine_source_sha256,
                "output": str(output.resolve()),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
