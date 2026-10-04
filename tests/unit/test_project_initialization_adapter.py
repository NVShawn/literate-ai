"""Focused tests for the public filesystem project-initialization adapter."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter as DefaultParentProjectInitializationAdapter,
)
from literate_ai.adapters.project_initialization import (
    ProjectInitializationError,
    apply_init_flavor_defaults,
    host_platform_selector,
)
from literate_ai.adapters.project_initialization import (
    initialize_project as _initialize_project,
)
from literate_ai.adapters.project_validation import validate_project
from literate_ai.adapters.repository_lineage import GitRepositorySnapshotProvider
from literate_ai.contracts import (
    ProjectInitializationBaseline,
    ProjectInitializationOrigin,
    RepositoryFetchDeadlinePolicy,
    RepositoryParentSelection,
    StandardLanguageCommandProfile,
)
from tests.unit.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)

DEFAULT_TEST_FLAVORS = ("+bazel", "+python", "+macos")


def initialize_project(*args, **kwargs):
    kwargs.setdefault("parent_selection", RepositoryParentSelection.root())
    return _initialize_project(*args, **kwargs)


class FilesystemProjectInitializationAdapterTests(unittest.TestCase):
    @staticmethod
    def _origin() -> ProjectInitializationOrigin:
        return ProjectInitializationOrigin(
            repository_url="ssh://git.example.test/operator/literate-ai.git",
            git_revision="a" * 40,
            distribution_name="literate-ai",
            distribution_version="0.2.0",
        )

    def test_initialization_preserves_git_metadata_and_existing_readme(self) -> None:
        origin = self._origin()
        readme = b"# Existing repository\n\nOperator-owned introduction.\n"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "repository"
            target.mkdir()
            subprocess.run(
                ("git", "-C", str(target), "init", "--quiet"),
                check=True,
                capture_output=True,
            )
            git_sentinel = target / ".git" / "litai-preserved"
            git_sentinel.write_bytes(b"repository metadata\n")
            (target / "README.md").write_bytes(readme)

            result = FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: origin,
            ).initialize(
                target,
                flavor_selectors=DEFAULT_TEST_FLAVORS,
                source_intelligence_provider="none",
            )
            baseline = ProjectInitializationBaseline.from_dict(
                json.loads(
                    (target / ".literate/initialization-baseline.json").read_text(
                        encoding="utf-8"
                    )
                )
            )

            self.assertEqual((target / "README.md").read_bytes(), readme)
            self.assertEqual(git_sentinel.read_bytes(), b"repository metadata\n")
            self.assertTrue((target / "literate.project.json").is_file())
            self.assertNotIn("README.md", result["created"])
            self.assertNotIn("README.md", {item.path for item in baseline.files})

    def test_credential_bearing_repository_origin_is_never_persistable(self) -> None:
        for repository_url in (
            "https://token@example.test/operator/literate-ai.git",
            "https://example.test/operator/literate-ai.git?access_token=secret",
            "ssh://user:password@example.test/operator/literate-ai.git",
        ):
            with self.subTest(repository_url=repository_url):
                with self.assertRaisesRegex(ValueError, "must not contain credentials"):
                    ProjectInitializationOrigin(
                        repository_url,
                        "a" * 40,
                        "literate-ai",
                        "0.2.0",
                    )

    def test_cli_delegates_and_exposes_complete_initialization_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = GitRepositorySnapshotProvider(
                Path(directory) / "repository-lineage",
                deadline_policy=RepositoryFetchDeadlinePolicy(),
                deadline_provenance="framework-default",
            )
            deadline = provider.deadline_evidence

        self.assertEqual(deadline["provenance"], "framework-default")
        self.assertEqual(
            deadline["policy"],
            {
                "schema": "urn:literate-ai:schema:v1:repository-fetch-deadline-policy",
                "total_seconds": 3600,
                "no_progress_seconds": 600,
                "connect_seconds": 30,
            },
        )

    def test_initialized_project_packages_every_supported_language_closure(
        self,
    ) -> None:
        expected = {
            "python": (
                "python-portable-application",
                "python",
                "python-tree",
                "python",
            ),
            "javascript": (
                "javascript-portable-json-application",
                "node",
                "javascript-tree",
                "javascript",
            ),
            "rust": (
                "rust-portable-json-application",
                "rust",
                "rust-executable",
                "rust",
            ),
            "cpp": (
                "cpp17-portable-json-application",
                "cpp",
                "cpp-executable",
                "cpp",
            ),
            "swift": (
                "swift-portable-json-application",
                "swift",
                "swift-executable",
                "swift",
            ),
            "typescript": (
                "typescript-portable-application",
                "node",
                "typescript-tree",
                "lang-typescript",
            ),
            "zig": (
                "zig-portable-application",
                "zig",
                "zig-executable",
                "lang-zig",
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "language-closures"
            initialize_project(
                target,
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=(
                    "+python",
                    "+javascript",
                    "+rust",
                    "+cpp",
                    "+swift",
                    "+swift-apple",
                    "+typescript",
                    "+zig",
                    "+bazel",
                    "+macos",
                ),
            )
            validation = validate_project(
                target,
                require_authority_review=True,
                include_test_receipt=False,
                synchronize_source_intelligence=False,
            )

            for language, (
                skill_id,
                toolchain,
                build_strategy,
                _flavor_dir,
            ) in expected.items():
                with self.subTest(language=language):
                    flavor_path = target / "flavors" / f"lang-{language}" / "flavor.md"
                    flavor = parse_flavor_markdown(
                        flavor_path.read_bytes(), source=flavor_path.as_posix()
                    )
                    self.assertEqual(
                        (flavor.name, flavor.target), (f"lang-{language}", language)
                    )
                    self.assertEqual(len(flavor.authoring_inputs), 1)
                    self.assertEqual(
                        (
                            flavor.authoring_inputs[0].kind,
                            flavor.authoring_inputs[0].uri,
                        ),
                        (
                            "specification-to-source-skill",
                            f"../../skills/specification-to-source/{skill_id}/SKILL.md",
                        ),
                    )
                    language_profiles = [
                        item
                        for item in flavor.contributions
                        if item.slot == "standard-language-command"
                    ]
                    self.assertEqual(len(language_profiles), 1)
                    self.assertEqual(
                        language_profiles[0].content.uri,
                        "standard-command-profile.json",
                    )
                    profile_path = flavor_path.parent / "standard-command-profile.json"
                    profile = StandardLanguageCommandProfile.from_dict(
                        json.loads(profile_path.read_text(encoding="utf-8"))
                    )
                    self.assertEqual(profile.target, language)
                    self.assertEqual(profile.toolchain, toolchain)
                    self.assertEqual(profile.build_strategy.value, build_strategy)
                    self.assertEqual(
                        profile.cpp_library_kind,
                        "static" if language == "cpp" else None,
                    )
                    self.assertTrue(
                        (
                            target
                            / "skills"
                            / "specification-to-source"
                            / skill_id
                            / "SKILL.md"
                        ).is_file()
                    )

        validated_skills = {
            item["skill_id"] for item in validation["specification_to_source_skills"]
        }
        self.assertTrue(
            {skill_id for skill_id, _, _, _ in expected.values()}.issubset(
                validated_skills
            )
        )

    def test_init_cli_applies_smart_defaults_when_no_flavor_given(self) -> None:
        selectors = apply_init_flavor_defaults([])

        self.assertIn("+flavor://literate-ai/lang-python", selectors)
        self.assertIn("+flavor://literate-ai/build-make", selectors)
        self.assertIn("+flavor://literate-ai/package-pip", selectors)
        self.assertIn(host_platform_selector(), selectors)
        self.assertNotIn("+flavor://literate-ai/build-bazel", selectors)


class ConvertModeTests(unittest.TestCase):
    @staticmethod
    def _origin() -> ProjectInitializationOrigin:
        return ProjectInitializationOrigin(
            repository_url="ssh://git.example.test/operator/literate-ai.git",
            git_revision="b" * 40,
            distribution_name="literate-ai",
            distribution_version="0.2.0",
        )

    def test_convert_parent_preflight_failure_leaves_repository_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing-project"
            target.mkdir()
            tracked = target / "Makefile"
            tracked.write_text("all:\n\t@true\ntest:\n\t@true\n", encoding="utf-8")
            subprocess.run(
                ("git", "-C", str(target), "init", "--quiet"),
                check=True,
                capture_output=True,
            )
            for key, value in (
                ("user.email", "test@example.test"),
                ("user.name", "Test"),
            ):
                subprocess.run(
                    ("git", "-C", str(target), "config", key, value),
                    check=True,
                    capture_output=True,
                )
            subprocess.run(
                ("git", "-C", str(target), "add", "Makefile"),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ("git", "-C", str(target), "commit", "--quiet", "-m", "baseline"),
                check=True,
                capture_output=True,
            )
            untracked = target / "operator-notes.txt"
            untracked.write_text("preserve me\n", encoding="utf-8")
            tracked_before = tracked.read_bytes()
            untracked_before = untracked.read_bytes()

            status_before = subprocess.run(
                ("git", "-C", str(target), "status", "--porcelain=v1", "-z"),
                check=True,
                capture_output=True,
            ).stdout
            with mock.patch(
                "literate_ai.adapters.project_initialization."
                "_default_repository_parent",
                side_effect=ProjectInitializationError(
                    "repository_lineage.tags_unavailable",
                    "framework release tags are unavailable",
                ),
            ):
                with self.assertRaises(ProjectInitializationError) as raised:
                    DefaultParentProjectInitializationAdapter(
                        initialization_origin_provider=lambda: self._origin(),
                        standard_binding_provider=lambda: None,
                    ).initialize(
                        target,
                        source_intelligence_provider="none",
                        convert=True,
                    )

            status_after = subprocess.run(
                ("git", "-C", str(target), "status", "--porcelain=v1", "-z"),
                check=True,
                capture_output=True,
            ).stdout
            self.assertEqual(
                raised.exception.code, "repository_lineage.tags_unavailable"
            )
            self.assertEqual(status_after, status_before)
            self.assertEqual(tracked.read_bytes(), tracked_before)
            self.assertEqual(untracked.read_bytes(), untracked_before)
            self.assertFalse((target / "_legacy").exists())
            self.assertFalse((target / "literate.project.json").exists())

    def test_convert_quarantines_existing_tree_into_unique_aside_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing-project"
            target.mkdir()
            legacy_layout = {
                "AGENTS.md": "# Project agents\n",
                "CHANGELOG.md": "# My changelog\n",
                ".gitignore": "build/\n*.pyc\n",
                "skills/config.yaml": "schema: 1\n",
                "src/main.py": "print('hello')\n",
                "docs/notes/design.md": "# Design\n",
            }
            for relative, content in sorted(legacy_layout.items()):
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            subprocess.run(
                ("git", "-C", str(target), "init", "--quiet"),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ("git", "-C", str(target), "add", "-A"),
                check=True,
                capture_output=True,
            )
            for arguments in (
                ("config", "user.email", "test@example.test"),
                ("config", "user.name", "Test"),
            ):
                subprocess.run(
                    ("git", "-C", str(target), *arguments),
                    check=True,
                    capture_output=True,
                )
            subprocess.run(
                ("git", "-C", str(target), "commit", "--quiet", "-m", "baseline"),
                check=True,
                capture_output=True,
                env={**os.environ, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z"},
            )

            result = FilesystemProjectInitializationAdapter(
                initialization_origin_provider=lambda: self._origin(),
                standard_binding_provider=lambda: None,
            ).initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )

            # Phase 0 recorded one exact quarantine; Phase 1.2 subsequently moved
            # that hierarchy intact into first-class Component authority and removed
            # the temporary _legacy directory only after parity passed.
            quarantine = result["convert_quarantine"]
            self.assertIsNotNone(quarantine)
            aside = target / quarantine["directory"]
            self.assertFalse(aside.exists())
            self.assertTrue(result["lift_shift"]["quarantine_removed"])
            implementation = target / result["lift_shift"]["implementation_directory"]
            self.assertTrue(implementation.is_dir())
            self.assertEqual(
                quarantine["directory"].split("/")[0],
                "_legacy",
            )
            self.assertEqual(len(quarantine["moved"]), len(legacy_layout))
            moved_names = {item["from"] for item in quarantine["moved"]}
            self.assertEqual(
                moved_names,
                {"AGENTS.md", "CHANGELOG.md", ".gitignore", "skills", "src", "docs"},
            )
            for relative, content in legacy_layout.items():
                self.assertEqual(
                    (implementation / relative).read_text(encoding="utf-8"), content
                )
            # Operator-owned entries must not reappear at the root; scaffold-managed
            # names (AGENTS.md, .gitignore, CHANGELOG.md, skills/, docs/) are freshly
            # stamped from the template.
            for relative in ("src", "skills/config.yaml", "docs/notes/design.md"):
                self.assertFalse((target / relative).exists())
            self.assertTrue((target / ".gitignore").is_file())
            stamped = (target / ".gitignore").read_text()
            self.assertNotIn("build/", stamped.replace("_build/", ""))
            self.assertNotIn("*.pyc", stamped)
            self.assertNotEqual(
                (target / "CHANGELOG.md").read_text(encoding="utf-8"),
                "# My changelog\n",
            )

            # Git-tracked entries were moved with git mv (rename detection applies).
            git_vcs = {item["from"]: item["vcs"] for item in quarantine["moved"]}
            self.assertEqual(git_vcs["src"], "git")
            status = subprocess.run(
                ("git", "-C", str(target), "status", "--porcelain", "-M"),
                check=True,
                capture_output=True,
                text=True,
            )
            renamed = [
                line
                for line in status.stdout.splitlines()
                if line.startswith("R  ") or line.startswith("R ")
            ]
            self.assertTrue(renamed)

            # The scaffold stamped a clean tree: no operator content remains at root.
            self.assertTrue((target / "literate.project.json").is_file())
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertFalse((target / "samples" / "hello-component").exists())
            self.assertNotIn(".git", moved_names)
            self.assertTrue((target / ".git").is_dir())
            self.assertEqual(result["detected_languages"], ["python"])

    def test_convert_resumes_interrupted_scaffold_without_requarantine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "partial-convert"
            target.mkdir()
            (target / ".literate").mkdir()
            (target / ".literate" / "initialization-baseline.json").write_text(
                "{}\n", encoding="utf-8"
            )
            leftover = target / "legacy-module.py"
            leftover.write_text("VALUE = 1\n", encoding="utf-8")
            (target / "Makefile").write_text(
                "all:\n\t@true\ntest:\n\t@true\n", encoding="utf-8"
            )
            FilesystemProjectInitializationAdapter(
                initialization_origin_provider=lambda: self._origin(),
                standard_binding_provider=lambda: None,
            ).initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )
            self.assertTrue((target / "literate.project.json").is_file())
            self.assertTrue(leftover.is_file())
            self.assertFalse((target / "_legacy").exists())


if __name__ == "__main__":
    unittest.main()
