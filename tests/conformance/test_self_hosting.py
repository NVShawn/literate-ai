from __future__ import annotations

import hashlib
import os
import runpy
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.self_hosting import (
    FrozenPackageTree,
    SelfHostError,
    run_snapshot_replication,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "src" / "literate_ai"
SAMPLE_ROOT = REPO_ROOT / "samples" / "self-hosting"
COMPONENT_PATH = SAMPLE_ROOT / "component.md"
PROOF_HARNESS = (
    REPO_ROOT / "tests" / "conformance" / "support" / "self_hosting_proof.py"
)
HARNESS_ROOT = REPO_ROOT / "samples" / "_harness" / "self-hosting"
PROOF_MANIFEST = HARNESS_ROOT / "conformance" / "snapshot-replication.json"
SKILLS_MANIFEST = HARNESS_ROOT / "skills" / "self-hosting.json"


def platform_flavor() -> Path:
    import sys

    if sys.platform.startswith("linux"):
        host_os = "linux"
    elif sys.platform == "darwin":
        host_os = "macos"
    elif sys.platform in {"win32", "cygwin"}:
        host_os = "windows"
    else:
        raise AssertionError(f"unsupported test platform: {sys.platform}")
    return REPO_ROOT / "flavors" / f"os-{host_os}" / "flavor.md"


def sample_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(SAMPLE_ROOT.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(SAMPLE_ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def self_host_temporary_directory() -> tempfile.TemporaryDirectory[str]:
    # Nested snapshot trees on Windows can exceed MAX_PATH in the default
    # per-runner temporary directory. The proof owns this disposable root, so
    # place it directly on the current drive there.
    directory = Path(tempfile.gettempdir()).anchor if os.name == "nt" else None
    return tempfile.TemporaryDirectory(prefix="litai-self-host-", dir=directory)


class SnapshotReplicationConformanceTests(unittest.TestCase):
    def test_isolated_candidate_timeout_terminates_descendant_process_tree(
        self,
    ) -> None:
        harness = runpy.run_path(str(PROOF_HARNESS))
        run_candidate = harness["_run"]
        globals_ = run_candidate.__globals__
        descendant = (
            "import time; print('descendant-ready', flush=True); time.sleep(60)"
        )
        parent = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
            "time.sleep(60)"
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(globals_, {"_CANDIDATE_PROCESS_TIMEOUT_SECONDS": 0.2}):
                with self.assertRaisesRegex(
                    harness["SelfHostSampleError"], "could not complete"
                ):
                    run_candidate(
                        [sys.executable, "-c", parent],
                        environment=dict(os.environ),
                        cwd=Path(temporary),
                    )
        self.assertLess(time.monotonic() - started, 5)

    def test_two_replays_are_stable_twice_from_clean_state(self) -> None:
        execute_proof = runpy.run_path(str(PROOF_HARNESS))["execute_replication_proof"]
        before = sample_digest()
        reports = []
        for _attempt in range(2):
            with self_host_temporary_directory() as temporary:
                with mock.patch(
                    "socket.create_connection",
                    side_effect=AssertionError(
                        "snapshot-replication proof must remain offline"
                    ),
                ):
                    reports.append(
                        execute_proof(
                            repo_root=REPO_ROOT,
                            sample_root=SAMPLE_ROOT,
                            scratch=Path(temporary),
                            host_build_acknowledged=True,
                        )
                    )
                    self.assertFalse((Path(temporary) / "cg").exists())
        self.assertEqual(reports[0], reports[1])
        report = reports[0]
        self.assertEqual(report["replication_count"], 2)
        self.assertEqual(
            report["classification"], "deterministic-offline-conformance-replay"
        )
        self.assertFalse(report["coding_cli_involved"])
        self.assertTrue(report["complete_file_set_stable"])
        self.assertFalse(report["original_import_fallback"])
        stable = report["stable_replication"]
        self.assertEqual(
            stable["classification"], "deterministic-offline-conformance-replay"
        )
        self.assertIn("security_classification", stable)
        self.assertEqual(
            stable["lifecycle_step_ids"],
            [
                "validate",
                "classify",
                "authorize-build",
                "build",
                "resolve-dependencies",
                "test-generated",
                "verify-independent",
                "prepare-tree",
                "commit-tree",
            ],
        )
        self.assertTrue(stable["validation"]["passed"])
        self.assertEqual(stable["authorization"]["profile"], "yolo")
        self.assertEqual(
            stable["build"]["compiled_file_count"], stable["python_file_count"]
        )
        self.assertTrue(stable["generated_tests"]["passed"])
        self.assertEqual(stable["generated_tests"]["test_count"], 3)
        self.assertTrue(stable["acceptance"]["passed"])
        self.assertEqual(
            stable["acceptance"]["execution_profile"],
            "non-executing-exact-tree",
        )
        self.assertEqual(
            stable["acceptance"]["suite_artifact_identity"],
            stable["generated_tests"]["test_suite_identity"],
        )
        for key in (
            "source_snapshot_id",
            "specification_id",
            "effective_revision_id",
            "accepted_tree_id",
        ):
            self.assertRegex(stable[key], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(sample_digest(), before)

    def test_missing_source_unpinned_replay_skill_and_original_fallback_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            copied_package = root / "literate_ai"
            shutil.copytree(
                PACKAGE_ROOT,
                copied_package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            (copied_package / "self_hosting.py").unlink()
            with self.assertRaisesRegex(SelfHostError, "lacks required files"):
                FrozenPackageTree.capture(copied_package)

            shutil.rmtree(copied_package)
            shutil.copytree(
                PACKAGE_ROOT,
                copied_package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            with self.assertRaisesRegex(SelfHostError, "not imported from the package"):
                run_snapshot_replication(
                    source_root=copied_package,
                    component_path=COMPONENT_PATH,
                    skills_path=SKILLS_MANIFEST,
                    proof_path=PROOF_MANIFEST,
                    platform_flavor_path=platform_flavor(),
                    runtime_root=root / "fallback-runtime",
                )

            tampered_skills = root / "self-hosting.json"
            tampered_skills.write_bytes(SKILLS_MANIFEST.read_bytes() + b"\n")
            with self.assertRaisesRegex(SelfHostError, "does not match"):
                run_snapshot_replication(
                    source_root=PACKAGE_ROOT,
                    component_path=COMPONENT_PATH,
                    skills_path=tampered_skills,
                    proof_path=PROOF_MANIFEST,
                    platform_flavor_path=platform_flavor(),
                    runtime_root=root / "tampered-runtime",
                )


if __name__ == "__main__":
    unittest.main()
