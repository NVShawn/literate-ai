from __future__ import annotations

import unittest
from pathlib import Path

from literate_ai.adapters.execution_dispatch import (
    CommandExecutionDispatcher,
    DispatcherProcessResult,
)
from literate_ai.contracts import (
    ContentIdentity,
    ContentReference,
    DispatchResultStatus,
    ExecutionDispatchRequest,
    ExecutionDispatchResult,
    ExecutionRequirements,
    ExecutionWorker,
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


class ExecutionDispatchAdapterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
