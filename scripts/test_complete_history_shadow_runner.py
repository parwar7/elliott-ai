"""Focused offline tests for the manual complete-history shadow runner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any, Mapping

from elliott_ai.complete_history_shadow_runner import (
    CompleteHistoryCandidateProvider,
    CompleteHistoryHumanApproval,
    CompleteHistoryLocalArtifactStore,
    CompleteHistoryMaximumStage,
    CompleteHistoryStageArtifact,
    CompleteHistoryStageArtifactOutputKind,
    CompleteHistoryStage1HardRuleCode,
    CompleteHistoryShadowError,
    CompleteHistoryShadowReasonCode,
    CompleteHistoryShadowRunner,
    build_complete_history_shadow_plan,
    complete_history_human_approval_content_hash,
    complete_history_stage_call_limit,
    load_complete_history_bundle,
    screen_complete_history_stage1_child_graph,
)
from elliott_ai.complete_history_openai_adapter import CompleteHistoryOpenAICandidateProvider
from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.lower_timeframe_recursive_proof import RecursiveProofHypothesisRole
from elliott_ai.lower_timeframe_subdivision_verifier import SubdivisionVerificationStatus


UTC_CUTOFF = "2026-08-07T20:00:00Z"


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_document_hash(value: Mapping[str, Any]) -> str:
    payload = json.loads(json.dumps(value))
    payload.pop("canonical_content_sha256", None)
    if isinstance(payload.get("metadata"), dict):
        payload["metadata"].pop("canonical_content_sha256", None)
    return canonical_sha256(payload)


def _write_json(path: Path, value: Mapping[str, Any]) -> tuple[str, str]:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    return _file_hash(path), _canonical_document_hash(value)


def _candle(date: str, low: float, high: float) -> dict[str, Any]:
    return {
        "date": date,
        "open": (low + high) / 2,
        "high": high,
        "low": low,
        "close": (low + high) / 2,
        "volume": 1_000.0,
    }


ROOT_ROWS = (
    _candle("2020-01-01T00:00:00+00:00", 100.0, 101.0),
    _candle("2020-01-10T00:00:00+00:00", 119.0, 120.0),
    _candle("2020-01-20T00:00:00+00:00", 110.0, 111.0),
    _candle("2020-01-30T00:00:00+00:00", 129.0, 130.0),
    _candle("2020-02-10T00:00:00+00:00", 80.0, 90.0),
)


DAILY_ROWS = (
    _candle("2020-01-01T00:00:00+00:00", 100.0, 101.0),
    _candle("2020-01-02T00:00:00+00:00", 107.0, 110.0),
    _candle("2020-01-03T00:00:00+00:00", 105.0, 109.0),
    _candle("2020-01-10T00:00:00+00:00", 119.0, 120.0),
    _candle("2020-01-11T00:00:00+00:00", 115.0, 116.0),
    _candle("2020-01-12T00:00:00+00:00", 117.0, 118.0),
    _candle("2020-01-20T00:00:00+00:00", 110.0, 111.0),
    _candle("2020-01-21T00:00:00+00:00", 118.0, 120.0),
    _candle("2020-01-22T00:00:00+00:00", 115.0, 119.0),
    _candle("2020-01-30T00:00:00+00:00", 129.0, 130.0),
    _candle("2020-02-10T00:00:00+00:00", 80.0, 90.0),
)


FOUR_HOUR_ROWS = (
    _candle("2022-01-03T14:30:00+00:00", 140.0, 145.0),
    _candle("2022-01-03T18:30:00+00:00", 142.0, 147.0),
)


def _data_document(timeframe: str, rows: tuple[Mapping[str, Any], ...], *, extra_metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    interval = {"monthly": "monthly", "weekly": "weekly", "daily": "daily", "4h": "4h", "1h": "1h", "15m": "15m"}[timeframe]
    metadata: dict[str, Any] = {
        "timeframe": timeframe,
        "provider": "twelve_data",
        "requested_symbol": "NASDAQ:GOOGL",
        "resolved_symbol": "GOOGL",
        "exchange": "NASDAQ",
        "session": "regular",
        "timezone": "UTC",
        "requested_adjustment": "splits",
        "price_basis": "split_adjusted_dividend_unadjusted_ohlcv",
        "adjustment_policy": "split-adjusted, dividend-unadjusted OHLCV",
        "native_or_resampled": "native_provider_candles",
        "completed_candles_only": True,
        "final_candle_completed": True,
        "validation_status": "data_contract_passed",
        "feed_identity": f"twelve_data|NASDAQ|GOOGL|{interval}|splits|regular",
        "dataset_cutoff_utc": UTC_CUTOFF,
        "candle_count": len(rows),
        "first_candle_timestamp_utc": rows[0]["date"],
        "last_candle_timestamp_utc": rows[-1]["date"],
        "provider_interval": interval,
        "regular_session_definition": "NASDAQ 09:30-16:00 America/New_York",
        "schema_version": "phase11-market-data-candles/1.0.0",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    document: dict[str, Any] = {"candles": list(rows), "metadata": metadata}
    metadata["canonical_content_sha256"] = _canonical_document_hash(document)
    return document


def _root_bridge(*, valid: bool = True) -> dict[str, Any]:
    bridge: dict[str, Any] = {
        "bridge_id": "fixture-class-a-bridge",
        "continuity_verdict": "verified_class_a_continuity" if valid else "invalid",
        "instrument": {
            "current_symbol": "NASDAQ:GOOGL",
            "security_class": "Alphabet Inc. Class A common stock",
        },
        "excluded_security": {
            "ticker": "GOOG",
            "security_class": "Class C capital stock",
            "from_utc": "2014-04-03T00:00:00Z",
        },
        "provider_policy": {
            "forbidden_post_2014_symbol_merge": "GOOG Class C",
            "manual_adjustments_performed": False,
            "requested_symbol": "NASDAQ:GOOGL",
            "provider_symbol": "GOOGL",
        },
        "ticker_history": (
            {"ticker": "GOOG", "security_class": "Class A common stock"},
            {"ticker": "GOOGL", "security_class": "Class A common stock"},
        ),
        "schema_version": "googl-class-a-identity-bridge/1.0.0",
    }
    bridge["canonical_content_sha256"] = _canonical_document_hash(bridge)
    return bridge


def _write_bundle(
    directory: Path,
    *,
    bridge_valid: bool = True,
    four_hour_rows: tuple[Mapping[str, Any], ...] | None = None,
) -> Path:
    source = directory / "sources"
    bundle = directory / "bundle"
    source.mkdir()
    bundle.mkdir()
    source_docs: dict[str, tuple[Path, dict[str, Any], str, str]] = {}
    rows_by_timeframe = {
        "monthly": ROOT_ROWS,
        "weekly": ROOT_ROWS,
        "daily": DAILY_ROWS[5:],
        "4h": four_hour_rows or FOUR_HOUR_ROWS,
        "1h": FOUR_HOUR_ROWS,
        "15m": FOUR_HOUR_ROWS,
    }
    for timeframe, rows in rows_by_timeframe.items():
        path = source / f"GOOGL_{timeframe}_closed.json"
        document = _data_document(timeframe, rows)
        file_hash, canonical_hash = _write_json(path, document)
        source_docs[timeframe] = (path, document, file_hash, canonical_hash)
    extension_path = source / "GOOGL_daily_extension.json"
    extension_doc = _data_document("daily", DAILY_ROWS[:5])
    extension_file_hash, extension_canonical_hash = _write_json(extension_path, extension_doc)
    decision_manifest_path = source / "GOOGL_decision_manifest.json"
    decision_manifest_path.write_text('{"kind":"fixture-decision-manifest"}', encoding="utf-8")
    extension_manifest_path = source / "GOOGL_daily_extension_manifest.json"
    extension_manifest_path.write_text('{"kind":"fixture-extension-manifest"}', encoding="utf-8")

    bridge_path = bundle / "GOOGL_class_a_identity_bridge.json"
    bridge = _root_bridge(valid=bridge_valid)
    bridge_file_hash, bridge_canonical_hash = _write_json(bridge_path, bridge)
    full_daily_path = bundle / "GOOGL_daily_full_history_closed.json"
    daily_snapshot = source_docs["daily"]
    full_daily_doc = _data_document(
        "daily",
        DAILY_ROWS,
        extra_metadata={
            "schema_version": "phase11-full-history-daily-candles/1.0.0",
            "identity_bridge_hash": bridge_canonical_hash,
            "shared_forecast_cutoff_utc": UTC_CUTOFF,
            "source_components": (
                {
                    "path": str(extension_path),
                    "file_sha256": extension_file_hash,
                    "canonical_content_sha256": extension_canonical_hash,
                    "candle_count": len(DAILY_ROWS[:5]),
                },
                {
                    "path": str(daily_snapshot[0]),
                    "file_sha256": daily_snapshot[2],
                    "canonical_content_sha256": daily_snapshot[3],
                    "candle_count": len(DAILY_ROWS[5:]),
                },
            ),
        },
    )
    full_daily_file_hash, full_daily_canonical_hash = _write_json(full_daily_path, full_daily_doc)
    coverage_path = bundle / "GOOGL_full_history_coverage_map.json"
    coverage_timeframes: dict[str, Any] = {}
    for timeframe in ("monthly", "weekly", "daily", "4h", "1h", "15m"):
        document = full_daily_doc if timeframe == "daily" else source_docs[timeframe][1]
        metadata = document["metadata"]
        entry: dict[str, Any] = {
            "timeframe": timeframe,
            "candle_count": metadata["candle_count"],
            "coverage_start_utc": metadata["first_candle_timestamp_utc"],
            "coverage_end_utc": metadata["last_candle_timestamp_utc"],
            "dataset_cutoff_utc": metadata["dataset_cutoff_utc"],
        }
        if timeframe == "4h":
            entry["proof_coverage"] = {
                "available_from_utc": metadata["first_candle_timestamp_utc"],
                "before_available_from_utc": "not_covered",
            }
        coverage_timeframes[timeframe] = entry
    coverage: dict[str, Any] = {
        "symbol": "NASDAQ:GOOGL",
        "shared_forecast_cutoff_utc": UTC_CUTOFF,
        "proof_constraints": {
            "native_4h_required_for_recursive_4h_proof": True,
            "older_structure_status_before_native_4h_start": "not_covered",
        },
        "timeframes": coverage_timeframes,
        "schema_version": "googl-full-history-coverage-map/1.0.0",
    }
    coverage["canonical_content_sha256"] = _canonical_document_hash(coverage)
    coverage_file_hash, coverage_canonical_hash = _write_json(coverage_path, coverage)

    frozen_entries: dict[str, Any] = {}
    for timeframe, (path, document, file_hash, canonical_hash) in source_docs.items():
        metadata = document["metadata"]
        frozen_entries[timeframe] = {
            "reference_path": str(path),
            "file_sha256": file_hash,
            "source_manifest_canonical_content_sha256": canonical_hash,
            "dataset_cutoff_utc": metadata["dataset_cutoff_utc"],
        }
    manifest: dict[str, Any] = {
        "bundle_id": "fixture-googl-full-history",
        "kind": "immutable_full_history_dataset_bundle",
        "schema_version": "googl-full-history-bundle-manifest/1.0.0",
        "shared_forecast_cutoff_utc": UTC_CUTOFF,
        "source_snapshot_shared_cutoff_utc": UTC_CUTOFF,
        "instrument": {
            "symbol": "NASDAQ:GOOGL",
            "provider": "twelve_data",
            "session": "regular",
            "price_basis": "split_adjusted_dividend_unadjusted_ohlcv",
        },
        "bundle_files": {
            "combined_daily": {
                "filename": full_daily_path.name,
                "file_sha256": full_daily_file_hash,
                "canonical_content_sha256": full_daily_canonical_hash,
                "file_size_bytes": full_daily_path.stat().st_size,
            },
            "coverage_map": {
                "filename": coverage_path.name,
                "file_sha256": coverage_file_hash,
                "canonical_content_sha256": coverage_canonical_hash,
                "file_size_bytes": coverage_path.stat().st_size,
            },
            "identity_bridge": {
                "filename": bridge_path.name,
                "file_sha256": bridge_file_hash,
                "canonical_content_sha256": bridge_canonical_hash,
                "file_size_bytes": bridge_path.stat().st_size,
            },
        },
        "immutable_sources": {
            "frozen_snapshot": {
                "datasets": frozen_entries,
                "decision_manifest_path": str(decision_manifest_path),
                "decision_manifest_file_sha256": _file_hash(decision_manifest_path),
            },
            "daily_history_extension": {
                "manifest_path": str(extension_manifest_path),
                "manifest_file_sha256": _file_hash(extension_manifest_path),
            },
        },
    }
    manifest["canonical_content_sha256"] = _canonical_document_hash(manifest)
    (bundle / "GOOGL_full_history_bundle_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return bundle


class _FakeProvider:
    """Strict offline fixture for the active-parent Monthly-map contract."""

    # The runner verifies that an approval's immutable provider/model binding
    # agrees with the supplied provider. This remains a no-network fixture.
    name = "openai"
    model = "gpt-5.6-terra"

    def __init__(
        self,
        *,
        invalid_map: bool = False,
        invalid_terminal_segment: bool = False,
        duplicate_maps: bool = False,
        no_completed_segments: bool = False,
    ) -> None:
        self.invalid_map = invalid_map
        self.invalid_terminal_segment = invalid_terminal_segment
        self.duplicate_maps = duplicate_maps
        self.no_completed_segments = no_completed_segments
        self.calls: list[tuple[str, str]] = []
        self.packets: list[Mapping[str, Any]] = []

    @staticmethod
    def _by_timestamp(catalog: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for item in catalog:
            groups.setdefault(item["timestamp_utc"], []).append(item)
        return [groups[key] for key in sorted(groups)]

    @staticmethod
    def _pivot_for(groups: Sequence[Sequence[Mapping[str, Any]]], *, timestamp: str, price: float) -> Mapping[str, Any]:
        for group in groups:
            for item in group:
                if item["timestamp_utc"] == timestamp and abs(item["price"] - price) < 1e-9:
                    return item
        raise AssertionError("Fixture packet did not preserve a matching pivot.")

    @staticmethod
    def _invalidation(start: Mapping[str, Any], direction: str) -> dict[str, Any]:
        return {
            "invalidation_id": f"fixture-invalidation-{start['pivot_id']}",
            "threshold_price": 0.0 if direction == "up" else 10_000.0,
            "direction": "below" if direction == "up" else "above",
            "evaluation_basis": "intrabar_touch_or_breach",
            "source_pivot_id": start["pivot_id"],
        }

    def _monthly_map_result(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        catalog = packet["pivot_catalog"]
        groups = self._by_timestamp(catalog)
        start = min(groups[0], key=lambda item: item["price"])
        completed_end = max(groups[-2], key=lambda item: item["price"])
        end = min(groups[-1], key=lambda item: item["price"])
        use_primary_shape = role is RecursiveProofHypothesisRole.PRIMARY or self.duplicate_maps
        completed_family = "zigzag" if use_primary_shape else "flat"
        active_family = "impulse" if use_primary_shape else "combination"
        result: dict[str, Any] = {
            "map_id": f"{role.value}-monthly-map",
            "outer_parent": {
                "parent_id": f"{role.value}-outer-active",
                "degree": "Cycle",
                "declared_family": "impulse" if use_primary_shape else "diagonal",
                "direction": "down",
                "completion_state": "active",
                "start_pivot_id": start["pivot_id"],
                "end_pivot_id": end["pivot_id"],
                "invalidation": self._invalidation(start, "down"),
            },
            "origin_control": None,
            "as_of_monthly_candle_id": packet["as_of_monthly_candle"]["candle_id"],
            "segments": (
                (
                    {
                        "segment_id": f"{role.value}-monthly-active",
                        "degree": "Primary",
                        "declared_family": active_family,
                        "direction": "down",
                        "completion_state": "active",
                        "start_pivot_id": start["pivot_id"],
                        "end_pivot_id": end["pivot_id"],
                        "invalidation": self._invalidation(start, "down"),
                    },
                )
                if self.no_completed_segments
                else (
                    {
                        "segment_id": f"{role.value}-monthly-completed",
                        "degree": "Primary",
                        "declared_family": completed_family,
                        "direction": "up",
                        "completion_state": "completed",
                        "start_pivot_id": start["pivot_id"],
                        "end_pivot_id": completed_end["pivot_id"],
                        "invalidation": self._invalidation(start, "up"),
                    },
                    {
                        "segment_id": f"{role.value}-monthly-active",
                        "degree": "Primary",
                        "declared_family": active_family,
                        "direction": "down",
                        "completion_state": "active",
                        "start_pivot_id": completed_end["pivot_id"],
                        "end_pivot_id": end["pivot_id"],
                        "invalidation": self._invalidation(completed_end, "down"),
                    },
                )
            ),
        }
        if self.invalid_map:
            result["verification_status"] = "verified"
        if self.invalid_terminal_segment:
            result["segments"] = tuple(
                {
                    **segment,
                    "completion_state": "completed",
                }
                for segment in result["segments"]
            )
        return result

    def generate_monthly_map(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del output_schema
        self.calls.append((role.value, "monthly_map"))
        self.packets.append(packet)
        return self._monthly_map_result(role=role, packet=packet)

    def generate_monthly_root_candidate(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Historical protocol method retained for adapter compatibility tests."""

        del output_schema
        self.calls.append((role.value, "monthly_root"))
        self.packets.append(packet)
        catalog = packet["pivot_catalog"]
        groups = self._by_timestamp(catalog)
        start = min(groups[0], key=lambda item: item["price"])
        end = max(groups[-1], key=lambda item: item["price"])
        return {
            "candidate_id": f"{role.value}-root",
            "degree": "Cycle",
            "declared_family": "combination",
            "direction": "up",
            "completion_state": "active",
            "start_pivot_id": start["pivot_id"],
            "end_pivot_id": end["pivot_id"],
            "origin_control": None,
            "invalidation": self._invalidation(start, "up"),
        }

    def _child_graph_response(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        parent = packet["parent_wave"]
        target = packet["target_timeframe"]
        groups = self._by_timestamp(packet["pivot_catalog"])
        start = self._pivot_for(groups, timestamp=parent["start_pivot"]["timestamp_utc"], price=parent["start_pivot"]["price"])
        end = self._pivot_for(groups, timestamp=parent["end_pivot"]["timestamp_utc"], price=parent["end_pivot"]["price"])
        middle_groups = [group for group in groups if start["timestamp_utc"] < group[0]["timestamp_utc"] < end["timestamp_utc"]]
        if len(middle_groups) < 2:
            raise AssertionError("Fixture Weekly window requires two internal pivot groups.")
        family = parent["declared_family"]
        points = (start, max(middle_groups[0], key=lambda item: item["price"]), min(middle_groups[-1], key=lambda item: item["price"]), end)
        if family == "combination":
            positions = ("W", "X", "Y")
            child_families = ("zigzag", "zigzag", "zigzag")
        elif family == "flat":
            positions = ("A", "B", "C")
            child_families = ("zigzag", "zigzag", "impulse")
        else:
            positions = ("A", "B", "C")
            child_families = ("impulse", "zigzag", "impulse")
        children = []
        for index, position in enumerate(positions):
            child_start = points[index]
            child_end = points[index + 1]
            direction = "up" if child_end["price"] > child_start["price"] else "down"
            children.append(
                {
                    "child_id": f"{role.value}-{target}-{parent['wave_id']}-{position}",
                    "sequence_position": position,
                    "direction": direction,
                    "declared_family": child_families[index],
                    "start_pivot_id": child_start["pivot_id"],
                    "end_pivot_id": child_end["pivot_id"],
                    "invalidation": self._invalidation(child_start, direction),
                }
            )
        return {
            "graph_id": f"{role.value}-{target}-{parent['wave_id']}",
            "declared_family": family,
            "boundary_lineage": list(packet["boundary_lineage"]),
            "children": children,
        }

    def generate_child_graph(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        target = packet["target_timeframe"]
        self.calls.append((role.value, target))
        self.packets.append(packet)
        return self._child_graph_response(role=role, packet=packet)

    def generate_weekly_child_graph_batch(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del output_schema
        self.calls.append((role.value, "weekly_batch"))
        self.packets.append(packet)
        graphs = []
        for request in packet["segment_requests"]:
            graph_packet = {
                "parent_wave": request["parent_wave"],
                "target_timeframe": request["target_timeframe"],
                "pivot_catalog": request["pivot_catalog"],
                "boundary_lineage": request["boundary_lineage"],
            }
            graphs.append(
                {
                    "segment_id": request["segment_id"],
                    "graph": self._child_graph_response(role=role, packet=graph_packet),
                }
            )
        return {"batch_id": f"{role.value}-weekly-batch", "graphs": graphs}


class _FourHourProbeProvider(_FakeProvider):
    """Stops after proving that a coverage-eligible branch reaches native 4h."""

    def generate_child_graph(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if packet["target_timeframe"] == "4h":
            self.calls.append((role.value, "4h"))
            # The strict output parser must reject this; the test only needs
            # to establish that the runner prepared the 4h request from a
            # fully covered native window.
            return {"unexpected": "verification_status"}
        return super().generate_child_graph(role=role, packet=packet, output_schema=output_schema)


class _TruncatedRootScopeProvider(_FakeProvider):
    """Produces a late-only map with an invalid oversized IPO control."""

    def __init__(self, *, role_to_truncate: RecursiveProofHypothesisRole, label: str) -> None:
        super().__init__()
        self.role_to_truncate = role_to_truncate
        self.label = label

    def generate_monthly_map(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        result = json.loads(
            json.dumps(
                super().generate_monthly_map(
                    role=role,
                    packet=packet,
                    output_schema=output_schema,
                )
            )
        )
        if role is self.role_to_truncate:
            groups = self._by_timestamp(packet["pivot_catalog"])
            late = min(groups[2], key=lambda item: item["price"])
            result["map_id"] = self.label
            result["outer_parent"]["start_pivot_id"] = late["pivot_id"]
            result["outer_parent"]["invalidation"] = self._invalidation(late, "down")
            result["segments"][0]["start_pivot_id"] = late["pivot_id"]
            result["segments"][0]["invalidation"] = self._invalidation(
                late,
                "up",
            )
            result["origin_control"] = {
                "start_pivot_id": min(groups[0], key=lambda item: item["price"])["pivot_id"],
                "end_pivot_id": late["pivot_id"],
            }
        return result


class _NoCallProvider:
    """A resume probe that fails immediately if the runner invokes it."""

    name = _FakeProvider.name
    model = _FakeProvider.model

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def generate_monthly_map(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del packet, output_schema
        self.calls.append((role.value, "monthly_map"))
        raise AssertionError("The resumed Monthly-map artifact should prevent a provider call.")

    def generate_weekly_child_graph_batch(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del packet, output_schema
        self.calls.append((role.value, "weekly_batch"))
        raise AssertionError("The resumed Weekly-batch artifact should prevent a provider call.")

    def generate_monthly_root_candidate(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((role.value, "monthly_root"))
        raise AssertionError("The resumed root artifact should prevent a provider call.")

    def generate_child_graph(
        self,
        *,
        role: RecursiveProofHypothesisRole,
        packet: Mapping[str, Any],
        output_schema: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((role.value, packet["target_timeframe"]))
        raise AssertionError("The resumed child-graph artifact should prevent a provider call.")


class _InvalidWeeklyAdapterTransport:
    """Provides valid maps, then one invalid Weekly batch without retrying."""

    name = "openai"
    model = "gpt-5.6-terra"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fixture = _FakeProvider()

    def generate_strict_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        packet: dict[str, Any],
    ) -> dict[str, Any]:
        del system_prompt, user_prompt
        role = RecursiveProofHypothesisRole(packet["role"])
        if packet["packet_kind"] == "blind_complete_history_monthly_map":
            self.calls.append((role.value, "monthly_map"))
            return dict(
                self._fixture._monthly_map_result(
                    role=role,
                    packet=packet,
                )
            )
        self.calls.append((role.value, "weekly_batch"))
        return {
            "batch_id": "invalid-weekly-response",
            "graphs": [],
            "verification_status": "verified",
        }


class CompleteHistoryShadowRunnerTests(unittest.TestCase):
    def _bundle(
        self,
        *,
        bridge_valid: bool = True,
        four_hour_rows: tuple[Mapping[str, Any], ...] | None = None,
    ):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = _write_bundle(
            Path(temporary.name),
            bridge_valid=bridge_valid,
            four_hour_rows=four_hour_rows,
        )
        return directory

    def test_verified_bundle_preflight_builds_a_deterministic_stage1_four_call_plan(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        first = build_complete_history_shadow_plan(bundle)
        second = build_complete_history_shadow_plan(bundle)

        self.assertEqual(bundle.dataset("daily").coverage_start_utc, "2020-01-01T00:00:00+00:00")
        self.assertEqual(bundle.dataset("4h").coverage_start_utc, "2022-01-03T14:30:00+00:00")
        self.assertEqual(first.total_reserved_model_calls, 4)
        self.assertEqual(first.hypotheses[0].reserved_model_calls, 2)
        self.assertEqual(first.hypotheses[0].maximum_nodes, 67)
        self.assertEqual(
            tuple(stage.target_timeframe for stage in first.hypotheses[0].stages),
            ("monthly", "weekly"),
        )
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertTrue(first.requires_human_approval)
        self.assertIn("four possible calls", " ".join(first.warnings))

    def test_rules_pack_hash_binds_plan_approval_packets_and_artifacts(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        rules_hash = plan.policy.blind_candidate_rules_pack.content_hash
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            max_stage=CompleteHistoryMaximumStage.WEEKLY,
            exact_call_limit=4,
            rules_pack_content_hash=rules_hash,
        )
        provider = _FakeProvider()

        with tempfile.TemporaryDirectory() as output_directory:
            execution = CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=approval,
                allow_model_calls=True,
                output_directory=output_directory,
            )
            self.assertEqual(execution.attempted_provider_calls, 4)
            self.assertEqual(len(provider.packets), 4)
            self.assertEqual({packet["role"] for packet in provider.packets}, {"primary", "alternative"})
            for packet in provider.packets:
                self.assertEqual(packet["rules_pack_content_hash"], rules_hash)
                self.assertEqual(packet["constraints"]["rules_pack_content_hash"], rules_hash)
                self.assertEqual(packet["blind_candidate_rules_pack"]["content_hash"], rules_hash)
            for artifact_path in Path(output_directory).glob("complete_history_stage_*.json"):
                payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["rules_pack_content_hash"], rules_hash)

        mismatched = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            max_stage=CompleteHistoryMaximumStage.MONTHLY,
            exact_call_limit=2,
            rules_pack_content_hash="f" * 64,
        )
        blocked_provider = _FakeProvider()
        with self.assertRaises(CompleteHistoryShadowError) as raised:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=blocked_provider,
                approval=mismatched,
                allow_model_calls=True,
            )
        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.APPROVAL_RULES_PACK_MISMATCH)
        self.assertEqual(blocked_provider.calls, [])

    def test_tampered_source_hash_blocks_preflight(self) -> None:
        directory = self._bundle()
        monthly = directory.parent / "sources" / "GOOGL_monthly_closed.json"
        monthly.write_text(monthly.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            load_complete_history_bundle(directory)

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.FILE_HASH_INVALID)

    def test_combined_daily_component_count_mismatch_blocks_preflight(self) -> None:
        directory = self._bundle()
        daily_path = directory / "GOOGL_daily_full_history_closed.json"
        daily = json.loads(daily_path.read_text(encoding="utf-8"))
        daily["metadata"]["source_components"][0]["candle_count"] += 1
        daily["metadata"]["canonical_content_sha256"] = _canonical_document_hash(daily)
        daily_file_hash, daily_canonical_hash = _write_json(daily_path, daily)

        manifest_path = directory / "GOOGL_full_history_bundle_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["bundle_files"]["combined_daily"].update(
            {
                "file_sha256": daily_file_hash,
                "canonical_content_sha256": daily_canonical_hash,
                "file_size_bytes": daily_path.stat().st_size,
            }
        )
        manifest["canonical_content_sha256"] = _canonical_document_hash(manifest)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            load_complete_history_bundle(directory)

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.DATASET_COVERAGE_INVALID)

    def test_invalid_class_a_bridge_blocks_preflight(self) -> None:
        with self.assertRaises(CompleteHistoryShadowError) as raised:
            load_complete_history_bundle(self._bundle(bridge_valid=False))

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.IDENTITY_BRIDGE_INVALID)

    def test_shared_cutoff_contradiction_blocks_preflight(self) -> None:
        directory = self._bundle()
        manifest_path = directory / "GOOGL_full_history_bundle_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["shared_forecast_cutoff_utc"] = "2020-01-01T00:00:00Z"
        manifest["canonical_content_sha256"] = _canonical_document_hash(manifest)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            load_complete_history_bundle(directory)

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.DATASET_CUTOFF_INVALID)

    def test_late_only_primary_monthly_map_is_rejected_before_weekly_generation(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
        )
        provider = _TruncatedRootScopeProvider(
            role_to_truncate=RecursiveProofHypothesisRole.PRIMARY,
            label="primary-2022-only-root",
        )

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=approval,
                allow_model_calls=True,
            )

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID)
        self.assertIn(CompleteHistoryStage1HardRuleCode.ORIGIN_CONTROL_INVALID.value, raised.exception.reason_codes)
        self.assertEqual(provider.calls, [("primary", "monthly_map")])

    def test_late_only_alternative_monthly_map_is_rejected_before_weekly_generation(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
        )
        provider = _TruncatedRootScopeProvider(
            role_to_truncate=RecursiveProofHypothesisRole.ALTERNATIVE,
            label="alternative-2026-only-root",
        )

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=approval,
                allow_model_calls=True,
            )

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.STAGE1_SCOPE_INVALID)
        self.assertIn(CompleteHistoryStage1HardRuleCode.ORIGIN_CONTROL_INVALID.value, raised.exception.reason_codes)
        self.assertEqual(
            provider.calls,
            [("primary", "monthly_map"), ("alternative", "monthly_map")],
        )

    @staticmethod
    def _stage1_screen_fixture(*, final_child_price: float = 408.61):
        def pivot(pivot_id: str, price: float):
            return SimpleNamespace(pivot_id=pivot_id, price=price)

        parent_start = pivot("monthly-ipo", 100.0)
        parent_end = pivot("monthly-final", 408.61)
        child_start = pivot("weekly-ipo", 100.0)
        child_one_end = pivot("weekly-wave-1-high", 153.78)
        child_two_end = pivot("weekly-wave-2-low", 120.0)
        child_three_end = pivot("weekly-wave-3-high", 250.0)
        child_four_end = pivot("weekly-wave-4-low", 140.53)
        child_five_end = pivot("weekly-final", final_child_price)

        def child(position: str, start, end):
            return SimpleNamespace(sequence_position=position, start_pivot=start, end_pivot=end)

        graph = SimpleNamespace(
            declared_family="impulse",
            children=(
                child("1", child_start, child_one_end),
                child("2", child_one_end, child_two_end),
                child("3", child_two_end, child_three_end),
                child("4", child_three_end, child_four_end),
                child("5", child_four_end, child_five_end),
            ),
        )
        request = SimpleNamespace(
            stage="weekly_children",
            parent_wave=SimpleNamespace(direction="up", start_pivot=parent_start, end_pivot=parent_end),
            parent_boundary_lineage=(
                SimpleNamespace(parent_pivot=parent_start, child_pivot=child_start),
                SimpleNamespace(parent_pivot=parent_end, child_pivot=pivot("weekly-parent-final", 408.61)),
            ),
        )
        return request, graph

    def test_stage1_rejects_153_78_wave_one_high_with_140_53_wave_four_overlap(self) -> None:
        request, graph = self._stage1_screen_fixture()

        screen = screen_complete_history_stage1_child_graph(request, graph)

        self.assertFalse(screen.passed)
        self.assertIn(
            CompleteHistoryStage1HardRuleCode.STANDARD_IMPULSE_WAVE4_OVERLAP,
            screen.reason_codes,
        )

    def test_stage1_rejects_402_weekly_endpoint_against_408_61_monthly_parent(self) -> None:
        request, graph = self._stage1_screen_fixture(final_child_price=402.00)

        screen = screen_complete_history_stage1_child_graph(request, graph)

        self.assertFalse(screen.passed)
        self.assertIn(
            CompleteHistoryStage1HardRuleCode.PARENT_CHILD_LINEAGE_MISMATCH,
            screen.reason_codes,
        )

    def test_stage1_rejects_incomplete_or_disconnected_child_sequences(self) -> None:
        request, graph = self._stage1_screen_fixture()
        incomplete = SimpleNamespace(declared_family="impulse", children=graph.children[:4])
        disconnected_children = list(graph.children)
        disconnected_children[2] = SimpleNamespace(
            sequence_position="3",
            start_pivot=SimpleNamespace(pivot_id="weekly-disconnected", price=120.0),
            end_pivot=graph.children[2].end_pivot,
        )
        disconnected = SimpleNamespace(declared_family="impulse", children=tuple(disconnected_children))

        incomplete_screen = screen_complete_history_stage1_child_graph(request, incomplete)
        disconnected_screen = screen_complete_history_stage1_child_graph(request, disconnected)

        self.assertIn(CompleteHistoryStage1HardRuleCode.CHILD_SEQUENCE_INCOMPLETE, incomplete_screen.reason_codes)
        self.assertIn(CompleteHistoryStage1HardRuleCode.CHILD_BOUNDARY_DISCONNECTED, disconnected_screen.reason_codes)

    def test_repaired_plan_cannot_resume_a_pre_scope_contract_artifact(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        self.assertEqual(plan.schema_version, "complete-history-shadow-plan-1.4.0")
        self.assertEqual(plan.policy.policy_id, "complete-history-shadow-policy-1.4.0")
        self.assertNotEqual(
            plan.content_hash,
            "025f8c713050a5b8b16ee5e1fb1c50fbc77e5b3f80bc3647ba4df9aeeb6dfc63",
        )
        structured_output = {"legacy": "root-artifact-kept-for-audit"}
        with tempfile.TemporaryDirectory() as output_directory:
            store = CompleteHistoryLocalArtifactStore(output_directory, bundle_directory=bundle.bundle_directory)
            legacy = CompleteHistoryStageArtifact.create(
                artifact_id="legacy-stage1-root",
                stage=CompleteHistoryMaximumStage.MONTHLY,
                role=RecursiveProofHypothesisRole.PRIMARY,
                plan_content_hash="a" * 64,
                approval_content_hash="b" * 64,
                bundle_content_hash=bundle.content_hash,
                max_stage=CompleteHistoryMaximumStage.WEEKLY,
                exact_call_limit=4,
                stage_input_hash="c" * 64,
                lineage_parent_artifact_hash=None,
                output_kind=CompleteHistoryStageArtifactOutputKind.ROOT_CANDIDATE,
                structured_output=structured_output,
                structured_output_hash=canonical_sha256(structured_output),
                normalized_output_content_hash="d" * 64,
                created_at_utc=UTC_CUTOFF,
                schema_version="complete-history-stage-artifact-1.0.0",
            )
            store.write(legacy)

            resumed = store.find(
                stage=CompleteHistoryMaximumStage.MONTHLY,
                role=RecursiveProofHypothesisRole.PRIMARY,
                plan_content_hash=plan.content_hash,
                approval_content_hash="b" * 64,
                bundle_content_hash=bundle.content_hash,
                max_stage=CompleteHistoryMaximumStage.WEEKLY,
                exact_call_limit=4,
                stage_input_hash="c" * 64,
                lineage_parent_artifact_hash=None,
            )

        self.assertIsNone(resumed)

    def test_execute_requires_a_matching_human_approval_before_provider_calls(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        provider = _FakeProvider()
        runner = CompleteHistoryShadowRunner()

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            runner.execute(bundle, plan, provider=provider, approval=None, allow_model_calls=True)

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.HUMAN_APPROVAL_REQUIRED)
        self.assertEqual(provider.calls, [])
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=canonical_sha256({"wrong": "plan"}),
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
        )
        with self.assertRaises(CompleteHistoryShadowError) as mismatched:
            runner.execute(bundle, plan, provider=provider, approval=approval, allow_model_calls=True)
        self.assertEqual(mismatched.exception.code, CompleteHistoryShadowReasonCode.APPROVAL_PLAN_MISMATCH)
        self.assertEqual(provider.calls, [])

    def test_approval_hash_binds_provider_model_and_runner_rejects_a_mismatch(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        bound = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            provider="openai",
            model="gpt-5.6-terra",
        )
        different_model = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            provider="openai",
            model="other-model",
        )
        different_provider = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            provider="other-provider",
            model="gpt-5.6-terra",
        )
        self.assertNotEqual(bound.content_hash, different_model.content_hash)
        self.assertNotEqual(bound.content_hash, different_provider.content_hash)
        provider = _FakeProvider()

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=different_model,
                allow_model_calls=True,
            )

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.APPROVAL_PROVIDER_MODEL_MISMATCH)
        self.assertEqual(provider.calls, [])

    def test_legacy_approval_hash_remains_readable_without_provider_model_fields(self) -> None:
        legacy_payload = {
            "approval_id": "legacy-approval",
            "plan_content_hash": "a" * 64,
            "approved_by": "fixture-reviewer",
            "approved_at_utc": UTC_CUTOFF,
            "max_stage": "weekly",
            "exact_call_limit": 4,
            "purpose": "complete_history_shadow_model_calls",
            "schema_version": "complete-history-shadow-approval-1.1.0",
        }
        # The old contract normalized UTC timestamps before calculating its
        # immutable hash. Simulate an actual v1.1 persisted payload while
        # intentionally leaving the future provider/model fields absent.
        legacy_draft = CompleteHistoryHumanApproval(**legacy_payload)
        legacy_hash = complete_history_human_approval_content_hash(legacy_draft)
        legacy_payload["approved_at_utc"] = legacy_draft.approved_at_utc
        restored = CompleteHistoryHumanApproval(
            **legacy_payload,
            content_hash=legacy_hash,
        )

        self.assertEqual(restored.content_hash, legacy_hash)
        self.assertEqual(complete_history_human_approval_content_hash(restored), legacy_hash)
        self.assertEqual(restored.provider, "openai")
        self.assertEqual(restored.model, "gpt-5.6-terra")

    def test_explicit_caller_authorization_is_required_even_with_human_approval(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
        )
        provider = _FakeProvider()

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=approval,
                allow_model_calls=False,
            )

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_CALL_NOT_AUTHORIZED)
        self.assertEqual(provider.calls, [])

    def test_active_parent_with_completed_monthly_segments_reaches_batched_weekly_stage(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
        )
        provider = _FakeProvider()

        execution = CompleteHistoryShadowRunner().execute(
            bundle,
            plan,
            provider=provider,
            approval=approval,
            allow_model_calls=True,
        )

        self.assertEqual(
            provider.calls,
            [
                ("primary", "monthly_map"),
                ("alternative", "monthly_map"),
                ("primary", "weekly_batch"),
                ("alternative", "weekly_batch"),
            ],
        )
        self.assertEqual(execution.calls_primary, 2)
        self.assertEqual(execution.calls_alternative, 2)
        self.assertEqual(execution.attempted_provider_calls, 4)
        self.assertEqual(execution.accepted_provider_calls, 4)
        self.assertEqual(execution.rejected_provider_calls, 0)
        self.assertEqual(len(execution.monthly_maps), 2)
        self.assertTrue(all(item.outer_parent.completion_state == "active" for item in execution.monthly_maps))
        self.assertTrue(all(len(item.completed_segments) == 1 for item in execution.monthly_maps))
        self.assertTrue(all(item.active_final_segment.completion_state == "active" for item in execution.monthly_maps))
        self.assertTrue(all(item.proof_result.status is SubdivisionVerificationStatus.UNPROVEN for item in execution.segment_proof_results))
        self.assertNotEqual(
            execution.proof_result.primary_result.bundle_content_hash,
            execution.proof_result.alternative_result.bundle_content_hash,
        )

    def test_no_weekly_batch_when_no_completed_monthly_segments(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
        )
        provider = _FakeProvider(no_completed_segments=True)
        execution = CompleteHistoryShadowRunner().execute(
            bundle,
            plan,
            provider=provider,
            approval=approval,
            allow_model_calls=True,
        )

        self.assertEqual(
            provider.calls,
            [("primary", "monthly_map"), ("alternative", "monthly_map")],
        )
        self.assertEqual(execution.attempted_provider_calls, 2)
        self.assertEqual(execution.segment_proof_results, ())
        self.assertTrue(all(not item.completed_segments for item in execution.monthly_maps))

    def test_duplicate_primary_and_alternative_monthly_maps_are_rejected_before_weekly(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
        )
        provider = _FakeProvider(duplicate_maps=True)

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=approval,
                allow_model_calls=True,
            )

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MONTHLY_MAP_PAIR_NOT_DISTINCT)
        self.assertIn(CompleteHistoryStage1HardRuleCode.MONTHLY_MAP_PAIR_NOT_DISTINCT.value, raised.exception.reason_codes)
        self.assertEqual(
            provider.calls,
            [("primary", "monthly_map"), ("alternative", "monthly_map")],
        )

    def test_monthly_map_requires_one_terminal_active_segment(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
        )
        provider = _FakeProvider(invalid_terminal_segment=True)

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=approval,
                allow_model_calls=True,
            )

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
        self.assertEqual(provider.calls, [("primary", "monthly_map")])

    def test_batched_weekly_graphs_preserve_each_completed_monthly_boundary_lineage(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
        )
        provider = _FakeProvider()

        with tempfile.TemporaryDirectory() as output_directory:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=approval,
                allow_model_calls=True,
                output_directory=output_directory,
            )
            weekly_packet = next(
                item
                for item in provider.packets
                if item["packet_kind"] == "blind_complete_history_weekly_child_graph_batch"
                and item["role"] == "primary"
            )
            weekly_artifact = next(
                json.loads(path.read_text(encoding="utf-8"))
                for path in Path(output_directory).glob("complete_history_stage_*.json")
                if json.loads(path.read_text(encoding="utf-8"))["output_kind"] == "weekly_child_graph_batch"
                and json.loads(path.read_text(encoding="utf-8"))["role"] == "primary"
            )

        request_by_segment = {item["segment_id"]: item for item in weekly_packet["segment_requests"]}
        for item in weekly_artifact["structured_output"]["graphs"]:
            request = request_by_segment[item["segment_id"]]
            graph = item["graph"]
            self.assertEqual(graph["boundary_lineage"], list(request["boundary_lineage"]))
            self.assertEqual(graph["children"][0]["start_pivot_id"], request["boundary_lineage"][0]["child_pivot_id"])
            self.assertEqual(graph["children"][-1]["end_pivot_id"], request["boundary_lineage"][1]["child_pivot_id"])

    def test_active_monthly_map_stage_rejects_daily_without_provider_call(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        with self.assertRaises(CompleteHistoryShadowError) as raised:
            complete_history_stage_call_limit(plan, CompleteHistoryMaximumStage.DAILY)
        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.ACTIVE_MONTHLY_MAP_STAGE_UNSUPPORTED)

    def test_weekly_stage_scope_is_exactly_four_calls_and_stops_before_daily(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        self.assertEqual(complete_history_stage_call_limit(plan, CompleteHistoryMaximumStage.MONTHLY), 2)
        self.assertEqual(complete_history_stage_call_limit(plan, CompleteHistoryMaximumStage.WEEKLY), 4)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            max_stage=CompleteHistoryMaximumStage.WEEKLY,
            exact_call_limit=4,
        )
        provider = _FakeProvider()
        with tempfile.TemporaryDirectory() as output_directory:
            execution = CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=approval,
                allow_model_calls=True,
                output_directory=output_directory,
            )

            self.assertEqual(
                provider.calls,
                [
                    ("primary", "monthly_map"),
                    ("alternative", "monthly_map"),
                    ("primary", "weekly_batch"),
                    ("alternative", "weekly_batch"),
                ],
            )
            self.assertEqual(execution.calls_primary, 2)
            self.assertEqual(execution.calls_alternative, 2)
            self.assertEqual(execution.attempted_provider_calls, 4)
            self.assertEqual(execution.accepted_provider_calls, 4)
            self.assertEqual(execution.rejected_provider_calls, 0)
            self.assertEqual(execution.max_stage, CompleteHistoryMaximumStage.WEEKLY)
            self.assertEqual(execution.exact_call_limit, 4)
            self.assertEqual(len(execution.created_artifact_content_hashes), 4)
            self.assertEqual(execution.reused_artifact_content_hashes, ())
            artifacts = sorted(Path(output_directory).glob("complete_history_stage_*.json"))
            self.assertEqual(len(artifacts), 4)
            for artifact_path in artifacts:
                payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                self.assertEqual(artifact_path.stem, "complete_history_stage_" + payload["content_hash"])
                self.assertIn(
                    payload["output_kind"],
                    {"monthly_map", "weekly_child_graph_batch"},
                )
                self.assertNotIn("hidden_reasoning", payload)
                self.assertNotIn("api_key", payload)
                self.assertNotIn("candles", payload["structured_output"])
            first_artifact_path = artifacts[0]
            original_bytes = first_artifact_path.read_bytes()
            artifact_store = CompleteHistoryLocalArtifactStore(
                output_directory,
                bundle_directory=bundle.bundle_directory,
            )
            first_artifact = CompleteHistoryStageArtifact.from_dict(
                json.loads(original_bytes.decode("utf-8"))
            )
            self.assertEqual(artifact_store.write(first_artifact), first_artifact_path)
            self.assertEqual(first_artifact_path.read_bytes(), original_bytes)

            resumed_provider = _NoCallProvider()
            resumed = CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=resumed_provider,
                approval=approval,
                allow_model_calls=True,
                output_directory=output_directory,
            )
            self.assertEqual(resumed_provider.calls, [])
            self.assertEqual(resumed.calls_primary, 0)
            self.assertEqual(resumed.calls_alternative, 0)
            self.assertEqual(resumed.attempted_provider_calls, 0)
            self.assertEqual(resumed.accepted_provider_calls, 0)
            self.assertEqual(resumed.rejected_provider_calls, 0)
            self.assertEqual(resumed.created_artifact_content_hashes, ())
            self.assertEqual(len(resumed.reused_artifact_content_hashes), 4)
            self.assertEqual(len(list(Path(output_directory).glob("complete_history_stage_*.json"))), 4)

    def test_invalid_adapter_response_stops_once_and_keeps_completed_root_artifacts(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            max_stage=CompleteHistoryMaximumStage.WEEKLY,
            exact_call_limit=4,
            provider="openai",
            model="gpt-5.6-terra",
        )
        transport = _InvalidWeeklyAdapterTransport()
        provider = CompleteHistoryOpenAICandidateProvider(transport)

        with tempfile.TemporaryDirectory() as output_directory:
            with self.assertRaises(CompleteHistoryShadowError) as raised:
                CompleteHistoryShadowRunner().execute(
                    bundle,
                    plan,
                    provider=provider,
                    approval=approval,
                    allow_model_calls=True,
                    output_directory=output_directory,
                )

            self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
            self.assertEqual(raised.exception.attempted_provider_calls, 3)
            self.assertEqual(raised.exception.accepted_provider_calls, 2)
            self.assertEqual(raised.exception.rejected_provider_calls, 1)
            self.assertEqual(
                transport.calls,
                [
                    ("primary", "monthly_map"),
                    ("alternative", "monthly_map"),
                    ("primary", "weekly_batch"),
                ],
            )
            self.assertEqual(len(list(Path(output_directory).glob("complete_history_stage_*.json"))), 2)
            self.assertEqual(
                provider.call_accounting(),
                {
                    "attempted_provider_calls": 3,
                    "accepted_provider_calls": 2,
                    "rejected_provider_calls": 1,
                },
            )

    def test_weekly_approval_rejects_any_call_limit_other_than_four(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            max_stage="weekly",
            exact_call_limit=5,
        )
        provider = _FakeProvider()

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=approval,
                allow_model_calls=True,
            )

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.APPROVAL_CALL_LIMIT_MISMATCH)
        self.assertEqual(provider.calls, [])

    def test_tampered_local_artifact_blocks_resume_without_provider_call(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            max_stage="weekly",
            exact_call_limit=4,
        )
        with tempfile.TemporaryDirectory() as output_directory:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=_FakeProvider(),
                approval=approval,
                allow_model_calls=True,
                output_directory=output_directory,
            )
            artifact_path = next(Path(output_directory).glob("complete_history_stage_*.json"))
            artifact_path.write_text(artifact_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            provider = _NoCallProvider()

            with self.assertRaises(CompleteHistoryShadowError) as raised:
                CompleteHistoryShadowRunner().execute(
                    bundle,
                    plan,
                    provider=provider,
                    approval=approval,
                    allow_model_calls=True,
                    output_directory=output_directory,
                )

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.ARTIFACT_HASH_INVALID)
        self.assertEqual(provider.calls, [])

    def test_artifact_directory_cannot_modify_the_immutable_bundle(self) -> None:
        directory = self._bundle()
        bundle = load_complete_history_bundle(directory)
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
            max_stage="weekly",
            exact_call_limit=4,
        )
        provider = _FakeProvider()

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            CompleteHistoryShadowRunner().execute(
                bundle,
                plan,
                provider=provider,
                approval=approval,
                allow_model_calls=True,
                output_directory=directory,
            )

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.ARTIFACT_DIRECTORY_INVALID)
        self.assertEqual(provider.calls, [])

    def test_provider_cannot_claim_monthly_map_verification(self) -> None:
        bundle = load_complete_history_bundle(self._bundle())
        plan = build_complete_history_shadow_plan(bundle)
        approval = CompleteHistoryHumanApproval.create(
            approval_id="",
            plan_content_hash=plan.content_hash,
            approved_by="fixture-reviewer",
            approved_at_utc=UTC_CUTOFF,
        )
        provider = _FakeProvider(invalid_map=True)

        with self.assertRaises(CompleteHistoryShadowError) as raised:
            CompleteHistoryShadowRunner().execute(bundle, plan, provider=provider, approval=approval, allow_model_calls=True)

        self.assertEqual(raised.exception.code, CompleteHistoryShadowReasonCode.MODEL_OUTPUT_INVALID)
        self.assertEqual(provider.calls, [("primary", "monthly_map")])


if __name__ == "__main__":
    unittest.main()
