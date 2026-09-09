"""Private cloud Telegram control surface for the existing Elliott AI."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .agent import AnalysisRequest, ElliottAgent
from .config import Settings
from .indicators import with_optional_rsi
from .knowledge import KnowledgeStore
from .live_market_data import (
    DEFAULT_ANALYSIS_TIMEFRAMES,
    MarketDataBundle,
    MarketDataProviderError,
    TwelveDataClient,
    normalize_market_symbol,
    normalize_timeframes,
)
from .providers import ProviderError, create_provider
from .reporting import default_report_name, write_degree_report
from .telegram_commands import (
    TelegramCommandError,
    parse_telegram_command,
    telegram_help_text,
    truncate_telegram_text,
)
from .telegram_monitor import TechnicalStateMonitor, WatchlistStore


LOGGER = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    """A sanitized Telegram API failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _environment_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be true or false.")


def _environment_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    value = int(raw) if raw is not None and raw.strip() else default
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _required_environment(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required.")
    return value


def _allowed_chat_ids(value: str) -> frozenset[int]:
    parts = [part.strip() for part in str(value).split(",") if part.strip()]
    if not parts:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS must contain at least one chat ID.")
    try:
        return frozenset(int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS must contain integers.") from exc


@dataclass(frozen=True)
class TelegramBotSettings:
    bot_token: str
    allowed_chat_ids: frozenset[int]
    twelve_data_api_key: str
    poll_timeout_seconds: int
    monitor_interval_minutes: int
    monitor_blind: bool
    analysis_timeframes: tuple[str, ...]
    results_root: Path
    market_data_root: Path
    watchlist_path: Path
    seed_database_path: Path | None

    @classmethod
    def from_environment(cls, *, workspace: Path) -> "TelegramBotSettings":
        data_root = Path(os.getenv("ELLIOTT_CLOUD_DATA_ROOT", workspace / ".elliott_ai"))
        if not data_root.is_absolute():
            data_root = workspace / data_root
        raw_timeframes = os.getenv("ELLIOTT_CLOUD_TIMEFRAMES", "")
        requested_timeframes = (
            tuple(part.strip() for part in raw_timeframes.split(",") if part.strip())
            if raw_timeframes.strip()
            else DEFAULT_ANALYSIS_TIMEFRAMES
        )
        seed_raw = str(os.getenv("ELLIOTT_SEED_DATABASE") or "").strip()
        seed_path = Path(seed_raw).resolve() if seed_raw else None
        return cls(
            bot_token=_required_environment("TELEGRAM_BOT_TOKEN"),
            allowed_chat_ids=_allowed_chat_ids(
                _required_environment("TELEGRAM_ALLOWED_CHAT_IDS")
            ),
            twelve_data_api_key=_required_environment("TWELVE_DATA_API_KEY"),
            poll_timeout_seconds=_environment_int(
                "TELEGRAM_POLL_TIMEOUT_SECONDS", 30, minimum=5, maximum=50
            ),
            monitor_interval_minutes=_environment_int(
                "TELEGRAM_MONITOR_INTERVAL_MINUTES", 0, minimum=0, maximum=1440
            ),
            monitor_blind=_environment_bool("TELEGRAM_MONITOR_BLIND", True),
            analysis_timeframes=normalize_timeframes(requested_timeframes),
            results_root=(data_root / "cloud_results").resolve(),
            market_data_root=(data_root / "cloud_market_data").resolve(),
            watchlist_path=(data_root / "telegram_watchlist.json").resolve(),
            seed_database_path=seed_path,
        )


@dataclass(frozen=True)
class CloudAnalysisResult:
    symbol: str
    blind: bool
    run_id: int
    resolution_id: int
    summary: str
    report_path: Path
    json_path: Path
    technical_digest: str
    market_data_bundle: MarketDataBundle


@dataclass
class AnalysisJob:
    job_id: str
    chat_id: int
    symbol: str
    blind: bool
    status: str = "queued"
    created_at: str = field(default_factory=_utc_now)
    completed_at: str | None = None
    result: CloudAnalysisResult | None = None
    error: str | None = None


def ensure_seed_database(database_path: Path, seed_path: Path | None) -> None:
    destination = Path(database_path).resolve()
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if seed_path is not None and seed_path.is_file():
        if seed_path.resolve() == destination:
            return
        shutil.copy2(seed_path, destination)


def technical_state_digest(resolution_response: Mapping[str, Any]) -> str:
    active_position = resolution_response.get("active_position")
    active = dict(active_position) if isinstance(active_position, Mapping) else {}
    active_ids = {
        str(value) for value in active.get("wave_ids", []) if str(value).strip()
    }
    nodes: list[dict[str, Any]] = []
    hierarchy = resolution_response.get("degree_hierarchy")
    if isinstance(hierarchy, list):
        for raw_node in hierarchy:
            if not isinstance(raw_node, Mapping):
                continue
            wave_id = str(raw_node.get("wave_id") or "")
            if active_ids and wave_id not in active_ids:
                continue
            if not active_ids and raw_node.get("completion_status") != "active":
                continue
            nodes.append(
                {
                    "wave_id": wave_id,
                    "parent_wave_id": raw_node.get("parent_wave_id"),
                    "degree": raw_node.get("degree"),
                    "wave": raw_node.get("wave"),
                    "sequence_position": raw_node.get("sequence_position"),
                    "direction": raw_node.get("direction"),
                    "start": raw_node.get("start"),
                    "end": raw_node.get("end"),
                    "structure": raw_node.get("structure"),
                    "completion_status": raw_node.get("completion_status"),
                    "invalidation": raw_node.get("invalidation"),
                }
            )
    nodes.sort(key=lambda item: item["wave_id"])
    payload = {
        "resolution_status": resolution_response.get("resolution_status"),
        "data_cutoff": resolution_response.get("data_cutoff"),
        "active_wave_ids": sorted(active_ids),
        "active_nodes": nodes,
        "invalidation_levels": sorted(
            str(value)
            for value in resolution_response.get("invalidation_levels", [])
        ),
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _analysis_summary(
    symbol: str,
    resolution_result: Mapping[str, Any],
) -> str:
    response = resolution_result.get("degree_resolution")
    response = dict(response) if isinstance(response, Mapping) else {}
    active = response.get("active_position")
    active = dict(active) if isinstance(active, Mapping) else {}
    readiness = resolution_result.get("readiness")
    readiness = dict(readiness) if isinstance(readiness, Mapping) else {}
    validation_errors = resolution_result.get("validation_errors")
    error_count = len(validation_errors) if isinstance(validation_errors, list) else 0
    lines = [
        f"{symbol} Elliott analysis",
        f"Status: {response.get('resolution_status', 'unavailable')}",
        f"Data cutoff: {response.get('data_cutoff') or 'unavailable'}",
        f"Scale: {response.get('scale_mode', 'undetermined')}",
        f"Current position: {active.get('summary') or 'unresolved'}",
        f"Confirmation: {active.get('confirmation') or 'not established'}",
        f"Invalidation: {active.get('invalidation') or 'not established'}",
        "Report readiness: "
        + ("ready" if readiness.get("final_report_ready") else "provisional"),
        f"Deterministic validation issues: {error_count}",
        "Research only. No trade was placed.",
    ]
    return truncate_telegram_text("\n".join(lines))


def _public_error(exc: BaseException) -> str:
    if isinstance(exc, (ValueError, MarketDataProviderError)):
        return truncate_telegram_text(str(exc), 700)
    if isinstance(exc, ProviderError):
        detail = str(exc).lower()
        if "http 401" in detail or "invalid_api_key" in detail:
            return "OpenAI rejected the API key (HTTP 401). Load a valid OPENAI_API_KEY."
        if "http 429" in detail or "insufficient_quota" in detail:
            return "OpenAI quota or billing blocked the request (HTTP 429)."
        if "model_not_found" in detail or "does not exist" in detail:
            return "OpenAI could not use ELLIOTT_MODEL. Set it to a model available to this API project."
        if "model output" in detail or "did not contain text output" in detail:
            return "The model response did not satisfy the required JSON contract. Retry once, then check the configured model."
        return "The AI provider could not complete this analysis. Check the private terminal or Railway logs."
    if isinstance(exc, TelegramAPIError):
        return "Telegram delivery failed. Check Railway logs."
    return "The analysis failed unexpectedly. Check Railway logs for the private diagnostic."


def _private_diagnostic(exc: BaseException) -> str:
    diagnostic = f"{type(exc).__name__}: {exc}"
    for name in (
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TWELVE_DATA_API_KEY",
    ):
        secret = str(os.getenv(name) or "")
        if secret:
            diagnostic = diagnostic.replace(secret, f"<{name}:redacted>")
    return diagnostic


class CloudAnalysisService:
    """Fetch candles, then call the unchanged Elliott analysis and degree passes."""

    def __init__(
        self,
        *,
        agent_settings: Settings,
        bot_settings: TelegramBotSettings,
        market_client: TwelveDataClient | None = None,
    ) -> None:
        ensure_seed_database(
            agent_settings.database_path, bot_settings.seed_database_path
        )
        self.agent_settings = agent_settings
        self.bot_settings = bot_settings
        self.store = KnowledgeStore(agent_settings.database_path)
        self.agent = ElliottAgent(
            workspace=agent_settings.workspace,
            store=self.store,
            provider=create_provider(agent_settings),
        )
        self.market_client = market_client or TwelveDataClient(
            bot_settings.twelve_data_api_key
        )
        self._analysis_lock = threading.Lock()

    def analyze_symbol(self, symbol_value: str, blind: bool = False) -> CloudAnalysisResult:
        symbol = normalize_market_symbol(symbol_value)
        with self._analysis_lock:
            bundle = self.market_client.fetch_bundle(
                symbol.canonical_symbol,
                self.bot_settings.market_data_root,
                timeframes=self.bot_settings.analysis_timeframes,
            )
            features = with_optional_rsi(
                (), enabled=self.agent_settings.rsi_enabled
            )
            mode_text = "Blind rules-only recount" if blind else "Top-down recount"
            question = (
                f"{mode_text} of {symbol.canonical_symbol} from the highest degree to the "
                "lowest degree supported by the supplied data. Validate price structure first, "
                "then Fibonacci, volume, EWO, MACD, and optional RSI. Test simple impulses, "
                "diagonals, corrections, and nested 1-2 alternatives. Preserve unresolved "
                "structurally valid alternatives. Do not produce a trade decision."
            )
            analysis_result = self.agent.analyze(
                AnalysisRequest(
                    question=question,
                    symbol=symbol.canonical_symbol,
                    timeframes=bundle.available_timeframes,
                    market_data_paths=bundle.paths,
                    features=features,
                    auto_discover_market_data=False,
                    blind=blind,
                    metadata={
                        "request_source": "private_telegram_bot",
                        "market_data_bundle_hash": bundle.content_hash,
                        "market_data_provider": "twelve_data",
                    },
                )
            )
            resolution_result = self.agent.resolve_degrees(
                int(analysis_result["run_id"]),
                "Resolve and validate the complete top-down degree hierarchy. Keep the "
                "technical result independent from fundamentals and do not issue a trade.",
            )
            run = self.store.get_run(int(analysis_result["run_id"]))
            stored_resolution = self.store.get_degree_resolution(
                int(resolution_result["resolution_id"])
            )
            if run is None or stored_resolution is None:
                raise RuntimeError("The analysis records could not be reloaded for reporting.")

            result_directory = (
                self.bot_settings.results_root
                / symbol.file_slug
                / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            )
            result_directory.mkdir(parents=True, exist_ok=True)
            report_path = write_degree_report(
                run,
                stored_resolution,
                result_directory
                / default_report_name(symbol.canonical_symbol, int(analysis_result["run_id"])),
            )
            json_path = result_directory / "analysis_result.json"
            serializable_result = {
                "market_data_bundle": bundle.to_dict(),
                "analysis_result": analysis_result,
                "degree_resolution_result": resolution_result,
            }
            json_path.write_text(
                json.dumps(serializable_result, ensure_ascii=True, indent=2) + "\n",
                encoding="utf-8",
            )
            degree_response = resolution_result.get("degree_resolution")
            degree_response = (
                degree_response if isinstance(degree_response, Mapping) else {}
            )
            return CloudAnalysisResult(
                symbol=symbol.canonical_symbol,
                blind=blind,
                run_id=int(analysis_result["run_id"]),
                resolution_id=int(resolution_result["resolution_id"]),
                summary=_analysis_summary(symbol.canonical_symbol, resolution_result),
                report_path=report_path,
                json_path=json_path,
                technical_digest=technical_state_digest(degree_response),
                market_data_bundle=bundle,
            )


class TelegramBotAPI:
    """Minimal Bot API client that never logs or serializes the bot token."""

    def __init__(self, token: str, *, timeout_seconds: int = 45) -> None:
        if not str(token or "").strip():
            raise ValueError("A Telegram bot token is required.")
        self._token = str(token).strip()
        self.timeout_seconds = timeout_seconds

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._token}/{method}"

    def _decode_response(self, response_bytes: bytes) -> Any:
        try:
            payload = json.loads(response_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise TelegramAPIError("Telegram returned malformed JSON.") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            description = (
                str(payload.get("description"))
                if isinstance(payload, dict)
                else "unknown error"
            )
            raise TelegramAPIError(f"Telegram rejected the request: {description}")
        return payload.get("result")

    def _post_json(
        self,
        method: str,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: int | None = None,
    ) -> Any:
        body = json.dumps(dict(payload), ensure_ascii=True).encode("utf-8")
        request = Request(
            self._url(method),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(
                request, timeout=timeout_seconds or self.timeout_seconds
            ) as response:
                return self._decode_response(response.read())
        except HTTPError as exc:
            raise TelegramAPIError(
                f"Telegram HTTP request failed with status {exc.code}."
            ) from exc
        except URLError as exc:
            raise TelegramAPIError("Telegram could not be reached.") from exc

    def delete_webhook(self) -> None:
        self._post_json("deleteWebhook", {"drop_pending_updates": False})

    def get_updates(self, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._post_json(
            "getUpdates", payload, timeout_seconds=timeout_seconds + 10
        )
        return [dict(item) for item in result] if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str) -> None:
        self._post_json(
            "sendMessage",
            {"chat_id": int(chat_id), "text": truncate_telegram_text(text)},
        )

    def send_document(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str = "",
    ) -> None:
        document = Path(path).resolve()
        if not document.is_file():
            raise TelegramAPIError(f"Report file is missing: {document.name}")
        boundary = f"elliott-{uuid.uuid4().hex}"
        body = bytearray()

        def add_field(name: str, value: str) -> None:
            body.extend(f"--{boundary}\r\n".encode("ascii"))
            body.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                )
            )
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")

        add_field("chat_id", str(int(chat_id)))
        if caption:
            add_field("caption", truncate_telegram_text(caption, 900))
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="document"; '
                f'filename="{document.name}"\r\n'
            ).encode("utf-8")
        )
        content_type = (
            "application/json" if document.suffix.lower() == ".json" else "text/markdown"
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("ascii"))
        body.extend(document.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
        request = Request(
            self._url("sendDocument"),
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                self._decode_response(response.read())
        except HTTPError as exc:
            raise TelegramAPIError(
                f"Telegram document upload failed with status {exc.code}."
            ) from exc
        except URLError as exc:
            raise TelegramAPIError("Telegram document upload could not connect.") from exc


class TelegramElliottBot:
    def __init__(
        self,
        *,
        settings: TelegramBotSettings,
        api: TelegramBotAPI,
        analysis_service: CloudAnalysisService,
        watchlist: WatchlistStore,
    ) -> None:
        self.settings = settings
        self.api = api
        self.analysis_service = analysis_service
        self.watchlist = watchlist
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="elliott-job")
        self._jobs: dict[str, AnalysisJob] = {}
        self._latest_job_by_chat: dict[int, str] = {}
        self._jobs_lock = threading.RLock()
        self._job_counter = 0
        self.monitor = TechnicalStateMonitor(
            watchlist=watchlist,
            interval_minutes=settings.monitor_interval_minutes,
            analyze=self.analysis_service.analyze_symbol,
            notify=self._monitor_notify,
            blind=settings.monitor_blind,
        )

    def _monitor_notify(self, chat_id: int, text: str, report: Path | None) -> None:
        self.api.send_message(chat_id, text)
        if report is not None:
            self.api.send_document(chat_id, report, caption="Full changed-state report")

    def _next_job_id(self) -> str:
        with self._jobs_lock:
            self._job_counter += 1
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            return f"job-{timestamp}-{self._job_counter:04d}"

    def _queue_analysis(self, chat_id: int, symbol: str, *, blind: bool) -> str:
        job_id = self._next_job_id()
        job = AnalysisJob(job_id=job_id, chat_id=chat_id, symbol=symbol, blind=blind)
        with self._jobs_lock:
            self._jobs[job_id] = job
            self._latest_job_by_chat[chat_id] = job_id
        self.api.send_message(
            chat_id,
            f"Started {job_id} for {symbol}. I will send the report when both analysis passes finish.",
        )
        self._executor.submit(self._run_analysis_job, job_id)
        return job_id

    def _run_analysis_job(self, job_id: str) -> None:
        with self._jobs_lock:
            job = self._jobs[job_id]
            job.status = "running"
        try:
            result = self.analysis_service.analyze_symbol(job.symbol, job.blind)
        except Exception as exc:
            LOGGER.error("Analysis job %s failed: %s", job_id, _private_diagnostic(exc))
            public_error = _public_error(exc)
            with self._jobs_lock:
                job.status = "failed"
                job.completed_at = _utc_now()
                job.error = public_error
            self.api.send_message(job.chat_id, f"{job_id} failed. {public_error}")
            return

        with self._jobs_lock:
            job.status = "completed"
            job.completed_at = _utc_now()
            job.result = result
        self.api.send_message(job.chat_id, result.summary)
        self.api.send_document(
            job.chat_id, result.report_path, caption=f"{result.symbol} clean degree report"
        )
        self.api.send_document(
            job.chat_id, result.json_path, caption=f"{result.symbol} complete JSON record"
        )

    def _status_text(self, chat_id: int, job_id: str | None) -> str:
        with self._jobs_lock:
            resolved_id = job_id or self._latest_job_by_chat.get(chat_id)
            if resolved_id is None:
                return "No analysis job has been submitted in this bot session."
            job = self._jobs.get(resolved_id)
            if job is None or job.chat_id != chat_id:
                return "That job is unavailable."
            lines = [
                f"Job: {job.job_id}",
                f"Symbol: {job.symbol}",
                f"Mode: {'blind' if job.blind else 'standard'}",
                f"Status: {job.status}",
                f"Created: {job.created_at}",
            ]
            if job.completed_at:
                lines.append(f"Completed: {job.completed_at}")
            if job.error:
                lines.append(f"Error: {job.error}")
            if job.result:
                lines.append(f"Run ID: {job.result.run_id}")
                lines.append(f"Resolution ID: {job.result.resolution_id}")
            return "\n".join(lines)

    def handle_text(self, chat_id: int, text: str) -> None:
        if int(chat_id) not in self.settings.allowed_chat_ids:
            return
        try:
            command = parse_telegram_command(text)
        except TelegramCommandError as exc:
            self.api.send_message(chat_id, str(exc))
            return

        if command.name in {"start", "help"}:
            self.api.send_message(
                chat_id,
                telegram_help_text(monitoring_enabled=self.monitor.enabled),
            )
            return
        if command.name == "analyze":
            self._queue_analysis(chat_id, str(command.symbol), blind=False)
            return
        if command.name == "blind":
            self._queue_analysis(chat_id, str(command.symbol), blind=True)
            return
        if command.name == "status":
            self.api.send_message(chat_id, self._status_text(chat_id, command.job_id))
            return
        if command.name == "watch":
            entry, created = self.watchlist.add(chat_id, str(command.symbol))
            state = "added" if created else "already present"
            monitor_note = (
                f"Checks run every {self.monitor.interval_minutes} minutes."
                if self.monitor.enabled
                else "Monitoring is disabled in cloud settings."
            )
            self.api.send_message(
                chat_id, f"{entry.symbol} is {state} in your watchlist. {monitor_note}"
            )
            return
        if command.name == "unwatch":
            removed = self.watchlist.remove(chat_id, str(command.symbol))
            self.api.send_message(
                chat_id,
                (
                    f"Stopped monitoring {command.symbol}."
                    if removed
                    else f"{command.symbol} was not in your watchlist."
                ),
            )
            return
        if command.name == "watchlist":
            entries = self.watchlist.list_for_chat(chat_id)
            if not entries:
                self.api.send_message(chat_id, "Your watchlist is empty.")
                return
            lines = ["Your monitored symbols:"]
            for entry in entries:
                state = "baseline ready" if entry.last_digest else "awaiting baseline"
                lines.append(f"- {entry.symbol}: {state}")
            self.api.send_message(chat_id, "\n".join(lines))

    def dispatch_update(self, update: Mapping[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, Mapping):
            return
        chat = message.get("chat")
        if not isinstance(chat, Mapping) or "id" not in chat:
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        self.handle_text(int(chat["id"]), text)

    def run_forever(self) -> None:
        self.api.delete_webhook()
        self.monitor.start()
        offset: int | None = None
        while True:
            try:
                updates = self.api.get_updates(
                    offset, self.settings.poll_timeout_seconds
                )
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    self.dispatch_update(update)
            except TelegramAPIError:
                time.sleep(5)

    def close(self) -> None:
        self.monitor.stop()
        self._executor.shutdown(wait=True, cancel_futures=False)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    agent_settings = Settings.from_environment()
    if agent_settings.provider == "packet":
        raise SystemExit(
            "ELLIOTT_PROVIDER must be openai or ollama for Telegram analyses."
        )
    if str(agent_settings.model or "").strip().upper() in {
        "YOUR_WORKING_MODEL",
        "<YOUR_WORKING_MODEL>",
    }:
        raise SystemExit(
            "ELLIOTT_MODEL still contains the placeholder YOUR_WORKING_MODEL. "
            "Set it to the model that works for your API project."
        )
    bot_settings = TelegramBotSettings.from_environment(
        workspace=agent_settings.workspace
    )
    api = TelegramBotAPI(bot_settings.bot_token)
    analysis_service = CloudAnalysisService(
        agent_settings=agent_settings,
        bot_settings=bot_settings,
    )
    watchlist = WatchlistStore(bot_settings.watchlist_path)
    bot = TelegramElliottBot(
        settings=bot_settings,
        api=api,
        analysis_service=analysis_service,
        watchlist=watchlist,
    )
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bot.close()


if __name__ == "__main__":
    main()
