from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai._cache_lock import CacheLockError
from literate_ai.adapters.builders._process import (
    BoundedProcessResult,
    run_bounded_process,
)
from literate_ai.adapters.builders.python import BuildError
from literate_ai.adapters.repository_lineage import (
    REPOSITORY_PARENT_FILE,
    GitRepositoryLineageError,
    GitRepositorySnapshotProvider,
    repository_parent_reference,
)
from literate_ai.application.repository_lineage import resolve_repository_lineage
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.project import _repository_fetch_provider
from literate_ai.contracts import (
    RepositoryFetchDeadlinePolicy,
    RepositoryParentReference,
    RepositoryParentSelection,
)

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = str(ROOT / "src")

# A standalone child-process body (run via `python -c`, not `multiprocessing`)
# that acquires `lock_path`, signals readiness by creating `ready_path`, then
# blocks. Deliberately outside `multiprocessing`: a `multiprocessing.Process`
# holds Event/semaphore state that a SIGKILL leaves in a half-torn-down state,
# hanging the *parent* interpreter's shutdown waiting on its resource
# tracker -- a plain OS process avoids that entirely and is what a genuinely
# crashed owner looks like.
_LOCK_HOLDER_CHILD_SOURCE = """
import sys, time
from pathlib import Path
lock_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
sys.path.insert(0, sys.argv[3])
from literate_ai._cache_lock import exclusive_cache_lock
with exclusive_cache_lock(lock_path):
    ready_path.write_text("ready")
    time.sleep(30)
"""

_RESOLVER_CHILD_SOURCE = """
import contextlib, json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from literate_ai.adapters import repository_lineage as lineage
from literate_ai.adapters.builders._process import run_bounded_process

repository, cache, coordination = map(Path, sys.argv[2:5])
name = sys.argv[5]
stage, revision = sys.argv[6:8]
real_lock = lineage.exclusive_cache_lock

@contextlib.contextmanager
def observed_lock(path):
    (coordination / (name + ".attempt")).touch()
    with real_lock(path):
        yield

def runner(command, **kwargs):
    if stage in command:
        (coordination / (name + "." + stage)).touch()
        deadline = time.monotonic() + 40
        while not (coordination / (name + ".release")).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("parent did not release the fetch probe")
            time.sleep(0.01)
    return run_bounded_process(command, **kwargs)

lineage.exclusive_cache_lock = observed_lock
provider = lineage.GitRepositorySnapshotProvider(cache, process_runner=runner)
resolved = provider.resolve(
    lineage.repository_parent_reference(str(repository) + "#" + revision)
)
print(json.dumps({"revision": resolved.resolved_revision,
                  "project_id": resolved.project_id}), flush=True)
"""


def _stop_child(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=15)


class _FetchConcurrencyProbe:
    """Wrap a real process runner to observe fetch-call overlap deterministically.

    Every ``git fetch`` invocation increments a shared counter before running
    and decrements it after, recording the maximum number ever observed
    in-flight simultaneously. A short sleep while "in" the fetch widens the
    race window so overlap is reliably observed if it can happen at all --
    the assertion afterward is a plain counter comparison, not a timing
    guess.
    """

    def __init__(self, real_runner=run_bounded_process, *, hold_seconds: float = 0.2):
        self._real_runner = real_runner
        self._hold_seconds = hold_seconds
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.fetch_calls = 0

    def __call__(self, command, **kwargs):
        is_fetch = "fetch" in command
        if is_fetch:
            with self._lock:
                self._active += 1
                self.max_active = max(self.max_active, self._active)
                self.fetch_calls += 1
            time.sleep(self._hold_seconds)
        try:
            return self._real_runner(command, **kwargs)
        finally:
            if is_fetch:
                with self._lock:
                    self._active -= 1


