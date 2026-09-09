from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from elliott_ai.adaptive_bar_estimation import (
    BarEstimateQuality,
    estimate_native_bars,
)
from elliott_ai.adaptive_market_data import NativeOHLCVBundle
from elliott_ai.adaptive_recount import (
    AdaptiveRecountBudget,
    AdaptiveRecountCoordinator,
    create_adaptive_recount_plan,
)
from elliott_ai.adaptive_timeframe_planner import (
    TimeframePlanningReason,
)
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import NativeCandle
from elliott_ai.timeframes import (
    MarketSessionMode,
    ProviderBarAlignment,
    ProviderCapabilitySet,
    ProviderTimeframeCapability,
)


STAMP = "2026-08-30T12:00:00+00:00"
ANALYSIS_START = "2026-05-27T00:00:00+00:00"
CONTEXT_START = "2026-01-01T00:00:00+00:00"
CUTOFF = "2026-08-28T20:00:00+00:00"


def equity_capability(
    timeframe: str,
    *,
    alignment: ProviderBarAlignment | None = None,
) -> ProviderTimeframeCapability:
    if alignment is None:
        alignment = (
            ProviderBarAlignment.SESSION_DAILY
            if timeframe == "daily"
            else ProviderBarAlignment.CALENDAR_BUCKET
            if timeframe in {"weekly", "monthly"}
            else ProviderBarAlignment.SESSION_OPEN_ALIGNED_WITH_SHORT_FINAL
        )
    return ProviderTimeframeCapability.create(
        canonical_name=timeframe,
        provider_alias=f"fixture-{timeframe}",
        native_available=True,
        derived=False,
        provider="fixture-feed",
        session_policy="nasdaq_regular_session",
        adjustment_policy="split_adjusted_dividend_unadjusted",
        market_session_mode=MarketSessionMode.REGULAR_SESSION_EQUITY,
        market_calendar_id="XNAS",
        market_calendar_source="elliott_ai.xnas_rules",
        market_calendar_version="1.0.0",
        market_timezone="America/New_York",
        session_start_local="09:30",
        session_end_local="16:00",
        session_minutes=390,
        provider_bar_alignment=alignment,
        shortened_final_bar_emitted=(
            alignment
            is ProviderBarAlignment.SESSION_OPEN_ALIGNED_WITH_SHORT_FINAL
        ),
    )


def equity_capabilities(
    *,
    four_hour_alignment: ProviderBarAlignment = (
        ProviderBarAlignment.SESSION_OPEN_ALIGNED_WITH_SHORT_FINAL
    ),
) -> ProviderCapabilitySet:
    return ProviderCapabilitySet.create(
        provider="fixture-feed",
        capabilities=(
            equity_capability("weekly"),
            equity_capability("daily"),
            equity_capability("4h", alignment=four_hour_alignment),
            equity_capability("1h"),
        ),
        discovery_status="offline_fixture",
        source_interface="scripts.test_adaptive_session_bar_estimation",
        discovered_at_utc=STAMP,
        limitations=(),
    )


def continuous_capability(timeframe: str) -> ProviderTimeframeCapability:
    return ProviderTimeframeCapability.create(
        canonical_name=timeframe,
        provider_alias=f"fixture-{timeframe}",
        native_available=True,
        derived=False,
        provider="crypto-fixture",
        session_policy="continuous_24_7",
        adjustment_policy="unadjusted",
        market_session_mode=MarketSessionMode.CONTINUOUS_24_7,
        provider_bar_alignment=ProviderBarAlignment.ELAPSED_TIME,
    )


def root_trace(*, requested_timeframe: str | None):
    provider_capabilities = equity_capabilities()
    plan = create_adaptive_recount_plan(
        symbol="NASDAQ:RKLB",
        analysis_start_utc=ANALYSIS_START,
        context_start_utc=CONTEXT_START,
        analysis_cutoff_utc=CUTOFF,
        market_data_provider=provider_capabilities.provider,
        provider_capabilities=provider_capabilities,
        created_at_utc=STAMP,
        budgets=AdaptiveRecountBudget(),
    )
    coordinator = AdaptiveRecountCoordinator()
    trace = coordinator.plan_initial_data(
        coordinator.initial_trace(plan),
        created_at_utc=STAMP,
        requested_timeframe=requested_timeframe,
        source_interface=provider_capabilities.source_interface,
    )
    return coordinator, trace


