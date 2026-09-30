from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.adapters.intelligence import (
    DurableIntelligenceAdapter,
    IntelligenceAdapterError,
    JsonCommandIntelligenceEngine,
)
from literate_ai.adapters.source import (
    CommandResult,
    GitSourceAdapter,
    GitSourceError,
    SubprocessCommandRunner,
)
from literate_ai.contracts import VerificationResult
from literate_ai.intelligence import (
    EvidenceBudgeter,
    EvidenceBudgetError,
    EvidenceItem,
    EvidenceLocation,
    IndexBinding,
    IntelligenceQuery,
)
from literate_ai.sources import (
    GitSignatureVerification,
    QuarantineError,
    QuarantineStore,
    SourceSnapshotError,
    SourceSnapshotter,
)
from literate_ai.storage import BlobRef, FileSystemCAS


class FakeRunner:
    def __init__(
        self,
        results: Mapping[tuple[str, ...], tuple[int, str, str]],
    ) -> None:
        self.results = dict(results)
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> CommandResult:
        del cwd, timeout_seconds
        self.calls.append(args)
        try:
            returncode, stdout, stderr = self.results[args]
        except KeyError as exc:
            raise AssertionError(f"unexpected command: {args!r}") from exc
        return CommandResult(args, returncode, stdout, stderr)


def git_results(
    root: Path, *, status: str = ""
) -> dict[tuple[str, ...], tuple[int, str, str]]:
    commit = "0123456789abcdef0123456789abcdef01234567"
    return {
        ("git", "rev-parse", "--show-toplevel"): (0, f"{root}\n", ""),
        ("git", "rev-parse", "HEAD"): (0, f"{commit}\n", ""),
        ("git", "symbolic-ref", "--short", "-q", "HEAD"): (0, "main\n", ""),
        (
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ): (0, status, ""),
        ("git", "submodule", "status", "--recursive"): (
            0,
            " abcdef0123456789abcdef0123456789abcdef01 vendor/lib (heads/main)\n",
            "",
        ),
        ("git", "ls-files", "-z", "--cached"): (
            0,
            "tracked.py\0vendor/lib\0",
            "",
        ),
        ("git", "verify-commit", "--raw", commit): (0, "", "good"),
        ("git", "log", "-1", "--format=%G?%x00%GF%x00%GS", commit): (
            0,
            "G\0ABCDEF1234\0Alice Example\n",
            "",
        ),
    }


class SourceSnapshotTests(unittest.TestCase):
    def test_local_snapshot_is_canonical_and_reports_lfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.py").write_text("print('a')\n", encoding="utf-8")
            (root / "asset.bin").write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                f"oid sha256:{'a' * 64}\n"
                "size 123\n",
                encoding="utf-8",
            )
            (root / ".git").mkdir()
            (root / ".git" / "noise").write_text("ignored", encoding="utf-8")
            (root / ".codegraph").mkdir()
            (root / ".codegraph" / "index").write_text("ignored", encoding="utf-8")

            snapshotter = SourceSnapshotter()
            first = snapshotter.snapshot_local(root)
            os.utime(root / "a.py", None)
            second = snapshotter.snapshot_local(root)

            self.assertEqual(first.snapshot_id, second.snapshot_id)
            self.assertEqual(
                [item.path for item in first.snapshot.entries], ["a.py", "asset.bin"]
            )
            self.assertEqual(first.lfs_pointers[0].object_size, 123)

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX executable mode bits")
    def test_local_snapshot_identity_includes_posix_executable_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "a.py"
            source.write_text("print('a')\n", encoding="utf-8")
            snapshotter = SourceSnapshotter()
            regular = snapshotter.snapshot_local(root)

            os.chmod(source, 0o755)
            executable = snapshotter.snapshot_local(root)

            self.assertNotEqual(regular.tree_id, executable.tree_id)

    def test_snapshot_rejects_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "real").write_text("bytes", encoding="utf-8")
            (root / "link").symlink_to(root / "real")
            with self.assertRaises(SourceSnapshotError):
                SourceSnapshotter().snapshot_local(root)

    def test_aggregate_identity_is_order_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            one = root / "one"
            two = root / "two"
            one.mkdir()
            two.mkdir()
            (one / "one.txt").write_text("one", encoding="utf-8")
            (two / "two.txt").write_text("two", encoding="utf-8")
            snapshotter = SourceSnapshotter()
            one_capture = snapshotter.snapshot_local(one)
            two_capture = snapshotter.snapshot_local(two)
            first = snapshotter.aggregate(
                (("two", "deps/two", two_capture), ("one", "deps/one", one_capture))
            )
            second = snapshotter.aggregate(
                (("one", "deps/one", one_capture), ("two", "deps/two", two_capture))
            )
            self.assertEqual(first.snapshot_id, second.snapshot_id)


