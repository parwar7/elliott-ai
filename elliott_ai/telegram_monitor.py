"""Persistent watchlist and bounded cloud monitoring for Telegram."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .live_market_data import normalize_market_symbol


WATCHLIST_SCHEMA_VERSION = "telegram-watchlist-v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class WatchEntry:
    chat_id: int
    symbol: str
    added_at: str
    last_checked_at: str | None = None
    last_digest: str | None = None
    last_error: str | None = None


class WatchlistStore:
    """Atomic JSON persistence; no Elliott or project database mutation."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        self._lock = threading.RLock()
        self._entries: dict[str, WatchEntry] = {}
        self._load()

    @staticmethod
    def _key(chat_id: int, symbol: str) -> str:
        return f"{int(chat_id)}:{symbol}"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Watchlist file is unreadable: {self.path}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != WATCHLIST_SCHEMA_VERSION:
            raise ValueError("Watchlist file has an unsupported schema version.")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise ValueError("Watchlist entries must be an array.")
        entries: dict[str, WatchEntry] = {}
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise ValueError("Each watchlist entry must be an object.")
            entry = WatchEntry(
                chat_id=int(raw["chat_id"]),
                symbol=normalize_market_symbol(str(raw["symbol"])).canonical_symbol,
                added_at=str(raw["added_at"]),
                last_checked_at=(
                    str(raw["last_checked_at"])
                    if raw.get("last_checked_at") is not None
                    else None
                ),
                last_digest=(
                    str(raw["last_digest"])
                    if raw.get("last_digest") is not None
                    else None
                ),
                last_error=(
                    str(raw["last_error"])
                    if raw.get("last_error") is not None
                    else None
                ),
            )
            key = self._key(entry.chat_id, entry.symbol)
            if key in entries:
                raise ValueError("Watchlist contains a duplicate chat/symbol entry.")
            entries[key] = entry
        self._entries = entries

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": WATCHLIST_SCHEMA_VERSION,
            "entries": [
                asdict(entry)
                for entry in sorted(
                    self._entries.values(), key=lambda item: (item.chat_id, item.symbol)
                )
            ],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def add(self, chat_id: int, symbol_value: str) -> tuple[WatchEntry, bool]:
        symbol = normalize_market_symbol(symbol_value).canonical_symbol
        key = self._key(chat_id, symbol)
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                return existing, False
            entry = WatchEntry(chat_id=int(chat_id), symbol=symbol, added_at=_utc_now())
            self._entries[key] = entry
            self._save()
            return entry, True

    def remove(self, chat_id: int, symbol_value: str) -> bool:
        symbol = normalize_market_symbol(symbol_value).canonical_symbol
        key = self._key(chat_id, symbol)
        with self._lock:
            if key not in self._entries:
                return False
            del self._entries[key]
            self._save()
            return True

    def list_for_chat(self, chat_id: int) -> tuple[WatchEntry, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        entry
                        for entry in self._entries.values()
                        if entry.chat_id == int(chat_id)
                    ),
                    key=lambda item: item.symbol,
                )
            )

    def all_entries(self) -> tuple[WatchEntry, ...]:
        with self._lock:
            return tuple(
                sorted(self._entries.values(), key=lambda item: (item.symbol, item.chat_id))
            )

    def record_result(self, chat_id: int, symbol: str, digest: str) -> WatchEntry:
        key = self._key(chat_id, symbol)
        with self._lock:
            current = self._entries[key]
            updated = WatchEntry(
                chat_id=current.chat_id,
                symbol=current.symbol,
                added_at=current.added_at,
                last_checked_at=_utc_now(),
                last_digest=digest,
                last_error=None,
            )
            self._entries[key] = updated
            self._save()
            return updated

    def record_error(self, chat_id: int, symbol: str, error_code: str) -> WatchEntry:
        key = self._key(chat_id, symbol)
        with self._lock:
            current = self._entries[key]
            updated = WatchEntry(
                chat_id=current.chat_id,
                symbol=current.symbol,
                added_at=current.added_at,
                last_checked_at=_utc_now(),
                last_digest=current.last_digest,
                last_error=error_code,
            )
            self._entries[key] = updated
            self._save()
            return updated


class TechnicalStateMonitor:
    """Re-analyze watched symbols and notify only on state-digest changes."""

    def __init__(
        self,
        *,
        watchlist: WatchlistStore,
        interval_minutes: int,
        analyze: Callable[[str, bool], Any],
        notify: Callable[[int, str, Path | None], None],
        blind: bool = True,
    ) -> None:
        if interval_minutes != 0 and interval_minutes < 15:
            raise ValueError(
                "TELEGRAM_MONITOR_INTERVAL_MINUTES must be 0 or at least 15."
            )
        self.watchlist = watchlist
        self.interval_minutes = interval_minutes
        self.analyze = analyze
        self.notify = notify
        self.blind = blind
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self.interval_minutes > 0

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="telegram-technical-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_minutes * 60):
            self.run_once()

    def run_once(self) -> None:
        entries = self.watchlist.all_entries()
        chats_by_symbol: dict[str, list[WatchEntry]] = {}
        for entry in entries:
            chats_by_symbol.setdefault(entry.symbol, []).append(entry)

        for symbol, symbol_entries in chats_by_symbol.items():
            try:
                result = self.analyze(symbol, self.blind)
            except Exception:
                for entry in symbol_entries:
                    was_same_error = entry.last_error == "analysis_failed"
                    self.watchlist.record_error(
                        entry.chat_id, entry.symbol, "analysis_failed"
                    )
                    if not was_same_error:
                        self.notify(
                            entry.chat_id,
                            f"{symbol} monitoring could not complete. Check the cloud logs.",
                            None,
                        )
                continue

            for entry in symbol_entries:
                previous_digest = entry.last_digest
                self.watchlist.record_result(
                    entry.chat_id, entry.symbol, result.technical_digest
                )
                if previous_digest is None:
                    self.notify(
                        entry.chat_id,
                        f"{symbol} monitoring baseline recorded.\n\n{result.summary}",
                        None,
                    )
                elif previous_digest != result.technical_digest:
                    self.notify(
                        entry.chat_id,
                        f"{symbol} technical state changed.\n\n{result.summary}",
                        result.report_path,
                    )
