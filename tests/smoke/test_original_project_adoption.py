"""Original authority preserves real source and never grants conversion execution."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.conversion_authority import (
    ConversionAuthorityError,
    FilesystemConversionAuthorityStore,
)
from literate_ai.adapters.original_project_adoption import (
    OriginalProjectAdoptionError,
    apply_original_project_adoption,
    plan_original_project_adoption,
)
from literate_ai.contracts import canonical_json_bytes
from literate_ai.contracts.operator_adoption import ConversionAuthorityStage
from tests.smoke.test_operator_adoption import invoke
from tests.smoke.test_project_reparenting import initialize_root
from tests.support.fixtures_test_repository_orchestration import git as fixture_git


class OriginalProjectAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "project"
        initialize_root(self.root)
        self.source = self.root / "usr.bin" / "sample" / "sample.c"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"int main(void) { return 0; }\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.commit()

    def git(self, *args):
        return fixture_git(self.root, "-c", "core.fsmonitor=false", *args)

    def test_fixture_git_does_not_detach_maintenance_during_cleanup(self):
        self.git("config", "maintenance.auto", "true")
        self.git("config", "gc.auto", "1")
        trace = self.root.parent / "git-trace.jsonl"
        with mock.patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}):
            self.source.write_text("int main(void) { return 2; }\n")
            self.git("add", ".")
            self.commit()
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertTrue(any(event.get("event") == "start" for event in events))
        children = [
            event["argv"] for event in events if event.get("event") == "child_start"
        ]
        self.assertFalse(
            any("maintenance" in argv or "gc" in argv for argv in children), children
        )

    def commit(self):
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Original authority",
        )

    def snapshot(self):
        return {
            p.relative_to(self.root).as_posix(): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(self.root).parts
        }

    def apply(self, plan):
        return apply_original_project_adoption(
            self.root,
            expected_plan_identity=plan.identity.uri,
            acknowledge=True,
        )

    def test_read_only_plan_and_metadata_only_apply_preserve_original_tree(self):
        before = self.snapshot()
        plan = plan_original_project_adoption(self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            plan.git_commit, self.git("rev-parse", "HEAD").decode().strip()
        )
        result = self.apply(plan)
        after = self.snapshot()
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertLessEqual(
            set(after) - set(before),
            {
                ".literate/original-adoption.json",
                ".literate/conversion-authority.json",
                ".literate/conversion-authority.lock",
            },
        )
        self.assertEqual(
            (self.root / ".literate/original-adoption.json").read_bytes(),
            canonical_json_bytes(plan.to_dict()),
        )
        self.assertFalse(result["admitted"])
        self.assertEqual(result["authority"]["stage"], "original")
        self.assertEqual(result["authority"]["release_authority"], "original-source")
        self.assertFalse((self.root / "_legacy").exists())

    def test_acknowledgement_and_current_reviewed_identity_required(self):
        plan = plan_original_project_adoption(self.root)
        before = self.snapshot()
        with self.assertRaisesRegex(OriginalProjectAdoptionError, "acknowledgment"):
            apply_original_project_adoption(
                self.root, expected_plan_identity=plan.identity.uri, acknowledge=False
            )
        self.assertEqual(before, self.snapshot())
        self.source.write_text("int main(void) { return 1; }\n")
        self.git("add", ".")
        self.commit()
        with self.assertRaisesRegex(OriginalProjectAdoptionError, "differs"):
            self.apply(plan)
        self.assertFalse((self.root / ".literate/original-adoption.json").exists())

    def test_dirty_or_untracked_source_refuses_initial_binding(self):
        for name in ("tracked", "untracked"):
            with self.subTest(name=name):
                path = self.source if name == "tracked" else self.root / "extra.c"
                old = path.read_bytes() if path.exists() else None
                path.write_text("changed\n")
                with self.assertRaises(OriginalProjectAdoptionError) as caught:
                    plan_original_project_adoption(self.root)
                self.assertEqual(
                    caught.exception.code, "original_adoption.dirty-source"
                )
                if old is None:
                    path.unlink()
                else:
                    path.write_bytes(old)

    def test_existing_evidence_is_preserved(self):
        evidence = self.root / ".literate/original-adoption.json"
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_bytes(b"existing evidence\n")
        self.git("add", ".")
        self.commit()
        plan = plan_original_project_adoption(self.root)
        with self.assertRaisesRegex(OriginalProjectAdoptionError, "already exists"):
            self.apply(plan)
        self.assertEqual(evidence.read_bytes(), b"existing evidence\n")
        self.assertIsNone(FilesystemConversionAuthorityStore(self.root).load_optional())

    def test_original_has_no_legacy_conversion_successor_or_reinitialization(self):
        plan = plan_original_project_adoption(self.root)
        self.apply(plan)
        store = FilesystemConversionAuthorityStore(self.root)
        original = store.load_optional()
        for stage in ConversionAuthorityStage:
            with self.subTest(stage=stage):
                with self.assertRaises(ConversionAuthorityError):
                    store.advance(stage, evidence_identities=(plan.identity,))
                self.assertEqual(store.load_optional(), original)
        with self.assertRaisesRegex(OriginalProjectAdoptionError, "cannot be replaced"):
            plan_original_project_adoption(self.root)

    def test_redirected_root_is_rejected_without_writes(self):
        alias = self.root.parent / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("host does not permit symlinks")
        before = self.snapshot()
        with self.assertRaises((OriginalProjectAdoptionError, OSError, ValueError)):
            plan_original_project_adoption(alias)
        self.assertEqual(before, self.snapshot())

    def test_public_command_requires_reviewed_acknowledged_plan(self):
        args = ("--json", "project", "adopt-original", "--project", str(self.root))
        code, document = invoke(*args)
        self.assertEqual(code, 0, document)
        plan = document["result"]
        self.assertFalse(plan["writes"])
        code, document = invoke(*args, "--apply", "--acknowledge")
        self.assertEqual(code, 2, document)
        self.assertEqual(document["error"]["code"], "original_adoption.plan-required")
        code, document = invoke(
            *args, "--apply", "--expected-plan-identity", plan["plan_identity"]
        )
        self.assertEqual(code, 2, document)
        self.assertEqual(
            document["error"]["code"], "original_adoption.acknowledgement-required"
        )
        code, document = invoke(
            *args,
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            plan["plan_identity"],
        )
        self.assertEqual(code, 0, document)
        self.assertEqual(document["result"]["authority"]["stage"], "original")

    def test_source_mutation_between_evidence_and_authority_is_refused(self):
        from literate_ai.adapters import original_project_adoption as owner

        plan = plan_original_project_adoption(self.root)
        actual = owner._plan

        def mutate(project, **kwargs):
            if kwargs.get("allowed_new"):
                self.source.write_text("concurrent edit\n")
            return actual(project, **kwargs)

        with mock.patch.object(owner, "_plan", side_effect=mutate):
            with self.assertRaises(OriginalProjectAdoptionError):
                self.apply(plan)
        self.assertIsNone(FilesystemConversionAuthorityStore(self.root).load_optional())
        self.assertTrue((self.root / ".literate/original-adoption.json").exists())
        self.assertEqual(self.source.read_text(), "concurrent edit\n")

    def test_ordinary_rebuild_cannot_regenerate_original_source(self):
        self.apply(plan_original_project_adoption(self.root))
        before = self.snapshot()
        # Refused commands still emit diagnostics; keep disposable output outside
        # the original source whose complete byte snapshot must stay unchanged.
        diagnostics = self.root.parent / "diagnostics"
        with mock.patch.dict(os.environ, {"OBJ_DIR": str(diagnostics)}):
            code, document = invoke(
                "--json",
                "rebuild",
                ".",
                "--project",
                str(self.root),
                "--allow-host-execution",
            )
        self.assertEqual(code, 2, document)
        self.assertEqual(
            document["error"]["code"], "rebuild.conversion_authority_not_qualified"
        )
        self.assertIn("--retained-project-plan", document["error"]["message"])
        self.assertEqual(before, self.snapshot())
        self.assertTrue(tuple((diagnostics / ".litai" / "perf").glob("*.jsonl")))
