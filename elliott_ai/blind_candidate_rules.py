"""Controlled, symbol-independent rules for blind candidate generation.

The complete-history shadow runner must not retrieve indexed brain material,
historical examples, prior analyses, outcomes, or lessons before it creates
independent Primary and Alternative candidate graphs.  This module therefore
contains one closed, versioned rules pack distilled from the repository's
approved deterministic Elliott-rule code.  It performs no file, database, or
knowledge-store access.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .forecast_records import canonical_sha256


BLIND_CANDIDATE_RULES_PACK_SCHEMA_VERSION = "blind-candidate-rules-pack-1.0.0"
BLIND_CANDIDATE_RULES_PACK_ID = "blind-candidate-rules-pack-v1"


class BlindCandidateRuleClass(StrEnum):
    HARD_STRUCTURAL = "hard_structural"
    SOFT_RANKING = "soft_ranking"
    PACKET_GUARDRAIL = "packet_guardrail"


def _text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")
    return value.strip()


def _hash(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash.")
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}.")


@dataclass(frozen=True, slots=True)
class BlindCandidateRule:
    """One source-traceable, generic instruction in the closed rules pack."""

    rule_id: str
    rule_class: BlindCandidateRuleClass
    source_reference: str
    instruction: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _text(self.rule_id, field_name="rule_id"))
        if not isinstance(self.rule_class, BlindCandidateRuleClass):
            try:
                object.__setattr__(self, "rule_class", BlindCandidateRuleClass(self.rule_class))
            except (TypeError, ValueError) as exc:
                raise ValueError("rule_class must be a controlled rule class.") from exc
        object.__setattr__(self, "source_reference", _text(self.source_reference, field_name="source_reference"))
        object.__setattr__(self, "instruction", _text(self.instruction, field_name="instruction"))

    def to_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "rule_class": self.rule_class.value,
            "source_reference": self.source_reference,
            "instruction": self.instruction,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BlindCandidateRule":
        if not isinstance(value, Mapping):
            raise TypeError("BlindCandidateRule must be an object.")
        raw = dict(value)
        expected = {field.name for field in fields(cls)}
        unknown = sorted(set(raw) - expected)
        missing = sorted(expected - set(raw))
        if unknown or missing:
            detail = []
            if unknown:
                detail.append("unknown=" + ", ".join(unknown))
            if missing:
                detail.append("missing=" + ", ".join(missing))
            raise ValueError("BlindCandidateRule has invalid fields: " + "; ".join(detail))
        return cls(**raw)


# These references identify approved code sources only. The pack intentionally
# does not read them at runtime: dynamic retrieval could pull indexed examples
# or symbol-specific context into an otherwise blind candidate request.
_CANONICAL_RULE_SOURCES = (
    "elliott_ai.agent.SYSTEM_PROMPT",
    "elliott_ai.complete_history_shadow_runner",
    "elliott_ai.correction_semantics",
    "elliott_ai.lower_timeframe_candidate_generator",
    "elliott_ai.lower_timeframe_recursive_proof",
)

_CANONICAL_RULES = (
    BlindCandidateRule(
        rule_id="price_first_catalog_pivots_only",
        rule_class=BlindCandidateRuleClass.HARD_STRUCTURAL,
        source_reference="elliott_ai.agent.SYSTEM_PROMPT",
        instruction="Build candidate structure from price first and use only typed pivot IDs supplied in the immutable pivot catalog.",
    ),
    BlindCandidateRule(
        rule_id="complete_history_scope",
        rule_class=BlindCandidateRuleClass.HARD_STRUCTURAL,
        source_reference="elliott_ai.complete_history_shadow_runner",
        instruction="Cover the approved complete analysis window. Only the narrowly bounded IPO origin-control interval may sit outside an explicit top-level wave.",
    ),
    BlindCandidateRule(
        rule_id="connected_boundaries",
        rule_class=BlindCandidateRuleClass.HARD_STRUCTURAL,
        source_reference="elliott_ai.complete_history_shadow_runner",
        instruction="Connect every child boundary to its parent through supplied typed pivot lineage; do not omit, merge, or silently skip an interval.",
    ),
    BlindCandidateRule(
        rule_id="motive_and_diagonal_families",
        rule_class=BlindCandidateRuleClass.HARD_STRUCTURAL,
        source_reference="elliott_ai.lower_timeframe_candidate_generator",
        instruction="Use only permitted motive families: impulse or diagonal. A diagonal declaration remains a candidate until separate deterministic geometry proof succeeds.",
    ),
    BlindCandidateRule(
        rule_id="correction_family_structure",
        rule_class=BlindCandidateRuleClass.HARD_STRUCTURAL,
        source_reference="elliott_ai.correction_semantics",
        instruction="Use permitted corrective families with their actual child structures: zigzag 5-3-5, flat 3-3-5, triangle 3-3-3-3-3, combination W-X-Y, or combination W-X-Y-X2-Z.",
    ),
    BlindCandidateRule(
        rule_id="wave_2_origin_rule",
        rule_class=BlindCandidateRuleClass.HARD_STRUCTURAL,
        source_reference="elliott_ai.lower_timeframe_recursive_proof",
        instruction="A motive Wave 2 cannot move beyond the origin of Wave 1.",
    ),
    BlindCandidateRule(
        rule_id="wave_3_not_shortest",
        rule_class=BlindCandidateRuleClass.HARD_STRUCTURAL,
        source_reference="elliott_ai.lower_timeframe_recursive_proof",
        instruction="For a motive five-wave candidate, Wave 3 cannot be the shortest actionary wave among Waves 1, 3, and 5.",
    ),
    BlindCandidateRule(
        rule_id="standard_impulse_wave_4_nonoverlap",
        rule_class=BlindCandidateRuleClass.HARD_STRUCTURAL,
        source_reference="elliott_ai.lower_timeframe_recursive_proof",
        instruction="For a standard impulse candidate, Wave 4 cannot overlap Wave 1 price territory. A claimed diagonal does not bypass its separate geometry proof.",
    ),
    BlindCandidateRule(
        rule_id="independent_primary_alternative",
        rule_class=BlindCandidateRuleClass.PACKET_GUARDRAIL,
        source_reference="elliott_ai.complete_history_shadow_runner",
        instruction="Generate Primary and Alternative independently over identical analysis-window boundaries; neither request may receive the other count.",
    ),
    BlindCandidateRule(
        rule_id="typed_invalidation_required",
        rule_class=BlindCandidateRuleClass.HARD_STRUCTURAL,
        source_reference="elliott_ai.lower_timeframe_candidate_generator",
        instruction="Every candidate wave requires a typed invalidation with a supplied source pivot, explicit threshold, direction, and evaluation basis.",
    ),
    BlindCandidateRule(
        rule_id="wave_3_extension_is_soft",
        rule_class=BlindCandidateRuleClass.SOFT_RANKING,
        source_reference="elliott_ai.agent.SYSTEM_PROMPT",
        instruction="Wave 3 often extends is a soft ranking tendency only; it cannot create pivots, choose a family, or verify a wave.",
    ),
    BlindCandidateRule(
        rule_id="fibonacci_duration_channeling_soft_only",
        rule_class=BlindCandidateRuleClass.SOFT_RANKING,
        source_reference="elliott_ai.agent.SYSTEM_PROMPT",
        instruction="Fibonacci relationships, duration, and channeling are soft ranking evidence only after price structure is proposed.",
    ),
    BlindCandidateRule(
        rule_id="indicator_soft_only",
        rule_class=BlindCandidateRuleClass.SOFT_RANKING,
        source_reference="elliott_ai.agent.SYSTEM_PROMPT",
        instruction="RSI, volume, EWO, and MACD are soft ranking evidence only; indicators cannot create pivots, determine families, or verify waves.",
    ),
    BlindCandidateRule(
        rule_id="indicator_availability",
        rule_class=BlindCandidateRuleClass.PACKET_GUARDRAIL,
        source_reference="elliott_ai.agent.SYSTEM_PROMPT",
        instruction="Do not claim indicator evidence when the packet does not contain calculated, compatible indicator values.",
    ),
)

_CANONICAL_RULE_PAYLOAD = tuple(rule.to_dict() for rule in _CANONICAL_RULES)


@dataclass(frozen=True, slots=True)
class BlindCandidateRulesPack:
    """The sole rules context allowed into blind complete-history requests."""

    pack_id: str
    schema_version: str
    source_references: tuple[str, ...]
    rules: tuple[BlindCandidateRule, ...]
    content_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pack_id", _text(self.pack_id, field_name="pack_id"))
        object.__setattr__(self, "schema_version", _text(self.schema_version, field_name="schema_version"))
        if self.pack_id != BLIND_CANDIDATE_RULES_PACK_ID:
            raise ValueError("BlindCandidateRulesPack pack_id is not approved for blind candidate generation.")
        if self.schema_version != BLIND_CANDIDATE_RULES_PACK_SCHEMA_VERSION:
            raise ValueError("BlindCandidateRulesPack schema_version is not supported.")
        if isinstance(self.source_references, (str, bytes)) or not isinstance(self.source_references, Sequence):
            raise TypeError("source_references must be a sequence.")
        sources = tuple(_text(item, field_name="source_references") for item in self.source_references)
        if sources != _CANONICAL_RULE_SOURCES:
            raise ValueError("BlindCandidateRulesPack must use only the controlled approved code-source references.")
        object.__setattr__(self, "source_references", sources)
        if isinstance(self.rules, (str, bytes)) or not isinstance(self.rules, Sequence):
            raise TypeError("rules must be a sequence.")
        rules = tuple(self.rules)
        if not all(isinstance(item, BlindCandidateRule) for item in rules):
            raise TypeError("rules must contain BlindCandidateRule values.")
        if tuple(item.to_dict() for item in rules) != _CANONICAL_RULE_PAYLOAD:
            raise ValueError("BlindCandidateRulesPack cannot contain historical, symbol-specific, or unapproved rule content.")
        object.__setattr__(self, "rules", rules)
        if self.content_hash and _hash(self.content_hash, field_name="content_hash") != blind_candidate_rules_pack_content_hash(self):
            raise ValueError("BlindCandidateRulesPack content_hash does not match its canonical payload.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "schema_version": self.schema_version,
            "source_references": list(self.source_references),
            "rules": [item.to_dict() for item in self.rules],
            "content_hash": self.content_hash,
        }

    @classmethod
    def create(cls, **values: Any) -> "BlindCandidateRulesPack":
        if values.pop("content_hash", ""):
            raise ValueError("create() calculates content_hash; do not supply it.")
        result = cls(**values)
        return replace(result, content_hash=blind_candidate_rules_pack_content_hash(result))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, verify_hash: bool = True) -> "BlindCandidateRulesPack":
        if not isinstance(value, Mapping):
            raise TypeError("BlindCandidateRulesPack must be an object.")
        raw = dict(value)
        expected = {field.name for field in fields(cls)}
        unknown = sorted(set(raw) - expected)
        missing = sorted(expected - set(raw))
        if unknown or missing:
            detail = []
            if unknown:
                detail.append("unknown=" + ", ".join(unknown))
            if missing:
                detail.append("missing=" + ", ".join(missing))
            raise ValueError("BlindCandidateRulesPack has invalid fields: " + "; ".join(detail))
        raw["rules"] = tuple(BlindCandidateRule.from_dict(item) for item in raw["rules"])
        raw["source_references"] = tuple(raw["source_references"])
        result = cls(**raw)
        if verify_hash and result.content_hash != blind_candidate_rules_pack_content_hash(result):
            raise ValueError("BlindCandidateRulesPack content_hash does not match its canonical payload.")
        return result


def blind_candidate_rules_pack_content_hash(value: BlindCandidateRulesPack | Mapping[str, Any]) -> str:
    payload = value.to_dict() if isinstance(value, BlindCandidateRulesPack) else _json_value(value)
    if not isinstance(payload, Mapping):
        raise TypeError("BlindCandidateRulesPack hash input must be an object.")
    canonical_payload = dict(payload)
    canonical_payload.pop("content_hash", None)
    return canonical_sha256(canonical_payload)


def default_blind_candidate_rules_pack() -> BlindCandidateRulesPack:
    """Return the single closed v1.0 pack used by blind candidate requests."""

    return BlindCandidateRulesPack.create(
        pack_id=BLIND_CANDIDATE_RULES_PACK_ID,
        schema_version=BLIND_CANDIDATE_RULES_PACK_SCHEMA_VERSION,
        source_references=_CANONICAL_RULE_SOURCES,
        rules=_CANONICAL_RULES,
    )


__all__ = [
    "BLIND_CANDIDATE_RULES_PACK_ID",
    "BLIND_CANDIDATE_RULES_PACK_SCHEMA_VERSION",
    "BlindCandidateRule",
    "BlindCandidateRuleClass",
    "BlindCandidateRulesPack",
    "blind_candidate_rules_pack_content_hash",
    "default_blind_candidate_rules_pack",
]
