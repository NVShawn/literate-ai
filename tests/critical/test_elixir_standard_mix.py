"""Standard Mix plan and current grant ownership; native proof is opt-in."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

from literate_ai.adapters.builders.elixir import ElixirToolchain
from literate_ai.adapters.builders.hex import HexToolchain
from literate_ai.adapters.lifecycle.standard_local import (
    LocalStandardLifecycleError,
)
from literate_ai.adapters.lifecycle.standard_mix import (
    StandardMixLifecyclePorts,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
)
from literate_ai.security import AuthorizationError, AuthorizationRevocationSet
from tests.support.fixtures_test_elixir_standard_mix import (
    _system,
)


class StandardMixAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for cls in (HexToolchain, ElixirToolchain):
            patch = mock.patch.object(cls, "require_unchanged")
            patch.start()
            self.addCleanup(patch.stop)

    def test_source_inventory_preserves_crlf_bytes(self):
        source = self.root / "source"
        source.mkdir()
        content = b"IO.puts(42)\r\n# retained source\r\n"
        (source / "main.exs").write_bytes(content)
        files = StandardMixLifecyclePorts._source_text_files(
            self.root, exact_bytes=True
        )
        self.assertEqual(files["source/main.exs"].encode("utf-8"), content)
        # Other ecosystems keep universal-newline admission text.
        self.assertEqual(
            StandardMixLifecyclePorts._source_text_files(self.root)["source/main.exs"],
            "IO.puts(42)\n# retained source\n",
        )

    def test_only_mix_components_bind_exact_source_bytes(self):
        ports, plan, _, _, _ = _system(self.root, verifier=AuthorizationRevocationSet())
        from literate_ai.contracts import canonical_identity

        self.assertTrue(ports._source_bytes_exact(plan.component_revision))
        self.assertFalse(
            ports._source_bytes_exact(canonical_identity({"other": "component"}))
        )

    def test_source_bundle_and_canonical_tree_are_distinct(self):
        from literate_ai.adapters.builders.python import canonical_tree_digest

        ports, plan, intent, authorization, source = _system(
            self.root, verifier=AuthorizationRevocationSet()
        )
        self.assertNotEqual(
            intent.source_bundle_identity.uri, canonical_tree_digest(source)
        )
        self.assertEqual(ports._require_mix_authority(plan), (intent, authorization))
        self.assertEqual(
            intent.build_request.requested_privileges,
            ("execute-build-tools", "network-access"),
        )
        self.assertEqual(len(plan.manifest.actions), 2)

    def test_default_verifier_refuses_before_native_process(self):
        ports, plan, _, _, _ = _system(self.root)
        with mock.patch(
            "literate_ai.adapters.builders.mix_project.run_bounded_process"
        ) as process:
            with self.assertRaises(AuthorizationError):
                ports.build(plan, ())
            process.assert_not_called()

    def test_revocation_refuses_before_native_process(self):
        verifier = AuthorizationRevocationSet()
        ports, plan, _, authorization, _ = _system(self.root, verifier=verifier)
        ports.mix_authorization_verifier = verifier.revoke(
            authorization.grant.authorization_id, actor="fixture", reason="revoked"
        )
        with self.assertRaises(AuthorizationError):
            ports.build(plan, ())

    def test_expired_grant_and_changed_source_fail(self):
        ports, plan, _, authorization, source = _system(
            self.root, verifier=AuthorizationRevocationSet()
        )
        ports.clock = lambda: authorization.grant.expires_at + timedelta(seconds=1)
        with self.assertRaises(AuthorizationError):
            ports._require_mix_authority(plan)
        ports.clock = lambda: authorization.grant.issued_at
        (source / "source/main.exs").write_text("changed")
        with self.assertRaises(LocalStandardLifecycleError):
            ports._require_mix_authority(plan)

    def test_forged_authorization_and_unfinalized_plan_fail(self):
        ports, plan, intent, authorization, _ = _system(
            self.root, verifier=AuthorizationRevocationSet()
        )
        foreign = replace(
            authorization,
            grant=replace(authorization.grant, authorization_id="foreign"),
        )
        with self.assertRaises(LocalStandardLifecycleError):
            ports.finalize(intent, foreign)
        ports._plans_by_revision.clear()
        with self.assertRaises(LocalStandardLifecycleError):
            ports._require_mix_authority(plan)

    def test_wrong_target_or_source_history_is_rejected(self):
        ports, plan, intent, _, _ = _system(
            self.root, verifier=AuthorizationRevocationSet()
        )
        document = {
            "standard_authority": {
                "schema": "literate-ai/standard-mix-build@1",
                "target_identity": "foreign",
            },
            "request": intent.build_request.to_dict(),
        }
        with self.assertRaises(LocalStandardLifecycleError):
            ports._history_verifier(plan, document)

    def test_historical_context_survives_latest_plan_without_authorizing_execution(
        self,
    ):
        ports, plan, intent, authorization, _ = _system(
            self.root, verifier=AuthorizationRevocationSet()
        )
        document = {
            "standard_authority": {
                "schema": "literate-ai/standard-mix-build@1",
                "build_plan_identity": plan.identity.uri,
                "target_identity": ports.mix_targets[
                    plan.component_revision.uri
                ].identity.uri,
                "source_tree_identity": intent.source_tree_identity.uri,
                "source_bundle_identity": intent.source_bundle_identity.uri,
                "provider_source_inputs": [],
            },
            "request": intent.build_request.to_dict(),
            "provider_libraries": [],
        }
        ports._plans_by_revision.clear()
        ports.mix_authorization_verifier = AuthorizationRevocationSet().revoke(
            authorization.grant.authorization_id,
            actor="fixture",
            reason="historical grants do not authorize current execution",
        )
        ports._history_verifier(plan, document, require_live=False)
        with self.assertRaises(LocalStandardLifecycleError):
            ports._history_verifier(plan, document)
        document["standard_authority"]["build_plan_identity"] = "foreign"
        with self.assertRaises(LocalStandardLifecycleError):
            ports._history_verifier(plan, document, require_live=False)
        ports._mix_finalized_plans.clear()
        with self.assertRaises(LocalStandardLifecycleError):
            ports._require_mix_authority(plan, require_source=False, require_live=False)

    def test_plan_finalization_requires_the_contract_bound_mix_binding(self):
        from literate_ai.application.standard_project_lifecycle import (
            StandardProjectLifecycleError,
        )

        ports, plan, intent, authorization, _ = _system(
            self.root, verifier=AuthorizationRevocationSet()
        )
        inputs = ports.plan_finalization_inputs(intent, authorization)
        contract = ports._contract(plan.component_revision)
        self.assertEqual(inputs.dependency_resolution, "mix")
        self.assertEqual(
            inputs.build_binding,
            (
                contract.locked_build_authority_identity.uri,
                contract.build_system_toolchain_identity.uri,
            ),
        )
        self.assertEqual(inputs.finalize().identity, plan.identity)
        builder, toolchain = inputs.build_binding
        for name, changes in (
            ("mix without binding", {"build_binding": None}),
            ("binding without mix", {"dependency_resolution": "npm"}),
            ("binding without mix", {"dependency_resolution": "none"}),
            ("malformed binding", {"build_binding": (builder,)}),
            ("malformed binding", {"build_binding": [builder, toolchain]}),
            ("malformed binding", {"build_binding": (builder, "")}),
            ("foreign builder", {"build_binding": ("sha256:" + "0" * 64, toolchain)}),
            ("foreign driver", {"build_binding": (builder, "sha256:" + "0" * 64)}),
            ("swapped binding", {"build_binding": (toolchain, builder)}),
        ):
            with (
                self.subTest(name, changes=changes),
                self.assertRaises(StandardProjectLifecycleError) as caught,
            ):
                replace(inputs, **changes).finalize()
            self.assertEqual(
                caught.exception.code, "standard_lifecycle.plan_inputs_mismatch"
            )

    def test_only_the_mix_bound_intent_and_request_are_retained(self):
        import json

        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceRecorder,
        )

        recorder = QualificationEvidenceRecorder(
            max_bytes=32 * 1024 * 1024, max_records=1000
        )
        ports, _, intent, _, _ = _system(
            self.root, verifier=AuthorizationRevocationSet(), evidence_recorder=recorder
        )
        requests = [
            document
            for document in (json.loads(item) for item in recorder._records.values())
            if isinstance(document, dict)
            and "builder_id" in document
            and "requested_privileges" in document
        ]
        self.assertEqual(requests, [intent.build_request.to_dict()])
        self.assertIn(intent.identity, recorder._records)

    def test_worker_built_mix_intent_is_refused_as_local_only(self):
        ports, plan, intent, _, _ = _system(
            self.root, verifier=AuthorizationRevocationSet()
        )
        with self.assertRaisesRegex(LocalStandardLifecycleError, "local-only"):
            ports.accept_build_intent(None, plan, None, (), (), intent)

    def test_runtime_without_external_checkpoint_is_rejected(self):
        ports, plan, _, _, source = _system(
            self.root, verifier=AuthorizationRevocationSet()
        )
        contract = ports._contract(plan.component_revision)
        with self.assertRaisesRegex(LocalStandardLifecycleError, "sealed"):
            ports._locked_argv(
                contract,
                ComponentCommandPhase.EXECUTE,
                source_root=source,
                object_root=self.root / "objects",
                artifact_root=self.root / "unknown",
                export_path=self.root / "unknown/app",
                providers=(),
            )


if __name__ == "__main__":
    unittest.main()
