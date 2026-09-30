from __future__ import annotations

import os
import tempfile
import unittest
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.cli.dispatch import _parser
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.execution_workers import (
    add_execution_worker_arguments,
    create_execution_dispatch_request,
    select_execution_worker,
    validate_worker_accelerator_flavors,
    validate_worker_platform_flavors,
)
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    ExecutionWorkerParameter,
    GpuRequirement,
    LifecycleDispatchAction,
    NvidiaProbeStatus,
    ObservedGpuDevice,
    WorkerHardwareObservation,
    WorkerHardwareObservationCatalog,
    canonical_identity,
    canonical_json_bytes,
)


def write_catalog(path: Path, *workers: ExecutionWorker) -> None:
    path.write_bytes(
        canonical_json_bytes(ExecutionWorkerCatalog(tuple(workers)).to_dict())
    )


class ExecutionWorkerCliTests(unittest.TestCase):
    def test_cuda_flavor_requires_current_healthy_matching_observation(self) -> None:
        @dataclass(frozen=True)
        class Flavor:
            axis: str
            value: str

        worker = ExecutionWorker(
            "gpu",
            ExecutionWorkerKind.SSH,
            requirements=ExecutionRequirements(
                os_family="linux",
                gpu=GpuRequirement(
                    vendor="nvidia",
                    minimum_count=1,
                    minimum_memory_mib=16000,
                    capabilities=("cuda", "sm-89"),
                ),
            ),
            endpoint="user@gpu-host",
            workspace="~/literate-ai",
        )
        selected = select_execution_worker(
            Namespace(worker=None, worker_param=[], worker_config=None),
            project_root=Path.cwd(),
            target_profile="host",
        ).__class__(
            worker, (), canonical_identity({"catalog": "test"}), Path("workers.json")
        )
        cuda = (Flavor("accelerator", "nvidia-cuda"),)

        def catalog(
            status: NvidiaProbeStatus, *, memory: int = 24576
        ) -> WorkerHardwareObservationCatalog:
            devices = (
                (
                    ObservedGpuDevice(
                        "nvidia", "RTX", 0, "GPU-1", memory, "8.9", "580.1"
                    ),
                )
                if status is NvidiaProbeStatus.OK
                else ()
            )
            return WorkerHardwareObservationCatalog(
                (
                    WorkerHardwareObservation(
                        "gpu",
                        __import__("datetime")
                        .datetime.now(__import__("datetime").UTC)
                        .isoformat(),
                        "linux",
                        "ubuntu",
                        "24.04",
                        "x86_64",
                        8,
                        16,
                        32768,
                        devices,
                        status,
                        "driver failed"
                        if status is NvidiaProbeStatus.DEGRADED
                        else None,
                    ),
                )
            )

        with mock.patch(
            "literate_ai.adapters.worker_capabilities.load_worker_observations",
            return_value=catalog(NvidiaProbeStatus.OK),
        ):
            validate_worker_accelerator_flavors(selected, cuda, project_root=Path.cwd())
        for status in (NvidiaProbeStatus.ABSENT, NvidiaProbeStatus.DEGRADED):
            with (
                self.subTest(status=status),
                mock.patch(
                    "literate_ai.adapters.worker_capabilities.load_worker_observations",
                    return_value=catalog(status),
                ),
                self.assertRaises(CliFailure) as raised,
            ):
                validate_worker_accelerator_flavors(
                    selected, cuda, project_root=Path.cwd()
                )
            self.assertEqual(
                raised.exception.code, "execution.worker_accelerator_ineligible"
            )
        with (
            mock.patch(
                "literate_ai.adapters.worker_capabilities.load_worker_observations",
                return_value=catalog(NvidiaProbeStatus.OK, memory=8192),
            ),
            self.assertRaises(CliFailure) as insufficient,
        ):
            validate_worker_accelerator_flavors(selected, cuda, project_root=Path.cwd())
        self.assertEqual(
            insufficient.exception.code,
            "execution.worker_accelerator_requirements_unsatisfied",
        )

    def test_cpu_flavor_never_reads_accelerator_observations(self) -> None:
        selected = select_execution_worker(
            Namespace(worker=None, worker_param=[], worker_config=None),
            project_root=Path.cwd(),
            target_profile="host",
        )
        flavor = SimpleNamespace(axis="accelerator", value="cpu")
        with mock.patch(
            "literate_ai.adapters.worker_capabilities.load_worker_observations"
        ) as loaded:
            validate_worker_accelerator_flavors(
                selected, (flavor,), project_root=Path.cwd()
            )
        loaded.assert_not_called()

    def test_worker_os_requirement_must_match_locked_platform_flavor(self) -> None:
        @dataclass(frozen=True)
        class Flavor:
            axis: str
            value: str

        worker = ExecutionWorker(
            "linux",
            ExecutionWorkerKind.SSH,
            requirements=ExecutionRequirements(os_family="linux"),
            endpoint="user@host",
            workspace="~/literate-ai",
        )
        selected = select_execution_worker(
            Namespace(worker=None, worker_param=[], worker_config=None),
            project_root=Path.cwd(),
            target_profile="host",
        )
        selected = selected.__class__(worker, (), selected.catalog_identity, None)

        validate_worker_platform_flavors(selected, (Flavor("platform.os", "linux"),))
        with self.assertRaises(CliFailure) as raised:
            validate_worker_platform_flavors(
                selected, (Flavor("platform.os", "windows"),)
            )
        self.assertEqual(
            raised.exception.code, "execution.worker_platform_flavor_mismatch"
        )

    def test_lifecycle_verbs_share_worker_grammar_and_distinct_target(self) -> None:
        parser = _parser()
        for verb in ("build", "test", "run"):
            with self.subTest(verb=verb):
                parsed = parser.parse_args(
                    [
                        verb,
                        "components/demo",
                        "--target",
                        "linux-host",
                        "--worker",
                        "fleet",
                        "--worker-param",
                        "queue=batch",
                        "--worker-config",
                        "/private/workers.json",
                        "--worker-timeout-seconds",
                        "90",
                    ]
                )
                self.assertEqual(parsed.target, "linux-host")
                self.assertEqual(parsed.worker, "fleet")
                self.assertEqual(parsed.worker_param, ["queue=batch"])
                self.assertEqual(parsed.worker_config, "/private/workers.json")
                self.assertEqual(parsed.worker_timeout_seconds, 90)

    def test_parser_exposes_worker_without_overloading_target(self) -> None:
        parser = ArgumentParser()
        parser.add_argument("--target", default="host")
        add_execution_worker_arguments(parser)

        parsed = parser.parse_args(
            [
                "--target",
                "linux-host",
                "--worker",
                "fleet",
                "--worker-param",
                "queue=batch",
            ]
        )

        self.assertEqual(parsed.target, "linux-host")
        self.assertEqual(parsed.worker, "fleet")
        self.assertEqual(parsed.worker_param, ["queue=batch"])

    def test_omitted_worker_selects_only_an_implicit_local_worker(self) -> None:
        selected = select_execution_worker(
            Namespace(worker=None, worker_param=[], worker_config=None),
            project_root=Path.cwd(),
            target_profile="macos-host",
        )

        self.assertTrue(selected.implicit_local)
        self.assertEqual(selected.worker.kind, ExecutionWorkerKind.LOCAL)
        self.assertEqual(selected.worker.target_profile, "macos-host")
        self.assertEqual(selected.parameters, ())

    def test_exact_worker_and_declared_parameters_resolve_from_private_catalog(
        self,
    ) -> None:
        worker = ExecutionWorker(
            "fleet",
            ExecutionWorkerKind.COMMAND,
            target_profile="linux-host",
            parameters=(
                ExecutionWorkerParameter(
                    "queue", ("batch", "interactive"), default="batch"
                ),
            ),
            command=("dispatcher",),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "private-workers.json"
            write_catalog(configured, worker)
            selected = select_execution_worker(
                Namespace(
                    worker="fleet",
                    worker_param=["queue=interactive"],
                    worker_config=str(configured),
                ),
                project_root=root,
                target_profile="linux-host",
            )

        self.assertEqual(selected.worker, worker)
        self.assertEqual(selected.parameters, (("queue", "interactive"),))
        self.assertEqual(
            selected.catalog_identity, ExecutionWorkerCatalog((worker,)).identity
        )
        self.assertFalse(selected.implicit_local)

    def test_environment_configuration_and_default_parameter_is_supported(self) -> None:
        worker = ExecutionWorker(
            "remote",
            ExecutionWorkerKind.COMMAND,
            parameters=(ExecutionWorkerParameter("tier", ("small",), default="small"),),
            command=("dispatcher",),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "workers.json"
            write_catalog(configured, worker)
            with mock.patch.dict(
                os.environ, {"LITAI_WORKER_CONFIG": str(configured)}, clear=False
            ):
                selected = select_execution_worker(
                    Namespace(worker="remote", worker_param=[], worker_config=None),
                    project_root=root,
                    target_profile="host",
                )

        self.assertEqual(selected.parameters, (("tier", "small"),))

    def test_parameter_value_and_target_mismatch_fail_closed(self) -> None:
        worker = ExecutionWorker(
            "fleet",
            ExecutionWorkerKind.COMMAND,
            target_profile="linux-host",
            parameters=(ExecutionWorkerParameter("queue", ("batch",)),),
            command=("dispatcher",),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "workers.json"
            write_catalog(configured, worker)
            base = {
                "worker": "fleet",
                "worker_config": str(configured),
            }
            for supplied in (["unknown=value"], ["queue=interactive"]):
                with self.subTest(supplied=supplied):
                    with self.assertRaises(CliFailure):
                        select_execution_worker(
                            Namespace(**base, worker_param=supplied),
                            project_root=root,
                            target_profile="linux-host",
                        )
            with self.assertRaises(CliFailure) as raised:
                select_execution_worker(
                    Namespace(**base, worker_param=[]),
                    project_root=root,
                    target_profile="macos-host",
                )
        self.assertEqual(
            raised.exception.code, "execution.worker_target_profile_mismatch"
        )

    def test_parameters_without_worker_and_malformed_pairs_are_rejected(self) -> None:
        with self.assertRaises(CliFailure) as raised:
            select_execution_worker(
                Namespace(
                    worker=None,
                    worker_param=["queue=batch"],
                    worker_config=None,
                ),
                project_root=Path.cwd(),
                target_profile="host",
            )
        self.assertEqual(raised.exception.code, "execution.worker_required")

        worker = ExecutionWorker(
            "fleet", ExecutionWorkerKind.COMMAND, command=("dispatcher",)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "workers.json"
            write_catalog(configured, worker)
            with self.assertRaises(CliFailure) as malformed:
                select_execution_worker(
                    Namespace(
                        worker="fleet",
                        worker_param=["missing-separator"],
                        worker_config=str(configured),
                    ),
                    project_root=root,
                    target_profile="host",
                )
        self.assertEqual(malformed.exception.code, "execution.worker_parameter_invalid")

    def test_dispatch_request_factory_binds_current_plan_and_source_index(self) -> None:
        root = Path.cwd()
        selected = select_execution_worker(
            Namespace(worker=None, worker_param=[], worker_config=None),
            project_root=root,
            target_profile="host",
        )
        project_identity = canonical_identity({"project": "demo"})
        source_identity = canonical_identity({"source": "demo"})
        specification_identity = canonical_identity({"specification": "demo"})
        source_index_identity = canonical_identity({"source-index": "demo"})
        plan = {
            "project": {"identity": project_identity.uri},
            "component": {"coordinate": "component://example/demo"},
            "resolution": {"root_revision_identity": specification_identity.uri},
            "input_closure": {"identity": source_identity.uri},
            "selected_flavors": [
                {
                    "id": "host-os",
                    "axis": "platform.os",
                    "value": "host",
                    "revision_identity": specification_identity.uri,
                }
            ],
            "toolchain_constraints": [{"name": "python", "version": "3"}],
            "model_scopes": [
                {
                    "component_revision": specification_identity.uri,
                    "binding": {"model_selector": "pipeline-model"},
                }
            ],
        }
        project = SimpleNamespace(
            root=root,
            definition=SimpleNamespace(source_intelligence=object()),
            flavor_selectors_for=lambda component, explicit=(): explicit,
        )
        args = Namespace(
            target="host",
            flavor=[],
            model="pipeline-model",
            verbose=True,
            worker_timeout_seconds=90,
            jobs=2,
            from_accepted_source=True,
        )
        provider_identity = canonical_identity({"provider": "inherited-session"})
        with (
            mock.patch("literate_ai.projects.discover_project", return_value=project),
            mock.patch("literate_ai.cli.generation.plan_from_args", return_value=plan),
            mock.patch(
                "literate_ai.project_source_index.require_lifecycle_project_index",
                return_value={"database_identity": source_index_identity.uri},
            ),
            mock.patch(
                "literate_ai.cli.execution_workers."
                "discover_accepted_source_provider_binding",
                return_value=("inherited-session", provider_identity),
            ),
            mock.patch.dict(
                os.environ,
                {
                    "LITAI_CODING_PROVIDER": "inherited-session",
                    "LITAI_INHERITED_SESSION_PROVIDER_IDENTITY": (
                        provider_identity.uri
                    ),
                    "LITAI_INHERITED_SESSION_IDENTITY": canonical_identity(
                        {"session": "fresh-workspace"}
                    ).uri,
                    "LITAI_INHERITED_SESSION_AUTH_KEY_ID": "fresh-workspace-key",
                    "LITAI_INHERITED_SESSION_TIMEOUT_SECONDS": "90",
                },
            ),
        ):
            request = create_execution_dispatch_request(
                args,
                project_root=root,
                component="components/demo",
                selected=selected,
                action=LifecycleDispatchAction.BUILD,
            )

        self.assertEqual(request.project_identity, project_identity)
        self.assertEqual(request.source_identity, source_identity)
        self.assertEqual(request.specification_identity, specification_identity)
        self.assertEqual(request.source_index_identity, source_index_identity)
        self.assertEqual(
            request.model_scope_identity,
            canonical_identity(
                {
                    "schema": "literate-ai/model-scope-set@1",
                    "bindings": plan["model_scopes"],
                }
            ),
        )
        self.assertEqual(request.timeout_seconds, 90)
        self.assertEqual(request.jobs, 2)
        self.assertEqual(request.model_selector, "pipeline-model")
        self.assertTrue(request.verbose)
        self.assertIsNone(request.artifact_reference)
        self.assertTrue(request.accepted_source_only)
        self.assertEqual(
            request.accepted_source_provider_id,
            "inherited-session",
        )
        self.assertEqual(
            request.accepted_source_provider_identity,
            provider_identity,
        )


if __name__ == "__main__":
    unittest.main()
