from __future__ import annotations

from dataclasses import fields
import unittest

from test_lower_timeframe_recursive_proof import build_bundle

from elliott_ai.forecast_records import canonical_sha256
from elliott_ai.lower_timeframe_recursive_proof import (
    RecursiveMultiTimeframeProofVerifier,
    RecursiveProofBundle,
    RecursiveProofNode,
    RecursiveProofReasonCode,
)
from elliott_ai.lower_timeframe_subdivision_verifier import SubdivisionVerificationStatus
from elliott_ai.session_aligned_derived_4h import (
    Derived4hCandidateGraphBinding,
    Derived4hReconstructionResult,
    Derived4hReconstructionStatus,
    Derived4hReasonCode,
    bind_candidate_graph_to_derived_4h,
    nasdaq_session_aligned_4h_policy,
)
from elliott_ai.session_aligned_derived_4h_proof import (
    Derived4hProofBindingKind,
    Derived4hProofBundleResult,
    Derived4hProofEvidenceBinding,
    Derived4hProofOverlay,
    SessionAlignedDerived4hProofVerifier,
)


def _recreate(model_type: type[object], value: object, **changes: object) -> object:
    values = {item.name: getattr(value, item.name) for item in fields(value)}
    values.update(changes)
    values.pop("content_hash", None)
    return model_type.create(**values)


def _not_covered_result(policy_hash: str) -> Derived4hReconstructionResult:
    return Derived4hReconstructionResult.create(
        result_id="fixture-not-covered-result",
        status=Derived4hReconstructionStatus.NOT_COVERED,
        request_hash=canonical_sha256({"fixture": "request"}),
        source_window_hash=canonical_sha256({"fixture": "source-window"}),
        policy_hash=policy_hash,
        schedule_hash=canonical_sha256({"fixture": "schedule"}),
        window=None,
        missing_interval_ids=("fixture:missing-source-interval",),
        reason_codes=(Derived4hReasonCode.BUCKET_COVERAGE_MISSING,),
        warnings=(),
    )


