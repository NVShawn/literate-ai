from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.artifact_exports import (
    ArtifactEntrypointCommand,
    ArtifactExport,
    artifact_execution_identity,
)
from literate_ai.cli import build_run
from literate_ai.cli.source_to_specification import CliFailure
from literate_ai.contracts import (
    ContentReference,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    LifecycleDispatchAction,
    ObservedExecutionEnvironment,
    canonical_identity,
)


def _request(
    _args: object,
    *,
    component: str,
    selected: object,
    action: LifecycleDispatchAction,
    artifact_reference: ContentReference | None = None,
    application_arguments: tuple[str, ...] = (),
    entrypoint: str | None = None,
    **_: object,
) -> ExecutionDispatchRequest:
    worker = selected.worker
    identities = tuple(canonical_identity({"authority": index}) for index in range(7))
    accepted_source_only = bool(getattr(_args, "from_accepted_source", False))
    provider_identity = canonical_identity({"provider": "accepted-source"})
    return ExecutionDispatchRequest(
        action,
        component,
        component,
        worker.target_profile,
        (),
        worker.identity,
        worker.requirements,
        selected.parameters,
        application_arguments,
        *identities,
        artifact_reference,
        60,
        accepted_source_only=accepted_source_only,
        accepted_source_provider_id=(
            "accepted-source" if accepted_source_only else None
        ),
        accepted_source_provider_identity=(
            provider_identity if accepted_source_only else None
        ),
        entrypoint=entrypoint,
    )


class _Dispatcher:
    calls: list[ExecutionDispatchRequest] = []

    def __init__(self, *_: object) -> None:
        pass

    def dispatch(
        self,
        worker: ExecutionWorker,
        request: ExecutionDispatchRequest,
        *,
        cwd: Path,
    ) -> ExecutionDispatchResult:
        self.calls.append(request)
        artifact = request.artifact_reference
        if request.action in {
            LifecycleDispatchAction.BUILD,
            LifecycleDispatchAction.TEST,
        }:
            artifact = ContentReference(
                "artifact-export",
                "cas:sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                canonical_identity({"artifact": "demo"}),
            )
        return ExecutionDispatchResult(
            request.identity,
            worker.identity,
            "synthetic-task",
            DispatchResultStatus.PASSED,
            ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                16384,
                toolchain_identities=(canonical_identity({"tool": "python"}),),
            ),
            0,
            artifact,
            canonical_identity({"evidence": request.action.value}),
            (
                "remote-app-output\n"
                if request.action is LifecycleDispatchAction.RUN
                else ""
            ),
            "",
        )


