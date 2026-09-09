"""Session-aware native-bar estimates for adaptive timeframe planning.

The estimator never treats an exchange schedule as price evidence.  It uses
versioned provider/session metadata only to estimate how many completed native
bars can exist inside a structural interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
import math
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .timeframes import (
    MarketSessionMode,
    ProviderBarAlignment,
    ProviderTimeframeCapability,
    timeframe_duration_seconds,
)


ADAPTIVE_BAR_ESTIMATION_POLICY_VERSION = "adaptive-bar-estimation-policy-1.0.0"
XNAS_RULE_CALENDAR_VERSION = "xnas-rules-1.0.0"


class BarEstimationBasis(StrEnum):
    CONTINUOUS_ELAPSED_TIME = "continuous_elapsed_time"
    EXCHANGE_SESSION_CALENDAR = "exchange_session_calendar"
    PROVIDER_SESSION_ALIGNMENT = "provider_session_alignment"
    SESSION_CONTAINING_CALENDAR_BUCKETS = "session_containing_calendar_buckets"
    WEEKDAY_SESSION_FALLBACK = "weekday_session_fallback"
    WALL_CLOCK_FALLBACK = "wall_clock_fallback"
    UNAVAILABLE = "unavailable"


class BarEstimateQuality(StrEnum):
    EXACT_CALENDAR = "exact_calendar"
    PROVIDER_DECLARED = "provider_declared"
    SESSION_CALENDAR_ESTIMATE = "session_calendar_estimate"
    COARSE_FALLBACK = "coarse_fallback"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class NativeBarEstimate:
    estimated_native_bars: int
    estimation_basis: BarEstimationBasis
    market_calendar_id: str | None
    session_policy: str
    session_minutes: int | None
    provider_bar_alignment: str
    estimate_quality: BarEstimateQuality
    assumptions: tuple[str, ...]
    structural_interval_start_utc: str
    structural_interval_end_utc: str

    def __post_init__(self) -> None:
        if not isinstance(self.estimated_native_bars, int) or self.estimated_native_bars < 0:
            raise ValueError("estimated_native_bars must be a non-negative integer.")
        object.__setattr__(self, "estimation_basis", BarEstimationBasis(self.estimation_basis))
        object.__setattr__(self, "estimate_quality", BarEstimateQuality(self.estimate_quality))
        object.__setattr__(self, "assumptions", tuple(str(item) for item in self.assumptions))
        object.__setattr__(
            self,
            "structural_interval_start_utc",
            _utc_text(self.structural_interval_start_utc),
        )
        object.__setattr__(
            self,
            "structural_interval_end_utc",
            _utc_text(self.structural_interval_end_utc),
        )


@dataclass(frozen=True, slots=True)
class _SessionWindow:
    trading_date: date
    open_utc: datetime
    close_utc: datetime
    minutes: int


def _utc_text(value: Any) -> str:
    parsed = _utc(value)
    return parsed.isoformat(timespec="seconds")


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Bar-estimation timestamps must include an offset.")
    return parsed.astimezone(timezone.utc)


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    current = date(year, month, 1)
    offset = (weekday - current.weekday()) % 7
    return current + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    return current - timedelta(days=(current.weekday() - weekday) % 7)


def _observed(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _xnas_holidays(year: int) -> set[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    # New Year's Day can be observed on 31 December of the preceding year.
    holidays.add(_observed(date(year + 1, 1, 1)))
    return holidays


def _xnas_early_close_dates(year: int) -> set[date]:
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    candidates = {
        thanksgiving + timedelta(days=1),
        date(year, 12, 24),
        date(year, 7, 3),
    }
    holidays = _xnas_holidays(year)
    return {
        item
        for item in candidates
        if item.weekday() < 5 and item not in holidays
    }


def _parse_local_time(value: str) -> time:
    hour, minute = (int(item) for item in value.split(":", 1))
    return time(hour, minute)


def _session_windows(
    start: datetime,
    end: datetime,
    capability: ProviderTimeframeCapability,
) -> tuple[tuple[_SessionWindow, ...], BarEstimateQuality, tuple[str, ...]]:
    if not capability.market_timezone:
        return (), BarEstimateQuality.UNAVAILABLE, (
            "market timezone unavailable",
        )
    try:
        market_zone = ZoneInfo(capability.market_timezone)
    except ZoneInfoNotFoundError:
        return (), BarEstimateQuality.UNAVAILABLE, (
            f"IANA timezone unavailable: {capability.market_timezone}",
        )
    local_open = _parse_local_time(capability.session_start_local or "00:00")
    start_date = start.astimezone(market_zone).date() - timedelta(days=1)
    end_date = end.astimezone(market_zone).date() + timedelta(days=1)
    supported_xnas = (capability.market_calendar_id or "").upper() in {
        "XNAS",
        "NASDAQ",
    }
    assumptions = []
    quality = BarEstimateQuality.SESSION_CALENDAR_ESTIMATE
    if supported_xnas:
        assumptions.extend(
            (
                f"calendar policy {XNAS_RULE_CALENDAR_VERSION}",
                "unexpected exchange closures are not inferred",
            )
        )
    else:
        quality = BarEstimateQuality.COARSE_FALLBACK
        assumptions.append("weekday-only session estimate; holiday calendar unavailable")

    sessions: list[_SessionWindow] = []
    current = start_date
    while current <= end_date:
        holiday = supported_xnas and current in _xnas_holidays(current.year)
        if current.weekday() < 5 and not holiday:
            minutes = int(capability.session_minutes or 0)
            if supported_xnas and current in _xnas_early_close_dates(current.year):
                minutes = min(minutes, 210)
            opened = datetime.combine(current, local_open, market_zone)
            closed = opened + timedelta(minutes=minutes)
            opened_utc = opened.astimezone(timezone.utc)
            closed_utc = closed.astimezone(timezone.utc)
            if closed_utc > start and opened_utc <= end and closed_utc <= end:
                sessions.append(
                    _SessionWindow(
                        trading_date=current,
                        open_utc=opened_utc,
                        close_utc=closed_utc,
                        minutes=minutes,
                    )
                )
        current += timedelta(days=1)
    return tuple(sessions), quality, tuple(assumptions)


def _wall_clock_estimate(
    start: datetime,
    end: datetime,
    capability: ProviderTimeframeCapability,
    *,
    assumptions: tuple[str, ...],
) -> NativeBarEstimate:
    bars = max(
        1,
        int(
            math.ceil(
                max(1.0, (end - start).total_seconds())
                / capability.duration_seconds
            )
        ),
    )
    return NativeBarEstimate(
        estimated_native_bars=bars,
        estimation_basis=BarEstimationBasis.WALL_CLOCK_FALLBACK,
        market_calendar_id=capability.market_calendar_id,
        session_policy=capability.session_policy,
        session_minutes=capability.session_minutes,
        provider_bar_alignment=capability.provider_bar_alignment.value,
        estimate_quality=BarEstimateQuality.COARSE_FALLBACK,
        assumptions=assumptions,
        structural_interval_start_utc=start.isoformat(),
        structural_interval_end_utc=end.isoformat(),
    )


def estimate_native_bars(
    *,
    structural_start_utc: str,
    structural_end_utc: str,
    capability: ProviderTimeframeCapability,
) -> NativeBarEstimate:
    """Estimate completed native bars without using context-window duration."""

    start = _utc(structural_start_utc)
    end = _utc(structural_end_utc)
    if end <= start:
        raise ValueError("Structural interval end must follow its start.")
    mode = capability.market_session_mode
    alignment = capability.provider_bar_alignment
    if mode is MarketSessionMode.CONTINUOUS_24_7:
        bars = max(
            1,
            int(math.ceil((end - start).total_seconds() / capability.duration_seconds)),
        )
        return NativeBarEstimate(
            estimated_native_bars=bars,
            estimation_basis=BarEstimationBasis.CONTINUOUS_ELAPSED_TIME,
            market_calendar_id=capability.market_calendar_id,
            session_policy=capability.session_policy,
            session_minutes=None,
            provider_bar_alignment=alignment.value,
            estimate_quality=BarEstimateQuality.PROVIDER_DECLARED,
            assumptions=("provider declares a continuous 24/7 market",),
            structural_interval_start_utc=start.isoformat(),
            structural_interval_end_utc=end.isoformat(),
        )
    if mode not in {
        MarketSessionMode.REGULAR_SESSION_EQUITY,
        MarketSessionMode.EXTENDED_SESSION_EQUITY,
    }:
        return _wall_clock_estimate(
            start,
            end,
            capability,
            assumptions=("session/calendar metadata unavailable",),
        )

    sessions, quality, assumptions = _session_windows(start, end, capability)
    if quality is BarEstimateQuality.UNAVAILABLE:
        return _wall_clock_estimate(
            start,
            end,
            capability,
            assumptions=assumptions,
        )
    timeframe = capability.canonical_name
    basis = BarEstimationBasis.EXCHANGE_SESSION_CALENDAR
    if timeframe == "daily":
        bars = len(sessions)
    elif timeframe == "weekly":
        bars = len(
            {
                (item.trading_date.isocalendar().year, item.trading_date.isocalendar().week)
                for item in sessions
            }
        )
        basis = BarEstimationBasis.SESSION_CONTAINING_CALENDAR_BUCKETS
    elif timeframe == "monthly":
        bars = len({(item.trading_date.year, item.trading_date.month) for item in sessions})
        basis = BarEstimationBasis.SESSION_CONTAINING_CALENDAR_BUCKETS
    elif capability.duration_seconds >= 86_400:
        sessions_per_bar = max(1, capability.duration_seconds // 86_400)
        bars = int(math.ceil(len(sessions) / sessions_per_bar))
        basis = BarEstimationBasis.EXCHANGE_SESSION_CALENDAR
    elif alignment is ProviderBarAlignment.SESSION_OPEN_ALIGNED_WITH_SHORT_FINAL:
        bars = sum(
            int(math.ceil(item.minutes * 60 / capability.duration_seconds))
            for item in sessions
        )
        basis = BarEstimationBasis.PROVIDER_SESSION_ALIGNMENT
        assumptions = (*assumptions, "provider emits a completed shortened terminal bar")
    elif alignment is ProviderBarAlignment.SESSION_OPEN_ALIGNED_FULL_ONLY:
        bars = sum(
            int(item.minutes * 60 // capability.duration_seconds)
            for item in sessions
        )
        basis = BarEstimationBasis.PROVIDER_SESSION_ALIGNMENT
        assumptions = (*assumptions, "provider emits full intraday bars only")
    else:
        return _wall_clock_estimate(
            start,
            end,
            capability,
            assumptions=(*assumptions, "provider intraday bar alignment unavailable"),
        )
    return NativeBarEstimate(
        estimated_native_bars=max(0, bars),
        estimation_basis=basis,
        market_calendar_id=capability.market_calendar_id,
        session_policy=capability.session_policy,
        session_minutes=capability.session_minutes,
        provider_bar_alignment=alignment.value,
        estimate_quality=quality,
        assumptions=assumptions,
        structural_interval_start_utc=start.isoformat(),
        structural_interval_end_utc=end.isoformat(),
    )


def estimate_quality_rank(value: BarEstimateQuality) -> int:
    return {
        BarEstimateQuality.EXACT_CALENDAR: 0,
        BarEstimateQuality.PROVIDER_DECLARED: 0,
        BarEstimateQuality.SESSION_CALENDAR_ESTIMATE: 1,
        BarEstimateQuality.COARSE_FALLBACK: 2,
        BarEstimateQuality.UNAVAILABLE: 3,
    }[BarEstimateQuality(value)]


__all__ = [
    "ADAPTIVE_BAR_ESTIMATION_POLICY_VERSION",
    "BarEstimateQuality",
    "BarEstimationBasis",
    "NativeBarEstimate",
    "XNAS_RULE_CALENDAR_VERSION",
    "estimate_native_bars",
    "estimate_quality_rank",
]
