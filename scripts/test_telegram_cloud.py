from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from elliott_ai.live_market_data import (
    MarketDataBundle,
    MarketDataProviderError,
    TwelveDataClient,
    normalize_market_symbol,
    normalize_timeframes,
)
from elliott_ai.telegram_bot import (
    CloudAnalysisResult,
    TelegramBotSettings,
    TelegramElliottBot,
    _public_error,
    technical_state_digest,
)
from elliott_ai.telegram_commands import (
    TelegramCommandError,
    parse_telegram_command,
)
from elliott_ai.telegram_monitor import TechnicalStateMonitor, WatchlistStore
from elliott_ai.providers import ProviderError


def _provider_payload(interval: str, *, symbol: str = "MSFT") -> dict[str, object]:
    values = []
    for index in range(40):
        price = 100.0 + index
        values.append(
            {
                "datetime": f"2026-01-{(index % 28) + 1:02d}T{index % 24:02d}:00:00+00:00",
                "open": str(price),
                "high": str(price + 2),
                "low": str(price - 2),
                "close": str(price + 1),
                "volume": str(1000 + index),
            }
        )
    return {
        "meta": {
            "symbol": symbol,
            "interval": interval,
            "exchange": "NASDAQ",
            "mic_code": "XNAS",
            "currency": "USD",
            "type": "Common Stock",
            "exchange_timezone": "America/New_York",
        },
        "values": values,
        "status": "ok",
    }


class FixtureTwelveDataClient(TwelveDataClient):
    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        super().__init__("private-test-key", maximum_retries=0)
        self.payloads = payloads
        self.requests: list[dict[str, object]] = []

    def _request_json(self, endpoint: str, parameters: dict[str, object]) -> dict[str, object]:
        self.requests.append({"endpoint": endpoint, **parameters})
        interval = str(parameters["interval"])
        payload = self.payloads.get(interval)
        if payload is None:
            return {"status": "error", "message": f"No fixture for {interval}"}
        return payload


