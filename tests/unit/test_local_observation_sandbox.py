from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.source_to_specification import (
    LocalObservationSandbox,
    SourceToSpecificationError,
    canonical_digest,
)
from literate_ai.source_to_specification.sandbox import (
    _remove_scratch_directory,
    _resource_limited_command,
)


def sandbox_profile_path(path: Path) -> str:
    """Render one host path exactly as a sandbox-exec string literal."""

    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


class LocalObservationSandboxTests(unittest.TestCase):
    def sandbox(self, root: Path, *, system: str, resolver):
        return LocalObservationSandbox(
            source_root=root,
            command=("/usr/bin/example-observer", "--json"),
            system=system,
            tool_resolver=resolver,
        )

    def authorization(self, request):
        return {
            "authorization_id": "observe-auth:test",
            "request_digest": canonical_digest(request),
            "privileges": request["requested_privileges"],
        }

    def request(self, sandbox):
        return {
            "runner_id": sandbox.runner_id,
            "harness_digest": sandbox.harness_digest,
            "source_digests": [sandbox.source_digest],
            "requested_privileges": ["processes"],
        }

    def test_macos_profile_denies_network_and_writes_except_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            scratch = base / "scratch"
            scratch.mkdir()
            sandbox = self.sandbox(
                root,
                system="Darwin",
                resolver=lambda name: f"/usr/bin/{name}",
            )
            command = sandbox.command_for(scratch)
            self.assertEqual(command[:2], ("/usr/bin/sandbox-exec", "-p"))
            profile = command[2]
            self.assertIn("(deny default)", profile)
            self.assertIn("(deny network*)", profile)
            escaped_root = sandbox_profile_path(root)
            self.assertIn(f'(allow file-read* (subpath "{escaped_root}"))', profile)
            escaped_scratch = sandbox_profile_path(scratch)
            self.assertIn(f'(allow file-write* (subpath "{escaped_scratch}"))', profile)
            self.assertEqual(command[-2:], ("/usr/bin/example-observer", "--json"))

    def test_linux_uses_read_only_namespace_and_clean_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            scratch = base / "scratch"
            scratch.mkdir()
            sandbox = self.sandbox(
                root,
                system="Linux",
                resolver=lambda name: f"/usr/bin/{name}",
            )
            command = sandbox.command_for(scratch)
            self.assertEqual(command[0], "/usr/bin/bwrap")
            self.assertIn("--unshare-all", command)
            self.assertIn("--clearenv", command)
            read_only_mounts = [
                command[index : index + 3]
                for index, value in enumerate(command)
                if value == "--ro-bind"
            ]
            self.assertNotIn(("--ro-bind", "/", "/"), read_only_mounts)
            self.assertIn(
                ("--ro-bind", str(root.resolve()), str(root.resolve())),
                read_only_mounts,
            )
            self.assertIn(
                ("--bind", str(scratch.resolve()), str(scratch.resolve())),
                [
                    command[index : index + 3]
                    for index, value in enumerate(command)
                    if value == "--bind"
                ],
            )
            self.assertNotIn(os.environ.get("HOME", "never-present"), command)

    def test_missing_or_unsupported_sandbox_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            scratch = base / "scratch"
            scratch.mkdir()
            for system in ("Darwin", "Linux"):
                with self.subTest(system=system):
                    sandbox = self.sandbox(
                        root, system=system, resolver=lambda _name: None
                    )
                    with self.assertRaisesRegex(
                        SourceToSpecificationError, "unavailable"
                    ):
                        sandbox.command_for(scratch)
            unsupported = self.sandbox(
                root,
                system="Windows",
                resolver=lambda name: f"/usr/bin/{name}",
            )
            with self.assertRaisesRegex(SourceToSpecificationError, "only macOS"):
                unsupported.command_for(scratch)

    def test_scratch_cannot_overlap_source_or_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            nested = source / "scratch"
            nested.mkdir()
            sandbox = self.sandbox(
                source,
                system="Darwin",
                resolver=lambda name: f"/usr/bin/{name}",
            )

            with self.assertRaisesRegex(SourceToSpecificationError, "overlap"):
                sandbox.command_for(nested)
            with self.assertRaisesRegex(SourceToSpecificationError, "overlap"):
                sandbox.command_for(base)

    def test_mismatched_harness_fails_before_process_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = self.sandbox(
                root,
                system="Darwin",
                resolver=lambda name: f"/usr/bin/{name}",
            )
            request = self.request(sandbox)
            request["harness_digest"] = "sha256:wrong"
            with patch("subprocess.Popen") as process:
                with self.assertRaisesRegex(SourceToSpecificationError, "harness"):
                    sandbox.run(request, {})
                process.assert_not_called()

    def test_missing_or_mismatched_authorization_fails_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = self.sandbox(
                root,
                system="Darwin",
                resolver=lambda name: f"/usr/bin/{name}",
            )
            request = self.request(sandbox)
            with patch("subprocess.Popen") as process:
                with self.assertRaisesRegex(
                    SourceToSpecificationError, "authorization"
                ):
                    sandbox.run(request, {})
                mismatch = self.authorization(request)
                mismatch["request_digest"] = "sha256:wrong"
                with self.assertRaisesRegex(
                    SourceToSpecificationError, "authorization"
                ):
                    sandbox.run(request, mismatch)
                process.assert_not_called()

    @unittest.skipUnless(
        hasattr(os, "killpg") and hasattr(signal, "SIGKILL"),
        "requires POSIX process groups",
    )
    def test_timeout_kills_the_complete_sandbox_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = LocalObservationSandbox(
                source_root=root,
                command=("/usr/bin/example-observer",),
                timeout_seconds=0.01,
                system="Darwin",
                tool_resolver=lambda name: f"/usr/bin/{name}",
            )
            request = self.request(sandbox)
            process = Mock(pid=4321)
            process.wait.side_effect = [
                subprocess.TimeoutExpired("sandbox", 0.01),
                0,
            ]
            with (
                patch("subprocess.Popen", return_value=process),
                patch("os.killpg") as kill_group,
            ):
                with self.assertRaisesRegex(SourceToSpecificationError, "timeout"):
                    sandbox.run(request, self.authorization(request))
            kill_group.assert_called_once_with(4321, signal.SIGKILL)

    def test_resource_limited_command_wraps_in_shell_ulimit_not_preexec_fn(
        self,
    ) -> None:
        """Unit-level regression for issue #73's fork-safety redesign.

        `_resource_limited_command` must express the FSIZE/CPU limits as a
        shell script that runs *after* `exec`, never as Python invoked from
        a `preexec_fn` between `fork()` and `exec()`.
        """

        wrapped = _resource_limited_command(
            ("/usr/bin/example-observer", "--json"),
            shell_executable="/bin/sh",
            maximum_output_bytes=2048,
            timeout_seconds=9.4,
        )
        self.assertEqual(wrapped[0], "/bin/sh")
        self.assertEqual(wrapped[1], "-c")
        script = wrapped[2]
        self.assertIn("ulimit -f 4", script)  # 2048 bytes / 512-byte blocks
        self.assertIn("ulimit -t 10", script)  # ceil(9.4) + 1
        self.assertIn('exec "$0" "$@"', script)
        # The original command follows unchanged, so exec reconstructs it.
        self.assertEqual(wrapped[3:], ("/usr/bin/example-observer", "--json"))

    @unittest.skipUnless(
        hasattr(os, "killpg") and hasattr(signal, "SIGKILL"),
        "requires POSIX process groups",
    )
    def test_run_never_passes_a_preexec_fn_to_popen(self) -> None:
        """Direct regression for issue #73.

        `preexec_fn` runs arbitrary Python inside a `fork()`ed child of a
        process that may already hold other threads (this process's own
        generation workers, HTTPS clients, logging). A lock one of those
        threads held at fork time stays permanently held in the child, and
        essentially all Python bytecode needs CPython's internal allocator
        lock -- so any non-trivial `preexec_fn` risks deadlocking the child
        before it ever reaches `exec()`. `LocalObservationSandbox.run` must
        never pass `preexec_fn` to `subprocess.Popen` at all; the resource
        limits are applied by the exec'd shell wrapper instead.
        """

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = LocalObservationSandbox(
                source_root=root,
                command=("/usr/bin/example-observer",),
                system="Darwin",
                tool_resolver=lambda name: f"/usr/bin/{name}",
            )
            request = self.request(sandbox)
            process = Mock(pid=4321)
            process.wait.return_value = 0
            with patch("subprocess.Popen", return_value=process) as popen:
                # The mocked process never actually writes valid harness
                # output, so `run` goes on to raise past the Popen call --
                # irrelevant here, since only the Popen kwargs matter.
                with self.assertRaises(SourceToSpecificationError):
                    sandbox.run(request, self.authorization(request))
            popen.assert_called_once()
            self.assertNotIn("preexec_fn", popen.call_args.kwargs)

    @unittest.skipUnless(
        platform.system() == "Darwin" and shutil.which("sandbox-exec"),
        "requires the macOS sandbox runtime",
    )
    def test_runtime_actually_applies_the_fsize_and_cpu_rlimits(self) -> None:
        """End-to-end regression for issue #73's redesign.

        Proves the shell-`ulimit` wrapper achieves the same effect the
        removed `preexec_fn` used to: the harness process, once running,
        really does observe the configured FSIZE/CPU resource limits.
        """

        program = """import json, resource
fsize_soft, _ = resource.getrlimit(resource.RLIMIT_FSIZE)
cpu_soft, _ = resource.getrlimit(resource.RLIMIT_CPU)
print(json.dumps({"fsize_soft": fsize_soft, "cpu_soft": cpu_soft}))
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = LocalObservationSandbox(
                source_root=root,
                command=(sys.executable, "-c", program),
                timeout_seconds=7.0,
                maximum_output_bytes=4096,
                system="Darwin",
            )
            result = sandbox.run(
                request := self.request(sandbox),
                self.authorization(request),
            )
            # Rounded up to whole blocks (see `_resource_limited_command`'s
            # docstring: the shell's own block unit is 512 bytes on strict
            # POSIX shells but 1024 bytes on macOS's bash-based /bin/sh), so
            # the observed limit must never be tighter than the configured
            # byte budget, but on a 1024-byte-block shell may end up looser
            # by up to roughly double.
            self.assertGreaterEqual(result["fsize_soft"], 4096)
            self.assertLessEqual(result["fsize_soft"], 4096 * 2 + 1024)
            self.assertEqual(result["cpu_soft"], 8)  # int(7.0) + 1

    @unittest.skipUnless(
        platform.system() == "Darwin" and shutil.which("sandbox-exec"),
        "requires the macOS sandbox runtime",
    )
    def test_timeout_post_terminate_wait_is_bounded_even_if_the_kill_is_a_noop(
        self,
    ) -> None:
        """Regression for issue #73's unbounded post-kill wait.

        Before the fix, the timeout branch called `os.killpg` then an
        unbounded `process.wait()`. If the kill signal never actually
        reached the process tree (simulated here, exactly per the issue's
        own reproducer, by patching `terminate_process_tree` to a no-op),
        that second wait would block for as long as the child kept running
        -- i.e. forever, for a long-lived/kill-resistant child. `run` must
        instead bound that wait (grace period, then its own `kill()`
        escalation) and still raise `sandbox.timeout` promptly.
        """

        script = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(30)\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = LocalObservationSandbox(
                source_root=root,
                command=(sys.executable, "-c", script),
                timeout_seconds=1.0,
                system="Darwin",
            )
            request = self.request(sandbox)
            with patch(
                "literate_ai.source_to_specification.sandbox.terminate_process_tree"
            ) as fake_terminate:
                fake_terminate.return_value = None
                started = time.monotonic()
                with self.assertRaisesRegex(SourceToSpecificationError, "timeout"):
                    sandbox.run(request, self.authorization(request))
                elapsed = time.monotonic() - started
            self.assertTrue(fake_terminate.called)
            # Bounded by timeout_seconds plus at most two grace windows and
            # the sandbox's own kill() escalation -- nowhere near the
            # child's 30s sleep that an unbounded wait() would have blocked
            # for.
            self.assertLess(elapsed, 20)

    @unittest.skipUnless(
        platform.system() == "Darwin" and shutil.which("sandbox-exec"),
        "requires the macOS sandbox runtime",
    )
    def test_macos_runtime_denies_source_writes_network_and_host_environment(
        self,
    ) -> None:
        program = """import errno, json, os, socket
