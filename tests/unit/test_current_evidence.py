"""A compact map references exact validated receipt custody without promoting it."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore
from literate_ai.adapters.project_validation import validated_project_authority_identity
from literate_ai.application.current_evidence import (
    prepare_current_evidence,
    retain_prepared_current_evidence,
)
from literate_ai.contracts import canonical_json_bytes
from literate_ai.projects import load_project
from literate_ai.security.evidence.plan import EvidenceVerificationPlan
from tests.support.fixtures_test_evidence_graph import _Graph
from tests.support.fixtures_test_evidence_retention import _claims
from tests.support.fixtures_test_project_test_receipt_cli import (
    configure_receipt_policy,
    finalize_candidate,
    invoke,
    make_candidate,
)


class CurrentEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve() / "project"
        status, result = invoke(
            "init",
            str(cls.root),
            "--flavor",
            "python",
            "--flavor",
            "macos",
            "--flavor",
            "bazel",
        )
        if status:
            raise AssertionError(result)
        configure_receipt_policy(cls.root)
        cls.finalized = finalize_candidate(make_candidate(cls.root))
        cls.authority = validated_project_authority_identity(
            cls.root, synchronize_source_intelligence=False
        )

    def setUp(self):
        self.project = load_project(self.root)
        self.graph = _Graph()
        self.bind_receipt(canonical_json_bytes(self.finalized) + b"\n")

    def bind_receipt(self, content):
        ref = self.graph.blob(content, "application/json")
        self.graph.resign("matrix", replace(self.graph.records["matrix"], subject=ref))
        req = self.graph.requirements["matrix"]
        self.graph.requirements["matrix"] = replace(
            req,
            expectation=replace(req.expectation, subject=ref),
            artifacts=(("subject", ref),),
        )
        self.plan = EvidenceVerificationPlan(
            self.authority,
            self.graph.refs["matrix"],
            tuple(
                sorted(
                    self.graph.requirements.values(), key=lambda r: r.envelope.identity
                )
            ),
        )

    def prepare(self, **kwargs):
        arguments = dict(
            plan=self.plan,
            policy=self.graph.policy,
            resolver=self.graph.resolver,
            retention=_claims(self.graph),
            current_state=self.graph.state,
            project_authority=lambda: self.authority,
        )
        arguments.update(kwargs)
        return prepare_current_evidence(self.project, **arguments)

    def publication(self):
        from literate_ai.adapters.current_evidence import publish_current_evidence

        prepared = self.prepare()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = FileSystemEvidenceStore(Path(temporary.name).resolve(), writable=True)
        retain_prepared_current_evidence(prepared, store)
        target = self.root / self.project.definition.test_receipt
        before = target.read_bytes() if target.exists() else None

        def restore():
            target.unlink(missing_ok=True)
            if before is not None:
                target.write_bytes(before)

        self.addCleanup(restore)
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy = canonical_json_bytes(self.finalized) + b"\n"
        target.write_bytes(legacy)

        def publish(**overrides):
            arguments = dict(
                plan=self.plan,
                policy=self.graph.policy,
                resolver=self.graph.resolver,
                bundle_store=store,
                current_state=self.graph.state,
                project_authority=lambda: validated_project_authority_identity(
                    self.root, synchronize_source_intelligence=False
                ),
                current_policy=lambda: self.graph.policy,
            )
            arguments.update(overrides)
            return publish_current_evidence(self.project, prepared.current, **arguments)

        return prepared, target, legacy, publish

    def publication_cli(self):
        prepared, target, legacy, _ = self.publication()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name).resolve()
        source = FileSystemEvidenceStore(directory / "source", writable=True)
        retain_prepared_current_evidence(prepared, source)
        for name, value in (
            ("plan", self.plan),
            ("policy", self.graph.policy),
            ("revocations", self.graph.snapshot.revocations),
        ):
            (directory / f"{name}.json").write_bytes(
                canonical_json_bytes(value.to_dict()) + b"\n"
            )
        args = [
            "project",
            "test-receipt",
            "publish-evidence",
            str(directory / "plan.json"),
            "--project",
            str(self.root),
            "--policy",
            str(directory / "policy.json"),
            "--revocations",
            str(directory / "revocations.json"),
            "--store",
            "local=" + str(directory / "source"),
            "--bundle-store",
            str(directory / "bundle"),
        ]
        for index, claim in enumerate(_claims(self.graph)):
            path = directory / f"retention-{index}.json"
            path.write_bytes(claim.envelope)
            args.extend(("--retention", str(path)))
        return args, directory, prepared, target, legacy

    def test_public_cli_publishes_complete_bundle_then_reverifies_current_receipt(self):
        args, directory, prepared, target, _ = self.publication_cli()
        with (
            patch("literate_ai.cli.evidence_publication.time.time", return_value=210),
            patch("literate_ai.cli.dispatch.maybe_host_self_update") as update,
            patch("literate_ai.cli.dispatch.ensure_user_mcp_catalog") as catalog,
            patch("literate_ai.cli.dispatch.journal_mutagenic_event") as journal,
        ):
            status, result = invoke(*args)
            update.assert_not_called()
            catalog.assert_not_called()
            journal.assert_not_called()
        self.assertEqual(status, 0, result)
        self.assertTrue(result["result"]["updated"])
        self.assertEqual(
            result["result"]["current_map_identity"], prepared.current.identity.uri
        )
        store = FileSystemEvidenceStore(directory / "bundle")
        for reference, content in prepared.objects:
            self.assertEqual(store.get_bytes(reference), content)
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        from literate_ai.schema_catalog import schema_path, verify_schema_catalog

        verify_schema_catalog("v2")
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
        Draft202012Validator(documents[0], registry=registry).validate(result["result"])
        args[2] = "verify-evidence"
        del args[args.index("--retention") :]
        args.extend(("--current-map", str(target)))
        with patch("literate_ai.cli.evidence_verification.time.time", return_value=210):
            status, result = invoke(*args)
        self.assertEqual(status, 0, result)
        self.assertTrue(result["result"]["authenticated"])
        self.assertFalse(result["result"]["receipt_updated"])
        args[2] = "require-current-evidence"
        del args[-2:]
        before = {
            p: p.read_bytes()
            for root in (self.root, directory)
            for p in root.rglob("*")
            if p.is_file()
        }
        with (
            patch("literate_ai.cli.evidence_verification.time.time", return_value=210),
            patch("literate_ai.cli.dispatch.maybe_host_self_update") as update,
            patch("literate_ai.cli.dispatch.ensure_user_mcp_catalog") as catalog,
            patch("literate_ai.cli.dispatch.journal_mutagenic_event") as journal,
        ):
            status, result = invoke(*args)
            update.assert_not_called()
            catalog.assert_not_called()
            journal.assert_not_called()
        self.assertEqual(status, 0, result)
        self.assertEqual(
            result["result"]["current_receipt_path"],
            self.project.definition.test_receipt,
        )
        self.assertTrue(result["result"]["authenticated"])
        Draft202012Validator(documents[0], registry=registry).validate(result["result"])
        self.assertEqual(
            before,
            {
                p: p.read_bytes()
                for root in (self.root, directory)
                for p in root.rglob("*")
                if p.is_file()
            },
        )
        from literate_ai.cli.evidence_verification import verify_evidence_from_args

        definition_path = self.root / "literate.project.json"
        definition_bytes = definition_path.read_bytes()
        for change in ("path", "policy"):

            def verify_then_change(selected, change=change):
                checked = verify_evidence_from_args(selected)
                definition = json.loads(definition_bytes)
                if change == "path":
                    definition["test_receipt"] = "verification/other.json"
                else:
                    definition["test_receipt_policy"]["suite_version"] = "9.9.9"
                definition_path.write_bytes(canonical_json_bytes(definition) + b"\n")
                return checked

            try:
                with (
                    patch(
                        "literate_ai.cli.evidence_verification.time.time",
                        return_value=210,
                    ),
                    patch(
                        "literate_ai.cli.evidence_verification.verify_evidence_from_args",
                        verify_then_change,
                    ),
                ):
                    self.assertNotEqual(invoke(*args)[0], 0)
            finally:
                definition_path.write_bytes(definition_bytes)
        for path in (directory / "source").rglob("*"):
            if path.is_file() and path.read_bytes() == b"sources":
                path.unlink()
        with patch("literate_ai.cli.evidence_verification.time.time", return_value=210):
            self.assertNotEqual(invoke(*args)[0], 0)

    def test_current_evidence_gate_refuses_unsigned_receipt_without_fallback(self):
        from literate_ai.test_receipts import inspect_project_test_receipt

        args, directory, prepared, target, legacy = self.publication_cli()
        args[2] = "require-current-evidence"
        del args[args.index("--retention") :]
        inspection = inspect_project_test_receipt(
            self.project, project_revision_identity=self.authority
        )
        self.assertEqual(inspection["state"], "current")
        self.assertFalse(inspection["authenticated"])
        self.assertNotEqual(invoke(*args)[0], 0)
        self.assertEqual(target.read_bytes(), legacy)
        # An unrelated valid map cannot override the configured receipt path.
        alternate = directory / "other-current.json"
        alternate.write_bytes(canonical_json_bytes(prepared.current.to_dict()) + b"\n")
        self.assertNotEqual(invoke(*args, "--current-map", str(alternate))[0], 0)
        self.assertFalse((directory / "bundle").exists())

    def test_real_cli_reverifies_map_without_writes_and_detects_pointer_change(self):
        prepared = self.prepare()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            store = FileSystemEvidenceStore(directory / "bundle", writable=True)
            retain_prepared_current_evidence(prepared, store)
            for name, value in (
                ("plan", self.plan),
                ("policy", self.graph.policy),
                ("revocations", self.graph.snapshot.revocations),
                ("current", prepared.current),
            ):
                (directory / f"{name}.json").write_bytes(
                    canonical_json_bytes(value.to_dict()) + b"\n"
                )
            arguments = (
                "project",
                "test-receipt",
                "verify-evidence",
                str(directory / "plan.json"),
                "--project",
                str(self.root),
                "--policy",
                str(directory / "policy.json"),
                "--revocations",
                str(directory / "revocations.json"),
                "--store",
                "local=" + str(directory / "bundle"),
                "--current-map",
                str(directory / "current.json"),
                "--bundle-store",
                str(directory / "bundle"),
            )

            def snapshot():
                return {
                    p: p.read_bytes()
                    for root in (directory, self.root)
                    for p in root.rglob("*")
                    if p.is_file()
                }

            before = snapshot()
            with patch(
                "literate_ai.cli.evidence_verification.time.time", return_value=210
            ):
                status, result = invoke(*arguments)
            self.assertEqual(status, 0, result)
            self.assertEqual(
                result["result"]["current_map_identity"], prepared.current.identity.uri
            )
            self.assertFalse(result["result"]["receipt_updated"])
            self.assertEqual(snapshot(), before)
            from jsonschema import Draft202012Validator
            from referencing import Registry, Resource

            from literate_ai.schema_catalog import schema_path

            schema = json.loads(
                schema_path(
                    "evidence-verification.schema.json", catalog_version="v2"
                ).read_bytes()
            )
            trust = json.loads(
                schema_path(
                    "evidence-trust.schema.json", catalog_version="v2"
                ).read_bytes()
            )
            registry = Registry().with_resources(
                (d["$id"], Resource.from_contents(d)) for d in (schema, trust)
            )
            Draft202012Validator(schema, registry=registry).validate(result["result"])
            self.assertNotEqual(invoke(*arguments[:-2])[0], 0)
            self.assertNotEqual(invoke(*arguments, "--retention", "unused")[0], 0)
            pointer = directory / "current.json"
            pointer.write_text(json.dumps(prepared.current.to_dict(), indent=2))
            self.assertNotEqual(invoke(*arguments)[0], 0)
            pointer.write_bytes(
                canonical_json_bytes(prepared.current.to_dict()) + b"\n"
            )
            original = FileSystemEvidenceStore.get_bytes

            def change_pointer(store, reference):
                content = original(store, reference)
                (directory / "current.json").write_bytes(b"{}\n")
                return content

            with (
                patch(
                    "literate_ai.cli.evidence_verification.time.time", return_value=210
                ),
                patch.object(FileSystemEvidenceStore, "get_bytes", change_pointer),
            ):
                self.assertNotEqual(invoke(*arguments)[0], 0)