class LiveMarketDataTests(unittest.TestCase):
    def test_symbol_normalization_preserves_provider_identity(self) -> None:
        symbol = normalize_market_symbol("nasdaq:msft")
        self.assertEqual(symbol.canonical_symbol, "NASDAQ:MSFT")
        self.assertEqual(symbol.provider_symbol, "MSFT")
        self.assertEqual(symbol.exchange, "NASDAQ")
        self.assertEqual(normalize_market_symbol("btc/usd").provider_symbol, "BTC/USD")

    def test_symbol_rejects_shell_and_url_characters(self) -> None:
        for value in ("MSFT;whoami", "https://example.com", "MSFT $(dir)"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_market_symbol(value)

    def test_timeframe_validation_is_deterministic(self) -> None:
        self.assertEqual(normalize_timeframes(["daily", "daily", "1h"]), ("daily", "1h"))
        with self.assertRaises(ValueError):
            normalize_timeframes(["3h"])

    def test_bundle_writes_same_feed_snapshots_without_api_key(self) -> None:
        client = FixtureTwelveDataClient(
            {
                "1month": _provider_payload("1month"),
                "1day": _provider_payload("1day"),
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            bundle = client.fetch_bundle(
                "NASDAQ:MSFT",
                Path(temp),
                timeframes=("monthly", "daily"),
                captured_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
            )
            self.assertEqual(bundle.available_timeframes, ("monthly", "daily"))
            self.assertEqual(bundle.unavailable_timeframes, ())
            self.assertEqual(len(bundle.paths), 2)
            for raw_path in bundle.paths:
                text = Path(raw_path).read_text(encoding="utf-8")
                self.assertNotIn("private-test-key", text)
                payload = json.loads(text)
                self.assertEqual(payload["metadata"]["provider"], "twelve_data")
                self.assertEqual(payload["metadata"]["adjustment"], "splits")
                self.assertTrue(payload["metadata"]["source_document_hash"])

    def test_bundle_records_optional_timeframe_failure(self) -> None:
        client = FixtureTwelveDataClient(
            {
                "1month": _provider_payload("1month"),
                "1day": _provider_payload("1day"),
            }
        )
        with tempfile.TemporaryDirectory() as temp:
            bundle = client.fetch_bundle(
                "MSFT",
                Path(temp),
                timeframes=("monthly", "daily", "1h"),
            )
        self.assertEqual(bundle.unavailable_timeframes, ("1h",))
        self.assertEqual(len(bundle.warnings), 1)

    def test_bundle_requires_daily_and_macro_data(self) -> None:
        client = FixtureTwelveDataClient({"1month": _provider_payload("1month")})
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(
            MarketDataProviderError
        ):
            client.fetch_bundle(
                "MSFT", Path(temp), timeframes=("monthly", "daily")
            )


class TelegramCommandTests(unittest.TestCase):
    def test_analysis_and_status_commands(self) -> None:
        command = parse_telegram_command("/analyze@MyBot nasdaq:msft")
        self.assertEqual(command.name, "analyze")
        self.assertEqual(command.symbol, "NASDAQ:MSFT")
        status = parse_telegram_command("/status job-123")
        self.assertEqual(status.job_id, "job-123")

    def test_invalid_command_shape_is_rejected(self) -> None:
        with self.assertRaises(TelegramCommandError):
            parse_telegram_command("/analyze")
        with self.assertRaises(TelegramCommandError):
            parse_telegram_command("hello")

    def test_provider_failures_have_actionable_safe_messages(self) -> None:
        self.assertIn(
            "API key",
            _public_error(ProviderError("Provider returned HTTP 401: invalid_api_key")),
        )
        self.assertIn(
            "quota",
            _public_error(ProviderError("Provider returned HTTP 429: insufficient_quota")),
        )
        self.assertIn(
            "ELLIOTT_MODEL",
            _public_error(ProviderError("model_not_found: does not exist")),
        )


class WatchlistTests(unittest.TestCase):
    def test_watchlist_is_deduplicated_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "watchlist.json"
            store = WatchlistStore(path)
            _, created = store.add(10, "msft")
            _, duplicate_created = store.add(10, "MSFT")
            store.record_result(10, "MSFT", "digest-1")
            reloaded = WatchlistStore(path)
            entries = reloaded.list_for_chat(10)
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].last_digest, "digest-1")

    def test_monitor_only_notifies_on_baseline_or_changed_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = WatchlistStore(root / "watchlist.json")
            store.add(10, "MSFT")
            report = root / "report.md"
            report.write_text("report", encoding="utf-8")
            digests = iter(("first", "first", "second"))
            notifications: list[tuple[int, str, Path | None]] = []

            def analyze(symbol: str, blind: bool) -> CloudAnalysisResult:
                digest = next(digests)
                bundle = MarketDataBundle(
                    requested_symbol=symbol,
                    canonical_symbol=symbol,
                    provider_symbol=symbol,
                    exchange=None,
                    captured_at="2026-08-07T00:00:00+00:00",
                    paths=(),
                    available_timeframes=("monthly", "daily"),
                    unavailable_timeframes=(),
                    warnings=(),
                    content_hash="bundle",
                )
                return CloudAnalysisResult(
                    symbol=symbol,
                    blind=blind,
                    run_id=1,
                    resolution_id=1,
                    summary="summary",
                    report_path=report,
                    json_path=report,
                    technical_digest=digest,
                    market_data_bundle=bundle,
                )

            monitor = TechnicalStateMonitor(
                watchlist=store,
                interval_minutes=15,
                analyze=analyze,
                notify=lambda chat_id, text, path: notifications.append(
                    (chat_id, text, path)
                ),
            )
            monitor.run_once()
            monitor.run_once()
            monitor.run_once()
            self.assertEqual(len(notifications), 2)
            self.assertIn("baseline", notifications[0][1])
            self.assertIn("state changed", notifications[1][1])


class TechnicalDigestTests(unittest.TestCase):
    def test_digest_ignores_hierarchy_order_but_changes_active_structure(self) -> None:
        node_1 = {
            "wave_id": "P1",
            "parent_wave_id": None,
            "degree": "Primary",
            "wave": "1",
            "sequence_position": "1",
            "direction": "up",
            "start": {"date": "2026-01-01", "price": 10},
            "end": {"date": "2026-02-01", "price": 20},
            "structure": "Impulse",
            "completion_status": "active",
            "invalidation": "Below 10",
        }
        node_2 = {
            **node_1,
            "wave_id": "I1",
            "parent_wave_id": "P1",
            "degree": "Intermediate",
        }
        response = {
            "resolution_status": "provisional",
            "data_cutoff": "2026-02-01",
            "active_position": {"wave_ids": ["P1", "I1"]},
            "degree_hierarchy": [node_1, node_2],
            "invalidation_levels": ["10"],
        }
        first = technical_state_digest(response)
        reordered = {**response, "degree_hierarchy": [node_2, node_1]}
        self.assertEqual(first, technical_state_digest(reordered))
        changed = {
            **response,
            "degree_hierarchy": [{**node_1, "structure": "Diagonal"}, node_2],
        }
        self.assertNotEqual(first, technical_state_digest(changed))


class FakeTelegramAPI:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.documents: list[tuple[int, Path, str]] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))

    def send_document(self, chat_id: int, path: Path, *, caption: str = "") -> None:
        self.documents.append((chat_id, Path(path), caption))


