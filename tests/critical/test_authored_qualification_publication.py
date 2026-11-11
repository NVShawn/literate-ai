"""Real publication/files/archive state around injected execution and admission.

The test uses complete typed Standard diamond products and actual bytes, owned
filesystem namespaces, temporary resource retirement, atomic references and
recovery. Current plan review, Standard execution/custody and semantic comparison
are explicit fixtures. This does not qualify a genuine public model/native run.
"""

import io
import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, nullcontext
from copy import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.authored_publication_files import AuthoredPublicationFiles
from literate_ai.adapters.authored_qualification_capture import (
    reopen_authored_product_membership,
)
from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
)
from literate_ai.adapters.authored_qualification_publication import (
    AuthoredPublicationError,
    apply_authored_qualification,
    read_published_authored_qualification,
)
from literate_ai.adapters.qualification import FilesystemQualificationError
from literate_ai.contracts.authored_qualification_reference import (
    AuthoredQualificationReference,
)
from literate_ai.contracts.identity import canonical_identity
from tests.critical import test_authored_qualification_capture as capture_fixtures
from tests.critical import test_authored_qualification_planning as planning
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

MODULE = "literate_ai.adapters.authored_qualification_publication."


class AuthoredQualificationPublicationTests(unittest.TestCase):
    def setUp(self):
        f = capture_fixtures.AuthoredQualificationCaptureTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.capture = f
        p = planning.AuthoredQualificationPlanningTests("runTest")
        p.setUp()
        self.addCleanup(p.doCleanups)
        self.planning = p
        self.root = p.fixture.root
        original = p.plan()
        # These fixture graphs deliberately differ. Original current-plan
        # admission is injected; no genuine reviewed diamond is claimed.
        locked = SimpleNamespace(
            authority=SimpleNamespace(lock=f.fixture.fixture.lock),
            component_root=lambda _identity: p.fixture.component,
        )
        oracles = SimpleNamespace(
            bindings=f.lifecycle.authored_plan.oracles.bindings,
            authority=SimpleNamespace(
                project=SimpleNamespace(root=self.root), locked=locked
            ),
        )
        self.current = replace(original, plan=f.result.plan, oracles=oracles)
        f.lifecycle.authored_plan = self.current
        f.lifecycle.prepared = SimpleNamespace(native_sdk_inputs=None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scratch_parent = Path(temporary.name).resolve()
        self.paths = []
        self.active = True
        self.interrupt_after_stage = False
        self.fail_replay = False
        self.drift_during_replay = False
        self.comparisons = 0
        self.executions = 0
        self.tools_active = True

    @contextmanager
    def admitted(self):
        def guard(_current):
            if not self.active:
                raise ValueError("fixture current authority changed")

        def projected_guard():
            self.current.require_unchanged()
            if not self.tools_active:
                raise ValueError("fixture projected tool drift")

        @contextmanager
        def execute(_current, _selection, **kwargs):
            self.executions += 1
            path = kwargs["scratch_root"]
            self.paths.append(path)
            (path / "generated-runtime.txt").write_text("fixture generated output")
            self.capture.active = True
            try:
                yield self.capture.owner
                if self.interrupt_after_stage:
                    raise RuntimeError("fixture interrupted after complete stage")
            finally:
                self.capture.active = False

        def compare(_projection, reader, identity, **kwargs):
            self.comparisons += 1
            if self.comparisons > 1:
                self.assertTrue(all(not p.exists() for p in self.paths))
                if self.fail_replay:
                    raise ValueError("fixture semantic replay refused")
                if self.drift_during_replay:
                    self.active = False
            return SimpleNamespace(
                membership=reopen_authored_product_membership(reader, identity)
            )

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    CurrentAuthoredQualificationPlan, "require_unchanged", guard
                )
            )
            stack.enter_context(
                patch(MODULE + "open_authored_qualification_execution", execute)
            )
            stack.enter_context(
                patch(
                    MODULE + "open_authored_execution_projection",
                    side_effect=lambda _owner: nullcontext(
                        SimpleNamespace(require_unchanged=projected_guard)
                    ),
                )
            )
            stack.enter_context(
                patch(
                    MODULE + "open_current_authored_qualification_projection",
                    side_effect=lambda *_a, **_k: nullcontext(
                        SimpleNamespace(require_unchanged=projected_guard)
                    ),
                )
            )
            stack.enter_context(
                patch(
                    MODULE + "reopen_projected_authored_qualification",
                    side_effect=compare,
                )
            )
            yield

    def apply(self, **kwargs):
        return apply_authored_qualification(
            self.current,
            self.planning.request,
            **(
                {
                    "acknowledged_plan": self.current.plan.identity,
                    "allow_host_execution": True,
                    "scratch_parent": self.scratch_parent,
                }
                | kwargs
            ),
        )

    def base(self, attempt):
        return self.root / self.current.plan.output_root / "a" / attempt

    def test_publish_after_real_resource_retirement_and_closed_archive_replay(self):
        with self.admitted():
            published = self.apply()
            self.assertEqual(self.executions, 1)
            self.assertEqual(self.comparisons, 2)
            self.assertTrue(all(not p.exists() for p in self.paths))
            reference = published.reference
            SchemaCatalog().validate(reference.SCHEMA, reference.to_dict())
            self.assertEqual(
                AuthoredQualificationReference.from_dict(reference.to_dict()), reference
            )
            reread = read_published_authored_qualification(
                self.current, reference.attempt, expected_identity=reference.identity
            )
            self.assertEqual(reread, published)
            self.assertTrue((self.root / published.reference_path).is_file())
            self.assertEqual(
                (
                    self.root / self.current.plan.output_root / "a/.gitignore"
                ).read_bytes(),
                b"*\n",
            )

    def test_acknowledgement_and_host_permission_precede_any_output(self):
        for changed in (
            {"acknowledged_plan": canonical_identity("wrong")},
            {"acknowledged_plan": self.current.plan.identity.uri},
            {"allow_host_execution": False},
            {"allow_host_execution": 1},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(FilesystemQualificationError):
                    self.apply(**changed)
        self.assertFalse((self.root / self.current.plan.output_root).exists())
        self.assertFalse(list(self.scratch_parent.iterdir()))

    def test_interrupted_stage_is_not_a_reference_and_recovery_does_not_delete_runs(
        self,
    ):
        with self.admitted():
            self.interrupt_after_stage = True
            with self.assertRaises(AuthoredPublicationError) as stopped:
                self.apply()
            attempt = stopped.exception.attempt
            self.assertTrue((self.base(attempt) / "stage.json").is_file())
            self.assertFalse((self.base(attempt) / "reference.json").exists())
            self.assertTrue(self.paths[0].exists())
            with self.assertRaisesRegex(
                AuthoredPublicationError, "run_resources_not_retired"
            ):
                self.apply(recover_attempt=attempt)
            self.assertTrue(self.paths[0].exists())
            self.assertEqual(self.executions, 1)

    def test_recovery_uses_new_attempt_fresh_replay_and_preserves_old_journal(self):
        with self.admitted():
            self.interrupt_after_stage = True
            with self.assertRaises(AuthoredPublicationError) as stopped:
                self.apply()
            old = stopped.exception.attempt
            journal = {p.name: p.read_bytes() for p in self.base(old).glob("*.json")}
            # The enclosing test owns this whole temporary tree; production
            # recovery must never derive deletion permission from its journal.
            shutil.rmtree(self.paths[0])
            self.interrupt_after_stage = False
            self.comparisons = 1
            recovered = self.apply(recover_attempt=old)
            self.assertNotEqual(recovered.reference.attempt, old)
            self.assertEqual(self.executions, 1)
            self.assertEqual(
                {p.name: p.read_bytes() for p in self.base(old).glob("*.json")}, journal
            )
            self.assertFalse((self.base(old) / "reference.json").exists())
            same = self.apply(recover_attempt=recovered.reference.attempt)
            self.assertEqual(same.reference, recovered.reference)

    def test_failed_replay_or_closing_authority_has_no_reference(self):
        for flag in ("fail_replay", "drift_during_replay"):
            with self.subTest(flag=flag), self.admitted():
                setattr(self, flag, True)
                with self.assertRaises(AuthoredPublicationError) as failed:
                    self.apply()
                self.assertFalse(
                    (self.base(failed.exception.attempt) / "reference.json").exists()
                )
                self.assertTrue(all(not p.exists() for p in self.paths))
                self.active = True
                setattr(self, flag, False)
                self.comparisons = 0

    def test_substituted_reference_and_wrong_reference_identity_refuse(self):
        with self.admitted():
            published = self.apply()
            with self.assertRaisesRegex(ValueError, "reference_mismatch"):
                read_published_authored_qualification(
                    self.current,
                    published.reference.attempt,
                    expected_identity=canonical_identity("foreign-reference"),
                )
            path = self.root / published.reference_path
            data = json.loads(path.read_bytes())
            data["products_identity"] = canonical_identity("foreign-products").to_dict()
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                read_published_authored_qualification(
                    self.current, published.reference.attempt
                )

    def test_output_namespace_links_refuse_and_leave_foreign_directory_empty(self):
        outside = self.scratch_parent / "foreign"
        outside.mkdir()
        linked = self.root / "litai/qualification"
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("controller cannot create directory links")
        with self.admitted(), self.assertRaises(AuthoredPublicationError):
            self.apply()
        self.assertEqual(list(outside.iterdir()), [])

    def test_file_owner_expires_and_exclusive_writes_never_replace_bytes(self):
        with self.admitted():
            with AuthoredPublicationFiles(self.current, writable=True) as files:
                name = "litai/qualification/test.json"
                files.write(name, b"original", max_bytes=32)
                with self.assertRaises(FileExistsError):
                    files.write(name, b"replacement", max_bytes=32)
                self.assertEqual(files.read(name, max_bytes=32), b"original")
                with self.assertRaises(ValueError):
                    files.read(name, max_bytes=2)
                with self.assertRaises(ValueError):
                    files.write("../escape", b"bad", max_bytes=32)
            with self.assertRaisesRegex(ValueError, "owner_expired"):
                files.write(name, b"late", max_bytes=32)

    def test_copied_file_owner_cannot_borrow_namespace_or_writer_mode(self):
        with (
            self.admitted(),
            AuthoredPublicationFiles(self.current, writable=True) as files,
        ):
            copied = copy(files)
            with self.assertRaisesRegex(
                ValueError, "original_publication_owner_required"
            ):
                copied.mkdir("litai/qualification/borrowed")
            self.assertFalse((self.root / "litai/qualification/borrowed").exists())

    def test_projected_tool_drift_after_link_cannot_return_success_or_reopen(self):
        original = AuthoredPublicationFiles.write

        def after_link(files, relative, *args, **kwargs):
            result = original(files, relative, *args, **kwargs)
            if relative.endswith("/reference.json"):
                self.tools_active = False
            return result

        with (
            self.admitted(),
            patch.object(AuthoredPublicationFiles, "write", after_link),
        ):
            with self.assertRaisesRegex(
                AuthoredPublicationError, "projected tool drift"
            ) as refused:
                self.apply()
            # The atomic commit may have happened. Its complete historical bytes
            # still confer no current permission after the closing tool refusal.
            with self.assertRaisesRegex(ValueError, "projected tool drift"):
                read_published_authored_qualification(
                    self.current, refused.exception.attempt
                )

    def test_reference_contract_refuses_source_history_and_unknown_origin(self):
        with self.admitted():
            reference = self.apply().reference
        for data in (
            {
                **reference.to_dict(),
                "promotion_tree_identity": canonical_identity("invented").to_dict(),
            },
            {**reference.to_dict(), "origin": {"origin": "unknown"}},
        ):
            with self.assertRaises(ValueError):
                AuthoredQualificationReference.from_dict(data)
            with self.assertRaises(AssertionError):
                SchemaCatalog().validate(reference.SCHEMA, data)

    def test_recovery_refuses_corrupt_archive_before_any_reference_or_execution(self):
        with self.admitted():
            self.interrupt_after_stage = True
            with self.assertRaises(AuthoredPublicationError) as stopped:
                self.apply()
            old = stopped.exception.attempt
            shutil.rmtree(self.paths[0])
            blobs = list((self.base(old) / "blobs/sha256").glob("*/*"))
            self.assertEqual(len(blobs), 1)
            blobs[0].write_bytes(b"substituted archive")
            with self.assertRaises(AuthoredPublicationError) as refused:
                self.apply(
                    recover_attempt=old, scratch_parent=self.root / "does-not-exist"
                )
            self.assertFalse(
                (self.base(refused.exception.attempt) / "reference.json").exists()
            )
            self.assertEqual(self.executions, 1)

    def cli(self, extra):
        from literate_ai.cli import authored_qualification, dispatch

        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(
                authored_qualification,
                "FilesystemAuthoredQualificationPlanner",
                return_value=SimpleNamespace(plan=lambda *_a, **_k: self.current),
            ),
            patch.object(
                dispatch,
                "maybe_host_self_update",
                side_effect=AssertionError("implicit host writer"),
            ),
            patch.object(
                dispatch,
                "ensure_user_mcp_catalog",
                side_effect=AssertionError("implicit catalog writer"),
            ),
            patch.object(
                dispatch,
                "journal_mutagenic_event",
                side_effect=AssertionError("implicit event writer"),
            ),
        ):
            status = dispatch.main(
                [
                    "--json",
                    "project",
                    "qualify",
                    "apply",
                    "components/greeting",
                    "--project",
                    str(self.root),
                    *extra,
                ],
                stdout=out,
                stderr=err,
            )
        return status, json.loads(out.getvalue() or err.getvalue())

    def test_public_apply_cli_requires_acknowledgement_and_host_permission(self):
        with self.admitted():
            for options in (
                [],
                [
                    "--acknowledge-plan",
                    canonical_identity("wrong").uri,
                    "--allow-host-execution",
                ],
                ["--acknowledge-plan", self.current.plan.identity.uri],
            ):
                with self.subTest(options=options):
                    status, envelope = self.cli(options)
                    self.assertNotEqual(status, 0)
                    self.assertFalse(envelope["ok"])
        self.assertFalse((self.root / self.current.plan.output_root).exists())
        self.assertEqual(self.executions, 0)

    def test_public_apply_cli_returns_exact_published_reference(self):
        with self.admitted():
            status, envelope = self.cli(
                [
                    "--acknowledge-plan",
                    self.current.plan.identity.uri,
                    "--allow-host-execution",
                    "--scratch-parent",
                    str(self.scratch_parent),
                ]
            )
        self.assertEqual(status, 0, envelope)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["command"], "project.qualify")
        result = envelope["result"]
        self.assertTrue(result["qualified"])
        reference = AuthoredQualificationReference.from_dict(result["reference"])
        self.assertEqual(reference.identity.uri, result["reference_identity"])
        self.assertEqual(
            json.loads((self.root / result["reference_path"]).read_bytes()),
            reference.to_dict(),
        )
