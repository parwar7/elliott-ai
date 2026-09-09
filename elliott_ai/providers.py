"""Model-provider adapters with no mandatory third-party dependencies."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings


class ProviderError(RuntimeError):
    pass


class AnalysisProvider(Protocol):
    name: str
    model: str | None

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
    ) -> dict[str, Any]: ...


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"Provider returned HTTP {exc.code}: {details[:800]}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Could not reach provider at {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError("Provider response was not valid JSON.") from exc


def _parse_model_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            raise ProviderError("Model did not return a JSON object.")
        try:
            value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        except json.JSONDecodeError as exc:
            raise ProviderError("Model output could not be parsed as JSON.") from exc
    if not isinstance(value, dict):
        raise ProviderError("Model output must be a JSON object.")
    return value


def _parse_strict_model_json(text: str) -> dict[str, Any]:
    """Parse exactly one JSON object without fences or surrounding prose."""

    if not isinstance(text, str):
        raise ProviderError("Model output must be a JSON string.")
    try:
        value = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ProviderError(
            "Model output must be exactly one JSON object with no extra prose."
        ) from exc
    if not isinstance(value, dict):
        raise ProviderError("Model output must be a JSON object.")
    return value


_OPENAI_JSON_SCHEMA_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _openai_strict_json_schema_format(
    *,
    schema: dict[str, Any],
    schema_name: str | None,
    schema_description: str | None,
) -> dict[str, Any]:
    """Build the Responses API strict structured-output format object.

    Callers opt into this explicitly so older provider paths retain their
    established prompt-only JSON behavior.  The named schema is sent to the
    Responses API before the response is parsed locally.
    """

    if not isinstance(schema_name, str) or _OPENAI_JSON_SCHEMA_NAME_PATTERN.fullmatch(schema_name) is None:
        raise ProviderError(
            "OpenAI strict JSON Schema requests require schema_name matching [A-Za-z0-9_-]{1,64}."
        )
    if schema_description is not None and (not isinstance(schema_description, str) or not schema_description.strip()):
        raise ProviderError("OpenAI strict JSON Schema description must be a non-empty string when supplied.")
    if not isinstance(schema, dict):
        raise ProviderError("OpenAI strict JSON Schema must be a JSON object.")
    result: dict[str, Any] = {
        "type": "json_schema",
        "name": schema_name,
        "schema": schema,
        "strict": True,
    }
    if schema_description is not None:
        result["description"] = schema_description.strip()
    return result


def _openai_output_text(response: dict[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    texts: list[str] = []
    for output_item in response.get("output", []):
        if not isinstance(output_item, dict):
            continue
        for content in output_item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                texts.append(text)
    if not texts:
        raise ProviderError("OpenAI response did not contain text output.")
    return "\n".join(texts)


@dataclass
class PacketProvider:
    """Build and save evidence without spending tokens or inventing a count."""

    name: str = "packet"
    model: str | None = None

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        if packet.get("request", {}).get("mode") == "degree_resolution":
            source = packet.get("source_hypothesis", {})
            return {
                "resolution_status": "evidence_packet_only",
                "symbol": packet.get("request", {}).get("symbol", ""),
                "source_run_id": packet.get("request", {}).get("source_run_id"),
                "data_cutoff": None,
                "scale_mode": "undetermined",
                "scope": (
                    "Strict degree-resolution evidence assembled; no reasoning model was called."
                ),
                "degree_hierarchy": [],
                "active_position": {
                    "summary": "No degree hierarchy was generated in packet mode.",
                    "wave_ids": [],
                    "confirmation": "Run the OpenAI or Ollama degree-resolution pass.",
                    "invalidation": "Not available before a hierarchy is resolved.",
                },
                "alternate_counts": source.get("alternate_counts", []),
                "invalidation_levels": [],
                "unresolved_items": [
                    "A reasoning provider must assign and validate the standard degree hierarchy."
                ],
                "confidence": {"structure": 0.0, "degree": 0.0, "active_wave": 0.0},
                "citations": [],
                "risk_note": "No wave-degree decision or trade instruction was produced.",
            }
        summaries = packet.get("market_data", [])
        contexts = packet.get("context_data", [])
        features = packet.get("feature_manifest", {}).get(
            "active_candle_features", []
        )
        zoom_windows = packet.get("zoom_windows", [])
        data_cutoffs = [
            summary.get("end_date")
            for summary in summaries
            if isinstance(summary, dict) and summary.get("end_date")
        ]
        reconciliation_status = packet.get("data_reconciliation", {}).get(
            "overall_status", "not_available"
        )
        freshness = packet.get("data_freshness", {})
        missing_data: list[str] = []
        if freshness.get("live_stream") is not True:
            missing_data.append(
                "The analysis input is a captured TradingView snapshot, not a continuously "
                "updating MCP stream. Refresh the snapshot before any later trade decision."
            )
        if reconciliation_status == "failed":
            missing_data.append(
                "Cross-timeframe reconciliation failed. Re-export quarantined timeframes "
                "from the same symbol, session, adjustment basis, feed, and chart timezone."
            )
        elif reconciliation_status == "passed_with_volume_limitations":
            missing_data.append(
                "Price structure reconciles, but cross-timeframe volume does not. Use volume "
                "only within each unchanged feed/timeframe and do not mix daily with intraday totals."
            )
        if packet.get("wave_verification") is None:
            missing_data.append(
                "Per-wave volume/EWO verification requires completed wave boundaries and "
                "their matching candle rows."
            )
        if reconciliation_status != "failed":
            missing_data.append(
                "Choose the OpenAI or Ollama provider to perform the reasoning pass."
            )
        view_confirmation = (
            f"Prepared {sum(1 for item in zoom_windows if item.get('status') == 'prepared')} "
            "targeted recursive zoom windows."
            if zoom_windows
            else f"Prepared {len(summaries)} continuous full-history and native zoom views."
        )
        return {
            "analysis_status": "evidence_packet_only",
            "symbol": packet.get("request", {}).get("symbol", ""),
            "data_cutoff": max(data_cutoffs) if data_cutoffs else None,
            "scope": "Retrieval and deterministic evidence assembled; no model was called.",
            "preferred_count": [],
            "alternate_counts": [],
            "confirmations": [
                f"Retrieved {len(packet.get('knowledge', []))} knowledge chunks.",
                f"Inspected {len(summaries)} OHLCV datasets.",
                f"Calculated candle features: {', '.join(features) or 'none'}.",
                f"Calculated {len(contexts)} optional context datasets.",
                view_confirmation,
                f"Analysis mode: {packet.get('evidence_policy', {}).get('analysis_mode', 'unknown')}.",
                f"Cross-timeframe reconciliation: {reconciliation_status}.",
            ],
            "invalidation_levels": [],
            "missing_data": missing_data,
            "confidence": {"structure": 0.0, "degree": 0.0, "active_wave": 0.0},
            "risk_note": (
                "No trade decision was produced. Elliott counts are probabilistic and require "
                "independent risk controls."
            ),
            "citations": [],
        }


@dataclass
class OpenAIResponsesProvider:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout: int = 600
    name: str = "openai"

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        response = _post_json(
            f"{self.base_url}/responses",
            {"model": self.model, "instructions": system_prompt, "input": user_prompt},
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        return _parse_model_json(_openai_output_text(response))

    def generate_strict_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
        schema_name: str | None = None,
        schema_description: str | None = None,
        enforce_schema: bool = False,
    ) -> dict[str, Any]:
        """Historical-analysis path with strict JSON parsing.

        ``enforce_schema=True`` sends the supplied JSON Schema to the OpenAI
        Responses API as a strict ``text.format`` contract before local
        parsing.  The default remains prompt-only for backwards compatibility
        with existing historical analysis callers.
        """

        if not isinstance(enforce_schema, bool):
            raise TypeError("enforce_schema must be boolean.")
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": system_prompt,
            "input": user_prompt,
        }
        if enforce_schema:
            payload["text"] = {
                "format": _openai_strict_json_schema_format(
                    schema=schema,
                    schema_name=schema_name,
                    schema_description=schema_description,
                )
            }
        response = _post_json(
            f"{self.base_url}/responses",
            payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        return _parse_strict_model_json(_openai_output_text(response))


@dataclass
class OllamaProvider:
    model: str
    base_url: str = "http://localhost:11434"
    timeout: int = 300
    name: str = "ollama"

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        response = _post_json(
            f"{self.base_url}/api/generate",
            {
                "model": self.model,
                "prompt": f"{system_prompt}\n\n{user_prompt}",
                "stream": False,
                "format": schema,
            },
            timeout=self.timeout,
        )
        text = response.get("response")
        if not isinstance(text, str):
            raise ProviderError("Ollama response did not contain a response string.")
        return _parse_model_json(text)

    def generate_strict_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        response = _post_json(
            f"{self.base_url}/api/generate",
            {
                "model": self.model,
                "prompt": f"{system_prompt}\n\n{user_prompt}",
                "stream": False,
                "format": schema,
                "options": {"temperature": 0, "seed": 0},
            },
            timeout=self.timeout,
        )
        text = response.get("response")
        if not isinstance(text, str):
            raise ProviderError("Ollama response did not contain a response string.")
        return _parse_strict_model_json(text)


def create_provider(settings: Settings) -> AnalysisProvider:
    if settings.provider == "packet":
        return PacketProvider()
    if settings.provider == "openai":
        if not settings.openai_api_key:
            raise ProviderError("OPENAI_API_KEY is required for the OpenAI provider.")
        if not settings.model:
            raise ProviderError("ELLIOTT_MODEL or --model is required.")
        return OpenAIResponsesProvider(
            api_key=settings.openai_api_key,
            model=settings.model,
            base_url=settings.openai_base_url,
        )
    if settings.provider == "ollama":
        if not settings.model:
            raise ProviderError("ELLIOTT_MODEL or --model is required for Ollama.")
        return OllamaProvider(model=settings.model, base_url=settings.ollama_base_url)
    raise ProviderError(f"Unknown provider: {settings.provider}")
