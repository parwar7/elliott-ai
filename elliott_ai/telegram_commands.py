"""Deterministic parsing and rendering for the private Telegram bot."""

from __future__ import annotations

from dataclasses import dataclass

from .live_market_data import normalize_market_symbol


SUPPORTED_TELEGRAM_COMMANDS = frozenset(
    {
        "start",
        "help",
        "analyze",
        "blind",
        "watch",
        "unwatch",
        "watchlist",
        "status",
    }
)


class TelegramCommandError(ValueError):
    """Raised when a Telegram message is not a valid bot command."""


@dataclass(frozen=True)
class TelegramCommand:
    name: str
    symbol: str | None = None
    job_id: str | None = None


def parse_telegram_command(text: str) -> TelegramCommand:
    raw = str(text or "").strip()
    if not raw.startswith("/"):
        raise TelegramCommandError("Send /help to see the available commands.")
    parts = raw.split()
    command_token = parts[0][1:].split("@", 1)[0].lower()
    if command_token not in SUPPORTED_TELEGRAM_COMMANDS:
        raise TelegramCommandError(
            f"Unknown command /{command_token}. Send /help for the command list."
        )

    if command_token in {"start", "help", "watchlist"}:
        if len(parts) != 1:
            raise TelegramCommandError(f"/{command_token} does not take an argument.")
        return TelegramCommand(name=command_token)

    if command_token == "status":
        if len(parts) > 2:
            raise TelegramCommandError("Use /status or /status JOB_ID.")
        return TelegramCommand(
            name=command_token,
            job_id=parts[1].strip() if len(parts) == 2 else None,
        )

    if len(parts) != 2:
        raise TelegramCommandError(f"Use /{command_token} SYMBOL.")
    symbol = normalize_market_symbol(parts[1]).canonical_symbol
    return TelegramCommand(name=command_token, symbol=symbol)


def telegram_help_text(*, monitoring_enabled: bool) -> str:
    monitoring = (
        "Cloud monitoring is enabled."
        if monitoring_enabled
        else "Cloud monitoring is disabled until TELEGRAM_MONITOR_INTERVAL_MINUTES is set."
    )
    return "\n".join(
        (
            "Elliott AI Telegram commands",
            "",
            "/analyze MSFT - analyze with approved memory available",
            "/blind MSFT - fresh rules-only recount",
            "/watch MSFT - add a symbol to technical-state monitoring",
            "/unwatch MSFT - stop monitoring a symbol",
            "/watchlist - list monitored symbols",
            "/status - show your latest analysis job",
            "/status JOB_ID - show one job",
            "/help - show this message",
            "",
            monitoring,
            "Research only. The bot does not place trades.",
        )
    )


def truncate_telegram_text(text: str, limit: int = 3900) -> str:
    clean = str(text).strip()
    if len(clean) <= limit:
        return clean
    suffix = "\n\n[Message shortened. See the attached full report.]"
    return clean[: max(1, limit - len(suffix))].rstrip() + suffix
