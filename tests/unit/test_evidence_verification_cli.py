"""Public verification checks complete signed custody without writing a receipt."""

import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore
from literate_ai.cli.dispatch import main
from literate_ai.contracts.identity import canonical_identity
from literate_ai.schema_catalog import schema_path, verify_schema_catalog
from literate_ai.security.evidence import DsseEnvelope
from literate_ai.security.evidence.plan import EvidenceVerificationPlan
from tests.unit.test_evidence_graph import _Graph
from tests.unit.test_evidence_retention import _claims


class EvidenceVerificationCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.graph = _Graph()
        self.authority = canonical_identity("independent project authority")
        self.plan = EvidenceVerificationPlan(
            self.authority,
            self.graph.refs["matrix"],
            tuple(
                sorted(
                    self.graph.requirements.values(), key=lambda r: r.envelope.identity
                )
            ),
        )
        self.plan_path = self.write("plan.json", self.plan.to_dict())
        self.policy_path = self.write("policy.json", self.graph.policy.to_dict())
        self.revocations_path = self.write(
            "revocations.json", self.graph.snapshot.revocations.to_dict()
        )
        store = FileSystemEvidenceStore(self.root / "store", writable=True)
        refs = {
            r.identity: r
            for item in self.plan.requirements
            for r in (
                item.envelope,
                *(c.blob for c in item.children),
                *(b for _, b in item.artifacts),
            )
        }
        for identity, content in self.graph.contents.items():
            self.assertEqual(
                store.put_bytes(content, media_type=refs[identity].media_type),
                refs[identity],
            )
        self.arguments = [
            "--json",
            "project",
            "test-receipt",
            "verify-evidence",
            str(self.plan_path),
            "--project",
            str(self.root),
            "--policy",
            str(self.policy_path),
            "--revocations",
            str(self.revocations_path),
            "--store",
            "local=" + str(self.root / "store"),
        ]
        for i, claim in enumerate(_claims(self.graph)):
            path = self.root / f"retention-{i}.json"
            path.write_bytes(claim.envelope)
            self.arguments += ["--retention", str(path)]

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value))
        return path

    def run_cli(self, *, arguments=None, authority=None, clock=None):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch(
                "literate_ai.cli.evidence_verification.validated_project_authority_identity",
                side_effect=authority or [self.authority, self.authority],
            ) as project_check,
            patch(
                "literate_ai.cli.evidence_verification.time.time",
                side_effect=clock or [210, 210],
            ),
            patch("literate_ai.cli.dispatch.maybe_host_self_update") as update,
            patch("literate_ai.cli.dispatch.ensure_user_mcp_catalog") as catalog,
            patch("literate_ai.cli.dispatch.journal_mutagenic_event") as journal,
        ):
            status = main(arguments or self.arguments, stdout=output, stderr=errors)
            update.assert_not_called()
            catalog.assert_not_called()
            journal.assert_not_called()
            for call in project_check.call_args_list:
                self.assertFalse(call.kwargs["synchronize_source_intelligence"])
        return status, json.loads(
            output.getvalue() if status == 0 else errors.getvalue()
        )

    def test_public_command_authenticates_complete_graph_and_writes_nothing(self):
        before = {
            p.relative_to(self.root): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        status, result = self.run_cli()
        self.assertEqual(status, 0, result)
        checked = result["result"]
        self.assertTrue(checked["authenticated"])
        self.assertTrue(checked["retained"])
        self.assertFalse(checked["receipt_updated"])
        self.assertFalse(checked["execution_authorized"])
        self.assertEqual(checked["run_count"], 4)
        self.assertEqual(checked["object_count"], len(self.graph.contents))
        self.assertEqual(checked["plan_identity"], self.plan.identity.uri)
        self.assertEqual(
            before,
            {
                p.relative_to(self.root): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            },
        )

    def test_wrong_or_changed_project_authority_refuses(self):
        other = canonical_identity("other")
        for authority in ([other], [self.authority, other]):
            with self.subTest(authority=authority):
                self.assertNotEqual(self.run_cli(authority=authority)[0], 0)

    def test_missing_blob_and_missing_retention_refuse(self):
        self.assertNotEqual(self.run_cli(arguments=self.arguments[:-2])[0], 0)
        blob = next((self.root / "store").rglob("*.blob"), None)
        # CAS filename suffixes are adapter-owned; select by known content instead.
        for path in (self.root / "store").rglob("*"):
            if path.is_file() and path.read_bytes() == b"sources":
                blob = path
                break
        self.assertIsNotNone(blob)
        blob.unlink()
        self.assertNotEqual(self.run_cli()[0], 0)

    def test_revocation_reload_after_reads_refuses_newly_revoked_signer(self):
        original = FileSystemEvidenceStore.get_bytes
        revoked = replace(
            self.graph.snapshot.revocations,
            key_identities=(canonical_identity("irrelevant"),),
        )
        from literate_ai.contracts.identity import ContentIdentity

        revoked = replace(
            revoked,
            key_identities=(ContentIdentity.parse_uri(self.graph.signer.key_identity),),
        )

        def read(store, reference):
            content = original(store, reference)
            self.revocations_path.write_text(json.dumps(revoked.to_dict()))
            return content

        with patch.object(FileSystemEvidenceStore, "get_bytes", read):
            self.assertNotEqual(self.run_cli()[0], 0)

    def test_changed_operator_policy_after_reads_refuses(self):
        original = FileSystemEvidenceStore.get_bytes

        def read(store, reference):
            content = original(store, reference)
            self.policy_path.write_text(
                json.dumps(
                    replace(self.graph.policy, minimum_retention_seconds=91).to_dict()
                )
            )
            return content

        with patch.object(FileSystemEvidenceStore, "get_bytes", read):
            self.assertNotEqual(self.run_cli()[0], 0)

    def test_ambiguous_json_and_duplicate_store_names_refuse(self):
        self.assertNotEqual(
            self.run_cli(arguments=self.arguments + ["--store", "local=other"])[0], 0
        )
        self.plan_path.write_bytes(b'{"schema": "a", "schema":"b"}')
        self.assertNotEqual(self.run_cli()[0], 0)

    def test_unsigned_retention_cannot_supply_routing_authority(self):
        path = self.root / "retention-0.json"
        envelope = DsseEnvelope.from_bytes(path.read_bytes())
        path.write_bytes(replace(envelope, signatures=()).to_bytes())
        self.assertNotEqual(self.run_cli()[0], 0)

    def test_plan_and_result_validate_against_public_schema(self):
        documents = [
            json.loads(schema_path(name, catalog_version="v2").read_bytes())
            for name in (
                "evidence-verification.schema.json",
                "evidence-trust.schema.json",
            )
        ]
        registry = Registry().with_resources(
            (d["$id"], Resource.from_contents(d)) for d in documents
        )
        validator = Draft202012Validator(documents[0], registry=registry)
        validator.validate(self.plan.to_dict())
        status, result = self.run_cli()
        self.assertEqual(status, 0)
        validator.validate(result["result"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes require POSIX")
    def test_nonregular_input_refuses_without_waiting_for_a_writer(self):
        self.plan_path.unlink()
        os.mkfifo(self.plan_path)
        self.assertNotEqual(self.run_cli()[0], 0)

    def test_command_verifies_against_real_initialized_project_authority(self):
        from literate_ai.adapters.project_validation import (
            validated_project_authority_identity,
        )
        from tests.unit.test_project_test_receipt_cli import invoke

        project = self.root / "project"
        status, result = invoke(
            "init",
            str(project),
            "--flavor",
            "python",
            "--flavor",
            "macos",
            "--flavor",
            "bazel",
        )
        self.assertEqual(status, 0, result)
        authority = validated_project_authority_identity(
            project, synchronize_source_intelligence=False
        )
        self.plan_path.write_text(
            json.dumps(replace(self.plan, project_authority=authority).to_dict())
        )
        arguments = list(self.arguments)
        arguments[arguments.index("--project") + 1] = str(project)
        before = {
            p.relative_to(project): p.read_bytes()
            for p in project.rglob("*")
            if p.is_file()
        }
        output, errors = io.StringIO(), io.StringIO()
        with patch("literate_ai.cli.evidence_verification.time.time", return_value=210):
            status = main(arguments, stdout=output, stderr=errors)
        self.assertEqual(status, 0, errors.getvalue())
        self.assertEqual(
            json.loads(output.getvalue())["result"]["project_authority"], authority.uri
        )
        self.assertEqual(
            before,
            {
                p.relative_to(project): p.read_bytes()
                for p in project.rglob("*")
                if p.is_file()
            },
        )

    def test_plan_is_closed_ordered_and_catalogued(self):
        verify_schema_catalog("v2")
        self.assertEqual(
            EvidenceVerificationPlan.from_dict(self.plan.to_dict()), self.plan
        )
        for change in (
            {**self.plan.to_dict(), "extra": True},
            {
                **self.plan.to_dict(),
                "requirements": list(reversed(self.plan.to_dict()["requirements"])),
            },
        ):
            with self.assertRaises(ValueError):
                EvidenceVerificationPlan.from_dict(change)

    def test_read_only_command_refuses_debug_files_and_discovery(self):
        for option in (["--debug", str(self.root / "debug.json")], ["--discover-mcps"]):
            self.assertNotEqual(self.run_cli(arguments=self.arguments + option)[0], 0)
        self.assertFalse((self.root / "debug.json").exists())
