"""Bounded opt-in project source-intelligence policy tests."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.contracts import (
    ProjectSourceIntelligencePolicy,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
)
from literate_ai.project_source_index import (
    CodeGraphProjectSourceIntelligence,
    ProjectSourceIntelligenceError,
    _CodeGraphCommandEngine,
    _ProjectIndexSnapshot,
    require_lifecycle_project_index,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def disabled_policy() -> ProjectSourceIntelligencePolicy:
    return ProjectSourceIntelligencePolicy(
        provider_id="none",
        command=None,
        minimum_version=None,
        artifact_path=None,
        stages=tuple(
            (stage, SourceIntelligenceMode.OFF) for stage in SourceIntelligenceStage
        ),
        artifact_publication=SourceIntelligenceArtifactPublication.METADATA_ONLY,
    )


def codegraph_policy(
    mode: SourceIntelligenceMode = SourceIntelligenceMode.REQUIRED,
    *,
    minimum_version: str = "1.1.1",
) -> ProjectSourceIntelligencePolicy:
    return ProjectSourceIntelligencePolicy(
        provider_id="codegraph-cli",
        command="codegraph",
        minimum_version=minimum_version,
        artifact_path=".codegraph/codegraph.db",
        stages=tuple((stage, mode) for stage in SourceIntelligenceStage),
        artifact_publication=SourceIntelligenceArtifactPublication.METADATA_ONLY,
    )


class _FakeEngine:
    def __init__(
        self,
        *,
        version: str = "1.1.1",
        error: ProjectSourceIntelligenceError | None = None,
    ) -> None:
        self.version = version
        self.error = error
        self.calls: list[tuple[Path, bool]] = []
        self.preflight_calls: list[Path] = []

    def preflight(self, working_directory: Path) -> tuple[str, str]:
        self.preflight_calls.append(working_directory)
        return self.version, "sha256:" + "1" * 64

    def capture(
        self, project_root: Path, *, synchronize: bool
    ) -> _ProjectIndexSnapshot:
        self.calls.append((project_root, synchronize))
        if self.error is not None:
            raise self.error
        return _ProjectIndexSnapshot(
            self.version,
            "sha256:" + "1" * 64,
            3,
            7,
            5,
            self.version,
            2,
            "sha256:" + "2" * 64,
        )


class ProjectSourceIntelligenceTests(unittest.TestCase):
    def test_distribution_does_not_depend_on_codegraph(self) -> None:
        project = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        dependencies = project["project"].get("dependencies", [])
        optional = project["project"].get("optional-dependencies", {})
        declared = [*dependencies]
        for values in optional.values():
            declared.extend(values)

        self.assertFalse(
            any("codegraph" in dependency.casefold() for dependency in declared)
        )

    @unittest.skipUnless(shutil.which("codegraph"), "codegraph CLI is not installed")
    def test_real_codegraph_sync_and_check_when_provider_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "sample.py").write_text(
                "def answer() -> int:\n    return 42\n", encoding="utf-8"
            )
            engine = _CodeGraphCommandEngine("codegraph", timeout_seconds=60)
            version, _identity = engine.preflight(root)
            provider = CodeGraphProjectSourceIntelligence(
                codegraph_policy(minimum_version=version),
                engine=engine,
            )

            synchronized = provider.sync(root)
            checked = provider.check(root)

        self.assertEqual(synchronized["state"], "current")
        self.assertEqual(checked["state"], "current")
        self.assertGreaterEqual(checked["file_count"], 1)

    def test_default_none_never_constructs_external_provider(self) -> None:
        with mock.patch(
            "literate_ai.project_source_index.CodeGraphProjectSourceIntelligence"
        ) as provider:
            report = require_lifecycle_project_index(
                Path("/project"), disabled_policy()
            )

        provider.assert_not_called()
        self.assertEqual(report["state"], "off")
        self.assertEqual(report["provider_id"], "none")
        self.assertEqual(report["file_count"], 0)
        self.assertEqual(report["node_count"], 0)
        self.assertEqual(report["edge_count"], 0)

    def test_explicit_off_stage_never_constructs_external_provider(self) -> None:
        with mock.patch(
            "literate_ai.project_source_index.CodeGraphProjectSourceIntelligence"
        ) as provider:
            report = require_lifecycle_project_index(
                Path("/project"),
                codegraph_policy(SourceIntelligenceMode.OFF),
            )

        provider.assert_not_called()
        self.assertEqual(report["state"], "off")
        self.assertEqual(report["provider_id"], "codegraph-cli")

    def test_explicit_check_and_sync_return_bounded_observation(self) -> None:
        engine = _FakeEngine()
        provider = CodeGraphProjectSourceIntelligence(codegraph_policy(), engine=engine)

        checked = provider.check(Path("/project with spaces"))
        synchronized = provider.sync(Path("/project with spaces"))

        self.assertEqual(
            engine.calls,
            [
                (Path("/project with spaces"), False),
                (Path("/project with spaces"), True),
            ],
        )
        self.assertEqual(checked, synchronized)
        self.assertEqual(checked["state"], "current")
        self.assertEqual(checked["file_count"], 3)
        self.assertEqual(checked["database_identity"], "sha256:" + "2" * 64)

    def test_minimum_version_fails_closed(self) -> None:
        provider = CodeGraphProjectSourceIntelligence(
            codegraph_policy(), engine=_FakeEngine(version="1.1.0")
        )

        with self.assertRaises(ProjectSourceIntelligenceError) as caught:
            provider.check(Path("/project"))

        self.assertEqual(
            caught.exception.code, "project.source_intelligence_version_unsupported"
        )
        self.assertEqual(provider.engine.calls, [])

    def test_required_lifecycle_propagates_provider_failure(self) -> None:
        error = ProjectSourceIntelligenceError(
            "project.source_intelligence_missing", "index missing"
        )
        with mock.patch(
            "literate_ai.project_source_index.CodeGraphProjectSourceIntelligence.sync",
            side_effect=error,
        ):
            with self.assertRaises(ProjectSourceIntelligenceError) as caught:
                require_lifecycle_project_index(
                    Path("/project"), codegraph_policy(), synchronize=True
                )

        self.assertIs(caught.exception, error)

    def test_preferred_lifecycle_reports_bounded_unavailable_evidence(self) -> None:
        error = ProjectSourceIntelligenceError(
            "project.source_intelligence_missing", "index missing"
        )
        with mock.patch(
            "literate_ai.project_source_index.CodeGraphProjectSourceIntelligence.check",
            side_effect=error,
        ):
            report = require_lifecycle_project_index(
                Path("/project"),
                codegraph_policy(SourceIntelligenceMode.PREFERRED),
                synchronize=False,
            )

        self.assertEqual(report["state"], "unavailable")
        self.assertEqual(report["provider_id"], "codegraph-cli")
        self.assertEqual(report["reason_code"], "project.source_intelligence_missing")

    def test_selected_stage_controls_required_and_off_behavior(self) -> None:
        policy = ProjectSourceIntelligencePolicy(
            provider_id="codegraph-cli",
            command="codegraph",
            minimum_version="1.1.1",
            artifact_path=".codegraph/codegraph.db",
            stages=tuple(
                (
                    stage,
                    (
                        SourceIntelligenceMode.OFF
                        if stage is SourceIntelligenceStage.SOURCE_GENERATION
                        else SourceIntelligenceMode.REQUIRED
                    ),
                )
                for stage in SourceIntelligenceStage
            ),
            artifact_publication=SourceIntelligenceArtifactPublication.METADATA_ONLY,
        )
        with mock.patch(
            "literate_ai.project_source_index.CodeGraphProjectSourceIntelligence"
        ) as provider:
            report = require_lifecycle_project_index(
                Path("/project"),
                policy,
                stage=SourceIntelligenceStage.SOURCE_GENERATION,
            )

        provider.assert_not_called()
        self.assertEqual(report["state"], "off")

    def test_missing_command_has_stable_error_without_installing(self) -> None:
        engine = _CodeGraphCommandEngine("missing-codegraph", timeout_seconds=1)
        with (
            mock.patch(
                "literate_ai.project_source_index.shutil.which", return_value=None
            ),
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(ProjectSourceIntelligenceError) as caught,
        ):
            engine.preflight(Path(directory))

        self.assertEqual(
            caught.exception.code, "project.source_intelligence_command_unavailable"
        )

    def test_stale_and_malformed_status_fail_closed(self) -> None:
        engine = _CodeGraphCommandEngine("codegraph", timeout_seconds=1)
        root = Path("C:/project with spaces")
        status: dict[str, object] = {
            "initialized": True,
            "projectPath": str(root),
            "indexPath": str(root / ".codegraph"),
            "version": "1.1.1",
            "pendingChanges": {"added": 0, "modified": 1, "removed": 0},
            "worktreeMismatch": None,
            "fileCount": 1,
            "nodeCount": 2,
            "edgeCount": 1,
            "index": {
                "builtWithVersion": "1.1.1",
                "builtWithExtractionVersion": 2,
                "currentExtractionVersion": 2,
                "reindexRecommended": False,
            },
        }

        with self.assertRaises(ProjectSourceIntelligenceError) as stale:
            engine._validated_snapshot(
                status,
                project_path=root,
                runtime_version="1.1.1",
                executable_identity="sha256:" + "1" * 64,
                database_identity="sha256:" + "2" * 64,
            )
        self.assertEqual(stale.exception.code, "project.source_intelligence_stale")

        status["pendingChanges"] = {"added": 0, "modified": 0}
        with self.assertRaises(ProjectSourceIntelligenceError) as malformed:
            engine._validated_snapshot(
                status,
                project_path=root,
                runtime_version="1.1.1",
                executable_identity="sha256:" + "1" * 64,
                database_identity="sha256:" + "2" * 64,
            )
        self.assertEqual(
            malformed.exception.code, "project.source_intelligence_status_invalid"
        )

    def test_database_change_during_observation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "codegraph.db"
            database.write_bytes(b"after")
            expected = "sha256:" + hashlib.sha256(b"before").hexdigest()

            with self.assertRaises(ProjectSourceIntelligenceError) as caught:
                _CodeGraphCommandEngine._require_database_unchanged(database, expected)

        self.assertEqual(
            caught.exception.code, "project.source_intelligence_database_changed"
        )

    def test_source_change_during_observation_fails_closed(self) -> None:
        engine = _CodeGraphCommandEngine("codegraph", timeout_seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source.py"
            source.write_text("before\n", encoding="utf-8")
            stat = source.stat(follow_symlinks=False)
            stable = (stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ino)
            identity = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            source.write_text("after\n", encoding="utf-8")

            with (
                mock.patch.object(engine, "_source_paths", return_value=("source.py",)),
                self.assertRaises(ProjectSourceIntelligenceError) as caught,
            ):
                engine._require_source_unchanged(
                    root, (("source.py", stable, identity),)
                )

        self.assertEqual(
            caught.exception.code, "project.source_intelligence_snapshot_changed"
        )

    def test_sqlite_snapshot_supports_uri_metacharacters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            name = "project #%" if os.name == "nt" else "project #%?"
            root = (Path(directory) / name).resolve()
            source = root / ".codegraph" / "codegraph.db"
            source.parent.mkdir(parents=True)
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
            connection.execute("INSERT INTO evidence VALUES ('current')")
            connection.commit()
            connection.close()
            target = root / "snapshot" / "codegraph.db"
            target.parent.mkdir()

            snapshot_identity, live_identity = (
                _CodeGraphCommandEngine._copy_database_snapshot(root, source, target)
            )

            copied = sqlite3.connect(target)
            self.assertEqual(
                copied.execute("SELECT value FROM evidence").fetchone(), ("current",)
            )
            copied.close()
            self.assertTrue(snapshot_identity.startswith("sha256:"))
            self.assertTrue(live_identity.startswith("sha256:"))

    def test_git_inventory_accepts_recurse_submodule_paths(self) -> None:
        engine = _CodeGraphCommandEngine("codegraph", timeout_seconds=1)
        with (
            mock.patch(
                "literate_ai.project_source_index.shutil.which", return_value="git"
            ),
            mock.patch.object(
                engine,
                "_run_git",
                side_effect=(b"vendor/library/source.py\0", b"", b""),
            ) as run_git,
        ):
            paths = engine._git_visible_paths(Path("/project"))

        self.assertEqual(paths, ("vendor/library/source.py",))
        self.assertIn("--recurse-submodules", run_git.call_args_list[0].args)

    def test_non_git_tree_inventories_generated_source_under_build(self) -> None:
        engine = _CodeGraphCommandEngine("codegraph", timeout_seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generated = root / "source" / "build"
            generated.mkdir(parents=True)
            (generated / "bundle.js").write_text(
                "export default 1;\n", encoding="utf-8"
            )
            with mock.patch.object(engine, "_git_visible_paths", return_value=None):
                paths = engine._source_paths(root)
            self.assertIn("source/build/bundle.js", paths)

    def test_command_output_and_timeout_are_bounded_during_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_engine = _CodeGraphCommandEngine("python", timeout_seconds=5)
            timeout_engine = _CodeGraphCommandEngine("python", timeout_seconds=1)
            with mock.patch(
                "literate_ai.project_source_index.shutil.which",
                return_value=sys.executable,
            ):
                with self.assertRaises(ProjectSourceIntelligenceError) as output:
                    output_engine._run(
                        root,
                        "-c",
                        "import sys; sys.stdout.write('x' * (1024 * 1024 + 1))",
                    )
                with self.assertRaises(ProjectSourceIntelligenceError) as timeout:
                    timeout_engine._run(
                        root,
                        "-c",
                        (
                            "import subprocess, sys, time; "
                            "subprocess.Popen([sys.executable, '-c', "
                            "'import time; time.sleep(30)']); "
                            "time.sleep(30)"
                        ),
                    )

        self.assertEqual(
            output.exception.code, "project.source_intelligence_command_output_limit"
        )
        self.assertEqual(
            timeout.exception.code, "project.source_intelligence_command_timeout"
        )
