"""Real Git custody fixtures for read-only orchestration inventory."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import repository_orchestration as orchestration
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.builders.python import BuildError


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "-c",
            "commit.gpgsign=false",
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            "-c",
            "core.autocrlf=false",
            *arguments,
        ),
        check=True,
        capture_output=True,
        timeout=20,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout


def repository(root: Path, *, sha256: bool = False) -> None:
    root.mkdir()
    git(
        root,
        "init",
        "-q",
        "-b",
        "main",
        *(("--object-format=sha256",) if sha256 else ()),
    )
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.test")
    git(root, "config", "core.autocrlf", "false")
    (root / "source.txt").write_text("original\n", encoding="utf-8", newline="\n")
    git(root, "add", "source.txt")
    git(root, "commit", "-q", "-m", "initial")


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


class GitFixturePolicyTests(unittest.TestCase):
    def test_fixture_clone_preserves_bytes_under_a_crlf_host_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            root = base / "original"
            write_text = Path.write_text

            def windows_text_defaults(
                path, data, encoding=None, errors=None, newline=None
            ):
                return write_text(
                    path,
                    data,
                    encoding=encoding,
                    errors=errors,
                    newline="\r\n" if newline is None else newline,
                )

            # Exercise Windows text defaults on every host, before Git preserves bytes.
            with patch.object(Path, "write_text", windows_text_defaults):
                repository(root)
            self.assertEqual((root / "source.txt").read_bytes(), b"original\n")
            self.assertEqual(git(root, "show", "HEAD:source.txt"), b"original\n")
            configuration = base / "global.gitconfig"
            configuration.write_text("[core]\nautocrlf = true\n", encoding="utf-8")
            original = configuration.read_bytes()
            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(configuration)}):
                clone = base / "clone"
                git(base, "clone", "--depth", "1", root.as_uri(), str(clone))
                self.assertEqual((clone / "source.txt").read_bytes(), b"original\n")
            self.assertEqual(configuration.read_bytes(), original)

    def test_fixture_commands_disable_automatic_maintenance_without_config_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fixture"
            repository(root)
            git(root, "config", "maintenance.auto", "true")
            git(root, "config", "gc.auto", "1")
            before = snapshot(root)
            self.assertEqual(
                git(root, "config", "--get", "maintenance.auto"), b"false\n"
            )
            self.assertEqual(git(root, "config", "--get", "gc.auto"), b"0\n")
            self.assertEqual(
                git(root, "config", "--local", "--get", "maintenance.auto"), b"true\n"
            )
            self.assertEqual(git(root, "config", "--local", "--get", "gc.auto"), b"1\n")
            self.assertEqual(snapshot(root), before)


class RepositoryOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "super"
        repository(self.root)
        self.pin = git(self.root, "rev-parse", "HEAD").decode().strip()

    def test_document_reader_allows_distinct_stable_ctime_clocks(self):
        path = self.root / "document.json"
        path.write_bytes(b"bounded\r\nfixture\r\n")
        fstat = os.fstat

        def metadata(descriptor):
            node = fstat(descriptor)
            if not stat.S_ISREG(node.st_mode):
                return node
            return self.with_ctime(node, node.st_ctime_ns + 2_000_000_000)

        with patch.object(orchestration.os, "fstat", side_effect=metadata):
            self.assertEqual(
                orchestration._read_document(path), b"bounded\r\nfixture\r\n"
            )

    @staticmethod
    def with_ctime(node, value):
        return SimpleNamespace(
            **{
                field: getattr(node, field)
                for field in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
            },
            st_ctime_ns=value,
            st_file_attributes=getattr(node, "st_file_attributes", 0),
        )

    def test_document_reader_refuses_descriptor_clock_drift(self):
        path = self.root / "document.json"
        path.write_bytes(b"fixture")
        fstat = os.fstat
        count = 0

        def metadata(descriptor):
            nonlocal count
            node = fstat(descriptor)
            if not stat.S_ISREG(node.st_mode):
                return node
            count += 1
            return self.with_ctime(node, node.st_ctime_ns + count * 2_000_000_000)

        with (
            patch.object(orchestration.os, "fstat", side_effect=metadata),
            self.assertRaises(orchestration.OrchestrationInventoryError),
        ):
            orchestration._read_document(path)
        self.assertEqual(path.read_bytes(), b"fixture")

    def test_document_reader_refuses_path_clock_drift(self):
        path = self.root / "document.json"
        path.write_bytes(b"fixture")
        lstat = Path.lstat
        count = 0

        def metadata(candidate, *args, **kwargs):
            nonlocal count
            node = lstat(candidate, *args, **kwargs)
            if candidate != path:
                return node
            count += 1
            return self.with_ctime(node, node.st_ctime_ns + (count > 1) * 2_000_000_000)

        with (
            patch.object(Path, "lstat", new=metadata),
            self.assertRaises(orchestration.OrchestrationInventoryError),
        ):
            orchestration._read_document(path)
        self.assertEqual(path.read_bytes(), b"fixture")

    def modules(self, text: str) -> None:
        (self.root / ".gitmodules").write_text(text, encoding="utf-8")
        git(self.root, "add", ".gitmodules")

    def uninitialized(self, *, url: str = "../child.git", branch: str | None = None):
        self.modules(
            '[submodule "child.name"]\n\tpath = children/one\n\turl = '
            + url
            + "\n"
            + ("\tbranch = " + branch + "\n" if branch is not None else "")
        )
        git(
            self.root,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000",
            self.pin,
            "children/one",
        )

    def initialized(self):
        child = self.base / "child"
        repository(child)
        git(
            self.root,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(child),
            "children/one",
        )
        git(self.root, "commit", "-q", "-am", "child")
        return self.root / "children/one"

    def refusal(self, code: str | None = None):
        before = snapshot(self.root)
        with self.assertRaises(orchestration.OrchestrationInventoryError) as caught:
            orchestration.inspect_gitlink_inventory(self.root)
        if code is not None:
            self.assertEqual(caught.exception.code, "orchestration." + code)
        self.assertEqual(snapshot(self.root), before)
        return caught.exception

    def test_exact_uninitialized_inventory_is_immutable_read_only_and_repeatable(self):
        self.uninitialized(branch=".")
        before = snapshot(self.root)
        result = orchestration.inspect_gitlink_inventory(self.root)
        self.assertEqual(
            result.children,
            (
                orchestration.GitlinkObservation(
                    "child.name",
                    "children/one",
                    "../child.git",
                    self.pin,
                    ".",
                    "uninitialized",
                    None,
                ),
            ),
        )
        self.assertTrue(result.gitmodules_identity.startswith("sha256:"))
        self.assertEqual(
            result.identity, orchestration.inspect_gitlink_inventory(self.root).identity
        )
        self.assertEqual(snapshot(self.root), before)
        with self.assertRaises(FrozenInstanceError):
            result.children[0].commit = "changed"
        wire = result.to_dict()
        wire["children"].clear()
        self.assertEqual(len(result.children), 1)

    def test_empty_repository_has_no_invented_children(self):
        before = snapshot(self.root)
        self.assertEqual(
            orchestration.inspect_gitlink_inventory(self.root).children, ()
        )
        self.assertEqual(snapshot(self.root), before)

    def test_absorbed_child_reports_head_separately_from_pin(self):
        child = self.initialized()
        self.assertTrue((child / ".git").is_file())
        pinned = git(child, "rev-parse", "HEAD").decode().strip()
        git(child, "config", "user.name", "Test")
        git(child, "config", "user.email", "test@example.test")
        (child / "source.txt").write_text("new child revision\n", encoding="utf-8")
        git(child, "commit", "-q", "-am", "advance child only")
        head = git(child, "rev-parse", "HEAD").decode().strip()
        before = snapshot(self.root)
        result = orchestration.inspect_gitlink_inventory(self.root).children[0]
        self.assertEqual((result.commit, result.checked_out_commit), (pinned, head))
        self.assertNotEqual(pinned, head)
        self.assertEqual(result.state, "initialized")
        self.assertEqual(snapshot(self.root), before)

    def test_deinitialized_child_does_not_resolve_parent_head(self):
        self.initialized()
        git(self.root, "submodule", "deinit", "--force", "children/one")
        before = snapshot(self.root)
        result = orchestration.inspect_gitlink_inventory(self.root).children[0]
        self.assertEqual(
            (result.state, result.checked_out_commit), ("uninitialized", None)
        )
        self.assertEqual(snapshot(self.root), before)

    def test_configured_url_forms_and_named_branch_are_preserved_without_fetch(self):
        for url in (
            "../child.git",
            "git@github.com:example/child.git",
            "https://gitlab.com/example/child.git",
            "ssh://git@gitlab.com/example/child.git",
        ):
            with self.subTest(url=url):
                self.uninitialized(url=url, branch="release/next")
                result = orchestration.inspect_gitlink_inventory(self.root).children[0]
                self.assertEqual((result.url, result.branch), (url, "release/next"))

    def test_working_configuration_drift_refuses(self):
        self.uninitialized()
        with (self.root / ".gitmodules").open("a", encoding="utf-8") as stream:
            stream.write("\tbranch = main\n")
        self.refusal("modules_drift")

    def test_missing_or_unindexed_configuration_refuses(self):
        self.uninitialized()
        git(self.root, "rm", "--cached", "--force", ".gitmodules")
        self.refusal("modules_invalid")
        (self.root / ".gitmodules").unlink()
        self.refusal("modules_invalid")

    def test_malformed_duplicate_include_and_mismatched_configuration_refuse(self):
        self.uninitialized()
        for text in (
            "[malformed\n",
            '[submodule "one"]\npath = children/one\n'
            "url = ../one.git\nurl = ../other.git\n",
            "[include]\npath = ../outside\n",
            '[submodule "one"]\npath = elsewhere\nurl = ../one.git\n',
            '[submodule "one"]\npath = children/one\n',
            '[submodule "one"]\npath = children/one\nurl = ../one.git\n'
            '[submodule "two"]\npath = children/one\nurl = ../two.git\n',
        ):
            with self.subTest(text=text):
                self.modules(text)
                self.refusal()

    def test_portable_alias_and_parent_path_collisions_refuse(self):
        self.uninitialized()
        for second in ("children/ONE", "children/one/nested"):
            with self.subTest(second=second):
                self.modules(
                    '[submodule "one"]\npath = children/one\nurl = ../one.git\n'
                    '[submodule "two"]\npath = ' + second + "\nurl = ../two.git\n"
                )
                self.refusal()

    def test_credential_bearing_urls_refuse_without_echo(self):
        for url in (
            "https://secret-token@example.test/child.git",
            "ssh://git:secret-token@example.test/child.git",
            "https://example.test/child.git?token=secret-token",
            "ext::secret-token",
        ):
            with self.subTest(url=url):
                self.uninitialized(url=url)
                error = self.refusal("modules_invalid")
                self.assertNotIn("secret-token", str(error))

    def test_conflicted_index_refuses(self):
        self.uninitialized()
        git(self.root, "update-index", "--force-remove", "children/one")
        subprocess.run(
            ("git", "-C", str(self.root), "update-index", "--index-info"),
            input=(
                f"160000 {self.pin} 1\tchildren/one\n"
                f"160000 {self.pin} 2\tchildren/one\n"
            ).encode(),
            check=True,
            capture_output=True,
            timeout=20,
        )
        self.refusal("index_invalid")

    def test_ambient_git_repository_and_index_selectors_are_ignored(self):
        self.uninitialized()
        other = self.base / "other"
        repository(other)
        expected = orchestration.inspect_gitlink_inventory(self.root)
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": str(other / ".git"),
                "GIT_WORK_TREE": str(other),
                "GIT_INDEX_FILE": str(other / ".git/index"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.bare",
                "GIT_CONFIG_VALUE_0": "true",
            },
        ):
            self.assertEqual(
                orchestration.inspect_gitlink_inventory(self.root), expected
            )

    def test_symlinked_configuration_and_child_paths_refuse(self):
        self.uninitialized()
        outside = self.base / "outside"
        outside.mkdir()
        try:
            (self.root / "children").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit symlink fixture creation")
        self.refusal()
        (self.root / "children").unlink()
        modules = self.root / ".gitmodules"
        original = modules.read_bytes()
        external = outside / "config"
        external.write_bytes(original)
        modules.unlink()
        modules.symlink_to(external)
        self.refusal("modules_invalid")

    def test_oversized_configuration_refuses_before_git_parsing(self):
        self.uninitialized()
        self.modules("#" + "x" * (256 * 1024))
        self.refusal("modules_invalid")

    def test_unborn_or_broken_initialized_child_refuses(self):
        self.uninitialized()
        child = self.root / "children/one"
        child.mkdir(parents=True)
        git(child, "init", "-q")
        self.refusal("inspection_failed")

    def test_worktree_superproject_and_shallow_clean_clone(self):
        self.initialized()
        worktree = self.base / "worktree"
        git(self.root, "worktree", "add", "--detach", str(worktree), "HEAD")
        result = orchestration.inspect_gitlink_inventory(worktree)
        self.assertEqual(result.children[0].state, "uninitialized")
        clone = self.base / "clone"
        git(self.base, "clone", "--depth", "1", self.root.as_uri(), str(clone))
        self.assertEqual(git(clone, "rev-parse", "--is-shallow-repository"), b"true\n")
        self.assertEqual(orchestration.inspect_gitlink_inventory(clone), result)

    def test_sha256_repository_keeps_full_pin(self):
        root = self.base / "sha256"
        repository(root, sha256=True)
        self.root = root
        self.pin = git(root, "rev-parse", "HEAD").decode().strip()
        self.uninitialized()
        result = orchestration.inspect_gitlink_inventory(root)
        self.assertEqual(result.children[0].commit, self.pin)
        self.assertEqual(len(self.pin), 64)

    def test_child_linked_worktree_keeps_its_independent_git_boundary(self):
        child_source = self.base / "independent"
        repository(child_source)
        self.uninitialized(url=child_source.as_uri())
        child = self.root / "children/one"
        child.parent.mkdir()
        git(child_source, "worktree", "add", "--detach", str(child), "HEAD")
        pin = git(child_source, "rev-parse", "HEAD").decode().strip()
        git(self.root, "update-index", "--cacheinfo", "160000", pin, "children/one")
        before = snapshot(self.base)
        result = orchestration.inspect_gitlink_inventory(self.root).children[0]
        self.assertEqual((result.commit, result.checked_out_commit), (pin, pin))
        self.assertEqual(snapshot(self.base), before)

    def test_nested_submodules_remain_child_owned_and_are_not_flattened(self):
        child = self.initialized()
        grandchild = self.base / "grandchild"
        repository(grandchild)
        git(child, "config", "user.name", "Test")
        git(child, "config", "user.email", "test@example.test")
        git(
            child,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(grandchild),
            "nested",
        )
        git(child, "commit", "-q", "-am", "nested child")
        before = snapshot(self.base)
        result = orchestration.inspect_gitlink_inventory(self.root)
        self.assertEqual([entry.path for entry in result.children], ["children/one"])
        self.assertEqual(snapshot(self.base), before)
        nested = orchestration.inspect_gitlink_inventory(child)
        self.assertEqual([entry.path for entry in nested.children], ["nested"])
        self.assertEqual(snapshot(self.base), before)

    def test_non_repository_subdirectory_cannot_borrow_parent_authority(self):
        child = self.root / "ordinary"
        child.mkdir()
        with self.assertRaises(orchestration.OrchestrationInventoryError):
            orchestration.inspect_gitlink_inventory(child)

    def test_invalid_branch_and_nonportable_paths_refuse(self):
        for branch in ("-option", "refs..bad", "space name"):
            with self.subTest(branch=branch):
                self.uninitialized(branch=branch)
                self.refusal()
        for path in ("../outside", "CON", "children/one."):
            with self.subTest(path=path):
                self.modules(
                    '[submodule "one"]\npath = ' + path + "\nurl = ../one.git\n"
                )
                self.refusal()

    def test_ambiguous_index_metadata_is_not_admitted(self):
        self.uninitialized()
        real = orchestration._git
        for output in (
            b"",
            b"index\nsha1",
            b"index\nextra\nsha1\n",
            b"index\r\nsha1\n",
            b"\nsha1\n",
        ):
            with self.subTest(output=output):

                def inspect(root, *arguments, output=output):
                    if (
                        "--git-path" in arguments
                        and "--show-object-format" in arguments
                    ):
                        return output
                    return real(root, *arguments)

                with patch.object(orchestration, "_git", side_effect=inspect):
                    self.refusal("index_invalid")

    def test_malformed_index_output_is_not_admitted(self):
        self.uninitialized()
        real = orchestration._git
        for output in (
            b"truncated",
            b"160000 invalid 0\tchildren/one\0",
            f"160000 {'0' * 40} 0\tchildren/one\0".encode(),
        ):
            with self.subTest(output=output):

                def inspect(root, *arguments, output=output):
                    return (
                        output if arguments[0] == "ls-files" else real(root, *arguments)
                    )

                with patch.object(orchestration, "_git", side_effect=inspect):
                    self.refusal("index_invalid")

    def test_gitmodules_descriptor_read_detects_replacement(self):
        self.uninitialized()
        real = orchestration.os.open
        modules = self.root / ".gitmodules"

        def replace_before_open(path, flags, *args, **kwargs):
            if path == modules:
                if hasattr(os, "O_NONBLOCK"):
                    self.assertTrue(flags & os.O_NONBLOCK)
                replacement = self.root / "replacement"
                replacement.write_bytes(modules.read_bytes())
                replacement.replace(modules)
            return real(path, flags, *args, **kwargs)

        with patch.object(orchestration.os, "open", side_effect=replace_before_open):
            with self.assertRaises(orchestration.OrchestrationInventoryError) as caught:
                orchestration.inspect_gitlink_inventory(self.root)
        self.assertEqual(caught.exception.code, "orchestration.inputs_changed")

    def test_concurrent_child_change_refuses_instead_of_returning_stale_inventory(self):
        self.uninitialized()
        first = orchestration.inspect_gitlink_inventory(self.root)
        second = replace(first, children=(replace(first.children[0], commit="f" * 40),))
        with patch.object(orchestration, "_observe", side_effect=(first, second)):
            self.refusal("inputs_changed")

    def test_bounded_git_failures_are_typed_and_diagnostics_not_echoed(self):
        for outcome in (
            BuildError("orchestration.git_output_limit", "private diagnostic"),
            BuildError("orchestration.git_timeout", "private diagnostic"),
        ):
            with (
                self.subTest(code=outcome.code),
                patch.object(
                    orchestration,
                    "run_bounded_process",
                    side_effect=outcome,
                ),
            ):
                error = self.refusal("inspection_failed")
                self.assertNotIn("private diagnostic", str(error))
        with patch.object(
            orchestration,
            "run_bounded_process",
            return_value=BoundedProcessResult(
                returncode=1,
                stdout=b"",
                stderr=b"private diagnostic",
            ),
        ):
            self.assertNotIn(
                "private diagnostic", str(self.refusal("inspection_failed"))
            )

    def test_git_transport_and_limits_are_read_only(self):
        self.uninitialized()
        real = orchestration.run_bounded_process
        with patch.object(orchestration, "run_bounded_process", wraps=real) as calls:
            orchestration.inspect_gitlink_inventory(self.root)
        for call in calls.call_args_list:
            command = call.args[0]
            self.assertIn(
                command[4], {"rev-parse", "ls-files", "config", "check-ref-format"}
            )
            self.assertEqual(call.kwargs["timeout_seconds"], 60)
            self.assertEqual(call.kwargs["environment"]["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(call.kwargs["environment"]["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(call.kwargs["environment"]["GIT_NO_REPLACE_OBJECTS"], "1")
            if command[4] == "config":
                self.assertIn("--no-includes", command)
                self.assertIn("--blob", command)