class GitAndQuarantineTests(unittest.TestCase):
    def _capture(self, root: Path, *, status: str = ""):
        runner = FakeRunner(git_results(root, status=status))
        adapter = GitSourceAdapter(runner)
        return adapter, runner, adapter.capture(root)

    def test_git_capture_and_signed_commit_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
            adapter, _runner, capture = self._capture(root)

            self.assertFalse(capture.git.dirty if capture.git else True)
            self.assertEqual(capture.git.submodules[0].state, "clean")
            self.assertEqual(capture.snapshot.entries[-1].path, "vendor/lib")
            verification = adapter.verify_signed_commit(root, capture)
            self.assertTrue(verification.verified)
            self.assertEqual(verification.signer, "Alice Example")

    def test_dirty_git_capture_is_never_covered_by_commit_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tracked.py").write_text("VALUE = 2\n", encoding="utf-8")
            adapter, runner, capture = self._capture(root, status=" M tracked.py\0")
            verification = adapter.verify_signed_commit(root, capture)
            self.assertEqual(verification.result, VerificationResult.REJECTED)
            self.assertIn("dirty", verification.reason)
            self.assertNotIn(
                ("git", "verify-commit", "--raw", capture.git.head_commit),
                runner.calls,
            )

    def test_quarantine_promotes_only_exact_verified_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
            adapter, _runner, capture = self._capture(source)
            verification = adapter.verify_signed_commit(source, capture)
            cas = FileSystemCAS(base / "cas")
            store = QuarantineStore(base / "intake", cas)
            record = store.materialize(capture, source)
            trusted = store.promote(record, verification)
            trusted.verify()
            self.assertTrue(cas.contains(trusted.decision_ref))

            wrong = GitSignatureVerification(
                source_snapshot_id=f"sha256:{'f' * 64}",
                source_tree_id=capture.tree_id,
                commit=capture.git.head_commit,
                result=VerificationResult.VERIFIED,
                signer="Alice",
                key_fingerprint="ABC",
                trust_code="G",
                verifier="git:test",
                detail_digest=f"sha256:{'e' * 64}",
            )
            with self.assertRaises(QuarantineError):
                store.promote(record, wrong)

    def test_existing_legacy_quarantine_record_is_semantically_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
            _adapter, _runner, capture = self._capture(source)
            store = QuarantineStore(base / "intake", FileSystemCAS(base / "cas"))
            first = store.materialize(capture, source)
            target = first.tree_path.parent
            marker = target / "record.json"
            legacy = json.loads(marker.read_text(encoding="utf-8"))
            legacy["schema"] = "urn:literate-ai:schema:v1:quarantine-record"
            os.chmod(target, 0o700)
            os.chmod(marker, 0o600)
            marker.write_text(
                json.dumps(legacy, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(marker, 0o400)
            os.chmod(target, 0o500)

            second = store.materialize(capture, source)

            self.assertEqual(second.capture, first.capture)
            second.verify()

    def test_aggregate_quarantine_faults_in_exact_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            one = base / "one"
            two = base / "two"
            one.mkdir()
            two.mkdir()
            (one / "one.txt").write_text("one", encoding="utf-8")
            (two / "two.txt").write_text("two", encoding="utf-8")
            snapshotter = SourceSnapshotter()
            one_capture = snapshotter.snapshot_local(one)
            two_capture = snapshotter.snapshot_local(two)
            aggregate = snapshotter.aggregate(
                (("one", "one", one_capture), ("two", "two", two_capture))
            )
            store = QuarantineStore(base / "intake", FileSystemCAS(base / "cas"))
            records = {
                "one": store.materialize(one_capture, one),
                "two": store.materialize(two_capture, two),
            }
            record = store.materialize_aggregate(aggregate, records)
            record.verify()
            self.assertEqual((record.tree_path / "one" / "one.txt").read_text(), "one")


@dataclass
class StructuredEngine:
    provider_id: str = "test-intelligence"
    provider_version: str = "1.0"
    empty: bool = False

    def ensure_index(
        self, source_path: Path, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.source_path = source_path
        return {
            "source_snapshot_id": request["source_snapshot_id"],
            "source_tree_id": request["source_tree_id"],
            "index_key": "engine-index-1",
            "document_count": 1,
            "symbols": 1,
        }

    def query(
        self, engine_index_key: str, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.engine_index_key = engine_index_key
        evidence = (
            []
            if self.empty
            else [
                {
                    "kind": "symbol-source",
                    "summary": "Definition and caller",
                    "content": "def run():\n    return 1",
                    "locations": [
                        {
                            "path": "tracked.py",
                            "start_line": 1,
                            "end_line": 2,
                            "symbol": "run",
                        }
                    ],
                    "call_path": ["main", "run"],
                }
            ]
        )
        return {
            "source_snapshot_id": request["source_snapshot_id"],
            "index_id": request["index_id"],
            "query_id": request["query_id"],
            "evidence": evidence,
        }


class IntelligenceTests(unittest.TestCase):
    def _trusted(self, base: Path):
        source = base / "source"
        source.mkdir()
        (source / "tracked.py").write_text(
            "def run():\n    return 1\n", encoding="utf-8"
        )
        runner = FakeRunner(git_results(source))
        git = GitSourceAdapter(runner)
        capture = git.capture(source)
        verification = git.verify_signed_commit(source, capture)
        cas = FileSystemCAS(base / "cas")
        store = QuarantineStore(base / "intake", cas)
        trusted = store.promote(store.materialize(capture, source), verification)
        return trusted, cas

    def test_structured_results_are_exact_bound_and_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted, cas = self._trusted(Path(temporary))
            engine = StructuredEngine()
            adapter = DurableIntelligenceAdapter(engine, cas)
            stored_index = adapter.ensure_index(trusted, {"language": "python"})
            query = IntelligenceQuery.create(
                stored_index.binding,
                text="Where is run called?",
                purpose="code-generation-context",
                required=True,
            )
            result = adapter.query(stored_index, query)

            self.assertEqual(result.evidence[0].call_path, ("main", "run"))
            self.assertTrue(cas.contains(stored_index.binding_ref))
            self.assertTrue(cas.contains(result.normalized_result_ref))
            self.assertTrue(all(cas.contains(item) for item in result.evidence_refs))

            other_binding = IndexBinding.create(
                source_snapshot_id=f"sha256:{'1' * 64}",
                source_tree_id=stored_index.binding.source_tree_id,
                provider_id="other",
                provider_version="1",
                configuration_id="config",
                engine_index_key="key",
                artifact_ref=BlobRef("2" * 64, 0),
                document_count=0,
            )
            wrong_query = IntelligenceQuery.create(
                other_binding,
                text="wrong source",
                purpose="test",
            )
            with self.assertRaises(IntelligenceAdapterError):
                adapter.query(stored_index, wrong_query)

    def test_required_query_cannot_silently_return_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trusted, cas = self._trusted(Path(temporary))
            adapter = DurableIntelligenceAdapter(StructuredEngine(empty=True), cas)
            index = adapter.ensure_index(trusted, {})
            query = IntelligenceQuery.create(
                index.binding,
                text="required fact",
                purpose="generation",
                required=True,
            )
            with self.assertRaises(IntelligenceAdapterError):
                adapter.query(index, query)

    def test_command_engine_rejects_human_output(self) -> None:
        runner = FakeRunner({})
        engine = JsonCommandIntelligenceEngine(
            provider_id="fixture-intelligence",
            provider_version="1",
            binary="fixture-intelligence",
            runner=runner,
        )
        request = {"source_snapshot_id": "snapshot", "source_tree_id": "tree"}
        source = Path("/tmp/source")
        args = (
            "fixture-intelligence",
            "index",
            "--format=json",
            "--source",
            str(source),
            "--request-json",
            '{"source_snapshot_id":"snapshot","source_tree_id":"tree"}',
        )
        runner.results[args] = (0, "Index ready: 12 symbols", "")
        with self.assertRaises(IntelligenceAdapterError):
            engine.ensure_index(source, request)

    def test_budget_protects_required_and_prioritizes_selected(self) -> None:
        artifact = BlobRef("a" * 64, 0)
        index = IndexBinding.create(
            source_snapshot_id="source",
            source_tree_id="tree",
            provider_id="test",
            provider_version="1",
            configuration_id="config",
            engine_index_key="key",
            artifact_ref=artifact,
            document_count=1,
        )

        def evidence(text: str, *, required: bool, selected: bool) -> EvidenceItem:
            query = IntelligenceQuery.create(
                index,
                text=text,
                purpose="budget",
                required=required,
                selected=selected,
            )
            return EvidenceItem.create(
                query,
                kind="source",
                summary=text,
                content=text * 8,
                locations=(EvidenceLocation("a.py", 1, 1),),
            )

        required = evidence("required", required=True, selected=False)
        unselected = evidence("global", required=False, selected=False)
        selected = evidence("selected", required=False, selected=True)
        limit = required.encoded_size + selected.encoded_size
        budget = EvidenceBudgeter(limit).apply((required, unselected, selected))
        self.assertEqual(
            {item.evidence_id for item in budget.included},
            {required.evidence_id, selected.evidence_id},
        )
        with self.assertRaises(EvidenceBudgetError):
            EvidenceBudgeter(required.encoded_size - 1).apply((required,))


class SubprocessCommandRunnerProcessTreeTests(unittest.TestCase):
    """Regression coverage for issue #79: `SubprocessCommandRunner.run` (the
    location the issue names directly) previously used bare
    `subprocess.run(timeout=...)`, which kills only the direct `git`
    process on timeout, leaving any helper tree (e.g. a hook or a
    credential-helper daemon) it spawned alive."""

    def test_successful_command_returns_captured_output(self) -> None:
        runner = SubprocessCommandRunner()
        with tempfile.TemporaryDirectory() as temporary:
            result = runner.run(
                (sys.executable, "-c", "print('hello')"),
                cwd=Path(temporary),
                timeout_seconds=10,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "hello")

    def test_timeout_raises_git_source_error(self) -> None:
        runner = SubprocessCommandRunner()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(GitSourceError):
                runner.run(
                    (sys.executable, "-c", "import time; time.sleep(60)"),
                    cwd=Path(temporary),
                    timeout_seconds=1,
                )

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_timeout_kills_the_whole_process_tree_not_just_the_direct_child(
        self,
    ) -> None:
        """Reproducer from the issue: a wrapper (standing in for `git` with
        a hook) that daemonizes a sleeper, then the runner times out. The
        sleeper must not remain."""

        runner = SubprocessCommandRunner()
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "grandchild.pid"
            grandchild_script = Path(temporary) / "grandchild.py"
            grandchild_script.write_text(
                "import os, time\n"
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            parent_script = Path(temporary) / "parent.py"
            parent_script.write_text(
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, {str(grandchild_script)!r}])\n"
                "time.sleep(60)\n"
            )

            with self.assertRaises(GitSourceError):
                runner.run(
                    (sys.executable, str(parent_script)),
                    cwd=Path(temporary),
                    timeout_seconds=1,
                )

            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "grandchild never started")
            grandchild_pid = int(pid_file.read_text().strip())

            deadline = time.monotonic() + 5
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(
                alive, "grandchild survived timeout; only the direct child was killed"
            )


if __name__ == "__main__":
    unittest.main()