class _BarrierGatedProbe:
    """Wrap a real process runner so every fetch call must rendezvous first.

    Used to prove independent cache entries are *not* serialized against one
    another: if the implementation held a single lock across all entries,
    the second concurrent fetch could never reach the barrier while the
    first is blocked waiting for that shared lock, so the barrier would time
    out instead of releasing both callers.
    """

    def __init__(self, barrier: threading.Barrier, real_runner=run_bounded_process):
        self._barrier = barrier
        self._real_runner = real_runner

    def __call__(self, command, **kwargs):
        if "fetch" in command:
            self._barrier.wait(timeout=10)
        return self._real_runner(command, **kwargs)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env={**os.environ, "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def create_repository(
    parent: Path,
    name: str,
    selection: RepositoryParentSelection,
    *,
    include_parent: bool = True,
) -> Path:
    repository = parent / name
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Literate AI Test")
    git(repository, "config", "user.email", "litai@example.invalid")
    manifest = json.loads((ROOT / "literate.project.json").read_bytes())
    manifest["project_id"] = name
    (repository / "literate.project.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if include_parent:
        destination = repository / REPOSITORY_PARENT_FILE
        destination.parent.mkdir(parents=True)
        destination.write_text(
            json.dumps(selection.to_dict(), indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    git(repository, "add", ".")
    git(repository, "commit", "-m", f"Create {name}")
    return repository


def reference(repository: Path) -> RepositoryParentReference:
    return RepositoryParentReference(repository.resolve().as_uri(), "HEAD")


class GitRepositorySnapshotProviderTests(unittest.TestCase):
    def test_shallow_fetches_do_not_launch_automatic_maintenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = create_repository(
                temporary, "parent", RepositoryParentSelection.root()
            )
            baseline = git(repository, "rev-parse", "HEAD")
            (repository / "new-file").write_text("next revision", encoding="utf-8")
            git(repository, "add", "new-file")
            git(repository, "commit", "-m", "Advance parent")
            advanced = git(repository, "rev-parse", "HEAD")
            provider = GitRepositorySnapshotProvider(temporary / "cache")
            locator = repository.resolve().as_uri()
            with provider._locked_repository(locator) as cache:
                git(cache, "config", "maintenance.auto", "true")
            trace = temporary / "git-trace.json"
            with patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace.resolve())}):
                for revision in (baseline, advanced, baseline):
                    snapshot = provider.resolve(
                        RepositoryParentReference(locator, revision)
                    )
                    self.assertEqual(snapshot.resolved_revision, revision)
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            # Observe real Git processes, not the adapter's constructed argv.
            self.assertTrue(any(event.get("event") == "start" for event in events))
            maintenance = [
                event["argv"]
                for event in events
                if event.get("event") == "child_start"
                and any(word in event.get("argv", ()) for word in ("maintenance", "gc"))
            ]
            self.assertEqual(maintenance, [])

    def test_cli_deadline_override_is_validated_and_provenanced(self) -> None:
        defaults = SimpleNamespace(
            repository_fetch_total_seconds=None,
            repository_fetch_no_progress_seconds=None,
            repository_fetch_connect_seconds=None,
        )
        explicit = SimpleNamespace(
            repository_fetch_total_seconds=600,
            repository_fetch_no_progress_seconds=300,
            repository_fetch_connect_seconds=60,
        )

        self.assertEqual(
            _repository_fetch_provider(defaults).deadline_evidence["provenance"],
            "framework-default",
        )
        selected = _repository_fetch_provider(explicit)
        self.assertEqual(selected.deadline_evidence["provenance"], "cli")
        self.assertEqual(
            selected.deadline_evidence["policy"],
            RepositoryFetchDeadlinePolicy(600, 300, 60).to_dict(),
        )
        explicit.repository_fetch_total_seconds = 29
        with self.assertRaises(CliFailure) as raised:
            _repository_fetch_provider(explicit)
        self.assertEqual(raised.exception.code, "repository_lineage.deadline_invalid")

    def test_fetch_deadline_policy_is_bounded_and_deterministic(self) -> None:
        default = RepositoryFetchDeadlinePolicy()
        self.assertEqual(
            (
                default.total_seconds,
                default.no_progress_seconds,
                default.connect_seconds,
            ),
            (3600, 600, 30),
        )
        self.assertEqual(
            RepositoryFetchDeadlinePolicy.from_dict(default.to_dict()).identity,
            default.identity,
        )
        for invalid in (
            {"total_seconds": 29},
            {"total_seconds": 14401},
            {"no_progress_seconds": 14},
            {"no_progress_seconds": 1801},
            {"connect_seconds": 4},
            {"connect_seconds": 121},
            {"total_seconds": 60, "no_progress_seconds": 61},
            {"no_progress_seconds": 30, "connect_seconds": 31},
        ):
            values = {
                "total_seconds": 3600,
                "no_progress_seconds": 600,
                "connect_seconds": 30,
                **invalid,
            }
            with self.subTest(values=values), self.assertRaises(ValueError):
                RepositoryFetchDeadlinePolicy(**values)

    def test_fetch_uses_separate_total_progress_and_ssh_connect_bounds(self) -> None:
        observed: dict[str, object] = {}

        def process_runner(command, **kwargs):
            observed["command"] = command
            observed.update(kwargs)
            return BoundedProcessResult(0, b"", b"", 138.452)

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            provider = GitRepositorySnapshotProvider(
                repository / "cache",
                process_runner=process_runner,
            )
            provider._run(repository, "fetch", "--progress")

        self.assertEqual(observed["timeout_seconds"], 3600)
        self.assertEqual(observed["inactivity_timeout_seconds"], 600)
        self.assertIn("ConnectTimeout=30", observed["environment"]["GIT_SSH_COMMAND"])
        self.assertEqual(
            provider.deadline_evidence,
            GitRepositorySnapshotProvider(Path("unused")).deadline_evidence,
        )

    def test_fetch_timeout_diagnostic_identifies_policy_without_locator(self) -> None:
        def process_runner(_command, **_kwargs):
            raise BuildError(
                "repository_lineage.git_timeout",
                "Tool process exceeded its total deadline "
                "(elapsed_seconds=300.000, deadline_seconds=300)",
            )

        provider = GitRepositorySnapshotProvider(
            Path("cache"),
            deadline_provenance="cli",
            process_runner=process_runner,
        )
        with self.assertRaises(GitRepositoryLineageError) as raised:
            provider._run(Path.cwd(), "fetch", "private-repository")

        self.assertEqual(raised.exception.code, "repository_lineage.fetch_timeout")
        self.assertIn("elapsed_seconds=300.000", raised.exception.message)
        self.assertIn(provider.deadline_policy.identity.uri, raised.exception.message)
        self.assertIn("provenance=cli", raised.exception.message)
        self.assertNotIn("private-repository", raised.exception.message)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_fetch_reaps_transport_descendant_holding_output_streams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = create_repository(
                temporary, "transport-parent", RepositoryParentSelection.root()
            )
            real_git = shutil.which("git")
            assert real_git is not None
            wrapper = temporary / "git-with-transport-descendant"
            wrapper.write_text(
                "#!" + sys.executable + "\n"
                "import subprocess, sys\n"
                f"result = subprocess.run([{real_git!r}, *sys.argv[1:]])\n"
                "if 'fetch' in sys.argv[1:]:\n"
                "    subprocess.Popen("
                "[sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "raise SystemExit(result.returncode)\n",
                encoding="utf-8",
                newline="\n",
            )
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
            provider = GitRepositorySnapshotProvider(
                temporary / "cache", git_binary=str(wrapper)
            )

            resolved = provider.resolve(reference(repository))

            self.assertEqual(resolved.project_id, "transport-parent")

    def test_cli_sources_normalize_local_scp_and_revision_forms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = repository_parent_reference("parent#release", base=root)
        self.assertEqual(local.repository_url, (root / "parent").resolve().as_uri())
        self.assertEqual(local.requested_revision, "release")
        scp = repository_parent_reference("git@example.test:org/parent.git#main")
        self.assertEqual(scp.repository_url, "ssh://git@example.test/org/parent.git")
        self.assertEqual(scp.requested_revision, "main")

    def test_cli_source_rejects_embedded_credentials_without_echoing_them(self) -> None:
        secret = "do-not-repeat"
        with self.assertRaises(GitRepositoryLineageError) as raised:
            repository_parent_reference(
                f"https://user:{secret}@example.test/parent.git"
            )
        self.assertEqual(raised.exception.code, "repository_lineage.source_invalid")
        self.assertNotIn(secret, str(raised.exception))

    def test_reads_a_three_repository_chain_without_checkout_or_hook_execution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            root = create_repository(
                temporary, "root", RepositoryParentSelection.root()
            )
            marker = temporary / "hook-ran"
            hook = root / ".git" / "hooks" / "post-checkout"
            hook.write_text(
                f"#!/bin/sh\nprintf ran > {marker}\n",
                encoding="utf-8",
                newline="\n",
            )
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
            parent = create_repository(
                temporary,
                "parent",
                RepositoryParentSelection.inherit((reference(root),)),
            )
            selected = create_repository(
                temporary,
                "selected",
                RepositoryParentSelection.inherit((reference(parent),)),
            )
            provider = GitRepositorySnapshotProvider(temporary / "cache")

            lineage = resolve_repository_lineage(
                RepositoryParentSelection.inherit((reference(selected),)), provider
            )

            self.assertEqual(
                tuple(item.project_id for item in lineage.nodes),
                ("root", "parent", "selected"),
            )
            self.assertTrue(
                all(len(item.resolved_revision) == 40 for item in lineage.nodes)
            )
            self.assertFalse(marker.exists())
            self.assertFalse(any((temporary / "cache").rglob(".git/index")))

    def test_deadline_policy_does_not_change_exact_revision_custody(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = create_repository(
                temporary, "exact-parent", RepositoryParentSelection.root()
            )
            selected = reference(repository)
            default = GitRepositorySnapshotProvider(
                temporary / "default-cache"
            ).resolve(selected)
            configured = GitRepositorySnapshotProvider(
                temporary / "configured-cache",
                deadline_policy=RepositoryFetchDeadlinePolicy(600, 300, 60),
                deadline_provenance="cli",
            ).resolve(selected)
            expected_revision = git(repository, "rev-parse", "HEAD")

        self.assertEqual(default, configured)
        self.assertEqual(default.resolved_revision, expected_revision)

    def test_missing_parent_authority_fails_instead_of_truncating_the_chain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            incomplete = create_repository(
                temporary,
                "incomplete",
                RepositoryParentSelection.root(),
                include_parent=False,
            )
            provider = GitRepositorySnapshotProvider(temporary / "cache")

            with self.assertRaises(GitRepositoryLineageError) as raised:
                provider.resolve(reference(incomplete))
        self.assertEqual(raised.exception.code, "repository_lineage.authority_missing")

    def test_list_remote_tags_omits_peeled_annotated_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = create_repository(
                temporary, "tagged", RepositoryParentSelection.root()
            )
            git(repository, "tag", "v0.1.0")
            git(repository, "tag", "-a", "v0.2.0", "-m", "annotated")
            git(repository, "tag", "not-a-release")
            provider = GitRepositorySnapshotProvider(temporary / "cache")
            tags = provider.list_remote_tags(repository.resolve().as_uri())

        self.assertIn("v0.1.0", tags)
        self.assertIn("v0.2.0", tags)
        self.assertIn("not-a-release", tags)
        self.assertFalse(any(tag.endswith("^{}") for tag in tags))

    def test_invalid_project_authority_names_the_contract_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = create_repository(
                temporary, "broken", RepositoryParentSelection.root()
            )
            (repository / "literate.project.json").write_text(
                "{not json\n", encoding="utf-8", newline="\n"
            )
            git(repository, "add", "literate.project.json")
            git(repository, "commit", "-m", "Break project authority")
            provider = GitRepositorySnapshotProvider(temporary / "cache")

            with self.assertRaises(GitRepositoryLineageError) as raised:
                provider.resolve(reference(repository))

        self.assertEqual(raised.exception.code, "repository_lineage.authority_invalid")
        self.assertIn("project authority is invalid", raised.exception.message)

    def test_unreachable_repository_fails_with_bounded_locator_free_diagnostics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            missing = temporary / "private-repository-name"
            provider = GitRepositorySnapshotProvider(temporary / "cache")

            with self.assertRaises(GitRepositoryLineageError) as raised:
                provider.resolve(reference(missing))

        self.assertEqual(raised.exception.code, "repository_lineage.fetch_failed")
        self.assertNotIn("private-repository-name", str(raised.exception))
        self.assertIn("deadline_seconds=3600", str(raised.exception))

    def test_reads_regular_catalog_blobs_from_the_locked_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = create_repository(
                temporary, "catalog", RepositoryParentSelection.root()
            )
            component = repository / "components" / "example"
            component.mkdir(parents=True)
            (component / "component.md").write_text(
                "# Example\n", encoding="utf-8", newline="\n"
            )
            (component / "data.bin").write_bytes(b"\x00catalog\n")
            workflow = repository / "workflows" / "example.md"
            workflow.parent.mkdir(parents=True)
            workflow.write_text("# Workflow\n", encoding="utf-8", newline="\n")
            route = repository / "routing" / "example.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8", newline="\n")
            git(repository, "add", ".")
            git(repository, "commit", "-m", "Add catalog")
            provider = GitRepositorySnapshotProvider(temporary / "cache")
            lineage = resolve_repository_lineage(
                RepositoryParentSelection.inherit((reference(repository),)), provider
            )

            resolved = provider.catalog(lineage.nodes[0])

            self.assertEqual(resolved.node, lineage.nodes[0])
            self.assertEqual(
                {item.path: item.content for item in resolved.files},
                {
                    "components/example/component.md": b"# Example\n",
                    "components/example/data.bin": b"\x00catalog\n",
                    "routing/example.json": b"{}\n",
                    "workflows/example.md": b"# Workflow\n",
                },
            )

    @unittest.skipIf(os.name == "nt", "symbolic-link fixture requires Unix Git")
    def test_catalog_symlinks_are_rejected_without_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = create_repository(
                temporary, "unsafe", RepositoryParentSelection.root()
            )
            component = repository / "components" / "unsafe"
            component.mkdir(parents=True)
            (component / "component.md").write_text("# Unsafe\n", encoding="utf-8")
            (component / "link").symlink_to("component.md")
            git(repository, "add", ".")
            git(repository, "commit", "-m", "Add unsafe catalog")
            provider = GitRepositorySnapshotProvider(temporary / "cache")
            lineage = resolve_repository_lineage(
                RepositoryParentSelection.inherit((reference(repository),)), provider
            )

            with self.assertRaises(GitRepositoryLineageError) as raised:
                provider.catalog(lineage.nodes[0])
        self.assertEqual(raised.exception.code, "repository_lineage.catalog_unsafe")

    def test_concurrent_resolve_of_the_same_repository_never_races_the_bare_cache(
        self,
    ) -> None:
        """Issue #324: two processes sharing one object root raced an
        uncoordinated fetch into the same derived bare-cache entry and one
        failed with Git's competing-process lock diagnostic. This proves the
        per-entry lock serializes the fetch instead: `max_active` can only
        be 1 if the two threads' `git fetch` calls never overlapped, and
        both callers still resolve the same, correct revision."""

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = create_repository(
                temporary, "shared", RepositoryParentSelection.root()
            )
            expected_revision = git(repository, "rev-parse", "HEAD")
            probe = _FetchConcurrencyProbe()
            provider = GitRepositorySnapshotProvider(
                temporary / "cache", process_runner=probe
            )
            start = threading.Barrier(2, timeout=10)
            results: list[object] = []
            errors: list[BaseException] = []
            results_lock = threading.Lock()

            def worker() -> None:
                start.wait()
                try:
                    resolved = provider.resolve(reference(repository))
                except BaseException as exc:  # noqa: BLE001 - captured for assertion
                    with results_lock:
                        errors.append(exc)
                else:
                    with results_lock:
                        results.append(resolved)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(probe.fetch_calls, 2)
            self.assertEqual(
                probe.max_active,
                1,
                "concurrent same-entry fetches overlapped instead of serializing",
            )
            for resolved in results:
                self.assertEqual(resolved.resolved_revision, expected_revision)

    def test_concurrent_resolve_of_independent_repositories_stays_concurrent(
        self,
    ) -> None:
        """Different repository identities must not be serialized against one
        another. A rendezvous barrier inside the fetch call can only release
        both threads if their fetches are genuinely in flight at the same
        time; a single cache-wide lock would strand one thread waiting to
        even start its fetch, and the barrier would time out."""

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository_a = create_repository(
                temporary, "independent-a", RepositoryParentSelection.root()
            )
            repository_b = create_repository(
                temporary, "independent-b", RepositoryParentSelection.root()
            )
            fetch_barrier = threading.Barrier(2)
            probe = _BarrierGatedProbe(fetch_barrier)
            provider = GitRepositorySnapshotProvider(
                temporary / "cache", process_runner=probe
            )
            start = threading.Barrier(2, timeout=10)
            results: dict[str, object] = {}
            errors: list[BaseException] = []
            results_lock = threading.Lock()

            def worker(name: str, repository: Path) -> None:
                start.wait()
                try:
                    resolved = provider.resolve(reference(repository))
                except BaseException as exc:  # noqa: BLE001 - captured for assertion
                    with results_lock:
                        errors.append(exc)
                else:
                    with results_lock:
                        results[name] = resolved

            threads = [
                threading.Thread(target=worker, args=("a", repository_a)),
                threading.Thread(target=worker, args=("b", repository_b)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(set(results), {"a", "b"})
            self.assertEqual(
                results["a"].resolved_revision, git(repository_a, "rev-parse", "HEAD")
            )
            self.assertEqual(
                results["b"].resolved_revision, git(repository_b, "rev-parse", "HEAD")
            )

    def _separate_process_resolves(
        self, *, same_repository: bool, read_during_fetch: bool = False
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            contextlib.ExitStack() as stack,
        ):
            temporary = Path(directory)
            repository_a = create_repository(
                temporary, "first", RepositoryParentSelection.root()
            )
            repository_b = (
                repository_a
                if same_repository
                else create_repository(
                    temporary, "second", RepositoryParentSelection.root()
                )
            )
            first_revision = git(repository_a, "rev-parse", "HEAD")
            first_stage = "show" if read_during_fetch else "fetch"
            if read_during_fetch:
                manifest_path = repository_a / "literate.project.json"
                manifest = json.loads(manifest_path.read_bytes())
                manifest["project_id"] = "updated"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                git(repository_a, "add", ".")
                git(repository_a, "commit", "-m", "Change project identity")
            second_revision = git(repository_b, "rev-parse", "HEAD")

            def start(
                name: str, repository: Path, stage: str, revision: str
            ) -> subprocess.Popen[str]:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _RESOLVER_CHILD_SOURCE,
                        SRC_ROOT,
                        str(repository),
                        str(temporary / "cache"),
                        str(temporary),
                        name,
                        stage,
                        revision,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stack.callback(_stop_child, process)
                return process

            def wait_for(marker: str, process: subprocess.Popen[str]) -> None:
                deadline = time.monotonic() + 20
                while not (temporary / marker).exists():
                    if process.poll() is not None:
                        stdout, stderr = process.communicate(timeout=5)
                        self.fail(f"child exited before {marker}: {stdout}\n{stderr}")
                    self.assertLess(time.monotonic(), deadline, marker)
                    time.sleep(0.01)

            first = start("first", repository_a, first_stage, first_revision)
            wait_for(f"first.{first_stage}", first)
            second = start("second", repository_b, "fetch", second_revision)
            wait_for("second.attempt", second)
            if read_during_fetch:
                # The old snapshot's immutable-object read must not hold the
                # mutation lock. Let a new revision finish resolving first.
                wait_for("second.fetch", second)
                (temporary / "second.release").touch()
                stdout, stderr = second.communicate(timeout=30)
                self.assertEqual(second.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)["project_id"], "updated")
                self.assertIsNone(first.poll())
                (temporary / "first.release").touch()
            elif same_repository:
                # Hold the first real fetch while the second process attempts
                # the same OS lock. It must not reach its fetch until release.
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    self.assertIsNone(second.poll())
                    self.assertFalse((temporary / "second.fetch").exists())
                    time.sleep(0.01)
                (temporary / "first.release").touch()
                wait_for("second.fetch", second)
            else:
                # Both fetches must rendezvous before either may finish: a
                # cache-wide lock would fail this bounded positive assertion.
                wait_for("second.fetch", second)
                self.assertIsNone(first.poll())
                (temporary / "first.release").touch()
            (temporary / "second.release").touch()
            for process, repository, revision in (
                (first, repository_a, first_revision),
                (second, repository_b, second_revision),
            ):
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                expected_manifest = json.loads(
                    git(repository, "show", f"{revision}:literate.project.json")
                )
                self.assertEqual(
                    json.loads(stdout),
                    {
                        "revision": revision,
                        "project_id": expected_manifest["project_id"],
                    },
                )

    def test_separate_processes_serialize_same_repository_fetches(self) -> None:
        self._separate_process_resolves(same_repository=True)

    def test_separate_processes_fetch_independent_repositories_concurrently(
        self,
    ) -> None:
        self._separate_process_resolves(same_repository=False)

    def test_resolved_snapshot_read_remains_available_during_another_process_fetch(
        self,
    ) -> None:
        self._separate_process_resolves(same_repository=True, read_during_fetch=True)

    def test_lock_acquisition_failure_identifies_entry_without_credentials(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = GitRepositorySnapshotProvider(Path(directory) / "cache")
            repository_url = "https://user:fixture-password@example.invalid/repo.git"
            key = hashlib.sha256(repository_url.encode("utf-8")).hexdigest()
            with patch(
                "literate_ai.adapters.repository_lineage.exclusive_cache_lock",
                side_effect=CacheLockError("fixture lock acquisition failure"),
            ):
                with self.assertRaises(GitRepositoryLineageError) as raised:
                    with provider._locked_repository(repository_url):
                        self.fail("failed acquisition yielded a repository")
            self.assertEqual(
                raised.exception.code, "repository_lineage.cache_lock_unavailable"
            )
            self.assertIn(key, raised.exception.message)
            self.assertNotIn("fixture-password", raised.exception.message)
            self.assertNotIn(repository_url, raised.exception.message)

    def test_an_interrupted_lock_owner_does_not_leave_an_unrecoverable_lock(
        self,
    ) -> None:
        """A process holding the per-entry lock that is killed outright (no
        graceful shutdown, no `finally` block executed) must not leave a
        permanently stuck lock behind: the OS-level advisory lock is tied to
        the killed process's file descriptor and is released when that
        descriptor closes, so a subsequent resolve should succeed promptly
        rather than exhaust the lock-acquisition timeout."""

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            repository = create_repository(
                temporary, "interrupted", RepositoryParentSelection.root()
            )
            cache_root = (temporary / "cache").resolve()
            cache_root.mkdir()
            selected = reference(repository)
            key = hashlib.sha256(selected.repository_url.encode("utf-8")).hexdigest()
            lock_path = cache_root / f"{key}.lock"
            ready_path = temporary / "lock-holder-ready"

            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _LOCK_HOLDER_CHILD_SOURCE,
                    str(lock_path),
                    str(ready_path),
                    SRC_ROOT,
                ]
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not ready_path.exists():
                    time.sleep(0.02)
                self.assertTrue(
                    ready_path.exists(), "lock holder never acquired the lock"
                )

                # Popen.kill is SIGKILL on POSIX and TerminateProcess on
                # Windows: neither runs the holder's Python finally blocks.
                holder.kill()
                returncode = holder.wait(timeout=15)
                if os.name == "posix":
                    self.assertEqual(returncode, -signal.SIGKILL)
                else:
                    self.assertNotEqual(returncode, 0)

                provider = GitRepositorySnapshotProvider(cache_root)
                started = time.monotonic()
                resolved = provider.resolve(selected)
                elapsed = time.monotonic() - started

                self.assertEqual(
                    resolved.resolved_revision, git(repository, "rev-parse", "HEAD")
                )
                self.assertLess(
                    elapsed,
                    15.0,
                    "resolve waited out the full lock timeout instead of "
                    "recovering the killed owner's lock",
                )
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.wait()


if __name__ == "__main__":
    unittest.main()
