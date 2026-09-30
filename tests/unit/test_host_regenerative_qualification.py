from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.source_to_specification import (
    LEGACY_QUALIFICATION_BLOCKER,
    LocalFilesystemQualificationCheckpointStore,
    LocalHmacQualificationAttestor,
    LocalHostParityVerifier,
    LocalHostSpecRegenerator,
    LocalQualificationCase,
    LocalQualificationProfile,
    ParityVerificationRequest,
    RegenerationRunPlan,
    RegenerativeAuthority,
    RegenerativeQualificationPolicy,
    canonical_digest,
    inventory_source,
    run_regenerative_qualification,
)
from literate_ai.source_to_specification.errors import SourceToSpecificationError
from literate_ai.source_to_specification.host_qualification import _run


class LocalHostQualificationTests(unittest.TestCase):
    def test_hmac_attestor_verifies_only_exact_payload_and_identity(self) -> None:
        attestor = LocalHmacQualificationAttestor(signer="test-operator", key=b"x" * 32)
        payload = {"measured": {"passed": True}}
        attestation_id = attestor.attest(payload)

        self.assertTrue(attestor.verify(payload, attestation_id))
        self.assertFalse(
            attestor.verify({"measured": {"passed": False}}, attestation_id)
        )
        self.assertFalse(attestor.verify(payload, canonical_digest("another")))
        self.assertNotEqual(
            attestor.provider_identity,
            LocalHmacQualificationAttestor(
                signer="test-operator", key=b"y" * 32
            ).provider_identity,
        )

    def test_checkpoint_store_fails_closed_on_malformed_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalFilesystemQualificationCheckpointStore(root)
            checkpoint_key = canonical_digest("checkpoint")
            checkpoint_path = root / f"{checkpoint_key.removeprefix('sha256:')}.json"
            checkpoint_path.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(SourceToSpecificationError, "malformed schema"):
                store.load(checkpoint_key)

    def test_structured_cli_failure_retains_safe_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "fail.py"
            script.write_text(
                "import json,sys\n"
                "print(json.dumps({'schema':'literate-ai/cli-error@1','ok':False,"
                "'error':{'code':'generate.fixture_failed',"
                "'message':'bounded diagnostic'}}),file=sys.stderr)\n"
                "raise SystemExit(2)\n"
            )
            with self.assertRaises(SourceToSpecificationError) as error:
                _run(
                    (sys.executable, str(script)),
                    cwd=root,
                    environment=os.environ,
                    timeout_seconds=10,
                    maximum_output_bytes=4096,
                )
        self.assertEqual(error.exception.code, "qualification_host.command_failed")
        self.assertIn(
            "generate.fixture_failed: bounded diagnostic", str(error.exception)
        )

    def test_generated_application_failure_is_measured_as_failed_parity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "app.py").write_text(
                "import json,sys\nprint(json.dumps(json.loads(sys.argv[1])))\n"
            )
            generated = root / "generated"
            generated_source = generated / "source"
            generated_source.mkdir(parents=True)
            (generated_source / "app.py").write_text("raise SystemExit(3)\n")
            source_snapshot_id = inventory_source(source).identity
            profile = LocalQualificationProfile(
                profile_id="measured-failure@1",
                build_command=(sys.executable, "-m", "compileall", "-q", "."),
                test_commands=((sys.executable, "-m", "compileall", "-q", "."),),
                source_command=(sys.executable, "app.py"),
                generated_command=(sys.executable, "app.py"),
                cases=(
                    LocalQualificationCase("value-one", ({"value": 1},), {"value": 1}),
                ),
                covered_surface_ids=("surface.value",),
            )
            result = LocalHostParityVerifier(
                source_root=source,
                source_snapshot_id=source_snapshot_id,
                profile=profile,
            ).verify(
                ParityVerificationRequest(
                    run_id=canonical_digest("run"),
                    source_snapshot_id=source_snapshot_id,
                    specification_set_id=canonical_digest("specification"),
                    target_profile_id=canonical_digest("target"),
                    generated_tree_id=inventory_source(generated).identity,
                    covered_surface_ids=profile.covered_surface_ids,
                ),
                generated,
            )
            self.assertFalse(result.passed)

    def _run_unstructured_test_command(self, test_body: str, *, nested: bool = False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            application = (
                "import json,sys\n"
                "value=json.loads(sys.argv[1])[0]\n"
                "print(json.dumps({'value': value['value'] * 2}, sort_keys=True))\n"
            )
            (source / "app.py").write_text(application)
            source_snapshot_id = inventory_source(source).identity

            generator = root / "generate.py"
            generated_parent = "root/'node'" if nested else "root"
            generator.write_text(
                "from pathlib import Path\n"
                "import json,sys\n"
                "root=Path(sys.argv[1])\n"
                f"component={generated_parent}\n"
                "native=component/'source'\n"
                "native.mkdir(parents=True,exist_ok=True)\n"
                f"(native/'app.py').write_text({application!r})\n"
                f"(native/'test_app.py').write_text({test_body!r})\n"
                + (
                    "print(json.dumps({'result':{'standard_source_generation':"
                    "{'root_revision':{'digest':'root'},'components':["
                    "{'component_revision':{'digest':'root'},"
                    "'workspace_locator':str(component)}]}}}))\n"
                    if nested
                    else ""
                )
            )
            profile = LocalQualificationProfile(
                profile_id="portable-double@1",
                build_command=(sys.executable, "-m", "compileall", "-q", "."),
                test_commands=((sys.executable, "test_app.py"),),
                source_command=(sys.executable, "app.py"),
                generated_command=(sys.executable, "app.py"),
                cases=(
                    LocalQualificationCase("zero", ({"value": 0},), {"value": 0}),
                    LocalQualificationCase("seven", ({"value": 7},), {"value": 14}),
                ),
                covered_surface_ids=("surface.double",),
            )
            specification_set_id = canonical_digest("specification")
            target_profile_id = canonical_digest("host-python")
            flavor_lock_id = canonical_digest("python-flavor")
            recipe_id = canonical_digest("recipe")
            plans = tuple(
                RegenerationRunPlan(
                    run_id=canonical_digest({"run": index}),
                    specification_set_id=specification_set_id,
                    target_profile_id=target_profile_id,
                    flavor_lock_id=flavor_lock_id,
                    generation_recipe_id=recipe_id,
                    covered_surface_ids=profile.covered_surface_ids,
                )
                for index in range(2)
            )
            regenerator = LocalHostSpecRegenerator(
                generation_command=(
                    sys.executable,
                    str(generator),
                    "{workspace}",
                ),
                generation_cwd=root,
                profile=profile,
            )
            verifier = LocalHostParityVerifier(
                source_root=source,
                source_snapshot_id=source_snapshot_id,
                profile=profile,
            )
            attestor = LocalHmacQualificationAttestor(
                signer="test-operator", key=b"x" * 32
            )
            result = run_regenerative_qualification(
                source_snapshot_id=source_snapshot_id,
                specification_set_id=specification_set_id,
                policy=RegenerativeQualificationPolicy(
                    policy_id="two-clean-host-runs@1",
                    minimum_clean_runs=2,
                    required_target_profile_ids=(target_profile_id,),
                    required_surface_ids=profile.covered_surface_ids,
                ),
                plans=plans,
                regenerator=regenerator,
                parity_verifier=verifier,
                attestor=attestor,
                scratch_root=root,
            )
            envelopes = tuple(attestor.envelopes)
        return result, envelopes

    def test_successful_unstructured_test_commands_remain_historical(self) -> None:
        cases = (
            ("assertion-only", "assert True\n"),
            ("empty", ""),
            ("unparsed-output", "print('not a typed test report')\n"),
        )
        for label, test_body in cases:
            with self.subTest(label=label):
                result, envelopes = self._run_unstructured_test_command(test_body)
                self.assertFalse(result.decision.qualified)
                self.assertEqual(
                    result.decision.effective_authority,
                    RegenerativeAuthority.SOURCE_BASELINE,
                )
                self.assertIn(LEGACY_QUALIFICATION_BLOCKER, result.decision.blockers)
                self.assertEqual(len(result.evidence), 2)
                self.assertTrue(
                    all(item.generated_tests_succeeded == 1 for item in result.evidence)
                )
                self.assertEqual(len(envelopes), 2)
                self.assertEqual(
                    {item["schema"] for item in envelopes},
                    {"urn:literate-ai:schema:v2:local-qualification-signature"},
                )

    def test_nested_standard_generation_locates_the_root_component(self) -> None:
        result, _envelopes = self._run_unstructured_test_command(
            "assert True\n", nested=True
        )
        self.assertEqual(len(result.evidence), 2)
        self.assertTrue(all(item.build_passed for item in result.evidence))
        self.assertTrue(all(item.independent_parity_passed for item in result.evidence))

    def test_profile_is_strict_versioned_execution_data_not_evidence(self) -> None:
        profile = LocalQualificationProfile(
            profile_id="strict@1",
            build_command=("build",),
            test_commands=(("test",),),
            source_command=("source",),
            generated_command=("generated",),
            cases=(LocalQualificationCase("value-one", ({"value": 1},), {"value": 1}),),
            covered_surface_ids=("surface",),
        )
        parsed = LocalQualificationProfile.from_dict(
            json.loads(json.dumps(profile.to_dict()))
        )
        self.assertEqual(parsed, profile)
        legacy = profile.to_dict()
        legacy["schema"] = (
            "urn:literate-ai:schema:v1:local-regenerative-qualification-profile"
        )
        with self.assertRaisesRegex(Exception, "schema or exact fields"):
            LocalQualificationProfile.from_dict(legacy)
        invalid = profile.to_dict()
        invalid["evidence"] = {"passed": True}
        with self.assertRaisesRegex(Exception, "exact fields"):
            LocalQualificationProfile.from_dict(invalid)


if __name__ == "__main__":
    unittest.main()