class SessionAlignedDerived4hProofTests(unittest.TestCase):
    def _bundle_with_derived_terminal_gap(self) -> tuple[RecursiveProofBundle, Derived4hProofOverlay, str]:
        base_bundle = build_bundle()
        terminal = next(node for node in base_bundle.nodes if node.proof_timeframe == "4h")
        daily = next(
            node
            for node in base_bundle.nodes
            if terminal.node_id in node.child_node_ids
        )
        derived_daily = _recreate(
            RecursiveProofNode,
            daily,
            child_window=None,
            child_pivot_catalog=(),
            child_coverage=None,
        )
        assert isinstance(derived_daily, RecursiveProofNode)
        terminal_nodes = tuple(
            next(node for node in base_bundle.nodes if node.node_id == child_id)
            for child_id in daily.child_node_ids
        )
        derived_terminals = tuple(
            _recreate(
                RecursiveProofNode,
                item,
                native_window=None,
                native_pivot_catalog=(),
                coverage=None,
            )
            for item in terminal_nodes
        )
        assert all(isinstance(item, RecursiveProofNode) for item in derived_terminals)
        derived_by_id = {item.node_id: item for item in derived_terminals}
        nodes = tuple(
            derived_daily
            if node.node_id == daily.node_id
            else derived_by_id.get(node.node_id, node)
            for node in base_bundle.nodes
        )
        bundle = _recreate(RecursiveProofBundle, base_bundle, nodes=nodes)
        assert isinstance(bundle, RecursiveProofBundle)
        policy = nasdaq_session_aligned_4h_policy()
        assert derived_daily.child_graph is not None
        daily_binding = Derived4hProofEvidenceBinding.create(
            binding_id="fixture-daily-graph-gap",
            binding_kind=Derived4hProofBindingKind.DAILY_CHILD_GRAPH_EVIDENCE,
            node_id=derived_daily.node_id,
            node_content_hash=derived_daily.content_hash,
            reconstruction_result=_not_covered_result(policy.content_hash),
            coverage=None,
            pivot_catalog=(),
            candidate_graph_binding=bind_candidate_graph_to_derived_4h(
                derived_daily.child_graph,
                policy,
            ),
        )
        terminal_bindings = tuple(
            Derived4hProofEvidenceBinding.create(
                binding_id=f"fixture-terminal-gap-{item.node_id}",
                binding_kind=Derived4hProofBindingKind.NODE_EVIDENCE,
                node_id=item.node_id,
                node_content_hash=item.content_hash,
                reconstruction_result=_not_covered_result(policy.content_hash),
                coverage=None,
                pivot_catalog=(),
                candidate_graph_binding=None,
            )
            for item in derived_terminals
        )
        overlay = Derived4hProofOverlay.create(
            overlay_id="fixture-derived-overlay",
            bundle_id=bundle.bundle_id,
            bundle_content_hash=bundle.content_hash,
            policy=policy,
            bindings=(daily_binding, *terminal_bindings),
            shadow_mode=True,
        )
        return bundle, overlay, derived_daily.node_id

    def test_native_verifier_is_unchanged_without_a_derived_overlay(self) -> None:
        native_result = RecursiveMultiTimeframeProofVerifier().verify(
            build_bundle(), shadow_mode=True
        )

        self.assertEqual(native_result.status, SubdivisionVerificationStatus.VERIFIED)
        self.assertTrue(
            any(
                RecursiveProofReasonCode.TERMINAL_4H_VERIFIED in item.reason_codes
                for item in native_result.node_results
                if item.proof_timeframe == "4h"
            )
        )

    def test_missing_derived_source_maps_to_not_covered_without_native_fallback(self) -> None:
        bundle, overlay, daily_id = self._bundle_with_derived_terminal_gap()

        result = SessionAlignedDerived4hProofVerifier().verify(
            bundle, overlay, shadow_mode=True
        )

        self.assertEqual(result.status, SubdivisionVerificationStatus.NOT_COVERED)
        daily_node = next(node for node in bundle.nodes if node.node_id == daily_id)
        self.assertIsNone(daily_node.child_window)
        self.assertTrue(
            all(
                next(node for node in bundle.nodes if node.node_id == child_id).native_window is None
                for child_id in daily_node.child_node_ids
            )
        )
        daily = next(item for item in result.node_results if item.node_id == daily_id)
        self.assertEqual(daily.status, SubdivisionVerificationStatus.NOT_COVERED)
        self.assertIn(
            RecursiveProofReasonCode.DERIVED_4H_SOURCE_NOT_COVERED,
            daily.reason_codes,
        )
        self.assertNotIn(
            RecursiveProofReasonCode.TERMINAL_4H_VERIFIED,
            daily.reason_codes,
        )
        self.assertEqual(Derived4hProofBundleResult.from_dict(result.to_dict()), result)

    def test_shadow_mode_is_mandatory(self) -> None:
        bundle, overlay, _ = self._bundle_with_derived_terminal_gap()

        result = SessionAlignedDerived4hProofVerifier().verify(
            bundle, overlay, shadow_mode=False
        )

        self.assertEqual(result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(
            RecursiveProofReasonCode.SHADOW_MODE_REQUIRED,
            result.base_result.reason_codes,
        )

    def test_overlay_requires_explicit_reachable_binding_and_matching_policy(self) -> None:
        bundle, overlay, _ = self._bundle_with_derived_terminal_gap()
        policy = overlay.policy
        with self.assertRaises(ValueError):
            Derived4hProofOverlay.create(
                overlay_id="empty-overlay",
                bundle_id=bundle.bundle_id,
                bundle_content_hash=bundle.content_hash,
                policy=policy,
                bindings=(),
                shadow_mode=True,
            )

    def test_daily_candidate_graph_is_bound_to_the_exact_aggregation_policy(self) -> None:
        bundle, overlay, _ = self._bundle_with_derived_terminal_gap()
        daily = next(
            node
            for node in bundle.nodes
            if (
                node.proof_timeframe == "daily"
                and node.child_graph is not None
                and node.child_window is None
            )
        )
        assert daily.child_graph is not None
        policy = nasdaq_session_aligned_4h_policy()
        graph_binding = bind_candidate_graph_to_derived_4h(daily.child_graph, policy)
        self.assertEqual(graph_binding.graph_id, daily.child_graph.graph_id)
        self.assertEqual(graph_binding.graph_content_hash, daily.child_graph.content_hash)
        self.assertEqual(graph_binding.policy_hash, policy.content_hash)

        evidence = Derived4hProofEvidenceBinding.create(
            binding_id="daily-graph-policy-fixture",
            binding_kind=Derived4hProofBindingKind.DAILY_CHILD_GRAPH_EVIDENCE,
            node_id=daily.node_id,
            node_content_hash=daily.content_hash,
            reconstruction_result=_not_covered_result(policy.content_hash),
            coverage=None,
            pivot_catalog=(),
            candidate_graph_binding=graph_binding,
        )
        self.assertEqual(
            evidence.candidate_graph_binding.policy_hash,
            policy.content_hash,
        )

        wrong_binding = Derived4hCandidateGraphBinding.create(
            binding_id="wrong-daily-graph-policy",
            graph_id=daily.child_graph.graph_id,
            graph_content_hash=daily.child_graph.content_hash,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            policy_hash=canonical_sha256({"wrong": "derived-policy"}),
        )
        wrong_evidence = Derived4hProofEvidenceBinding.create(
            binding_id="wrong-daily-graph-policy-evidence",
            binding_kind=Derived4hProofBindingKind.DAILY_CHILD_GRAPH_EVIDENCE,
            node_id=daily.node_id,
            node_content_hash=daily.content_hash,
            reconstruction_result=_not_covered_result(policy.content_hash),
            coverage=None,
            pivot_catalog=(),
            candidate_graph_binding=wrong_binding,
        )
        with self.assertRaises(ValueError):
            Derived4hProofOverlay.create(
                overlay_id="wrong-daily-graph-policy-overlay",
                bundle_id=bundle.bundle_id,
                bundle_content_hash=bundle.content_hash,
                policy=policy,
                bindings=(wrong_evidence,),
                shadow_mode=True,
            )

        binding = next(
            item
            for item in overlay.bindings
            if item.binding_kind is Derived4hProofBindingKind.NODE_EVIDENCE
        )
        mismatched_result = _not_covered_result(canonical_sha256({"wrong": "policy"}))
        mismatched_binding = Derived4hProofEvidenceBinding.create(
            binding_id="mismatched-policy-binding",
            binding_kind=binding.binding_kind,
            node_id=binding.node_id,
            node_content_hash=binding.node_content_hash,
            reconstruction_result=mismatched_result,
            coverage=None,
            pivot_catalog=(),
            candidate_graph_binding=None,
        )
        with self.assertRaises(ValueError):
            Derived4hProofOverlay.create(
                overlay_id="mismatched-policy-overlay",
                bundle_id=bundle.bundle_id,
                bundle_content_hash=bundle.content_hash,
                policy=policy,
                bindings=(mismatched_binding,),
                shadow_mode=True,
            )

    def test_partial_derived_daily_branch_cannot_fall_back_to_native_4h(self) -> None:
        bundle, overlay, _ = self._bundle_with_derived_terminal_gap()
        missing_leaf_binding = next(
            item
            for item in overlay.bindings
            if item.binding_kind is Derived4hProofBindingKind.NODE_EVIDENCE
        )
        partial_overlay = Derived4hProofOverlay.create(
            overlay_id="partial-derived-branch",
            bundle_id=bundle.bundle_id,
            bundle_content_hash=bundle.content_hash,
            policy=overlay.policy,
            bindings=tuple(
                item
                for item in overlay.bindings
                if item.binding_id != missing_leaf_binding.binding_id
            ),
            shadow_mode=True,
        )

        result = SessionAlignedDerived4hProofVerifier().verify(
            bundle, partial_overlay, shadow_mode=True
        )

        self.assertEqual(result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(
            RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID,
            result.base_result.reason_codes,
        )

    def test_tampering_or_detached_binding_is_not_accepted_as_proof(self) -> None:
        bundle, overlay, _ = self._bundle_with_derived_terminal_gap()
        original = next(
            item
            for item in overlay.bindings
            if item.binding_kind is Derived4hProofBindingKind.NODE_EVIDENCE
        )
        detached_binding = Derived4hProofEvidenceBinding.create(
            binding_id="detached-binding",
            binding_kind=original.binding_kind,
            node_id="not-a-reachable-node",
            node_content_hash=canonical_sha256({"detached": "node"}),
            reconstruction_result=original.reconstruction_result,
            coverage=None,
            pivot_catalog=(),
            candidate_graph_binding=None,
        )
        detached_overlay = Derived4hProofOverlay.create(
            overlay_id="detached-overlay",
            bundle_id=bundle.bundle_id,
            bundle_content_hash=bundle.content_hash,
            policy=overlay.policy,
            bindings=(detached_binding,),
            shadow_mode=True,
        )

        result = SessionAlignedDerived4hProofVerifier().verify(
            bundle, detached_overlay, shadow_mode=True
        )

        self.assertEqual(result.status, SubdivisionVerificationStatus.INCONSISTENT)
        self.assertIn(
            RecursiveProofReasonCode.DERIVED_4H_BINDING_INVALID,
            result.base_result.reason_codes,
        )


if __name__ == "__main__":
    unittest.main()
