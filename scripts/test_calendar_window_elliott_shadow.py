"""Focused tests for the twelve-completed-Monthly GOOGL calendar policy."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from elliott_ai.calendar_window_elliott_shadow import (
    CALENDAR_WINDOW_COMPLETED_MONTHS,
    CALENDAR_WINDOW_STAGE1_CALL_BUDGET,
    CalendarWindowElliottApproval,
    CalendarWindowElliottShadowError,
    CalendarWindowElliottShadowRunner,
    _CalendarContextBoundCandidateProvider,
    build_calendar_window_elliott_preflight_from_bundle,
)
from elliott_ai.complete_history_shadow_runner import load_complete_history_bundle
from elliott_ai.scoped_elliott_shadow_runner import ScopedCoverageStatus


_ROOT = Path(__file__).resolve().parents[1]
_BUNDLE_DIRECTORY = _ROOT / "phase11_input" / "GOOGL_full_history_bundle_20260810T175555Z"
_FROZEN_CUTOFF = "2026-08-07T20:00:00Z"


class _CaptureProvider:
    name = "openai"
    model = "gpt-5.6-terra"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

    def generate_monthly_map(self, *, role, packet, output_schema):
        self.calls.append(("monthly", role, packet))
        return {}

    def generate_weekly_child_graph_batch(self, *, role, packet, output_schema):
        self.calls.append(("weekly", role, packet))
        return {}


class CalendarWindowElliottShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_complete_history_bundle(_BUNDLE_DIRECTORY)

    def preflight(self):
        return CalendarWindowElliottShadowRunner().preflight(
            _BUNDLE_DIRECTORY,
            created_at_utc=_FROZEN_CUTOFF,
        )

    def test_last_twelve_completed_months_are_calendar_context_not_a_fake_wave_origin(self) -> None:
        preflight = self.preflight()
        scope = preflight.calendar_scope

        self.assertEqual(scope.completed_months, CALENDAR_WINDOW_COMPLETED_MONTHS)
        self.assertEqual(scope.calendar_monthly_start_utc, "2025-08-01T00:00:00+00:00")
        self.assertEqual(scope.calendar_monthly_end_utc, "2026-07-01T00:00:00+00:00")
        self.assertEqual(scope.analysis_cutoff_utc, "2026-08-07T20:00:00+00:00")
        self.assertEqual(len(preflight.calendar_monthly_window.candles), 12)
        self.assertEqual(len(preflight.context_monthly_window.candles), 3)
        self.assertEqual(scope.context_start_utc, "2025-08-01T00:00:00+00:00")
        self.assertEqual(scope.context_end_exclusive_utc, "2025-11-01T00:00:00+00:00")
        self.assertEqual(scope.first_structural_source_pivot.timestamp_utc, "2025-11-01T00:00:00+00:00")
        self.assertEqual(scope.first_structural_source_pivot.price, 328.82999)
        self.assertEqual(scope.first_structural_source_pivot.price_field.value, "high")
        self.assertEqual(
            preflight.structural_preflight.monthly_window.candles[0].candle_id,
            "GOOGL-full-history-20260810T175555Z:monthly:255",
        )
        self.assertNotIn(
            preflight.calendar_monthly_window.candles[0].candle_id,
            {item.candle_id for item in preflight.structural_preflight.monthly_window.candles},
        )

    def test_calendar_window_coverage_is_explicit_and_intraday_remains_nonstructural(self) -> None:
        preflight = self.preflight()
        coverage = {item.timeframe: item for item in preflight.calendar_scope.coverage}

        for timeframe in ("monthly", "weekly", "daily", "4h"):
            self.assertEqual(coverage[timeframe].status, ScopedCoverageStatus.COMPLETE)
            self.assertTrue(coverage[timeframe].required_for_proof)
        self.assertEqual(coverage["1h"].status, ScopedCoverageStatus.COMPLETE)
        self.assertFalse(coverage["1h"].required_for_proof)
        self.assertEqual(coverage["15m"].status, ScopedCoverageStatus.PARTIAL_OPTIONAL)
        self.assertFalse(coverage["15m"].required_for_proof)

    def test_plan_is_deterministic_and_reserves_exactly_four_possible_calls(self) -> None:
        first = self.preflight()
        second = self.preflight()

        self.assertEqual(first.calendar_scope.content_hash, second.calendar_scope.content_hash)
        self.assertEqual(first.plan.content_hash, second.plan.content_hash)
        self.assertEqual(first.plan.total_reserved_model_calls, CALENDAR_WINDOW_STAGE1_CALL_BUDGET)
        self.assertEqual(
            first.plan.structural_scoped_plan_content_hash,
            first.structural_preflight.plan.content_hash,
        )
        self.assertEqual(
            first.plan.rules_pack_content_hash,
            first.structural_preflight.plan.policy.blind_candidate_rules_pack.content_hash,
        )

    def test_future_approval_binds_calendar_context_and_the_structural_start_pivot(self) -> None:
        preflight = self.preflight()
        approval = CalendarWindowElliottShadowRunner.approval_template(
            preflight,
            approved_by="fixture-reviewer",
            approved_at_utc=_FROZEN_CUTOFF,
            provider="openai",
            model="gpt-5.6-terra",
        )

        self.assertEqual(approval.calendar_plan_content_hash, preflight.plan.content_hash)
        self.assertEqual(approval.calendar_scope_content_hash, preflight.calendar_scope.content_hash)
        self.assertEqual(
            approval.structural_start_pivot_content_hash,
            preflight.calendar_scope.first_structural_scoped_pivot.content_hash,
        )
        self.assertEqual(approval.exact_call_limit, CALENDAR_WINDOW_STAGE1_CALL_BUDGET)

    def test_invalid_calendar_length_cannot_silently_create_a_structural_scope(self) -> None:
        with self.assertRaises(ValueError):
            build_calendar_window_elliott_preflight_from_bundle(
                self.bundle,
                completed_months=1,
                created_at_utc=_FROZEN_CUTOFF,
            )

    def test_scope_hash_tampering_is_rejected(self) -> None:
        preflight = self.preflight()
        with self.assertRaises(ValueError):
            replace(preflight.calendar_scope, content_hash="0" * 64)

    def test_context_is_injected_without_becoming_candidate_pivot_data(self) -> None:
        preflight = self.preflight()
        provider = _CaptureProvider()
        bound = _CalendarContextBoundCandidateProvider(provider, preflight)
        structural_pivot = preflight.calendar_scope.first_structural_scoped_pivot.to_dict()
        bound.generate_monthly_map(
            role="primary",
            packet={
                "packet_kind": "fixture",
                "pivot_catalog": [structural_pivot],
                "constraints": {"blind": True},
            },
            output_schema={},
        )

        self.assertEqual(len(provider.calls), 1)
        sent = provider.calls[0][2]
        context = sent["calendar_window_context"]
        self.assertEqual(context["classification"], "context_before_first_confirmed_pivot")
        self.assertEqual(context["context_start_utc"], "2025-08-01T00:00:00+00:00")
        self.assertEqual(context["context_end_exclusive_utc"], "2025-11-01T00:00:00+00:00")
        self.assertEqual(len(context["context_candles"]), 3)
        self.assertIn("candidate-wave origin", context["instructions"][1])
        self.assertEqual(sent["pivot_catalog"], [structural_pivot])
        structural_source_hashes = {item["source_bar_hash"] for item in sent["pivot_catalog"]}
        self.assertNotIn(
            preflight.context_monthly_window.candles[0].source_row_hash,
            structural_source_hashes,
        )

    def test_execution_is_model_disabled_by_default(self) -> None:
        preflight = self.preflight()
        approval = CalendarWindowElliottShadowRunner.approval_template(
            preflight,
            approved_by="fixture-reviewer",
            approved_at_utc=_FROZEN_CUTOFF,
            provider="openai",
            model="gpt-5.6-terra",
        )
        provider = _CaptureProvider()
        with self.assertRaises(CalendarWindowElliottShadowError):
            CalendarWindowElliottShadowRunner().execute(
                preflight,
                provider=provider,
                approval=approval,
                allow_model_calls=False,
            )
        self.assertEqual(provider.calls, [])

    def test_execution_rejects_a_different_calendar_context_before_a_provider_call(self) -> None:
        preflight = self.preflight()
        approval = CalendarWindowElliottShadowRunner.approval_template(
            preflight,
            approved_by="fixture-reviewer",
            approved_at_utc=_FROZEN_CUTOFF,
            provider="openai",
            model="gpt-5.6-terra",
        )
        altered = CalendarWindowElliottApproval.create(
            **{
                **approval.to_dict(),
                "calendar_scope_content_hash": "0" * 64,
                "content_hash": "",
            }
        )
        provider = _CaptureProvider()
        with self.assertRaises(CalendarWindowElliottShadowError):
            CalendarWindowElliottShadowRunner().execute(
                preflight,
                provider=provider,
                approval=altered,
                allow_model_calls=True,
            )
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
