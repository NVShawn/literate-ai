from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.execution_dispatch import (
    BoundedDispatcherProcessRunner,
    CommandExecutionDispatcher,
    DispatcherProcessResult,
    ExecutionDispatchAdapterError,
    LocalExecutionDispatcher,
    SshExecutionDispatcher,
    load_execution_worker_catalog,
)
from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerEnvironment,
    ExecutionWorkerKind,
    HashAlgorithm,
    LifecycleDispatchAction,
    ObservedExecutionEnvironment,
    canonical_json_bytes,
)


def identity(character: str) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, character * 64)


def artifact_reference(character: str = "7") -> ContentReference:
    return ContentReference(
        "artifact-export",
        f"cas:sha256:{character * 64}",
        identity(character),
    )


def command_worker(*, request_file: bool = False) -> ExecutionWorker:
    command = ("dispatcher", "{request_file}") if request_file else ("dispatcher",)
    return ExecutionWorker(
        "fleet",
        ExecutionWorkerKind.COMMAND,
        target_profile="linux-host",
        requirements=ExecutionRequirements(os_family="linux", minimum_cpu_cores=4),
        command=command,
        environment=(
            ExecutionWorkerEnvironment("DISPATCH_TOKEN", "PRIVATE_TOKEN", True),
        ),
    )


def request(
    worker: ExecutionWorker,
    *,
    requirements: ExecutionRequirements | None = None,
) -> ExecutionDispatchRequest:
    return ExecutionDispatchRequest(
        LifecycleDispatchAction.TEST,
        "component://example/service",
        "components/service",
        worker.target_profile,
        (),
        worker.identity,
        worker.requirements if requirements is None else requirements,
        (),
        (),
        identity("1"),
        identity("2"),
        identity("3"),
        identity("4"),
        identity("5"),
        identity("6"),
        identity("7"),
        None,
        30,
    )


def result(
    dispatch_request: ExecutionDispatchRequest,
    worker: ExecutionWorker,
    *,
    os_family: str = "linux",
) -> ExecutionDispatchResult:
    return ExecutionDispatchResult(
        dispatch_request.identity,
        worker.identity,
        "third-party-task",
        DispatchResultStatus.PASSED,
        ObservedExecutionEnvironment(
            os_family,
            "24.04",
            "x86_64",
            8,
            32768,
            toolchain_identities=(identity("5"),),
        ),
        0,
        artifact_reference("7"),
        identity("8"),
    )