def bundle_with_structural_count(manifest, capabilities, *, count: int):
    structural_start = datetime.fromisoformat(manifest.structural_scoring_start_utc)
    structural_end = datetime.fromisoformat(manifest.structural_scoring_end_utc)
    requested_start = datetime.fromisoformat(manifest.requested_start_utc)
    if count < 2:
        raise ValueError("Fixture count must include at least two structural rows.")
    span = structural_end - structural_start
    structural_timestamps = tuple(
        structural_start + span * index / (count - 1)
        for index in range(count)
    )
    timestamps = (requested_start, *structural_timestamps)
    timestamps = tuple(dict.fromkeys(timestamps))
    candles = tuple(
        NativeCandle.create(
            candle_id=f"bar-{index:04d}",
            timestamp_utc=timestamp.isoformat(),
            open=10.0 + index * 0.01,
            high=10.5 + index * 0.01,
            low=9.5 + index * 0.01,
            close=10.2 + index * 0.01,
            volume=1000.0 + index,
        )
        for index, timestamp in enumerate(timestamps)
    )
    return NativeOHLCVBundle.create(
        bundle_id=f"session-estimate-{manifest.canonical_timeframe}-{count}",
        source_request_id=manifest.request_id,
        source_request_hash=manifest.content_hash,
        provider_capability_set_hash=capabilities.content_hash,
        canonical_symbol=manifest.symbol,
        provider_symbol="RKLB",
        exchange="NASDAQ",
        provider=manifest.provider,
        source_interface=manifest.source_interface,
        feed_identity="fixture-feed",
        session=manifest.session_policy,
        timezone="America/New_York",
        price_adjustment=manifest.adjustment_policy,
        dividend_adjustment="unadjusted",
        volume_adjustment="split_adjusted",
        price_basis="split-adjusted dividend-unadjusted OHLCV",
        timeframe=manifest.canonical_timeframe,
        provider_interval_alias=manifest.provider_interval_alias,
        native=True,
        derived=False,
        requested_start_utc=manifest.requested_start_utc,
        requested_end_utc=manifest.requested_end_utc,
        actual_first_candle_utc=candles[0].timestamp_utc,
        actual_final_completed_candle_utc=candles[-1].timestamp_utc,
        acquisition_timestamp_utc=STAMP,
        analysis_cutoff_utc=manifest.analysis_cutoff_utc,
        timestamp_semantics="bar_open_utc_with_provider_interval",
        completion_verification_method="fixture_completed_session_calendar",
        incomplete_candles_excluded=True,
        source_response_hash=canonical_sha256(
            [item.to_dict() for item in candles]
        ),
        candles=candles,
    )


