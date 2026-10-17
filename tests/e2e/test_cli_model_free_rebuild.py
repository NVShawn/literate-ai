"""`litai rebuild` of a fresh starter project with only the model provider faked.

The fake `claude` executable stands at the model-provider boundary and writes the
source a model would. Initialization, locking, source admission, Make build,
generated tests, execution, independent acceptance and receipt commit are real.
An editable checkout cannot pin a Standard lifecycle distribution, so the test
supplies a synthetic distribution through the same seams installed wheels use.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    InstalledFrameworkDistribution,
    InstalledFrameworkDistributionMember,
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.cli import main
from literate_ai.contracts import (
    ProjectInitializationOrigin,
    ProjectTestReceiptPolicy,
    RepositoryParentSelection,
    StandardProjectLifecycleDriver,
    load_current_standard_lifecycle_policy,
)
from tests.support.loader_environment import without_empty_loader_entries

FAKE_CLAUDE = Path(__file__).resolve().parents[1] / "support" / "fake_claude.py"


def install_fake_claude(directory: Path) -> Path:
    """Copy the fake as a regular executable named `claude`; never a symlink."""
    path = directory / "claude"
    path.write_text(
        f"#!{sys.executable}\n" + FAKE_CLAUDE.read_text("utf-8"), encoding="utf-8"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@unittest.skipUnless(
    shutil.which("make") and os.name == "posix",
    "the starter Make build needs GNU make on a POSIX host",
)
class ModelFreeRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.distribution = InstalledFrameworkDistribution(
            "literate-ai",
            "1.1.0",
            (
                InstalledFrameworkDistributionMember(
                    "literate_ai/__init__.py", 1, "sha256:" + "a" * 64
                ),
            ),
        )
        policy = load_current_standard_lifecycle_policy()
        driver = StandardProjectLifecycleDriver(
            self.distribution.identity, policy.identity
        )
        receipt = ProjectTestReceiptPolicy(
            policy.policy_id,
            policy.policy_version,
            driver.identity,
            policy.required_evidence_kinds,
            policy.minimum_test_count,
        )
        self.project = self.root / "project"
        self.project.mkdir()
        FilesystemProjectInitializationAdapter(
            standard_binding_provider=lambda: (driver, receipt),
            initialization_origin_provider=lambda: ProjectInitializationOrigin(
                repository_url="https://github.com/jordanhubbard/literate-ai.git",
                git_revision="a" * 40,
                distribution_name="literate-ai",
                distribution_version="1.1.0",
            ),
        ).initialize(
            self.project,
            parent_selection=RepositoryParentSelection.root(),
            source_intelligence_provider="none",
        )
        tools = self.root / "tools"
        tools.mkdir()
        install_fake_claude(tools)
        self.environment = {
            "CODING_CLI": "claude",
            "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
            "BUILD_DIR": str(self.root / "build"),
            "OBJ_DIR": str(self.root / "obj"),
        }

    def rebuild(self, *extra: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with (
            patch.dict(os.environ, self.environment),
            patch(
                "literate_ai.cli.rebuild.resolve_standard_project_lifecycle_driver",
                lambda driver: resolve_standard_project_lifecycle_driver(
                    driver, distribution_observer=lambda: self.distribution
                ),
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            status = main(
                [
                    "--json",
                    "rebuild",
                    "samples/hello-component",
                    "--project",
                    str(self.project),
                    "--allow-host-execution",
                    "--update-receipt",
                    *extra,
                ]
            )
        return status, json.loads(output.getvalue())

    def test_starter_rebuilds_and_commits_a_receipt_without_a_model(self):
        status, envelope = self.rebuild()
        self.assertEqual(status, 0, envelope)
        self.assertTrue(envelope["ok"], envelope)
        result = envelope["result"]
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["receipt_committed"], result)

    def admit_worker(self, command, *, finalize=None):
        """Register one private command worker the CLI must admit and use.

        `command(cas, workspace)` returns the worker argv. The operator-owned
        state written here (catalog, hardware observation, storage health and
        action-execution configuration) is what a deployment provisions.
        """
        from datetime import UTC, datetime

        from literate_ai import worker_storage_probe
        from literate_ai.contracts.execution_dispatch import (
            LIFECYCLE_ACTION_WIRE_PROTOCOL,
            ExecutionWorker,
            ExecutionWorkerCatalog,
            ExecutionWorkerEnvironment,
            ExecutionWorkerKind,
        )
        from literate_ai.contracts.identity import canonical_json_bytes
        from literate_ai.contracts.worker_capabilities import (
            NvidiaProbeStatus,
            WorkerHardwareObservation,
            WorkerHardwareObservationCatalog,
        )
        from literate_ai.storage import FileSystemCAS
        from tests.support import fixtures_test_cli_worker_health as health_fixture

        private = self.root / "private"
        cas = private / "source-cas"
        workspace = private / "worker-jobs"
        workspace.mkdir(parents=True)
        FileSystemCAS(cas)  # the operator initializes the shared CAS layout
        argv = command(cas, workspace)
        if callable(finalize):
            finalize = finalize()  # may depend on the receiver config just written
        worker = ExecutionWorker(
            "cli-worker",
            ExecutionWorkerKind.COMMAND,
            command=argv,
            environment=(
                ExecutionWorkerEnvironment(
                    "LITAI_ACTION_WORKER_IDENTITY", "CLI_WORKER_IDENTITY", True
                ),
            ),
            action_protocol=LIFECYCLE_ACTION_WIRE_PROTOCOL,
        )
        catalog = private / "workers.json"
        catalog.write_bytes(
            canonical_json_bytes(ExecutionWorkerCatalog((worker,)).to_dict())
        )
        family = {"darwin": "macos", "linux": "linux"}[sys.platform]
        observations = private / "worker-observations.json"
        observations.write_bytes(
            canonical_json_bytes(
                WorkerHardwareObservationCatalog(
                    (
                        WorkerHardwareObservation(
                            "cli-worker",
                            datetime.now(UTC).isoformat(),
                            family,
                            family,
                            "1",
                            "arm64" if os.uname().machine == "arm64" else "x86_64",
                            os.cpu_count() or 1,
                            os.cpu_count() or 1,
                            16384,
                            (),
                            NvidiaProbeStatus.NOT_APPLICABLE,
                        ),
                    )
                ).to_dict()
            )
        )
        health = health_fixture.WorkerHealthCliTests()
        health.setUp()
        self.addCleanup(health.doCleanups)
        health.config["worker_id"] = "cli-worker"
        health.config["health_command"] = {
            "schema": "literate-ai/private-worker-storage-command@1",
            "command": [sys.executable, "-B", worker_storage_probe.__file__],
            "environment": [],
        }
        health.write_config()
        configuration = private / "action-execution.json"
        configuration.write_bytes(
            canonical_json_bytes(
                {
                    "schema": "literate-ai/private-action-execution@1",
                    "source_cas_root": str(cas),
                    "source_handoff": "filesystem-cas",
                    "duration_seconds": 600,
                    "maximum_hardware_age_seconds": 600,
                    "health_configurations": {"cli-worker": str(health.config_file)},
                    "result_sources": {"cli-worker": {"kind": "shared-cas"}},
                    **({} if finalize is None else {"finalize": finalize}),
                }
            )
        )
        self.environment |= {
            "LITAI_ACTION_EXECUTION_CONFIG": str(configuration),
            "LITAI_WORKER_CONFIG": str(catalog),
            "LITAI_WORKER_OBSERVATIONS": str(observations),
            "CLI_WORKER_IDENTITY": worker.identity.uri,
        }
        return private

    def test_rebuild_routes_lifecycle_actions_through_an_admitted_worker(self):
        """The CLI admits a private command worker and dispatches actions to it."""
        log = self.root / "receiver.log"
        receiver = (
            "import runpy,sys;"
            f"open({str(log)!r},'a').write(' '.join(sys.argv[1:])+'\\n');"
            "sys.argv[0]='action_worker';"
            "runpy.run_module('literate_ai.action_worker',run_name='__main__')"
        )
        self.admit_worker(
            lambda cas, workspace: (
                sys.executable,
                "-I",
                "-c",
                receiver,
                "--cas",
                str(cas),
                "--workspace",
                str(workspace),
            )
        )
        status, envelope = self.rebuild()
        self.assertEqual(status, 0, envelope)
        self.assertTrue(envelope["result"]["passed"], envelope)
        invocations = log.read_text("utf-8").splitlines()
        self.assertTrue(any("--describe" in line for line in invocations))
        self.assertTrue(
            any("--describe" not in line for line in invocations), invocations
        )

    def reference_receiver(self, phases, *, finalize=None, worker_host=False):
        """Write the operator's private receiver config; return its command.

        With `worker_host`, the worker keeps its own environment and tools, as a
        separate host would, instead of mirroring the controller's PATH.
        """
        from literate_ai.adapters.builders.make import discover_make_toolchain
        from literate_ai.adapters.builders.python import discover_python_toolchain

        environment = without_empty_loader_entries(
            os.environ if worker_host else {**os.environ, **self.environment}
        )
        config = self.root / "receiver" / "standard-receiver.json"
        config.parent.mkdir(exist_ok=True)

        def command(cas, workspace):
            document = {
                "schema": "literate-ai/standard-receiver@1",
                "cas": str(cas),
                "workspace": str(workspace),
                "phases": list(phases),
                "tools": {
                    # The operator pins the same tools the controller observes,
                    # so locked toolchain identities match on this host.
                    "python": list(discover_python_toolchain(environment).command),
                    "make": list(discover_make_toolchain(environment).command),
                },
                "child_environment": environment,
                "contract_policy": "portable-starter@1",
                # Operator-private, outside the workspace and CAS actions write.
                "dependency_cache": str(config.parent / "dependency-facts.json"),
            }
            if finalize is not None:
                document["finalize"] = finalize
            config.write_text(json.dumps(document), "utf-8")
            return (
                sys.executable,
                "-I",
                "-m",
                "literate_ai.standard_receiver",
                "--config",
                str(config),
            )

        return config, command

    @contextlib.contextmanager
    def without_local_component_phases(self):
        """The controller must never quietly run a Component phase itself."""
        from literate_ai.adapters.lifecycle.standard_local import (
            LocalStandardLifecyclePorts,
        )

        def local(phase):
            def refuse(*_args, **_kwargs):
                raise AssertionError(f"controller ran {phase} locally")

            return refuse

        with (
            patch.object(LocalStandardLifecyclePorts, "build", local("BUILD")),
            patch.object(LocalStandardLifecyclePorts, "test", local("TEST")),
            patch.object(LocalStandardLifecyclePorts, "execute", local("EXECUTE")),
            patch.object(LocalStandardLifecyclePorts, "accept", local("ACCEPT")),
        ):
            yield

    def test_reference_receiver_runs_component_phases_on_the_worker(self):
        """BUILD, TEST, EXECUTE and ACCEPT run in the reference receiver only."""
        _config, command = self.reference_receiver(
            ("BUILD", "TEST", "EXECUTE", "ACCEPT")
        )
        self.admit_worker(command)
        with self.without_local_component_phases():
            status, envelope = self.rebuild()
        self.assertEqual(status, 0, envelope)
        self.assertTrue(envelope["result"]["passed"], envelope)
        self.assertTrue(envelope["result"]["receipt_committed"], envelope)

    def rebuild_with_operator_grant(self, *, worker_host=False):
        """Run every phase on the receiver; an operator grants FINALIZE mid-run."""
        import subprocess
        import threading
        import time

        private = self.root / "operator"
        (private / "grants").mkdir(parents=True)
        oracle = private / "hello-component.json"
        shutil.copyfile(
            self.project / "verification" / "acceptance" / "hello-component.json",
            oracle,
        )
        grant = private / "grants" / "finalize.json"
        request = private / "finalize-request.json"
        config, command = self.reference_receiver(
            ("BUILD", "TEST", "EXECUTE", "ACCEPT", "PACKAGE", "FINALIZE"),
            finalize={
                "grant_path": str(grant),
                "oracle": {"component": "hello-component", "path": str(oracle)},
            },
            worker_host=worker_host,
        )

        def receiver(*arguments):
            return subprocess.run(
                (
                    sys.executable,
                    "-I",
                    "-m",
                    "literate_ai.standard_receiver",
                    "--config",
                    str(config),
                    *arguments,
                ),
                capture_output=True,
                text=True,
                check=True,
                timeout=120,
            ).stdout

        self.admit_worker(
            command,
            finalize=lambda: {
                # The operator pins the receiver's printed private profile.
                "profile_identity": json.loads(receiver("--print-profiles"))[
                    "finalize_profile"
                ],
                "verifier": "portable-application@1",
                "grant_request_path": str(request),
                "grant_wait_seconds": 300,
            },
        )
        issued = []

        def operator():
            # Review happens out of band; here the operator grants what was asked.
            stop = time.monotonic() + 540
            while not request.exists() and time.monotonic() < stop:
                time.sleep(0.5)
            if request.exists():
                issued.append(
                    receiver(
                        "--issue-finalize-grant",
                        str(request),
                        "--actor",
                        "operator",
                        "--reason",
                        "reviewed starter FINALIZE",
                        "--expires-in",
                        "1800",
                    )
                )

        thread = threading.Thread(target=operator, daemon=True)
        thread.start()
        with self.without_local_component_phases():
            status, envelope = self.rebuild()
        thread.join(timeout=60)
        # The rebuild's own result explains why no request was described.
        self.assertTrue(
            issued, f"FINALIZE never described a grant request: {status} {envelope}"
        )
        self.assertTrue(grant.exists())
        return status, envelope

    def test_finalize_waits_for_the_operator_to_grant_the_described_request(self):
        """FINALIZE describes its exact request and continues once it is granted."""
        status, envelope = self.rebuild_with_operator_grant()
        self.assertEqual(status, 0, envelope)
        self.assertTrue(envelope["result"]["passed"], envelope)
        self.assertTrue(envelope["result"]["receipt_committed"], envelope)

    def test_controller_without_the_tools_uses_the_worker_toolchain(self):
        """Locked tool identities come from the worker, not the controller host."""
        # This controller cannot run Make: local toolchain discovery would fail,
        # so only a closure derived from the admitted worker can succeed.
        fake_make = self.root / "tools" / "make"
        fake_make.write_text("#!/bin/sh\necho controller make >&2\nexit 97\n")
        fake_make.chmod(0o755)
        status, envelope = self.rebuild_with_operator_grant(worker_host=True)
        self.assertEqual(status, 0, envelope)
        self.assertTrue(envelope["result"]["passed"], envelope)
        self.assertTrue(envelope["result"]["receipt_committed"], envelope)


if __name__ == "__main__":
    unittest.main()
