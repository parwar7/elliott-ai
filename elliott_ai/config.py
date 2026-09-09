"""Configuration helpers for the Elliott AI agent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = PACKAGE_DIR.parent


def _environment_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be true/false, yes/no, on/off, or 1/0.")


@dataclass(frozen=True)
class Settings:
    workspace: Path
    database_path: Path
    provider: str
    model: str | None
    openai_api_key: str | None
    openai_base_url: str
    ollama_base_url: str
    rsi_enabled: bool = False

    @classmethod
    def from_environment(
        cls,
        *,
        workspace: Path | None = None,
        database_path: Path | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> "Settings":
        resolved_workspace = Path(
            workspace or os.getenv("ELLIOTT_WORKSPACE", DEFAULT_WORKSPACE)
        ).resolve()
        resolved_database = Path(
            database_path
            or os.getenv(
                "ELLIOTT_DATABASE",
                resolved_workspace / ".elliott_ai" / "elliott_ai.sqlite3",
            )
        ).resolve()
        resolved_provider = (provider or os.getenv("ELLIOTT_PROVIDER", "packet")).lower()
        resolved_model = model or os.getenv("ELLIOTT_MODEL")
        if not resolved_model and resolved_provider == "openai":
            resolved_model = "gpt-5.6-terra"
        return cls(
            workspace=resolved_workspace,
            database_path=resolved_database,
            provider=resolved_provider,
            model=resolved_model,
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_base_url=os.getenv(
                "OPENAI_BASE_URL", "https://api.openai.com/v1"
            ).rstrip("/"),
            ollama_base_url=os.getenv(
                "OLLAMA_BASE_URL", "http://localhost:11434"
            ).rstrip("/"),
            rsi_enabled=_environment_flag("ELLIOTT_ENABLE_RSI", False),
        )
