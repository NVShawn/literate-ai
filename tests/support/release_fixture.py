from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import literate_ai.project_releases as project_releases
from literate_ai.project_releases import (
    create_release_plan,
    prepare_release,
)


class ProjectReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence_environment = patch.dict(
            project_releases.os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

    def git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def write_policy(self, root: Path) -> None:
        policy = {
            "schema": "literate-ai/release-policy@1",
            "version_scheme": "semver",
            "version_authority": {
                "path": "project.json",
                "format": "json-pointer",
                "selectors": ["/version"],
            },
            "version_mirrors": [
                {
                    "path": "version.py",
                    "format": "python-constant",
                    "selectors": ["VERSION"],
                }
            ],
            "allowed_transitions": ["patch", "minor", "major", "explicit"],
            "gate": {
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('project.json').is_file()",
                ],
                "timeout_seconds": 30,
            },
            "changelog": {
                "path": "CHANGELOG.md",
                "unreleased_heading": "## Unreleased",
            },
            "tag": {"prefix": "v", "signed": False},
            "remote": "origin",
            "provider": None,
        }
        (root / "literate.release.json").write_text(
            json.dumps(policy, indent=2) + "\n", encoding="utf-8"
        )

    def write_v2_policy(self, root: Path) -> None:
        self.write_policy(root)
        path = root / "literate.release.json"
        policy = json.loads(path.read_text(encoding="utf-8"))
        policy.update(
            {
                "schema": "literate-ai/release-policy@2",
            }
        )
        path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")

    def initialize(self, parent: Path) -> tuple[Path, Path]:
        root = parent / "project"
        remote = parent / "remote.git"
        root.mkdir()
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.email", "release@example.invalid")
        self.git(root, "config", "user.name", "Release Test")
        (root / "literate.project.json").write_text("{}\n", encoding="utf-8")
        (root / "project.json").write_text(
            json.dumps({"version": "1.2.3"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "version.py").write_text('VERSION = "1.2.3"\n', encoding="utf-8")
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## Unreleased\n\n- Prepared feature.\n",
            encoding="utf-8",
        )
        (root / ".gitignore").write_text(
            "_build/\n.litai-cache-locks/\n", encoding="utf-8"
        )
        self.write_policy(root)
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "initial")
        self.git(parent, "init", "--bare", str(remote))
        self.git(root, "remote", "add", "origin", str(remote))
        self.git(root, "push", "-u", "origin", "main")
        # A newly initialized bare repository may still point HEAD at ``master``.
        # Pin its advertised default branch so clones are independent of the host's
        # init.defaultBranch setting and always check out the commit just pushed.
        self.git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
        return root, remote

    @staticmethod
    def write_record(path: Path, record: dict[str, object]) -> None:
        path.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def _initialize_with_default_branch(self, parent: Path) -> tuple[Path, Path]:
        root, remote = self.initialize(parent)
        policy_path = root / "literate.release.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["default_branch"] = "main"
        policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        self.git(root, "add", "literate.release.json")
        self.git(root, "commit", "-m", "declare main as the default branch")
        self.git(root, "push", "origin", "main")
        return root, remote

    def _initialize_v2_minor_release_line(
        self, parent: Path
    ) -> tuple[Path, Path, SimpleNamespace]:
        root, remote = self.initialize(parent)
        self.git(root, "tag", "v1.2.3")
        self.git(root, "push", "origin", "v1.2.3")
        self.write_v2_policy(root)
        (root / "project.json").write_text(
            json.dumps({"version": "1.3.0"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "version.py").write_text('VERSION = "1.3.0"\n', encoding="utf-8")
        self.git(root, "add", "literate.release.json", "project.json", "version.py")
        self.git(root, "commit", "-m", "Target 1.3")
        self.git(root, "push", "origin", "main")
        self.git(root, "checkout", "-b", "release/1.3.x")
        self.git(root, "push", "-u", "origin", "release/1.3.x")
        project = SimpleNamespace(
            root=root,
            definition=SimpleNamespace(
                repository_policy=SimpleNamespace(
                    default_branch="main",
                    main_state=SimpleNamespace(value="pre-release"),
                    pre_release_version="1.3",
                )
            ),
        )
        return root, remote, project

    def _prepare_v2_minor_release(
        self, parent: Path
    ) -> tuple[Path, Path, SimpleNamespace, Path]:
        root, remote, project = self._initialize_v2_minor_release_line(parent)
        with (
            patch(
                "literate_ai.project_releases.discover_project",
                return_value=project,
            ),
            patch(
                "literate_ai.project_releases._authorize",
                return_value={
                    "actor": "release-admin",
                    "mode": "strict",
                    "reason": None,
                },
            ),
        ):
            plan = create_release_plan(
                root, transition="explicit", explicit_version="1.3.0"
            )
            plan_path = parent / "plan.json"
            self.write_record(plan_path, plan)
            prepare_release(root, plan_path)
        self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
        self.git(root, "commit", "-m", "Prepare 1.3.0")
        return root, remote, project, plan_path

    def _advance_remote_main_after_minor_preparation(self, root: Path) -> None:
        self.git(root, "checkout", "main")
        (root / "future.txt").write_text("future\n", encoding="utf-8")
        self.git(root, "add", "future.txt")
        self.git(root, "commit", "-m", "Advance main after release preparation")
        self.git(root, "push", "origin", "main")
        self.git(root, "checkout", "release/1.3.x")

    def write_policy_with_documentation_authority(self, root: Path) -> None:
        self.write_policy(root)
        policy_path = root / "literate.release.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["documentation_authority"] = ["AUTHORITY.md"]
        policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        (root / "AUTHORITY.md").write_text(
            "<!-- literate-ai:authority-reviewed sha256:" + "0" * 64 + " -->\nstale\n",
            encoding="utf-8",
        )