class RecordingRunner:
    def __init__(self, output: DispatcherProcessResult) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        stdin: bytes | None,
        timeout_seconds: int,
    ) -> DispatcherProcessResult:
        request_file_bytes = None
        if len(argv) == 2:
            request_path = Path(argv[1])
            self.assert_request_path(request_path)
            request_file_bytes = request_path.read_bytes()
        self.calls.append(
            {
                "argv": argv,
                "cwd": cwd,
                "environment": dict(environment),
                "stdin": stdin,
                "request_file_bytes": request_file_bytes,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.output

    @staticmethod
    def assert_request_path(path: Path) -> None:
        if not path.is_file() or path.is_symlink():
            raise AssertionError(
                "request file must exist as a regular file during dispatch"
            )


class RecordingLocalHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[ExecutionDispatchRequest, Path]] = []
        self.output: ExecutionDispatchResult | None = None

    def execute(
        self, request: ExecutionDispatchRequest, *, cwd: Path
    ) -> ExecutionDispatchResult:
        self.calls.append((request, cwd))
        assert self.output is not None
        return self.output


class RecordingSshHandler(RecordingLocalHandler):
    def __init__(self) -> None:
        super().__init__()
        self.worker: ExecutionWorker | None = None

    def execute(
        self,
        worker: ExecutionWorker,
        request: ExecutionDispatchRequest,
        *,
        cwd: Path,
    ) -> ExecutionDispatchResult:
        self.worker = worker
        return super().execute(request, cwd=cwd)


class ExecutionDispatchAdapterTests(unittest.TestCase):
    def test_local_dispatch_delegates_to_existing_lifecycle(self) -> None:
        worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        dispatch_request = request(worker)
        handler = RecordingLocalHandler()
        handler.output = result(dispatch_request, worker)

        actual = LocalExecutionDispatcher(handler).dispatch(
            worker, dispatch_request, cwd=Path.cwd()
        )

        self.assertEqual(actual, handler.output)
        self.assertEqual(handler.calls, [(dispatch_request, Path.cwd().resolve())])

    def test_local_dispatch_rejects_wrong_kind_and_invalid_result(self) -> None:
        worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        dispatch_request = request(worker)
        handler = RecordingLocalHandler()
        handler.output = result(dispatch_request, worker, os_family="windows")
        with self.assertRaises(ExecutionDispatchAdapterError) as raised:
            LocalExecutionDispatcher(handler).dispatch(
                command_worker(), dispatch_request, cwd=Path.cwd()
            )
        self.assertEqual(raised.exception.code, "execution.worker_kind_invalid")

        constrained = ExecutionWorker(
            "linux",
            ExecutionWorkerKind.LOCAL,
            requirements=ExecutionRequirements(os_family="linux"),
        )
        constrained_request = request(constrained)
        handler.output = result(constrained_request, constrained, os_family="windows")
        with self.assertRaises(ExecutionDispatchAdapterError) as mismatch:
            LocalExecutionDispatcher(handler).dispatch(
                constrained, constrained_request, cwd=Path.cwd()
            )
        self.assertEqual(
            mismatch.exception.code, "execution.dispatcher_requirements_unsatisfied"
        )

    def test_dispatch_rejects_a_target_profile_mismatch(self) -> None:
        worker = ExecutionWorker(
            "local", ExecutionWorkerKind.LOCAL, target_profile="linux-host"
        )
        dispatch_request = ExecutionDispatchRequest.from_dict(
            {**request(worker).to_dict(), "target_profile": "windows-host"}
        )
        handler = RecordingLocalHandler()
        handler.output = result(dispatch_request, worker)

        with self.assertRaises(ExecutionDispatchAdapterError) as raised:
            LocalExecutionDispatcher(handler).dispatch(
                worker, dispatch_request, cwd=Path.cwd()
            )
        self.assertEqual(
            raised.exception.code, "execution.worker_target_profile_mismatch"
        )

    def test_ssh_dispatch_delegates_to_bounded_lane_with_exact_worker(self) -> None:
        worker = ExecutionWorker(
            "ssh",
            ExecutionWorkerKind.SSH,
            endpoint="user@host",
            workspace="~/literate-ai",
        )
        dispatch_request = request(worker)
        handler = RecordingSshHandler()
        handler.output = result(dispatch_request, worker)

        actual = SshExecutionDispatcher(handler).dispatch(
            worker, dispatch_request, cwd=Path.cwd()
        )

        self.assertEqual(actual, handler.output)
        self.assertEqual(handler.worker, worker)
        self.assertEqual(handler.calls, [(dispatch_request, Path.cwd().resolve())])

    def test_successful_run_must_preserve_its_input_artifact(self) -> None:
        worker = ExecutionWorker(
            "local",
            ExecutionWorkerKind.LOCAL,
            target_profile="linux-host",
            requirements=ExecutionRequirements(os_family="linux"),
        )
        run_request = ExecutionDispatchRequest.from_dict(
            {
                **request(worker).to_dict(),
                "action": "run",
                "artifact_reference": artifact_reference("7").to_dict(),
            }
        )
        handler = RecordingLocalHandler()
        handler.output = ExecutionDispatchResult(
            run_request.identity,
            worker.identity,
            "task",
            DispatchResultStatus.PASSED,
            ObservedExecutionEnvironment(
                "linux",
                "24.04",
                "x86_64",
                8,
                32768,
                toolchain_identities=(identity("5"),),
            ),
            0,
            artifact_reference("8"),
            identity("9"),
        )
        with self.assertRaises(ExecutionDispatchAdapterError) as raised:
            LocalExecutionDispatcher(handler).dispatch(
                worker,
                run_request,
                cwd=Path.cwd(),
            )
        self.assertEqual(
            raised.exception.code, "execution.dispatcher_artifact_mismatch"
        )

    def test_catalog_loader_accepts_only_bounded_non_symlink_contracts(self) -> None:
        catalog = ExecutionWorkerCatalog(
            (ExecutionWorker("local", ExecutionWorkerKind.LOCAL),)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "workers.json"
            configured.write_bytes(canonical_json_bytes(catalog.to_dict()))

            self.assertEqual(load_execution_worker_catalog(configured), catalog)
            link = root / "workers-link.json"
            try:
                link.symlink_to(configured)
            except OSError:
                pass
            else:
                with self.assertRaises(ExecutionDispatchAdapterError) as raised:
                    load_execution_worker_catalog(link)
                self.assertEqual(
                    raised.exception.code, "execution.worker_catalog_unavailable"
                )
            configured.write_text("not-json", encoding="utf-8")
            with self.assertRaises(ExecutionDispatchAdapterError) as raised:
                load_execution_worker_catalog(configured)
            self.assertEqual(raised.exception.code, "execution.worker_catalog_invalid")

    def test_stdin_dispatch_is_shell_free_minimal_and_does_not_persist_secrets(
        self,
    ) -> None:
        worker = command_worker()
        dispatch_request = request(worker)
        expected = result(dispatch_request, worker)
        runner = RecordingRunner(
            DispatcherProcessResult(0, canonical_json_bytes(expected.to_dict()), b"")
        )
        dispatcher = CommandExecutionDispatcher(
            runner,
            environment={
                "PATH": "/safe/bin",
                "PRIVATE_TOKEN": "secret-value",
                "UNRELATED_SECRET": "must-not-leak",
            },
        )

        actual = dispatcher.dispatch(worker, dispatch_request, cwd=Path.cwd())

        self.assertEqual(actual, expected)
        call = runner.calls[0]
        self.assertEqual(call["argv"], ("dispatcher",))
        self.assertEqual(
            call["stdin"], canonical_json_bytes(dispatch_request.to_dict())
        )
        environment = call["environment"]
        assert isinstance(environment, dict)
        self.assertEqual(environment["DISPATCH_TOKEN"], "secret-value")
        self.assertNotIn("PRIVATE_TOKEN", environment)
        self.assertNotIn("UNRELATED_SECRET", environment)
        self.assertNotIn(b"secret-value", call["stdin"])

    def test_request_file_exists_only_during_the_dispatch_call(self) -> None:
        worker = command_worker(request_file=True)
        dispatch_request = request(worker)
        expected = result(dispatch_request, worker)
        runner = RecordingRunner(
            DispatcherProcessResult(0, canonical_json_bytes(expected.to_dict()), b"")
        )

        CommandExecutionDispatcher(
            runner, environment={"PRIVATE_TOKEN": "secret-value"}
        ).dispatch(worker, dispatch_request, cwd=Path.cwd())

        call = runner.calls[0]
        argv = call["argv"]
        assert isinstance(argv, tuple)
        self.assertFalse(Path(argv[1]).exists())
        self.assertEqual(
            call["request_file_bytes"], canonical_json_bytes(dispatch_request.to_dict())
        )
        self.assertIsNone(call["stdin"])

    def test_required_environment_binding_reports_authentication_prerequisite(
        self,
    ) -> None:
        worker = command_worker()
        dispatch_request = request(worker)
        dispatcher = CommandExecutionDispatcher(
            RecordingRunner(DispatcherProcessResult(0, b"{}", b"")), environment={}
        )

        with self.assertRaises(ExecutionDispatchAdapterError) as raised:
            dispatcher.dispatch(worker, dispatch_request, cwd=Path.cwd())

        self.assertEqual(
            raised.exception.code, "execution.dispatcher_authentication_required"
        )

    def test_dispatch_rejects_requirements_different_from_the_selected_worker(
        self,
    ) -> None:
        worker = command_worker()
        dispatch_request = request(worker, requirements=ExecutionRequirements())
        dispatcher = CommandExecutionDispatcher(
            RecordingRunner(DispatcherProcessResult(0, b"{}", b"")),
            environment={"PRIVATE_TOKEN": "secret-value"},
        )

        with self.assertRaises(ExecutionDispatchAdapterError) as raised:
            dispatcher.dispatch(worker, dispatch_request, cwd=Path.cwd())

        self.assertEqual(
            raised.exception.code, "execution.worker_requirements_mismatch"
        )

    def test_dispatch_rejects_failure_invalid_identity_and_capability_conflict(
        self,
    ) -> None:
        worker = command_worker()
        dispatch_request = request(worker)
        cases = (
            (
                DispatcherProcessResult(7, b"", b"failed"),
                "execution.dispatcher_failed",
            ),
            (
                DispatcherProcessResult(0, b"not-json", b""),
                "execution.dispatcher_result_invalid",
            ),
            (
                DispatcherProcessResult(
                    0,
                    canonical_json_bytes(
                        ExecutionDispatchResult(
                            identity("9"),
                            worker.identity,
                            "third-party-task",
                            DispatchResultStatus.PASSED,
                            ObservedExecutionEnvironment(
                                "linux",
                                "24.04",
                                "x86_64",
                                8,
                                32768,
                                toolchain_identities=(identity("5"),),
                            ),
                            0,
                            artifact_reference("7"),
                            identity("8"),
                        ).to_dict()
                    ),
                    b"",
                ),
                "execution.dispatcher_request_mismatch",
            ),
            (
                DispatcherProcessResult(
                    0,
                    canonical_json_bytes(
                        result(dispatch_request, worker, os_family="windows").to_dict()
                    ),
                    b"",
                ),
                "execution.dispatcher_requirements_unsatisfied",
            ),
        )
        for output, code in cases:
            with self.subTest(code=code):
                dispatcher = CommandExecutionDispatcher(
                    RecordingRunner(output),
                    environment={"PRIVATE_TOKEN": "secret-value"},
                )
                with self.assertRaises(ExecutionDispatchAdapterError) as raised:
                    dispatcher.dispatch(worker, dispatch_request, cwd=Path.cwd())
                self.assertEqual(raised.exception.code, code)

    def test_real_process_runner_uses_an_argument_vector_and_canonical_stdin(
        self,
    ) -> None:
        runner = BoundedDispatcherProcessRunner()
        payload = b'{"safe":true}'

        completed = runner.run(
            (
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ),
            cwd=Path.cwd(),
            environment={"PATH": ""},
            stdin=payload,
            timeout_seconds=10,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, payload)
        self.assertEqual(completed.stderr, b"")

    def test_timeout_path_bounds_the_post_terminate_wait(self) -> None:
        """Regression test for issue #71.

        Before the fix, the timeout branch called `terminate_process_tree`
        and then an *unbounded* `process.communicate()`. If the tree kill
        did not actually reach the process (simulated here by patching
        `terminate_process_tree` to a no-op, exactly as the issue's
        reproducer prescribes), that second `communicate()` would block for
        as long as the child kept running -- i.e. forever, for a
        long-lived/kill-resistant child. The runner must instead bound that
        wait and still raise the dispatcher-timed-out error promptly.
        """

        runner = BoundedDispatcherProcessRunner()
        # Ignores SIGTERM (what a no-op tree kill would otherwise have sent)
        # but sleeps far longer than the runner's post-terminate grace
        # windows, so only a real SIGKILL from the runner's own escalation
        # -- not from the (mocked away) tree kill -- can end it quickly.
        script = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(30)\n"
        )

        with mock.patch(
            "literate_ai.adapters.execution_dispatch.terminate_process_tree"
        ) as fake_terminate_process_tree:
            fake_terminate_process_tree.return_value = None
            started = time.monotonic()
            with self.assertRaises(ExecutionDispatchAdapterError) as raised:
                runner.run(
                    (sys.executable, "-c", script),
                    cwd=Path.cwd(),
                    environment={"PATH": ""},
                    stdin=None,
                    timeout_seconds=1,
                )
            elapsed = time.monotonic() - started

        self.assertTrue(fake_terminate_process_tree.called)
        self.assertEqual(raised.exception.code, "execution.dispatcher_timed_out")
        # Bounded by timeout_seconds plus at most two grace windows and the
        # runner's own kill() escalation -- nowhere near the child's 30s
        # sleep that an unbounded communicate() would have blocked for.
        self.assertLess(elapsed, 20)


if __name__ == "__main__":
    unittest.main()
