"""Shared test fixtures extracted from test_standard_rebuild_adapter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.generation_preparation import PreparedLockedGeneration
from literate_ai.adapters.standard_lifecycle_binding import (
    InstalledFrameworkDistribution,
    InstalledFrameworkDistributionMember,
    ResolvedStandardProjectLifecycleDriver,
)
from literate_ai.adapters.standard_project import (
    FilesystemStandardProjectRuntime,
    PlannedStandardProject,
    StandardProjectRuntimeReadiness,
)
from literate_ai.adapters.standard_rebuild import (
    FilesystemStandardRebuildAdapter,
    FilesystemStandardRebuildError,
    FilesystemStandardRebuildRequest,
    _configured_python_wheelhouse,
    _resolved_source_cache,
)
from literate_ai.application.component_generation_scheduling import (
    ComponentInvalidationDecision,
)
from literate_ai.contracts import (
    ComponentChangeSurface,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestSummary,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheRootKind,
    SourceCacheTarget,
    StandardProjectLifecycleDriver,
    VersionedContentRef,
    canonical_identity,
    load_current_standard_lifecycle_policy,
)
from literate_ai.projects import LoadedProject


def _identity(label: str):
    return canonical_identity({"standard-rebuild-test": label})


class _Snapshot:
    def __init__(self, lock_identity):
        self.authority = SimpleNamespace(lock=SimpleNamespace(identity=lock_identity))
        self.calls = 0

    def require_unchanged(self):
        self.calls += 1


class FilesystemStandardRebuildAdapterTests(unittest.TestCase):
    def test_retained_authorization_and_checkpoint_invalidation(self):
        from literate_ai.adapters.retained_source import (
            RetainedSourceError,
            RetainedSourceInput,
        )

        retained_root = self.root / "retained"
        retained_root.mkdir()
        (retained_root / "main.py").write_text("pass\n")
        validated = _identity("reviewed-project")
        revision = _identity("component")
        self.snapshot.authority.lock.nodes = (
            SimpleNamespace(revision=SimpleNamespace(identity=revision)),
        )
        self.snapshot.authority.lock.root_revision = revision
        self.snapshot.authority.lock.target_name = "host"
        retained = RetainedSourceInput.capture(
            retained_root,
            component_lock_identity=self.lock_identity,
            project_authority_identity=validated,
            target="host",
        )

        def adapter(authorization):
            return FilesystemStandardRebuildAdapter(
                project=self.project,
                binding=self.binding,
                runtime=self.runtime,
                source_cache_configuration=self.source_cache_configuration,
                authority_validator=lambda _root: validated,
                retained_source=retained,
                retained_source_authorization=authorization,
            )

        with self.assertRaises(RetainedSourceError):
            adapter(None)
        approved = adapter(retained.identity.uri)
        planned = PlannedStandardProject(
            SimpleNamespace(require_unchanged=lambda: None),
            SimpleNamespace(identity=_identity("plan")),
        )
        with (
            mock.patch.object(
                FilesystemStandardProjectRuntime, "plan", return_value=planned
            ),
            mock.patch.object(
                FilesystemStandardProjectRuntime,
                "production_readiness",
                return_value=StandardProjectRuntimeReadiness(True, ()),
            ),
            mock.patch.object(
                FilesystemStandardProjectRuntime,
                "execute",
                side_effect=RuntimeError("boundary reached"),
            ) as execute,
        ):
            with self.assertRaisesRegex(RuntimeError, "boundary reached"):
                approved.rebuild(
                    FilesystemStandardRebuildRequest(
                        self.prepared, self.source_root, self.invalidation
                    )
                )
        self.assertTrue(execute.call_args.args[1].fresh_source)
        self.assertEqual(execute.call_args.args[1].invalidation, self.invalidation)
        self.snapshot.authority.lock.target_name = "other"
        with self.assertRaises(FilesystemStandardRebuildError):
            approved.rebuild(
                FilesystemStandardRebuildRequest(
                    self.prepared, self.source_root, self.invalidation
                )
            )

    def test_python_wheel_provisioning_is_explicit_and_not_package_authority(self):
        self.assertIsNone(_configured_python_wheelhouse(None, {}))
        selected = self.root / "wheel pool"
        self.assertEqual(
            _configured_python_wheelhouse(
                None, {"LITAI_PYTHON_WHEELHOUSE": str(selected)}
            ),
            selected,
        )
        self.assertEqual(
            _configured_python_wheelhouse(
                selected, {"LITAI_PYTHON_WHEELHOUSE": "invalid-relative"}
            ),
            selected,
        )
        self.assertFalse(selected.exists())
        for raw in ("", "relative/wheels", "~", "bad\x00path"):
            with (
                self.subTest(value=raw),
                self.assertRaises(FilesystemStandardRebuildError),
            ):
                _configured_python_wheelhouse(None, {"LITAI_PYTHON_WHEELHOUSE": raw})

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.source_root = self.root / "runtime" / "sources"
        policy = load_current_standard_lifecycle_policy()
        distribution = InstalledFrameworkDistribution(
            "literate-ai",
            "0.2.0",
            (
                InstalledFrameworkDistributionMember(
                    "literate_ai/__init__.py",
                    1,
                    "sha256:" + "a" * 64,
                ),
            ),
        )
        driver = StandardProjectLifecycleDriver(
            distribution.identity,
            policy.identity,
        )
        self.binding = ResolvedStandardProjectLifecycleDriver(
            driver,
            distribution,
            policy,
            lambda: distribution,
            lambda: policy,
        )
        self.project = LoadedProject(
            self.project_root,
            SimpleNamespace(
                project_id="adapter-proof",
                lifecycle_driver=driver,
                test_receipt_policy=object(),
                # A real ProjectDefinition always carries this; declaring none here
                # exercises the runtime-only default the composition falls back to.
                source_cache=None,
            ),
        )
        self.closure = SimpleNamespace(
            record=SimpleNamespace(identity=_identity("toolchain"))
        )
        self.runtime = FilesystemStandardProjectRuntime(
            None,
            None,
            None,
            None,
            None,
            True,
            True,
            self.closure,
            object(),
            object(),
        )
        self.source_cache_configuration = SourceCacheConfiguration(
            SourceCacheMode.READ_WRITE,
            (
                SourceCacheTarget(
                    "standard-local",
                    root_kind=SourceCacheRootKind.OPERATOR_BOUND,
                    root_reference="standard-local-source-cache",
                ),
            ),
            write_target_id="standard-local",
        )
        self.lock_identity = _identity("lock")
        self.snapshot = _Snapshot(self.lock_identity)
        self.prepared = PreparedLockedGeneration(
            self.project_root,
            self.project_root,
            None,
            None,
            (),
            None,
            (),
            None,
            _identity("catalog-audit"),
            self.snapshot,
        )
        revision = _identity("component")
        self.invalidation = ComponentInvalidationDecision(
            "adapter-proof",
            revision,
            ComponentChangeSurface.LOCAL_AUTHORITY,
            (revision,),
            (revision,),
            (revision,),
        )

    def test_automatic_parallelism_uses_admitted_slots_and_explicit_jobs_caps_them(
        self,
    ):
        from literate_ai.adapters.action_admission import CommandActionWorkerPool
        from literate_ai.adapters.action_dispatch_wire import ActionWireError

        planned = PlannedStandardProject(
            SimpleNamespace(require_unchanged=lambda: None),
            SimpleNamespace(identity=_identity("execution-plan")),
        )
        for requested, slots, expected in (
            (None, None, 1),
            (3, None, 3),
            (None, (2, 3), 5),
            (2, (2, 3), 2),
            (12, (2, 3), 5),
            (None, (2, 3), "expired"),
        ):
            with self.subTest(requested=requested, slots=slots):
                pool = None
                if slots is not None:
                    pool = mock.Mock(spec=CommandActionWorkerPool)
                    pool.workers = tuple(
                        SimpleNamespace(slots=count) for count in slots
                    )
                    pool.deadline = mock.Mock()
                    pool.identity = _identity("admitted-pool")
                    if expected == "expired":
                        pool.deadline.remaining.side_effect = ActionWireError(
                            "action_wire.expired", "admission expired"
                        )
                adapter = FilesystemStandardRebuildAdapter(
                    project=self.project,
                    binding=self.binding,
                    runtime=self.runtime,
                    source_cache_configuration=self.source_cache_configuration,
                    authority_validator=lambda _root: _identity("project-authority"),
                    action_workers=pool,
                )
                with (
                    mock.patch.object(
                        FilesystemStandardProjectRuntime, "plan", return_value=planned
                    ),
                    mock.patch.object(
                        FilesystemStandardProjectRuntime,
                        "production_readiness",
                        return_value=StandardProjectRuntimeReadiness(True, ()),
                    ),
                    mock.patch.object(
                        FilesystemStandardProjectRuntime,
                        "execute",
                        side_effect=RuntimeError("captured execution"),
                    ) as execute,
                    self.assertRaisesRegex(
                        FilesystemStandardRebuildError
                        if expected == "expired"
                        else RuntimeError,
                        "admission expired"
                        if expected == "expired"
                        else "captured execution",
                    ),
                ):
                    adapter.rebuild(
                        FilesystemStandardRebuildRequest(
                            self.prepared,
                            self.source_root,
                            self.invalidation,
                            max_parallelism=requested,
                        )
                    )
                if expected == "expired":
                    execute.assert_not_called()
                else:
                    self.assertEqual(
                        execute.call_args.args[1].max_parallelism, expected
                    )
                if pool is not None:
                    pool.deadline.remaining.assert_called_once_with()

    def test_direct_adapter_guards_authority_executes_and_projects_receipt(self):
        planned = PlannedStandardProject(
            SimpleNamespace(require_unchanged=lambda: None),
            SimpleNamespace(identity=_identity("execution-plan")),
        )
        lifecycle = SimpleNamespace(successful=True)
        executed = SimpleNamespace(lifecycle=lifecycle)
        validated = _identity("project-authority")
        projected: list[ProjectTestReceipt] = []

        def project_receipt(_lifecycle, **context):
            receipt = ProjectTestReceipt(
                project_id="adapter-proof",
                project_revision_identity=context["project_revision_identity"],
                subject_identity=_identity("subject"),
                suite=VersionedContentRef(
                    "test-suite", "standard", "1.0.0", _identity("suite")
                ),
                outcome="passed",
                summary=ProjectTestSummary(1, 1, 0, 0),
                result_identity=_identity("result"),
                evidence=(
                    ProjectTestEvidence(
                        "lifecycle-command",
                        context["lifecycle_invocation_identity"],
                    ),
                    ProjectTestEvidence(
                        "lifecycle-request",
                        context["lifecycle_request_identity"],
                    ),
                    ProjectTestEvidence(
                        "source-cache-decision", _identity("cache-decision")
                    ),
                    ProjectTestEvidence(
                        "source-cache-lifecycle", _identity("cache-lifecycle")
                    ),
                ),
            )
            projected.append(receipt)
            return receipt

        adapter = FilesystemStandardRebuildAdapter(
            project=self.project,
            binding=self.binding,
            runtime=self.runtime,
            source_cache_configuration=self.source_cache_configuration,
            authority_validator=lambda _root: validated,
        )
        with (
            mock.patch.object(
                FilesystemStandardProjectRuntime,
                "plan",
                return_value=planned,
            ),
            mock.patch.object(
                FilesystemStandardProjectRuntime,
                "production_readiness",
                return_value=StandardProjectRuntimeReadiness(True, ()),
            ),
            mock.patch.object(
                FilesystemStandardProjectRuntime,
                "execute",
                return_value=executed,
            ) as execute,
            mock.patch(
                "literate_ai.adapters.standard_rebuild."
                "project_standard_project_test_receipt",
                side_effect=project_receipt,
            ) as project_receipt,
        ):
            result = adapter.rebuild(
                FilesystemStandardRebuildRequest(
                    self.prepared,
                    self.source_root,
                    self.invalidation,
                    max_parallelism=3,
                )
            )

        receipt = projected[0]
        self.assertIs(result.execution, executed)
        self.assertIs(result.receipt, receipt)
        self.assertIs(result.finalized_receipt.receipt, receipt)
        self.assertEqual(
            result.finalized_receipt.component_lock_identities,
            (self.lock_identity,),
        )
        self.assertIsNone(result.receipt_update)
        self.assertGreaterEqual(self.snapshot.calls, 2)
        execute.assert_called_once()
        request = execute.call_args.args[1]
        self.assertEqual(request.source_root, self.source_root)
        self.assertEqual(request.max_parallelism, 3)
        project_receipt.assert_called_once()
        call = project_receipt.call_args
        self.assertEqual(call.args, (lifecycle,))
        self.assertEqual(
            call.kwargs,
            {
                "project_id": "adapter-proof",
                "project_revision_identity": result.project_revision_identity,
                "lifecycle_policy": self.binding.policy,
                "receipt_policy": self.project.definition.test_receipt_policy,
                "lifecycle_request_identity": result.lifecycle_request_identity,
                "lifecycle_invocation_identity": (result.lifecycle_invocation_identity),
                "runner_identity": self.binding.driver.identity,
            },
        )

    def test_adapter_rejects_runtime_inside_project_before_execution(self):
        adapter = FilesystemStandardRebuildAdapter(
            project=self.project,
            binding=self.binding,
            runtime=self.runtime,
            source_cache_configuration=self.source_cache_configuration,
            authority_validator=lambda _root: _identity("project-authority"),
        )
        with self.assertRaisesRegex(
            FilesystemStandardRebuildError,
            "outside project authority",
        ):
            adapter.rebuild(
                FilesystemStandardRebuildRequest(
                    self.prepared,
                    self.project_root / "generated",
                    self.invalidation,
                )
            )

    def test_adapter_reports_exact_typed_lifecycle_failure(self):
        planned = PlannedStandardProject(
            SimpleNamespace(require_unchanged=lambda: None),
            SimpleNamespace(identity=_identity("execution-plan")),
        )
        component = _identity("failed-component")
        failure = SimpleNamespace(
            phase=SimpleNamespace(value="test"),
            code="test.failed",
            component_revision=component,
            identity=_identity("failure-evidence"),
        )
        executed = SimpleNamespace(
            lifecycle=SimpleNamespace(
                successful=False,
                node_results=(SimpleNamespace(failure_evidence=failure),),
            )
        )
        adapter = FilesystemStandardRebuildAdapter(
            project=self.project,
            binding=self.binding,
            runtime=self.runtime,
            source_cache_configuration=self.source_cache_configuration,
            authority_validator=lambda _root: _identity("project-authority"),
        )
        with (
            mock.patch.object(
                FilesystemStandardProjectRuntime,
                "plan",
                return_value=planned,
            ),
            mock.patch.object(
                FilesystemStandardProjectRuntime,
                "production_readiness",
                return_value=StandardProjectRuntimeReadiness(True, ()),
            ),
            mock.patch.object(
                FilesystemStandardProjectRuntime,
                "execute",
                return_value=executed,
            ),
            self.assertRaisesRegex(
                FilesystemStandardRebuildError,
                rf"test:test\.failed for {component.uri}",
            ),
        ):
            adapter.rebuild(
                FilesystemStandardRebuildRequest(
                    self.prepared,
                    self.source_root,
                    self.invalidation,
                )
            )

    def test_declared_cache_opens_only_the_write_target_writable(self):
        runtime_root = self.root / "runtime-cache"
        committed_root = self.project_root / "generated" / "committed-source-cache"
        configuration = SourceCacheConfiguration(
            SourceCacheMode.READ_WRITE,
            (
                SourceCacheTarget(
                    "standard-local",
                    root_kind=SourceCacheRootKind.OPERATOR_BOUND,
                    root_reference="standard-local-source-cache",
                ),
                SourceCacheTarget(
                    "project-committed",
                    root_kind=SourceCacheRootKind.PROJECT_RELATIVE,
                    root_reference="generated/committed-source-cache",
                ),
            ),
            write_target_id="standard-local",
            require_unique=True,
        )
        project = LoadedProject(
            self.project_root,
            SimpleNamespace(source_cache=configuration),
        )

        resolved, caches = _resolved_source_cache(project, runtime_root)

        self.assertIs(resolved, configuration)
        self.assertTrue(caches["standard-local"].writable)
        self.assertTrue(runtime_root.is_dir())
        self.assertFalse(caches["project-committed"].writable)
        self.assertFalse(caches["project-committed"].available)
        self.assertFalse(committed_root.exists())

    def test_read_only_runtime_cache_is_not_created(self):
        runtime_root = self.root / "runtime-cache"
        configuration = SourceCacheConfiguration(
            SourceCacheMode.READ_ONLY,
            (
                SourceCacheTarget(
                    "standard-local",
                    root_kind=SourceCacheRootKind.OPERATOR_BOUND,
                    root_reference="standard-local-source-cache",
                ),
            ),
            require_unique=True,
        )
        project = LoadedProject(
            self.project_root,
            SimpleNamespace(source_cache=configuration),
        )

        _resolved, caches = _resolved_source_cache(project, runtime_root)

        self.assertFalse(caches["standard-local"].writable)
        self.assertFalse(caches["standard-local"].available)
        self.assertFalse(runtime_root.exists())

    def test_declared_format_only_committed_cache_is_an_empty_miss(self):
        runtime_root = self.root / "runtime-cache"
        committed_root = self.project_root / "generated" / "committed-source-cache"
        committed_root.mkdir(parents=True)
        format_path = committed_root / "format.json"
        format_path.write_bytes(
            b'{"format":"filesystem-v2","schema":"literate-ai/source-cache-layout@2"}'
        )
        configuration = SourceCacheConfiguration(
            SourceCacheMode.READ_WRITE,
            (
                SourceCacheTarget(
                    "standard-local",
                    root_kind=SourceCacheRootKind.OPERATOR_BOUND,
                    root_reference="standard-local-source-cache",
                ),
                SourceCacheTarget(
                    "project-committed",
                    root_kind=SourceCacheRootKind.PROJECT_RELATIVE,
                    root_reference="generated/committed-source-cache",
                ),
            ),
            write_target_id="standard-local",
            require_unique=True,
        )
        project = LoadedProject(
            self.project_root,
            SimpleNamespace(source_cache=configuration),
        )

        _resolved, caches = _resolved_source_cache(project, runtime_root)

        self.assertTrue(caches["standard-local"].available)
        self.assertFalse(caches["project-committed"].available)
        self.assertEqual(tuple(committed_root.iterdir()), (format_path,))


if __name__ == "__main__":
    unittest.main()