class SessionAwareEstimateTests(unittest.TestCase):
    def test_regular_equity_daily_excludes_weekends_and_xnas_holidays(self) -> None:
        estimate = estimate_native_bars(
            structural_start_utc=ANALYSIS_START,
            structural_end_utc=CUTOFF,
            capability=equity_capability("daily"),
        )
        self.assertEqual(estimate.estimated_native_bars, 66)
        self.assertNotEqual(estimate.estimated_native_bars, 94)
        self.assertEqual(
            estimate.estimate_quality,
            BarEstimateQuality.SESSION_CALENDAR_ESTIMATE,
        )
        self.assertEqual(estimate.market_calendar_id, "XNAS")

    def test_supported_xnas_holiday_is_not_counted_as_daily_bar(self) -> None:
        estimate = estimate_native_bars(
            structural_start_utc="2026-06-18T00:00:00+00:00",
            structural_end_utc="2026-06-20T23:59:59+00:00",
            capability=equity_capability("daily"),
        )
        self.assertEqual(estimate.estimated_native_bars, 1)

    def test_continuous_market_retains_elapsed_time_estimation(self) -> None:
        estimate = estimate_native_bars(
            structural_start_utc="2026-01-01T00:00:00+00:00",
            structural_end_utc="2026-01-11T00:00:00+00:00",
            capability=continuous_capability("4h"),
        )
        self.assertEqual(estimate.estimated_native_bars, 60)
        self.assertEqual(estimate.estimation_basis.value, "continuous_elapsed_time")

    def test_regular_equity_4h_uses_provider_session_alignment(self) -> None:
        estimate = estimate_native_bars(
            structural_start_utc=ANALYSIS_START,
            structural_end_utc=CUTOFF,
            capability=equity_capability("4h"),
        )
        self.assertEqual(estimate.estimated_native_bars, 132)
        self.assertEqual(estimate.session_minutes, 390)
        self.assertEqual(
            estimate.provider_bar_alignment,
            ProviderBarAlignment.SESSION_OPEN_ALIGNED_WITH_SHORT_FINAL.value,
        )

    def test_daily_and_4h_ranking_uses_session_bar_estimates(self) -> None:
        _coordinator, trace = root_trace(requested_timeframe=None)
        decision = trace.planner_decisions[-1]
        estimates = {
            item.timeframe: item.estimated_native_bars
            for item in decision.assessments
        }
        self.assertEqual(estimates["daily"], 66)
        self.assertEqual(estimates["4h"], 132)
        self.assertEqual(decision.selected_timeframe, "4h")

    def test_alignment_metadata_not_a_hardcoded_4h_preference(self) -> None:
        provider_capabilities = equity_capabilities(
            four_hour_alignment=ProviderBarAlignment.SESSION_OPEN_ALIGNED_FULL_ONLY
        )
        plan = create_adaptive_recount_plan(
            symbol="NASDAQ:RKLB",
            analysis_start_utc=ANALYSIS_START,
            context_start_utc=CONTEXT_START,
            analysis_cutoff_utc=CUTOFF,
            market_data_provider=provider_capabilities.provider,
            provider_capabilities=provider_capabilities,
            created_at_utc=STAMP,
        )
        coordinator = AdaptiveRecountCoordinator()
        trace = coordinator.plan_initial_data(
            coordinator.initial_trace(plan),
            created_at_utc=STAMP,
            source_interface=provider_capabilities.source_interface,
        )
        estimates = {
            item.timeframe: item.estimated_native_bars
            for item in trace.planner_decisions[-1].assessments
        }
        self.assertEqual(estimates["daily"], 66)
        self.assertEqual(estimates["4h"], 66)
        self.assertEqual(trace.planner_decisions[-1].selected_timeframe, "daily")

    def test_context_does_not_change_session_aware_estimates(self) -> None:
        coordinator, trace = root_trace(requested_timeframe=None)
        long_decision = trace.planner_decisions[-1]
        plan = create_adaptive_recount_plan(
            symbol="NASDAQ:RKLB",
            analysis_start_utc=ANALYSIS_START,
            context_start_utc=ANALYSIS_START,
            analysis_cutoff_utc=CUTOFF,
            market_data_provider=trace.plan.provider_capabilities.provider,
            provider_capabilities=trace.plan.provider_capabilities,
            created_at_utc=STAMP,
        )
        short_trace = coordinator.plan_initial_data(
            coordinator.initial_trace(plan),
            created_at_utc=STAMP,
            source_interface=plan.provider_capabilities.source_interface,
        )
        self.assertEqual(
            [(item.timeframe, item.estimated_native_bars) for item in long_decision.assessments],
            [
                (item.timeframe, item.estimated_native_bars)
                for item in short_trace.planner_decisions[-1].assessments
            ],
        )


class RootTimeframeReplanTests(unittest.TestCase):
    def test_material_post_fetch_mismatch_creates_one_bounded_replan(self) -> None:
        coordinator, trace = root_trace(requested_timeframe="daily")
        first_manifest = trace.data_manifests[-1]
        bundle = bundle_with_structural_count(
            first_manifest,
            trace.plan.provider_capabilities,
            count=50,
        )
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)

        self.assertEqual(len(trace.planner_requests), 2)
        self.assertEqual(len(trace.planner_decisions), 2)
        self.assertEqual(len(trace.data_manifests), 2)
        self.assertEqual(
            trace.planner_decisions[-1].reason_code,
            TimeframePlanningReason.ROOT_TIMEFRAME_REPLAN,
        )
        self.assertEqual(trace.data_manifests[-1].canonical_timeframe, "4h")
        self.assertEqual(trace.planner_requests[-1].reason_code, "root_timeframe_replan")

    def test_small_in_target_mismatch_does_not_replan(self) -> None:
        coordinator, trace = root_trace(requested_timeframe=None)
        first_manifest = trace.data_manifests[-1]
        self.assertEqual(first_manifest.canonical_timeframe, "4h")
        bundle = bundle_with_structural_count(
            first_manifest,
            trace.plan.provider_capabilities,
            count=130,
        )
        trace = coordinator.ingest_bundle(trace, bundle, updated_at_utc=STAMP)

        self.assertEqual(len(trace.planner_requests), 1)
        self.assertEqual(len(trace.data_manifests), 1)


if __name__ == "__main__":
    unittest.main()