class CommandWorkerLifecycleCliTests(unittest.TestCase):
    def test_local_run_selects_named_multi_entrypoint_command(self) -> None:
        worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        selected = SimpleNamespace(
            worker=worker,
            parameters=(),
            catalog_identity=canonical_identity({"catalog": "local"}),
        )
        build_request = _request(
            object(),
            component="components/demo",
            selected=selected,
            action=LifecycleDispatchAction.BUILD,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "artifact"
            artifact.mkdir()
            reference = ContentReference(
                "artifact-export",
                artifact.as_uri(),
                canonical_identity({"artifact": "multi"}),
            )
            commands = (
                ArtifactEntrypointCommand(
                    "api",
                    "portable-application",
                    "api-service",
                    (__import__("sys").executable, "-c", "print('api')"),
                    {},
                ),
                ArtifactEntrypointCommand(
                    "collector",
                    "portable-application",
                    "collector-service",
                    (__import__("sys").executable, "-c", "print('collector')"),
                    {},
                ),
            )
            export = ArtifactExport(
                "components/demo",
                "host",
                worker.worker_id,
                worker.identity,
                "local",
                artifact,
                reference,
                build_request.authority_identity,
                commands[0].argv,
                {},
                commands,
                "api",
                artifact_execution_identity(reference, commands, "api"),
                "gpt-5.6-sol",
            )
            args = Namespace(
                project=str(root),
                component="components/demo",
                target="host",
                entrypoint="collector",
                arguments=[],
            )
            project = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(source_intelligence=object()),
            )
            reconstructed_models: list[str | None] = []

            def reconstructed_request(args: object, **kwargs: object):
                reconstructed_models.append(getattr(args, "model", None))
                return _request(args, **kwargs)

            with (
                patch("literate_ai.cli.build_run._project", return_value=project),
                patch(
                    "literate_ai.cli.build_run.select_execution_worker",
                    return_value=selected,
                ),
                patch(
                    "literate_ai.cli.build_run.discover_project", return_value=project
                ),
                patch(
                    "literate_ai.cli.build_run.require_lifecycle_project_index",
                    return_value={"database_identity": "sha256:" + "f" * 64},
                ),
                patch(
                    "literate_ai.cli.build_run.load_artifact_export",
                    return_value=export,
                ),
                patch(
                    "literate_ai.cli.build_run.create_execution_dispatch_request",
                    side_effect=reconstructed_request,
                ),
                patch(
                    "literate_ai.cli.build_run._demonstration_arguments",
                    return_value=([], "none"),
                ),
            ):
                ran, status = build_run.run_from_args(args)

        self.assertEqual(status, 0)
        self.assertEqual(ran["stdout"], "collector\n")
        self.assertEqual(ran["entrypoint"], "collector")
        self.assertEqual(ran["deployment_unit"], "collector-service")
        self.assertEqual(reconstructed_models, ["gpt-5.6-sol"])

    def test_build_test_run_share_worker_authority_and_remote_artifact(self) -> None:
        worker = ExecutionWorker(
            "fleet",
            ExecutionWorkerKind.COMMAND,
            command=("dispatcher",),
        )
        catalog = ExecutionWorkerCatalog((worker,))
        _Dispatcher.calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workers = root / "workers.json"
            workers.write_text(
                __import__("json").dumps(catalog.to_dict()), encoding="utf-8"
            )
            args = Namespace(
                project=str(root),
                component="components/demo",
                target="host",
                worker="fleet",
                worker_param=[],
                worker_config=str(workers),
                worker_timeout_seconds=60,
                flavor=[],
                arguments=["customer-input"],
                from_accepted_source=True,
            )
            project = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(source_intelligence=object()),
            )
            with (
                patch("literate_ai.cli.build_run._project_root", return_value=root),
                patch(
                    "literate_ai.cli.build_run.create_execution_dispatch_request",
                    side_effect=_request,
                ),
                patch(
                    "literate_ai.cli.build_run.CommandExecutionDispatcher",
                    _Dispatcher,
                ),
                patch(
                    "literate_ai.cli.build_run.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.cli.build_run.require_lifecycle_project_index",
                    return_value={"database_identity": "sha256:" + "f" * 64},
                ),
            ):
                built = build_run.build_from_args(args)
                tested = build_run.test_from_args(args)
                args.component = "demo"
                # ``run`` has no public accepted-source flag. It must reconstruct
                # this non-default authority input from the retained export.
                args.from_accepted_source = False
                ran, exit_status = build_run.run_from_args(args)

        self.assertTrue(built["passed"])
        self.assertTrue(tested["passed"])
        self.assertEqual(exit_status, 0)
        self.assertEqual(ran["stdout"], "remote-app-output\n")
        self.assertEqual(
            [request.action for request in _Dispatcher.calls],
            [
                LifecycleDispatchAction.BUILD,
                LifecycleDispatchAction.TEST,
                LifecycleDispatchAction.RUN,
            ],
        )
        self.assertIsNone(_Dispatcher.calls[0].artifact_reference)
        self.assertIsNone(_Dispatcher.calls[1].artifact_reference)
        self.assertIsNotNone(_Dispatcher.calls[2].artifact_reference)
        self.assertEqual(_Dispatcher.calls[2].component, "components/demo")
        self.assertEqual(_Dispatcher.calls[2].arguments, ("customer-input",))
        self.assertTrue(_Dispatcher.calls[2].accepted_source_only)
        self.assertEqual(
            _Dispatcher.calls[1].authority_identity,
            _Dispatcher.calls[2].authority_identity,
        )
        self.assertIsNone(built["coverage_gaps"])

    def test_build_and_test_forward_source_cache_entry_to_the_local_rebuild(
        self,
    ) -> None:
        # litai test had no way to disambiguate a cache key with multiple
        # accepted entries, unlike litai rebuild's --source-cache-entry
        # (see issue #54); build/test share the same underlying rebuild call.
        captured: list[object] = []

        def fake_rebuild_from_args(request: object) -> dict[str, object]:
            captured.append(request)
            return {
                "runtime_root": None,
                "artifact": "/tmp/artifact",
                "execution_command": {},
                "component": "components/demo",
                "target_profile": "host",
                "execution_worker": {},
                "passed": True,
                "test_summary": None,
                "project_source_intelligence": {},
            }

        provider_identity = canonical_identity(
            {"provider": "published-inherited-session"}
        )

        def accepted_request(
            *args: object, **kwargs: object
        ) -> ExecutionDispatchRequest:
            return replace(
                _request(*args, **kwargs),
                accepted_source_only=True,
                accepted_source_provider_id="inherited-session",
                accepted_source_provider_identity=provider_identity,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = Namespace(
                project=str(root),
                component="components/demo",
                target="host",
                worker=None,
                worker_param=[],
                flavor=[],
                source_cache_entry=["sha256:" + "a" * 64],
                force_regeneration=False,
                model=None,
                jobs=1,
                keep_runtime=False,
                update_receipt=False,
                from_accepted_source=True,
            )
            project = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(source_intelligence=object()),
            )
            with (
                patch("literate_ai.cli.build_run._project_root", return_value=root),
                patch(
                    "literate_ai.cli.build_run.create_execution_dispatch_request",
                    side_effect=accepted_request,
                ),
                patch(
                    "literate_ai.cli.build_run.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.cli.build_run.require_lifecycle_project_index",
                    return_value={"database_identity": "sha256:" + "f" * 64},
                ),
                patch(
                    "literate_ai.cli.rebuild.rebuild_from_args",
                    side_effect=fake_rebuild_from_args,
                ),
                patch(
                    "literate_ai.cli.build_run.record_artifact_export",
                    return_value=SimpleNamespace(
                        artifact="/tmp/exported-artifact",
                        component="components/demo",
                        artifact_reference=SimpleNamespace(
                            uri="cas:demo", identity=SimpleNamespace(uri="sha256:demo")
                        ),
                    ),
                ),
            ):
                build_run.build_from_args(args)
                build_run.test_from_args(args)

        self.assertEqual(len(captured), 2)
        for request in captured:
            self.assertEqual(request.source_cache_entry, ["sha256:" + "a" * 64])
            self.assertTrue(request.from_accepted_source)
            self.assertEqual(request.accepted_source_provider_id, "inherited-session")
            self.assertEqual(
                request.accepted_source_provider_identity, provider_identity
            )

    def test_ssh_build_test_run_use_the_same_public_lifecycle(self) -> None:
        worker = ExecutionWorker(
            "ssh",
            ExecutionWorkerKind.SSH,
            endpoint="user@host",
            workspace="~/literate-ai",
        )
        catalog = ExecutionWorkerCatalog((worker,))
        _Dispatcher.calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workers = root / "workers.json"
            workers.write_text(
                __import__("json").dumps(catalog.to_dict()), encoding="utf-8"
            )
            args = Namespace(
                project=str(root),
                component="components/demo",
                target="host",
                worker="ssh",
                worker_param=[],
                worker_config=str(workers),
                worker_timeout_seconds=60,
                flavor=[],
                arguments=["input"],
            )
            project = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(source_intelligence=object()),
            )
            with (
                patch("literate_ai.cli.build_run._project_root", return_value=root),
                patch(
                    "literate_ai.cli.build_run.create_execution_dispatch_request",
                    side_effect=_request,
                ),
                patch("literate_ai.cli.build_run.SshExecutionDispatcher", _Dispatcher),
                patch("literate_ai.cli.build_run.SshLifecycleRequestHandler"),
                patch(
                    "literate_ai.cli.build_run.discover_project", return_value=project
                ),
                patch(
                    "literate_ai.cli.build_run.require_lifecycle_project_index",
                    return_value={"database_identity": "sha256:" + "f" * 64},
                ),
            ):
                built = build_run.build_from_args(args)
                tested = build_run.test_from_args(args)
                args.component = "demo"
                ran, exit_status = build_run.run_from_args(args)

        self.assertTrue(built["passed"])
        self.assertTrue(tested["passed"])
        self.assertEqual(exit_status, 0)
        self.assertEqual(ran["stdout"], "remote-app-output\n")
        self.assertEqual(
            [item.action for item in _Dispatcher.calls],
            [
                LifecycleDispatchAction.BUILD,
                LifecycleDispatchAction.TEST,
                LifecycleDispatchAction.RUN,
            ],
        )


