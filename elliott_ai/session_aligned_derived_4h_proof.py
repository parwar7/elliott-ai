"""Shadow-only recursive-proof adapter for session-aligned ``derived_4h`` data.

The existing ``RecursiveMultiTimeframeProofVerifier`` remains the native-data
default.  This module provides a separate opt-in overlay that can use only
complete, policy-bound ``derived_4h`` evidence at the terminal 4h rung.  It
never writes to SQLite, fetches market data, calls a model, changes a stored
resolution, or turns a candidate graph into an authoritative count.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, fields, replace
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .forecast_records import DatasetCutoff, canonical_sha256
from .lower_timeframe_candidate_generator import (
    CandidateChildGraph,
    TypedPivot,
    candidate_child_graph_content_hash,
    expected_child_positions,
    family_children_are_admissible,
)
from .market_data_identity import dataset_feed_family_matches
from .lower_timeframe_recursive_proof import (
    RECURSIVE_PROOF_TIMEFRAME_LADDER,
    RecursiveMultiTimeframeProofVerifier,
    RecursiveProofBundle,
    RecursiveProofBundlePair,
    RecursiveProofBundleResult,
    RecursiveProofBundlePairResult,
    RecursiveProofNode,
    RecursiveProofNodeResult,
    RecursiveProofReasonCode,
    _aggregate_status,
    _motive_hard_price_rules_pass,
    _same_anchor,
)
from .lower_timeframe_subdivision_verifier import SubdivisionVerificationStatus
from .correction_semantics import MOTIVE_FAMILIES
from .session_aligned_derived_4h import (
    DERIVED_4H_BINDING_SCHEMA_VERSION,
    DERIVED_4H_RESULT_SCHEMA_VERSION,
    Derived4hCandidateGraphBinding,
    Derived4hCoverageAttestation,
    Derived4hEvidenceKind,
    Derived4hReconstructionResult,
    Derived4hReconstructionStatus,
    Derived4hWindow,
    SessionAligned4hAggregationPolicy,
    derived_4h_candidate_graph_binding_content_hash,
    derived_4h_coverage_attestation_content_hash,
    derived_4h_invalidation_breached,
    derived_4h_pivot_matches,
    derived_4h_reconstruction_result_content_hash,
    derived_4h_window_content_hash,
    session_aligned_4h_policy_content_hash,
)


DERIVED_4H_PROOF_SCHEMA_VERSION = "session-aligned-derived-4h-proof-1.0.0"
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


class Derived4hProofBindingKind(StrEnum):
    """The two places a recursive bundle needs terminal 4h evidence."""

    NODE_EVIDENCE = "node_evidence"
    DAILY_CHILD_GRAPH_EVIDENCE = "daily_child_graph_evidence"


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Canonical JSON cannot contain non-finite numbers.")
        return value
    raise TypeError(f"Unsupported canonical value: {type(value).__name__}.")


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _require_hash(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return value


def _enum(value: Any, enum_type: type[StrEnum], *, field_name: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}.") from exc


def _string_tuple(value: Sequence[str], *, field_name: str, sort_unique: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence of strings.")
    result = tuple(_require_text(item, field_name=field_name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} cannot contain duplicates.")
    return tuple(sorted(result)) if sort_unique else result


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], *, model_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{model_name} contains unknown fields: {', '.join(unknown)}.")


def _coerce(value: Any, model_type: type[Any], *, field_name: str) -> Any:
    if isinstance(value, model_type):
        return value
    if isinstance(value, Mapping):
        parser = getattr(model_type, "from_dict", None)
        if callable(parser):
            return parser(value)
    raise TypeError(f"{field_name} must contain {model_type.__name__} values.")


class _JsonContract:
    def to_dict(self) -> dict[str, Any]:
        return {field.name: _json_value(getattr(self, field.name)) for field in fields(self)}


def _hash_payload(value: Any) -> str:
    payload = value.to_dict() if hasattr(value, "to_dict") else _json_value(value)
    if isinstance(payload, Mapping):
        payload = dict(payload)
        payload.pop("content_hash", None)
    return canonical_sha256(payload)


@dataclass(frozen=True, slots=True)
class Derived4hProofEvidenceBinding(_JsonContract):
    """One immutable overlay binding for a terminal node or Daily child graph."""

    binding_id: str
    binding_kind: Derived4hProofBindingKind
    node_id: str
    node_content_hash: str
    reconstruction_result: Derived4hReconstructionResult
    coverage: Derived4hCoverageAttestation | None
    pivot_catalog: tuple[TypedPivot, ...]
    candidate_graph_binding: Derived4hCandidateGraphBinding | None
    schema_version: str = DERIVED_4H_PROOF_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _require_text(self.binding_id, field_name="binding_id"))
        object.__setattr__(self, "binding_kind", _enum(self.binding_kind, Derived4hProofBindingKind, field_name="binding_kind"))
        object.__setattr__(self, "node_id", _require_text(self.node_id, field_name="node_id"))
        object.__setattr__(self, "node_content_hash", _require_hash(self.node_content_hash, field_name="node_content_hash"))
        object.__setattr__(self, "reconstruction_result", _coerce(self.reconstruction_result, Derived4hReconstructionResult, field_name="reconstruction_result"))
        if self.coverage is not None:
            object.__setattr__(self, "coverage", _coerce(self.coverage, Derived4hCoverageAttestation, field_name="coverage"))
        if isinstance(self.pivot_catalog, (str, bytes)) or not isinstance(self.pivot_catalog, Sequence):
            raise TypeError("pivot_catalog must be a sequence.")
        pivots = tuple(_coerce(item, TypedPivot, field_name="pivot_catalog") for item in self.pivot_catalog)
        if len({item.pivot_id for item in pivots}) != len(pivots):
            raise ValueError("pivot_catalog cannot contain duplicate pivot IDs.")
        object.__setattr__(self, "pivot_catalog", pivots)
        if self.candidate_graph_binding is not None:
            object.__setattr__(self, "candidate_graph_binding", _coerce(self.candidate_graph_binding, Derived4hCandidateGraphBinding, field_name="candidate_graph_binding"))
        if self.binding_kind is Derived4hProofBindingKind.NODE_EVIDENCE:
            if self.candidate_graph_binding is not None:
                raise ValueError("A direct 4h node binding cannot carry a candidate-graph binding.")
        elif self.candidate_graph_binding is None:
            raise ValueError("A Daily child-graph binding requires a candidate-graph binding.")
        if self.reconstruction_result.status is Derived4hReconstructionStatus.AVAILABLE and (self.coverage is None or not pivots):
            raise ValueError("Available derived evidence requires explicit coverage and a pivot catalog.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_proof_evidence_binding_content_hash(self):
            raise ValueError("Derived4hProofEvidenceBinding content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "Derived4hProofEvidenceBinding":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("binding_id"):
            values["binding_id"] = f"derived_4h_proof_binding_{canonical_sha256({'node': values.get('node_content_hash'), 'kind': _json_value(values.get('binding_kind')), 'result': _json_value(values.get('reconstruction_result'))})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=derived_4h_proof_evidence_binding_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hProofEvidenceBinding":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_proof_evidence_binding_content_hash(result):
            raise ValueError("Derived4hProofEvidenceBinding content_hash does not match its payload.")
        return result


def derived_4h_proof_evidence_binding_content_hash(value: Derived4hProofEvidenceBinding | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class Derived4hProofOverlay(_JsonContract):
    """A separate, content-hashed overlay for one Primary or Alternative bundle."""

    overlay_id: str
    bundle_id: str
    bundle_content_hash: str
    policy: SessionAligned4hAggregationPolicy
    bindings: tuple[Derived4hProofEvidenceBinding, ...]
    shadow_mode: bool
    schema_version: str = DERIVED_4H_PROOF_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "overlay_id", _require_text(self.overlay_id, field_name="overlay_id"))
        object.__setattr__(self, "bundle_id", _require_text(self.bundle_id, field_name="bundle_id"))
        object.__setattr__(self, "bundle_content_hash", _require_hash(self.bundle_content_hash, field_name="bundle_content_hash"))
        object.__setattr__(self, "policy", _coerce(self.policy, SessionAligned4hAggregationPolicy, field_name="policy"))
        if isinstance(self.bindings, (str, bytes)) or not isinstance(self.bindings, Sequence):
            raise TypeError("bindings must be a sequence.")
        bindings = tuple(_coerce(item, Derived4hProofEvidenceBinding, field_name="bindings") for item in self.bindings)
        if not bindings:
            raise ValueError("Derived4hProofOverlay requires at least one explicit derived_4h binding.")
        keys = tuple((item.node_id, item.binding_kind.value) for item in bindings)
        if len(keys) != len(set(keys)):
            raise ValueError("Derived4hProofOverlay cannot contain duplicate node/kind bindings.")
        for binding in bindings:
            result = binding.reconstruction_result
            if result.policy_hash != self.policy.content_hash:
                raise ValueError("Derived evidence must use the overlay aggregation policy hash.")
            if binding.candidate_graph_binding is not None:
                graph_binding = binding.candidate_graph_binding
                if (
                    graph_binding.policy_id != self.policy.policy_id
                    or graph_binding.policy_version != self.policy.policy_version
                    or graph_binding.policy_hash != self.policy.content_hash
                    or graph_binding.evidence_kind is not Derived4hEvidenceKind.DERIVED_4H
                ):
                    raise ValueError("Candidate graph binding does not match the overlay aggregation policy.")
        object.__setattr__(self, "bindings", bindings)
        if not isinstance(self.shadow_mode, bool) or not self.shadow_mode:
            raise ValueError("Derived4hProofOverlay must remain shadow-only.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_proof_overlay_content_hash(self):
            raise ValueError("Derived4hProofOverlay content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "Derived4hProofOverlay":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("overlay_id"):
            values["overlay_id"] = f"derived_4h_overlay_{canonical_sha256({'bundle': values.get('bundle_content_hash'), 'policy': _json_value(values.get('policy')), 'bindings': _json_value(values.get('bindings'))})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=derived_4h_proof_overlay_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hProofOverlay":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_proof_overlay_content_hash(result):
            raise ValueError("Derived4hProofOverlay content_hash does not match its payload.")
        return result

    def binding_for(self, node_id: str, binding_kind: Derived4hProofBindingKind) -> Derived4hProofEvidenceBinding | None:
        return next(
            (
                item
                for item in self.bindings
                if item.node_id == node_id and item.binding_kind is binding_kind
            ),
            None,
        )


def derived_4h_proof_overlay_content_hash(value: Derived4hProofOverlay | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class Derived4hProofOverlayPair(_JsonContract):
    pair_id: str
    primary_overlay: Derived4hProofOverlay
    alternative_overlay: Derived4hProofOverlay
    schema_version: str = DERIVED_4H_PROOF_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair_id", _require_text(self.pair_id, field_name="pair_id"))
        object.__setattr__(self, "primary_overlay", _coerce(self.primary_overlay, Derived4hProofOverlay, field_name="primary_overlay"))
        object.__setattr__(self, "alternative_overlay", _coerce(self.alternative_overlay, Derived4hProofOverlay, field_name="alternative_overlay"))
        if self.primary_overlay.bundle_content_hash == self.alternative_overlay.bundle_content_hash:
            raise ValueError("Primary and Alternative derived proof overlays must remain separate.")
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_proof_overlay_pair_content_hash(self):
            raise ValueError("Derived4hProofOverlayPair content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "Derived4hProofOverlayPair":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        if not values.get("pair_id"):
            values["pair_id"] = f"derived_4h_overlay_pair_{canonical_sha256({'primary': _json_value(values.get('primary_overlay')), 'alternative': _json_value(values.get('alternative_overlay'))})[:32]}"
        result = cls(**values)
        return replace(result, content_hash=derived_4h_proof_overlay_pair_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hProofOverlayPair":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_proof_overlay_pair_content_hash(result):
            raise ValueError("Derived4hProofOverlayPair content_hash does not match its payload.")
        return result


def derived_4h_proof_overlay_pair_content_hash(value: Derived4hProofOverlayPair | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class Derived4hProofBundleResult(_JsonContract):
    """Wrap a normal recursive result with immutable derived-evidence lineage."""

    base_result: RecursiveProofBundleResult
    overlay_id: str
    overlay_content_hash: str
    evidence_kind: Derived4hEvidenceKind = Derived4hEvidenceKind.DERIVED_4H
    schema_version: str = DERIVED_4H_RESULT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_result", _coerce(self.base_result, RecursiveProofBundleResult, field_name="base_result"))
        object.__setattr__(self, "overlay_id", _require_text(self.overlay_id, field_name="overlay_id"))
        object.__setattr__(self, "overlay_content_hash", _require_hash(self.overlay_content_hash, field_name="overlay_content_hash"))
        object.__setattr__(self, "evidence_kind", _enum(self.evidence_kind, Derived4hEvidenceKind, field_name="evidence_kind"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_proof_bundle_result_content_hash(self):
            raise ValueError("Derived4hProofBundleResult content_hash does not match its payload.")

    @property
    def status(self) -> SubdivisionVerificationStatus:
        return self.base_result.status

    @property
    def node_results(self) -> tuple[RecursiveProofNodeResult, ...]:
        return self.base_result.node_results

    @classmethod
    def create(cls, **values: Any) -> "Derived4hProofBundleResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=derived_4h_proof_bundle_result_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hProofBundleResult":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_proof_bundle_result_content_hash(result):
            raise ValueError("Derived4hProofBundleResult content_hash does not match its payload.")
        return result


def derived_4h_proof_bundle_result_content_hash(value: Derived4hProofBundleResult | Mapping[str, Any]) -> str:
    return _hash_payload(value)


@dataclass(frozen=True, slots=True)
class Derived4hProofBundlePairResult(_JsonContract):
    primary_result: Derived4hProofBundleResult
    alternative_result: Derived4hProofBundleResult
    overlay_pair_content_hash: str
    schema_version: str = DERIVED_4H_RESULT_SCHEMA_VERSION
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "primary_result", _coerce(self.primary_result, Derived4hProofBundleResult, field_name="primary_result"))
        object.__setattr__(self, "alternative_result", _coerce(self.alternative_result, Derived4hProofBundleResult, field_name="alternative_result"))
        object.__setattr__(self, "overlay_pair_content_hash", _require_hash(self.overlay_pair_content_hash, field_name="overlay_pair_content_hash"))
        object.__setattr__(self, "schema_version", _require_text(self.schema_version, field_name="schema_version"))
        if self.content_hash and _require_hash(self.content_hash, field_name="content_hash") != derived_4h_proof_bundle_pair_result_content_hash(self):
            raise ValueError("Derived4hProofBundlePairResult content_hash does not match its payload.")

    @classmethod
    def create(cls, **values: Any) -> "Derived4hProofBundlePairResult":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=derived_4h_proof_bundle_pair_result_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "Derived4hProofBundlePairResult":
        raw = dict(value)
        _reject_unknown(raw, {field.name for field in fields(cls)}, model_name=cls.__name__)
        result = cls(**raw)
        if verify_hash and result.content_hash != derived_4h_proof_bundle_pair_result_content_hash(result):
            raise ValueError("Derived4hProofBundlePairResult content_hash does not match its payload.")
        return result


def derived_4h_proof_bundle_pair_result_content_hash(value: Derived4hProofBundlePairResult | Mapping[str, Any]) -> str:
    return _hash_payload(value)


_ACTIVE_OVERLAY: ContextVar[Derived4hProofOverlay | None] = ContextVar("active_derived_4h_proof_overlay", default=None)


class SessionAlignedDerived4hProofVerifier(RecursiveMultiTimeframeProofVerifier):
    """Opt-in shadow verifier that overlays complete `derived_4h` evidence.

    The inherited verifier remains responsible for recursive ordering, family
    validation, hard Elliott rules, invalidations, and bottom-up propagation.
    This subclass only replaces the terminal 4h data-validation calls for nodes
    explicitly bound by an immutable overlay.
    """

    def verify(
        self,
        bundle: RecursiveProofBundle,
        overlay: Derived4hProofOverlay,
        *,
        shadow_mode: bool = False,
    ) -> Derived4hProofBundleResult:
        if not isinstance(bundle, RecursiveProofBundle):
            raise TypeError("bundle must be a RecursiveProofBundle.")
        if not isinstance(overlay, Derived4hProofOverlay):
            raise TypeError("overlay must be a Derived4hProofOverlay.")
        token = _ACTIVE_OVERLAY.set(overlay)
        try:
            base_result = super().verify(bundle, shadow_mode=shadow_mode)
        finally:
            _ACTIVE_OVERLAY.reset(token)
        return Derived4hProofBundleResult.create(
            base_result=base_result,
            overlay_id=overlay.overlay_id,
            overlay_content_hash=overlay.content_hash,
        )

    def verify_pair(
        self,
        pair: RecursiveProofBundlePair,
        overlays: Derived4hProofOverlayPair,
        *,
        shadow_mode: bool = False,
    ) -> Derived4hProofBundlePairResult:
        if not isinstance(pair, RecursiveProofBundlePair):
            raise TypeError("pair must be a RecursiveProofBundlePair.")
        if not isinstance(overlays, Derived4hProofOverlayPair):
            raise TypeError("overlays must be a Derived4hProofOverlayPair.")
        primary = self.verify(pair.primary_bundle, overlays.primary_overlay, shadow_mode=shadow_mode)
        alternative = self.verify(pair.alternative_bundle, overlays.alternative_overlay, shadow_mode=shadow_mode)
        return Derived4hProofBundlePairResult.create(
            primary_result=primary,
            alternative_result=alternative,
            overlay_pair_content_hash=overlays.content_hash,
        )

    @staticmethod
    def _overlay() -> Derived4hProofOverlay:
        overlay = _ACTIVE_OVERLAY.get()
        if overlay is None:
            raise RuntimeError("Derived 4h proof validation requires an active immutable overlay.")
        return overlay

    def _bundle_static_reasons(
        self,
        bundle: RecursiveProofBundle,
        node_by_id: Mapping[str, RecursiveProofNode],
        *,
        shadow_mode: bool,
    ) -> tuple[RecursiveProofReasonCode, ...]:
        reasons = list(super()._bundle_static_reasons(bundle, node_by_id, shadow_mode=shadow_mode))
        overlay = self._overlay()
        if not overlay.shadow_mode:
            reasons.append(RecursiveProofReasonCode.SHADOW_MODE_REQUIRED)
        if overlay.content_hash != derived_4h_proof_overlay_content_hash(overlay):
            reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
        if overlay.policy.content_hash != session_aligned_4h_policy_content_hash(overlay.policy):
            reasons.append(RecursiveProofReasonCode.DERIVED_4H_POLICY_MISMATCH)
        if overlay.bundle_id != bundle.bundle_id or overlay.bundle_content_hash != bundle.content_hash:
            reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
        reachable_node_ids = self._reachable_node_ids(bundle.root_node_id, node_by_id)
        bindings_by_key = {
            (binding.node_id, binding.binding_kind): binding
            for binding in overlay.bindings
        }
        for binding in overlay.bindings:
            if binding.node_id not in reachable_node_ids:
                reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
            reasons.extend(self._binding_static_reasons(bundle, node_by_id, binding, overlay))
        reasons.extend(
            self._derived_branch_reasons(
                node_by_id,
                bindings_by_key,
            )
        )
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def _reachable_node_ids(
        root_node_id: str,
        node_by_id: Mapping[str, RecursiveProofNode],
    ) -> frozenset[str]:
        """Return the supplied proof-tree nodes reachable from its root.

        An overlay cannot introduce evidence for a detached node.  Keeping this
        check here rather than mutating the base bundle preserves the native
        verifier's semantics and makes an invalid sidecar explicit.
        """

        reachable: set[str] = set()
        pending = [root_node_id]
        while pending:
            node_id = pending.pop()
            if node_id in reachable:
                continue
            node = node_by_id.get(node_id)
            if node is None:
                continue
            reachable.add(node_id)
            pending.extend(node.child_node_ids)
        return frozenset(reachable)

    @staticmethod
    def _derived_branch_reasons(
        node_by_id: Mapping[str, RecursiveProofNode],
        bindings_by_key: Mapping[tuple[str, Derived4hProofBindingKind], Derived4hProofEvidenceBinding],
    ) -> tuple[RecursiveProofReasonCode, ...]:
        """Require one explicit evidence kind for each Daily-to-4h branch.

        A terminal derived leaf cannot be validated through a Daily parent that
        still uses ``native_4h`` graph evidence. Conversely, a Daily derived
        graph binding must declare all of its terminal 4h descendants. This is
        deliberately a static guardrail: no status can fall back to native rows
        merely because a derived sidecar is incomplete.
        """

        reasons: list[RecursiveProofReasonCode] = []
        parents_by_child: dict[str, list[RecursiveProofNode]] = {}
        for parent in node_by_id.values():
            for child_id in parent.child_node_ids:
                parents_by_child.setdefault(child_id, []).append(parent)

        for (node_id, kind), _binding in bindings_by_key.items():
            node = node_by_id.get(node_id)
            if node is None:
                reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
                continue
            if kind is Derived4hProofBindingKind.NODE_EVIDENCE:
                parents = parents_by_child.get(node_id, [])
                if len(parents) != 1 or parents[0].proof_timeframe != "daily":
                    reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
                    continue
                if (
                    parents[0].node_id,
                    Derived4hProofBindingKind.DAILY_CHILD_GRAPH_EVIDENCE,
                ) not in bindings_by_key:
                    reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
                continue

            if node.proof_timeframe != "daily":
                reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
                continue
            if not node.child_node_ids:
                reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
                continue
            for child_id in node.child_node_ids:
                child = node_by_id.get(child_id)
                if (
                    child is None
                    or child.proof_timeframe != "4h"
                    or (
                        child_id,
                        Derived4hProofBindingKind.NODE_EVIDENCE,
                    ) not in bindings_by_key
                ):
                    reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def _binding_static_reasons(
        bundle: RecursiveProofBundle,
        node_by_id: Mapping[str, RecursiveProofNode],
        binding: Derived4hProofEvidenceBinding,
        overlay: Derived4hProofOverlay,
    ) -> tuple[RecursiveProofReasonCode, ...]:
        reasons: list[RecursiveProofReasonCode] = []
        node = node_by_id.get(binding.node_id)
        if node is None or node.content_hash != binding.node_content_hash:
            reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
            return tuple(reasons)
        result = binding.reconstruction_result
        if result.content_hash != derived_4h_reconstruction_result_content_hash(result):
            reasons.append(RecursiveProofReasonCode.DERIVED_4H_SOURCE_INCONSISTENT)
        if result.policy_hash != overlay.policy.content_hash or result.schedule_hash == "":
            reasons.append(RecursiveProofReasonCode.DERIVED_4H_POLICY_MISMATCH)
        if result.window is not None and result.window.content_hash != derived_4h_window_content_hash(result.window):
            reasons.append(RecursiveProofReasonCode.DERIVED_4H_SOURCE_INCONSISTENT)
        if binding.coverage is not None and binding.coverage.content_hash != derived_4h_coverage_attestation_content_hash(binding.coverage):
            reasons.append(RecursiveProofReasonCode.DERIVED_4H_SOURCE_INCONSISTENT)
        if binding.binding_kind is Derived4hProofBindingKind.NODE_EVIDENCE:
            if node.proof_timeframe != "4h" or node.native_window is not None:
                reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
        else:
            graph = node.child_graph
            graph_binding = binding.candidate_graph_binding
            if node.proof_timeframe != "daily" or node.child_window is not None or graph is None or graph_binding is None:
                reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
            elif (
                graph.target_timeframe != "4h"
                or graph.graph_id != graph_binding.graph_id
                or graph.content_hash != graph_binding.graph_content_hash
                or graph.content_hash != candidate_child_graph_content_hash(graph)
                or graph_binding.policy_hash != overlay.policy.content_hash
            ):
                reasons.append(RecursiveProofReasonCode.DERIVED_4H_POLICY_MISMATCH)
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def _status_for_static_reasons(
        reasons: Sequence[RecursiveProofReasonCode],
        current: SubdivisionVerificationStatus,
    ) -> SubdivisionVerificationStatus:
        if current is SubdivisionVerificationStatus.INCONSISTENT:
            return current
        if RecursiveProofReasonCode.DERIVED_4H_SOURCE_NOT_COVERED in reasons:
            return SubdivisionVerificationStatus.NOT_COVERED
        return RecursiveMultiTimeframeProofVerifier._status_for_static_reasons(reasons, current)

    def _validate_node_native_data(
        self,
        bundle: RecursiveProofBundle,
        node: RecursiveProofNode,
        *,
        root_dataset: DatasetCutoff | None,
    ) -> tuple[SubdivisionVerificationStatus, tuple[RecursiveProofReasonCode, ...]]:
        binding = self._overlay().binding_for(node.node_id, Derived4hProofBindingKind.NODE_EVIDENCE)
        if binding is None:
            return super()._validate_node_native_data(bundle, node, root_dataset=root_dataset)
        return self._validate_derived_evidence(
            bundle,
            node,
            binding,
            root_dataset=root_dataset,
            expected_start=node.wave.start_pivot.timestamp_utc,
            expected_end=node.wave.end_pivot.timestamp_utc,
            require_graph_binding=False,
        )

    def _validate_direct_child_graph(
        self,
        bundle: RecursiveProofBundle,
        node: RecursiveProofNode,
        *,
        expected_timeframe: str,
        node_by_id: Mapping[str, RecursiveProofNode],
        root_dataset: DatasetCutoff | None,
    ) -> tuple[SubdivisionVerificationStatus, tuple[RecursiveProofReasonCode, ...], tuple[str, ...]]:
        binding = self._overlay().binding_for(node.node_id, Derived4hProofBindingKind.DAILY_CHILD_GRAPH_EVIDENCE)
        if binding is None or expected_timeframe != "4h":
            return super()._validate_direct_child_graph(
                bundle,
                node,
                expected_timeframe=expected_timeframe,
                node_by_id=node_by_id,
                root_dataset=root_dataset,
            )
        evidence_status, evidence_reasons = self._validate_derived_evidence(
            bundle,
            node,
            binding,
            root_dataset=root_dataset,
            expected_start=node.wave.start_pivot.timestamp_utc,
            expected_end=node.wave.end_pivot.timestamp_utc,
            require_graph_binding=True,
        )
        if evidence_status is not SubdivisionVerificationStatus.VERIFIED:
            return evidence_status, evidence_reasons, ()
        assert binding.reconstruction_result.window is not None
        assert binding.coverage is not None
        return self._validate_derived_child_graph(
            bundle,
            node,
            binding,
            window=binding.reconstruction_result.window,
            expected_timeframe=expected_timeframe,
            node_by_id=node_by_id,
        )

    def _validate_derived_evidence(
        self,
        bundle: RecursiveProofBundle,
        node: RecursiveProofNode,
        binding: Derived4hProofEvidenceBinding,
        *,
        root_dataset: DatasetCutoff | None,
        expected_start: str,
        expected_end: str,
        require_graph_binding: bool,
    ) -> tuple[SubdivisionVerificationStatus, tuple[RecursiveProofReasonCode, ...]]:
        overlay = self._overlay()
        result = binding.reconstruction_result
        if result.status is Derived4hReconstructionStatus.NOT_COVERED:
            return SubdivisionVerificationStatus.NOT_COVERED, (RecursiveProofReasonCode.DERIVED_4H_SOURCE_NOT_COVERED,)
        if result.status is Derived4hReconstructionStatus.INCONSISTENT:
            return SubdivisionVerificationStatus.INCONSISTENT, (RecursiveProofReasonCode.DERIVED_4H_SOURCE_INCONSISTENT,)
        window = result.window
        coverage = binding.coverage
        if window is None:
            return SubdivisionVerificationStatus.INCONSISTENT, (RecursiveProofReasonCode.DERIVED_4H_SOURCE_INCONSISTENT,)
        if coverage is None:
            return SubdivisionVerificationStatus.NOT_COVERED, (RecursiveProofReasonCode.REQUIRED_COVERAGE_ATTESTATION_MISSING,)
        reasons: list[RecursiveProofReasonCode] = []
        if binding.node_content_hash != node.content_hash or window.evidence_kind is not Derived4hEvidenceKind.DERIVED_4H:
            reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
        if (
            window.policy.content_hash != overlay.policy.content_hash
            or result.policy_hash != overlay.policy.content_hash
            or coverage.policy_hash != overlay.policy.content_hash
            or coverage.schedule_hash != window.schedule.content_hash
            or coverage.derived_window_hash != window.content_hash
            or coverage.derived_rows_hash != window.derived_rows_hash
        ):
            reasons.append(RecursiveProofReasonCode.DERIVED_4H_POLICY_MISMATCH)
        if require_graph_binding:
            graph = node.child_graph
            graph_binding = binding.candidate_graph_binding
            if graph is None or graph_binding is None:
                reasons.append(RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID)
            elif (
                graph.target_timeframe != "4h"
                or graph.graph_id != graph_binding.graph_id
                or graph.content_hash != graph_binding.graph_content_hash
                or graph_binding.policy_hash != overlay.policy.content_hash
            ):
                reasons.append(RecursiveProofReasonCode.DERIVED_4H_POLICY_MISMATCH)
        if root_dataset is not None and not dataset_feed_family_matches(root_dataset, window.dataset_cutoff):
            reasons.append(RecursiveProofReasonCode.WINDOW_METADATA_INCOMPATIBLE)
        cutoff = _utc(bundle.analysis_cutoff_utc)
        if (
            _utc(window.dataset_cutoff.cutoff_utc) > cutoff
            or any(_utc(bar.bucket_end_utc) > cutoff for bar in window.bars)
        ):
            reasons.append(RecursiveProofReasonCode.WINDOW_AFTER_CUTOFF)
        if not window.dataset_cutoff.completed_candles_only:
            reasons.append(RecursiveProofReasonCode.INCOMPLETE_CANDLE)
        if len(window.bars) < bundle.policy.minimum_native_bars:
            return SubdivisionVerificationStatus.NOT_COVERED, tuple(dict.fromkeys((*reasons, RecursiveProofReasonCode.COVERAGE_INCOMPLETE)))
        if not coverage.coverage_complete or coverage.missing_interval_ids:
            return SubdivisionVerificationStatus.NOT_COVERED, tuple(dict.fromkeys((*reasons, RecursiveProofReasonCode.DERIVED_4H_SOURCE_NOT_COVERED)))
        if coverage.interval_start_utc > expected_start or coverage.interval_end_utc < expected_end:
            return SubdivisionVerificationStatus.NOT_COVERED, tuple(dict.fromkeys((*reasons, RecursiveProofReasonCode.COVERAGE_INTERVAL_INSUFFICIENT)))
        if reasons:
            return SubdivisionVerificationStatus.INCONSISTENT, tuple(dict.fromkeys(reasons))
        return SubdivisionVerificationStatus.VERIFIED, ()

    def _validate_derived_child_graph(
        self,
        bundle: RecursiveProofBundle,
        node: RecursiveProofNode,
        binding: Derived4hProofEvidenceBinding,
        *,
        window: Derived4hWindow,
        expected_timeframe: str,
        node_by_id: Mapping[str, RecursiveProofNode],
    ) -> tuple[SubdivisionVerificationStatus, tuple[RecursiveProofReasonCode, ...], tuple[str, ...]]:
        graph = node.child_graph
        assert graph is not None
        if graph.target_timeframe != expected_timeframe:
            return SubdivisionVerificationStatus.UNPROVEN, (RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED,), ()
        if graph.role.value != bundle.role.value or graph.parent_candidate_id != node.wave.wave_id or graph.declared_family != node.wave.declared_family:
            return SubdivisionVerificationStatus.INCONSISTENT, (RecursiveProofReasonCode.BUNDLE_GRAPH_INVALID,), ()
        if graph.proof_scope.verified_to_timeframe is not None or any(child.candidate_state != "candidate" for child in graph.children):
            return SubdivisionVerificationStatus.INCONSISTENT, (RecursiveProofReasonCode.CANDIDATE_STATUS_FORBIDDEN,), ()

        catalog = {item.pivot_id: item for item in self._derived_catalog(binding)}
        reasons: list[RecursiveProofReasonCode] = []
        positions = tuple(child.sequence_position for child in graph.children)
        expected_positions = expected_child_positions(graph.declared_family, positions)
        if expected_positions is None or positions != expected_positions:
            reasons.append(RecursiveProofReasonCode.CHILD_ORDER_INVALID)
        by_position = {child.sequence_position: child for child in graph.children}
        if expected_positions is None or not family_children_are_admissible(graph.declared_family, by_position):
            reasons.append(RecursiveProofReasonCode.FAMILY_CHILDREN_INCOMPATIBLE)
        for child in graph.children:
            if child.timeframe != expected_timeframe:
                reasons.append(RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED)
            for pivot in (child.start_pivot, child.end_pivot):
                catalogued = catalog.get(pivot.pivot_id)
                if catalogued is None:
                    reasons.append(RecursiveProofReasonCode.PIVOT_NOT_CATALOGUED)
                elif catalogued.content_hash != pivot.content_hash:
                    reasons.append(RecursiveProofReasonCode.PIVOT_LINEAGE_MISMATCH)
                elif not derived_4h_pivot_matches(pivot, window):
                    reasons.append(RecursiveProofReasonCode.PIVOT_VALUE_MISMATCH)
            if child.invalidation.source_pivot_id not in catalog:
                reasons.append(RecursiveProofReasonCode.INVALIDATION_PIVOT_UNKNOWN)
            elif derived_4h_invalidation_breached(child.invalidation, child.start_pivot, window):
                reasons.append(RecursiveProofReasonCode.INVALIDATION_BREACHED)
        if graph.children:
            if not _same_anchor(node.wave.start_pivot, graph.children[0].start_pivot) or not _same_anchor(node.wave.end_pivot, graph.children[-1].end_pivot):
                reasons.append(RecursiveProofReasonCode.PARENT_CHILD_BOUNDARY_MISMATCH)
        for previous, current in zip(graph.children, graph.children[1:]):
            if not _same_anchor(previous.end_pivot, current.start_pivot):
                reasons.append(RecursiveProofReasonCode.CHILD_PIVOT_CHAIN_DISCONTINUOUS)
        if graph.declared_family in MOTIVE_FAMILIES:
            directions = ("up", "down", "up", "down", "up") if node.wave.direction == "up" else ("down", "up", "down", "up", "down")
            if tuple(child.direction for child in graph.children) != directions:
                reasons.append(RecursiveProofReasonCode.DIRECTION_SEQUENCE_INVALID)
            if not _motive_hard_price_rules_pass(graph, node.wave):
                reasons.append(RecursiveProofReasonCode.HARD_PRICE_RULE_FAILURE)
        if graph.declared_family == "diagonal":
            return SubdivisionVerificationStatus.UNPROVEN, tuple(dict.fromkeys((*reasons, RecursiveProofReasonCode.DIAGONAL_GEOMETRY_UNPROVEN))), ()

        graph_children = {item.child_id: item for item in graph.children}
        supplied = set(node.child_node_ids)
        required = set(graph_children)
        if supplied != required:
            return SubdivisionVerificationStatus.UNPROVEN, tuple(dict.fromkeys((*reasons, RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING))), tuple(sorted(supplied & required))
        for child_id in node.child_node_ids:
            child_node = node_by_id.get(child_id)
            graph_child = graph_children[child_id]
            if child_node is None:
                reasons.append(RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING)
                continue
            if child_node.proof_timeframe != expected_timeframe:
                reasons.append(RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED)
                continue
            if (
                child_node.wave.wave_id != graph_child.child_id
                or child_node.wave.degree != graph_child.degree
                or child_node.wave.declared_family != graph_child.declared_family
                or child_node.wave.direction != graph_child.direction
                or not _same_anchor(child_node.wave.start_pivot, graph_child.start_pivot)
                or not _same_anchor(child_node.wave.end_pivot, graph_child.end_pivot)
            ):
                reasons.append(RecursiveProofReasonCode.CHILD_SNAPSHOT_MISMATCH)
        if reasons:
            status = SubdivisionVerificationStatus.UNPROVEN if all(reason in {RecursiveProofReasonCode.REQUIRED_DESCENDANT_MISSING, RecursiveProofReasonCode.PROOF_TIMEFRAME_SKIPPED} for reason in reasons) else SubdivisionVerificationStatus.INCONSISTENT
            return status, tuple(dict.fromkeys(reasons)), tuple(node.child_node_ids)
        return SubdivisionVerificationStatus.VERIFIED, (), tuple(node.child_node_ids)

    @staticmethod
    def _derived_catalog(binding: Derived4hProofEvidenceBinding) -> tuple[TypedPivot, ...]:
        """Return only caller-supplied immutable pivots; never synthesize them."""

        return binding.pivot_catalog

    def _verify_node(self, *args: Any, **kwargs: Any) -> RecursiveProofNodeResult:
        result = super()._verify_node(*args, **kwargs)
        node = args[1] if len(args) >= 2 else kwargs.get("node")
        if not isinstance(node, RecursiveProofNode) or node.proof_timeframe != "4h":
            return result
        if self._overlay().binding_for(node.node_id, Derived4hProofBindingKind.NODE_EVIDENCE) is None:
            return result
        if result.status is not SubdivisionVerificationStatus.VERIFIED:
            return result
        reasons = tuple(
            RecursiveProofReasonCode.TERMINAL_DERIVED_4H_VERIFIED
            if item is RecursiveProofReasonCode.TERMINAL_4H_VERIFIED
            else item
            for item in result.reason_codes
        )
        return RecursiveProofNodeResult.create(
            node_id=result.node_id,
            proof_timeframe=result.proof_timeframe,
            status=result.status,
            reason_codes=reasons,
            child_node_ids=result.child_node_ids,
            verified_to_timeframe=result.verified_to_timeframe,
            warnings=tuple((*result.warnings, "Terminal proof used explicit derived_4h session-aligned evidence.")),
        )


__all__ = [
    "DERIVED_4H_PROOF_SCHEMA_VERSION",
    "Derived4hProofBindingKind",
    "Derived4hProofBundlePairResult",
    "Derived4hProofBundleResult",
    "Derived4hProofEvidenceBinding",
    "Derived4hProofOverlay",
    "Derived4hProofOverlayPair",
    "SessionAlignedDerived4hProofVerifier",
    "derived_4h_proof_bundle_pair_result_content_hash",
    "derived_4h_proof_bundle_result_content_hash",
    "derived_4h_proof_evidence_binding_content_hash",
    "derived_4h_proof_overlay_content_hash",
    "derived_4h_proof_overlay_pair_content_hash",
]
