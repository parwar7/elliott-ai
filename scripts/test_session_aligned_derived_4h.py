from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from elliott_ai.forecast_records import DatasetCutoff, DatasetHashScope, canonical_sha256
from elliott_ai.lower_timeframe_candidate_generator import (
    NativeCandle,
    NativeOHLCVWindow,
    WindowCoverageRole,
)
from elliott_ai.session_aligned_derived_4h import (
    Derived4hDurationClass,
    Derived4hReasonCode,
    Derived4hReconstructionRequest,
    Derived4hReconstructionResult,
    Derived4hReconstructionStatus,
    IntradaySourceInterval,
    NasdaqSessionSchedule,
    NasdaqTradingSession,
    SourceTimestampSemantics,
    build_derived_4h_pivot_catalog,
    nasdaq_session_aligned_4h_policy,
    reconstruct_session_aligned_4h,
)


UTC = timezone.utc


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _session(
    *,
    trading_date: str,
    open_utc: str,
    close_utc: str,
    early_close: bool,
) -> NasdaqTradingSession:
    return NasdaqTradingSession.create(
        session_id=f"NASDAQ:{trading_date}",
        trading_date=trading_date,
        session_open_utc=open_utc,
        session_close_utc=close_utc,
        is_early_close=early_close,
    )


def _request_for_session(
    session: NasdaqTradingSession,
    *,
    timestamp_semantics: SourceTimestampSemantics = SourceTimestampSemantics.BAR_START,
    omit_interval_index: int | None = None,
    omit_source_index: int | None = None,
    cross_session_interval_index: int | None = None,
    dataset_timezone: str = "America/New_York",
    is_native: bool = True,
) -> tuple[Derived4hReconstructionRequest, NativeOHLCVWindow]:
    start = _utc(session.session_open_utc)
    close = _utc(session.session_close_utc)
    segments: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < close:
        end = min(cursor + timedelta(hours=1), close)
        segments.append((cursor, end))
        cursor = end
    if omit_source_index is not None:
        del segments[omit_source_index]

    dataset = DatasetCutoff(
        dataset_id=f"fixture-source-{session.trading_date}-{len(segments)}",
        source_reference=f"offline://derived-4h/{session.trading_date}",
        timeframe="1h",
        cutoff_utc=_iso(close),
        dataset_hash=canonical_sha256(
            {"fixture": "derived_4h", "date": session.trading_date, "segments": len(segments)}
        ),
        hash_scope=DatasetHashScope.STORED_DATASET_SUMMARY,
        provider="offline_fixture",
        feed_identity="fixture_regular_session",
        requested_symbol="NASDAQ:GOOGL",
        resolved_symbol="GOOGL",
        exchange="NASDAQ",
        session="regular",
        timezone=dataset_timezone,
        adjustment={"splits_adjusted": True, "dividends_adjusted": False},
        price_basis="split_adjusted_dividend_unadjusted_ohlc",
        completed_candles_only=True,
        bar_count=len(segments),
    )
    candles = tuple(
        NativeCandle.create(
            candle_id=f"{session.trading_date}:source:{index}",
            timestamp_utc=_iso(start_at if timestamp_semantics is SourceTimestampSemantics.BAR_START else end_at),
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            volume=1000.0 + index,
        )
        for index, (start_at, end_at) in enumerate(segments)
    )
    source = NativeOHLCVWindow.create(
        window_id=f"fixture-window-{session.trading_date}",
        dataset_cutoff=dataset,
        timeframe="1h",
        candles=candles,
        coverage_role=WindowCoverageRole.REQUIRED_GENERATION,
        is_native=is_native,
    )
    intervals: list[IntradaySourceInterval] = []
    for index, (candle, (start_at, end_at)) in enumerate(zip(candles, segments, strict=True)):
        if index == omit_interval_index:
            continue
        if index == cross_session_interval_index:
            end_at = _utc(session.session_close_utc) + timedelta(minutes=30)
        intervals.append(
            IntradaySourceInterval.create(
                candle_id=candle.candle_id,
                source_row_hash=candle.source_row_hash,
                interval_start_utc=_iso(start_at),
                interval_end_utc=_iso(end_at),
                timestamp_semantics=timestamp_semantics,
            )
        )
    schedule = NasdaqSessionSchedule.create(
        schedule_id=f"fixture-schedule-{session.trading_date}",
        calendar_source="offline_fixture_nasdaq_calendar",
        calendar_version="2026.08.fixture",
        exchange="NASDAQ",
        session="regular",
        timezone="America/New_York",
        sessions=(session,),
    )
    return (
        Derived4hReconstructionRequest.create(
            request_id=f"fixture-request-{session.trading_date}",
            source_window=source,
            source_intervals=tuple(intervals),
            schedule=schedule,
            policy=nasdaq_session_aligned_4h_policy(),
            analysis_cutoff_utc=_iso(close),
        ),
        source,
    )