class FakeAnalysisService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.called = threading.Event()

    def analyze_symbol(self, symbol: str, blind: bool = False) -> CloudAnalysisResult:
        report = self.root / "report.md"
        result_json = self.root / "result.json"
        report.write_text("report", encoding="utf-8")
        result_json.write_text("{}", encoding="utf-8")
        self.called.set()
        bundle = MarketDataBundle(
            requested_symbol=symbol,
            canonical_symbol=symbol,
            provider_symbol=symbol,
            exchange=None,
            captured_at="2026-08-07T00:00:00+00:00",
            paths=(),
            available_timeframes=("monthly", "daily"),
            unavailable_timeframes=(),
            warnings=(),
            content_hash="bundle",
        )
        return CloudAnalysisResult(
            symbol=symbol,
            blind=blind,
            run_id=4,
            resolution_id=5,
            summary="clean summary",
            report_path=report,
            json_path=result_json,
            technical_digest="digest",
            market_data_bundle=bundle,
        )


class TelegramBotTests(unittest.TestCase):
    def _settings(self, root: Path) -> TelegramBotSettings:
        return TelegramBotSettings(
            bot_token="test-token",
            allowed_chat_ids=frozenset({10}),
            twelve_data_api_key="test-market-key",
            poll_timeout_seconds=30,
            monitor_interval_minutes=0,
            monitor_blind=True,
            analysis_timeframes=("monthly", "daily"),
            results_root=root / "results",
            market_data_root=root / "market",
            watchlist_path=root / "watchlist.json",
            seed_database_path=None,
        )

    def test_unauthorized_chat_is_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            api = FakeTelegramAPI()
            bot = TelegramElliottBot(
                settings=self._settings(root),
                api=api,  # type: ignore[arg-type]
                analysis_service=FakeAnalysisService(root),  # type: ignore[arg-type]
                watchlist=WatchlistStore(root / "watchlist.json"),
            )
            bot.handle_text(999, "/help")
            bot.handle_text(10, "/help")
            bot.close()
        self.assertEqual(len(api.messages), 1)
        self.assertEqual(api.messages[0][0], 10)

    def test_analysis_command_returns_summary_and_two_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            api = FakeTelegramAPI()
            service = FakeAnalysisService(root)
            bot = TelegramElliottBot(
                settings=self._settings(root),
                api=api,  # type: ignore[arg-type]
                analysis_service=service,  # type: ignore[arg-type]
                watchlist=WatchlistStore(root / "watchlist.json"),
            )
            bot.handle_text(10, "/blind MSFT")
            self.assertTrue(service.called.wait(timeout=2))
            bot.close()
        self.assertTrue(any("Started job-" in message for _, message in api.messages))
        self.assertTrue(any(message == "clean summary" for _, message in api.messages))
        self.assertEqual(len(api.documents), 2)

    def test_environment_settings_require_allowlist_and_normalize_timeframes(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            "os.environ",
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_ALLOWED_CHAT_IDS": "10, 20",
                "TWELVE_DATA_API_KEY": "market",
                "ELLIOTT_CLOUD_TIMEFRAMES": "monthly,daily,1h",
                "ELLIOTT_CLOUD_DATA_ROOT": temp,
            },
            clear=False,
        ):
            settings = TelegramBotSettings.from_environment(workspace=Path(temp))
        self.assertEqual(settings.allowed_chat_ids, frozenset({10, 20}))
        self.assertEqual(settings.analysis_timeframes, ("monthly", "daily", "1h"))


if __name__ == "__main__":
    unittest.main()
