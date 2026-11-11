"""Real preparation and held namespaces around injected downstream execution.

Current graph/model/private-input reads, preparation and directory owners are
real. Standard execution, comparison and final result admission are explicit
fixture ports. These controls establish no genuine generation, native acceptance,
closed capture, publication or retained qualification.
"""

import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.authored_qualification_execution import (
    open_authored_qualification_execution,
)
from literate_ai.adapters.qualification import FilesystemQualificationError
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
)
from literate_ai.contracts.identity import canonical_identity
from tests.critical import test_authored_qualification_planning as planning

MODULE = "literate_ai.adapters.authored_qualification_execution."


class _RunObservation:
    def __init__(self, request):
        self.request = request

    def to_dict(self):
        return {"fixture-observed-request": self.request.to_dict()}


class AuthoredQualificationExecutionTests(unittest.TestCase):
    def setUp(self):
        f = planning.AuthoredQualificationPlanningTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.fixture = f
        self.current = f.plan()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name).resolve()
        self.arguments = {
            "acknowledged_plan": self.current.plan.identity,
            "allow_host_execution": True,
            "scratch_root": self.scratch,
        }
        self.ports = []
        self.hook = None

    @contextmanager
    def injected_execution(self):
        outer = self

        class Port:
            def __init__(self, **arguments):
                self.arguments = arguments
                self.workspaces = arguments["workspaces"]
                self.product_sources = {}
                self.evidence_recorder = None
                self.requests = []
                self.paths = []
                outer.ports.append(self)

            def require_current_authority(self):
                outer.current.require_unchanged()

            def execute(self, request):
                with self.workspaces.capture_authored_run(
                    request.workspace_allocation_identity, request.run_identity
                ) as path:
                    outer.assertFalse(list(path.iterdir()))
                    self.requests.append(request)
                    self.paths.append(path)
                    (path / "fixture-execution.txt").write_text(
                        request.run_identity.uri
                    )
                    if outer.hook is not None:
                        outer.hook(self, request)
                return SimpleNamespace(request=request)

        def result(plan, runs):
            return SimpleNamespace(
                plan=plan,
                runs=runs,
                to_dict=lambda: {"fixture-result": [r.to_dict() for r in runs]},
            )

        with ExitStack() as stack:
            stack.enter_context(
                patch(MODULE + "FilesystemStandardQualificationLifecyclePort", Port)
            )
            observer = stack.enter_context(
                patch(
                    MODULE + "observe_authored_qualification_run",
                    side_effect=lambda _current, **kw: _RunObservation(kw["request"]),
                )
            )
            result_admission = stack.enter_context(
                patch(MODULE + "AuthoredQualificationResult", side_effect=result)
            )
            yield observer, result_admission

    def open(self, **changes):
        return open_authored_qualification_execution(
            self.current, self.fixture.request, **(self.arguments | changes)
        )

    def test_acknowledgement_and_host_permission_precede_preparation_or_allocation(
        self,
    ):
        for changes in (
            {"acknowledged_plan": canonical_identity({"wrong-plan": True})},
            {"acknowledged_plan": self.current.plan.identity.uri},
            {"allow_host_execution": False},
            {"allow_host_execution": 1},
        ):
            with (
                self.subTest(changes=changes),
                patch(
                    MODULE + "FilesystemLockedGenerationApplicationAdapter"
                ) as prepare,
                patch(MODULE + "FilesystemQualificationWorkspaceAllocator") as allocate,
                self.assertRaises(FilesystemQualificationError),
                self.open(**changes),
            ):
                self.fail("unauthorized execution yielded an owner")
            prepare.assert_not_called()
            allocate.assert_not_called()

    def test_complete_run_loop_keeps_original_owners_then_expires_without_publication(
        self,
    ):
        with self.injected_execution():
            with self.open() as owner:
                owner.require_unchanged()
                self.assertTrue(owner.evidence_records)
                reader = QualificationEvidenceReader(
                    owner.evidence_records,
                    max_bytes=256 * 1024 * 1024,
                    max_records=100_000,
                )
                port = self.ports[0]
                self.assertEqual(len(port.requests), 2)
                self.assertEqual(len({r.run_identity for r in port.requests}), 2)
                self.assertEqual(
                    len({r.workspace_allocation_identity for r in port.requests}), 2
                )
                self.assertEqual(len(set(port.paths)), 2)
                self.assertIs(port.arguments["authored_plan"], self.current)
                self.assertIs(port.arguments["binding"], self.current.binding)
                expected = {
                    n.revision.identity
                    for n in self.current.oracles.authority.locked.authority.lock.nodes
                }
                self.assertEqual(
                    set(port.arguments["invalidation"].regenerate), expected
                )
                for request in port.requests:
                    self.assertTrue(request.force_regeneration)
                    self.assertEqual(request.plan_identity, self.current.plan.identity)
                    self.assertEqual(request.origin, self.current.plan.origin)
                selections = [reader.read_json(r.run_identity) for r in port.requests]
                self.assertEqual([r["ordinal"] for r in selections], [0, 1])
                self.assertEqual(len({r["session_identity"] for r in selections}), 1)
            with self.assertRaisesRegex(ValueError, "execution_owner_expired"):
                owner.require_unchanged()
            self.assertTrue(port.workspaces._closed)
            self.assertIsNone(port.evidence_recorder)
            self.assertEqual(port.product_sources, {})
            self.assertTrue(
                all((p / "fixture-execution.txt").exists() for p in port.paths)
            )
            self.assertFalse(
                (self.fixture.fixture.root / self.current.plan.output_root).exists()
            )

    def test_second_run_failure_preserves_diagnostics_and_returns_no_result_owner(self):
        def fail(port, _request):
            if len(port.requests) == 2:
                raise RuntimeError("second native run failed")

        self.hook = fail
        with self.injected_execution() as (observer, result):
            with (
                self.assertRaisesRegex(RuntimeError, "second native run failed"),
                self.open(),
            ):
                self.fail("failed execution yielded a positive owner")
            self.assertEqual(observer.call_count, 1)
            result.assert_not_called()
        port = self.ports[0]
        self.assertTrue(port.workspaces._closed)
        self.assertIsNone(port.evidence_recorder)
        self.assertEqual(len(port.paths), 2)
        self.assertTrue(all((p / "fixture-execution.txt").exists() for p in port.paths))

    def test_private_input_drift_cannot_return_an_execution_owner(self):
        def change_input(port, _request):
            if len(port.requests) == 2:
                path = self.fixture.fixture.oracle_root / "harness.py"
                path.write_bytes(path.read_bytes() + b"\n")

        self.hook = change_input
        with self.injected_execution() as (_observer, result):
            with self.assertRaises(ValueError), self.open():
                self.fail("stale verifier yielded an owner")
            result.assert_not_called()
        self.assertTrue(self.ports[0].workspaces._closed)

    def test_substituted_live_namespace_cannot_survive_the_capture_scope(self):
        with self.injected_execution():
            with self.open() as owner:
                path = self.ports[0].paths[0]
                retired = path.with_name("retired-" + path.name)
                if os.name == "nt":
                    with self.assertRaises(OSError):
                        path.rename(retired)
                    return
                path.rename(retired)
                path.mkdir()
                try:
                    with self.assertRaises(ValueError):
                        owner.require_unchanged()
                finally:
                    path.rmdir()
                    retired.rename(path)

    def test_authority_scratch_and_prepared_graph_substitution_refuse_before_allocation(
        self,
    ):
        with (
            self.assertRaises(FilesystemQualificationError),
            self.open(scratch_root=self.fixture.fixture.root),
        ):
            self.fail("authority-local scratch was accepted")
        from literate_ai.adapters.generation_preparation import (
            FilesystemLockedGenerationApplicationAdapter,
        )

        original = FilesystemLockedGenerationApplicationAdapter().prepare(
            self.fixture.request
        )
        changed = replace(original, component_root=self.scratch)
        with (
            patch(
                MODULE + "FilesystemLockedGenerationApplicationAdapter.prepare",
                return_value=changed,
            ),
            patch(MODULE + "FilesystemQualificationWorkspaceAllocator") as allocate,
            self.assertRaises(FilesystemQualificationError),
            self.open(),
        ):
            self.fail("substituted preparation yielded an owner")
        allocate.assert_not_called()

    def test_capture_limit_refuses_before_any_run_allocation(self):
        with (
            self.injected_execution(),
            patch(MODULE + "FilesystemQualificationWorkspaceAllocator") as allocate,
            self.assertRaises(QualificationCaptureError),
            self.open(maximum_capture_bytes=1),
        ):
            self.fail("unretainable plan yielded an owner")
        allocate.assert_not_called()
        self.assertEqual(self.ports, [])

    def test_capture_consumer_failure_closes_the_original_scope(self):
        with self.injected_execution():
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                with self.open() as owner:
                    raise RuntimeError("capture failed")
            with self.assertRaisesRegex(ValueError, "execution_owner_expired"):
                _ = owner.evidence_records
        self.assertTrue(self.ports[0].workspaces._closed)
        self.assertIsNone(self.ports[0].evidence_recorder)