class SessionAlignedDerived4hTests(unittest.TestCase):
    def test_normal_session_builds_full_and_complete_terminal_buckets(self) -> None:
        session = _session(
            trading_date="2024-07-01",
            open_utc="2024-07-01T13:30:00+00:00",
            close_utc="2024-07-01T20:00:00+00:00",
            early_close=False,
        )
        request, source = _request_for_session(session)

        result = reconstruct_session_aligned_4h(request)

        self.assertEqual(result.status, Derived4hReconstructionStatus.AVAILABLE)
        assert result.window is not None
        self.assertEqual(len(result.window.bars), 2)
        first, terminal = result.window.bars
        self.assertEqual(first.bucket_start_utc, "2024-07-01T13:30:00+00:00")
        self.assertEqual(first.bucket_end_utc, "2024-07-01T17:30:00+00:00")
        self.assertEqual(first.duration_class, Derived4hDurationClass.FULL_4H)
        self.assertEqual(terminal.bucket_start_utc, "2024-07-01T17:30:00+00:00")
        self.assertEqual(terminal.bucket_end_utc, "2024-07-01T20:00:00+00:00")
        self.assertEqual(terminal.duration_seconds, 2 * 60 * 60 + 30 * 60)
        self.assertEqual(terminal.duration_class, Derived4hDurationClass.SESSION_TERMINAL_SHORTENED)
        self.assertTrue(terminal.is_terminal_bucket)
        self.assertEqual(terminal.source_candle_ids, tuple(item.candle_id for item in source.candles[4:]))
        self.assertIn(Derived4hReasonCode.TERMINAL_SHORTENED_ACCEPTED, result.reason_codes)

    def test_early_close_terminal_bucket_is_valid_when_complete(self) -> None:
        session = _session(
            trading_date="2024-07-03",
            open_utc="2024-07-03T13:30:00+00:00",
            close_utc="2024-07-03T17:00:00+00:00",
            early_close=True,
        )
        result = reconstruct_session_aligned_4h(_request_for_session(session)[0])

        self.assertEqual(result.status, Derived4hReconstructionStatus.AVAILABLE)
        assert result.window is not None
        self.assertEqual(len(result.window.bars), 1)
        terminal = result.window.bars[0]
        self.assertEqual(terminal.bucket_start_utc, "2024-07-03T13:30:00+00:00")
        self.assertEqual(terminal.bucket_end_utc, "2024-07-03T17:00:00+00:00")
        self.assertEqual(terminal.duration_seconds, 3 * 60 * 60 + 30 * 60)
        self.assertEqual(terminal.duration_class, Derived4hDurationClass.SESSION_TERMINAL_SHORTENED)
        self.assertTrue(terminal.is_terminal_bucket)

    def test_dst_boundaries_come_from_the_immutable_schedule(self) -> None:
        before_dst = _session(
            trading_date="2024-03-08",
            open_utc="2024-03-08T14:30:00+00:00",
            close_utc="2024-03-08T21:00:00+00:00",
            early_close=False,
        )
        after_dst = _session(
            trading_date="2024-03-11",
            open_utc="2024-03-11T13:30:00+00:00",
            close_utc="2024-03-11T20:00:00+00:00",
            early_close=False,
        )

        before = reconstruct_session_aligned_4h(_request_for_session(before_dst)[0])
        after = reconstruct_session_aligned_4h(_request_for_session(after_dst)[0])

        self.assertEqual(before.status, Derived4hReconstructionStatus.AVAILABLE)
        self.assertEqual(after.status, Derived4hReconstructionStatus.AVAILABLE)
        assert before.window is not None and after.window is not None
        self.assertEqual(before.window.bars[0].bucket_start_utc, "2024-03-08T14:30:00+00:00")
        self.assertEqual(after.window.bars[0].bucket_start_utc, "2024-03-11T13:30:00+00:00")

    def test_missing_timestamp_semantics_returns_not_covered(self) -> None:
        session = _session(
            trading_date="2024-07-01",
            open_utc="2024-07-01T13:30:00+00:00",
            close_utc="2024-07-01T20:00:00+00:00",
            early_close=False,
        )
        result = reconstruct_session_aligned_4h(
            _request_for_session(session, omit_interval_index=3)[0]
        )

        self.assertEqual(result.status, Derived4hReconstructionStatus.NOT_COVERED)
        self.assertIn(Derived4hReasonCode.SOURCE_TIMESTAMP_SEMANTICS_MISSING, result.reason_codes)
        self.assertTrue(result.missing_interval_ids)

    def test_missing_underlying_interval_returns_not_covered_without_repair(self) -> None:
        session = _session(
            trading_date="2024-07-01",
            open_utc="2024-07-01T13:30:00+00:00",
            close_utc="2024-07-01T20:00:00+00:00",
            early_close=False,
        )
        result = reconstruct_session_aligned_4h(
            _request_for_session(session, omit_source_index=2)[0]
        )

        self.assertEqual(result.status, Derived4hReconstructionStatus.NOT_COVERED)
        self.assertIn(Derived4hReasonCode.BUCKET_COVERAGE_MISSING, result.reason_codes)
        self.assertTrue(any(":gap:" in item for item in result.missing_interval_ids))

    def test_cross_session_data_is_inconsistent_and_explicit_bar_end_semantics_work(self) -> None:
        session = _session(
            trading_date="2024-07-01",
            open_utc="2024-07-01T13:30:00+00:00",
            close_utc="2024-07-01T20:00:00+00:00",
            early_close=False,
        )
        crossed = reconstruct_session_aligned_4h(
            _request_for_session(session, cross_session_interval_index=6)[0]
        )
        mismatched = reconstruct_session_aligned_4h(
            _request_for_session(session, timestamp_semantics=SourceTimestampSemantics.BAR_END)[0]
        )

        self.assertEqual(crossed.status, Derived4hReconstructionStatus.INCONSISTENT)
        self.assertIn(Derived4hReasonCode.SOURCE_INTERVAL_CROSSES_SESSION, crossed.reason_codes)
        self.assertEqual(mismatched.status, Derived4hReconstructionStatus.AVAILABLE)

    def test_timestamp_semantics_must_match_the_immutable_source_row(self) -> None:
        session = _session(
            trading_date="2024-07-01",
            open_utc="2024-07-01T13:30:00+00:00",
            close_utc="2024-07-01T20:00:00+00:00",
            early_close=False,
        )
        request, _ = _request_for_session(session)
        original = request.source_intervals[0]
        wrong_semantics = IntradaySourceInterval.create(
            candle_id=original.candle_id,
            source_row_hash=original.source_row_hash,
            interval_start_utc=original.interval_start_utc,
            interval_end_utc=original.interval_end_utc,
            timestamp_semantics=SourceTimestampSemantics.BAR_END,
        )
        invalid_request = Derived4hReconstructionRequest.create(
            request_id="fixture-wrong-timestamp-semantics",
            source_window=request.source_window,
            source_intervals=(wrong_semantics, *request.source_intervals[1:]),
            schedule=request.schedule,
            policy=request.policy,
            analysis_cutoff_utc=request.analysis_cutoff_utc,
        )

        result = reconstruct_session_aligned_4h(invalid_request)

        self.assertEqual(result.status, Derived4hReconstructionStatus.INCONSISTENT)
        self.assertIn(Derived4hReasonCode.SOURCE_INTERVAL_MISMATCH, result.reason_codes)

    def test_incompatible_metadata_and_non_native_source_are_inconsistent(self) -> None:
        session = _session(
            trading_date="2024-07-01",
            open_utc="2024-07-01T13:30:00+00:00",
            close_utc="2024-07-01T20:00:00+00:00",
            early_close=False,
        )
        incompatible = reconstruct_session_aligned_4h(
            _request_for_session(session, dataset_timezone="UTC")[0]
        )
        non_native = reconstruct_session_aligned_4h(
            _request_for_session(session, is_native=False)[0]
        )

        self.assertEqual(incompatible.status, Derived4hReconstructionStatus.INCONSISTENT)
        self.assertIn(Derived4hReasonCode.SOURCE_METADATA_INCOMPATIBLE, incompatible.reason_codes)
        self.assertEqual(non_native.status, Derived4hReconstructionStatus.INCONSISTENT)
        self.assertIn(Derived4hReasonCode.SOURCE_WINDOW_NOT_NATIVE, non_native.reason_codes)

    def test_derived_pivots_and_result_hashes_preserve_derived_lineage(self) -> None:
        session = _session(
            trading_date="2024-07-01",
            open_utc="2024-07-01T13:30:00+00:00",
            close_utc="2024-07-01T20:00:00+00:00",
            early_close=False,
        )
        request, source = _request_for_session(session)
        result = reconstruct_session_aligned_4h(request)

        assert result.window is not None
        pivots = build_derived_4h_pivot_catalog(result.window)
        self.assertTrue(pivots)
        self.assertTrue(all(item.source_window_hash == result.window.derived_rows_hash for item in pivots))
        self.assertTrue(all(item.source_window_hash != source.native_rows_hash for item in pivots))
        self.assertEqual(Derived4hReconstructionResult.from_dict(result.to_dict()), result)
        tampered = result.to_dict()
        tampered["warnings"] = ["tampered"]
        with self.assertRaises(ValueError):
            Derived4hReconstructionResult.from_dict(tampered)


if __name__ == "__main__":
    unittest.main()