class CommandWorkerCoverageGapTests(unittest.TestCase):
    def _worker_args(self, root: Path) -> Namespace:
        worker = ExecutionWorker(
            "fleet",
            ExecutionWorkerKind.COMMAND,
            command=("dispatcher",),
        )
        catalog = ExecutionWorkerCatalog((worker,))
        workers = root / "workers.json"
        workers.write_text(
            __import__("json").dumps(catalog.to_dict()), encoding="utf-8"
        )
        return Namespace(
            project=str(root),
            component="components/demo",
            target="host",
            worker="fleet",
            worker_param=[],
            worker_config=str(workers),
            worker_timeout_seconds=60,
            flavor=[],
            arguments=[],
        )

    def test_worker_build_returns_coverage_gaps_and_fails_closed(self) -> None:
        from literate_ai.adapters.coverage_gaps import find_coverage_gaps
        from literate_ai.contracts import Entrypoint

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "stub"
            sources.mkdir()
            (sources / "server.py").write_text(
                "ROUTES = {'/health': None}\n", encoding="utf-8"
            )
            payload = find_coverage_gaps(
                (Entrypoint("api", "portable-application", "run"),),
                sources,
            ).to_dict()

            class _BlockingDispatcher:
                def dispatch(self, worker, request, *, cwd):  # noqa: ANN001
                    artifact = ContentReference(
                        "artifact-export",
                        "cas:sha256:" + "a" * 64,
                        canonical_identity({"artifact": "demo"}),
                    )
                    return ExecutionDispatchResult(
                        request.identity,
                        worker.identity,
                        "synthetic-task",
                        DispatchResultStatus.PASSED,
                        ObservedExecutionEnvironment(
                            "linux",
                            "24.04",
                            "x86_64",
                            8,
                            16384,
                            toolchain_identities=(canonical_identity({"tool": "py"}),),
                        ),
                        0,
                        artifact,
                        canonical_identity({"evidence": "build"}),
                        coverage_gaps=payload,
                    )

            args = self._worker_args(root)
            project = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(source_intelligence=object()),
            )
            with (
                patch("literate_ai.cli.build_run._project_root", return_value=root),
                patch(
                    "literate_ai.cli.build_run.create_execution_dispatch_request",
                    side_effect=_request,
                ),
                patch(
                    "literate_ai.cli.build_run.CommandExecutionDispatcher",
                    lambda *_: _BlockingDispatcher(),
                ),
                patch(
                    "literate_ai.cli.build_run.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.cli.build_run.require_lifecycle_project_index",
                    return_value={"database_identity": "sha256:" + "f" * 64},
                ),
                patch(
                    "literate_ai.cli.build_run.record_remote_artifact_export"
                ) as export,
            ):
                with self.assertRaises(CliFailure) as raised:
                    build_run.build_from_args(args)
            self.assertEqual(raised.exception.code, "build.unimplemented_surface")
            export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