result = {'home_absent': 'HOME' not in os.environ}
try:
    open('source.txt', 'w').write('mutated')
    result['source_write_denied'] = False
except PermissionError:
    result['source_write_denied'] = True
connection = socket.socket()
try:
    connection.connect(('127.0.0.1', 9))
    result['network_denied'] = False
except OSError as exc:
    result['network_denied'] = exc.errno in (errno.EPERM, errno.EACCES)
print(json.dumps(result))
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.txt"
            source.write_text("read-only\n")
            sandbox = LocalObservationSandbox(
                source_root=root,
                command=(sys.executable, "-c", program),
                system="Darwin",
            )
            result = sandbox.run(
                request := self.request(sandbox),
                self.authorization(request),
            )
            self.assertEqual(
                result,
                {
                    "home_absent": True,
                    "network_denied": True,
                    "source_write_denied": True,
                },
            )
            self.assertEqual(source.read_text(), "read-only\n")

    def test_scratch_cleanup_retries_past_a_transient_open_handle(self) -> None:
        # Regression test for #80: a still-alive child holding an open
        # handle inside the scratch directory must not fail cleanup
        # outright -- it should retry with backoff and succeed once the
        # handle closes.
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary) / "literate-observe-race"
            scratch.mkdir()
            errors = [OSError("still open"), OSError("still open")]
            real_rmtree = shutil.rmtree

            def flaky_rmtree(path):
                if errors:
                    raise errors.pop(0)
                real_rmtree(path)

            with (
                patch(
                    "literate_ai.source_to_specification.sandbox.shutil.rmtree",
                    side_effect=flaky_rmtree,
                ) as rmtree,
                patch("literate_ai.source_to_specification.sandbox.time.sleep"),
            ):
                _remove_scratch_directory(scratch)
            self.assertEqual(rmtree.call_count, 3)
            self.assertFalse(scratch.exists())

    def test_scratch_cleanup_raises_cleanup_failed_after_exhausting_retries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary) / "literate-observe-stuck"
            scratch.mkdir()
            with (
                patch(
                    "literate_ai.source_to_specification.sandbox.shutil.rmtree",
                    side_effect=OSError("still open"),
                ),
                patch("literate_ai.source_to_specification.sandbox.time.sleep"),
            ):
                with self.assertRaisesRegex(
                    SourceToSpecificationError, "could not be removed"
                ):
                    _remove_scratch_directory(scratch)

    def test_scratch_cleanup_failure_never_masks_an_in_flight_error(self) -> None:
        # If the sandboxed observation already failed for its own reason
        # (e.g. a timeout), a stale scratch directory is a leak to clean up
        # later, not a correctness problem -- it must not replace the
        # original, more actionable error.
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary) / "literate-observe-masking"
            scratch.mkdir()
            with (
                patch(
                    "literate_ai.source_to_specification.sandbox.shutil.rmtree",
                    side_effect=OSError("still open"),
                ),
                patch("literate_ai.source_to_specification.sandbox.time.sleep"),
            ):

                def raise_original() -> None:
                    try:
                        raise SourceToSpecificationError("sandbox.timeout", "boom")
                    except SourceToSpecificationError:
                        # No exception escapes here; the original "boom"
                        # continues to propagate untouched.
                        _remove_scratch_directory(scratch)
                        raise

                with self.assertRaisesRegex(SourceToSpecificationError, "boom"):
                    raise_original()

    @unittest.skipUnless(
        hasattr(os, "killpg") and hasattr(signal, "SIGKILL"),
        "requires POSIX process groups",
    )
    def test_timeout_still_removes_scratch_despite_a_lingering_descendant(
        self,
    ) -> None:
        # End-to-end regression for #80: even when the timed-out process
        # group leaves a descendant that keeps the scratch directory's
        # handle open for one retry attempt, `run` must still surface the
        # timeout error (not a cleanup error) and the scratch directory must
        # not survive the call.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = LocalObservationSandbox(
                source_root=root,
                command=("/usr/bin/example-observer",),
                timeout_seconds=0.01,
                system="Darwin",
                tool_resolver=lambda name: f"/usr/bin/{name}",
            )
            request = self.request(sandbox)
            process = Mock(pid=4321)
            process.wait.side_effect = [
                subprocess.TimeoutExpired("sandbox", 0.01),
                0,
            ]
            captured_scratch: list[Path] = []
            real_rmtree = shutil.rmtree
            attempts = {"count": 0}

            def flaky_rmtree(path):
                captured_scratch.append(Path(path))
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise OSError("still open")
                real_rmtree(path)

            with (
                patch("subprocess.Popen", return_value=process),
                patch("os.killpg") as kill_group,
                patch(
                    "literate_ai.source_to_specification.sandbox.shutil.rmtree",
                    side_effect=flaky_rmtree,
                ),
                patch("literate_ai.source_to_specification.sandbox.time.sleep"),
            ):
                with self.assertRaisesRegex(SourceToSpecificationError, "timeout"):
                    sandbox.run(request, self.authorization(request))
            kill_group.assert_called_once_with(4321, signal.SIGKILL)
            self.assertEqual(attempts["count"], 2)
            self.assertTrue(captured_scratch)
            self.assertFalse(captured_scratch[0].exists())

    def test_request_for_another_source_fails_before_process_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sandbox = self.sandbox(
                root,
                system="Darwin",
                resolver=lambda name: f"/usr/bin/{name}",
            )
            request = self.request(sandbox)
            request["source_digests"] = ["sha256:" + "0" * 64]

            with patch("subprocess.Popen") as process:
                with self.assertRaisesRegex(SourceToSpecificationError, "source"):
                    sandbox.run(request, self.authorization(request))

            process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
