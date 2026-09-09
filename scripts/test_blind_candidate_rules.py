"""Offline contract tests for the closed BlindCandidateRulesPack v1.0."""

from __future__ import annotations

import json
import unittest

from elliott_ai.blind_candidate_rules import (
    BLIND_CANDIDATE_RULES_PACK_ID,
    BLIND_CANDIDATE_RULES_PACK_SCHEMA_VERSION,
    BlindCandidateRuleClass,
    BlindCandidateRulesPack,
    blind_candidate_rules_pack_content_hash,
    default_blind_candidate_rules_pack,
)


class BlindCandidateRulesPackTests(unittest.TestCase):
    def test_default_pack_is_static_canonical_and_symbol_independent(self) -> None:
        first = default_blind_candidate_rules_pack()
        second = default_blind_candidate_rules_pack()

        self.assertEqual(first.pack_id, BLIND_CANDIDATE_RULES_PACK_ID)
        self.assertEqual(first.schema_version, BLIND_CANDIDATE_RULES_PACK_SCHEMA_VERSION)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.content_hash, blind_candidate_rules_pack_content_hash(first))
        self.assertEqual(
            set(first.source_references),
            {
                "elliott_ai.agent.SYSTEM_PROMPT",
                "elliott_ai.complete_history_shadow_runner",
                "elliott_ai.correction_semantics",
                "elliott_ai.lower_timeframe_candidate_generator",
                "elliott_ai.lower_timeframe_recursive_proof",
            },
        )
        self.assertIn(
            BlindCandidateRuleClass.HARD_STRUCTURAL,
            {rule.rule_class for rule in first.rules},
        )
        self.assertIn(
            BlindCandidateRuleClass.SOFT_RANKING,
            {rule.rule_class for rule in first.rules},
        )
        self.assertIn(
            BlindCandidateRuleClass.PACKET_GUARDRAIL,
            {rule.rule_class for rule in first.rules},
        )

    def test_pack_contains_required_hard_and_soft_rules(self) -> None:
        pack = default_blind_candidate_rules_pack()
        rules = {rule.rule_id: rule for rule in pack.rules}

        self.assertTrue(
            {
                "price_first_catalog_pivots_only",
                "complete_history_scope",
                "connected_boundaries",
                "motive_and_diagonal_families",
                "correction_family_structure",
                "wave_2_origin_rule",
                "wave_3_not_shortest",
                "standard_impulse_wave_4_nonoverlap",
                "independent_primary_alternative",
                "typed_invalidation_required",
                "wave_3_extension_is_soft",
                "fibonacci_duration_channeling_soft_only",
                "indicator_soft_only",
                "indicator_availability",
            }.issubset(rules)
        )
        self.assertIn("soft ranking", rules["wave_3_extension_is_soft"].instruction)
        self.assertIn("cannot create pivots", rules["indicator_soft_only"].instruction)
        self.assertIn("does not contain calculated", rules["indicator_availability"].instruction)

    def test_forbidden_symbol_or_historical_content_cannot_enter_pack(self) -> None:
        payload = default_blind_candidate_rules_pack().to_dict()
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).lower()
        for forbidden in (
            "googl",
            "nasdaq",
            "msft",
            "nflx",
            "ai_brain_current",
            "accepted_case",
            "mistake_memory",
        ):
            self.assertNotIn(forbidden, encoded)

        injected_rule = default_blind_candidate_rules_pack().to_dict()
        injected_rule["rules"][0]["instruction"] = "Use a prior ticker-specific example."
        with self.assertRaises(ValueError):
            BlindCandidateRulesPack.from_dict(injected_rule)

        injected_source = default_blind_candidate_rules_pack().to_dict()
        injected_source["source_references"].append("AI_BRAIN_CURRENT.md")
        with self.assertRaises(ValueError):
            BlindCandidateRulesPack.from_dict(injected_source)

        injected_field = default_blind_candidate_rules_pack().to_dict()
        injected_field["historical_context"] = "forbidden"
        with self.assertRaises(ValueError):
            BlindCandidateRulesPack.from_dict(injected_field)

    def test_round_trip_and_tampering_detection_are_deterministic(self) -> None:
        pack = default_blind_candidate_rules_pack()
        restored = BlindCandidateRulesPack.from_dict(pack.to_dict())
        self.assertEqual(restored, pack)

        tampered_hash = pack.to_dict()
        tampered_hash["content_hash"] = "0" * 64
        with self.assertRaises(ValueError):
            BlindCandidateRulesPack.from_dict(tampered_hash)


if __name__ == "__main__":
    unittest.main()
