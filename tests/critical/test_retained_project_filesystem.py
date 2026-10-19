"""Real file effects, wrong-product refusal and private verifier custody."""

import hashlib
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_project_filesystem as observer
from literate_ai.adapters.retained_project_acceptance import (
    RetainedProjectAcceptanceError,
)
from literate_ai.adapters.retained_project_receipt import admit_retained_project
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptanceCase,
)
from literate_ai.contracts.retained_project_filesystem import (
    RetainedFilesystemCase,
    RetainedFilesystemInput,
    RetainedFilesystemState,
)
from literate_ai.storage.cas import FileSystemCAS
from tests.critical import test_retained_project_acceptance as acceptance
from tests.critical import test_retained_project_completion as completion
from tests.support.fixtures_test_retained_filesystem import (
    filesystem_case,
    select_filesystem_case,
)


class RetainedFilesystemContractTests(unittest.TestCase):
    def test_opt_in_roundtrip_and_legacy_case_wire_identity(self):
        case = RetainedProjectAcceptanceCase("c", "a", "app", (), b"", 0, b"", b"", 10)
        legacy = {
            "id": "c",
            "action": "a",
            "executable": "app",
            "argv": [],
            "stdin": "",
            "expected_exit": 0,
            "expected_stdout": "",
            "expected_stderr": "",
            "timeout_seconds": 10,
        }
        self.assertEqual(case.to_dict(), legacy)
        self.assertEqual(case.identity, canonical_identity(legacy))
        opted = replace(case, filesystem=filesystem_case())
        self.assertEqual(
            RetainedProjectAcceptanceCase.from_dict(opted.to_dict()), opted
        )
        self.assertNotEqual(opted.identity, case.identity)

    def test_aliases_collisions_incomplete_fixtures_and_budgets_refuse(self):
        original = filesystem_case()
        # Validate construction boundaries with real typed records.
        for names in (("A/x", "a/y"), ("a", "a/b")):
            values = tuple(RetainedFilesystemInput(n, b"x") for n in names)
            expected = tuple(RetainedFilesystemState(n, "absent") for n in names)
            with self.subTest(names=names), self.assertRaises(ValueError):
                RetainedFilesystemCase(
                    values, expected, RetainedProjectLimits(8, 16, 64)
                )
        with self.assertRaises(ValueError):
            RetainedFilesystemInput("../escape", b"x")
        with self.assertRaises(ValueError):
            replace(original, limits=RetainedProjectLimits(1, 2, 4))
        with self.assertRaises(ValueError):
            replace(original, inputs=(RetainedFilesystemInput("undeclared", b"x"),))
        with self.assertRaises(ValueError):
            RetainedFilesystemState("data", "file", 3, "not-a-hash")


class RetainedFilesystemObserverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.outputs = self.root / "exports"
        self.outputs.mkdir()
        self.workspace = observer.RetainedFilesystemWorkspace(
            filesystem_case(), self.root, self.outputs
        )
        self.addCleanup(self.workspace.close)

    def test_real_content_effect_and_extra_output_refusal(self):
        (self.workspace.root / "data").write_bytes(b"abc")
        observed = self.workspace.observe()
        self.assertTrue(self.workspace.matches(observed))
        (self.workspace.root / "extra").write_bytes(b"unexpected")
        self.assertFalse(self.workspace.matches(self.workspace.observe()))
        self.assertNotEqual(observed.identity, self.workspace.observe().identity)

    @unittest.skipIf(os.name == "nt", "requires POSIX links and special nodes")
    def test_symlinks_hardlinks_and_special_nodes_refuse_without_following(self):
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        node = self.workspace.root / "unsafe"
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    node.symlink_to(outside)
                elif kind == "hardlink":
                    os.link(outside, node)
                else:
                    os.mkfifo(node)
                with self.assertRaisesRegex(ValueError, "link-or-special"):
                    self.workspace.observe()
                node.unlink()
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_entry_and_byte_growth_refuse(self):
        (self.workspace.root / "data").write_bytes(b"x" * 17)
        with self.assertRaises(ValueError):
            self.workspace.observe()
        (self.workspace.root / "data").write_bytes(b"abc")
        for index in range(9):
            (self.workspace.root / str(index)).write_bytes(b"")
        with self.assertRaises(ValueError):
            self.workspace.observe()

    def test_aggregate_byte_budget_and_midread_mutation_refuse(self):
        bounded = replace(filesystem_case(), limits=RetainedProjectLimits(8, 8, 8))
        workspace = observer.RetainedFilesystemWorkspace(
            bounded, self.root, self.outputs
        )
        self.addCleanup(workspace.close)
        (workspace.root / "data").write_bytes(b"abc")
        (workspace.root / "extra").write_bytes(b"123456")
        with self.assertRaises(ValueError):
            workspace.observe()
        original = observer._stream

        def mutate(root, name, limit):
            result = original(root, name, limit)
            (root / name).write_bytes(b"changed after read")
            return result

        with patch.object(observer, "_stream", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "file-changed"):
                self.workspace.observe()

    def test_export_contamination_and_case_replacement_refuse(self):
        with self.assertRaises(ValueError):
            observer.RetainedFilesystemWorkspace(
                filesystem_case(), self.outputs, self.outputs
            )
        moved = self.root / "moved-case"
        self.workspace.root.rename(moved)
        self.workspace.root.mkdir()
        with self.assertRaisesRegex(ValueError, "case-replaced"):
            self.workspace.observe()
        with self.assertRaisesRegex(ValueError, "case-replaced-during-cleanup"):
            self.workspace.close()
        self.assertTrue(self.workspace.root.exists())


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc"), "requires native POSIX compiler"
)
class RetainedFilesystemExecutableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        acceptance.RetainedProjectAcceptanceTests.setUpClass()
        cls.addClassCleanup(acceptance.RetainedProjectAcceptanceTests.doClassCleanups)

    def setUp(self):
        self.fixture = acceptance.RetainedProjectAcceptanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.case = replace(
            self.fixture.case,
            argv=("report", "{case_root}/data", "abc"),
            filesystem=filesystem_case(),
        )
        self.fixture.policy = replace(self.fixture.policy, cases=(self.fixture.case,))
        self.fixture.write_policy()
        self.parent = self.fixture.fixture.base / "cases"
        self.parent.mkdir()

    def execute(self, command_scope=None):
        return acceptance.acceptance.verify_retained_acceptance_action(
            self.fixture.policy,
            self.fixture.plan,
            "accept",
            self.fixture.outputs,
            self.fixture.manifest,
            environment=self.fixture.environment,
            require_current=self.fixture.require_current,
            case_parent=self.parent,
            command_scope=command_scope,
        )

    def test_real_program_changes_private_file_and_preserves_sealed_exports(self):
        before = (self.fixture.outputs / "app.bin").read_bytes()
        (result,) = self.execute()
        (state,) = result.filesystem_observation.entries
        self.assertEqual(
            (state.size, state.sha256), (3, hashlib.sha256(b"abc").hexdigest())
        )
        self.assertEqual(list(self.parent.iterdir()), [])
        self.assertEqual((self.fixture.outputs / "app.bin").read_bytes(), before)
        (repeated,) = self.execute()
        self.assertEqual(repeated.filesystem_observation, result.filesystem_observation)
        self.assertNotEqual(
            repeated.filesystem_environment_identity,
            result.filesystem_environment_identity,
        )
        self.assertNotIn(
            "filesystem_observation",
            replace(
                result,
                filesystem_observation=None,
                filesystem_dependency_identity=None,
                filesystem_environment_identity=None,
            ).to_dict(),
        )

    def test_scope_cannot_substitute_initial_fixtures_before_execution(self):
        @contextmanager
        def changed_scope(_command, **values):
            (values["cwd"] / "data").write_bytes(b"abc")
            yield None

        with patch.object(acceptance.acceptance, "run_bounded_process") as run:
            with self.assertRaisesRegex(ValueError, "fixtures-changed"):
                self.execute(command_scope=changed_scope)
            run.assert_not_called()

    def test_always_zero_wrong_product_retains_actual_mismatch(self):
        self.fixture.case = replace(self.fixture.case, argv=("empty",))
        self.fixture.policy = replace(self.fixture.policy, cases=(self.fixture.case,))
        self.fixture.write_policy()
        with self.assertRaises(RetainedProjectAcceptanceError) as failure:
            self.execute()
        (result,) = failure.exception.observations
        self.assertEqual(result.exit_code, 0)
        (state,) = result.filesystem_observation.entries
        self.assertEqual(state.size, 6)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_receipt_retains_filesystem_facts_and_join_refuses_substitution(self):
        fixture = completion.RetainedProjectCompletionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        select_filesystem_case(fixture)
        workspace, run = fixture.finish()
        (observation,) = run.acceptance_observations
        with self.assertRaisesRegex(ValueError, "filesystem-observation-mismatch"):
            replace(
                run,
                acceptance_observations=(
                    replace(
                        observation,
                        filesystem_observation=None,
                        filesystem_dependency_identity=None,
                        filesystem_environment_identity=None,
                    ),
                ),
            )
        admission = admit_retained_project(
            workspace, FileSystemCAS(fixture.fixture.base / "fs-cas")
        )
        reference = next(
            v
            for v in admission.receipt.records
            if v.identity == observation.identity.uri
        )
        retained = admission.store.get_manifest(reference)
        self.assertEqual(
            retained["filesystem_observation"],
            observation.filesystem_observation.to_dict(),
        )
        self.assertFalse(admission.receipt.to_dict()["release_authorized"])
