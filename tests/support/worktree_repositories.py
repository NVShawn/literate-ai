"""Isolated real-Git repositories for worktree custody tests."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class WorktreeRepositoryFixture:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls._seed_repository = Path(temporary.name).resolve() / "seed"
        cls.initialize(cls._seed_repository)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "project"
        shutil.copytree(self._seed_repository, self.root)

    @staticmethod
    def git(root, *arguments):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
        )
        return subprocess.run(
            (
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                # The seed is copied after commit returns. Detached maintenance
                # can remove a lock while copytree is enumerating its objects.
                "-c",
                "maintenance.auto=false",
                "-c",
                "gc.auto=0",
                *arguments,
            ),
            cwd=root,
            env=environment,
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout

    @classmethod
    def initialize(cls, path):
        path.mkdir()
        cls.git(path, "init", "-q")
        (path / "source").write_bytes(b"original source\n")
        cls.git(path, "add", "source")
        cls.git(path, "commit", "-qm", "initial")

    def linked(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.root, "worktree", "add", "-q", "--detach", str(path), "HEAD")
        return path
