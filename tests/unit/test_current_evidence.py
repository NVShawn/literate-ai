"""A compact map references exact validated receipt custody without promoting it."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore
from literate_ai.adapters.project_validation import validated_project_authority_identity
from literate_ai.application.current_evidence import (
    prepare_current_evidence,
    retain_prepared_current_evidence,
    verify_current_evidence,
)
from literate_ai.contracts import canonical_json_bytes
from literate_ai.projects import load_project
from literate_ai.security.evidence.current import CurrentEvidenceMap
from literate_ai.security.evidence.graph import EvidenceGraphLimits
from literate_ai.security.evidence.plan import EvidenceVerificationPlan
from literate_ai.security.evidence.storage import EvidenceReadLimits
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

    def test_publication_installs_map_but_plain_currentness_requires_authentication(
        self,
    ):
        from literate_ai.test_receipts import (
            inspect_project_test_receipt,
            require_current_project_test_receipt,
        )

        prepared, target, _, publish = self.publication()
        result = publish()
        self.assertTrue(result["updated"])
        self.assertTrue(result["authenticated"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(
            target.read_bytes(),
            canonical_json_bytes(prepared.current.to_dict()) + b"\n",
        )
        inspection = inspect_project_test_receipt(
            self.project, project_revision_identity=self.authority
        )
        self.assertEqual(inspection["state"], "authentication-required")
        self.assertFalse(inspection["authenticated"])
        with self.assertRaisesRegex(ValueError, "fresh verification"):
            require_current_project_test_receipt(
                self.project, project_revision_identity=self.authority
            )
        self.graph.reads.clear()
        self.assertFalse(publish()["updated"])
        self.assertTrue(self.graph.reads)
        self.graph.state.return_value = replace(self.graph.snapshot, now=251)
        with self.assertRaises(ValueError):
            publish()
        self.assertEqual(
            target.read_bytes(),
            canonical_json_bytes(prepared.current.to_dict()) + b"\n",
        )

    def test_publication_preserves_legacy_on_expiry_policy_change_or_replace_failure(
        self,
    ):
        _, target, legacy, publish = self.publication()
        with self.assertRaises(ValueError):
            publish(
                current_state=Mock(return_value=replace(self.graph.snapshot, now=251))
            )
        self.assertEqual(target.read_bytes(), legacy)
        with self.assertRaisesRegex(ValueError, "authority-changed"):
            publish(
                current_policy=Mock(
                    side_effect=[
                        self.graph.policy,
                        replace(self.graph.policy, minimum_retention_seconds=91),
                    ]
                )
            )
        self.assertEqual(target.read_bytes(), legacy)
        with patch(
            "literate_ai.adapters.current_evidence.os.replace",
            side_effect=OSError("disk"),
        ):
            with self.assertRaisesRegex(OSError, "disk"):
                publish()
        self.assertEqual(target.read_bytes(), legacy)
        self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))

    def test_publication_refuses_pointer_changed_during_verification(self):
        _, target, _, publish = self.publication()
        other = (
            canonical_json_bytes(
                finalize_candidate(make_candidate(self.root, project_id="peer-project"))
            )
            + b"\n"
        )

        def state():
            target.write_bytes(other)
            return self.graph.snapshot

        with self.assertRaisesRegex(ValueError, "pointer-changed"):
            publish(current_state=state)
        self.assertEqual(target.read_bytes(), other)
        self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))

    def test_publication_obeys_existing_project_lifecycle_lock(self):
        from literate_ai.adapters.lifecycle_lock import (
            ProjectLifecycleLockError,
            project_lifecycle_lock,
        )

        _, target, legacy, publish = self.publication()
        policy = Mock(return_value=self.graph.policy)
        with project_lifecycle_lock(self.root, operation="peer-rebuild"):
            with self.assertRaises(ProjectLifecycleLockError):
                publish(current_policy=policy)
        policy.assert_not_called()
        self.assertEqual(target.read_bytes(), legacy)

    def test_publication_refuses_modified_staging_bytes(self):
        _, target, legacy, publish = self.publication()

        def state():
            for path in target.parent.glob(f".{target.name}.*.tmp"):
                path.write_bytes(b"substituted")
            return self.graph.snapshot

        with self.assertRaisesRegex(ValueError, "staging-changed"):
            publish(current_state=state)
        self.assertEqual(target.read_bytes(), legacy)
        self.assertFalse(list(target.parent.glob(f".{target.name}.*.tmp")))

    def reverify(self, prepared, **kwargs):
        contents = dict(prepared.objects)
        arguments = dict(
            plan=self.plan,
            policy=self.graph.policy,
            resolver=self.graph.resolver,
            bundle_store=Mock(get_bytes=Mock(side_effect=contents.__getitem__)),
            current_state=self.graph.state,
            project_authority=lambda: self.authority,
        )
        arguments.update(kwargs)
        return verify_current_evidence(self.project, prepared.current, **arguments)

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

    def test_release_binding_uses_pinned_inputs_and_fresh_original_store_reads(self):
        from literate_ai.adapters.release_evidence import (
            verify_release_current_evidence,
        )

        prepared, target, legacy, publish = self.publication()
        build = self.root / "_build"
        build.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=build)
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name).resolve()
        store = FileSystemEvidenceStore(directory / "store", writable=True)
        retain_prepared_current_evidence(prepared, store)
        for name, value in (
            ("plan", self.plan),
            ("policy", self.graph.policy),
            ("revocations", self.graph.snapshot.revocations),
        ):
            (directory / f"{name}.json").write_bytes(
                canonical_json_bytes(value.to_dict()) + b"\n"
            )
        relative = directory.relative_to(self.root).as_posix()
        binding = {
            "plan_path": relative + "/plan.json",
            "plan_identity": self.plan.identity.uri,
            "policy_path": relative + "/policy.json",
            "policy_identity": self.graph.policy.identity.uri,
            "revocations_path": relative + "/revocations.json",
            "bundle_store": relative + "/store",
            "stores": [
                {"id": "local", "kind": "filesystem", "location": relative + "/store"}
            ],
        }
        with patch("literate_ai.adapters.release_evidence.time.time", return_value=210):
            with self.assertRaisesRegex(ValueError, "unauthenticated"):
                verify_release_current_evidence(self.root, binding)
            self.assertEqual(target.read_bytes(), legacy)
            publish()
            self.assertEqual(
                verify_release_current_evidence(self.root, binding),
                prepared.current.identity.uri,
            )
            for wrong in (
                {**binding, "plan_identity": "sha256:" + "0" * 64},
                {**binding, "policy_identity": "sha256:" + "0" * 64},
                {**binding, "plan_path": "../plan.json"},
                {**binding, "stores": binding["stores"] * 2},
            ):
                with self.assertRaises(ValueError):
                    verify_release_current_evidence(self.root, wrong)
            (directory / "plan.json").write_bytes(b"{}")
            with self.assertRaises(ValueError):
                verify_release_current_evidence(self.root, binding)
            (directory / "plan.json").write_bytes(
                canonical_json_bytes(self.plan.to_dict()) + b"\n"
            )
            for path in (directory / "store").rglob("*"):
                if path.is_file() and path.read_bytes() == b"sources":
                    path.unlink()
            with self.assertRaises(ValueError):
                verify_release_current_evidence(self.root, binding)

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

    def test_public_cli_invalid_proof_refuses_before_bundle_publication(self):
        args, directory, _, target, legacy = self.publication_cli()
        (directory / "retention-0.json").write_bytes(b"{}")
        with patch("literate_ai.cli.evidence_publication.time.time", return_value=210):
            self.assertNotEqual(invoke(*args)[0], 0)
        self.assertEqual(target.read_bytes(), legacy)
        self.assertFalse((directory / "bundle").exists())

    def test_public_cli_refreshes_authority_revocations_and_availability_after_storage(
        self,
    ):
        from literate_ai.contracts.identity import ContentIdentity

        original = FileSystemEvidenceStore.put_bytes
        for mutation in ("plan", "revocations", "source"):
            with self.subTest(mutation=mutation):
                args, directory, _, target, legacy = self.publication_cli()

                def put(
                    store,
                    content,
                    *,
                    media_type,
                    mutation=mutation,
                    directory=directory,
                ):
                    reference = original(store, content, media_type=media_type)
                    if mutation == "plan":
                        (directory / "plan.json").write_bytes(b"{}")
                    elif mutation == "revocations":
                        revoked = replace(
                            self.graph.snapshot.revocations,
                            key_identities=(
                                ContentIdentity.parse_uri(
                                    self.graph.signer.key_identity
                                ),
                            ),
                        )
                        (directory / "revocations.json").write_bytes(
                            canonical_json_bytes(revoked.to_dict()) + b"\n"
                        )
                    else:
                        for path in (directory / "source").rglob("*"):
                            if path.is_file() and path.read_bytes() == b"sources":
                                path.unlink()
                    return reference

                with (
                    patch(
                        "literate_ai.cli.evidence_publication.time.time",
                        return_value=210,
                    ),
                    patch.object(FileSystemEvidenceStore, "put_bytes", put),
                ):
                    self.assertNotEqual(invoke(*args)[0], 0)
                self.assertEqual(target.read_bytes(), legacy)

    def test_public_cli_rejects_unrelated_debug_and_discovery_writes(self):
        args, directory, _, target, legacy = self.publication_cli()
        for extra in (("--debug", str(directory / "debug.log")), ("--discover-mcps",)):
            self.assertNotEqual(invoke(*args, *extra)[0], 0)
        self.assertEqual(target.read_bytes(), legacy)
        self.assertFalse((directory / "bundle").exists())
        self.assertFalse((directory / "debug.log").exists())

    def test_stored_map_refreshes_complete_signed_custody(self):
        prepared = self.prepare()
        self.graph.reads.clear()
        with tempfile.TemporaryDirectory() as temp:
            store = FileSystemEvidenceStore(Path(temp).resolve(), writable=True)
            retain_prepared_current_evidence(prepared, store)
            checked = self.reverify(prepared, bundle_store=store)
        self.assertEqual(checked.current, prepared.current)
        self.assertEqual(checked.objects, prepared.objects)
        self.assertTrue(self.graph.reads)
        self.assertEqual(checked.verification.graph.final_state, self.graph.snapshot)

    def test_stored_map_authority_and_budget_refuse_before_bundle_reads(self):
        from literate_ai.contracts import canonical_identity

        prepared = self.prepare()
        total = sum(ref.size for ref, _ in prepared.objects)
        limits = EvidenceGraphLimits(
            reads=EvidenceReadLimits(
                maximum_blob_bytes=max(ref.size for ref, _ in prepared.objects),
                maximum_total_bytes=total - 1,
                maximum_objects=len(prepared.objects),
            )
        )
        for overrides in (
            {"policy": replace(self.graph.policy, minimum_retention_seconds=91)},
            {"plan": replace(self.plan, project_authority=canonical_identity("other"))},
            {"project_authority": lambda: canonical_identity("changed")},
            {"limits": limits},
        ):
            with self.subTest(overrides=overrides):
                store = Mock()
                with self.assertRaises(ValueError):
                    self.reverify(prepared, bundle_store=store, **overrides)
                store.get_bytes.assert_not_called()

    def test_bundle_cannot_replace_missing_signed_store_availability(self):
        prepared = self.prepare()
        del self.graph.contents[prepared.current.receipt.identity]
        with self.assertRaises(ValueError):
            self.reverify(prepared)

    def test_stored_map_rejects_corrupt_plan_or_detached_proof(self):
        prepared = self.prepare()
        for ref in (prepared.current.plan, prepared.current.retention_roots[0]):
            contents = dict(prepared.objects)
            contents[ref] = b"corrupt"
            with self.assertRaises(ValueError):
                self.reverify(
                    prepared,
                    bundle_store=Mock(get_bytes=Mock(side_effect=contents.__getitem__)),
                )

    def test_stored_map_requires_fresh_revocations_and_project_authority(self):
        from literate_ai.contracts import canonical_identity
        from literate_ai.contracts.identity import ContentIdentity

        prepared = self.prepare()
        revoked = replace(
            self.graph.snapshot.revocations,
            key_identities=(ContentIdentity.parse_uri(self.graph.signer.key_identity),),
        )
        for state in (
            replace(self.graph.snapshot, now=251),
            replace(self.graph.snapshot, revocations=revoked),
        ):
            with self.assertRaises(ValueError):
                self.reverify(prepared, current_state=Mock(return_value=state))
        with self.assertRaisesRegex(ValueError, "authority-changed"):
            self.reverify(
                prepared,
                project_authority=Mock(
                    side_effect=[
                        self.authority,
                        self.authority,
                        canonical_identity("new"),
                    ]
                ),
            )

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

    def test_prepares_complete_canonical_bundle_without_current_receipt_write(self):
        before = {
            p.relative_to(self.root): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        prepared = self.prepare()
        self.assertEqual(
            CurrentEvidenceMap.from_dict(prepared.current.to_dict()), prepared.current
        )
        objects = dict(prepared.objects)
        self.assertEqual(
            objects[prepared.current.receipt],
            canonical_json_bytes(self.finalized) + b"\n",
        )
        self.assertEqual(
            objects[prepared.current.plan],
            canonical_json_bytes(self.plan.to_dict()) + b"\n",
        )
        self.assertEqual(prepared.current.matrix, self.plan.root)
        self.assertEqual(prepared.current.identity, self.prepare().current.identity)
        self.assertLess(len(canonical_json_bytes(prepared.current.to_dict())), 4096)
        self.assertEqual(
            before,
            {
                p.relative_to(self.root): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            },
        )
        for claim in _claims(self.graph):
            self.assertEqual(objects[claim.reference], claim.envelope)

    def test_retains_complete_immutable_bundle_with_readback(self):
        prepared = self.prepare()
        with tempfile.TemporaryDirectory() as temp:
            store = FileSystemEvidenceStore(Path(temp).resolve(), writable=True)
            self.assertEqual(
                retain_prepared_current_evidence(prepared, store), prepared.current
            )
            for ref, content in prepared.objects:
                self.assertEqual(store.get_bytes(ref), content)
            self.assertFalse((Path(temp) / "current.json").exists())

    def test_correct_signatures_do_not_admit_noncanonical_or_wrong_project_receipt(
        self,
    ):
        malformed = json.dumps(self.finalized, indent=2).encode()
        self.bind_receipt(malformed)
        with self.assertRaisesRegex(ValueError, "receipt-not-canonical"):
            self.prepare()
        other = finalize_candidate(
            make_candidate(self.root, project_id="another-project")
        )
        self.bind_receipt(canonical_json_bytes(other) + b"\n")
        with self.assertRaisesRegex(ValueError, "another project"):
            self.prepare()

    def test_preparation_rejects_changed_authority_and_partial_graph(self):
        from literate_ai.contracts import canonical_identity

        with self.assertRaisesRegex(ValueError, "authority-changed"):
            self.prepare(
                project_authority=Mock(
                    side_effect=[self.authority, canonical_identity("changed")]
                )
            )
        with self.assertRaises(ValueError):
            self.prepare(retention=_claims(self.graph)[:-1])

    def test_plan_and_detached_roots_share_bundle_budget(self):
        prepared = self.prepare()
        total = sum(ref.size for ref, _ in prepared.objects)
        limits = EvidenceGraphLimits(
            reads=EvidenceReadLimits(
                maximum_blob_bytes=max(ref.size for ref, _ in prepared.objects),
                maximum_total_bytes=total,
                maximum_objects=len(prepared.objects),
            )
        )
        self.assertEqual(self.prepare(limits=limits).current, prepared.current)
        with self.assertRaises(ValueError):
            self.prepare(
                limits=replace(
                    limits, reads=replace(limits.reads, maximum_total_bytes=total - 1)
                )
            )

    def test_invalid_bundle_refuses_before_any_publication(self):
        prepared = self.prepare()
        for value in (
            replace(prepared, objects=prepared.objects[:-1]),
            replace(prepared, objects=(prepared.objects[0], *prepared.objects)),
            replace(
                prepared,
                objects=((prepared.objects[0][0], b"changed"), *prepared.objects[1:]),
            ),
        ):
            store = Mock()
            with self.assertRaises(ValueError):
                retain_prepared_current_evidence(value, store)
            store.put_bytes.assert_not_called()

    def test_wrong_publication_identity_or_corrupt_readback_never_returns_map(self):
        prepared = self.prepare()
        store = Mock()
        store.put_bytes.return_value = prepared.current.receipt
        with self.assertRaises(ValueError):
            retain_prepared_current_evidence(prepared, store)
        store = Mock()
        store.put_bytes.side_effect = [ref for ref, _ in prepared.objects]
        store.get_bytes.return_value = b"corrupt"
        with self.assertRaises(ValueError):
            retain_prepared_current_evidence(prepared, store)

    def test_map_validates_against_public_language_neutral_schema(self):
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
        Draft202012Validator(documents[0], registry=registry).validate(
            self.prepare().current.to_dict()
        )

    def test_map_is_closed_and_has_no_log_or_unsigned_success_fields(self):
        current = self.prepare().current
        for field in ("authenticated", "history", "journal", "token"):
            with self.assertRaises(ValueError):
                CurrentEvidenceMap.from_dict({**current.to_dict(), field: True})
        with self.assertRaises(ValueError):
            replace(current, retention_roots=tuple(reversed(current.retention_roots)))
