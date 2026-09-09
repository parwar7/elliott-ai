"""SQLite knowledge retrieval and approved-memory storage."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .correction_state import (
    CORRECTION_STATE_CALCULATION_VERSION,
    CORRECTION_STATE_SCHEMA_VERSION,
    OUTCOME_SCHEMA_VERSION,
    build_outcome_review,
    correction_case_content_hash,
    create_review_snapshot,
    finalize_outcome_revision,
    outcome_content_hash,
    review_content_hash,
    snapshot_content_hash,
    validate_snapshot,
)
from .fingerprints import (
    CALCULATION_VERSION as FINGERPRINT_CALCULATION_VERSION,
    FEATURE_SCHEMA_DEFINITION,
    FEATURE_SCHEMA_VERSION,
    fingerprint_content_hash,
)
from .experience import (
    DNA_KINDS,
    EXPERIENCE_CALCULATION_VERSION,
    EXPERIENCE_SCHEMA_VERSION,
    EXPERIENCE_STATES,
    EXPERIENCE_TAG_SCHEMA_VERSION,
    PATTERN_DNA_CALCULATION_VERSION,
    PATTERN_DNA_SCHEMA_VERSION,
    QUALITY_STATUSES,
    assemble_experience_candidate,
    build_experience_review,
    build_experience_tag,
    build_tag_assignment,
    experience_case_content_hash,
    experience_review_content_hash,
    market_episode_identity,
    pattern_dna_content_hash,
    tag_assignment_content_hash,
    tag_content_hash,
)
from .experience_comparison import (
    ComparisonConfig,
    compatibility_matrix_manifest,
    filter_structurally_comparable_cases,
)
from .experience_analogue import (
    COMPARISON_SPECIFICATION_VERSION,
    compare_filtered_experiences,
    comparison_specification,
)
from .experience_retrieval import (
    DEFAULT_RESULT_LIMIT,
    DEFAULT_RETRIEVAL_POLICY_ID,
    DEFAULT_RETRIEVAL_POLICY_VERSION,
    retrieval_policy,
    retrieve_analogues,
)
from .experience_outcomes import (
    DEFAULT_HORIZON_ID,
    DEFAULT_HORIZON_VERSION,
    attach_reviewed_outcome_evidence,
    inspect_outcome_source,
    outcome_specification_manifest,
)
from .experience_workflow import (
    EXPERIENCE_WORKFLOW_MIGRATION_SQL,
    FINAL_ACCEPTANCE_VERSION,
    OUTCOME_DECISION_STATE,
    OUTCOME_REVIEW_VERSION,
    STRUCTURAL_DECISION_STATE,
    STRUCTURAL_REVIEW_VERSION,
    WORKFLOW_CASE_CALCULATION_VERSION,
    WORKFLOW_CASE_SCHEMA_VERSION,
    WORKFLOW_EVENT_SCHEMA_VERSION,
    WORKFLOW_INDEXES,
    WORKFLOW_SPEC_VERSION,
    WORKFLOW_STATES,
    WORKFLOW_TABLES,
    assemble_workflow_draft,
    build_outcome_review_block,
    build_structural_review_block,
    build_workflow_event,
    validate_event_chain,
    validate_workflow_sources,
    workflow_case_content_hash,
    workflow_event_content_hash,
    workflow_specification,
)
from .forecast_records import (
    FORECAST_CALCULATION_VERSION,
    FORECAST_LEDGER_INDEXES,
    FORECAST_LEDGER_MIGRATION_SQL,
    FORECAST_LEDGER_TABLES,
    FORECAST_LEDGER_TRIGGERS,
    FORECAST_POLICY_VERSION,
    FORECAST_SCHEMA_DEFINITION,
    FORECAST_SCHEMA_VERSION,
    ForecastRecord,
    canonical_forecast_json,
    dataset_cutoff_from_stored_summary,
    forecast_schema_definition_hash,
    stored_record_content_hash,
    validate_forecast_record,
)
from .forecast_outcomes import (
    FORECAST_OUTCOME_INDEXES,
    FORECAST_OUTCOME_MIGRATION_SQL,
    FORECAST_OUTCOME_TABLES,
    FORECAST_OUTCOME_TRIGGERS,
    OBSERVATION_SET_SCHEMA_VERSION,
    OUTCOME_CALCULATION_VERSION,
    OUTCOME_EVALUATION_SCHEMA_VERSION,
    OUTCOME_POLICY_VERSION,
    ForecastObservationSet,
    ForecastOutcomeEvaluation,
    forecast_observation_set_content_hash,
    forecast_outcome_evaluation_content_hash,
    validate_forecast_observation_set,
    validate_forecast_outcome_evaluation,
)
from .mistake_memory import (
    LESSON_EVENT_SCHEMA_VERSION,
    LESSON_SCHEMA_VERSION,
    LESSON_SOURCE_SCHEMA_VERSION,
    MISTAKE_MEMORY_CALCULATION_VERSION,
    MISTAKE_MEMORY_INDEXES,
    MISTAKE_MEMORY_MIGRATION_SQL,
    MISTAKE_MEMORY_POLICY_VERSION,
    MISTAKE_MEMORY_TABLES,
    MISTAKE_MEMORY_TRIGGERS,
    OUTCOME_REVIEW_SCHEMA_VERSION,
    ForecastOutcomeReview,
    LessonEvent,
    LessonSource,
    MistakeMemoryLesson,
    build_lesson_event,
    forecast_outcome_review_content_hash,
    lesson_event_content_hash,
    lesson_source_content_hash,
    mistake_memory_lesson_content_hash,
    validate_forecast_outcome_review,
    validate_lesson_event_chain,
)
from .technical_agent_orchestrator import (
    FORECAST_AGENT_ORCHESTRATION_INDEXES,
    FORECAST_AGENT_ORCHESTRATION_MIGRATION_SQL,
    FORECAST_AGENT_ORCHESTRATION_TABLES,
    FORECAST_AGENT_ORCHESTRATION_TRIGGERS,
    TECHNICAL_AGENT_CALCULATION_VERSION,
    TECHNICAL_AGENT_ORCHESTRATION_SCHEMA_VERSION,
    TECHNICAL_AGENT_POLICY_VERSION,
    TechnicalAgentOrchestration,
    technical_agent_orchestration_content_hash,
    validate_technical_agent_orchestration,
)
from .outcome_learning_agents import (
    FORECAST_OUTCOME_LEARNING_ORCHESTRATION_INDEXES,
    FORECAST_OUTCOME_LEARNING_ORCHESTRATION_MIGRATION_SQL,
    FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TABLES,
    FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TRIGGERS,
    OUTCOME_LEARNING_CALCULATION_VERSION,
    OUTCOME_LEARNING_ORCHESTRATION_SCHEMA_VERSION,
    OUTCOME_LEARNING_POLICY_VERSION,
    MistakeMemoryProposalRequest,
    OutcomeLearningAgentRole,
    OutcomeLearningOrchestration,
    OutcomeReviewerRequest,
    outcome_learning_orchestration_content_hash,
    validate_outcome_learning_orchestration,
)


MAX_CHUNK_CHARS = 3_500
ANALYSIS_NAME_PATTERN = re.compile(
    r"(analysis|recount|report|audit|verification|backtest|brain)", re.IGNORECASE
)
OHLC_KEYS = {"date", "time", "timestamp", "open", "high", "low", "close", "volume"}
ANALYSIS_KEYS = {
    "symbol",
    "status",
    "preferred_count",
    "preferred_high_degree",
    "hierarchy",
    "waves",
    "verdict",
    "analysis",
    "levels",
    "alternates",
}
RAW_CONTEXT_TYPES = {
    "benchmark",
    "breadth",
    "yields",
    "options",
    "fundamentals",
    "news",
    "order_book",
}


@dataclass(frozen=True)
class SourceDocument:
    path: str
    kind: str
    status: str
    content: str
    parsed: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class KnowledgeHit:
    evidence_id: str
    source: str
    kind: str
    status: str
    section: str
    content: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "kind": self.kind,
            "status": self.status,
            "section": self.section,
            "content": self.content,
            "score": self.score,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _is_ohlcv_row(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    keys = {str(key).lower() for key in value}
    return {"high", "low", "close"}.issubset(keys) and bool(
        keys & {"date", "time", "timestamp"}
    )


def _looks_like_raw_market_data(value: Any) -> bool:
    if isinstance(value, list) and value:
        return _is_ohlcv_row(value[0])
    if isinstance(value, dict):
        context_type = str(
            value.get("context_type") or value.get("type") or ""
        ).lower()
        if context_type in RAW_CONTEXT_TYPES:
            return True
        for key in ("candles", "bars", "ohlcv", "data", "records"):
            rows = value.get(key)
            if isinstance(rows, list) and rows and _is_ohlcv_row(rows[0]):
                return True
    return False


def _normalize_status(path: Path, parsed: Any, content: str) -> str:
    if path.name in {"AI_BRAIN_MASTER_RULES.json", "AI_BRAIN_CURRENT.md"}:
        return "canonical"
    status = ""
    if isinstance(parsed, dict):
        status = str(parsed.get("status", ""))
    probe = f"{path.name}\n{status}\n{content[:2500]}".lower()
    if "superseded" in probe:
        return "superseded"
    if "canonical" in probe:
        return "canonical"
    if "draft" in probe:
        return "draft"
    return "active"


def _source_from_path(path: Path, *, force_kind: str | None = None) -> SourceDocument | None:
    if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".txt"}:
        return None
    content = _read_text(path)
    parsed: Any = None
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return None
        if _looks_like_raw_market_data(parsed):
            return None
    if force_kind is None:
        if path.name == "AI_BRAIN_MASTER_RULES.json":
            kind = "rulebook"
        elif path.name == "AI_BRAIN_CURRENT.md":
            kind = "brain_summary"
        elif isinstance(parsed, dict) and (
            {str(key).lower() for key in parsed} & ANALYSIS_KEYS
        ):
            kind = "case_report"
        elif ANALYSIS_NAME_PATTERN.search(path.name):
            kind = "case_report"
        else:
            return None
    else:
        kind = force_kind
    return SourceDocument(
        path=str(path.resolve()),
        kind=kind,
        status=_normalize_status(path, parsed, content),
        content=content,
        parsed=parsed,
    )


def discover_workspace_sources(workspace: Path) -> list[SourceDocument]:
    """Find rule and report files while excluding OHLCV and browser dumps."""
    workspace = workspace.resolve()
    sources: dict[str, SourceDocument] = {}
    for name in ("AI_BRAIN_MASTER_RULES.json", "AI_BRAIN_CURRENT.md"):
        document = _source_from_path(workspace / name)
        if document:
            sources[document.path] = document

    for path in sorted(workspace.iterdir()):
        if path.name.startswith(".") or ".pre_" in path.name.lower():
            continue
        if not ANALYSIS_NAME_PATTERN.search(path.name):
            continue
        document = _source_from_path(path)
        if document:
            sources[document.path] = document
    return list(sources.values())


def _split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> Iterator[str]:
    text = text.strip()
    if not text:
        return
    paragraphs = re.split(r"\n\s*\n", text)
    current = ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            yield current
            current = ""
        while len(paragraph) > max_chars:
            split_at = paragraph.rfind("\n", 0, max_chars)
            if split_at < max_chars // 2:
                split_at = paragraph.rfind(" ", 0, max_chars)
            if split_at < max_chars // 2:
                split_at = max_chars
            yield paragraph[:split_at].strip()
            paragraph = paragraph[split_at:].strip()
        current = paragraph
    if current:
        yield current


def _markdown_chunks(content: str) -> Iterator[tuple[str, str]]:
    heading = "Document"
    section_lines: list[str] = []

    def emit() -> Iterator[tuple[str, str]]:
        section = "\n".join(section_lines).strip()
        for piece in _split_text(section):
            yield heading, piece

    for line in content.splitlines():
        if re.match(r"^#{1,6}\s+", line):
            yield from emit()
            heading = re.sub(r"^#{1,6}\s+", "", line).strip() or "Document"
            section_lines = []
        else:
            section_lines.append(line)
    yield from emit()


def _json_chunks(value: Any, pointer: str = "$") -> Iterator[tuple[str, str]]:
    rendered = json.dumps(value, ensure_ascii=True, indent=2)
    if len(rendered) <= MAX_CHUNK_CHARS or not isinstance(value, (dict, list)):
        for piece in _split_text(rendered):
            yield pointer, piece
        return

    if isinstance(value, dict):
        for key, child in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from _json_chunks(child, f"{pointer}/{escaped}")
        return

    group: list[Any] = []
    group_start = 0
    for index, child in enumerate(value):
        candidate = group + [child]
        if group and len(json.dumps(candidate, ensure_ascii=True, indent=2)) > MAX_CHUNK_CHARS:
            yield f"{pointer}[{group_start}:{index}]", json.dumps(
                group, ensure_ascii=True, indent=2
            )
            group = [child]
            group_start = index
        elif len(json.dumps([child], ensure_ascii=True, indent=2)) > MAX_CHUNK_CHARS:
            if group:
                yield f"{pointer}[{group_start}:{index}]", json.dumps(
                    group, ensure_ascii=True, indent=2
                )
                group = []
            yield from _json_chunks(child, f"{pointer}[{index}]")
            group_start = index + 1
        else:
            group = candidate
    if group:
        yield f"{pointer}[{group_start}:{len(value)}]", json.dumps(
            group, ensure_ascii=True, indent=2
        )


def _document_chunks(document: SourceDocument) -> Iterable[tuple[str, str]]:
    if document.parsed is not None:
        return _json_chunks(document.parsed)
    return _markdown_chunks(document.content)


class KnowledgeStore:
    """Persistent local retrieval store with an explicit approval gate for memory."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).resolve()
        database_preexisted = (
            self.database_path.exists() and self.database_path.stat().st_size > 0
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.phase4_backup_path = self._backup_before_phase4_migration()
        self._phase5a1_pre_migration_audit: dict[str, Any] | None = None
        self.phase5a1_backup_path = self._backup_before_phase5a1_migration()
        self.phase5a1_migration_audit_path: Path | None = None
        self.initialize()
        self.phase5a1_migration_audit_path = self._finalize_phase5a1_migration_audit()
        self._experience_workflow_pre_migration_audit: dict[str, Any] | None = None
        self.experience_workflow_backup_path = (
            self._backup_before_experience_workflow_migration(
                database_preexisted=database_preexisted
            )
        )
        self.experience_workflow_migration_audit_path: Path | None = None
        self.experience_workflow_migration_applied = (
            self._initialize_experience_workflow_schema()
        )
        self.experience_workflow_migration_audit_path = (
            self._finalize_experience_workflow_migration_audit()
        )
        self._forecast_ledger_pre_migration_audit: dict[str, Any] | None = None
        self.forecast_ledger_backup_path = self._backup_before_forecast_ledger_migration(
            database_preexisted=database_preexisted
        )
        self.forecast_ledger_migration_audit_path: Path | None = None
        self.forecast_ledger_migration_applied = self._initialize_forecast_ledger_schema()
        self.forecast_ledger_migration_audit_path = (
            self._finalize_forecast_ledger_migration_audit()
        )
        self._forecast_outcome_pre_migration_audit: dict[str, Any] | None = None
        self.forecast_outcome_backup_path = (
            self._backup_before_forecast_outcome_migration(
                database_preexisted=database_preexisted
            )
        )
        self.forecast_outcome_migration_audit_path: Path | None = None
        self.forecast_outcome_migration_applied = (
            self._initialize_forecast_outcome_schema()
        )
        self.forecast_outcome_migration_audit_path = (
            self._finalize_forecast_outcome_migration_audit()
        )
        self._mistake_memory_pre_migration_audit: dict[str, Any] | None = None
        self.mistake_memory_backup_path = self._backup_before_mistake_memory_migration(
            database_preexisted=database_preexisted
        )
        self.mistake_memory_migration_audit_path: Path | None = None
        self.mistake_memory_migration_applied = (
            self._initialize_mistake_memory_schema()
        )
        self.mistake_memory_migration_audit_path = (
            self._finalize_mistake_memory_migration_audit()
        )
        self._forecast_agent_pre_migration_audit: dict[str, Any] | None = None
        self.forecast_agent_backup_path = (
            self._backup_before_forecast_agent_migration(
                database_preexisted=database_preexisted
            )
        )
        self.forecast_agent_migration_audit_path: Path | None = None
        self.forecast_agent_migration_applied = (
            self._initialize_forecast_agent_schema()
        )
        self.forecast_agent_migration_audit_path = (
            self._finalize_forecast_agent_migration_audit()
        )
        self._outcome_learning_pre_migration_audit: dict[str, Any] | None = None
        self.outcome_learning_pre_migration_audit_path: Path | None = None
        self.outcome_learning_backup_path = (
            self._backup_before_outcome_learning_migration(
                database_preexisted=database_preexisted
            )
        )
        self.outcome_learning_migration_audit_path: Path | None = None
        self.outcome_learning_migration_applied = (
            self._initialize_outcome_learning_schema()
        )
        self.outcome_learning_migration_audit_path = (
            self._finalize_outcome_learning_migration_audit()
        )

    def _backup_before_phase4_migration(self) -> Path | None:
        """Create one consistent backup before adding the Phase 4 tables."""
        if not self.database_path.exists() or self.database_path.stat().st_size == 0:
            return None
        source = sqlite3.connect(self.database_path)
        try:
            tables = {
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not tables or "correction_cases" in tables:
                return None
            backup_dir = self.database_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            backup_path = backup_dir / (
                f"{self.database_path.stem}.pre_phase4_{stamp}{self.database_path.suffix}"
            )
            if not backup_path.exists():
                destination = sqlite3.connect(backup_path)
                try:
                    source.backup(destination)
                    destination.commit()
                finally:
                    destination.close()
            return backup_path.resolve()
        finally:
            source.close()

    @staticmethod
    def _database_integrity_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        counts = {
            name: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{name.replace(chr(34), chr(34) * 2)}"'
                ).fetchone()[0]
            )
            for name in table_names
        }
        foreign_key_rows = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        return {
            "integrity_check": str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
            "foreign_key_violation_count": len(foreign_key_rows),
            "foreign_key_violations": foreign_key_rows,
            "table_counts": counts,
        }

    def _backup_before_phase5a1_migration(self) -> Path | None:
        """Back up and audit an existing store before adding Phase 5A.1 tables."""
        if not self.database_path.exists() or self.database_path.stat().st_size == 0:
            return None
        source = sqlite3.connect(self.database_path)
        try:
            tables = {
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not tables or "experience_cases" in tables:
                return None
            pre_audit = self._database_integrity_snapshot(source)
            if pre_audit["integrity_check"] != "ok":
                raise RuntimeError("Refusing Phase 5A.1 migration: SQLite integrity_check failed.")
            if pre_audit["foreign_key_violation_count"]:
                raise RuntimeError(
                    "Refusing Phase 5A.1 migration: existing foreign-key violations were found."
                )
            backup_dir = self.database_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / (
                f"{self.database_path.stem}.pre_phase5a1_{stamp}{self.database_path.suffix}"
            )
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
            self._phase5a1_pre_migration_audit = {
                "phase": "5A.1",
                "migration_started_at": _utc_now(),
                "database_path": str(self.database_path),
                "backup_path": str(backup_path.resolve()),
                "pre_migration": pre_audit,
            }
            return backup_path.resolve()
        finally:
            source.close()

    def _finalize_phase5a1_migration_audit(self) -> Path | None:
        if self._phase5a1_pre_migration_audit is None:
            return None
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            post_audit = self._database_integrity_snapshot(connection)
        finally:
            connection.close()
        if post_audit["integrity_check"] != "ok" or post_audit[
            "foreign_key_violation_count"
        ]:
            raise RuntimeError("Phase 5A.1 migration failed post-migration integrity checks.")
        audit = {
            **self._phase5a1_pre_migration_audit,
            "migration_completed_at": _utc_now(),
            "post_migration": post_audit,
            "restore_command": (
                "Close Elliott AI, preserve the failed database, then replace it with the "
                "recorded SQLite backup using a filesystem copy operation."
            ),
        }
        assert self.phase5a1_backup_path is not None
        audit_path = self.phase5a1_backup_path.with_suffix(
            self.phase5a1_backup_path.suffix + ".migration-audit.json"
        )
        audit_path.write_text(
            json.dumps(audit, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
        )
        return audit_path.resolve()

    @staticmethod
    def _experience_workflow_schema_objects(
        connection: sqlite3.Connection,
    ) -> tuple[set[str], set[str]]:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if str(row[0]) in WORKFLOW_TABLES
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
            if str(row[0]) in WORKFLOW_INDEXES
        }
        return tables, indexes

    @staticmethod
    def _validate_experience_workflow_schema(
        connection: sqlite3.Connection,
    ) -> None:
        tables, indexes = KnowledgeStore._experience_workflow_schema_objects(connection)
        if tables != set(WORKFLOW_TABLES):
            raise RuntimeError(
                "Historical experience workflow schema is partial or missing."
            )
        if indexes != set(WORKFLOW_INDEXES):
            raise RuntimeError(
                "Historical experience workflow indexes are partial or missing."
            )
        expected_columns = {
            "experience_workflow_cases": (
                "workflow_case_id",
                "market_episode_id",
                "workflow_version",
                "supersedes_workflow_case_id",
                "source_correction_case_id",
                "source_endpoint_snapshot_id",
                "source_fingerprint_id",
                "source_pair_hash",
                "material_episode_hash",
                "schema_version",
                "calculation_version",
                "content_hash",
                "draft_json",
                "created_at",
            ),
            "experience_workflow_events": (
                "event_id",
                "workflow_case_id",
                "parent_event_id",
                "sequence_number",
                "event_kind",
                "from_state",
                "to_state",
                "decision",
                "actor_reference",
                "reviewer_reference",
                "human_confirmed",
                "source_outcome_id",
                "source_outcome_review_id",
                "result_experience_case_id",
                "result_experience_review_id",
                "workflow_spec_version",
                "event_schema_version",
                "content_hash",
                "event_json",
                "created_at",
            ),
        }
        for table, expected in expected_columns.items():
            actual = tuple(
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != expected:
                raise RuntimeError(
                    f"Historical experience workflow table {table} has an unsupported layout."
                )
            foreign_keys = connection.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall()
            if any(str(row[6]).upper() != "RESTRICT" for row in foreign_keys):
                raise RuntimeError(
                    f"Historical experience workflow table {table} has a non-RESTRICT deletion relationship."
                )

    def _backup_before_experience_workflow_migration(
        self, *, database_preexisted: bool
    ) -> Path | None:
        """Back up an existing store before the approved two-table migration."""
        if not database_preexisted:
            return None
        source = sqlite3.connect(self.database_path)
        try:
            source.execute("PRAGMA foreign_keys = ON")
            present, _ = self._experience_workflow_schema_objects(source)
            if present == set(WORKFLOW_TABLES):
                self._validate_experience_workflow_schema(source)
                return None
            if present:
                raise RuntimeError(
                    "Refusing historical experience workflow migration: only one approved workflow table exists."
                )
            pre_audit = self._database_integrity_snapshot(source)
            if pre_audit["integrity_check"] != "ok":
                raise RuntimeError(
                    "Refusing historical experience workflow migration: SQLite integrity_check failed."
                )
            if pre_audit["foreign_key_violation_count"]:
                raise RuntimeError(
                    "Refusing historical experience workflow migration: existing foreign-key violations were found."
                )
            backup_dir = self.database_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / (
                f"{self.database_path.stem}.pre_experience_workflow_{stamp}"
                f"{self.database_path.suffix}"
            )
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
            self._experience_workflow_pre_migration_audit = {
                "phase": "Historical Experience Population and Review Workflow",
                "migration_scope": [
                    "experience_workflow_cases",
                    "experience_workflow_events",
                ],
                "migration_started_at": _utc_now(),
                "database_path": str(self.database_path),
                "backup_path": str(backup_path.resolve()),
                "pre_migration": pre_audit,
            }
            return backup_path.resolve()
        finally:
            source.close()

    def _initialize_experience_workflow_schema(self) -> bool:
        """Apply the exact approved DDL in one explicit immediate transaction."""
        connection = sqlite3.connect(self.database_path, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            present, _ = self._experience_workflow_schema_objects(connection)
            if present == set(WORKFLOW_TABLES):
                self._validate_experience_workflow_schema(connection)
                return False
            if present:
                raise RuntimeError(
                    "Refusing historical experience workflow migration: partial schema detected."
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in EXPERIENCE_WORKFLOW_MIGRATION_SQL:
                    connection.execute(statement)
                self._validate_experience_workflow_schema(connection)
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(
                        "Historical experience workflow migration failed integrity_check."
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError(
                        "Historical experience workflow migration created foreign-key violations."
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return True
        finally:
            connection.close()

    def _finalize_experience_workflow_migration_audit(self) -> Path | None:
        if self._experience_workflow_pre_migration_audit is None:
            return None
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            self._validate_experience_workflow_schema(connection)
            post_audit = self._database_integrity_snapshot(connection)
        finally:
            connection.close()
        if post_audit["integrity_check"] != "ok" or post_audit[
            "foreign_key_violation_count"
        ]:
            raise RuntimeError(
                "Historical experience workflow migration failed post-migration checks."
            )
        audit = {
            **self._experience_workflow_pre_migration_audit,
            "migration_completed_at": _utc_now(),
            "workflow_spec_version": WORKFLOW_SPEC_VERSION,
            "case_schema_version": WORKFLOW_CASE_SCHEMA_VERSION,
            "event_schema_version": WORKFLOW_EVENT_SCHEMA_VERSION,
            "post_migration": post_audit,
            "restore_policy": (
                "Close Elliott AI, preserve the migrated database and audit files, "
                "then restore the recorded SQLite backup only after explicit destructive approval."
            ),
        }
        assert self.experience_workflow_backup_path is not None
        audit_path = self.experience_workflow_backup_path.with_suffix(
            self.experience_workflow_backup_path.suffix + ".migration-audit.json"
        )
        audit_path.write_text(
            json.dumps(audit, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return audit_path.resolve()

    @staticmethod
    def _forecast_ledger_schema_objects(
        connection: sqlite3.Connection,
    ) -> tuple[set[str], set[str], set[str]]:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if str(row[0]) in FORECAST_LEDGER_TABLES
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
            if str(row[0]) in FORECAST_LEDGER_INDEXES
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
            if str(row[0]) in FORECAST_LEDGER_TRIGGERS
        }
        return tables, indexes, triggers

    @staticmethod
    def _register_forecast_schema_version(connection: sqlite3.Connection) -> None:
        schema_json = canonical_forecast_json(FORECAST_SCHEMA_DEFINITION)
        content_hash = forecast_schema_definition_hash()
        connection.execute(
            """
            INSERT OR IGNORE INTO forecast_schema_versions(
                schema_version, calculation_version, policy_version,
                schema_json, content_hash, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                FORECAST_SCHEMA_VERSION,
                FORECAST_CALCULATION_VERSION,
                FORECAST_POLICY_VERSION,
                schema_json,
                content_hash,
                _utc_now(),
            ),
        )
        row = connection.execute(
            "SELECT * FROM forecast_schema_versions WHERE schema_version = ?",
            (FORECAST_SCHEMA_VERSION,),
        ).fetchone()
        if row is None:
            raise RuntimeError("Forecast schema version registration failed.")
        values = dict(row)
        expected = {
            "calculation_version": FORECAST_CALCULATION_VERSION,
            "policy_version": FORECAST_POLICY_VERSION,
            "schema_json": schema_json,
            "content_hash": content_hash,
        }
        for key, expected_value in expected.items():
            if values.get(key) != expected_value:
                raise RuntimeError(
                    "Stored forecast schema registration conflicts with the current "
                    f"definition at {key}."
                )

    @staticmethod
    def _validate_forecast_ledger_schema(
        connection: sqlite3.Connection, *, require_registered_version: bool = True
    ) -> None:
        tables, indexes, triggers = KnowledgeStore._forecast_ledger_schema_objects(
            connection
        )
        if tables != set(FORECAST_LEDGER_TABLES):
            raise RuntimeError("Forecast ledger schema is partial or missing.")
        if indexes != set(FORECAST_LEDGER_INDEXES):
            raise RuntimeError("Forecast ledger indexes are partial or missing.")
        if triggers != set(FORECAST_LEDGER_TRIGGERS):
            raise RuntimeError("Forecast ledger append-only triggers are partial or missing.")
        expected_columns = {
            "forecast_schema_versions": (
                "schema_version",
                "calculation_version",
                "policy_version",
                "schema_json",
                "content_hash",
                "created_at_utc",
            ),
            "forecast_records": (
                "forecast_id",
                "forecast_version",
                "supersedes_forecast_id",
                "source_analysis_run_id",
                "source_degree_resolution_id",
                "schema_version",
                "calculation_version",
                "policy_version",
                "evaluation_eligibility",
                "record_state",
                "symbol",
                "analysis_cutoff_utc",
                "content_hash",
                "record_json",
                "created_at_utc",
            ),
        }
        for table, expected in expected_columns.items():
            actual = tuple(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != expected:
                raise RuntimeError(
                    f"Forecast ledger table {table} has an unsupported layout."
                )
            foreign_keys = connection.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall()
            if any(str(row[6]).upper() != "RESTRICT" for row in foreign_keys):
                raise RuntimeError(
                    f"Forecast ledger table {table} has a non-RESTRICT deletion relationship."
                )
        references = {
            (
                str(row[3]),
                str(row[2]),
                str(row[4]),
            )
            for row in connection.execute("PRAGMA foreign_key_list(forecast_records)")
        }
        required_references = {
            ("source_analysis_run_id", "analysis_runs", "id"),
            ("source_degree_resolution_id", "degree_resolutions", "id"),
            ("supersedes_forecast_id", "forecast_records", "forecast_id"),
            ("schema_version", "forecast_schema_versions", "schema_version"),
        }
        if references != required_references:
            raise RuntimeError("Forecast ledger foreign-key references are unsupported.")
        if require_registered_version:
            row = connection.execute(
                "SELECT * FROM forecast_schema_versions WHERE schema_version = ?",
                (FORECAST_SCHEMA_VERSION,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Forecast schema version is not registered.")
            if str(row["content_hash"]) != forecast_schema_definition_hash():
                raise RuntimeError("Forecast schema definition hash does not match.")
            if str(row["schema_json"]) != canonical_forecast_json(
                FORECAST_SCHEMA_DEFINITION
            ):
                raise RuntimeError("Forecast schema definition JSON does not match.")

    def _backup_before_forecast_ledger_migration(
        self, *, database_preexisted: bool
    ) -> Path | None:
        """Back up an existing store before adding the two Phase 11A tables."""

        if not database_preexisted:
            return None
        source = sqlite3.connect(self.database_path)
        try:
            source.execute("PRAGMA foreign_keys = ON")
            tables, _, _ = self._forecast_ledger_schema_objects(source)
            if tables == set(FORECAST_LEDGER_TABLES):
                self._validate_forecast_ledger_schema(
                    source, require_registered_version=False
                )
                return None
            if tables:
                raise RuntimeError(
                    "Refusing Phase 11A migration: partial forecast ledger detected."
                )
            pre_audit = self._database_integrity_snapshot(source)
            if pre_audit["integrity_check"] != "ok":
                raise RuntimeError(
                    "Refusing Phase 11A migration: SQLite integrity_check failed."
                )
            if pre_audit["foreign_key_violation_count"]:
                raise RuntimeError(
                    "Refusing Phase 11A migration: existing foreign-key violations were found."
                )
            backup_dir = self.database_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / (
                f"{self.database_path.stem}.pre_phase11a_{stamp}{self.database_path.suffix}"
            )
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
            self._forecast_ledger_pre_migration_audit = {
                "phase": "11A - Specification and Immutable Forecast Ledger",
                "migration_scope": sorted(FORECAST_LEDGER_TABLES),
                "migration_started_at": _utc_now(),
                "database_path": str(self.database_path),
                "backup_path": str(backup_path.resolve()),
                "pre_migration": pre_audit,
            }
            return backup_path.resolve()
        finally:
            source.close()

    def _initialize_forecast_ledger_schema(self) -> bool:
        """Apply the Phase 11A DDL atomically and register its schema manifest."""

        connection = sqlite3.connect(self.database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            tables, _, _ = self._forecast_ledger_schema_objects(connection)
            if tables == set(FORECAST_LEDGER_TABLES):
                self._validate_forecast_ledger_schema(
                    connection, require_registered_version=False
                )
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._register_forecast_schema_version(connection)
                    self._validate_forecast_ledger_schema(connection)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                return False
            if tables:
                raise RuntimeError(
                    "Refusing Phase 11A migration: partial forecast ledger detected."
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in FORECAST_LEDGER_MIGRATION_SQL:
                    connection.execute(statement)
                self._register_forecast_schema_version(connection)
                self._validate_forecast_ledger_schema(connection)
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(
                        "Phase 11A forecast ledger migration failed integrity_check."
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError(
                        "Phase 11A forecast ledger migration created foreign-key violations."
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return True
        finally:
            connection.close()

    def _finalize_forecast_ledger_migration_audit(self) -> Path | None:
        if self._forecast_ledger_pre_migration_audit is None:
            return None
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            self._validate_forecast_ledger_schema(connection)
            post_audit = self._database_integrity_snapshot(connection)
        finally:
            connection.close()
        if post_audit["integrity_check"] != "ok" or post_audit[
            "foreign_key_violation_count"
        ]:
            raise RuntimeError("Phase 11A migration failed post-migration checks.")
        audit = {
            **self._forecast_ledger_pre_migration_audit,
            "migration_completed_at": _utc_now(),
            "forecast_schema_version": FORECAST_SCHEMA_VERSION,
            "calculation_version": FORECAST_CALCULATION_VERSION,
            "policy_version": FORECAST_POLICY_VERSION,
            "post_migration": post_audit,
            "restore_policy": (
                "Close Elliott AI, preserve the migrated database and audit files, "
                "then restore the recorded SQLite backup only after explicit "
                "destructive approval."
            ),
        }
        assert self.forecast_ledger_backup_path is not None
        audit_path = self.forecast_ledger_backup_path.with_suffix(
            self.forecast_ledger_backup_path.suffix + ".migration-audit.json"
        )
        audit_path.write_text(
            json.dumps(audit, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return audit_path.resolve()

    @staticmethod
    def _forecast_outcome_schema_objects(
        connection: sqlite3.Connection,
    ) -> tuple[set[str], set[str], set[str]]:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if str(row[0]) in FORECAST_OUTCOME_TABLES
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
            if str(row[0]) in FORECAST_OUTCOME_INDEXES
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
            if str(row[0]) in FORECAST_OUTCOME_TRIGGERS
        }
        return tables, indexes, triggers

    @staticmethod
    def _validate_forecast_outcome_schema(connection: sqlite3.Connection) -> None:
        tables, indexes, triggers = KnowledgeStore._forecast_outcome_schema_objects(
            connection
        )
        if tables != set(FORECAST_OUTCOME_TABLES):
            raise RuntimeError("Forecast outcome schema is partial or missing.")
        if indexes != set(FORECAST_OUTCOME_INDEXES):
            raise RuntimeError("Forecast outcome indexes are partial or missing.")
        if triggers != set(FORECAST_OUTCOME_TRIGGERS):
            raise RuntimeError(
                "Forecast outcome append-only triggers are partial or missing."
            )
        expected_columns = {
            "forecast_observation_sets": (
                "observation_set_id",
                "observation_set_version",
                "supersedes_observation_set_id",
                "forecast_id",
                "source_dataset_id",
                "schema_version",
                "calculation_version",
                "policy_version",
                "symbol",
                "timeframe",
                "actual_evaluation_cutoff_utc",
                "horizon_end_utc",
                "candle_manifest_hash",
                "policy_hash",
                "content_hash",
                "record_json",
                "created_at_utc",
            ),
            "forecast_outcome_evaluations": (
                "evaluation_id",
                "evaluation_version",
                "supersedes_evaluation_id",
                "forecast_id",
                "observation_set_id",
                "schema_version",
                "calculation_version",
                "policy_version",
                "outcome_status",
                "evaluated_through_utc",
                "evaluation_policy_hash",
                "content_hash",
                "record_json",
                "created_at_utc",
            ),
        }
        for table, expected in expected_columns.items():
            actual = tuple(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != expected:
                raise RuntimeError(
                    f"Forecast outcome table {table} has an unsupported layout."
                )
            foreign_keys = connection.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall()
            if any(str(row[6]).upper() != "RESTRICT" for row in foreign_keys):
                raise RuntimeError(
                    f"Forecast outcome table {table} has a non-RESTRICT deletion relationship."
                )
        observation_references = {
            (str(row[3]), str(row[2]), str(row[4]))
            for row in connection.execute(
                "PRAGMA foreign_key_list(forecast_observation_sets)"
            )
        }
        if observation_references != {
            ("forecast_id", "forecast_records", "forecast_id"),
            (
                "supersedes_observation_set_id",
                "forecast_observation_sets",
                "observation_set_id",
            ),
        }:
            raise RuntimeError("Forecast observation foreign-key references are unsupported.")
        evaluation_references = {
            (str(row[3]), str(row[2]), str(row[4]))
            for row in connection.execute(
                "PRAGMA foreign_key_list(forecast_outcome_evaluations)"
            )
        }
        if evaluation_references != {
            ("forecast_id", "forecast_records", "forecast_id"),
            (
                "observation_set_id",
                "forecast_observation_sets",
                "observation_set_id",
            ),
            (
                "supersedes_evaluation_id",
                "forecast_outcome_evaluations",
                "evaluation_id",
            ),
        }:
            raise RuntimeError("Forecast evaluation foreign-key references are unsupported.")

    def _backup_before_forecast_outcome_migration(
        self, *, database_preexisted: bool
    ) -> Path | None:
        """Back up an existing store before adding the two Phase 11B tables."""

        if not database_preexisted:
            return None
        source = sqlite3.connect(self.database_path)
        source.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA foreign_keys = ON")
            tables, _, _ = self._forecast_outcome_schema_objects(source)
            if tables == set(FORECAST_OUTCOME_TABLES):
                self._validate_forecast_outcome_schema(source)
                return None
            if tables:
                raise RuntimeError(
                    "Refusing Phase 11B migration: partial forecast outcome schema detected."
                )
            pre_audit = self._database_integrity_snapshot(source)
            if pre_audit["integrity_check"] != "ok":
                raise RuntimeError(
                    "Refusing Phase 11B migration: SQLite integrity_check failed."
                )
            if pre_audit["foreign_key_violation_count"]:
                raise RuntimeError(
                    "Refusing Phase 11B migration: existing foreign-key violations were found."
                )
            backup_dir = self.database_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / (
                f"{self.database_path.stem}.pre_phase11b_{stamp}{self.database_path.suffix}"
            )
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
            self._forecast_outcome_pre_migration_audit = {
                "phase": "11B - Immutable Post-Cutoff Observation Sets and Deterministic Outcome Evaluation",
                "migration_scope": sorted(FORECAST_OUTCOME_TABLES),
                "migration_started_at": _utc_now(),
                "database_path": str(self.database_path),
                "backup_path": str(backup_path.resolve()),
                "pre_migration": pre_audit,
            }
            return backup_path.resolve()
        finally:
            source.close()

    def _initialize_forecast_outcome_schema(self) -> bool:
        """Apply the two-table Phase 11B migration atomically."""

        connection = sqlite3.connect(self.database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            tables, _, _ = self._forecast_outcome_schema_objects(connection)
            if tables == set(FORECAST_OUTCOME_TABLES):
                self._validate_forecast_outcome_schema(connection)
                return False
            if tables:
                raise RuntimeError(
                    "Refusing Phase 11B migration: partial forecast outcome schema detected."
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in FORECAST_OUTCOME_MIGRATION_SQL:
                    connection.execute(statement)
                self._validate_forecast_outcome_schema(connection)
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(
                        "Phase 11B forecast outcome migration failed integrity_check."
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError(
                        "Phase 11B forecast outcome migration created foreign-key violations."
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return True
        finally:
            connection.close()

    def _finalize_forecast_outcome_migration_audit(self) -> Path | None:
        if self._forecast_outcome_pre_migration_audit is None:
            return None
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            self._validate_forecast_outcome_schema(connection)
            post_audit = self._database_integrity_snapshot(connection)
        finally:
            connection.close()
        if post_audit["integrity_check"] != "ok" or post_audit[
            "foreign_key_violation_count"
        ]:
            raise RuntimeError("Phase 11B migration failed post-migration checks.")
        audit = {
            **self._forecast_outcome_pre_migration_audit,
            "migration_completed_at": _utc_now(),
            "observation_set_schema_version": OBSERVATION_SET_SCHEMA_VERSION,
            "outcome_evaluation_schema_version": OUTCOME_EVALUATION_SCHEMA_VERSION,
            "calculation_version": OUTCOME_CALCULATION_VERSION,
            "policy_version": OUTCOME_POLICY_VERSION,
            "post_migration": post_audit,
            "restore_policy": (
                "Close Elliott AI, preserve the migrated database and audit files, "
                "then restore the recorded SQLite backup only after explicit destructive approval."
            ),
        }
        assert self.forecast_outcome_backup_path is not None
        audit_path = self.forecast_outcome_backup_path.with_suffix(
            self.forecast_outcome_backup_path.suffix + ".migration-audit.json"
        )
        audit_path.write_text(
            json.dumps(audit, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return audit_path.resolve()

    @staticmethod
    def _mistake_memory_schema_objects(
        connection: sqlite3.Connection,
    ) -> tuple[set[str], set[str], set[str]]:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if str(row[0]) in MISTAKE_MEMORY_TABLES
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
            if str(row[0]) in MISTAKE_MEMORY_INDEXES
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
            if str(row[0]) in MISTAKE_MEMORY_TRIGGERS
        }
        return tables, indexes, triggers

    @staticmethod
    def _validate_mistake_memory_schema(connection: sqlite3.Connection) -> None:
        tables, indexes, triggers = KnowledgeStore._mistake_memory_schema_objects(
            connection
        )
        if tables != set(MISTAKE_MEMORY_TABLES):
            raise RuntimeError("Mistake-memory schema is partial or missing.")
        if indexes != set(MISTAKE_MEMORY_INDEXES):
            raise RuntimeError("Mistake-memory indexes are partial or missing.")
        if triggers != set(MISTAKE_MEMORY_TRIGGERS):
            raise RuntimeError("Mistake-memory append-only triggers are partial or missing.")
        expected_columns = {
            "forecast_outcome_reviews": (
                "review_id",
                "review_version",
                "parent_review_id",
                "evaluation_id",
                "required_superseding_evaluation_id",
                "forecast_id",
                "decision",
                "reviewed_outcome_status",
                "reviewer_reference",
                "reviewed_at_utc",
                "schema_version",
                "policy_version",
                "content_hash",
                "record_json",
            ),
            "mistake_memory_lessons": (
                "lesson_id",
                "lesson_version",
                "supersedes_lesson_id",
                "scope_type",
                "initial_status",
                "proposed_by",
                "proposed_at_utc",
                "deduplication_key",
                "schema_version",
                "policy_version",
                "content_hash",
                "record_json",
            ),
            "mistake_memory_sources": (
                "source_id",
                "lesson_id",
                "review_id",
                "evaluation_id",
                "forecast_id",
                "source_role",
                "symbol",
                "added_by",
                "added_at_utc",
                "schema_version",
                "policy_version",
                "content_hash",
                "record_json",
            ),
            "mistake_memory_events": (
                "event_id",
                "lesson_id",
                "sequence_number",
                "parent_event_id",
                "related_lesson_id",
                "from_status",
                "to_status",
                "actor_reference",
                "recorded_at_utc",
                "reason",
                "schema_version",
                "policy_version",
                "content_hash",
                "record_json",
            ),
        }
        for table, expected in expected_columns.items():
            actual = tuple(
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != expected:
                raise RuntimeError(
                    f"Mistake-memory table {table} has an unsupported layout."
                )
            foreign_keys = connection.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall()
            if any(str(row[6]).upper() != "RESTRICT" for row in foreign_keys):
                raise RuntimeError(
                    f"Mistake-memory table {table} has a non-RESTRICT deletion relationship."
                )
        expected_references = {
            "forecast_outcome_reviews": {
                ("parent_review_id", "forecast_outcome_reviews", "review_id"),
                ("evaluation_id", "forecast_outcome_evaluations", "evaluation_id"),
                (
                    "required_superseding_evaluation_id",
                    "forecast_outcome_evaluations",
                    "evaluation_id",
                ),
                ("forecast_id", "forecast_records", "forecast_id"),
            },
            "mistake_memory_lessons": {
                ("supersedes_lesson_id", "mistake_memory_lessons", "lesson_id"),
            },
            "mistake_memory_sources": {
                ("lesson_id", "mistake_memory_lessons", "lesson_id"),
                ("review_id", "forecast_outcome_reviews", "review_id"),
                ("evaluation_id", "forecast_outcome_evaluations", "evaluation_id"),
                ("forecast_id", "forecast_records", "forecast_id"),
            },
            "mistake_memory_events": {
                ("lesson_id", "mistake_memory_lessons", "lesson_id"),
                ("parent_event_id", "mistake_memory_events", "event_id"),
                ("related_lesson_id", "mistake_memory_lessons", "lesson_id"),
            },
        }
        for table, expected in expected_references.items():
            actual = {
                (str(row[3]), str(row[2]), str(row[4]))
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            if actual != expected:
                raise RuntimeError(
                    f"Mistake-memory table {table} has unsupported foreign-key references."
                )

    def _backup_before_mistake_memory_migration(
        self, *, database_preexisted: bool
    ) -> Path | None:
        """Back up an existing store before the four-table Phase 11C migration."""

        if not database_preexisted:
            return None
        source = sqlite3.connect(self.database_path)
        source.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA foreign_keys = ON")
            tables, _, _ = self._mistake_memory_schema_objects(source)
            if tables == set(MISTAKE_MEMORY_TABLES):
                self._validate_mistake_memory_schema(source)
                return None
            if tables:
                raise RuntimeError(
                    "Refusing Phase 11C migration: partial mistake-memory schema detected."
                )
            pre_audit = self._database_integrity_snapshot(source)
            if pre_audit["integrity_check"] != "ok":
                raise RuntimeError(
                    "Refusing Phase 11C migration: SQLite integrity_check failed."
                )
            if pre_audit["foreign_key_violation_count"]:
                raise RuntimeError(
                    "Refusing Phase 11C migration: existing foreign-key violations were found."
                )
            backup_dir = self.database_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / (
                f"{self.database_path.stem}.pre_phase11c_{stamp}{self.database_path.suffix}"
            )
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
            self._mistake_memory_pre_migration_audit = {
                "phase": "11C - Human-Reviewed Outcome Decisions and Mistake Memory",
                "migration_scope": sorted(MISTAKE_MEMORY_TABLES),
                "migration_started_at": _utc_now(),
                "database_path": str(self.database_path),
                "backup_path": str(backup_path.resolve()),
                "pre_migration": pre_audit,
            }
            return backup_path.resolve()
        finally:
            source.close()

    def _initialize_mistake_memory_schema(self) -> bool:
        """Apply the four-table Phase 11C migration atomically."""

        connection = sqlite3.connect(self.database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            tables, _, _ = self._mistake_memory_schema_objects(connection)
            if tables == set(MISTAKE_MEMORY_TABLES):
                self._validate_mistake_memory_schema(connection)
                return False
            if tables:
                raise RuntimeError(
                    "Refusing Phase 11C migration: partial mistake-memory schema detected."
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in MISTAKE_MEMORY_MIGRATION_SQL:
                    connection.execute(statement)
                self._validate_mistake_memory_schema(connection)
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(
                        "Phase 11C mistake-memory migration failed integrity_check."
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError(
                        "Phase 11C mistake-memory migration created foreign-key violations."
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return True
        finally:
            connection.close()

    def _finalize_mistake_memory_migration_audit(self) -> Path | None:
        if self._mistake_memory_pre_migration_audit is None:
            return None
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            self._validate_mistake_memory_schema(connection)
            post_audit = self._database_integrity_snapshot(connection)
        finally:
            connection.close()
        if post_audit["integrity_check"] != "ok" or post_audit[
            "foreign_key_violation_count"
        ]:
            raise RuntimeError("Phase 11C migration failed post-migration checks.")
        audit = {
            **self._mistake_memory_pre_migration_audit,
            "migration_completed_at": _utc_now(),
            "outcome_review_schema_version": OUTCOME_REVIEW_SCHEMA_VERSION,
            "lesson_schema_version": LESSON_SCHEMA_VERSION,
            "lesson_source_schema_version": LESSON_SOURCE_SCHEMA_VERSION,
            "lesson_event_schema_version": LESSON_EVENT_SCHEMA_VERSION,
            "calculation_version": MISTAKE_MEMORY_CALCULATION_VERSION,
            "policy_version": MISTAKE_MEMORY_POLICY_VERSION,
            "post_migration": post_audit,
            "restore_policy": (
                "Close Elliott AI, preserve the migrated database and audit files, "
                "then restore the recorded SQLite backup only after explicit destructive approval."
            ),
        }
        assert self.mistake_memory_backup_path is not None
        audit_path = self.mistake_memory_backup_path.with_suffix(
            self.mistake_memory_backup_path.suffix + ".migration-audit.json"
        )
        audit_path.write_text(
            json.dumps(audit, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return audit_path.resolve()

    @staticmethod
    def _forecast_agent_schema_objects(
        connection: sqlite3.Connection,
    ) -> tuple[set[str], set[str], set[str]]:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if str(row[0]) in FORECAST_AGENT_ORCHESTRATION_TABLES
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
            if str(row[0]) in FORECAST_AGENT_ORCHESTRATION_INDEXES
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
            if str(row[0]) in FORECAST_AGENT_ORCHESTRATION_TRIGGERS
        }
        return tables, indexes, triggers

    @staticmethod
    def _validate_forecast_agent_schema(connection: sqlite3.Connection) -> None:
        tables, indexes, triggers = KnowledgeStore._forecast_agent_schema_objects(
            connection
        )
        if tables != set(FORECAST_AGENT_ORCHESTRATION_TABLES):
            raise RuntimeError("Forecast-agent orchestration schema is missing.")
        if indexes != set(FORECAST_AGENT_ORCHESTRATION_INDEXES):
            raise RuntimeError(
                "Forecast-agent orchestration indexes are partial or missing."
            )
        if triggers != set(FORECAST_AGENT_ORCHESTRATION_TRIGGERS):
            raise RuntimeError(
                "Forecast-agent orchestration append-only triggers are partial or missing."
            )
        expected_columns = (
            "orchestration_id",
            "orchestration_version",
            "supersedes_orchestration_id",
            "source_analysis_run_id",
            "source_degree_resolution_id",
            "request_id",
            "analysis_cutoff_utc",
            "decision_time_input_hash",
            "primary_result_hash",
            "alternative_result_hash",
            "auditor_result_hash",
            "final_result_hash",
            "validation_manifest_hash",
            "shadow_resolution_hash",
            "shadow_comparison_hash",
            "retrieved_lesson_ids_json",
            "selected_candidate_ids_json",
            "status",
            "provider",
            "model",
            "prompt_versions_json",
            "policy_version",
            "schema_version",
            "content_hash",
            "record_json",
            "created_at_utc",
        )
        actual_columns = tuple(
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(forecast_agent_orchestrations)"
            )
        )
        if actual_columns != expected_columns:
            raise RuntimeError(
                "Forecast-agent orchestration table has an unsupported layout."
            )
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(forecast_agent_orchestrations)"
        ).fetchall()
        if any(str(row[6]).upper() != "RESTRICT" for row in foreign_keys):
            raise RuntimeError(
                "Forecast-agent orchestration has a non-RESTRICT deletion relationship."
            )
        references = {
            (str(row[3]), str(row[2]), str(row[4])) for row in foreign_keys
        }
        if references != {
            ("source_analysis_run_id", "analysis_runs", "id"),
            ("source_degree_resolution_id", "degree_resolutions", "id"),
            (
                "supersedes_orchestration_id",
                "forecast_agent_orchestrations",
                "orchestration_id",
            ),
        }:
            raise RuntimeError(
                "Forecast-agent orchestration foreign-key references are unsupported."
            )

    def _backup_before_forecast_agent_migration(
        self, *, database_preexisted: bool
    ) -> Path | None:
        """Back up an existing store before the one-table Phase 11D1 migration."""

        if not database_preexisted:
            return None
        source = sqlite3.connect(self.database_path)
        source.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA foreign_keys = ON")
            tables, _, _ = self._forecast_agent_schema_objects(source)
            if tables == set(FORECAST_AGENT_ORCHESTRATION_TABLES):
                self._validate_forecast_agent_schema(source)
                return None
            if tables:
                raise RuntimeError(
                    "Refusing Phase 11D1 migration: partial forecast-agent schema detected."
                )
            pre_audit = self._database_integrity_snapshot(source)
            if pre_audit["integrity_check"] != "ok":
                raise RuntimeError(
                    "Refusing Phase 11D1 migration: SQLite integrity_check failed."
                )
            if pre_audit["foreign_key_violation_count"]:
                raise RuntimeError(
                    "Refusing Phase 11D1 migration: existing foreign-key violations were found."
                )
            backup_dir = self.database_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / (
                f"{self.database_path.stem}.pre_phase11d1_{stamp}"
                f"{self.database_path.suffix}"
            )
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
            self._forecast_agent_pre_migration_audit = {
                "phase": "11D1 - Decision-Time Technical Agents in Shadow Mode",
                "migration_scope": sorted(FORECAST_AGENT_ORCHESTRATION_TABLES),
                "migration_started_at": _utc_now(),
                "database_path": str(self.database_path),
                "backup_path": str(backup_path.resolve()),
                "pre_migration": pre_audit,
            }
            return backup_path.resolve()
        finally:
            source.close()

    def _initialize_forecast_agent_schema(self) -> bool:
        """Apply the single additive Phase 11D1 table in one transaction."""

        connection = sqlite3.connect(self.database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            tables, _, _ = self._forecast_agent_schema_objects(connection)
            if tables == set(FORECAST_AGENT_ORCHESTRATION_TABLES):
                self._validate_forecast_agent_schema(connection)
                return False
            if tables:
                raise RuntimeError(
                    "Refusing Phase 11D1 migration: partial forecast-agent schema detected."
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in FORECAST_AGENT_ORCHESTRATION_MIGRATION_SQL:
                    connection.execute(statement)
                self._validate_forecast_agent_schema(connection)
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(
                        "Phase 11D1 forecast-agent migration failed integrity_check."
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError(
                        "Phase 11D1 forecast-agent migration created foreign-key violations."
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return True
        finally:
            connection.close()

    def _finalize_forecast_agent_migration_audit(self) -> Path | None:
        if self._forecast_agent_pre_migration_audit is None:
            return None
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            self._validate_forecast_agent_schema(connection)
            post_audit = self._database_integrity_snapshot(connection)
        finally:
            connection.close()
        if post_audit["integrity_check"] != "ok" or post_audit[
            "foreign_key_violation_count"
        ]:
            raise RuntimeError("Phase 11D1 migration failed post-migration checks.")
        audit = {
            **self._forecast_agent_pre_migration_audit,
            "migration_completed_at": _utc_now(),
            "orchestration_schema_version": (
                TECHNICAL_AGENT_ORCHESTRATION_SCHEMA_VERSION
            ),
            "calculation_version": TECHNICAL_AGENT_CALCULATION_VERSION,
            "policy_version": TECHNICAL_AGENT_POLICY_VERSION,
            "post_migration": post_audit,
            "restore_policy": (
                "Close Elliott AI, preserve the migrated database and audit files, "
                "then restore the recorded SQLite backup only after explicit "
                "destructive approval."
            ),
        }
        assert self.forecast_agent_backup_path is not None
        audit_path = self.forecast_agent_backup_path.with_suffix(
            self.forecast_agent_backup_path.suffix + ".migration-audit.json"
        )
        audit_path.write_text(
            json.dumps(audit, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return audit_path.resolve()

    @staticmethod
    def _outcome_learning_schema_objects(
        connection: sqlite3.Connection,
    ) -> tuple[set[str], set[str], set[str]]:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if str(row[0]) in FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TABLES
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
            if str(row[0]) in FORECAST_OUTCOME_LEARNING_ORCHESTRATION_INDEXES
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
            if str(row[0]) in FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TRIGGERS
        }
        return tables, indexes, triggers

    @staticmethod
    def _validate_outcome_learning_schema(
        connection: sqlite3.Connection,
    ) -> None:
        tables, indexes, triggers = KnowledgeStore._outcome_learning_schema_objects(
            connection
        )
        if tables != set(FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TABLES):
            raise RuntimeError("Outcome-learning orchestration schema is missing.")
        if indexes != set(FORECAST_OUTCOME_LEARNING_ORCHESTRATION_INDEXES):
            raise RuntimeError(
                "Outcome-learning orchestration indexes are partial or missing."
            )
        if triggers != set(FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TRIGGERS):
            raise RuntimeError(
                "Outcome-learning append-only triggers are partial or missing."
            )
        expected_columns = (
            "orchestration_id",
            "orchestration_version",
            "supersedes_orchestration_id",
            "orchestration_kind",
            "status",
            "human_action_required",
            "forecast_id",
            "observation_set_id",
            "evaluation_id",
            "outcome_review_id",
            "existing_lesson_id",
            "request_id",
            "provider",
            "model",
            "prompt_version",
            "policy_version",
            "frozen_input_hash",
            "structured_draft_hash",
            "result_hash",
            "warnings_json",
            "validation_errors_json",
            "started_at_utc",
            "completed_at_utc",
            "schema_version",
            "calculation_version",
            "content_hash",
            "record_json",
        )
        actual_columns = tuple(
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(forecast_outcome_learning_orchestrations)"
            )
        )
        if actual_columns != expected_columns:
            raise RuntimeError(
                "Outcome-learning orchestration table has an unsupported layout."
            )
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(forecast_outcome_learning_orchestrations)"
        ).fetchall()
        if any(str(row[6]).upper() != "RESTRICT" for row in foreign_keys):
            raise RuntimeError(
                "Outcome-learning orchestration has a non-RESTRICT deletion relationship."
            )
        references = {
            (str(row[3]), str(row[2]), str(row[4])) for row in foreign_keys
        }
        expected_references = {
            (
                "supersedes_orchestration_id",
                "forecast_outcome_learning_orchestrations",
                "orchestration_id",
            ),
            ("forecast_id", "forecast_records", "forecast_id"),
            (
                "observation_set_id",
                "forecast_observation_sets",
                "observation_set_id",
            ),
            (
                "evaluation_id",
                "forecast_outcome_evaluations",
                "evaluation_id",
            ),
            ("outcome_review_id", "forecast_outcome_reviews", "review_id"),
            ("existing_lesson_id", "mistake_memory_lessons", "lesson_id"),
        }
        if references != expected_references:
            raise RuntimeError(
                "Outcome-learning orchestration foreign-key references are unsupported."
            )

    @staticmethod
    def _forecast_agent_schema_hashes(
        connection: sqlite3.Connection,
    ) -> dict[str, str]:
        protected_names = {
            *FORECAST_AGENT_ORCHESTRATION_TABLES,
            *FORECAST_AGENT_ORCHESTRATION_INDEXES,
            *FORECAST_AGENT_ORCHESTRATION_TRIGGERS,
        }
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE name IN ("
            + ",".join("?" for _ in protected_names)
            + ") ORDER BY name",
            tuple(sorted(protected_names)),
        ).fetchall()
        hashes = {
            str(row[0]): _sha256(str(row[1] or ""))
            for row in rows
        }
        if set(hashes) != protected_names:
            raise RuntimeError(
                "Protected Phase 11D1 schema objects are missing before D2 migration."
            )
        return hashes

    def _backup_before_outcome_learning_migration(
        self, *, database_preexisted: bool
    ) -> Path | None:
        """Back up an existing store before the one-table Phase 11D2 migration."""

        if not database_preexisted:
            return None
        source = sqlite3.connect(self.database_path)
        source.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA foreign_keys = ON")
            tables, _, _ = self._outcome_learning_schema_objects(source)
            if tables == set(FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TABLES):
                self._validate_outcome_learning_schema(source)
                return None
            if tables:
                raise RuntimeError(
                    "Refusing Phase 11D2 migration: partial outcome-learning schema detected."
                )
            pre_audit = self._database_integrity_snapshot(source)
            if pre_audit["integrity_check"] != "ok":
                raise RuntimeError(
                    "Refusing Phase 11D2 migration: SQLite integrity_check failed."
                )
            if pre_audit["foreign_key_violation_count"]:
                raise RuntimeError(
                    "Refusing Phase 11D2 migration: existing foreign-key violations were found."
                )
            backup_dir = self.database_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = backup_dir / (
                f"{self.database_path.stem}.pre_phase11d2_{stamp}"
                f"{self.database_path.suffix}"
            )
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
            self._outcome_learning_pre_migration_audit = {
                "phase": "11D2 - Outcome Reviewer and Mistake Memory Proposal Agents",
                "migration_scope": sorted(
                    FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TABLES
                ),
                "migration_started_at": _utc_now(),
                "database_path": str(self.database_path),
                "backup_path": str(backup_path.resolve()),
                "pre_migration": pre_audit,
                "protected_phase11d1_schema_hashes": (
                    self._forecast_agent_schema_hashes(source)
                ),
            }
            pre_audit_path = backup_path.with_suffix(
                backup_path.suffix + ".pre-migration-audit.json"
            )
            pre_audit_path.write_text(
                json.dumps(
                    self._outcome_learning_pre_migration_audit,
                    ensure_ascii=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            self.outcome_learning_pre_migration_audit_path = (
                pre_audit_path.resolve()
            )
            return backup_path.resolve()
        finally:
            source.close()

    def _initialize_outcome_learning_schema(self) -> bool:
        """Apply the single additive Phase 11D2 table in one transaction."""

        connection = sqlite3.connect(self.database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            tables, _, _ = self._outcome_learning_schema_objects(connection)
            if tables == set(FORECAST_OUTCOME_LEARNING_ORCHESTRATION_TABLES):
                self._validate_outcome_learning_schema(connection)
                return False
            if tables:
                raise RuntimeError(
                    "Refusing Phase 11D2 migration: partial outcome-learning schema detected."
                )
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in FORECAST_OUTCOME_LEARNING_ORCHESTRATION_MIGRATION_SQL:
                    connection.execute(statement)
                self._validate_outcome_learning_schema(connection)
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(
                        "Phase 11D2 outcome-learning migration failed integrity_check."
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError(
                        "Phase 11D2 outcome-learning migration created foreign-key violations."
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return True
        finally:
            connection.close()

    def _finalize_outcome_learning_migration_audit(self) -> Path | None:
        if self._outcome_learning_pre_migration_audit is None:
            return None
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            self._validate_outcome_learning_schema(connection)
            post_audit = self._database_integrity_snapshot(connection)
            protected_hashes = self._forecast_agent_schema_hashes(connection)
        finally:
            connection.close()
        if post_audit["integrity_check"] != "ok" or post_audit[
            "foreign_key_violation_count"
        ]:
            raise RuntimeError("Phase 11D2 migration failed post-migration checks.")
        if protected_hashes != self._outcome_learning_pre_migration_audit[
            "protected_phase11d1_schema_hashes"
        ]:
            raise RuntimeError(
                "Phase 11D2 migration changed protected Phase 11D1 schema objects."
            )
        audit = {
            **self._outcome_learning_pre_migration_audit,
            "migration_completed_at": _utc_now(),
            "orchestration_schema_version": (
                OUTCOME_LEARNING_ORCHESTRATION_SCHEMA_VERSION
            ),
            "calculation_version": OUTCOME_LEARNING_CALCULATION_VERSION,
            "policy_version": OUTCOME_LEARNING_POLICY_VERSION,
            "post_migration": post_audit,
            "d1_table_unchanged": True,
            "post_phase11d1_schema_hashes": protected_hashes,
            "restore_policy": (
                "Close Elliott AI, preserve the migrated database and audit files, "
                "then restore the recorded SQLite backup only after explicit "
                "destructive approval."
            ),
        }
        assert self.outcome_learning_backup_path is not None
        audit_path = self.outcome_learning_backup_path.with_suffix(
            self.outcome_learning_backup_path.suffix + ".migration-audit.json"
        )
        audit_path.write_text(
            json.dumps(audit, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return audit_path.resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _immediate_connection(self) -> Iterator[sqlite3.Connection]:
        """Serialize workflow writers and keep every related write atomic."""
        connection = sqlite3.connect(self.database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    indexed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    section TEXT NOT NULL,
                    content TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    section,
                    content,
                    tokenize = 'unicode61'
                );
                CREATE TABLE IF NOT EXISTS registered_sources (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    question TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accepted_cases (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL UNIQUE REFERENCES analysis_runs(id),
                    title TEXT NOT NULL,
                    user_note TEXT NOT NULL,
                    accepted_payload_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_reviews (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    review_status TEXT NOT NULL,
                    review_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, revision)
                );
                CREATE TABLE IF NOT EXISTS degree_resolutions (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    request_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    readiness_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feature_schema_versions (
                    schema_version TEXT PRIMARY KEY,
                    calculation_version TEXT NOT NULL,
                    schema_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wave_observations (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER REFERENCES analysis_runs(id) ON DELETE SET NULL,
                    resolution_id INTEGER REFERENCES degree_resolutions(id) ON DELETE SET NULL,
                    wave_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    cutoff_timestamp TEXT NOT NULL,
                    observation_hash TEXT NOT NULL UNIQUE,
                    observation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wave_fingerprints (
                    id INTEGER PRIMARY KEY,
                    observation_id INTEGER NOT NULL REFERENCES wave_observations(id) ON DELETE CASCADE,
                    run_id INTEGER REFERENCES analysis_runs(id) ON DELETE SET NULL,
                    resolution_id INTEGER REFERENCES degree_resolutions(id) ON DELETE SET NULL,
                    feature_schema_version TEXT NOT NULL REFERENCES feature_schema_versions(schema_version),
                    calculation_version TEXT NOT NULL,
                    identity_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    fingerprint_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS wave_fingerprints_identity_idx
                    ON wave_fingerprints(identity_hash);
                CREATE INDEX IF NOT EXISTS wave_fingerprints_run_idx
                    ON wave_fingerprints(run_id, resolution_id);
                CREATE TABLE IF NOT EXISTS correction_schema_versions (
                    schema_version TEXT PRIMARY KEY,
                    calculation_version TEXT NOT NULL,
                    schema_kind TEXT NOT NULL,
                    schema_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS correction_cases (
                    case_id TEXT PRIMARY KEY,
                    source_run_id INTEGER REFERENCES analysis_runs(id) ON DELETE SET NULL,
                    source_resolution_id INTEGER REFERENCES degree_resolutions(id) ON DELETE SET NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    endpoint_timestamp TEXT NOT NULL,
                    elliott_degree TEXT NOT NULL,
                    parent_pattern_family TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    calculation_version TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    case_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hypothesis_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES correction_cases(case_id) ON DELETE CASCADE,
                    parent_snapshot_id TEXT REFERENCES hypothesis_snapshots(snapshot_id) ON DELETE RESTRICT,
                    source_run_id INTEGER REFERENCES analysis_runs(id) ON DELETE SET NULL,
                    sequence_number INTEGER NOT NULL,
                    cutoff_timestamp TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    calculation_version TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS hypothesis_snapshots_case_idx
                    ON hypothesis_snapshots(case_id, sequence_number, created_at);
                CREATE TABLE IF NOT EXISTS hypothesis_states (
                    id INTEGER PRIMARY KEY,
                    snapshot_id TEXT NOT NULL REFERENCES hypothesis_snapshots(snapshot_id) ON DELETE CASCADE,
                    hypothesis_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    UNIQUE(snapshot_id, hypothesis_id)
                );
                CREATE TABLE IF NOT EXISTS hypothesis_transitions (
                    transition_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES correction_cases(case_id) ON DELETE CASCADE,
                    snapshot_id TEXT NOT NULL REFERENCES hypothesis_snapshots(snapshot_id) ON DELETE CASCADE,
                    hypothesis_id TEXT NOT NULL,
                    previous_state TEXT NOT NULL,
                    new_state TEXT NOT NULL,
                    cutoff_timestamp TEXT NOT NULL,
                    provisional INTEGER NOT NULL,
                    calculation_version TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    transition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS hypothesis_transitions_case_idx
                    ON hypothesis_transitions(case_id, created_at);
                CREATE TABLE IF NOT EXISTS resolved_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES correction_cases(case_id) ON DELETE CASCADE,
                    selected_snapshot_id TEXT NOT NULL REFERENCES hypothesis_snapshots(snapshot_id) ON DELETE RESTRICT,
                    review_snapshot_id TEXT REFERENCES hypothesis_snapshots(snapshot_id) ON DELETE RESTRICT,
                    revision INTEGER NOT NULL,
                    resolved_hypothesis TEXT NOT NULL,
                    resolution_cutoff TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    outcome_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(case_id, revision)
                );
                CREATE TABLE IF NOT EXISTS outcome_reviews (
                    review_id TEXT PRIMARY KEY,
                    outcome_id TEXT NOT NULL REFERENCES resolved_outcomes(outcome_id) ON DELETE CASCADE,
                    case_id TEXT NOT NULL REFERENCES correction_cases(case_id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    review_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS outcome_reviews_case_idx
                    ON outcome_reviews(case_id, created_at);
                CREATE TABLE IF NOT EXISTS experience_schema_versions (
                    schema_version TEXT PRIMARY KEY,
                    calculation_version TEXT NOT NULL,
                    schema_kind TEXT NOT NULL,
                    schema_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experience_cases (
                    experience_case_id TEXT PRIMARY KEY,
                    market_episode_id TEXT NOT NULL,
                    case_version INTEGER NOT NULL,
                    supersedes_case_id TEXT REFERENCES experience_cases(experience_case_id) ON DELETE RESTRICT,
                    source_correction_case_id TEXT NOT NULL REFERENCES correction_cases(case_id) ON DELETE RESTRICT,
                    source_endpoint_snapshot_id TEXT NOT NULL REFERENCES hypothesis_snapshots(snapshot_id) ON DELETE RESTRICT,
                    source_fingerprint_id INTEGER NOT NULL REFERENCES wave_fingerprints(id) ON DELETE RESTRICT,
                    source_outcome_id TEXT NOT NULL REFERENCES resolved_outcomes(outcome_id) ON DELETE RESTRICT,
                    source_outcome_review_id TEXT NOT NULL REFERENCES outcome_reviews(review_id) ON DELETE RESTRICT,
                    assembly_state TEXT NOT NULL,
                    schema_version TEXT NOT NULL REFERENCES experience_schema_versions(schema_version),
                    calculation_version TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    case_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(market_episode_id, case_version)
                );
                CREATE INDEX IF NOT EXISTS experience_cases_episode_idx
                    ON experience_cases(market_episode_id, case_version);
                CREATE INDEX IF NOT EXISTS experience_cases_sources_idx
                    ON experience_cases(source_correction_case_id, source_outcome_id);
                CREATE TABLE IF NOT EXISTS pattern_dna (
                    dna_id TEXT PRIMARY KEY,
                    experience_case_id TEXT NOT NULL REFERENCES experience_cases(experience_case_id) ON DELETE RESTRICT,
                    market_episode_id TEXT NOT NULL,
                    dna_kind TEXT NOT NULL,
                    schema_version TEXT NOT NULL REFERENCES experience_schema_versions(schema_version),
                    calculation_version TEXT NOT NULL,
                    cutoff_timestamp TEXT,
                    source_content_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    dna_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(experience_case_id, dna_kind)
                );
                CREATE INDEX IF NOT EXISTS pattern_dna_case_idx
                    ON pattern_dna(experience_case_id, dna_kind);
                CREATE TABLE IF NOT EXISTS experience_tags (
                    tag_id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    value TEXT NOT NULL,
                    tag_key TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    tag_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(schema_version, tag_key)
                );
                CREATE TABLE IF NOT EXISTS experience_case_tags (
                    assignment_id TEXT PRIMARY KEY,
                    experience_case_id TEXT NOT NULL REFERENCES experience_cases(experience_case_id) ON DELETE RESTRICT,
                    tag_id TEXT NOT NULL REFERENCES experience_tags(tag_id) ON DELETE RESTRICT,
                    action TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    parent_assignment_id TEXT REFERENCES experience_case_tags(assignment_id) ON DELETE RESTRICT,
                    actor TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    assignment_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(experience_case_id, tag_id, revision)
                );
                CREATE INDEX IF NOT EXISTS experience_case_tags_case_idx
                    ON experience_case_tags(experience_case_id, tag_id, revision);
                CREATE TABLE IF NOT EXISTS experience_reviews (
                    review_id TEXT PRIMARY KEY,
                    experience_case_id TEXT NOT NULL REFERENCES experience_cases(experience_case_id) ON DELETE RESTRICT,
                    parent_review_id TEXT REFERENCES experience_reviews(review_id) ON DELETE RESTRICT,
                    source_outcome_review_id TEXT NOT NULL REFERENCES outcome_reviews(review_id) ON DELETE RESTRICT,
                    action TEXT NOT NULL,
                    effective_action TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    human_confirmed INTEGER NOT NULL,
                    quality_status TEXT,
                    content_hash TEXT NOT NULL UNIQUE,
                    review_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS experience_reviews_case_idx
                    ON experience_reviews(experience_case_id, created_at);
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO feature_schema_versions(
                    schema_version, calculation_version, schema_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    FEATURE_SCHEMA_VERSION,
                    FINGERPRINT_CALCULATION_VERSION,
                    json.dumps(FEATURE_SCHEMA_DEFINITION, ensure_ascii=True, sort_keys=True),
                    _utc_now(),
                ),
            )
            for schema_version, calculation_version, schema_kind, definition in (
                (
                    CORRECTION_STATE_SCHEMA_VERSION,
                    CORRECTION_STATE_CALCULATION_VERSION,
                    "decision_snapshot",
                    {
                        "immutability": "Snapshots are append-only and linked parent to child.",
                        "look_ahead": "Evidence is filtered by declared cutoff and timing class.",
                        "invalidation": "Only hard structure or explicit price levels invalidate.",
                    },
                ),
                (
                    OUTCOME_SCHEMA_VERSION,
                    CORRECTION_STATE_CALCULATION_VERSION,
                    "reviewed_outcome",
                    {
                        "separation": "Historical outcomes are separate from degree_resolutions.",
                        "review": "Outcome confidence is a human field, never a model probability.",
                        "history": "Revisions and rejections are append-only.",
                    },
                ),
            ):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO correction_schema_versions(
                        schema_version, calculation_version, schema_kind,
                        schema_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        schema_version,
                        calculation_version,
                        schema_kind,
                        json.dumps(definition, ensure_ascii=True, sort_keys=True),
                        _utc_now(),
                    ),
                )
            for schema_version, calculation_version, schema_kind, definition in (
                (
                    EXPERIENCE_SCHEMA_VERSION,
                    EXPERIENCE_CALCULATION_VERSION,
                    "experience_case",
                    {
                        "identity": "One market episode may have append-only case versions.",
                        "acceptance": "Only an explicit named human review can accept a case.",
                        "retrieval": "Similarity retrieval is deferred beyond Phase 5A.1.",
                    },
                ),
                (
                    PATTERN_DNA_SCHEMA_VERSION,
                    PATTERN_DNA_CALCULATION_VERSION,
                    "pattern_dna",
                    {
                        "timing_layers": list(DNA_KINDS),
                        "endpoint_source": "Phase 3 fingerprint projection only.",
                        "confirmation_source": "Explicit Phase 4 events and transitions only.",
                        "missing_values": "Null plus unavailable or incomparable reason.",
                    },
                ),
            ):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO experience_schema_versions(
                        schema_version, calculation_version, schema_kind,
                        schema_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        schema_version,
                        calculation_version,
                        schema_kind,
                        json.dumps(definition, ensure_ascii=True, sort_keys=True),
                        _utc_now(),
                    ),
                )
            accepted_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(accepted_cases)").fetchall()
            }
            if "accepted_payload_json" not in accepted_columns:
                connection.execute(
                    "ALTER TABLE accepted_cases ADD COLUMN accepted_payload_json TEXT"
                )

    def register_source(
        self, path: Path, *, kind: str = "research", status: str = "candidate"
    ) -> int:
        resolved = Path(path).resolve()
        if _source_from_path(resolved, force_kind=kind) is None:
            raise ValueError("Source must be a readable text, Markdown, or non-OHLCV JSON file.")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO registered_sources(path, kind, status, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, status=excluded.status
                """,
                (str(resolved), kind, status, _utc_now()),
            )
            row = connection.execute(
                "SELECT id FROM registered_sources WHERE path = ?", (str(resolved),)
            ).fetchone()
            return int(row["id"])

    def _registered_documents(self) -> list[SourceDocument]:
        documents: list[SourceDocument] = []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT path, kind, status FROM registered_sources ORDER BY id"
            ).fetchall()
        for row in rows:
            document = _source_from_path(Path(row["path"]), force_kind=row["kind"])
            if document:
                documents.append(
                    SourceDocument(
                        path=document.path,
                        kind=row["kind"],
                        status=row["status"],
                        content=document.content,
                        parsed=document.parsed,
                    )
                )
        return documents

    def _accepted_documents(self) -> list[SourceDocument]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.title, c.user_note, c.accepted_payload_json,
                       r.symbol, r.question, r.response_json
                FROM accepted_cases c
                JOIN analysis_runs r ON r.id = c.run_id
                ORDER BY c.id
                """
            ).fetchall()
        documents: list[SourceDocument] = []
        for row in rows:
            payload = None
            if row["accepted_payload_json"]:
                try:
                    payload = json.loads(row["accepted_payload_json"])
                except json.JSONDecodeError:
                    payload = None
            if not isinstance(payload, dict):
                payload = {
                    "title": row["title"],
                    "symbol": row["symbol"],
                    "original_question": row["question"],
                    "user_note": row["user_note"],
                    "accepted_analysis": json.loads(row["response_json"]),
                }
            content = json.dumps(payload, ensure_ascii=True, indent=2)
            documents.append(
                SourceDocument(
                    path=f"memory://accepted/{row['id']}",
                    kind="approved_case_memory",
                    status="canonical",
                    content=content,
                    parsed=json.loads(content),
                )
            )
        return documents

    def index_workspace(self, workspace: Path) -> dict[str, int]:
        documents_by_path = {
            document.path: document for document in discover_workspace_sources(workspace)
        }
        for document in self._registered_documents() + self._accepted_documents():
            documents_by_path[document.path] = document

        chunk_count = 0
        with self.connect() as connection:
            connection.execute("DELETE FROM chunks_fts")
            connection.execute("DELETE FROM chunks")
            connection.execute("DELETE FROM documents")
            for document in documents_by_path.values():
                cursor = connection.execute(
                    """
                    INSERT INTO documents(path, kind, status, sha256, indexed_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        document.path,
                        document.kind,
                        document.status,
                        _sha256(document.content),
                        _utc_now(),
                    ),
                )
                document_id = int(cursor.lastrowid)
                for ordinal, (section, content) in enumerate(_document_chunks(document)):
                    content = content.strip()
                    if not content:
                        continue
                    chunk_cursor = connection.execute(
                        """
                        INSERT INTO chunks(document_id, ordinal, section, content)
                        VALUES (?, ?, ?, ?)
                        """,
                        (document_id, ordinal, section, content),
                    )
                    connection.execute(
                        "INSERT INTO chunks_fts(chunk_id, section, content) VALUES (?, ?, ?)",
                        (int(chunk_cursor.lastrowid), section, content),
                    )
                    chunk_count += 1
        return {"documents": len(documents_by_path), "chunks": chunk_count}

    def stats(self) -> dict[str, int]:
        with self.connect() as connection:
            return {
                "documents": int(connection.execute("SELECT count(*) FROM documents").fetchone()[0]),
                "chunks": int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0]),
                "runs": int(connection.execute("SELECT count(*) FROM analysis_runs").fetchone()[0]),
                "accepted_cases": int(
                    connection.execute("SELECT count(*) FROM accepted_cases").fetchone()[0]
                ),
                "registered_sources": int(
                    connection.execute("SELECT count(*) FROM registered_sources").fetchone()[0]
                ),
                "reviews": int(
                    connection.execute("SELECT count(*) FROM run_reviews").fetchone()[0]
                ),
                "degree_resolutions": int(
                    connection.execute("SELECT count(*) FROM degree_resolutions").fetchone()[0]
                ),
                "wave_observations": int(
                    connection.execute("SELECT count(*) FROM wave_observations").fetchone()[0]
                ),
                "wave_fingerprints": int(
                    connection.execute("SELECT count(*) FROM wave_fingerprints").fetchone()[0]
                ),
                "feature_schema_versions": int(
                    connection.execute("SELECT count(*) FROM feature_schema_versions").fetchone()[0]
                ),
                "correction_cases": int(
                    connection.execute("SELECT count(*) FROM correction_cases").fetchone()[0]
                ),
                "hypothesis_snapshots": int(
                    connection.execute("SELECT count(*) FROM hypothesis_snapshots").fetchone()[0]
                ),
                "hypothesis_transitions": int(
                    connection.execute("SELECT count(*) FROM hypothesis_transitions").fetchone()[0]
                ),
                "resolved_outcomes": int(
                    connection.execute("SELECT count(*) FROM resolved_outcomes").fetchone()[0]
                ),
                "outcome_reviews": int(
                    connection.execute("SELECT count(*) FROM outcome_reviews").fetchone()[0]
                ),
                "correction_schema_versions": int(
                    connection.execute("SELECT count(*) FROM correction_schema_versions").fetchone()[0]
                ),
                "experience_schema_versions": int(
                    connection.execute("SELECT count(*) FROM experience_schema_versions").fetchone()[0]
                ),
                "experience_cases": int(
                    connection.execute("SELECT count(*) FROM experience_cases").fetchone()[0]
                ),
                "pattern_dna": int(
                    connection.execute("SELECT count(*) FROM pattern_dna").fetchone()[0]
                ),
                "experience_reviews": int(
                    connection.execute("SELECT count(*) FROM experience_reviews").fetchone()[0]
                ),
                "experience_tag_actions": int(
                    connection.execute("SELECT count(*) FROM experience_case_tags").fetchone()[0]
                ),
                "experience_workflow_cases": int(
                    connection.execute(
                        "SELECT count(*) FROM experience_workflow_cases"
                    ).fetchone()[0]
                ),
                "experience_workflow_events": int(
                    connection.execute(
                        "SELECT count(*) FROM experience_workflow_events"
                    ).fetchone()[0]
                ),
            }

    @staticmethod
    def _match_query(query: str) -> str:
        stop_words = {"the", "and", "for", "from", "this", "that", "with", "what"}
        terms: list[str] = []
        for term in re.findall(r"[A-Za-z0-9_]+", query.lower()):
            if term in stop_words or term in terms:
                continue
            terms.append(term)
        return " OR ".join(f'"{term}"' for term in terms[:24])

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        include_superseded: bool = False,
        kinds: Iterable[str] | None = None,
    ) -> list[KnowledgeHit]:
        match_query = self._match_query(query)
        if not match_query:
            return []
        conditions = ["chunks_fts MATCH ?"]
        parameters: list[Any] = [match_query]
        if not include_superseded:
            conditions.append("d.status != 'superseded'")
        kind_values = tuple(kinds or ())
        if kind_values:
            conditions.append(
                "d.kind IN (" + ", ".join("?" for _ in kind_values) + ")"
            )
            parameters.extend(kind_values)
        sql = f"""
            SELECT
                c.id AS chunk_id,
                d.path,
                d.kind,
                d.status,
                c.section,
                c.content,
                (
                    bm25(chunks_fts, 0.0, 2.0, 1.0)
                    - CASE d.kind
                        WHEN 'rulebook' THEN 4.0
                        WHEN 'approved_case_memory' THEN 3.5
                        WHEN 'brain_summary' THEN 3.0
                        ELSE 0.0
                      END
                    - CASE d.status WHEN 'canonical' THEN 2.0 ELSE 0.0 END
                ) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.id = CAST(chunks_fts.chunk_id AS INTEGER)
            JOIN documents d ON d.id = c.document_id
            WHERE {' AND '.join(conditions)}
            ORDER BY rank ASC
            LIMIT ?
        """
        parameters.append(max(1, limit))
        with self.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [
            KnowledgeHit(
                evidence_id=f"K{index}",
                source=row["path"],
                kind=row["kind"],
                status=row["status"],
                section=row["section"],
                content=row["content"],
                score=round(-float(row["rank"]), 6),
            )
            for index, row in enumerate(rows, start=1)
        ]

    def save_run(
        self,
        *,
        symbol: str,
        provider: str,
        model: str | None,
        question: str,
        request: dict[str, Any],
        evidence: dict[str, Any],
        response: dict[str, Any],
        validation_errors: list[str],
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO analysis_runs(
                    created_at, symbol, provider, model, question,
                    request_json, evidence_json, response_json, validation_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(),
                    symbol,
                    provider,
                    model,
                    question,
                    json.dumps(request, ensure_ascii=True),
                    json.dumps(evidence, ensure_ascii=True),
                    json.dumps(response, ensure_ascii=True),
                    json.dumps(validation_errors, ensure_ascii=True),
                ),
            )
            return int(cursor.lastrowid)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in ("request_json", "evidence_json", "response_json", "validation_json"):
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        return result

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.id, r.created_at, r.symbol, r.provider, r.model, r.question,
                       CASE WHEN c.id IS NULL THEN 0 ELSE 1 END AS accepted
                FROM analysis_runs r
                LEFT JOIN accepted_cases c ON c.run_id = r.id
                ORDER BY r.id DESC LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_review(self, run_id: int, review: dict[str, Any]) -> dict[str, Any]:
        """Save an immutable review revision for a source analysis run."""
        from .review import validate_review

        if self.get_run(run_id) is None:
            raise KeyError(f"Analysis run {run_id} does not exist.")
        errors = validate_review(review)
        if errors:
            raise ValueError("Invalid correction document: " + " ".join(errors))
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM run_reviews WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            revision = int(row["revision"])
            cursor = connection.execute(
                """
                INSERT INTO run_reviews(run_id, revision, review_status, review_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    revision,
                    review["review_status"],
                    json.dumps(review, ensure_ascii=True),
                    _utc_now(),
                ),
            )
            review_id = int(cursor.lastrowid)
        return {
            "review_id": review_id,
            "run_id": run_id,
            "revision": revision,
            "review_status": review["review_status"],
        }

    def get_latest_review(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM run_reviews
                WHERE run_id = ?
                ORDER BY revision DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["review"] = json.loads(result.pop("review_json"))
        return result

    def save_degree_resolution(
        self,
        *,
        run_id: int,
        provider: str,
        model: str | None,
        request: dict[str, Any],
        response: dict[str, Any],
        validation_errors: list[str],
        readiness: dict[str, Any],
    ) -> int:
        if self.get_run(run_id) is None:
            raise KeyError(f"Analysis run {run_id} does not exist.")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO degree_resolutions(
                    run_id, created_at, provider, model, request_json,
                    response_json, validation_json, readiness_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    _utc_now(),
                    provider,
                    model,
                    json.dumps(request, ensure_ascii=True),
                    json.dumps(response, ensure_ascii=True),
                    json.dumps(validation_errors, ensure_ascii=True),
                    json.dumps(readiness, ensure_ascii=True),
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _degree_resolution_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("request_json", "response_json", "validation_json", "readiness_json"):
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        return result

    def get_degree_resolution(self, resolution_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM degree_resolutions WHERE id = ?", (resolution_id,)
            ).fetchone()
        return self._degree_resolution_row(row) if row is not None else None

    def get_latest_degree_resolution(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM degree_resolutions
                WHERE run_id = ?
                ORDER BY CASE WHEN provider = 'packet' THEN 1 ELSE 0 END, id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return self._degree_resolution_row(row) if row is not None else None

    def list_degree_resolutions(self, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, created_at, provider, model, readiness_json
                FROM degree_resolutions WHERE run_id = ? ORDER BY id DESC
                """,
                (run_id,),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            readiness = json.loads(item.pop("readiness_json"))
            item["final_report_ready"] = bool(readiness.get("final_report_ready"))
            results.append(item)
        return results

    @staticmethod
    def _forecast_source_errors(
        connection: sqlite3.Connection, forecast: ForecastRecord
    ) -> tuple[str, ...]:
        errors: list[str] = []
        run_row = connection.execute(
            "SELECT * FROM analysis_runs WHERE id = ?",
            (forecast.source_analysis_run_id,),
        ).fetchone()
        if run_row is None:
            return (
                f"Analysis run {forecast.source_analysis_run_id} does not exist.",
            )
        run_payload = dict(run_row)
        try:
            for field in (
                "request_json",
                "evidence_json",
                "response_json",
                "validation_json",
            ):
                run_payload[field.removesuffix("_json")] = json.loads(
                    run_payload.pop(field)
                )
        except (json.JSONDecodeError, TypeError) as exc:
            return (f"Source analysis run JSON is invalid: {exc}",)
        resolution_row = connection.execute(
            "SELECT * FROM degree_resolutions WHERE id = ?",
            (forecast.source_degree_resolution_id,),
        ).fetchone()
        if resolution_row is None:
            return (
                "Degree resolution "
                f"{forecast.source_degree_resolution_id} does not exist.",
            )
        try:
            resolution_payload = KnowledgeStore._degree_resolution_row(resolution_row)
        except (json.JSONDecodeError, TypeError) as exc:
            return (f"Source degree-resolution JSON is invalid: {exc}",)
        if int(resolution_payload["run_id"]) != forecast.source_analysis_run_id:
            errors.append(
                "Forecast degree resolution does not belong to its source analysis run."
            )
        if forecast.source_hashes["analysis_run"] != stored_record_content_hash(
            run_payload
        ):
            errors.append("Forecast analysis-run source hash does not match storage.")
        if forecast.source_hashes[
            "degree_resolution"
        ] != stored_record_content_hash(resolution_payload):
            errors.append(
                "Forecast degree-resolution source hash does not match storage."
            )

        evidence = run_payload.get("evidence")
        market_data = evidence.get("market_data") if isinstance(evidence, Mapping) else ()
        if isinstance(market_data, Sequence) and not isinstance(
            market_data, (str, bytes)
        ):
            expected_datasets = tuple(
                dataset_cutoff_from_stored_summary(
                    item, requested_symbol=forecast.symbol
                )
                for item in market_data
                if isinstance(item, Mapping)
            )
            expected_by_summary_hash = {
                item.dataset_hash: item for item in expected_datasets
            }
            expected_source_hashes = {
                item.source_document_hash
                for item in expected_datasets
                if item.source_document_hash is not None
            }
            covered_summary_hashes: set[str] = set()
            covered_source_hashes: set[str] = set()
            for dataset in forecast.dataset_cutoffs:
                if dataset.hash_scope.value == "stored_dataset_summary":
                    expected = expected_by_summary_hash.get(dataset.dataset_hash)
                    if expected is None:
                        errors.append(
                            f"Dataset {dataset.dataset_id} is not present in the stored analysis evidence."
                        )
                    elif expected.to_dict() != dataset.to_dict():
                        errors.append(
                            f"Dataset {dataset.dataset_id} metadata differs from its stored summary."
                        )
                    else:
                        covered_summary_hashes.add(dataset.dataset_hash)
                elif dataset.source_document_hash in expected_source_hashes:
                    assert dataset.source_document_hash is not None
                    covered_source_hashes.add(dataset.source_document_hash)
            for expected in expected_datasets:
                covered = expected.dataset_hash in covered_summary_hashes
                if expected.source_document_hash is not None:
                    covered = covered or (
                        expected.source_document_hash in covered_source_hashes
                    )
                if not covered:
                    errors.append(
                        f"Stored dataset {expected.dataset_id} is missing from the forecast."
                    )
        return tuple(errors)

    @staticmethod
    def _forecast_record_from_row(
        row: sqlite3.Row, connection: sqlite3.Connection | None = None
    ) -> ForecastRecord:
        try:
            payload = json.loads(str(row["record_json"]))
            record = ForecastRecord.from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Stored forecast {row['forecast_id']} failed immutable validation."
            ) from exc
        mirrored_fields = {
            "forecast_id": record.forecast_id,
            "forecast_version": record.forecast_version,
            "supersedes_forecast_id": record.supersedes_forecast_id,
            "source_analysis_run_id": record.source_analysis_run_id,
            "source_degree_resolution_id": record.source_degree_resolution_id,
            "schema_version": record.schema_version,
            "calculation_version": record.calculation_version,
            "policy_version": record.policy_version,
            "evaluation_eligibility": record.evaluation_eligibility.status.value,
            "record_state": record.evaluation_eligibility.record_state.value,
            "symbol": record.symbol,
            "analysis_cutoff_utc": record.analysis_cutoff_utc,
            "content_hash": record.content_hash,
            "created_at_utc": record.created_at_utc,
        }
        for field_name, expected in mirrored_fields.items():
            if row[field_name] != expected:
                raise RuntimeError(
                    f"Stored forecast {record.forecast_id} has a mismatched {field_name}."
                )
        if connection is not None:
            source_errors = KnowledgeStore._forecast_source_errors(connection, record)
            if source_errors:
                raise RuntimeError(
                    f"Stored forecast {record.forecast_id} failed source-chain validation: "
                    + " ".join(source_errors)
                )
        return record

    def create_forecast_record(self, forecast: ForecastRecord) -> dict[str, Any]:
        """Append one immutable forecast after validating every source reference."""

        if not isinstance(forecast, ForecastRecord):
            raise TypeError("forecast must be a ForecastRecord.")
        errors = validate_forecast_record(forecast)
        if errors:
            raise ValueError("Invalid ForecastRecord: " + " ".join(errors))
        record_json = canonical_forecast_json(forecast.to_dict())
        with self._immediate_connection() as connection:
            source_errors = self._forecast_source_errors(connection, forecast)
            if source_errors:
                raise ValueError(" ".join(source_errors))
            schema_row = connection.execute(
                "SELECT content_hash FROM forecast_schema_versions WHERE schema_version = ?",
                (forecast.schema_version,),
            ).fetchone()
            if schema_row is None or schema_row["content_hash"] != forecast_schema_definition_hash():
                raise ValueError("Forecast schema registration is missing or incompatible.")
            existing = connection.execute(
                "SELECT * FROM forecast_records WHERE forecast_id = ?",
                (forecast.forecast_id,),
            ).fetchone()
            if existing is not None:
                stored = self._forecast_record_from_row(existing, connection)
                if stored.content_hash != forecast.content_hash:
                    raise ValueError(
                        "Forecast ID already exists with different immutable content."
                    )
                return {
                    "forecast_id": stored.forecast_id,
                    "content_hash": stored.content_hash,
                    "inserted": False,
                }
            if forecast.supersedes_forecast_id is None:
                if forecast.forecast_version != 1:
                    raise ValueError(
                        "A forecast without a superseded predecessor must have version 1."
                    )
            else:
                predecessor_row = connection.execute(
                    "SELECT * FROM forecast_records WHERE forecast_id = ?",
                    (forecast.supersedes_forecast_id,),
                ).fetchone()
                if predecessor_row is None:
                    raise KeyError(
                        f"Superseded forecast {forecast.supersedes_forecast_id} does not exist."
                    )
                predecessor = self._forecast_record_from_row(
                    predecessor_row, connection
                )
                if forecast.forecast_version != predecessor.forecast_version + 1:
                    raise ValueError(
                        "A superseding forecast version must increment its predecessor by one."
                    )
                if forecast.symbol.casefold() != predecessor.symbol.casefold():
                    raise ValueError("A forecast may supersede only the same symbol lineage.")
                already_superseded = connection.execute(
                    "SELECT forecast_id FROM forecast_records WHERE supersedes_forecast_id = ?",
                    (forecast.supersedes_forecast_id,),
                ).fetchone()
                if already_superseded is not None:
                    raise ValueError(
                        "The predecessor already has an explicit superseding record."
                    )
            connection.execute(
                """
                INSERT INTO forecast_records(
                    forecast_id, forecast_version, supersedes_forecast_id,
                    source_analysis_run_id, source_degree_resolution_id,
                    schema_version, calculation_version, policy_version,
                    evaluation_eligibility, record_state, symbol,
                    analysis_cutoff_utc, content_hash, record_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    forecast.forecast_id,
                    forecast.forecast_version,
                    forecast.supersedes_forecast_id,
                    forecast.source_analysis_run_id,
                    forecast.source_degree_resolution_id,
                    forecast.schema_version,
                    forecast.calculation_version,
                    forecast.policy_version,
                    forecast.evaluation_eligibility.status.value,
                    forecast.evaluation_eligibility.record_state.value,
                    forecast.symbol,
                    forecast.analysis_cutoff_utc,
                    forecast.content_hash,
                    record_json,
                    forecast.created_at_utc,
                ),
            )
        return {
            "forecast_id": forecast.forecast_id,
            "content_hash": forecast.content_hash,
            "inserted": True,
        }

    def get_forecast_record(self, forecast_id: str) -> ForecastRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM forecast_records WHERE forecast_id = ?",
                (forecast_id,),
            ).fetchone()
            return (
                self._forecast_record_from_row(row, connection)
                if row is not None
                else None
            )

    def list_forecast_records(
        self,
        *,
        symbol: str | None = None,
        source_analysis_run_id: int | None = None,
        source_degree_resolution_id: int | None = None,
        include_superseded: bool = True,
        limit: int = 100,
    ) -> list[ForecastRecord]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if symbol is not None:
            conditions.append("f.symbol = ? COLLATE NOCASE")
            parameters.append(symbol)
        if source_analysis_run_id is not None:
            conditions.append("f.source_analysis_run_id = ?")
            parameters.append(source_analysis_run_id)
        if source_degree_resolution_id is not None:
            conditions.append("f.source_degree_resolution_id = ?")
            parameters.append(source_degree_resolution_id)
        if not include_superseded:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM forecast_records newer "
                "WHERE newer.supersedes_forecast_id = f.forecast_id)"
            )
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT f.* FROM forecast_records f"
                + where
                + " ORDER BY f.analysis_cutoff_utc DESC, f.forecast_version DESC, "
                "f.forecast_id ASC LIMIT ?",
                parameters,
            ).fetchall()
            return [
                self._forecast_record_from_row(row, connection) for row in rows
            ]

    @staticmethod
    def _forecast_observation_set_from_row(
        row: sqlite3.Row, connection: sqlite3.Connection | None = None
    ) -> ForecastObservationSet:
        try:
            payload = json.loads(str(row["record_json"]))
            record = ForecastObservationSet.from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Stored observation set {row['observation_set_id']} failed immutable validation."
            ) from exc
        mirrored_fields = {
            "observation_set_id": record.observation_set_id,
            "observation_set_version": record.observation_set_version,
            "supersedes_observation_set_id": record.supersedes_observation_set_id,
            "forecast_id": record.forecast_id,
            "source_dataset_id": record.source_dataset_id,
            "schema_version": record.schema_version,
            "calculation_version": record.calculation_version,
            "policy_version": record.policy_version,
            "symbol": record.symbol,
            "timeframe": record.timeframe,
            "actual_evaluation_cutoff_utc": record.actual_evaluation_cutoff_utc,
            "horizon_end_utc": record.horizon_end_utc,
            "candle_manifest_hash": record.candle_manifest_hash,
            "policy_hash": record.policy_hash,
            "content_hash": record.content_hash,
            "created_at_utc": record.created_at_utc,
        }
        for field_name, expected in mirrored_fields.items():
            if row[field_name] != expected:
                raise RuntimeError(
                    f"Stored observation set {record.observation_set_id} has a mismatched {field_name}."
                )
        if connection is not None:
            forecast_row = connection.execute(
                "SELECT * FROM forecast_records WHERE forecast_id = ?",
                (record.forecast_id,),
            ).fetchone()
            if forecast_row is None:
                raise RuntimeError("Stored observation set has no forecast source.")
            forecast = KnowledgeStore._forecast_record_from_row(
                forecast_row, connection
            )
            errors = validate_forecast_observation_set(record, forecast)
            if errors:
                raise RuntimeError(
                    f"Stored observation set {record.observation_set_id} failed source validation: "
                    + " ".join(errors)
                )
        return record

    def create_forecast_observation_set(
        self, observation_set: ForecastObservationSet
    ) -> dict[str, Any]:
        """Append one immutable post-cutoff candle observation set."""

        if not isinstance(observation_set, ForecastObservationSet):
            raise TypeError("observation_set must be a ForecastObservationSet.")
        record_json = canonical_forecast_json(observation_set.to_dict())
        with self._immediate_connection() as connection:
            forecast_row = connection.execute(
                "SELECT * FROM forecast_records WHERE forecast_id = ?",
                (observation_set.forecast_id,),
            ).fetchone()
            if forecast_row is None:
                raise KeyError(
                    f"Forecast {observation_set.forecast_id} does not exist."
                )
            forecast = self._forecast_record_from_row(forecast_row, connection)
            errors = validate_forecast_observation_set(observation_set, forecast)
            if errors:
                raise ValueError("Invalid ForecastObservationSet: " + " ".join(errors))
            existing = connection.execute(
                "SELECT * FROM forecast_observation_sets WHERE observation_set_id = ?",
                (observation_set.observation_set_id,),
            ).fetchone()
            if existing is not None:
                stored = self._forecast_observation_set_from_row(existing, connection)
                if stored.content_hash != observation_set.content_hash:
                    raise ValueError(
                        "Observation-set ID already exists with different immutable content."
                    )
                return {
                    "observation_set_id": stored.observation_set_id,
                    "content_hash": stored.content_hash,
                    "inserted": False,
                }
            if observation_set.supersedes_observation_set_id is None:
                if observation_set.observation_set_version != 1:
                    raise ValueError(
                        "An observation set without a predecessor must have version 1."
                    )
            else:
                predecessor_row = connection.execute(
                    "SELECT * FROM forecast_observation_sets WHERE observation_set_id = ?",
                    (observation_set.supersedes_observation_set_id,),
                ).fetchone()
                if predecessor_row is None:
                    raise KeyError(
                        "Superseded observation set "
                        f"{observation_set.supersedes_observation_set_id} does not exist."
                    )
                predecessor = self._forecast_observation_set_from_row(
                    predecessor_row, connection
                )
                if (
                    observation_set.observation_set_version
                    != predecessor.observation_set_version + 1
                ):
                    raise ValueError(
                        "A superseding observation version must increment its predecessor by one."
                    )
                if observation_set.forecast_id != predecessor.forecast_id:
                    raise ValueError(
                        "An observation set may supersede only the same forecast lineage."
                    )
                if observation_set.source_dataset_id != predecessor.source_dataset_id:
                    raise ValueError(
                        "An observation set may supersede only the same source-dataset lineage."
                    )
                if (
                    observation_set.horizon_window_ids
                    != predecessor.horizon_window_ids
                    or observation_set.horizon_end_utc != predecessor.horizon_end_utc
                ):
                    raise ValueError(
                        "Observation supersession cannot change the predetermined horizon."
                    )
                if (
                    observation_set.actual_evaluation_cutoff_utc
                    < predecessor.actual_evaluation_cutoff_utc
                ):
                    raise ValueError(
                        "Observation supersession cannot move the evaluation cutoff backward."
                    )
                already_superseded = connection.execute(
                    "SELECT observation_set_id FROM forecast_observation_sets "
                    "WHERE supersedes_observation_set_id = ?",
                    (observation_set.supersedes_observation_set_id,),
                ).fetchone()
                if already_superseded is not None:
                    raise ValueError(
                        "The predecessor already has an explicit superseding observation set."
                    )
            connection.execute(
                """
                INSERT INTO forecast_observation_sets(
                    observation_set_id, observation_set_version,
                    supersedes_observation_set_id, forecast_id, source_dataset_id,
                    schema_version, calculation_version, policy_version,
                    symbol, timeframe, actual_evaluation_cutoff_utc,
                    horizon_end_utc, candle_manifest_hash, policy_hash,
                    content_hash, record_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_set.observation_set_id,
                    observation_set.observation_set_version,
                    observation_set.supersedes_observation_set_id,
                    observation_set.forecast_id,
                    observation_set.source_dataset_id,
                    observation_set.schema_version,
                    observation_set.calculation_version,
                    observation_set.policy_version,
                    observation_set.symbol,
                    observation_set.timeframe,
                    observation_set.actual_evaluation_cutoff_utc,
                    observation_set.horizon_end_utc,
                    observation_set.candle_manifest_hash,
                    observation_set.policy_hash,
                    observation_set.content_hash,
                    record_json,
                    observation_set.created_at_utc,
                ),
            )
        return {
            "observation_set_id": observation_set.observation_set_id,
            "content_hash": observation_set.content_hash,
            "inserted": True,
        }

    def get_forecast_observation_set(
        self, observation_set_id: str
    ) -> ForecastObservationSet | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM forecast_observation_sets WHERE observation_set_id = ?",
                (observation_set_id,),
            ).fetchone()
            return (
                self._forecast_observation_set_from_row(row, connection)
                if row is not None
                else None
            )

    def list_forecast_observation_sets(
        self,
        *,
        forecast_id: str | None = None,
        symbol: str | None = None,
        source_dataset_id: str | None = None,
        include_superseded: bool = True,
        limit: int = 100,
    ) -> list[ForecastObservationSet]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if forecast_id is not None:
            conditions.append("o.forecast_id = ?")
            parameters.append(forecast_id)
        if symbol is not None:
            conditions.append("o.symbol = ? COLLATE NOCASE")
            parameters.append(symbol)
        if source_dataset_id is not None:
            conditions.append("o.source_dataset_id = ?")
            parameters.append(source_dataset_id)
        if not include_superseded:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM forecast_observation_sets newer "
                "WHERE newer.supersedes_observation_set_id = o.observation_set_id)"
            )
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT o.* FROM forecast_observation_sets o"
                + where
                + " ORDER BY o.actual_evaluation_cutoff_utc DESC, "
                "o.observation_set_version DESC, o.observation_set_id ASC LIMIT ?",
                parameters,
            ).fetchall()
            return [
                self._forecast_observation_set_from_row(row, connection)
                for row in rows
            ]

    @staticmethod
    def _forecast_outcome_evaluation_from_row(
        row: sqlite3.Row, connection: sqlite3.Connection | None = None
    ) -> ForecastOutcomeEvaluation:
        try:
            payload = json.loads(str(row["record_json"]))
            record = ForecastOutcomeEvaluation.from_dict(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Stored outcome evaluation {row['evaluation_id']} failed immutable validation."
            ) from exc
        mirrored_fields = {
            "evaluation_id": record.evaluation_id,
            "evaluation_version": record.evaluation_version,
            "supersedes_evaluation_id": record.supersedes_evaluation_id,
            "forecast_id": record.forecast_id,
            "observation_set_id": record.observation_set_id,
            "schema_version": record.schema_version,
            "calculation_version": record.calculation_version,
            "policy_version": record.policy_version,
            "outcome_status": record.outcome_status.value,
            "evaluated_through_utc": record.evaluated_through_utc,
            "evaluation_policy_hash": record.evaluation_policy_hash,
            "content_hash": record.content_hash,
            "created_at_utc": record.created_at_utc,
        }
        for field_name, expected in mirrored_fields.items():
            if row[field_name] != expected:
                raise RuntimeError(
                    f"Stored outcome evaluation {record.evaluation_id} has a mismatched {field_name}."
                )
        if connection is not None:
            forecast_row = connection.execute(
                "SELECT * FROM forecast_records WHERE forecast_id = ?",
                (record.forecast_id,),
            ).fetchone()
            observation_row = connection.execute(
                "SELECT * FROM forecast_observation_sets WHERE observation_set_id = ?",
                (record.observation_set_id,),
            ).fetchone()
            if forecast_row is None or observation_row is None:
                raise RuntimeError("Stored outcome evaluation has a missing source record.")
            forecast = KnowledgeStore._forecast_record_from_row(
                forecast_row, connection
            )
            observation = KnowledgeStore._forecast_observation_set_from_row(
                observation_row, connection
            )
            lower_sets: list[ForecastObservationSet] = []
            for item_id in record.lower_timeframe_observation_set_ids:
                lower_row = connection.execute(
                    "SELECT * FROM forecast_observation_sets WHERE observation_set_id = ?",
                    (item_id,),
                ).fetchone()
                if lower_row is None:
                    raise RuntimeError(
                        f"Stored outcome evaluation has missing lower observation {item_id}."
                    )
                lower_sets.append(
                    KnowledgeStore._forecast_observation_set_from_row(
                        lower_row, connection
                    )
                )
            errors = validate_forecast_outcome_evaluation(
                record,
                forecast=forecast,
                observation_set=observation,
                lower_timeframe_observation_sets=tuple(lower_sets),
            )
            if errors:
                raise RuntimeError(
                    f"Stored outcome evaluation {record.evaluation_id} failed source validation: "
                    + " ".join(errors)
                )
        return record

    def create_forecast_outcome_evaluation(
        self, evaluation: ForecastOutcomeEvaluation
    ) -> dict[str, Any]:
        """Append one deterministic outcome evaluation and preserve all sources."""

        if not isinstance(evaluation, ForecastOutcomeEvaluation):
            raise TypeError("evaluation must be a ForecastOutcomeEvaluation.")
        record_json = canonical_forecast_json(evaluation.to_dict())
        with self._immediate_connection() as connection:
            forecast_row = connection.execute(
                "SELECT * FROM forecast_records WHERE forecast_id = ?",
                (evaluation.forecast_id,),
            ).fetchone()
            observation_row = connection.execute(
                "SELECT * FROM forecast_observation_sets WHERE observation_set_id = ?",
                (evaluation.observation_set_id,),
            ).fetchone()
            if forecast_row is None:
                raise KeyError(f"Forecast {evaluation.forecast_id} does not exist.")
            if observation_row is None:
                raise KeyError(
                    f"Observation set {evaluation.observation_set_id} does not exist."
                )
            forecast = self._forecast_record_from_row(forecast_row, connection)
            observation = self._forecast_observation_set_from_row(
                observation_row, connection
            )
            lower_sets: list[ForecastObservationSet] = []
            for item_id in evaluation.lower_timeframe_observation_set_ids:
                lower_row = connection.execute(
                    "SELECT * FROM forecast_observation_sets WHERE observation_set_id = ?",
                    (item_id,),
                ).fetchone()
                if lower_row is None:
                    raise KeyError(
                        f"Lower-timeframe observation set {item_id} is not registered."
                    )
                lower_sets.append(
                    self._forecast_observation_set_from_row(lower_row, connection)
                )
            errors = validate_forecast_outcome_evaluation(
                evaluation,
                forecast=forecast,
                observation_set=observation,
                lower_timeframe_observation_sets=tuple(lower_sets),
            )
            if errors:
                raise ValueError(
                    "Invalid ForecastOutcomeEvaluation: " + " ".join(errors)
                )
            existing = connection.execute(
                "SELECT * FROM forecast_outcome_evaluations WHERE evaluation_id = ?",
                (evaluation.evaluation_id,),
            ).fetchone()
            if existing is not None:
                stored = self._forecast_outcome_evaluation_from_row(
                    existing, connection
                )
                if stored.content_hash != evaluation.content_hash:
                    raise ValueError(
                        "Evaluation ID already exists with different immutable content."
                    )
                return {
                    "evaluation_id": stored.evaluation_id,
                    "content_hash": stored.content_hash,
                    "inserted": False,
                }
            if evaluation.supersedes_evaluation_id is None:
                if evaluation.evaluation_version != 1:
                    raise ValueError(
                        "An evaluation without a predecessor must have version 1."
                    )
            else:
                predecessor_row = connection.execute(
                    "SELECT * FROM forecast_outcome_evaluations WHERE evaluation_id = ?",
                    (evaluation.supersedes_evaluation_id,),
                ).fetchone()
                if predecessor_row is None:
                    raise KeyError(
                        f"Superseded evaluation {evaluation.supersedes_evaluation_id} does not exist."
                    )
                predecessor = self._forecast_outcome_evaluation_from_row(
                    predecessor_row, connection
                )
                if evaluation.evaluation_version != predecessor.evaluation_version + 1:
                    raise ValueError(
                        "A superseding evaluation version must increment its predecessor by one."
                    )
                if evaluation.forecast_id != predecessor.forecast_id:
                    raise ValueError(
                        "An evaluation may supersede only the same forecast lineage."
                    )
                if evaluation.evaluated_through_utc < predecessor.evaluated_through_utc:
                    raise ValueError(
                        "Evaluation supersession cannot move the evaluated cutoff backward."
                    )
                already_superseded = connection.execute(
                    "SELECT evaluation_id FROM forecast_outcome_evaluations "
                    "WHERE supersedes_evaluation_id = ?",
                    (evaluation.supersedes_evaluation_id,),
                ).fetchone()
                if already_superseded is not None:
                    raise ValueError(
                        "The predecessor already has an explicit superseding evaluation."
                    )
            connection.execute(
                """
                INSERT INTO forecast_outcome_evaluations(
                    evaluation_id, evaluation_version, supersedes_evaluation_id,
                    forecast_id, observation_set_id, schema_version,
                    calculation_version, policy_version, outcome_status,
                    evaluated_through_utc, evaluation_policy_hash,
                    content_hash, record_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.evaluation_version,
                    evaluation.supersedes_evaluation_id,
                    evaluation.forecast_id,
                    evaluation.observation_set_id,
                    evaluation.schema_version,
                    evaluation.calculation_version,
                    evaluation.policy_version,
                    evaluation.outcome_status.value,
                    evaluation.evaluated_through_utc,
                    evaluation.evaluation_policy_hash,
                    evaluation.content_hash,
                    record_json,
                    evaluation.created_at_utc,
                ),
            )
        return {
            "evaluation_id": evaluation.evaluation_id,
            "content_hash": evaluation.content_hash,
            "inserted": True,
        }

    def get_forecast_outcome_evaluation(
        self, evaluation_id: str
    ) -> ForecastOutcomeEvaluation | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM forecast_outcome_evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            return (
                self._forecast_outcome_evaluation_from_row(row, connection)
                if row is not None
                else None
            )

    def list_forecast_outcome_evaluations(
        self,
        *,
        forecast_id: str | None = None,
        observation_set_id: str | None = None,
        outcome_status: str | None = None,
        include_superseded: bool = True,
        limit: int = 100,
    ) -> list[ForecastOutcomeEvaluation]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if forecast_id is not None:
            conditions.append("e.forecast_id = ?")
            parameters.append(forecast_id)
        if observation_set_id is not None:
            conditions.append("e.observation_set_id = ?")
            parameters.append(observation_set_id)
        if outcome_status is not None:
            conditions.append("e.outcome_status = ?")
            parameters.append(outcome_status)
        if not include_superseded:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM forecast_outcome_evaluations newer "
                "WHERE newer.supersedes_evaluation_id = e.evaluation_id)"
            )
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT e.* FROM forecast_outcome_evaluations e"
                + where
                + " ORDER BY e.evaluated_through_utc DESC, "
                "e.evaluation_version DESC, e.evaluation_id ASC LIMIT ?",
                parameters,
            ).fetchall()
            return [
                self._forecast_outcome_evaluation_from_row(row, connection)
                for row in rows
            ]

    @staticmethod
    def _forecast_outcome_review_from_row(
        row: sqlite3.Row, connection: sqlite3.Connection | None = None
    ) -> ForecastOutcomeReview:
        try:
            record = ForecastOutcomeReview.from_dict(json.loads(str(row["record_json"])))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Stored outcome review {row['review_id']} failed immutable validation."
            ) from exc
        mirrored = {
            "review_id": record.review_id,
            "review_version": record.review_version,
            "parent_review_id": record.parent_review_id,
            "evaluation_id": record.evaluation_id,
            "required_superseding_evaluation_id": record.required_superseding_evaluation_id,
            "forecast_id": record.forecast_id,
            "decision": record.decision.value,
            "reviewed_outcome_status": record.reviewed_outcome_status.value,
            "reviewer_reference": record.reviewer_reference,
            "reviewed_at_utc": record.reviewed_at_utc,
            "schema_version": record.schema_version,
            "policy_version": record.policy_version,
            "content_hash": record.content_hash,
        }
        for name, expected in mirrored.items():
            if row[name] != expected:
                raise RuntimeError(
                    f"Stored outcome review {record.review_id} has a mismatched {name}."
                )
        if connection is not None:
            evaluation_row = connection.execute(
                "SELECT * FROM forecast_outcome_evaluations WHERE evaluation_id = ?",
                (record.evaluation_id,),
            ).fetchone()
            if evaluation_row is None:
                raise RuntimeError("Stored outcome review has a missing evaluation.")
            evaluation = KnowledgeStore._forecast_outcome_evaluation_from_row(
                evaluation_row, connection
            )
            parent = None
            if record.parent_review_id:
                parent_row = connection.execute(
                    "SELECT * FROM forecast_outcome_reviews WHERE review_id = ?",
                    (record.parent_review_id,),
                ).fetchone()
                if parent_row is None:
                    raise RuntimeError("Stored outcome review has a missing parent.")
                parent = KnowledgeStore._forecast_outcome_review_from_row(parent_row)
            superseding = None
            if record.required_superseding_evaluation_id:
                superseding_row = connection.execute(
                    "SELECT * FROM forecast_outcome_evaluations WHERE evaluation_id = ?",
                    (record.required_superseding_evaluation_id,),
                ).fetchone()
                if superseding_row is None:
                    raise RuntimeError(
                        "Stored outcome review has a missing required superseding evaluation."
                    )
                superseding = KnowledgeStore._forecast_outcome_evaluation_from_row(
                    superseding_row, connection
                )
            errors = validate_forecast_outcome_review(
                record,
                evaluation=evaluation,
                parent_review=parent,
                superseding_evaluation=superseding,
            )
            if errors:
                raise RuntimeError(
                    f"Stored outcome review {record.review_id} failed source validation: "
                    + " ".join(errors)
                )
        return record

    def create_forecast_outcome_review(
        self, review: ForecastOutcomeReview
    ) -> dict[str, Any]:
        """Append one human outcome review without changing its evaluation."""

        if not isinstance(review, ForecastOutcomeReview):
            raise TypeError("review must be a ForecastOutcomeReview.")
        record_json = canonical_forecast_json(review.to_dict())
        with self._immediate_connection() as connection:
            evaluation_row = connection.execute(
                "SELECT * FROM forecast_outcome_evaluations WHERE evaluation_id = ?",
                (review.evaluation_id,),
            ).fetchone()
            if evaluation_row is None:
                raise KeyError(f"Evaluation {review.evaluation_id} does not exist.")
            evaluation = self._forecast_outcome_evaluation_from_row(
                evaluation_row, connection
            )
            parent = None
            if review.parent_review_id:
                parent_row = connection.execute(
                    "SELECT * FROM forecast_outcome_reviews WHERE review_id = ?",
                    (review.parent_review_id,),
                ).fetchone()
                if parent_row is None:
                    raise KeyError(
                        f"Parent review {review.parent_review_id} does not exist."
                    )
                parent = self._forecast_outcome_review_from_row(parent_row, connection)
                branch = connection.execute(
                    "SELECT review_id FROM forecast_outcome_reviews "
                    "WHERE parent_review_id = ? AND review_id <> ?",
                    (review.parent_review_id, review.review_id),
                ).fetchone()
                if branch is not None:
                    raise ValueError("The parent review already has an immutable revision.")
            superseding = None
            if review.required_superseding_evaluation_id:
                superseding_row = connection.execute(
                    "SELECT * FROM forecast_outcome_evaluations WHERE evaluation_id = ?",
                    (review.required_superseding_evaluation_id,),
                ).fetchone()
                if superseding_row is None:
                    raise KeyError(
                        f"Required evaluation {review.required_superseding_evaluation_id} does not exist."
                    )
                superseding = self._forecast_outcome_evaluation_from_row(
                    superseding_row, connection
                )
            errors = validate_forecast_outcome_review(
                review,
                evaluation=evaluation,
                parent_review=parent,
                superseding_evaluation=superseding,
            )
            if errors:
                raise ValueError("Invalid ForecastOutcomeReview: " + " ".join(errors))
            existing = connection.execute(
                "SELECT * FROM forecast_outcome_reviews WHERE review_id = ?",
                (review.review_id,),
            ).fetchone()
            if existing is not None:
                stored = self._forecast_outcome_review_from_row(existing, connection)
                if stored.content_hash != review.content_hash:
                    raise ValueError(
                        "Review ID already exists with different immutable content."
                    )
                return {
                    "review_id": stored.review_id,
                    "content_hash": stored.content_hash,
                    "inserted": False,
                }
            connection.execute(
                """
                INSERT INTO forecast_outcome_reviews(
                    review_id, review_version, parent_review_id, evaluation_id,
                    required_superseding_evaluation_id, forecast_id, decision,
                    reviewed_outcome_status, reviewer_reference, reviewed_at_utc,
                    schema_version, policy_version, content_hash, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review.review_id,
                    review.review_version,
                    review.parent_review_id,
                    review.evaluation_id,
                    review.required_superseding_evaluation_id,
                    review.forecast_id,
                    review.decision.value,
                    review.reviewed_outcome_status.value,
                    review.reviewer_reference,
                    review.reviewed_at_utc,
                    review.schema_version,
                    review.policy_version,
                    review.content_hash,
                    record_json,
                ),
            )
        return {
            "review_id": review.review_id,
            "content_hash": review.content_hash,
            "inserted": True,
        }

    def get_forecast_outcome_review(
        self, review_id: str
    ) -> ForecastOutcomeReview | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM forecast_outcome_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            return (
                self._forecast_outcome_review_from_row(row, connection)
                if row is not None
                else None
            )

    def list_forecast_outcome_reviews(
        self,
        *,
        evaluation_id: str | None = None,
        forecast_id: str | None = None,
        decision: str | None = None,
        include_superseded: bool = True,
        limit: int = 100,
    ) -> list[ForecastOutcomeReview]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if evaluation_id is not None:
            conditions.append("r.evaluation_id = ?")
            parameters.append(evaluation_id)
        if forecast_id is not None:
            conditions.append("r.forecast_id = ?")
            parameters.append(forecast_id)
        if decision is not None:
            conditions.append("r.decision = ?")
            parameters.append(decision)
        if not include_superseded:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM forecast_outcome_reviews newer "
                "WHERE newer.parent_review_id = r.review_id)"
            )
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT r.* FROM forecast_outcome_reviews r"
                + where
                + " ORDER BY r.reviewed_at_utc DESC, r.review_version DESC, "
                "r.review_id ASC LIMIT ?",
                parameters,
            ).fetchall()
            return [
                self._forecast_outcome_review_from_row(row, connection)
                for row in rows
            ]

    @staticmethod
    def _mistake_memory_lesson_from_row(row: sqlite3.Row) -> MistakeMemoryLesson:
        try:
            record = MistakeMemoryLesson.from_dict(json.loads(str(row["record_json"])))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Stored mistake-memory lesson {row['lesson_id']} failed immutable validation."
            ) from exc
        mirrored = {
            "lesson_id": record.lesson_id,
            "lesson_version": record.lesson_version,
            "supersedes_lesson_id": record.supersedes_lesson_id,
            "scope_type": record.scope.scope_type.value,
            "initial_status": record.initial_status.value,
            "proposed_by": record.proposed_by,
            "proposed_at_utc": record.proposed_at_utc,
            "deduplication_key": record.deduplication_key,
            "schema_version": record.schema_version,
            "policy_version": record.policy_version,
            "content_hash": record.content_hash,
        }
        for name, expected in mirrored.items():
            if row[name] != expected:
                raise RuntimeError(
                    f"Stored mistake-memory lesson {record.lesson_id} has a mismatched {name}."
                )
        return record

    def create_mistake_memory_lesson(
        self, lesson: MistakeMemoryLesson
    ) -> dict[str, Any]:
        if not isinstance(lesson, MistakeMemoryLesson):
            raise TypeError("lesson must be a MistakeMemoryLesson.")
        if lesson.content_hash != mistake_memory_lesson_content_hash(lesson):
            raise ValueError("MistakeMemoryLesson content_hash does not match.")
        record_json = canonical_forecast_json(lesson.to_dict())
        with self._immediate_connection() as connection:
            existing = connection.execute(
                "SELECT * FROM mistake_memory_lessons WHERE lesson_id = ?",
                (lesson.lesson_id,),
            ).fetchone()
            if existing is not None:
                stored = self._mistake_memory_lesson_from_row(existing)
                if stored.content_hash != lesson.content_hash:
                    raise ValueError(
                        "Lesson ID already exists with different immutable content."
                    )
                return {
                    "lesson_id": stored.lesson_id,
                    "content_hash": stored.content_hash,
                    "inserted": False,
                }
            if lesson.supersedes_lesson_id is None:
                if lesson.lesson_version != 1:
                    raise ValueError("A lesson without a predecessor must have version 1.")
            else:
                predecessor_row = connection.execute(
                    "SELECT * FROM mistake_memory_lessons WHERE lesson_id = ?",
                    (lesson.supersedes_lesson_id,),
                ).fetchone()
                if predecessor_row is None:
                    raise KeyError(
                        f"Superseded lesson {lesson.supersedes_lesson_id} does not exist."
                    )
                predecessor = self._mistake_memory_lesson_from_row(predecessor_row)
                if lesson.lesson_version != predecessor.lesson_version + 1:
                    raise ValueError(
                        "A replacement lesson version must increment its predecessor by one."
                    )
                branch = connection.execute(
                    "SELECT lesson_id FROM mistake_memory_lessons WHERE supersedes_lesson_id = ?",
                    (lesson.supersedes_lesson_id,),
                ).fetchone()
                if branch is not None:
                    raise ValueError("The lesson already has an explicit replacement.")
            connection.execute(
                """
                INSERT INTO mistake_memory_lessons(
                    lesson_id, lesson_version, supersedes_lesson_id, scope_type,
                    initial_status, proposed_by, proposed_at_utc,
                    deduplication_key, schema_version, policy_version,
                    content_hash, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lesson.lesson_id,
                    lesson.lesson_version,
                    lesson.supersedes_lesson_id,
                    lesson.scope.scope_type.value,
                    lesson.initial_status.value,
                    lesson.proposed_by,
                    lesson.proposed_at_utc,
                    lesson.deduplication_key,
                    lesson.schema_version,
                    lesson.policy_version,
                    lesson.content_hash,
                    record_json,
                ),
            )
        return {
            "lesson_id": lesson.lesson_id,
            "content_hash": lesson.content_hash,
            "inserted": True,
        }

    def get_mistake_memory_lesson(
        self, lesson_id: str
    ) -> MistakeMemoryLesson | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mistake_memory_lessons WHERE lesson_id = ?",
                (lesson_id,),
            ).fetchone()
            return self._mistake_memory_lesson_from_row(row) if row is not None else None

    def list_mistake_memory_lessons(
        self,
        *,
        scope_type: str | None = None,
        deduplication_key: str | None = None,
        include_superseded: bool = True,
        limit: int = 100,
    ) -> list[MistakeMemoryLesson]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if scope_type is not None:
            conditions.append("l.scope_type = ?")
            parameters.append(scope_type)
        if deduplication_key is not None:
            conditions.append("l.deduplication_key = ?")
            parameters.append(deduplication_key)
        if not include_superseded:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM mistake_memory_lessons newer "
                "WHERE newer.supersedes_lesson_id = l.lesson_id)"
            )
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT l.* FROM mistake_memory_lessons l"
                + where
                + " ORDER BY l.proposed_at_utc DESC, l.lesson_version DESC, "
                "l.lesson_id ASC LIMIT ?",
                parameters,
            ).fetchall()
            return [self._mistake_memory_lesson_from_row(row) for row in rows]

    @staticmethod
    def _mistake_memory_source_from_row(row: sqlite3.Row) -> LessonSource:
        try:
            record = LessonSource.from_dict(json.loads(str(row["record_json"])))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Stored mistake-memory source {row['source_id']} failed immutable validation."
            ) from exc
        mirrored = {
            "source_id": record.source_id,
            "lesson_id": record.lesson_id,
            "review_id": record.review_id,
            "evaluation_id": record.evaluation_id,
            "forecast_id": record.forecast_id,
            "source_role": record.source_role.value,
            "symbol": record.symbol,
            "added_by": record.added_by,
            "added_at_utc": record.added_at_utc,
            "schema_version": record.schema_version,
            "policy_version": record.policy_version,
            "content_hash": record.content_hash,
        }
        for name, expected in mirrored.items():
            if row[name] != expected:
                raise RuntimeError(
                    f"Stored mistake-memory source {record.source_id} has a mismatched {name}."
                )
        return record

    def create_mistake_memory_source(
        self, source: LessonSource
    ) -> dict[str, Any]:
        if not isinstance(source, LessonSource):
            raise TypeError("source must be a LessonSource.")
        if source.content_hash != lesson_source_content_hash(source):
            raise ValueError("LessonSource content_hash does not match.")
        record_json = canonical_forecast_json(source.to_dict())
        with self._immediate_connection() as connection:
            lesson_row = connection.execute(
                "SELECT * FROM mistake_memory_lessons WHERE lesson_id = ?",
                (source.lesson_id,),
            ).fetchone()
            review_row = connection.execute(
                "SELECT * FROM forecast_outcome_reviews WHERE review_id = ?",
                (source.review_id,),
            ).fetchone()
            evaluation_row = connection.execute(
                "SELECT * FROM forecast_outcome_evaluations WHERE evaluation_id = ?",
                (source.evaluation_id,),
            ).fetchone()
            forecast_row = connection.execute(
                "SELECT * FROM forecast_records WHERE forecast_id = ?",
                (source.forecast_id,),
            ).fetchone()
            if None in (lesson_row, review_row, evaluation_row, forecast_row):
                raise KeyError("Lesson source has an unregistered immutable reference.")
            lesson = self._mistake_memory_lesson_from_row(lesson_row)
            review = self._forecast_outcome_review_from_row(review_row, connection)
            evaluation = self._forecast_outcome_evaluation_from_row(
                evaluation_row, connection
            )
            forecast = self._forecast_record_from_row(forecast_row, connection)
            if source.lesson_content_hash != lesson.content_hash:
                raise ValueError("Lesson source references a different lesson hash.")
            if (
                source.source_id in lesson.supporting_source_ids
                and source.source_role.value != "supporting"
            ) or (
                source.source_id in lesson.counterexample_source_ids
                and source.source_role.value != "counterexample"
            ):
                raise ValueError("An initial lesson source has a mismatched source role.")
            if source.review_content_hash != review.content_hash:
                raise ValueError("Lesson source references a different review hash.")
            if source.evaluation_content_hash != evaluation.content_hash:
                raise ValueError("Lesson source references a different evaluation hash.")
            if source.forecast_content_hash != forecast.content_hash:
                raise ValueError("Lesson source references a different forecast hash.")
            if review.evaluation_id != source.evaluation_id or review.forecast_id != source.forecast_id:
                raise ValueError("Lesson source review lineage is inconsistent.")
            if forecast.symbol.casefold() != source.symbol.casefold():
                raise ValueError("Lesson source symbol differs from the immutable forecast.")
            if review.decision.value not in {"approved", "revised"}:
                raise ValueError("Lesson sources require an approved or revised review.")
            if source.source_role.value == "supporting":
                if review.deterministic_scoring_error or review.reviewed_outcome_status.value not in {"failed", "partial"}:
                    raise ValueError(
                        "A supporting lesson source requires an approved failed or partial deterministic outcome."
                    )
            existing = connection.execute(
                "SELECT * FROM mistake_memory_sources WHERE source_id = ?",
                (source.source_id,),
            ).fetchone()
            if existing is not None:
                stored = self._mistake_memory_source_from_row(existing)
                if stored.content_hash != source.content_hash:
                    raise ValueError(
                        "Source ID already exists with different immutable content."
                    )
                return {
                    "source_id": stored.source_id,
                    "content_hash": stored.content_hash,
                    "inserted": False,
                }
            connection.execute(
                """
                INSERT INTO mistake_memory_sources(
                    source_id, lesson_id, review_id, evaluation_id, forecast_id,
                    source_role, symbol, added_by, added_at_utc, schema_version,
                    policy_version, content_hash, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.source_id,
                    source.lesson_id,
                    source.review_id,
                    source.evaluation_id,
                    source.forecast_id,
                    source.source_role.value,
                    source.symbol,
                    source.added_by,
                    source.added_at_utc,
                    source.schema_version,
                    source.policy_version,
                    source.content_hash,
                    record_json,
                ),
            )
        return {
            "source_id": source.source_id,
            "content_hash": source.content_hash,
            "inserted": True,
        }

    def get_mistake_memory_source(self, source_id: str) -> LessonSource | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mistake_memory_sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            return self._mistake_memory_source_from_row(row) if row is not None else None

    def list_mistake_memory_sources(
        self,
        *,
        lesson_id: str | None = None,
        review_id: str | None = None,
        source_role: str | None = None,
        limit: int = 100,
    ) -> list[LessonSource]:
        conditions: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("lesson_id", lesson_id),
            ("review_id", review_id),
            ("source_role", source_role),
        ):
            if value is not None:
                conditions.append(f"s.{column} = ?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT s.* FROM mistake_memory_sources s"
                + where
                + " ORDER BY s.added_at_utc ASC, s.source_id ASC LIMIT ?",
                parameters,
            ).fetchall()
            return [self._mistake_memory_source_from_row(row) for row in rows]

    @staticmethod
    def _mistake_memory_event_from_row(row: sqlite3.Row) -> LessonEvent:
        try:
            record = LessonEvent.from_dict(json.loads(str(row["record_json"])))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Stored mistake-memory event {row['event_id']} failed immutable validation."
            ) from exc
        mirrored = {
            "event_id": record.event_id,
            "lesson_id": record.lesson_id,
            "sequence_number": record.sequence_number,
            "parent_event_id": record.parent_event_id,
            "related_lesson_id": record.related_lesson_id,
            "from_status": record.from_status.value if record.from_status else None,
            "to_status": record.to_status.value,
            "actor_reference": record.actor_reference,
            "recorded_at_utc": record.recorded_at_utc,
            "reason": record.reason,
            "schema_version": record.schema_version,
            "policy_version": record.policy_version,
            "content_hash": record.content_hash,
        }
        for name, expected in mirrored.items():
            if row[name] != expected:
                raise RuntimeError(
                    f"Stored mistake-memory event {record.event_id} has a mismatched {name}."
                )
        return record

    def create_mistake_memory_event(self, event: LessonEvent) -> dict[str, Any]:
        if not isinstance(event, LessonEvent):
            raise TypeError("event must be a LessonEvent.")
        if event.content_hash != lesson_event_content_hash(event):
            raise ValueError("LessonEvent content_hash does not match.")
        record_json = canonical_forecast_json(event.to_dict())
        with self._immediate_connection() as connection:
            lesson_row = connection.execute(
                "SELECT * FROM mistake_memory_lessons WHERE lesson_id = ?",
                (event.lesson_id,),
            ).fetchone()
            if lesson_row is None:
                raise KeyError(f"Lesson {event.lesson_id} does not exist.")
            lesson = self._mistake_memory_lesson_from_row(lesson_row)
            existing = connection.execute(
                "SELECT * FROM mistake_memory_events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                stored = self._mistake_memory_event_from_row(existing)
                if stored.content_hash != event.content_hash:
                    raise ValueError(
                        "Event ID already exists with different immutable content."
                    )
                return {
                    "event_id": stored.event_id,
                    "content_hash": stored.content_hash,
                    "inserted": False,
                }
            existing_rows = connection.execute(
                "SELECT * FROM mistake_memory_events WHERE lesson_id = ? "
                "ORDER BY sequence_number ASC, event_id ASC",
                (event.lesson_id,),
            ).fetchall()
            existing_events = tuple(
                self._mistake_memory_event_from_row(row) for row in existing_rows
            )
            source_rows = []
            for source_id in event.source_ids_considered:
                source_row = connection.execute(
                    "SELECT * FROM mistake_memory_sources WHERE source_id = ?",
                    (source_id,),
                ).fetchone()
                if source_row is None:
                    raise KeyError(f"Lesson source {source_id} does not exist.")
                source_rows.append(source_row)
            sources = tuple(
                self._mistake_memory_source_from_row(row) for row in source_rows
            )
            if any(
                source.lesson_id != lesson.lesson_id
                or source.lesson_content_hash != lesson.content_hash
                for source in sources
            ):
                raise ValueError("A lesson event contains a source from another lesson.")
            available_rows = connection.execute(
                "SELECT * FROM mistake_memory_sources WHERE lesson_id = ? "
                "AND added_at_utc <= ? ORDER BY source_id ASC",
                (event.lesson_id, event.recorded_at_utc),
            ).fetchall()
            available_source_ids = {
                self._mistake_memory_source_from_row(row).source_id
                for row in available_rows
            }
            if available_source_ids != set(event.source_ids_considered):
                raise ValueError(
                    "A lesson event must consider every lesson source available at its timestamp."
                )
            if event.sequence_number == 1:
                errors = validate_lesson_event_chain(lesson, (event,))
                if errors:
                    raise ValueError("Invalid initial lesson event: " + " ".join(errors))
                if existing_events:
                    raise ValueError("The lesson already has an initial event.")
                if event.recorded_at_utc != lesson.proposed_at_utc:
                    raise ValueError("The initial event must share the proposal timestamp.")
                expected_initial_sources = set(lesson.supporting_source_ids) | set(
                    lesson.counterexample_source_ids
                )
                if expected_initial_sources != set(event.source_ids_considered):
                    raise ValueError(
                        "The initial event must contain every source named by the lesson proposal."
                    )
            else:
                if not existing_events:
                    raise ValueError("A later lesson event requires an existing chain.")
                related_lesson = None
                if event.related_lesson_id:
                    related_row = connection.execute(
                        "SELECT * FROM mistake_memory_lessons WHERE lesson_id = ?",
                        (event.related_lesson_id,),
                    ).fetchone()
                    if related_row is None:
                        raise KeyError(
                            f"Related lesson {event.related_lesson_id} does not exist."
                        )
                    related_lesson = self._mistake_memory_lesson_from_row(related_row)
                expected = build_lesson_event(
                    lesson,
                    existing_events,
                    sources,
                    to_status=event.to_status,
                    actor_reference=event.actor_reference,
                    recorded_at_utc=event.recorded_at_utc,
                    reason=event.reason,
                    related_lesson=related_lesson,
                    human_exception_reason=event.human_exception_reason,
                )
                if expected != event:
                    raise ValueError(
                        "Lesson event differs from the deterministic transition result."
                    )
            connection.execute(
                """
                INSERT INTO mistake_memory_events(
                    event_id, lesson_id, sequence_number, parent_event_id,
                    related_lesson_id, from_status, to_status, actor_reference,
                    recorded_at_utc, reason, schema_version, policy_version,
                    content_hash, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.lesson_id,
                    event.sequence_number,
                    event.parent_event_id,
                    event.related_lesson_id,
                    event.from_status.value if event.from_status else None,
                    event.to_status.value,
                    event.actor_reference,
                    event.recorded_at_utc,
                    event.reason,
                    event.schema_version,
                    event.policy_version,
                    event.content_hash,
                    record_json,
                ),
            )
        return {
            "event_id": event.event_id,
            "content_hash": event.content_hash,
            "inserted": True,
        }

    def get_mistake_memory_event(self, event_id: str) -> LessonEvent | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM mistake_memory_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return self._mistake_memory_event_from_row(row) if row is not None else None

    def list_mistake_memory_events(
        self,
        *,
        lesson_id: str | None = None,
        to_status: str | None = None,
        limit: int = 1000,
    ) -> list[LessonEvent]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if lesson_id is not None:
            conditions.append("e.lesson_id = ?")
            parameters.append(lesson_id)
        if to_status is not None:
            conditions.append("e.to_status = ?")
            parameters.append(to_status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT e.* FROM mistake_memory_events e"
                + where
                + " ORDER BY e.lesson_id ASC, e.sequence_number ASC, e.event_id ASC LIMIT ?",
                parameters,
            ).fetchall()
            result = [self._mistake_memory_event_from_row(row) for row in rows]
            if lesson_id is not None:
                lesson_row = connection.execute(
                    "SELECT * FROM mistake_memory_lessons WHERE lesson_id = ?",
                    (lesson_id,),
                ).fetchone()
                if lesson_row is None:
                    raise KeyError(f"Lesson {lesson_id} does not exist.")
                errors = validate_lesson_event_chain(
                    self._mistake_memory_lesson_from_row(lesson_row), result
                )
                if errors:
                    raise RuntimeError("Stored lesson event chain is invalid: " + " ".join(errors))
            return result

    @staticmethod
    def _analysis_run_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in (
            "request_json",
            "evidence_json",
            "response_json",
            "validation_json",
        ):
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        return result

    @staticmethod
    def _forecast_agent_orchestration_from_row(
        row: sqlite3.Row,
    ) -> TechnicalAgentOrchestration:
        try:
            record = TechnicalAgentOrchestration.from_dict(
                json.loads(str(row["record_json"]))
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Stored forecast-agent orchestration {row['orchestration_id']} "
                "failed immutable validation."
            ) from exc
        retrieved_lesson_ids = sorted(
            {item.lesson_id for item in record.retrieved_lessons}
        )
        selected_candidate_ids = list(
            record.shadow_resolution.ranked_valid_candidate_ids
        )
        mirrored: dict[str, Any] = {
            "orchestration_id": record.orchestration_id,
            "orchestration_version": record.orchestration_version,
            "supersedes_orchestration_id": record.supersedes_orchestration_id,
            "source_analysis_run_id": record.request.source_analysis_run_id,
            "source_degree_resolution_id": (
                record.request.source_degree_resolution_id
            ),
            "request_id": record.request.request_id,
            "analysis_cutoff_utc": record.request.analysis_cutoff_utc,
            "decision_time_input_hash": record.request.decision_time_input_hash,
            "primary_result_hash": record.primary_result.content_hash,
            "alternative_result_hash": record.alternative_result.content_hash,
            "auditor_result_hash": record.auditor_result.content_hash,
            "final_result_hash": record.final_result.content_hash,
            "validation_manifest_hash": record.source_hashes[
                "validation_manifest"
            ],
            "shadow_resolution_hash": record.shadow_resolution.content_hash,
            "shadow_comparison_hash": record.shadow_comparison.content_hash,
            "retrieved_lesson_ids_json": canonical_forecast_json(
                retrieved_lesson_ids
            ),
            "selected_candidate_ids_json": canonical_forecast_json(
                selected_candidate_ids
            ),
            "status": record.status.value,
            "provider": record.provider,
            "model": record.model,
            "prompt_versions_json": canonical_forecast_json(
                record.prompt_versions
            ),
            "policy_version": record.policy_version,
            "schema_version": record.schema_version,
            "content_hash": record.content_hash,
            "created_at_utc": record.created_at_utc,
        }
        for name, expected in mirrored.items():
            if row[name] != expected:
                raise RuntimeError(
                    f"Stored forecast-agent orchestration "
                    f"{record.orchestration_id} has a mismatched {name}."
                )
        return record

    def create_forecast_agent_orchestration(
        self, orchestration: TechnicalAgentOrchestration
    ) -> dict[str, Any]:
        """Append one immutable Phase 11D1 shadow trace."""

        if not isinstance(orchestration, TechnicalAgentOrchestration):
            raise TypeError(
                "orchestration must be a TechnicalAgentOrchestration."
            )
        validation_errors = validate_technical_agent_orchestration(orchestration)
        if validation_errors:
            raise ValueError(
                "Invalid TechnicalAgentOrchestration: "
                + " ".join(validation_errors)
            )
        record_json = canonical_forecast_json(orchestration.to_dict())
        request = orchestration.request
        with self._immediate_connection() as connection:
            run_row = connection.execute(
                "SELECT * FROM analysis_runs WHERE id = ?",
                (request.source_analysis_run_id,),
            ).fetchone()
            resolution_row = connection.execute(
                "SELECT * FROM degree_resolutions WHERE id = ?",
                (request.source_degree_resolution_id,),
            ).fetchone()
            if run_row is None or resolution_row is None:
                raise KeyError(
                    "Forecast-agent orchestration has an unregistered source reference."
                )
            run = self._analysis_run_from_row(run_row)
            resolution = self._degree_resolution_row(resolution_row)
            if resolution["run_id"] != run["id"]:
                raise ValueError(
                    "Source degree resolution belongs to a different analysis run."
                )
            if run["symbol"].casefold() != request.symbol.casefold():
                raise ValueError(
                    "Forecast-agent request symbol differs from its source run."
                )
            if (
                stored_record_content_hash(run)
                != request.source_analysis_run_content_hash
            ):
                raise ValueError(
                    "Forecast-agent request references a different analysis-run hash."
                )
            if (
                stored_record_content_hash(resolution)
                != request.source_degree_resolution_content_hash
            ):
                raise ValueError(
                    "Forecast-agent request references a different degree-resolution hash."
                )
            existing = connection.execute(
                "SELECT * FROM forecast_agent_orchestrations "
                "WHERE orchestration_id = ?",
                (orchestration.orchestration_id,),
            ).fetchone()
            if existing is not None:
                stored = self._forecast_agent_orchestration_from_row(existing)
                if stored.content_hash != orchestration.content_hash:
                    raise ValueError(
                        "Orchestration ID already exists with different immutable content."
                    )
                return {
                    "orchestration_id": stored.orchestration_id,
                    "content_hash": stored.content_hash,
                    "inserted": False,
                }
            if orchestration.supersedes_orchestration_id is None:
                if orchestration.orchestration_version != 1:
                    raise ValueError(
                        "An orchestration without a predecessor must have version 1."
                    )
            else:
                predecessor_row = connection.execute(
                    "SELECT * FROM forecast_agent_orchestrations "
                    "WHERE orchestration_id = ?",
                    (orchestration.supersedes_orchestration_id,),
                ).fetchone()
                if predecessor_row is None:
                    raise KeyError(
                        "Superseded forecast-agent orchestration does not exist."
                    )
                predecessor = self._forecast_agent_orchestration_from_row(
                    predecessor_row
                )
                if (
                    orchestration.orchestration_version
                    != predecessor.orchestration_version + 1
                ):
                    raise ValueError(
                        "A replacement orchestration must increment its predecessor version by one."
                    )
                if (
                    request.source_analysis_run_id
                    != predecessor.request.source_analysis_run_id
                    or request.source_degree_resolution_id
                    != predecessor.request.source_degree_resolution_id
                    or request.symbol.casefold()
                    != predecessor.request.symbol.casefold()
                ):
                    raise ValueError(
                        "An orchestration replacement must remain in the same source lineage."
                    )
                branch = connection.execute(
                    "SELECT orchestration_id FROM forecast_agent_orchestrations "
                    "WHERE supersedes_orchestration_id = ?",
                    (orchestration.supersedes_orchestration_id,),
                ).fetchone()
                if branch is not None:
                    raise ValueError(
                        "The predecessor already has an explicit replacement."
                    )
            retrieved_lesson_ids = sorted(
                {item.lesson_id for item in orchestration.retrieved_lessons}
            )
            selected_candidate_ids = list(
                orchestration.shadow_resolution.ranked_valid_candidate_ids
            )
            connection.execute(
                """
                INSERT INTO forecast_agent_orchestrations(
                    orchestration_id, orchestration_version,
                    supersedes_orchestration_id, source_analysis_run_id,
                    source_degree_resolution_id, request_id,
                    analysis_cutoff_utc, decision_time_input_hash,
                    primary_result_hash, alternative_result_hash,
                    auditor_result_hash, final_result_hash,
                    validation_manifest_hash, shadow_resolution_hash,
                    shadow_comparison_hash, retrieved_lesson_ids_json,
                    selected_candidate_ids_json, status, provider, model,
                    prompt_versions_json, policy_version, schema_version,
                    content_hash, record_json, created_at_utc
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    orchestration.orchestration_id,
                    orchestration.orchestration_version,
                    orchestration.supersedes_orchestration_id,
                    request.source_analysis_run_id,
                    request.source_degree_resolution_id,
                    request.request_id,
                    request.analysis_cutoff_utc,
                    request.decision_time_input_hash,
                    orchestration.primary_result.content_hash,
                    orchestration.alternative_result.content_hash,
                    orchestration.auditor_result.content_hash,
                    orchestration.final_result.content_hash,
                    orchestration.source_hashes["validation_manifest"],
                    orchestration.shadow_resolution.content_hash,
                    orchestration.shadow_comparison.content_hash,
                    canonical_forecast_json(retrieved_lesson_ids),
                    canonical_forecast_json(selected_candidate_ids),
                    orchestration.status.value,
                    orchestration.provider,
                    orchestration.model,
                    canonical_forecast_json(orchestration.prompt_versions),
                    orchestration.policy_version,
                    orchestration.schema_version,
                    orchestration.content_hash,
                    record_json,
                    orchestration.created_at_utc,
                ),
            )
        return {
            "orchestration_id": orchestration.orchestration_id,
            "content_hash": orchestration.content_hash,
            "inserted": True,
        }

    def get_forecast_agent_orchestration(
        self, orchestration_id: str
    ) -> TechnicalAgentOrchestration | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM forecast_agent_orchestrations "
                "WHERE orchestration_id = ?",
                (orchestration_id,),
            ).fetchone()
            return (
                self._forecast_agent_orchestration_from_row(row)
                if row is not None
                else None
            )

    def list_forecast_agent_orchestrations(
        self,
        *,
        source_analysis_run_id: int | None = None,
        source_degree_resolution_id: int | None = None,
        status: str | None = None,
        include_superseded: bool = True,
        limit: int = 100,
    ) -> list[TechnicalAgentOrchestration]:
        conditions: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("source_analysis_run_id", source_analysis_run_id),
            ("source_degree_resolution_id", source_degree_resolution_id),
            ("status", status),
        ):
            if value is not None:
                conditions.append(f"o.{column} = ?")
                parameters.append(value)
        if not include_superseded:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM forecast_agent_orchestrations newer "
                "WHERE newer.supersedes_orchestration_id = o.orchestration_id)"
            )
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT o.* FROM forecast_agent_orchestrations o"
                + where
                + " ORDER BY o.created_at_utc DESC, "
                "o.orchestration_version DESC, o.orchestration_id ASC LIMIT ?",
                parameters,
            ).fetchall()
            return [
                self._forecast_agent_orchestration_from_row(row) for row in rows
            ]

    @staticmethod
    def _outcome_learning_orchestration_from_row(
        row: sqlite3.Row,
    ) -> OutcomeLearningOrchestration:
        try:
            record = OutcomeLearningOrchestration.from_dict(
                json.loads(str(row["record_json"]))
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Stored outcome-learning orchestration {row['orchestration_id']} "
                "failed immutable validation."
            ) from exc
        mirrored: dict[str, Any] = {
            "orchestration_id": record.orchestration_id,
            "orchestration_version": record.orchestration_version,
            "supersedes_orchestration_id": record.supersedes_orchestration_id,
            "orchestration_kind": record.orchestration_kind.value,
            "status": record.status.value,
            "human_action_required": int(record.human_action_required),
            "forecast_id": record.forecast_id,
            "observation_set_id": record.observation_set_id,
            "evaluation_id": record.evaluation_id,
            "outcome_review_id": record.outcome_review_id,
            "existing_lesson_id": record.existing_lesson_id,
            "request_id": record.request.request_id,
            "provider": record.provider,
            "model": record.model,
            "prompt_version": record.prompt_version,
            "policy_version": record.policy_version,
            "frozen_input_hash": record.request.frozen_input_hash,
            "structured_draft_hash": record.structured_draft_hash,
            "result_hash": record.result.content_hash,
            "warnings_json": canonical_forecast_json(list(record.warnings)),
            "validation_errors_json": canonical_forecast_json(
                list(record.validation_errors)
            ),
            "started_at_utc": record.started_at_utc,
            "completed_at_utc": record.completed_at_utc,
            "schema_version": record.schema_version,
            "calculation_version": record.calculation_version,
            "content_hash": record.content_hash,
        }
        for name, expected in mirrored.items():
            if row[name] != expected:
                raise RuntimeError(
                    f"Stored outcome-learning orchestration "
                    f"{record.orchestration_id} has a mismatched {name}."
                )
        validation_errors = validate_outcome_learning_orchestration(record)
        if validation_errors:
            raise RuntimeError(
                f"Stored outcome-learning orchestration {record.orchestration_id} "
                "failed replay validation: "
                + " ".join(validation_errors)
            )
        return record

    def create_forecast_outcome_learning_orchestration(
        self, orchestration: OutcomeLearningOrchestration
    ) -> dict[str, Any]:
        """Append one immutable Phase 11D2 shadow orchestration."""

        if not isinstance(orchestration, OutcomeLearningOrchestration):
            raise TypeError(
                "orchestration must be an OutcomeLearningOrchestration."
            )
        validation_errors = validate_outcome_learning_orchestration(orchestration)
        if validation_errors:
            raise ValueError(
                "Invalid OutcomeLearningOrchestration: "
                + " ".join(validation_errors)
            )
        record_json = canonical_forecast_json(orchestration.to_dict())
        request = orchestration.request

        def same_record(actual: Any, expected: Any, label: str) -> None:
            if actual.content_hash != expected.content_hash:
                raise ValueError(f"{label} hash differs from the stored immutable record.")

        with self._immediate_connection() as connection:
            forecasts: tuple[ForecastRecord, ...]
            evaluations: tuple[ForecastOutcomeEvaluation, ...]
            reviews: tuple[ForecastOutcomeReview, ...]
            lessons: tuple[MistakeMemoryLesson, ...]
            sources: tuple[LessonSource, ...]
            events: tuple[LessonEvent, ...]
            if isinstance(request, OutcomeReviewerRequest):
                forecasts = (request.forecast,)
                evaluations = (request.evaluation,)
                reviews = ()
                lessons = ()
                sources = ()
                events = ()
                observation_row = connection.execute(
                    "SELECT * FROM forecast_observation_sets "
                    "WHERE observation_set_id = ?",
                    (request.observation_set.observation_set_id,),
                ).fetchone()
                if observation_row is None:
                    raise KeyError(
                        "Outcome Reviewer observation set is not registered."
                    )
                stored_observation = self._forecast_observation_set_from_row(
                    observation_row
                )
                same_record(
                    stored_observation,
                    request.observation_set,
                    "Outcome Reviewer observation set",
                )
            elif isinstance(request, MistakeMemoryProposalRequest):
                forecasts = (
                    request.forecast,
                    *request.additional_supporting_forecasts,
                    *request.counterexample_forecasts,
                )
                evaluations = (
                    request.evaluation,
                    *request.additional_supporting_evaluations,
                    *request.counterexample_evaluations,
                )
                reviews = (
                    request.approved_review,
                    *request.additional_supporting_reviews,
                    *request.counterexample_reviews,
                )
                lessons = request.existing_lessons
                sources = request.existing_lesson_sources
                events = request.existing_lesson_events
            else:
                raise TypeError("Unsupported outcome-learning request type.")

            for forecast in forecasts:
                row = connection.execute(
                    "SELECT * FROM forecast_records WHERE forecast_id = ?",
                    (forecast.forecast_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Forecast {forecast.forecast_id} is not registered.")
                same_record(
                    self._forecast_record_from_row(row), forecast, "Forecast record"
                )
            for evaluation in evaluations:
                row = connection.execute(
                    "SELECT * FROM forecast_outcome_evaluations WHERE evaluation_id = ?",
                    (evaluation.evaluation_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(
                        f"Outcome evaluation {evaluation.evaluation_id} is not registered."
                    )
                same_record(
                    self._forecast_outcome_evaluation_from_row(row),
                    evaluation,
                    "Outcome evaluation",
                )
            for review in reviews:
                row = connection.execute(
                    "SELECT * FROM forecast_outcome_reviews WHERE review_id = ?",
                    (review.review_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Outcome review {review.review_id} is not registered.")
                stored_review = self._forecast_outcome_review_from_row(row)
                same_record(stored_review, review, "Outcome review")
                if review.decision.value not in {"approved", "revised"}:
                    raise ValueError(
                        "Mistake proposal persistence requires an explicitly approved or revised review."
                    )
            for lesson in lessons:
                row = connection.execute(
                    "SELECT * FROM mistake_memory_lessons WHERE lesson_id = ?",
                    (lesson.lesson_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Existing lesson {lesson.lesson_id} is not registered.")
                same_record(
                    self._mistake_memory_lesson_from_row(row),
                    lesson,
                    "Existing lesson",
                )
            for source in sources:
                row = connection.execute(
                    "SELECT * FROM mistake_memory_sources WHERE source_id = ?",
                    (source.source_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Lesson source {source.source_id} is not registered.")
                same_record(
                    self._mistake_memory_source_from_row(row),
                    source,
                    "Lesson source",
                )
            for event in events:
                row = connection.execute(
                    "SELECT * FROM mistake_memory_events WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Lesson event {event.event_id} is not registered.")
                same_record(
                    self._mistake_memory_event_from_row(row),
                    event,
                    "Lesson event",
                )

            if isinstance(request, MistakeMemoryProposalRequest):
                review_ids = {item.review_id for item in reviews}
                lesson_ids = {item.lesson_id for item in lessons}
                draft = orchestration.mistake_lesson_proposal_draft
                if draft is not None:
                    unknown_reviews = (
                        set(draft.supporting_review_ids)
                        | set(draft.counterexample_review_ids)
                    ) - review_ids
                    if unknown_reviews:
                        raise ValueError(
                            "Structured draft contains unregistered review references: "
                            + ", ".join(sorted(unknown_reviews))
                            + "."
                        )
                    unknown_lessons = set(
                        draft.possible_duplicate_lesson_ids
                    ) - lesson_ids
                    if unknown_lessons:
                        raise ValueError(
                            "Structured draft contains unregistered lesson references: "
                            + ", ".join(sorted(unknown_lessons))
                            + "."
                        )

            existing = connection.execute(
                "SELECT * FROM forecast_outcome_learning_orchestrations "
                "WHERE orchestration_id = ?",
                (orchestration.orchestration_id,),
            ).fetchone()
            if existing is not None:
                stored = self._outcome_learning_orchestration_from_row(existing)
                if stored.content_hash != orchestration.content_hash:
                    raise ValueError(
                        "Outcome-learning orchestration ID already exists with "
                        "different immutable content."
                    )
                return {
                    "orchestration_id": stored.orchestration_id,
                    "content_hash": stored.content_hash,
                    "inserted": False,
                }
            if orchestration.supersedes_orchestration_id is None:
                if orchestration.orchestration_version != 1:
                    raise ValueError(
                        "An orchestration without a predecessor must have version 1."
                    )
            else:
                predecessor_row = connection.execute(
                    "SELECT * FROM forecast_outcome_learning_orchestrations "
                    "WHERE orchestration_id = ?",
                    (orchestration.supersedes_orchestration_id,),
                ).fetchone()
                if predecessor_row is None:
                    raise KeyError(
                        "Superseded outcome-learning orchestration does not exist."
                    )
                predecessor = self._outcome_learning_orchestration_from_row(
                    predecessor_row
                )
                if (
                    orchestration.orchestration_version
                    != predecessor.orchestration_version + 1
                ):
                    raise ValueError(
                        "A replacement orchestration must increment its predecessor version by one."
                    )
                if (
                    orchestration.orchestration_kind
                    is not predecessor.orchestration_kind
                    or orchestration.forecast_id != predecessor.forecast_id
                    or orchestration.observation_set_id
                    != predecessor.observation_set_id
                    or orchestration.evaluation_id != predecessor.evaluation_id
                    or orchestration.outcome_review_id
                    != predecessor.outcome_review_id
                ):
                    raise ValueError(
                        "An orchestration replacement must remain in the same source lineage."
                    )
                branch = connection.execute(
                    "SELECT orchestration_id "
                    "FROM forecast_outcome_learning_orchestrations "
                    "WHERE supersedes_orchestration_id = ?",
                    (orchestration.supersedes_orchestration_id,),
                ).fetchone()
                if branch is not None:
                    raise ValueError(
                        "The predecessor already has an explicit replacement."
                    )

            connection.execute(
                """
                INSERT INTO forecast_outcome_learning_orchestrations(
                    orchestration_id, orchestration_version,
                    supersedes_orchestration_id, orchestration_kind, status,
                    human_action_required, forecast_id, observation_set_id,
                    evaluation_id, outcome_review_id, existing_lesson_id,
                    request_id, provider, model, prompt_version, policy_version,
                    frozen_input_hash, structured_draft_hash, result_hash,
                    warnings_json, validation_errors_json, started_at_utc,
                    completed_at_utc, schema_version, calculation_version,
                    content_hash, record_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    orchestration.orchestration_id,
                    orchestration.orchestration_version,
                    orchestration.supersedes_orchestration_id,
                    orchestration.orchestration_kind.value,
                    orchestration.status.value,
                    int(orchestration.human_action_required),
                    orchestration.forecast_id,
                    orchestration.observation_set_id,
                    orchestration.evaluation_id,
                    orchestration.outcome_review_id,
                    orchestration.existing_lesson_id,
                    request.request_id,
                    orchestration.provider,
                    orchestration.model,
                    orchestration.prompt_version,
                    orchestration.policy_version,
                    request.frozen_input_hash,
                    orchestration.structured_draft_hash,
                    orchestration.result.content_hash,
                    canonical_forecast_json(list(orchestration.warnings)),
                    canonical_forecast_json(
                        list(orchestration.validation_errors)
                    ),
                    orchestration.started_at_utc,
                    orchestration.completed_at_utc,
                    orchestration.schema_version,
                    orchestration.calculation_version,
                    orchestration.content_hash,
                    record_json,
                ),
            )
        return {
            "orchestration_id": orchestration.orchestration_id,
            "content_hash": orchestration.content_hash,
            "inserted": True,
        }

    def get_forecast_outcome_learning_orchestration(
        self, orchestration_id: str
    ) -> OutcomeLearningOrchestration | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM forecast_outcome_learning_orchestrations "
                "WHERE orchestration_id = ?",
                (orchestration_id,),
            ).fetchone()
            return (
                self._outcome_learning_orchestration_from_row(row)
                if row is not None
                else None
            )

    def list_forecast_outcome_learning_orchestrations(
        self,
        *,
        forecast_id: str | None = None,
        evaluation_id: str | None = None,
        orchestration_kind: str | None = None,
        status: str | None = None,
        include_superseded: bool = True,
        limit: int = 100,
    ) -> list[OutcomeLearningOrchestration]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if orchestration_kind is not None:
            try:
                orchestration_kind = OutcomeLearningAgentRole(
                    orchestration_kind
                ).value
            except ValueError as exc:
                raise ValueError("Invalid outcome-learning orchestration kind.") from exc
        for column, value in (
            ("forecast_id", forecast_id),
            ("evaluation_id", evaluation_id),
            ("orchestration_kind", orchestration_kind),
            ("status", status),
        ):
            if value is not None:
                conditions.append(f"o.{column} = ?")
                parameters.append(value)
        if not include_superseded:
            conditions.append(
                "NOT EXISTS ("
                "SELECT 1 FROM forecast_outcome_learning_orchestrations newer "
                "WHERE newer.supersedes_orchestration_id = o.orchestration_id)"
            )
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT o.* FROM forecast_outcome_learning_orchestrations o"
                + where
                + " ORDER BY o.completed_at_utc DESC, "
                "o.orchestration_version DESC, o.orchestration_id ASC LIMIT ?",
                parameters,
            ).fetchall()
            return [
                self._outcome_learning_orchestration_from_row(row) for row in rows
            ]

    def save_wave_fingerprint(
        self,
        fingerprint: dict[str, Any],
        *,
        run_id: int | None = None,
        resolution_id: int | None = None,
    ) -> dict[str, Any]:
        """Persist one immutable fingerprint, deduplicated by canonical content hash."""
        payload = deepcopy(fingerprint)
        expected_hash = fingerprint_content_hash(payload)
        supplied_hash = payload.get("content_hash")
        if supplied_hash is not None and supplied_hash != expected_hash:
            raise ValueError("Fingerprint content_hash does not match its canonical payload.")
        payload["content_hash"] = expected_hash
        identity = payload.get("identity")
        cutoff = payload.get("cutoff")
        if not isinstance(identity, dict) or not isinstance(cutoff, dict):
            raise ValueError("Fingerprint identity and cutoff blocks are required.")
        schema_version = str(payload.get("feature_schema_version") or "")
        calculation_version = str(payload.get("calculation_version") or "")
        identity_hash = str(payload.get("identity_hash") or "")
        if not schema_version or not calculation_version or not identity_hash:
            raise ValueError("Fingerprint schema, calculation, and identity versions are required.")
        origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
        resolved_run_id = run_id if run_id is not None else origin.get("analysis_run_id")
        resolved_resolution_id = (
            resolution_id
            if resolution_id is not None
            else origin.get("degree_resolution_id")
        )
        observation = {
            "identity": identity,
            "origin": origin,
            "cutoff": cutoff,
            "source": payload.get("source"),
            "data_quality": payload.get("data_quality"),
        }
        observation_json = json.dumps(
            observation, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        observation_hash = _sha256(observation_json)
        fingerprint_json = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        schema_definition = (
            FEATURE_SCHEMA_DEFINITION
            if schema_version == FEATURE_SCHEMA_VERSION
            else {
                "schema_version": schema_version,
                "status": "legacy_or_external",
                "read_policy": "Preserve raw JSON without coercing it to the current schema.",
            }
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO feature_schema_versions(
                    schema_version, calculation_version, schema_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    schema_version,
                    calculation_version,
                    json.dumps(schema_definition, ensure_ascii=True, sort_keys=True),
                    _utc_now(),
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO wave_observations(
                    run_id, resolution_id, wave_id, symbol, timeframe,
                    cutoff_timestamp, observation_hash, observation_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_run_id,
                    resolved_resolution_id,
                    str(identity.get("wave_id") or "unknown"),
                    str(identity.get("symbol") or "unknown"),
                    str(identity.get("timeframe") or "unknown"),
                    str(cutoff.get("timestamp") or "unknown"),
                    observation_hash,
                    observation_json,
                    _utc_now(),
                ),
            )
            observation_row = connection.execute(
                "SELECT id FROM wave_observations WHERE observation_hash = ?",
                (observation_hash,),
            ).fetchone()
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO wave_fingerprints(
                    observation_id, run_id, resolution_id, feature_schema_version,
                    calculation_version, identity_hash, content_hash,
                    fingerprint_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(observation_row["id"]),
                    resolved_run_id,
                    resolved_resolution_id,
                    schema_version,
                    calculation_version,
                    identity_hash,
                    expected_hash,
                    fingerprint_json,
                    _utc_now(),
                ),
            )
            inserted = cursor.rowcount == 1
            fingerprint_row = connection.execute(
                "SELECT id, observation_id FROM wave_fingerprints WHERE content_hash = ?",
                (expected_hash,),
            ).fetchone()
        return {
            "fingerprint_id": int(fingerprint_row["id"]),
            "observation_id": int(fingerprint_row["observation_id"]),
            "content_hash": expected_hash,
            "inserted": inserted,
        }

    @staticmethod
    def _wave_fingerprint_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["fingerprint"] = json.loads(result.pop("fingerprint_json"))
        return result

    def get_wave_fingerprint(self, fingerprint_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM wave_fingerprints WHERE id = ?", (fingerprint_id,)
            ).fetchone()
        return self._wave_fingerprint_row(row) if row is not None else None

    def get_wave_fingerprint_by_hash(self, content_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM wave_fingerprints WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
        return self._wave_fingerprint_row(row) if row is not None else None

    def list_wave_fingerprints(
        self, *, run_id: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if run_id is None:
                rows = connection.execute(
                    "SELECT * FROM wave_fingerprints ORDER BY id DESC LIMIT ?",
                    (max(1, limit),),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM wave_fingerprints
                    WHERE run_id = ? ORDER BY id DESC LIMIT ?
                    """,
                    (run_id, max(1, limit)),
                ).fetchall()
        return [self._wave_fingerprint_row(row) for row in rows]

    @staticmethod
    def _canonical_payload(value: Mapping[str, Any]) -> str:
        return json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def save_correction_case(
        self,
        case: Mapping[str, Any],
        *,
        initial_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a case and optional immutable endpoint snapshot."""
        payload = deepcopy(dict(case))
        expected_hash = correction_case_content_hash(payload)
        if payload.get("content_hash") != expected_hash:
            raise ValueError("Correction case content_hash does not match its canonical payload.")
        required = (
            "case_id",
            "source_identity",
            "symbol",
            "timeframe",
            "candidate_endpoint",
            "elliott_degree",
            "parent_pattern_family",
            "schema_version",
            "calculation_version",
        )
        missing = [field for field in required if field not in payload]
        if missing:
            raise ValueError("Missing correction case fields: " + ", ".join(missing))
        endpoint = payload["candidate_endpoint"]
        if not isinstance(endpoint, Mapping) or not endpoint.get("timestamp"):
            raise ValueError("Correction case candidate endpoint is incomplete.")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO correction_cases(
                    case_id, source_run_id, source_resolution_id, symbol, timeframe,
                    endpoint_timestamp, elliott_degree, parent_pattern_family,
                    schema_version, calculation_version, content_hash, case_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["case_id"],
                    payload.get("source_run_id"),
                    payload.get("source_resolution_id"),
                    payload["symbol"],
                    payload["timeframe"],
                    endpoint["timestamp"],
                    payload["elliott_degree"],
                    payload["parent_pattern_family"],
                    payload["schema_version"],
                    payload["calculation_version"],
                    expected_hash,
                    self._canonical_payload(payload),
                    payload.get("created_at") or _utc_now(),
                ),
            )
            inserted = cursor.rowcount == 1
            existing = connection.execute(
                "SELECT content_hash FROM correction_cases WHERE case_id = ?",
                (payload["case_id"],),
            ).fetchone()
            if existing is None or existing["content_hash"] != expected_hash:
                raise ValueError("Correction case ID conflicts with different content.")
            snapshot_result = None
            if initial_snapshot is not None:
                snapshot_result = self._save_snapshot(connection, initial_snapshot)
        return {
            "case_id": payload["case_id"],
            "content_hash": expected_hash,
            "inserted": inserted,
            "initial_snapshot": snapshot_result,
        }

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["snapshot_json"])

    def _save_snapshot(
        self, connection: sqlite3.Connection, snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        payload = deepcopy(dict(snapshot))
        errors = validate_snapshot(payload)
        if errors:
            raise ValueError("Invalid hypothesis snapshot: " + " ".join(errors))
        case_row = connection.execute(
            "SELECT case_id FROM correction_cases WHERE case_id = ?",
            (payload["case_id"],),
        ).fetchone()
        if case_row is None:
            raise KeyError(f"Correction case {payload['case_id']} does not exist.")
        existing = connection.execute(
            "SELECT snapshot_id, content_hash, sequence_number FROM hypothesis_snapshots "
            "WHERE snapshot_id = ? OR content_hash = ?",
            (payload["snapshot_id"], payload["content_hash"]),
        ).fetchone()
        if existing is not None:
            if (
                existing["snapshot_id"] != payload["snapshot_id"]
                or existing["content_hash"] != payload["content_hash"]
            ):
                raise ValueError("Snapshot ID or content hash conflicts with stored content.")
            return {
                "snapshot_id": payload["snapshot_id"],
                "sequence_number": int(existing["sequence_number"]),
                "content_hash": payload["content_hash"],
                "inserted": False,
            }
        parent_id = payload.get("parent_snapshot_id")
        if parent_id:
            parent = connection.execute(
                "SELECT case_id, sequence_number FROM hypothesis_snapshots WHERE snapshot_id = ?",
                (parent_id,),
            ).fetchone()
            if parent is None:
                raise KeyError(f"Parent snapshot {parent_id} does not exist.")
            if parent["case_id"] != payload["case_id"]:
                raise ValueError("Parent and child snapshots belong to different cases.")
            sequence = int(parent["sequence_number"]) + 1
        else:
            sequence = 1
        connection.execute(
            """
            INSERT INTO hypothesis_snapshots(
                snapshot_id, case_id, parent_snapshot_id, source_run_id,
                sequence_number, cutoff_timestamp, schema_version,
                calculation_version, content_hash, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["snapshot_id"],
                payload["case_id"],
                parent_id,
                payload.get("source_run_id"),
                sequence,
                payload["cutoff"],
                payload["schema_version"],
                payload["calculation_version"],
                payload["content_hash"],
                self._canonical_payload(payload),
                payload.get("created_at") or _utc_now(),
            ),
        )
        for state in payload.get("hypothesis_states", []):
            connection.execute(
                """
                INSERT INTO hypothesis_states(
                    snapshot_id, hypothesis_id, state, status, state_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    payload["snapshot_id"],
                    state["hypothesis_id"],
                    state["state"],
                    state["status"],
                    self._canonical_payload(state),
                ),
            )
        for transition in payload.get("transitions", []):
            transition_payload = deepcopy(dict(transition))
            if transition_payload.get("snapshot_id") != payload["snapshot_id"]:
                raise ValueError("Transition references a different snapshot.")
            transition_json = self._canonical_payload(transition_payload)
            transition_hash = _sha256(transition_json)
            connection.execute(
                """
                INSERT INTO hypothesis_transitions(
                    transition_id, case_id, snapshot_id, hypothesis_id,
                    previous_state, new_state, cutoff_timestamp, provisional,
                    calculation_version, content_hash, transition_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition_payload["transition_id"],
                    payload["case_id"],
                    payload["snapshot_id"],
                    transition_payload["hypothesis_id"],
                    transition_payload["previous_state"],
                    transition_payload["new_state"],
                    transition_payload["analysis_cutoff"],
                    int(bool(transition_payload["provisional"])),
                    transition_payload["calculation_version"],
                    transition_hash,
                    transition_json,
                    transition_payload.get("transition_timestamp") or _utc_now(),
                ),
            )
        return {
            "snapshot_id": payload["snapshot_id"],
            "sequence_number": sequence,
            "content_hash": payload["content_hash"],
            "inserted": True,
        }

    def save_hypothesis_snapshot(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            return self._save_snapshot(connection, snapshot)

    def get_correction_case(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT case_json FROM correction_cases WHERE case_id = ?", (case_id,)
            ).fetchone()
        return json.loads(row["case_json"]) if row is not None else None

    def get_hypothesis_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM hypothesis_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return self._snapshot_row(row) if row is not None else None

    def get_latest_hypothesis_snapshot(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM hypothesis_snapshots
                WHERE case_id = ? ORDER BY sequence_number DESC, rowid DESC LIMIT 1
                """,
                (case_id,),
            ).fetchone()
        return self._snapshot_row(row) if row is not None else None

    def get_snapshot_lineage(self, snapshot_id: str) -> list[dict[str, Any]]:
        lineage: list[dict[str, Any]] = []
        current = self.get_hypothesis_snapshot(snapshot_id)
        while current is not None:
            lineage.append(current)
            parent_id = current.get("parent_snapshot_id")
            current = self.get_hypothesis_snapshot(parent_id) if parent_id else None
        lineage.reverse()
        return lineage

    def get_snapshot_evidence(self, snapshot_id: str) -> dict[str, Any] | None:
        snapshot = self.get_hypothesis_snapshot(snapshot_id)
        if snapshot is None:
            return None
        return {
            "snapshot_id": snapshot_id,
            "case_id": snapshot["case_id"],
            "cutoff": snapshot["cutoff"],
            "evidence_references": snapshot.get("evidence_references", {}),
            "evidence_catalog": snapshot.get("evidence_catalog", []),
            "fingerprint_references": snapshot.get("fingerprint_references", []),
            "post_terminal_events": snapshot.get("post_terminal_events", {}),
            "look_ahead_policy": "Only evidence available at or before this cutoff is shown.",
        }

    @staticmethod
    def _outcome_row(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["outcome_json"])

    @staticmethod
    def _review_row(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["review_json"])

    def get_outcome_resolution(self, outcome_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM resolved_outcomes WHERE outcome_id = ?", (outcome_id,)
            ).fetchone()
        return self._outcome_row(row) if row is not None else None

    def get_outcome_history(self, case_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            outcomes = connection.execute(
                "SELECT * FROM resolved_outcomes WHERE case_id = ? ORDER BY revision",
                (case_id,),
            ).fetchall()
            reviews = connection.execute(
                "SELECT * FROM outcome_reviews WHERE case_id = ? ORDER BY rowid",
                (case_id,),
            ).fetchall()
        reviews_by_outcome: dict[str, list[dict[str, Any]]] = {}
        for row in reviews:
            review = self._review_row(row)
            reviews_by_outcome.setdefault(str(review["outcome_id"]), []).append(review)
        result: list[dict[str, Any]] = []
        for row in outcomes:
            outcome = self._outcome_row(row)
            result.append(
                {
                    "outcome": outcome,
                    "reviews": reviews_by_outcome.get(str(outcome["outcome_id"]), []),
                }
            )
        return result

    def get_current_reviewed_outcome(self, case_id: str) -> dict[str, Any] | None:
        for item in reversed(self.get_outcome_history(case_id)):
            reviews = item["reviews"]
            if not reviews:
                continue
            latest = reviews[-1]
            if latest.get("action") in {"approved", "revised"} and latest.get(
                "review_status"
            ) in {"reviewed", "approved", "resolved"}:
                return {**item["outcome"], "latest_review": latest}
        return None

    def list_correction_cases(
        self, *, status: str = "unresolved", limit: int = 100
    ) -> list[dict[str, Any]]:
        if status not in {"unresolved", "resolved", "all"}:
            raise ValueError("Correction case status must be unresolved, resolved, or all.")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT case_json FROM correction_cases ORDER BY created_at DESC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            case = json.loads(row["case_json"])
            latest = self.get_latest_hypothesis_snapshot(case["case_id"])
            outcome = self.get_current_reviewed_outcome(case["case_id"])
            case_status = "resolved" if outcome is not None else "unresolved"
            if status != "all" and status != case_status:
                continue
            result.append(
                {
                    "case_id": case["case_id"],
                    "symbol": case["symbol"],
                    "timeframe": case["timeframe"],
                    "candidate_endpoint": case["candidate_endpoint"],
                    "elliott_degree": case["elliott_degree"],
                    "parent_pattern_family": case["parent_pattern_family"],
                    "status": case_status,
                    "latest_snapshot_id": latest.get("snapshot_id") if latest else None,
                    "current_state": latest.get("current_state") if latest else "unresolved",
                    "resolved_hypothesis": outcome.get("resolved_hypothesis") if outcome else None,
                }
            )
        return result

    def get_correction_lineage(self, case_id: str) -> dict[str, Any] | None:
        case = self.get_correction_case(case_id)
        if case is None:
            return None
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM hypothesis_snapshots WHERE case_id = ?
                ORDER BY sequence_number, rowid
                """,
                (case_id,),
            ).fetchall()
        return {
            "case": case,
            "snapshots": [self._snapshot_row(row) for row in rows],
            "outcome_history": self.get_outcome_history(case_id),
            "current_reviewed_outcome": self.get_current_reviewed_outcome(case_id),
        }

    def _next_outcome_revision(self, connection: sqlite3.Connection, case_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
            "FROM resolved_outcomes WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        return int(row["revision"])

    def _insert_outcome(
        self,
        connection: sqlite3.Connection,
        outcome: Mapping[str, Any],
    ) -> None:
        payload = deepcopy(dict(outcome))
        if payload.get("content_hash") != outcome_content_hash(payload):
            raise ValueError("Outcome content_hash does not match its canonical payload.")
        connection.execute(
            """
            INSERT INTO resolved_outcomes(
                outcome_id, case_id, selected_snapshot_id, review_snapshot_id,
                revision, resolved_hypothesis, resolution_cutoff, schema_version,
                content_hash, outcome_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["outcome_id"],
                payload["case_id"],
                payload["selected_snapshot_id"],
                payload.get("review_snapshot_id"),
                payload["revision"],
                payload["resolved_hypothesis"],
                payload["resolution_cutoff"],
                payload["schema_version"],
                payload["content_hash"],
                self._canonical_payload(payload),
                _utc_now(),
            ),
        )

    def _insert_outcome_review(
        self, connection: sqlite3.Connection, review: Mapping[str, Any]
    ) -> None:
        payload = deepcopy(dict(review))
        if payload.get("content_hash") != review_content_hash(payload):
            raise ValueError("Outcome review content_hash does not match its canonical payload.")
        connection.execute(
            """
            INSERT INTO outcome_reviews(
                review_id, outcome_id, case_id, action, reviewer, review_status,
                content_hash, review_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["review_id"],
                payload["outcome_id"],
                payload["case_id"],
                payload["action"],
                payload["reviewer"],
                payload["review_status"],
                payload["content_hash"],
                self._canonical_payload(payload),
                payload.get("reviewed_at") or _utc_now(),
            ),
        )

    def propose_outcome_resolution(self, outcome: Mapping[str, Any]) -> dict[str, Any]:
        """Store an unreviewed proposal; it does not resolve the case."""
        base = deepcopy(dict(outcome))
        case_id = str(base.get("case_id") or "")
        selected = self.get_hypothesis_snapshot(str(base.get("selected_snapshot_id") or ""))
        if selected is None or selected.get("case_id") != case_id:
            raise ValueError("Outcome proposal references no matching historical snapshot.")
        with self.connect() as connection:
            revision = self._next_outcome_revision(connection, case_id)
            finalized = finalize_outcome_revision(base, revision)
            self._insert_outcome(connection, finalized)
        return finalized

    def save_reviewed_outcome(
        self,
        outcome: Mapping[str, Any],
        *,
        action: str = "approved",
    ) -> dict[str, Any]:
        """Append an explicit human resolution and a linked reviewed snapshot."""
        if action not in {"approved", "revised"}:
            raise ValueError("Reviewed outcome action must be approved or revised.")
        base = deepcopy(dict(outcome))
        case_id = str(base.get("case_id") or "")
        selected = self.get_hypothesis_snapshot(str(base.get("selected_snapshot_id") or ""))
        latest = self.get_latest_hypothesis_snapshot(case_id)
        if selected is None or latest is None or selected.get("case_id") != case_id:
            raise ValueError("Reviewed outcome references no matching historical snapshot.")
        if action == "revised":
            current = self.get_current_reviewed_outcome(case_id)
            prior_hypothesis = (
                str(current.get("resolved_hypothesis")) if current is not None else ""
            )
            rejected = list(base.get("rejected_alternatives") or [])
            rejected_ids = {
                str(item.get("hypothesis_id"))
                for item in rejected
                if isinstance(item, Mapping)
            }
            if (
                prior_hypothesis
                and prior_hypothesis != str(base.get("resolved_hypothesis") or "")
                and prior_hypothesis not in rejected_ids
            ):
                rejected.append(
                    {
                        "hypothesis_id": prior_hypothesis,
                        "reason": "Superseded by a later explicit human outcome revision.",
                    }
                )
            base["rejected_alternatives"] = rejected
        with self.connect() as connection:
            revision = self._next_outcome_revision(connection, case_id)
        finalized = finalize_outcome_revision(base, revision)
        reviewed_snapshot = create_review_snapshot(latest, finalized)
        finalized["review_snapshot_id"] = reviewed_snapshot["snapshot_id"]
        finalized = finalize_outcome_revision(finalized, revision)
        review = build_outcome_review(
            finalized,
            action=action,
            reviewer=str(finalized.get("reviewer") or ""),
            review_status=str(finalized.get("review_status") or "reviewed"),
            reason=str(finalized.get("notes") or "Explicit human outcome review."),
            reviewed_at=finalized["resolution_cutoff"],
        )
        with self.connect() as connection:
            self._save_snapshot(connection, reviewed_snapshot)
            self._insert_outcome(connection, finalized)
            self._insert_outcome_review(connection, review)
        return {
            "case_id": case_id,
            "outcome_id": finalized["outcome_id"],
            "review_id": review["review_id"],
            "review_snapshot_id": reviewed_snapshot["snapshot_id"],
            "revision": revision,
            "resolved_hypothesis": finalized["resolved_hypothesis"],
            "review_status": review["review_status"],
        }

    def reject_outcome_resolution(
        self,
        outcome_id: str,
        *,
        reviewer: str,
        reason: str,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        outcome = self.get_outcome_resolution(outcome_id)
        if outcome is None:
            raise KeyError(f"Outcome proposal {outcome_id} does not exist.")
        if not str(reason).strip():
            raise ValueError("A rejection reason is required.")
        review = build_outcome_review(
            outcome,
            action="rejected",
            reviewer=reviewer,
            review_status="rejected",
            reason=reason,
            reviewed_at=reviewed_at or _utc_now(),
        )
        with self.connect() as connection:
            self._insert_outcome_review(connection, review)
        return review

    def revise_outcome_resolution(self, outcome: Mapping[str, Any]) -> dict[str, Any]:
        return self.save_reviewed_outcome(outcome, action="revised")

    def export_reviewed_correction_cases(self) -> dict[str, Any]:
        cases: list[dict[str, Any]] = []
        for summary in self.list_correction_cases(status="resolved", limit=100_000):
            lineage = self.get_correction_lineage(summary["case_id"])
            if lineage is not None:
                cases.append(lineage)
        return {
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "exported_at": _utc_now(),
            "case_count": len(cases),
            "cases": cases,
            "policy": "Only explicitly reviewed outcomes are exported as resolved.",
        }

    def _current_phase4_outcome_item(
        self, case_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        """Return only the latest revision when its latest review is non-stale."""
        history = self.get_outcome_history(case_id)
        if not history:
            return None, None, "No Phase 4 reviewed outcome exists."
        latest = history[-1]
        reviews = list(latest.get("reviews") or [])
        if not reviews:
            return None, None, "The latest Phase 4 outcome revision has no review."
        review = reviews[-1]
        if review.get("action") not in {"approved", "revised"} or review.get(
            "review_status"
        ) not in {"reviewed", "approved", "resolved"}:
            return (
                None,
                review,
                "The latest Phase 4 outcome review is rejected, unresolved, or stale.",
            )
        return deepcopy(latest["outcome"]), deepcopy(review), None

    def _endpoint_snapshot_for_case(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM hypothesis_snapshots
                WHERE case_id = ? AND parent_snapshot_id IS NULL
                ORDER BY sequence_number, rowid LIMIT 1
                """,
                (case_id,),
            ).fetchone()
        return self._snapshot_row(row) if row is not None else None

    @staticmethod
    def _ineligible_experience_result(
        case: Mapping[str, Any], *, code: str, reason: str, disposition: str = "rejected"
    ) -> dict[str, Any]:
        market_episode_id = None
        try:
            from .experience import market_episode_identity

            market_episode_id = market_episode_identity(case)["market_episode_id"]
        except (KeyError, TypeError, ValueError):
            pass
        item = {"code": code, "message": reason, "disposition": disposition}
        return {
            "state": disposition,
            "market_episode_id": market_episode_id,
            "episode_identity": None,
            "eligibility": {
                "status": disposition,
                "eligible": False,
                "rejection_reasons": [item] if disposition == "rejected" else [],
                "quarantine_reasons": [item] if disposition == "quarantined" else [],
                "endpoint_alignment": None,
                "source_versions": None,
            },
            "experience_case": None,
            "pattern_dna": None,
        }

    def assemble_experience_for_correction(
        self,
        correction_case_id: str,
        *,
        fingerprint_content_hash: str | None = None,
        endpoint_alignment_tolerance_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Resolve stored Phase 3/4 sources and assemble one candidate in memory."""
        case = self.get_correction_case(correction_case_id)
        if case is None:
            raise KeyError(f"Correction case {correction_case_id} does not exist.")
        endpoint = self._endpoint_snapshot_for_case(correction_case_id)
        if endpoint is None:
            return self._ineligible_experience_result(
                case,
                code="missing_endpoint_snapshot",
                reason="The correction case has no immutable endpoint snapshot.",
            )
        outcome, outcome_review, outcome_error = self._current_phase4_outcome_item(
            correction_case_id
        )
        if outcome is None or outcome_review is None:
            return self._ineligible_experience_result(
                case,
                code="missing_or_stale_reviewed_outcome",
                reason=outcome_error or "A current reviewed Phase 4 outcome is required.",
            )
        fingerprint_hashes = [
            str(item.get("content_hash") or "")
            for item in endpoint.get("fingerprint_references", [])
            if isinstance(item, Mapping) and item.get("content_hash")
        ]
        if fingerprint_content_hash is not None:
            fingerprint_hashes = [fingerprint_content_hash]
        if not fingerprint_hashes:
            return self._ineligible_experience_result(
                case,
                code="missing_endpoint_fingerprint",
                reason="The endpoint snapshot references no original Phase 3 fingerprint.",
            )
        target_snapshot_id = str(
            outcome.get("review_snapshot_id")
            or outcome.get("selected_snapshot_id")
            or endpoint.get("snapshot_id")
        )
        lineage = self.get_snapshot_lineage(target_snapshot_id)
        attempts: list[dict[str, Any]] = []
        for content_hash in dict.fromkeys(fingerprint_hashes):
            stored = self.get_wave_fingerprint_by_hash(content_hash)
            if stored is None:
                attempts.append(
                    self._ineligible_experience_result(
                        case,
                        code="missing_fingerprint_record",
                        reason=f"No stored Phase 3 fingerprint has content hash {content_hash}.",
                    )
                )
                continue
            assembled = assemble_experience_candidate(
                correction_case=case,
                endpoint_snapshot=endpoint,
                fingerprint=stored["fingerprint"],
                outcome=outcome,
                outcome_review=outcome_review,
                snapshot_lineage=lineage,
                endpoint_alignment_tolerance_seconds=endpoint_alignment_tolerance_seconds,
                outcome_is_current=True,
            )
            assembled["source_fingerprint_id"] = int(stored["id"])
            attempts.append(assembled)
            if assembled["state"] == "assembled_candidate":
                return assembled
        if not attempts:
            return self._ineligible_experience_result(
                case,
                code="missing_fingerprint_record",
                reason="No usable Phase 3 fingerprint record was found.",
            )
        quarantined = next(
            (item for item in attempts if item["state"] == "quarantined"), None
        )
        return quarantined or attempts[0]

    def list_experience_candidates(
        self,
        *,
        include_ineligible: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT case_id FROM correction_cases ORDER BY created_at, rowid"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            assembled = self.assemble_experience_for_correction(str(row["case_id"]))
            if assembled["state"] != "assembled_candidate" and not include_ineligible:
                continue
            case_payload = assembled.get("experience_case") or {}
            existing = None
            experience_case_id = case_payload.get("experience_case_id")
            if experience_case_id:
                existing = self.get_experience_case_record(str(experience_case_id))
            result.append(
                {
                    "correction_case_id": str(row["case_id"]),
                    "market_episode_id": assembled.get("market_episode_id"),
                    "experience_case_id": experience_case_id,
                    "candidate_state": assembled["state"],
                    "eligibility": assembled.get("eligibility"),
                    "proposed_quality": case_payload.get("proposed_quality"),
                    "already_created": existing is not None,
                    "stored_state": (
                        self.get_experience_case_status(str(experience_case_id))
                        if existing is not None
                        else None
                    ),
                }
            )
            if len(result) >= max(1, limit):
                break
        return result

    @staticmethod
    def _experience_case_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["experience_case"] = json.loads(result.pop("case_json"))
        return result

    @staticmethod
    def _pattern_dna_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["dna"] = json.loads(result.pop("dna_json"))
        return result

    @staticmethod
    def _experience_review_row(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["review_json"])

    @staticmethod
    def _tag_assignment_row(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["assignment_json"])

    def get_experience_case_record(
        self, experience_case_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM experience_cases WHERE experience_case_id = ?",
                (experience_case_id,),
            ).fetchone()
        return self._experience_case_row(row) if row is not None else None

    def _latest_experience_version(
        self, market_episode_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM experience_cases WHERE market_episode_id = ?
                ORDER BY case_version DESC, rowid DESC LIMIT 1
                """,
                (market_episode_id,),
            ).fetchone()
        return self._experience_case_row(row) if row is not None else None

    def _save_experience_candidate_in_connection(
        self,
        connection: sqlite3.Connection,
        assembled: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist a Phase 5A.1 candidate using the caller's transaction."""
        if assembled.get("state") != "assembled_candidate":
            return {
                "inserted": False,
                "state": assembled.get("state"),
                "eligibility": deepcopy(assembled.get("eligibility")),
                "experience_case_id": None,
            }
        case = deepcopy(dict(assembled.get("experience_case") or {}))
        dna_records = assembled.get("pattern_dna")
        if not isinstance(dna_records, Mapping) or set(dna_records) != set(DNA_KINDS):
            raise ValueError("An experience candidate requires endpoint, confirmation, and resolved-outcome DNA.")
        expected_case_hash = experience_case_content_hash(case)
        if case.get("content_hash") != expected_case_hash:
            raise ValueError("Experience case content_hash does not match its canonical payload.")
        if case.get("schema_version") != EXPERIENCE_SCHEMA_VERSION:
            raise ValueError("Unsupported experience case schema version.")
        if case.get("assembly_state") != "assembled_candidate":
            raise ValueError("Only an assembled candidate may enter pending experience review.")
        experience_case_id = str(case.get("experience_case_id") or "")
        market_episode_id = str(case.get("market_episode_id") or "")
        source_refs = case.get("source_references") if isinstance(case.get("source_references"), Mapping) else {}
        source_fingerprint_id = assembled.get("source_fingerprint_id")
        if not experience_case_id or not market_episode_id or not isinstance(source_fingerprint_id, int):
            raise ValueError("Experience candidate identity or stored fingerprint reference is missing.")
        for kind in DNA_KINDS:
            dna = dna_records[kind]
            if not isinstance(dna, Mapping):
                raise ValueError(f"Pattern DNA {kind!r} is malformed.")
            if dna.get("dna_kind") != kind or dna.get("experience_case_id") != experience_case_id:
                raise ValueError(f"Pattern DNA {kind!r} references a different case.")
            if dna.get("content_hash") != pattern_dna_content_hash(dna):
                raise ValueError(f"Pattern DNA {kind!r} content hash is invalid.")
        existing = connection.execute(
            "SELECT * FROM experience_cases WHERE experience_case_id = ? OR content_hash = ?",
            (experience_case_id, expected_case_hash),
        ).fetchone()
        if existing is not None:
            if (
                existing["experience_case_id"] != experience_case_id
                or existing["content_hash"] != expected_case_hash
            ):
                raise ValueError("Experience case ID or content hash conflicts with stored content.")
            return {
                "inserted": False,
                "experience_case_id": experience_case_id,
                "market_episode_id": market_episode_id,
                "case_version": int(existing["case_version"]),
                "state": "existing",
                "supersedes_case_id": existing["supersedes_case_id"],
            }
        previous = connection.execute(
            """
            SELECT experience_case_id, case_version FROM experience_cases
            WHERE market_episode_id = ? ORDER BY case_version DESC, rowid DESC LIMIT 1
            """,
            (market_episode_id,),
        ).fetchone()
        case_version = int(previous["case_version"]) + 1 if previous is not None else 1
        supersedes = str(previous["experience_case_id"]) if previous is not None else None
        connection.execute(
            """
            INSERT INTO experience_cases(
                experience_case_id, market_episode_id, case_version,
                supersedes_case_id, source_correction_case_id,
                source_endpoint_snapshot_id, source_fingerprint_id,
                source_outcome_id, source_outcome_review_id, assembly_state,
                schema_version, calculation_version, content_hash,
                case_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                experience_case_id,
                market_episode_id,
                case_version,
                supersedes,
                source_refs.get("correction_case_id"),
                source_refs.get("endpoint_snapshot_id"),
                source_fingerprint_id,
                source_refs.get("outcome_id"),
                source_refs.get("outcome_review_id"),
                "pending_experience_review",
                case["schema_version"],
                case["calculation_version"],
                expected_case_hash,
                self._canonical_payload(case),
                _utc_now(),
            ),
        )
        for kind in DNA_KINDS:
            dna = deepcopy(dict(dna_records[kind]))
            if kind == "endpoint":
                source_hash = str(dna.get("source_fingerprint_hash") or "")
            elif kind == "confirmation":
                source_hash = _sha256(
                    self._canonical_payload(
                        {"snapshot_hashes": dna.get("source_snapshot_hashes") or []}
                    )
                )
            else:
                source_hash = str(dna.get("source_outcome_hash") or "")
            connection.execute(
                """
                INSERT INTO pattern_dna(
                    dna_id, experience_case_id, market_episode_id, dna_kind,
                    schema_version, calculation_version, cutoff_timestamp,
                    source_content_hash, content_hash, dna_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dna["dna_id"],
                    experience_case_id,
                    market_episode_id,
                    kind,
                    dna["schema_version"],
                    dna["calculation_version"],
                    dna.get("cutoff"),
                    source_hash,
                    dna["content_hash"],
                    self._canonical_payload(dna),
                    _utc_now(),
                ),
            )
        return {
            "inserted": True,
            "experience_case_id": experience_case_id,
            "market_episode_id": market_episode_id,
            "case_version": case_version,
            "state": "pending_experience_review",
            "supersedes_case_id": supersedes,
        }

    def save_experience_candidate(self, assembled: Mapping[str, Any]) -> dict[str, Any]:
        """Persist one eligible candidate and exactly three immutable DNA layers."""
        with self.connect() as connection:
            result = self._save_experience_candidate_in_connection(connection, assembled)
        if not result.get("inserted") and result.get("experience_case_id"):
            result["state"] = self.get_experience_case_status(
                str(result["experience_case_id"])
            )["state"]
        return result

    def create_experience_case(
        self,
        correction_case_id: str,
        *,
        fingerprint_content_hash: str | None = None,
        endpoint_alignment_tolerance_seconds: float = 0.0,
    ) -> dict[str, Any]:
        assembled = self.assemble_experience_for_correction(
            correction_case_id,
            fingerprint_content_hash=fingerprint_content_hash,
            endpoint_alignment_tolerance_seconds=endpoint_alignment_tolerance_seconds,
        )
        saved = self.save_experience_candidate(assembled)
        return {**saved, "candidate": assembled}

    def get_pattern_dna(
        self, experience_case_id: str, *, dna_kind: str | None = None
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        if dna_kind is not None and dna_kind not in DNA_KINDS:
            raise ValueError(f"DNA kind must be one of: {', '.join(DNA_KINDS)}.")
        with self.connect() as connection:
            if dna_kind is None:
                rows = connection.execute(
                    """
                    SELECT * FROM pattern_dna WHERE experience_case_id = ?
                    ORDER BY CASE dna_kind
                        WHEN 'endpoint' THEN 1
                        WHEN 'confirmation' THEN 2
                        WHEN 'resolved_outcome' THEN 3 ELSE 4 END
                    """,
                    (experience_case_id,),
                ).fetchall()
                return [self._pattern_dna_row(row) for row in rows]
            row = connection.execute(
                "SELECT * FROM pattern_dna WHERE experience_case_id = ? AND dna_kind = ?",
                (experience_case_id, dna_kind),
            ).fetchone()
        return self._pattern_dna_row(row) if row is not None else None

    def get_experience_review_history(
        self, experience_case_id: str
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM experience_reviews WHERE experience_case_id = ?
                ORDER BY rowid
                """,
                (experience_case_id,),
            ).fetchall()
        return [self._experience_review_row(row) for row in rows]

    def get_experience_case_status(self, experience_case_id: str) -> dict[str, Any]:
        record = self.get_experience_case_record(experience_case_id)
        if record is None:
            raise KeyError(f"Experience case {experience_case_id} does not exist.")
        case = record["experience_case"]
        proposed = case.get("proposed_quality") if isinstance(case.get("proposed_quality"), Mapping) else {}
        state = "pending_experience_review"
        quality = str(proposed.get("status") or "low")
        current_review: dict[str, Any] | None = None
        rationale: list[str] = []
        for review in self.get_experience_review_history(experience_case_id):
            current_review = review
            action = str(review.get("effective_action") or review.get("action") or "")
            if action == "accept":
                state = "accepted"
            elif action == "reject":
                state = "rejected"
            elif action == "quarantine":
                state = "quarantined"
            if review.get("quality_status") in QUALITY_STATUSES:
                quality = str(review["quality_status"])
            if review.get("rationale"):
                rationale.append(str(review["rationale"]))
        latest = self._latest_experience_version(record["market_episode_id"])
        active = latest is not None and latest["experience_case_id"] == experience_case_id
        if not active:
            state = "superseded"
        accepted_pool_eligible = active and state == "accepted" and quality != "quarantined"
        return {
            "experience_case_id": experience_case_id,
            "market_episode_id": record["market_episode_id"],
            "case_version": int(record["case_version"]),
            "state": state,
            "quality_status": quality,
            "active_version": active,
            "accepted_pool_eligible": accepted_pool_eligible,
            "current_review_id": current_review.get("review_id") if current_review else None,
            "rationale_history": rationale,
            "superseded_by_case_id": (
                latest["experience_case_id"] if latest is not None and not active else None
            ),
        }

    def _insert_experience_review_in_connection(
        self,
        connection: sqlite3.Connection,
        review: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = deepcopy(dict(review))
        if payload.get("content_hash") != experience_review_content_hash(payload):
            raise ValueError("Experience review content_hash does not match its canonical payload.")
        experience_case_id = str(payload.get("experience_case_id") or "")
        case = connection.execute(
            "SELECT experience_case_id FROM experience_cases WHERE experience_case_id = ?",
            (experience_case_id,),
        ).fetchone()
        if case is None:
            raise KeyError("Experience review references a missing experience case.")
        parent_id = payload.get("parent_review_id")
        if parent_id:
            parent = connection.execute(
                "SELECT review_id FROM experience_reviews "
                "WHERE review_id = ? AND experience_case_id = ?",
                (parent_id, experience_case_id),
            ).fetchone()
            if parent is None:
                raise ValueError("Experience review parent does not belong to this case.")
        existing = connection.execute(
            "SELECT review_id, content_hash FROM experience_reviews WHERE review_id = ? OR content_hash = ?",
            (payload["review_id"], payload["content_hash"]),
        ).fetchone()
        if existing is not None:
            if existing["review_id"] != payload["review_id"] or existing[
                "content_hash"
            ] != payload["content_hash"]:
                raise ValueError("Experience review ID or hash conflicts with stored content.")
            return {**payload, "inserted": False}
        connection.execute(
            """
            INSERT INTO experience_reviews(
                review_id, experience_case_id, parent_review_id,
                source_outcome_review_id, action, effective_action,
                reviewer, human_confirmed, quality_status, content_hash,
                review_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["review_id"],
                payload["experience_case_id"],
                payload.get("parent_review_id"),
                payload.get("source_outcome_review_id"),
                payload["action"],
                payload["effective_action"],
                payload["reviewer"],
                int(bool(payload["human_confirmed"])),
                payload.get("quality_status"),
                payload["content_hash"],
                self._canonical_payload(payload),
                payload["reviewed_at"],
            ),
        )
        return {**payload, "inserted": True}

    def _insert_experience_review(
        self, review: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self.connect() as connection:
            return self._insert_experience_review_in_connection(connection, review)

    def review_experience_case(
        self,
        experience_case_id: str,
        *,
        action: str,
        reviewer: str,
        rationale: str,
        quality_status: str | None = None,
        human_confirmed: bool = False,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"accept", "reject", "quarantine"}:
            raise ValueError("Experience review action must be accept, reject, or quarantine.")
        record = self.get_experience_case_record(experience_case_id)
        if record is None:
            raise KeyError(f"Experience case {experience_case_id} does not exist.")
        status = self.get_experience_case_status(experience_case_id)
        if not status["active_version"]:
            raise ValueError("A superseded experience version cannot receive a new effective review.")
        history = self.get_experience_review_history(experience_case_id)
        if action == "accept" and quality_status is None:
            quality_status = status["quality_status"]
        if action == "accept" and quality_status == "quarantined":
            raise ValueError("A quarantined-quality case cannot enter the accepted pool.")
        review = build_experience_review(
            experience_case_id=experience_case_id,
            action=action,
            reviewer=reviewer,
            rationale=rationale,
            reviewed_at=reviewed_at or _utc_now(),
            quality_status=quality_status,
            parent_review_id=history[-1]["review_id"] if history else None,
            source_outcome_review_id=record["source_outcome_review_id"],
            human_confirmed=human_confirmed,
        )
        stored = self._insert_experience_review(review)
        return {**stored, "effective_status": self.get_experience_case_status(experience_case_id)}

    def revise_experience_review(
        self,
        experience_case_id: str,
        *,
        parent_review_id: str,
        effective_action: str,
        reviewer: str,
        rationale: str,
        quality_status: str | None = None,
        human_confirmed: bool = False,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        record = self.get_experience_case_record(experience_case_id)
        if record is None:
            raise KeyError(f"Experience case {experience_case_id} does not exist.")
        history = self.get_experience_review_history(experience_case_id)
        if not history or history[-1].get("review_id") != parent_review_id:
            raise ValueError("A revision must name the current effective review as its parent.")
        review = build_experience_review(
            experience_case_id=experience_case_id,
            action="revise",
            effective_action=effective_action,
            reviewer=reviewer,
            rationale=rationale,
            reviewed_at=reviewed_at or _utc_now(),
            quality_status=quality_status,
            parent_review_id=parent_review_id,
            source_outcome_review_id=record["source_outcome_review_id"],
            human_confirmed=human_confirmed,
        )
        stored = self._insert_experience_review(review)
        return {**stored, "effective_status": self.get_experience_case_status(experience_case_id)}

    def assign_experience_quality(
        self,
        experience_case_id: str,
        *,
        quality_status: str,
        reviewer: str,
        rationale: str,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        if quality_status not in QUALITY_STATUSES:
            raise ValueError("Quality must be high, medium, low, or quarantined.")
        record = self.get_experience_case_record(experience_case_id)
        if record is None:
            raise KeyError(f"Experience case {experience_case_id} does not exist.")
        history = self.get_experience_review_history(experience_case_id)
        review = build_experience_review(
            experience_case_id=experience_case_id,
            action="assign_quality",
            reviewer=reviewer,
            rationale=rationale,
            reviewed_at=reviewed_at or _utc_now(),
            quality_status=quality_status,
            parent_review_id=history[-1]["review_id"] if history else None,
            source_outcome_review_id=record["source_outcome_review_id"],
            human_confirmed=False,
        )
        stored = self._insert_experience_review(review)
        return {**stored, "effective_status": self.get_experience_case_status(experience_case_id)}

    def add_experience_rationale(
        self,
        experience_case_id: str,
        *,
        reviewer: str,
        rationale: str,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        record = self.get_experience_case_record(experience_case_id)
        if record is None:
            raise KeyError(f"Experience case {experience_case_id} does not exist.")
        history = self.get_experience_review_history(experience_case_id)
        review = build_experience_review(
            experience_case_id=experience_case_id,
            action="add_rationale",
            reviewer=reviewer,
            rationale=rationale,
            reviewed_at=reviewed_at or _utc_now(),
            parent_review_id=history[-1]["review_id"] if history else None,
            source_outcome_review_id=record["source_outcome_review_id"],
            human_confirmed=False,
        )
        stored = self._insert_experience_review(review)
        return {**stored, "effective_status": self.get_experience_case_status(experience_case_id)}

    def tag_experience_case(
        self,
        experience_case_id: str,
        *,
        tag: str,
        action: str,
        actor: str,
        rationale: str = "",
        assigned_at: str | None = None,
    ) -> dict[str, Any]:
        if self.get_experience_case_record(experience_case_id) is None:
            raise KeyError(f"Experience case {experience_case_id} does not exist.")
        tag_payload = build_experience_tag(tag)
        if tag_payload.get("content_hash") != tag_content_hash(tag_payload):
            raise ValueError("Experience tag hash is invalid.")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO experience_tags(
                    tag_id, namespace, value, tag_key, schema_version,
                    content_hash, tag_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tag_payload["tag_id"],
                    tag_payload["namespace"],
                    tag_payload["value"],
                    tag_payload["tag"],
                    tag_payload["schema_version"],
                    tag_payload["content_hash"],
                    self._canonical_payload(tag_payload),
                    _utc_now(),
                ),
            )
            prior = connection.execute(
                """
                SELECT * FROM experience_case_tags
                WHERE experience_case_id = ? AND tag_id = ?
                ORDER BY revision DESC, rowid DESC LIMIT 1
                """,
                (experience_case_id, tag_payload["tag_id"]),
            ).fetchone()
            if action == "remove" and (prior is None or prior["action"] != "add"):
                raise ValueError("A tag can be removed only after an active add action.")
            if action == "add" and prior is not None and prior["action"] == "add":
                return {
                    "inserted": False,
                    "experience_case_id": experience_case_id,
                    "tag": tag_payload["tag"],
                    "action": "add",
                    "assignment_id": prior["assignment_id"],
                    "revision": int(prior["revision"]),
                }
            revision = int(prior["revision"]) + 1 if prior is not None else 1
            assignment = build_tag_assignment(
                experience_case_id=experience_case_id,
                tag=tag_payload,
                action=action,
                actor=actor,
                rationale=rationale,
                assigned_at=assigned_at or _utc_now(),
                parent_assignment_id=prior["assignment_id"] if prior is not None else None,
                revision=revision,
            )
            if assignment.get("content_hash") != tag_assignment_content_hash(assignment):
                raise ValueError("Experience tag-assignment hash is invalid.")
            connection.execute(
                """
                INSERT INTO experience_case_tags(
                    assignment_id, experience_case_id, tag_id, action,
                    revision, parent_assignment_id, actor, content_hash,
                    assignment_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment["assignment_id"],
                    experience_case_id,
                    tag_payload["tag_id"],
                    action,
                    revision,
                    assignment.get("parent_assignment_id"),
                    actor,
                    assignment["content_hash"],
                    self._canonical_payload(assignment),
                    assignment["assigned_at"],
                ),
            )
        return {**assignment, "inserted": True}

    def get_experience_tags(self, experience_case_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM experience_case_tags
                WHERE experience_case_id = ? ORDER BY rowid
                """,
                (experience_case_id,),
            ).fetchall()
        history = [self._tag_assignment_row(row) for row in rows]
        current: dict[str, dict[str, Any]] = {}
        for item in history:
            current[str(item["tag"])] = item
        return {
            "active_tags": sorted(
                tag for tag, item in current.items() if item.get("action") == "add"
            ),
            "history": history,
        }

    def inspect_experience_case(self, experience_case_id: str) -> dict[str, Any]:
        record = self.get_experience_case_record(experience_case_id)
        if record is None:
            raise KeyError(f"Experience case {experience_case_id} does not exist.")
        with self.connect() as connection:
            versions = connection.execute(
                """
                SELECT experience_case_id, case_version, supersedes_case_id,
                       source_outcome_id, source_outcome_review_id, content_hash
                FROM experience_cases WHERE market_episode_id = ?
                ORDER BY case_version
                """,
                (record["market_episode_id"],),
            ).fetchall()
        dna_rows = self.get_pattern_dna(experience_case_id)
        assert isinstance(dna_rows, list)
        return {
            "experience_case": record["experience_case"],
            "storage": {
                key: value
                for key, value in record.items()
                if key != "experience_case"
            },
            "effective_status": self.get_experience_case_status(experience_case_id),
            "review_lineage": self.get_experience_review_history(experience_case_id),
            "pattern_dna": {
                str(item["dna_kind"]): item["dna"] for item in dna_rows
            },
            "tags": self.get_experience_tags(experience_case_id),
            "episode_versions": [dict(row) for row in versions],
        }

    def list_experience_cases(
        self, *, state: str = "all", active_only: bool = True, limit: int = 100
    ) -> list[dict[str, Any]]:
        allowed = {"all", "pending_experience_review", "accepted", "rejected", "quarantined", "superseded"}
        if state not in allowed:
            raise ValueError(f"Unsupported experience state: {state!r}")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT experience_case_id FROM experience_cases ORDER BY created_at, rowid"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            status = self.get_experience_case_status(str(row["experience_case_id"]))
            if active_only and not status["active_version"]:
                continue
            if state != "all" and status["state"] != state:
                continue
            record = self.get_experience_case_record(status["experience_case_id"])
            assert record is not None
            case = record["experience_case"]
            result.append(
                {
                    **status,
                    "symbol": case.get("symbol"),
                    "timeframe": case.get("timeframe"),
                    "elliott_degree": case.get("elliott_degree"),
                    "candidate_role": case.get("candidate_role"),
                    "parent_pattern_family": case.get("parent_pattern_family"),
                    "endpoint_timestamp": case.get("endpoint_timestamp"),
                }
            )
            if len(result) >= max(1, limit):
                break
        return result

    def get_accepted_experience_pool(self, *, limit: int = 100_000) -> list[dict[str, Any]]:
        return [
            item
            for item in self.list_experience_cases(
                state="accepted", active_only=True, limit=limit
            )
            if item["accepted_pool_eligible"]
        ]

    @staticmethod
    def _workflow_case_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["workflow_case"] = json.loads(result.pop("draft_json"))
        return result

    @staticmethod
    def _workflow_event_row(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["event_json"])

    def _workflow_events_in_connection(
        self, connection: sqlite3.Connection, workflow_case_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM experience_workflow_events "
            "WHERE workflow_case_id = ? ORDER BY sequence_number, rowid",
            (workflow_case_id,),
        ).fetchall()
        return [self._workflow_event_row(row) for row in rows]

    def _workflow_case_in_connection(
        self, connection: sqlite3.Connection, workflow_case_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM experience_workflow_cases WHERE workflow_case_id = ?",
            (workflow_case_id,),
        ).fetchone()
        return self._workflow_case_row(row) if row is not None else None

    def get_experience_workflow_case(
        self, workflow_case_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._workflow_case_in_connection(connection, workflow_case_id)

    def get_experience_workflow_events(
        self, workflow_case_id: str
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if self._workflow_case_in_connection(connection, workflow_case_id) is None:
                raise KeyError(f"Workflow case {workflow_case_id} does not exist.")
            return self._workflow_events_in_connection(connection, workflow_case_id)

    def _workflow_source_bundle(
        self,
        connection: sqlite3.Connection,
        source_reference: str,
        *,
        fingerprint_id: int | None = None,
        fingerprint_content_hash_value: str | None = None,
    ) -> dict[str, Any]:
        case_row = connection.execute(
            "SELECT * FROM correction_cases WHERE case_id = ?",
            (source_reference,),
        ).fetchone()
        snapshot_row = None
        if case_row is not None:
            snapshot_row = connection.execute(
                "SELECT * FROM hypothesis_snapshots "
                "WHERE case_id = ? AND parent_snapshot_id IS NULL "
                "ORDER BY sequence_number, rowid LIMIT 1",
                (source_reference,),
            ).fetchone()
        else:
            snapshot_row = connection.execute(
                "SELECT * FROM hypothesis_snapshots WHERE snapshot_id = ?",
                (source_reference,),
            ).fetchone()
            if snapshot_row is None:
                raise KeyError(
                    f"No correction case or endpoint snapshot matches {source_reference}."
                )
            case_row = connection.execute(
                "SELECT * FROM correction_cases WHERE case_id = ?",
                (snapshot_row["case_id"],),
            ).fetchone()
        if case_row is None or snapshot_row is None:
            raise ValueError("The source has no immutable correction case and endpoint snapshot pair.")
        if snapshot_row["parent_snapshot_id"] is not None:
            raise ValueError("Workflow drafts require the root endpoint snapshot, not a later snapshot.")
        case = json.loads(case_row["case_json"])
        snapshot = json.loads(snapshot_row["snapshot_json"])
        references = snapshot.get("fingerprint_references")
        references = references if isinstance(references, list) else []
        referenced_hashes = {
            str(item.get("content_hash"))
            for item in references
            if isinstance(item, Mapping) and item.get("content_hash")
        }
        if fingerprint_id is not None:
            fingerprint_row = connection.execute(
                "SELECT * FROM wave_fingerprints WHERE id = ?",
                (int(fingerprint_id),),
            ).fetchone()
        elif fingerprint_content_hash_value:
            fingerprint_row = connection.execute(
                "SELECT * FROM wave_fingerprints WHERE content_hash = ?",
                (fingerprint_content_hash_value,),
            ).fetchone()
        else:
            matching = connection.execute(
                "SELECT * FROM wave_fingerprints WHERE content_hash IN ("
                + ",".join("?" for _ in referenced_hashes)
                + ") ORDER BY id",
                tuple(sorted(referenced_hashes)),
            ).fetchall() if referenced_hashes else []
            if len(matching) != 1:
                raise ValueError(
                    "The endpoint must reference exactly one stored fingerprint, or a fingerprint selector is required."
                )
            fingerprint_row = matching[0]
        if fingerprint_row is None:
            raise KeyError("The selected Phase 3 fingerprint does not exist.")
        if str(fingerprint_row["content_hash"]) not in referenced_hashes:
            raise ValueError("The selected fingerprint is not referenced by the endpoint snapshot.")
        return {
            "correction_case": case,
            "endpoint_snapshot": snapshot,
            "fingerprint": json.loads(fingerprint_row["fingerprint_json"]),
            "stored_correction_case_hash": str(case_row["content_hash"]),
            "stored_endpoint_snapshot_hash": str(snapshot_row["content_hash"]),
            "stored_fingerprint_hash": str(fingerprint_row["content_hash"]),
            "source_fingerprint_id": int(fingerprint_row["id"]),
        }

    def _workflow_source_bundle_for_record(
        self, connection: sqlite3.Connection, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._workflow_source_bundle(
            connection,
            str(record["source_endpoint_snapshot_id"]),
            fingerprint_id=int(record["source_fingerprint_id"]),
        )

    def _append_workflow_event(
        self,
        connection: sqlite3.Connection,
        workflow_case_id: str,
        *,
        event_kind: str,
        to_state: str,
        actor_reference: str,
        recorded_at: str,
        decision: str | None = None,
        reviewer_reference: str | None = None,
        human_confirmed: bool = False,
        source_outcome_id: str | None = None,
        source_outcome_review_id: str | None = None,
        result_experience_case_id: str | None = None,
        result_experience_review_id: str | None = None,
        event_data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        events = self._workflow_events_in_connection(connection, workflow_case_id)
        chain = validate_event_chain(events)
        if events and not chain["valid"]:
            raise ValueError("Cannot append to an invalid workflow event chain.")
        previous = events[-1] if events else None
        event = build_workflow_event(
            workflow_case_id=workflow_case_id,
            sequence_number=len(events) + 1,
            parent_event_id=str(previous["event_id"]) if previous else None,
            event_kind=event_kind,
            from_state=str(previous["to_state"]) if previous else None,
            to_state=to_state,
            actor_reference=actor_reference,
            recorded_at=recorded_at,
            decision=decision,
            reviewer_reference=reviewer_reference,
            human_confirmed=human_confirmed,
            source_outcome_id=source_outcome_id,
            source_outcome_review_id=source_outcome_review_id,
            result_experience_case_id=result_experience_case_id,
            result_experience_review_id=result_experience_review_id,
            event_data=event_data,
        )
        connection.execute(
            """
            INSERT INTO experience_workflow_events(
                event_id, workflow_case_id, parent_event_id, sequence_number,
                event_kind, from_state, to_state, decision, actor_reference,
                reviewer_reference, human_confirmed, source_outcome_id,
                source_outcome_review_id, result_experience_case_id,
                result_experience_review_id, workflow_spec_version,
                event_schema_version, content_hash, event_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["event_id"],
                event["workflow_case_id"],
                event["parent_event_id"],
                event["sequence_number"],
                event["event_kind"],
                event["from_state"],
                event["to_state"],
                event["decision"],
                event["actor_reference"],
                event["reviewer_reference"],
                int(bool(event["human_confirmed"])),
                event["source_outcome_id"],
                event["source_outcome_review_id"],
                event["result_experience_case_id"],
                event["result_experience_review_id"],
                event["workflow_spec_version"],
                event["event_schema_version"],
                event["content_hash"],
                self._canonical_payload(event),
                event["recorded_at"],
            ),
        )
        return event

    @staticmethod
    def experience_workflow_specification() -> dict[str, Any]:
        return workflow_specification()

    def discover_experience_workflow_candidates(
        self, *, include_invalid: bool = False, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Discover root endpoint/fingerprint pairs without reading outcomes."""
        result: list[dict[str, Any]] = []
        with self.connect() as connection:
            snapshot_rows = connection.execute(
                "SELECT snapshot_id, snapshot_json FROM hypothesis_snapshots "
                "WHERE parent_snapshot_id IS NULL ORDER BY case_id, snapshot_id"
            ).fetchall()
            for snapshot_row in snapshot_rows:
                snapshot = json.loads(snapshot_row["snapshot_json"])
                references = snapshot.get("fingerprint_references")
                references = references if isinstance(references, list) else []
                if not references:
                    if include_invalid:
                        result.append(
                            {
                                "candidate_status": "unavailable",
                                "source_endpoint_snapshot_id": snapshot_row["snapshot_id"],
                                "reason": "The endpoint snapshot references no Phase 3 fingerprint.",
                                "accepted": False,
                            }
                        )
                    continue
                for reference in references:
                    if not isinstance(reference, Mapping):
                        continue
                    fingerprint_hash = str(reference.get("content_hash") or "")
                    fingerprint_row = connection.execute(
                        "SELECT id FROM wave_fingerprints WHERE content_hash = ?",
                        (fingerprint_hash,),
                    ).fetchone()
                    if fingerprint_row is None:
                        if include_invalid:
                            result.append(
                                {
                                    "candidate_status": "unavailable",
                                    "source_endpoint_snapshot_id": snapshot_row["snapshot_id"],
                                    "fingerprint_content_hash": fingerprint_hash,
                                    "reason": "The referenced fingerprint is not stored.",
                                    "accepted": False,
                                }
                            )
                        continue
                    try:
                        bundle = self._workflow_source_bundle(
                            connection,
                            str(snapshot_row["snapshot_id"]),
                            fingerprint_id=int(fingerprint_row["id"]),
                        )
                        episode = market_episode_identity(bundle["correction_case"])
                        previous = connection.execute(
                            "SELECT COALESCE(MAX(workflow_version), 0) AS version "
                            "FROM experience_workflow_cases WHERE market_episode_id = ?",
                            (episode["market_episode_id"],),
                        ).fetchone()
                        draft = assemble_workflow_draft(
                            **bundle,
                            workflow_version=int(previous["version"]) + 1,
                        )
                        existing = connection.execute(
                            "SELECT workflow_case_id FROM experience_workflow_cases "
                            "WHERE source_endpoint_snapshot_id = ? AND source_fingerprint_id = ?",
                            (
                                draft["source_references"]["endpoint_snapshot_id"],
                                draft["source_references"]["fingerprint_id"],
                            ),
                        ).fetchone()
                        candidate = {
                            "candidate_status": (
                                "eligible_for_draft"
                                if draft["validation"]["valid"]
                                else "validation_failed"
                            ),
                            "source_correction_case_id": draft["source_references"]["correction_case_id"],
                            "source_endpoint_snapshot_id": draft["source_references"]["endpoint_snapshot_id"],
                            "source_fingerprint_id": draft["source_references"]["fingerprint_id"],
                            "source_pair_hash": draft["source_pair_hash"],
                            "material_episode_hash": draft["material_episode_hash"],
                            "symbol": draft["identity"]["symbol"],
                            "timeframe": draft["identity"]["timeframe"],
                            "elliott_degree": draft["identity"]["elliott_degree"],
                            "structural_role": draft["identity"]["structural_role"],
                            "validation": draft["validation"],
                            "already_drafted": existing is not None,
                            "workflow_case_id": (
                                str(existing["workflow_case_id"])
                                if existing is not None
                                else draft["workflow_case_id"]
                            ),
                            "accepted": False,
                            "policy": "Discovery is not structural review or acceptance.",
                        }
                        if include_invalid or draft["validation"]["valid"]:
                            result.append(candidate)
                    except (KeyError, TypeError, ValueError) as error:
                        if include_invalid:
                            result.append(
                                {
                                    "candidate_status": "unavailable",
                                    "source_endpoint_snapshot_id": snapshot_row["snapshot_id"],
                                    "source_fingerprint_id": int(fingerprint_row["id"]),
                                    "reason": str(error),
                                    "accepted": False,
                                }
                            )
                    if len(result) >= max(1, limit):
                        return result
        return result

    def create_experience_workflow_draft(
        self,
        source_reference: str,
        *,
        fingerprint_id: int | None = None,
        fingerprint_content_hash_value: str | None = None,
        supersedes_workflow_case_id: str | None = None,
        actor_reference: str = "experience-workflow-service",
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = recorded_at or _utc_now()
        duplicate_id: str | None = None
        workflow_case_id: str | None = None
        with self._immediate_connection() as connection:
            bundle = self._workflow_source_bundle(
                connection,
                source_reference,
                fingerprint_id=fingerprint_id,
                fingerprint_content_hash_value=fingerprint_content_hash_value,
            )
            pair_snapshot_id = str(bundle["endpoint_snapshot"]["snapshot_id"])
            pair_fingerprint_id = int(bundle["source_fingerprint_id"])
            existing = connection.execute(
                "SELECT workflow_case_id FROM experience_workflow_cases "
                "WHERE source_endpoint_snapshot_id = ? AND source_fingerprint_id = ?",
                (pair_snapshot_id, pair_fingerprint_id),
            ).fetchone()
            if existing is not None:
                duplicate_id = str(existing["workflow_case_id"])
            else:
                episode = market_episode_identity(bundle["correction_case"])
                previous_rows = connection.execute(
                    "SELECT workflow_case_id, workflow_version, material_episode_hash "
                    "FROM experience_workflow_cases WHERE market_episode_id = ? "
                    "ORDER BY workflow_version",
                    (episode["market_episode_id"],),
                ).fetchall()
                workflow_version = (
                    int(previous_rows[-1]["workflow_version"]) + 1
                    if previous_rows
                    else 1
                )
                supersedes = None
                if supersedes_workflow_case_id:
                    superseded = connection.execute(
                        "SELECT workflow_case_id, market_episode_id FROM experience_workflow_cases "
                        "WHERE workflow_case_id = ?",
                        (supersedes_workflow_case_id,),
                    ).fetchone()
                    if superseded is None:
                        raise KeyError("The explicitly superseded workflow case does not exist.")
                    if superseded["market_episode_id"] != episode["market_episode_id"]:
                        raise ValueError("A superseding workflow case must describe the same market episode.")
                    supersedes = str(superseded["workflow_case_id"])
                draft = assemble_workflow_draft(
                    **bundle,
                    workflow_version=workflow_version,
                    supersedes_workflow_case_id=supersedes,
                    duplicate_workflow_case_ids=[
                        str(row["workflow_case_id"]) for row in previous_rows
                    ],
                )
                workflow_case_id = str(draft["workflow_case_id"])
                connection.execute(
                    """
                    INSERT INTO experience_workflow_cases(
                        workflow_case_id, market_episode_id, workflow_version,
                        supersedes_workflow_case_id, source_correction_case_id,
                        source_endpoint_snapshot_id, source_fingerprint_id,
                        source_pair_hash, material_episode_hash, schema_version,
                        calculation_version, content_hash, draft_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_case_id,
                        draft["market_episode_id"],
                        draft["workflow_version"],
                        draft["supersedes_workflow_case_id"],
                        draft["source_references"]["correction_case_id"],
                        draft["source_references"]["endpoint_snapshot_id"],
                        draft["source_references"]["fingerprint_id"],
                        draft["source_pair_hash"],
                        draft["material_episode_hash"],
                        draft["schema_version"],
                        draft["calculation_version"],
                        draft["content_hash"],
                        self._canonical_payload(draft),
                        timestamp,
                    ),
                )
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind="candidate_discovery",
                    to_state="discovered",
                    actor_reference=actor_reference,
                    recorded_at=timestamp,
                    event_data={
                        "source_pair_hash": draft["source_pair_hash"],
                        "outcome_loaded": False,
                        "acceptance_effect": None,
                    },
                )
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind="draft_creation",
                    to_state="draft",
                    actor_reference=actor_reference,
                    recorded_at=timestamp,
                    event_data={
                        "draft_content_hash": draft["content_hash"],
                        "missing_field_warnings": draft["missing_field_warnings"],
                    },
                )
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind="draft_assembly",
                    to_state="assembled",
                    actor_reference=actor_reference,
                    recorded_at=timestamp,
                    event_data={
                        "validation_status_at_assembly": draft["validation"]["status"],
                        "technical_validation_accepts_case": False,
                    },
                )
        if duplicate_id is not None:
            inspected = self.inspect_experience_workflow_case(duplicate_id)
            return {
                **inspected,
                "created": False,
                "duplicate_source_pair": True,
            }
        assert workflow_case_id is not None
        return {
            **self.inspect_experience_workflow_case(workflow_case_id),
            "created": True,
            "duplicate_source_pair": False,
        }

    def inspect_experience_workflow_case(
        self, workflow_case_id: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            record = self._workflow_case_in_connection(connection, workflow_case_id)
            if record is None:
                raise KeyError(f"Workflow case {workflow_case_id} does not exist.")
            events = self._workflow_events_in_connection(connection, workflow_case_id)
            chain = validate_event_chain(events)
            draft = record["workflow_case"]
            row_projection_valid = all(
                (
                    record["market_episode_id"] == draft.get("market_episode_id"),
                    int(record["workflow_version"]) == draft.get("workflow_version"),
                    record["source_pair_hash"] == draft.get("source_pair_hash"),
                    record["material_episode_hash"] == draft.get("material_episode_hash"),
                    record["content_hash"] == draft.get("content_hash"),
                    draft.get("content_hash") == workflow_case_content_hash(draft),
                )
            )
            try:
                bundle = self._workflow_source_bundle_for_record(connection, record)
                current_validation = validate_workflow_sources(**bundle)
            except (KeyError, TypeError, ValueError) as error:
                current_validation = {
                    "status": "failed",
                    "valid": False,
                    "checks": [],
                    "hard_exclusions": [
                        {"check_id": "source_load", "message": str(error)}
                    ],
                    "warnings": [],
                }
            result_ids = [
                {
                    "experience_case_id": event.get("result_experience_case_id"),
                    "experience_review_id": event.get("result_experience_review_id"),
                }
                for event in events
                if event.get("event_kind") == "final_acceptance"
            ]
        status = {
            **chain,
            "row_projection_valid": row_projection_valid,
            "source_validation_current": current_validation["status"],
            "final_acceptance_blocked": bool(
                chain["final_acceptance_blocked"]
                or not row_projection_valid
                or not current_validation["valid"]
                or chain["current_state"] != "accepted"
            ),
            "accepted_into_existing_pool": chain["current_state"] == "accepted",
        }
        return {
            "workflow_case": draft,
            "storage": {key: value for key, value in record.items() if key != "workflow_case"},
            "status": status,
            "current_source_validation": current_validation,
            "events": events,
            "result_links": result_ids,
            "policy": (
                "Current state is derived from the validated append-only event chain."
            ),
        }

    def validate_experience_workflow_case(
        self,
        workflow_case_id: str,
        *,
        actor_reference: str = "experience-workflow-validator",
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = recorded_at or _utc_now()
        with self._immediate_connection() as connection:
            record = self._workflow_case_in_connection(connection, workflow_case_id)
            if record is None:
                raise KeyError(f"Workflow case {workflow_case_id} does not exist.")
            events = self._workflow_events_in_connection(connection, workflow_case_id)
            chain = validate_event_chain(events)
            if not chain["valid"]:
                raise ValueError("Workflow event chain is invalid.")
            if chain["current_state"] == "validation_failed":
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind="draft_assembly",
                    to_state="assembled",
                    actor_reference=actor_reference,
                    recorded_at=timestamp,
                    event_data={"purpose": "explicit_revalidation"},
                )
            elif chain["current_state"] != "assembled":
                raise ValueError(
                    "Source validation is permitted only from assembled or validation_failed."
                )
            bundle = self._workflow_source_bundle_for_record(connection, record)
            validation = validate_workflow_sources(**bundle)
            self._append_workflow_event(
                connection,
                workflow_case_id,
                event_kind="source_validation",
                to_state=(
                    "ready_for_structural_review"
                    if validation["valid"]
                    else "validation_failed"
                ),
                actor_reference=actor_reference,
                recorded_at=timestamp,
                decision=("validation_passed" if validation["valid"] else "validation_failed"),
                event_data={
                    "validation": validation,
                    "automatic_acceptance": False,
                },
            )
        return self.inspect_experience_workflow_case(workflow_case_id)

    @staticmethod
    def _latest_workflow_review_event(
        events: Sequence[Mapping[str, Any]], review_version: str
    ) -> dict[str, Any] | None:
        for event in reversed(events):
            event_data = event.get("event_data")
            event_data = event_data if isinstance(event_data, Mapping) else {}
            review = event_data.get("review")
            if isinstance(review, Mapping) and review.get("review_version") == review_version:
                return dict(event)
        return None

    @staticmethod
    def _review_from_event(event: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(event, Mapping):
            return None
        event_data = event.get("event_data")
        event_data = event_data if isinstance(event_data, Mapping) else {}
        review = event_data.get("review")
        return deepcopy(dict(review)) if isinstance(review, Mapping) else None

    @staticmethod
    def _require_disagreement_references(
        chain: Mapping[str, Any], review: Mapping[str, Any]
    ) -> None:
        unresolved = {
            str(item) for item in chain.get("unresolved_disagreement_event_ids", [])
        }
        referenced = {str(item) for item in review.get("resolves_event_ids", [])}
        if not unresolved or not unresolved.issubset(referenced):
            raise ValueError(
                "A disagreement resolution must reference every unresolved conflicting review event."
            )

    def review_experience_workflow_structure(
        self,
        workflow_case_id: str,
        *,
        decision: str,
        reviewer: str,
        review: Mapping[str, Any],
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = reviewed_at or _utc_now()
        review_block = build_structural_review_block(
            review,
            decision=decision,
            reviewer=reviewer,
            reviewed_at=timestamp,
        )
        target_state = STRUCTURAL_DECISION_STATE[decision]
        with self._immediate_connection() as connection:
            record = self._workflow_case_in_connection(connection, workflow_case_id)
            if record is None:
                raise KeyError(f"Workflow case {workflow_case_id} does not exist.")
            events = self._workflow_events_in_connection(connection, workflow_case_id)
            chain = validate_event_chain(events)
            if not chain["valid"]:
                raise ValueError("Workflow event chain is invalid.")
            state = str(chain["current_state"])
            if state == "structural_review_ambiguous":
                self._require_disagreement_references(chain, review_block)
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind="disagreement_resolution",
                    to_state=target_state,
                    actor_reference=reviewer,
                    recorded_at=timestamp,
                    decision=decision,
                    reviewer_reference=reviewer,
                    human_confirmed=True,
                    event_data={
                        "review_stage": "structural",
                        "review": review_block,
                        "resolution_policy": "Explicit human resolution; no majority vote.",
                    },
                )
            else:
                if state not in {
                    "ready_for_structural_review",
                    "structural_review_accepted",
                    "structural_review_rejected",
                    "structural_review_needs_revision",
                    "ready_for_outcome_review",
                    "outcome_review_accepted",
                    "ready_for_final_acceptance",
                }:
                    raise ValueError(
                        f"Structural review cannot start from workflow state {state!r}."
                    )
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind="stage_transition",
                    to_state="structural_review_in_progress",
                    actor_reference="experience-workflow-service",
                    recorded_at=timestamp,
                    event_data={"review_stage": "structural", "reviewer": reviewer},
                )
                prior_event = self._latest_workflow_review_event(
                    events, STRUCTURAL_REVIEW_VERSION
                )
                prior_review = self._review_from_event(prior_event)
                conflict = bool(
                    prior_review
                    and prior_review.get("decision") in {"accepted", "rejected"}
                    and decision in {"accepted", "rejected"}
                    and prior_review.get("decision") != decision
                )
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind=("review_disagreement" if conflict else "structural_review"),
                    to_state=("structural_review_ambiguous" if conflict else target_state),
                    actor_reference=reviewer,
                    recorded_at=timestamp,
                    decision=("structurally_ambiguous" if conflict else decision),
                    reviewer_reference=reviewer,
                    human_confirmed=True,
                    event_data={
                        "review_stage": "structural",
                        "review": review_block,
                        "conflicting_review_event_ids": (
                            [prior_event["event_id"]] if conflict and prior_event else []
                        ),
                        "automatic_resolution": False,
                    },
                )
        return self.inspect_experience_workflow_case(workflow_case_id)

    def _current_phase4_outcome_in_connection(
        self, connection: sqlite3.Connection, case_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        outcome_row = connection.execute(
            "SELECT * FROM resolved_outcomes WHERE case_id = ? "
            "ORDER BY revision DESC, rowid DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if outcome_row is None:
            return None, None, "No Phase 4 reviewed outcome exists."
        outcome = json.loads(outcome_row["outcome_json"])
        review_row = connection.execute(
            "SELECT * FROM outcome_reviews WHERE outcome_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (outcome_row["outcome_id"],),
        ).fetchone()
        if review_row is None:
            return outcome, None, "The latest Phase 4 outcome has no review."
        review = json.loads(review_row["review_json"])
        if review.get("action") not in {"approved", "revised"} or review.get(
            "review_status"
        ) not in {"reviewed", "approved", "resolved"}:
            return outcome, review, "The latest Phase 4 outcome review is not accepted."
        return outcome, review, None

    def review_experience_workflow_outcome(
        self,
        workflow_case_id: str,
        *,
        decision: str,
        reviewer: str,
        review: Mapping[str, Any],
        horizon_id: str = DEFAULT_HORIZON_ID,
        horizon_version: str = DEFAULT_HORIZON_VERSION,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = reviewed_at or _utc_now()
        with self._immediate_connection() as connection:
            record = self._workflow_case_in_connection(connection, workflow_case_id)
            if record is None:
                raise KeyError(f"Workflow case {workflow_case_id} does not exist.")
            events = self._workflow_events_in_connection(connection, workflow_case_id)
            chain = validate_event_chain(events)
            if not chain["valid"]:
                raise ValueError("Workflow event chain is invalid.")
            latest_structural_event = self._latest_workflow_review_event(
                events, STRUCTURAL_REVIEW_VERSION
            )
            latest_structural = self._review_from_event(latest_structural_event)
            if not latest_structural or latest_structural.get("decision") != "accepted":
                raise ValueError("Outcome review requires an accepted structural review.")
            correction_case_id = str(record["source_correction_case_id"])
            outcome, outcome_review, outcome_error = (
                self._current_phase4_outcome_in_connection(
                    connection, correction_case_id
                )
            )
            if decision == "accepted" and (outcome is None or outcome_review is None):
                raise ValueError(outcome_error or "An accepted Phase 4 outcome is required.")
            resolved_hypothesis = str(
                (outcome or {}).get("resolved_hypothesis") or "unresolved"
            )
            review_block = build_outcome_review_block(
                review,
                decision=decision,
                reviewer=reviewer,
                reviewed_at=timestamp,
                resolved_hypothesis=resolved_hypothesis,
                horizon_id=horizon_id,
                horizon_version=horizon_version,
            )
            target_state = OUTCOME_DECISION_STATE[decision]
            state = str(chain["current_state"])
            if state == "outcome_review_ambiguous":
                self._require_disagreement_references(chain, review_block)
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind="disagreement_resolution",
                    to_state=target_state,
                    actor_reference=reviewer,
                    recorded_at=timestamp,
                    decision=decision,
                    reviewer_reference=reviewer,
                    human_confirmed=True,
                    source_outcome_id=(str(outcome["outcome_id"]) if outcome else None),
                    source_outcome_review_id=(
                        str(outcome_review["review_id"]) if outcome_review else None
                    ),
                    event_data={
                        "review_stage": "outcome",
                        "review": review_block,
                        "resolution_policy": "Explicit human resolution; no majority vote.",
                    },
                )
            else:
                if state == "structural_review_accepted":
                    self._append_workflow_event(
                        connection,
                        workflow_case_id,
                        event_kind="stage_transition",
                        to_state="ready_for_outcome_review",
                        actor_reference="experience-workflow-service",
                        recorded_at=timestamp,
                        event_data={"structural_review_event_id": latest_structural_event["event_id"]},
                    )
                    state = "ready_for_outcome_review"
                if state not in {
                    "ready_for_outcome_review",
                    "outcome_review_accepted",
                    "outcome_review_rejected",
                    "outcome_review_needs_revision",
                }:
                    raise ValueError(
                        f"Outcome review cannot start from workflow state {state!r}."
                    )
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind="stage_transition",
                    to_state="outcome_review_in_progress",
                    actor_reference="experience-workflow-service",
                    recorded_at=timestamp,
                    event_data={"review_stage": "outcome", "reviewer": reviewer},
                )
                prior_event = self._latest_workflow_review_event(
                    events, OUTCOME_REVIEW_VERSION
                )
                prior_review = self._review_from_event(prior_event)
                conflict = bool(
                    prior_review
                    and prior_review.get("decision") in {"accepted", "rejected"}
                    and decision in {"accepted", "rejected"}
                    and prior_review.get("decision") != decision
                )
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind=("review_disagreement" if conflict else "outcome_review"),
                    to_state=("outcome_review_ambiguous" if conflict else target_state),
                    actor_reference=reviewer,
                    recorded_at=timestamp,
                    decision=("structurally_ambiguous" if conflict else decision),
                    reviewer_reference=reviewer,
                    human_confirmed=True,
                    source_outcome_id=(str(outcome["outcome_id"]) if outcome else None),
                    source_outcome_review_id=(
                        str(outcome_review["review_id"]) if outcome_review else None
                    ),
                    event_data={
                        "review_stage": "outcome",
                        "review": review_block,
                        "outcome_error": outcome_error,
                        "conflicting_review_event_ids": (
                            [prior_event["event_id"]] if conflict and prior_event else []
                        ),
                        "automatic_resolution": False,
                    },
                )
        return self.inspect_experience_workflow_case(workflow_case_id)

    def _workflow_acceptance_prerequisites(
        self, connection: sqlite3.Connection, workflow_case_id: str
    ) -> dict[str, Any]:
        record = self._workflow_case_in_connection(connection, workflow_case_id)
        if record is None:
            raise KeyError(f"Workflow case {workflow_case_id} does not exist.")
        draft = record["workflow_case"]
        events = self._workflow_events_in_connection(connection, workflow_case_id)
        chain = validate_event_chain(events)
        checks: list[dict[str, Any]] = []

        def add(check_id: str, passed: bool, message: str) -> None:
            checks.append(
                {"check_id": check_id, "passed": bool(passed), "message": message}
            )

        add("event_chain", chain["valid"], "The append-only event chain must validate.")
        add(
            "no_unresolved_disagreement",
            not chain["unresolved_disagreement_event_ids"],
            "Every conflicting review must have an explicit referenced resolution.",
        )
        add(
            "workflow_case_hash",
            bool(
                draft.get("content_hash") == workflow_case_content_hash(draft)
                and record["content_hash"] == draft.get("content_hash")
            ),
            "The workflow case row and canonical draft hash must agree.",
        )
        try:
            bundle = self._workflow_source_bundle_for_record(connection, record)
            source_validation = validate_workflow_sources(**bundle)
        except (KeyError, TypeError, ValueError) as error:
            bundle = None
            source_validation = {
                "valid": False,
                "status": "failed",
                "hard_exclusions": [{"check_id": "source_load", "message": str(error)}],
            }
        add(
            "immutable_sources",
            bool(source_validation["valid"]),
            "All Phase 3/4 source hashes, provenance, linkage, and cutoffs must validate.",
        )
        structural_event = self._latest_workflow_review_event(
            events, STRUCTURAL_REVIEW_VERSION
        )
        structural_review = self._review_from_event(structural_event)
        add(
            "structural_review_accepted",
            bool(structural_review and structural_review.get("decision") == "accepted"),
            "A separate named-human structural review must be accepted.",
        )
        outcome_event = self._latest_workflow_review_event(events, OUTCOME_REVIEW_VERSION)
        workflow_outcome_review = self._review_from_event(outcome_event)
        add(
            "outcome_review_accepted",
            bool(
                workflow_outcome_review
                and workflow_outcome_review.get("decision") == "accepted"
            ),
            "A separate named-human historical outcome review must be accepted.",
        )
        outcome, outcome_review, outcome_error = (
            self._current_phase4_outcome_in_connection(
                connection, str(record["source_correction_case_id"])
            )
        )
        add(
            "current_phase4_outcome",
            outcome is not None and outcome_review is not None,
            outcome_error or "The current Phase 4 outcome and review are accepted.",
        )
        add(
            "outcome_event_linkage",
            bool(
                outcome_event
                and outcome
                and outcome_review
                and outcome_event.get("source_outcome_id") == outcome.get("outcome_id")
                and outcome_event.get("source_outcome_review_id")
                == outcome_review.get("review_id")
            ),
            "The workflow outcome review must link to the current Phase 4 outcome revision and review.",
        )
        add(
            "state_ready",
            chain["current_state"]
            in {"outcome_review_accepted", "ready_for_final_acceptance"},
            "The workflow must finish outcome review before final acceptance.",
        )
        return {
            "workflow_case_id": workflow_case_id,
            "eligible_for_final_acceptance": all(item["passed"] for item in checks),
            "checks": checks,
            "current_state": chain["current_state"],
            "event_chain": chain,
            "source_validation": source_validation,
            "structural_review_event_id": (
                structural_event.get("event_id") if structural_event else None
            ),
            "outcome_review_event_id": outcome_event.get("event_id") if outcome_event else None,
            "source_outcome": outcome,
            "source_outcome_review": outcome_review,
            "source_bundle": bundle,
            "policy": (
                "Technical validity and historical outcome direction cannot replace explicit final human acceptance."
            ),
        }

    def experience_workflow_acceptance_status(
        self, workflow_case_id: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            return self._workflow_acceptance_prerequisites(
                connection, workflow_case_id
            )

    def _snapshot_lineage_in_connection(
        self, connection: sqlite3.Connection, snapshot_id: str
    ) -> list[dict[str, Any]]:
        lineage: list[dict[str, Any]] = []
        current_id: str | None = snapshot_id
        seen: set[str] = set()
        while current_id:
            if current_id in seen:
                raise ValueError("Phase 4 snapshot lineage contains a cycle.")
            seen.add(current_id)
            row = connection.execute(
                "SELECT parent_snapshot_id, snapshot_json FROM hypothesis_snapshots "
                "WHERE snapshot_id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Snapshot {current_id} does not exist.")
            lineage.append(json.loads(row["snapshot_json"]))
            current_id = (
                str(row["parent_snapshot_id"])
                if row["parent_snapshot_id"] is not None
                else None
            )
        lineage.reverse()
        return lineage

    def accept_experience_workflow_case(
        self,
        workflow_case_id: str,
        *,
        reviewer: str,
        rationale: str,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        if not str(reviewer).strip() or not str(rationale).strip():
            raise ValueError("Final acceptance requires a named reviewer and rationale.")
        timestamp = reviewed_at or _utc_now()
        result_experience_case_id: str | None = None
        result_experience_review_id: str | None = None
        with self._immediate_connection() as connection:
            record = self._workflow_case_in_connection(connection, workflow_case_id)
            if record is None:
                raise KeyError(f"Workflow case {workflow_case_id} does not exist.")
            events = self._workflow_events_in_connection(connection, workflow_case_id)
            chain = validate_event_chain(events)
            if chain["current_state"] == "outcome_review_accepted":
                self._append_workflow_event(
                    connection,
                    workflow_case_id,
                    event_kind="stage_transition",
                    to_state="ready_for_final_acceptance",
                    actor_reference="experience-workflow-service",
                    recorded_at=timestamp,
                    event_data={
                        "final_acceptance_version": FINAL_ACCEPTANCE_VERSION,
                        "automatic_acceptance": False,
                    },
                )
            prerequisites = self._workflow_acceptance_prerequisites(
                connection, workflow_case_id
            )
            if not prerequisites["eligible_for_final_acceptance"]:
                failed = [
                    item["check_id"]
                    for item in prerequisites["checks"]
                    if not item["passed"]
                ]
                raise ValueError(
                    "Final acceptance prerequisites failed: " + ", ".join(failed)
                )
            bundle = prerequisites["source_bundle"]
            assert isinstance(bundle, Mapping)
            outcome = prerequisites["source_outcome"]
            outcome_review = prerequisites["source_outcome_review"]
            assert isinstance(outcome, Mapping) and isinstance(outcome_review, Mapping)
            lineage_snapshot_id = str(
                outcome.get("review_snapshot_id")
                or outcome.get("selected_snapshot_id")
                or bundle["endpoint_snapshot"].get("snapshot_id")
            )
            lineage = self._snapshot_lineage_in_connection(
                connection, lineage_snapshot_id
            )
            assembled = assemble_experience_candidate(
                correction_case=bundle["correction_case"],
                endpoint_snapshot=bundle["endpoint_snapshot"],
                fingerprint=bundle["fingerprint"],
                outcome=outcome,
                outcome_review=outcome_review,
                snapshot_lineage=lineage,
                outcome_is_current=True,
            )
            assembled["source_fingerprint_id"] = int(bundle["source_fingerprint_id"])
            if assembled.get("state") != "assembled_candidate":
                raise ValueError(
                    "The existing Phase 5A.1 assembler rejected the final source set."
                )
            saved = self._save_experience_candidate_in_connection(
                connection, assembled
            )
            result_experience_case_id = str(saved["experience_case_id"])
            parent_row = connection.execute(
                "SELECT review_id FROM experience_reviews "
                "WHERE experience_case_id = ? ORDER BY rowid DESC LIMIT 1",
                (result_experience_case_id,),
            ).fetchone()
            proposed_quality = (
                assembled["experience_case"].get("proposed_quality")
                if isinstance(assembled.get("experience_case"), Mapping)
                else {}
            )
            quality_status = (
                str(proposed_quality.get("status") or "low")
                if isinstance(proposed_quality, Mapping)
                else "low"
            )
            legacy_review = build_experience_review(
                experience_case_id=result_experience_case_id,
                action="accept",
                reviewer=reviewer,
                rationale=rationale,
                reviewed_at=timestamp,
                quality_status=quality_status,
                parent_review_id=(
                    str(parent_row["review_id"]) if parent_row is not None else None
                ),
                source_outcome_review_id=str(outcome_review["review_id"]),
                human_confirmed=True,
            )
            stored_review = self._insert_experience_review_in_connection(
                connection, legacy_review
            )
            result_experience_review_id = str(stored_review["review_id"])
            self._append_workflow_event(
                connection,
                workflow_case_id,
                event_kind="final_acceptance",
                to_state="accepted",
                actor_reference=reviewer,
                recorded_at=timestamp,
                decision="accepted",
                reviewer_reference=reviewer,
                human_confirmed=True,
                source_outcome_id=str(outcome["outcome_id"]),
                source_outcome_review_id=str(outcome_review["review_id"]),
                result_experience_case_id=result_experience_case_id,
                result_experience_review_id=result_experience_review_id,
                event_data={
                    "final_acceptance_version": FINAL_ACCEPTANCE_VERSION,
                    "rationale": str(rationale).strip(),
                    "prerequisite_checks": prerequisites["checks"],
                    "legacy_case_inserted": bool(saved["inserted"]),
                    "automatic_acceptance": False,
                },
            )
            supersedes = record.get("supersedes_workflow_case_id")
            if supersedes:
                prior_events = self._workflow_events_in_connection(
                    connection, str(supersedes)
                )
                prior_chain = validate_event_chain(prior_events)
                if not prior_chain["valid"]:
                    raise ValueError("The superseded workflow case has an invalid event chain.")
                if prior_chain["current_state"] != "superseded":
                    self._append_workflow_event(
                        connection,
                        str(supersedes),
                        event_kind="supersession",
                        to_state="superseded",
                        actor_reference=reviewer,
                        recorded_at=timestamp,
                        decision="superseded",
                        event_data={
                            "replacement_workflow_case_id": workflow_case_id,
                            "replacement_experience_case_id": result_experience_case_id,
                            "records_deleted": False,
                        },
                    )
        inspected = self.inspect_experience_workflow_case(workflow_case_id)
        inspected["final_acceptance"] = {
            "experience_case_id": result_experience_case_id,
            "experience_review_id": result_experience_review_id,
            "legacy_status": self.get_experience_case_status(
                str(result_experience_case_id)
            ),
        }
        return inspected

    def reject_experience_workflow_case(
        self,
        workflow_case_id: str,
        *,
        reviewer: str,
        reason_code: str,
        notes: str,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        if not all(str(item).strip() for item in (reviewer, reason_code, notes)):
            raise ValueError("Final rejection requires reviewer, reason code, and notes.")
        timestamp = reviewed_at or _utc_now()
        with self._immediate_connection() as connection:
            if self._workflow_case_in_connection(connection, workflow_case_id) is None:
                raise KeyError(f"Workflow case {workflow_case_id} does not exist.")
            self._append_workflow_event(
                connection,
                workflow_case_id,
                event_kind="final_rejection",
                to_state="rejected",
                actor_reference=reviewer,
                recorded_at=timestamp,
                decision="rejected",
                reviewer_reference=reviewer,
                human_confirmed=True,
                event_data={
                    "reason_code": str(reason_code).strip(),
                    "notes": str(notes).strip(),
                    "historical_outcome_valence_used": False,
                },
            )
        return self.inspect_experience_workflow_case(workflow_case_id)

    def withdraw_experience_workflow_case(
        self,
        workflow_case_id: str,
        *,
        reviewer: str,
        notes: str,
        reviewed_at: str | None = None,
    ) -> dict[str, Any]:
        if not str(reviewer).strip() or not str(notes).strip():
            raise ValueError("Withdrawal requires a named human and notes.")
        timestamp = reviewed_at or _utc_now()
        with self._immediate_connection() as connection:
            if self._workflow_case_in_connection(connection, workflow_case_id) is None:
                raise KeyError(f"Workflow case {workflow_case_id} does not exist.")
            self._append_workflow_event(
                connection,
                workflow_case_id,
                event_kind="withdrawal",
                to_state="withdrawn",
                actor_reference=reviewer,
                recorded_at=timestamp,
                decision="withdrawn",
                reviewer_reference=reviewer,
                human_confirmed=True,
                event_data={"notes": str(notes).strip(), "records_deleted": False},
            )
        return self.inspect_experience_workflow_case(workflow_case_id)

    def supersede_experience_workflow_case(
        self,
        workflow_case_id: str,
        *,
        replacement_workflow_case_id: str,
        actor_reference: str,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        if not str(actor_reference).strip():
            raise ValueError("Supersession requires an explicit actor reference.")
        timestamp = recorded_at or _utc_now()
        with self._immediate_connection() as connection:
            current = self._workflow_case_in_connection(connection, workflow_case_id)
            replacement = self._workflow_case_in_connection(
                connection, replacement_workflow_case_id
            )
            if current is None or replacement is None:
                raise KeyError("Both superseded and replacement workflow cases must exist.")
            if replacement.get("supersedes_workflow_case_id") != workflow_case_id:
                raise ValueError(
                    "The replacement workflow case does not explicitly reference this predecessor."
                )
            if replacement["market_episode_id"] != current["market_episode_id"]:
                raise ValueError("Supersession requires the same material market episode.")
            self._append_workflow_event(
                connection,
                workflow_case_id,
                event_kind="supersession",
                to_state="superseded",
                actor_reference=actor_reference,
                recorded_at=timestamp,
                decision="superseded",
                event_data={
                    "replacement_workflow_case_id": replacement_workflow_case_id,
                    "records_deleted": False,
                    "audit_history_preserved": True,
                },
            )
        return self.inspect_experience_workflow_case(workflow_case_id)

    def list_experience_workflow_cases(
        self, *, state: str = "all", limit: int = 100
    ) -> list[dict[str, Any]]:
        if state != "all" and state not in WORKFLOW_STATES:
            raise ValueError(f"Unsupported workflow state: {state!r}.")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT workflow_case_id FROM experience_workflow_cases "
                "ORDER BY market_episode_id, workflow_version, workflow_case_id"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            inspected = self.inspect_experience_workflow_case(
                str(row["workflow_case_id"])
            )
            current = inspected["status"]["current_state"]
            if state != "all" and current != state:
                continue
            draft = inspected["workflow_case"]
            result.append(
                {
                    "workflow_case_id": draft["workflow_case_id"],
                    "market_episode_id": draft["market_episode_id"],
                    "workflow_version": draft["workflow_version"],
                    "state": current,
                    "symbol": draft["identity"]["symbol"],
                    "timeframe": draft["identity"]["timeframe"],
                    "elliott_degree": draft["identity"]["elliott_degree"],
                    "structural_role": draft["identity"]["structural_role"],
                    "accepted_into_existing_pool": inspected["status"][
                        "accepted_into_existing_pool"
                    ],
                    "result_links": inspected["result_links"],
                }
            )
            if len(result) >= max(1, limit):
                break
        return result

    def get_experience_workflow_pool(self, *, limit: int = 100_000) -> list[dict[str, Any]]:
        return self.list_experience_workflow_cases(state="accepted", limit=limit)

    def _experience_comparison_record(
        self, experience_case_id: str
    ) -> dict[str, Any]:
        """Load only case metadata, effective review state, and endpoint DNA."""
        record = self.get_experience_case_record(experience_case_id)
        if record is None:
            raise KeyError(f"Experience case {experience_case_id} does not exist.")
        status = self.get_experience_case_status(experience_case_id)
        endpoint = self.get_pattern_dna(experience_case_id, dna_kind="endpoint")
        endpoint_dna = endpoint.get("dna") if isinstance(endpoint, Mapping) else None
        checks: list[dict[str, Any]] = []

        def check(name: str, passed: bool, reason: str) -> None:
            checks.append({"check": name, "passed": bool(passed), "reason": reason})

        check(
            "endpoint_dna_row_hash",
            bool(
                isinstance(endpoint, Mapping)
                and isinstance(endpoint_dna, Mapping)
                and endpoint.get("content_hash") == endpoint_dna.get("content_hash")
                and endpoint_dna.get("content_hash")
                == pattern_dna_content_hash(endpoint_dna)
            ),
            "The Pattern DNA row, JSON payload, and canonical endpoint hash must agree.",
        )
        fingerprint = self.get_wave_fingerprint(int(record["source_fingerprint_id"]))
        fingerprint_payload = (
            fingerprint.get("fingerprint")
            if isinstance(fingerprint, Mapping)
            and isinstance(fingerprint.get("fingerprint"), Mapping)
            else None
        )
        fingerprint_valid = bool(
            isinstance(fingerprint, Mapping)
            and isinstance(fingerprint_payload, Mapping)
            and fingerprint.get("content_hash") == fingerprint_payload.get("content_hash")
            and fingerprint_payload.get("content_hash")
            == fingerprint_content_hash(fingerprint_payload)
            and isinstance(endpoint_dna, Mapping)
            and endpoint_dna.get("source_fingerprint_hash")
            == fingerprint_payload.get("content_hash")
            and endpoint.get("source_content_hash")
            == fingerprint_payload.get("content_hash")
        )
        check(
            "source_fingerprint_hash",
            fingerprint_valid,
            "The stored Phase 3 fingerprint, endpoint DNA reference, and row source hash must agree.",
        )
        snapshot_id = str(record.get("source_endpoint_snapshot_id") or "")
        snapshot = self.get_hypothesis_snapshot(snapshot_id) if snapshot_id else None
        snapshot_valid = bool(
            isinstance(snapshot, Mapping)
            and snapshot.get("content_hash") == snapshot_content_hash(snapshot)
            and isinstance(endpoint_dna, Mapping)
            and endpoint_dna.get("source_endpoint_snapshot_id") == snapshot_id
            and endpoint_dna.get("source_endpoint_snapshot_hash")
            == snapshot.get("content_hash")
        )
        check(
            "source_endpoint_snapshot_hash",
            snapshot_valid,
            "The immutable Phase 4 decision snapshot ID and canonical hash must agree.",
        )
        return {
            **status,
            "experience_schema_version": record["schema_version"],
            "endpoint_dna": endpoint_dna,
            "source_reference_integrity": {
                "valid": all(item["passed"] for item in checks),
                "checks": checks,
                "policy": "Only immutable endpoint-time Phase 3 and Phase 4 references were validated.",
            },
        }

    def filter_experience_cases(
        self,
        current_experience_case_id: str,
        *,
        config: ComparisonConfig | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Filter all stored versions using endpoint DNA only and persist nothing."""
        current = self._experience_comparison_record(current_experience_case_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT experience_case_id FROM experience_cases ORDER BY market_episode_id, experience_case_id"
            ).fetchall()
        historical = [
            self._experience_comparison_record(str(row["experience_case_id"]))
            for row in rows
        ]
        return filter_structurally_comparable_cases(
            current,
            historical,
            config=config,
        )

    @staticmethod
    def experience_compatibility_matrix() -> dict[str, Any]:
        return compatibility_matrix_manifest()

    @staticmethod
    def experience_comparison_specification(
        version: str = COMPARISON_SPECIFICATION_VERSION,
    ) -> dict[str, Any]:
        return comparison_specification(version)

    def compare_experience_cases(
        self,
        current_experience_case_id: str,
        *,
        candidate_case_ids: Iterable[str] | None = None,
        maximum_comparison_level: int = 4,
        specification_version: str = COMPARISON_SPECIFICATION_VERSION,
        filter_config: ComparisonConfig | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compare only Phase 5A.2-eligible endpoint DNA and persist nothing."""
        current = self._experience_comparison_record(current_experience_case_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT experience_case_id FROM experience_cases "
                "ORDER BY market_episode_id, experience_case_id"
            ).fetchall()
        historical = [
            self._experience_comparison_record(str(row["experience_case_id"]))
            for row in rows
        ]
        filtered = filter_structurally_comparable_cases(
            current,
            historical,
            config=filter_config,
        )
        return compare_filtered_experiences(
            current,
            historical,
            filtered,
            candidate_case_ids=candidate_case_ids,
            maximum_comparison_level=maximum_comparison_level,
            specification_version=specification_version,
        )

    @staticmethod
    def experience_retrieval_policy(
        policy_id: str = DEFAULT_RETRIEVAL_POLICY_ID,
        policy_version: str = DEFAULT_RETRIEVAL_POLICY_VERSION,
    ) -> dict[str, Any]:
        return retrieval_policy(policy_id, policy_version)

    def retrieve_experience_analogues(
        self,
        current_experience_case_id: str,
        *,
        candidate_case_ids: Iterable[str] | None = None,
        maximum_comparison_level: int = 4,
        comparison_specification_version: str = COMPARISON_SPECIFICATION_VERSION,
        policy_id: str = DEFAULT_RETRIEVAL_POLICY_ID,
        policy_version: str = DEFAULT_RETRIEVAL_POLICY_VERSION,
        limit: int = DEFAULT_RESULT_LIMIT,
        filter_config: ComparisonConfig | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Order eligible endpoint comparisons for human review and persist nothing."""
        comparison_set = self.compare_experience_cases(
            current_experience_case_id,
            candidate_case_ids=candidate_case_ids,
            maximum_comparison_level=maximum_comparison_level,
            specification_version=comparison_specification_version,
            filter_config=filter_config,
        )
        return retrieve_analogues(
            comparison_set,
            policy_id=policy_id,
            policy_version=policy_version,
            maximum_comparison_level=maximum_comparison_level,
            limit=limit,
        )

    @staticmethod
    def experience_outcome_specification() -> dict[str, Any]:
        """Return the immutable Phase 5B evidence, taxonomy, and horizon manifest."""
        return outcome_specification_manifest()

    def get_experience_outcome_source(
        self, experience_case_id: str
    ) -> dict[str, Any]:
        """Load linked reviewed-outcome sources without changing their stored state."""
        record = self.get_experience_case_record(experience_case_id)
        if record is None:
            return {
                "load_status": "experience_case_missing",
                "reason": f"Experience case {experience_case_id} does not exist.",
            }

        correction_case_id = str(record.get("source_correction_case_id") or "")
        source_outcome_id = str(record.get("source_outcome_id") or "")
        source_review_id = str(record.get("source_outcome_review_id") or "")
        history = self.get_outcome_history(correction_case_id)
        outcome_item = next(
            (
                item
                for item in history
                if str((item.get("outcome") or {}).get("outcome_id") or "")
                == source_outcome_id
            ),
            None,
        )
        outcome = (
            deepcopy(outcome_item.get("outcome"))
            if isinstance(outcome_item, Mapping)
            and isinstance(outcome_item.get("outcome"), Mapping)
            else None
        )
        outcome_reviews = (
            list(outcome_item.get("reviews") or [])
            if isinstance(outcome_item, Mapping)
            else []
        )
        outcome_review = next(
            (
                deepcopy(item)
                for item in outcome_reviews
                if str(item.get("review_id") or "") == source_review_id
            ),
            None,
        )
        current = self.get_current_reviewed_outcome(correction_case_id)
        current_outcome_id = str((current or {}).get("outcome_id") or "")
        latest_review_id = str(
            (outcome_reviews[-1] if outcome_reviews else {}).get("review_id") or ""
        )

        endpoint_dna = self.get_pattern_dna(
            experience_case_id, dna_kind="endpoint"
        )
        confirmation_dna = self.get_pattern_dna(
            experience_case_id, dna_kind="confirmation"
        )
        resolved_outcome_dna = self.get_pattern_dna(
            experience_case_id, dna_kind="resolved_outcome"
        )
        fingerprint_id = record.get("source_fingerprint_id")
        fingerprint = (
            self.get_wave_fingerprint(int(fingerprint_id))
            if isinstance(fingerprint_id, int)
            else None
        )
        endpoint_snapshot_id = str(
            record.get("source_endpoint_snapshot_id") or ""
        )
        endpoint_snapshot = (
            self.get_hypothesis_snapshot(endpoint_snapshot_id)
            if endpoint_snapshot_id
            else None
        )
        snapshot_lineage = (
            self.get_snapshot_lineage(str(outcome.get("review_snapshot_id") or ""))
            if isinstance(outcome, Mapping) and outcome.get("review_snapshot_id")
            else []
        )
        return {
            "load_status": "loaded",
            "experience_case_record": deepcopy(record),
            "effective_status": self.get_experience_case_status(
                experience_case_id
            ),
            "experience_review_history": self.get_experience_review_history(
                experience_case_id
            ),
            "correction_case": self.get_correction_case(correction_case_id),
            "endpoint_snapshot": endpoint_snapshot,
            "fingerprint_record": fingerprint,
            "endpoint_dna_row": endpoint_dna,
            "confirmation_dna_row": confirmation_dna,
            "resolved_outcome_dna_row": resolved_outcome_dna,
            "outcome": outcome,
            "outcome_review": outcome_review,
            "outcome_is_current": bool(
                source_outcome_id and source_outcome_id == current_outcome_id
            ),
            "outcome_review_is_latest": bool(
                source_review_id and source_review_id == latest_review_id
            ),
            "snapshot_lineage": snapshot_lineage,
        }

    def inspect_experience_outcome(
        self,
        experience_case_id: str,
        *,
        horizon_id: str = DEFAULT_HORIZON_ID,
        horizon_version: str = DEFAULT_HORIZON_VERSION,
        include_outcome_details: bool = False,
    ) -> dict[str, Any]:
        """Audit one linked historical outcome without running analogue retrieval."""
        return inspect_outcome_source(
            self.get_experience_outcome_source(experience_case_id),
            horizon_id=horizon_id,
            horizon_version=horizon_version,
            include_outcome_details=include_outcome_details,
        )

    def build_experience_evidence(
        self,
        current_experience_case_id: str,
        *,
        candidate_case_ids: Iterable[str] | None = None,
        maximum_comparison_level: int = 4,
        comparison_specification_version: str = COMPARISON_SPECIFICATION_VERSION,
        policy_id: str = DEFAULT_RETRIEVAL_POLICY_ID,
        policy_version: str = DEFAULT_RETRIEVAL_POLICY_VERSION,
        limit: int = DEFAULT_RESULT_LIMIT,
        filter_config: ComparisonConfig | Mapping[str, Any] | None = None,
        horizon_id: str = DEFAULT_HORIZON_ID,
        horizon_version: str = DEFAULT_HORIZON_VERSION,
        include_retrieval_details: bool = False,
        include_outcome_details: bool = False,
    ) -> dict[str, Any]:
        """Freeze Phase 5A.4, then attach outcomes for only its selected cases."""
        retrieval = self.retrieve_experience_analogues(
            current_experience_case_id,
            candidate_case_ids=candidate_case_ids,
            maximum_comparison_level=maximum_comparison_level,
            comparison_specification_version=comparison_specification_version,
            policy_id=policy_id,
            policy_version=policy_version,
            limit=limit,
            filter_config=filter_config,
        )
        return attach_reviewed_outcome_evidence(
            retrieval,
            self.get_experience_outcome_source,
            horizon_id=horizon_id,
            horizon_version=horizon_version,
            include_retrieval_details=include_retrieval_details,
            include_outcome_details=include_outcome_details,
        )

    def export_experience_cases(
        self,
        *,
        accepted_only: bool = False,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        summaries = self.list_experience_cases(
            state="accepted" if accepted_only else "all",
            active_only=not include_superseded,
            limit=100_000,
        )
        cases: list[dict[str, Any]] = []
        seen_episodes: set[str] = set()
        for summary in summaries:
            if accepted_only and not summary["accepted_pool_eligible"]:
                continue
            episode_id = str(summary["market_episode_id"])
            if not include_superseded and episode_id in seen_episodes:
                continue
            seen_episodes.add(episode_id)
            cases.append(self.inspect_experience_case(summary["experience_case_id"]))
        return {
            "schema_version": EXPERIENCE_SCHEMA_VERSION,
            "pattern_dna_schema_version": PATTERN_DNA_SCHEMA_VERSION,
            "exported_at": _utc_now(),
            "accepted_only": accepted_only,
            "include_superseded_for_audit": include_superseded,
            "episode_count": len({item["effective_status"]["market_episode_id"] for item in cases}),
            "case_count": len(cases),
            "cases": cases,
            "policy": (
                "Each active market episode appears once. Superseded versions are included "
                "only when explicitly requested for audit and are never counted as new episodes."
            ),
        }

    def accept_run(
        self,
        run_id: int,
        *,
        title: str,
        user_note: str = "",
        resolution_id: int | None = None,
    ) -> int:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"Analysis run {run_id} does not exist.")
        resolution = (
            self.get_degree_resolution(resolution_id)
            if resolution_id is not None
            else self.get_latest_degree_resolution(run_id)
        )
        if resolution is None or resolution.get("run_id") != run_id:
            raise ValueError(
                "A degree resolution for this run is required before it can become approved memory."
            )
        if resolution.get("validation"):
            raise ValueError("The selected degree resolution has validation errors.")
        readiness = resolution.get("readiness", {})
        if readiness.get("final_report_ready") is not True:
            blockers = readiness.get("blockers", [])
            detail = " ".join(str(item) for item in blockers[:3])
            raise ValueError(
                "The selected degree resolution is not final-report ready. " + detail
            )
        review = self.get_latest_review(run_id)
        payload = {
            "title": title,
            "symbol": run["symbol"],
            "original_question": run["question"],
            "user_note": user_note,
            "source_run_id": run_id,
            "accepted_resolution_id": resolution["id"],
            "review": review.get("review") if review else None,
            "accepted_degree_resolution": resolution["response"],
            "deterministic_readiness": readiness,
        }
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO accepted_cases(
                    run_id, title, user_note, accepted_payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    title=excluded.title,
                    user_note=excluded.user_note,
                    accepted_payload_json=excluded.accepted_payload_json
                """,
                (
                    run_id,
                    title,
                    user_note,
                    json.dumps(payload, ensure_ascii=True),
                    _utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT id FROM accepted_cases WHERE run_id = ?", (run_id,)
            ).fetchone()
            return int(row["id"])
