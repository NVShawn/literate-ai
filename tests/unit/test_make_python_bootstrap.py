from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
FINDER = REPO_ROOT / "scripts" / "find-compatible-python.sh"
SESSION_KEY = REPO_ROOT / "scripts" / "build_session_key.py"


def fake_python(path: Path, *, compatible: bool) -> None:
    path.write_text(
        "#!/bin/sh\n"
        + (
            'case "$*" in *build_session_key.py*) echo make-123;; esac\nexit 0\n'
            if compatible
            else "exit 1\n"
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def concurrent_fake_python(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *build_session_key.py*) printf '%s\\n' \"$LITAI_SESSION_ID\";;\n"
        "  *'-m venv'*)\n"
        "    destination=''\n"
        "    for argument do destination=$argument; done\n"
        '    mkdir -p "$destination/bin"\n'
        '    cp "$0" "$destination/bin/python"\n'
        '    chmod +x "$destination/bin/python";;\n'
        "  *--session-probe*) printf '%s\\n' \"$LITAI_SESSION_ID\";;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def isolated_make_environment() -> dict[str, str]:
    """Keep an outer Make invocation from rewriting nested Make test semantics."""

    environment = dict(os.environ)
    for name in (
        "BUILD_DIR",
        "OBJ_DIR",
        "PYTHON",
        "PYTHON_ENV",
        "LITAI_SESSION_ID",
        "MAKEFLAGS",
        "MAKEOVERRIDES",
    ):
        environment.pop(name, None)
    return environment


class MakePythonBootstrapTests(unittest.TestCase):
    def test_bootstrap_installs_skill_evaluator_before_validation(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        declaration = re.search(r"^bootstrap:\s+([^#\n]+)", makefile, re.MULTILINE)
        self.assertIsNotNone(declaration)
        prerequisites = declaration.group(1).split()
        self.assertIn("skill-evaluator-install", prerequisites)
        self.assertLess(
            prerequisites.index("skill-evaluator-install"),
            prerequisites.index("validate"),
        )

    def test_python_resolver_reports_compatible_interpreter(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "python_resolver.py"),
                "--check",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"compatible": true', result.stdout)
        self.assertIn('"executable":', result.stdout)

    @unittest.skipUnless(os.name == "posix", "uses a POSIX incompatible fixture")
    def test_python_resolver_rejects_nonconforming_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "python3"
            candidate.write_text(
                "#!/bin/sh\nexit 1\n",
                encoding="utf-8",
            )
            candidate.chmod(0o755)
            result = subprocess.run(
                [
                    str(candidate),
                    str(REPO_ROOT / "scripts" / "python_resolver.py"),
                    "--check",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_session_key_is_safe_and_operator_override_is_validated(self) -> None:
        default = subprocess.run(
            [sys.executable, str(SESSION_KEY)],
            capture_output=True,
            text=True,
            check=False,
            env={
                name: value
                for name, value in os.environ.items()
                if name != "LITAI_SESSION_ID"
            },
        )
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertRegex(default.stdout.strip(), r"^make-[0-9]+$")

        invalid_environment = dict(os.environ)
        invalid_environment["LITAI_SESSION_ID"] = "../../shared"
        invalid = subprocess.run(
            [sys.executable, str(SESSION_KEY)],
            capture_output=True,
            text=True,
            check=False,
            env=invalid_environment,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(invalid.stdout, "")

    def test_default_environment_is_scoped_beneath_object_build_directory(self) -> None:
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is unavailable")
        environment = isolated_make_environment()
        environment["LITAI_SESSION_ID"] = "parallel-agent-a"

        check = subprocess.run(
            [make, "-n", "python-check"],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(check.returncode, 0, check.stderr)
        expected = REPO_ROOT / "_build" / "python-envs" / "parallel-agent-a"
        self.assertIn(f'-m venv "{expected.as_posix()}"', check.stdout)
        self.assertNotIn('-m venv ".venv"', check.stdout)
        self.assertIn(
            f'-X pycache_prefix="{(REPO_ROOT / "_build" / "pycache").as_posix()}"',
            check.stdout,
        )

        clean = subprocess.run(
            [make, "-n", "clean"],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertIn("scripts/clean_cache_directories.py", clean.stdout)
        self.assertNotIn(" -m venv ", clean.stdout)
        self.assertNotIn(str(expected / "bin" / "python"), clean.stdout)

    def test_nested_make_environment_discards_outer_make_and_python_overrides(
        self,
    ) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PYTHON": "/outer/python",
                "PYTHON_ENV": "/outer/python-env",
                "BUILD_DIR": "/outer/generated",
                "OBJ_DIR": "/outer/build",
                "MAKEFLAGS": " -- PYTHON=/outer/python",
                "MAKEOVERRIDES": "PYTHON=/outer/python",
            },
        ):
            environment = isolated_make_environment()

        for name in (
            "BUILD_DIR",
            "OBJ_DIR",
            "PYTHON",
            "PYTHON_ENV",
            "MAKEFLAGS",
            "MAKEOVERRIDES",
        ):
            self.assertNotIn(name, environment)

    def test_default_targets_bootstrap_an_ignored_environment_from_system_python(
        self,
    ) -> None:
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            environment = isolated_make_environment()
            python_environment = Path(temporary) / "python-env"

            check = subprocess.run(
                [
                    make,
                    "-n",
                    f"PYTHON_ENV={python_environment}",
                    "python-check",
                ],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertIn(f'-m venv "{python_environment}"', check.stdout)
            managed_python = python_environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            # GNU Make renders ``abspath`` values with forward slashes on Windows.
            # Keep the assertion about the selected managed interpreter without
            # confusing that shell-safe spelling with a different path.
            managed_python_command = managed_python.as_posix()
            self.assertIn(
                f'"{managed_python_command}" -m pip install -e .', check.stdout
            )
            self.assertIn(
                f'PYTHONPATH=src "{managed_python_command}" '
                "scripts/litai_step.py --name python-check -- "
                f'"{managed_python_command}" '
                "scripts/run_checkpointed_unittests.py --state ",
                check.stdout,
            )
            self.assertIn("--start-directory tests", check.stdout)
            contributor_install = (
                f'"{managed_python_command}" -m pip install -e ".[dev]"'
            )
            self.assertIn(contributor_install, check.stdout)
            self.assertLess(
                check.stdout.index(contributor_install),
                check.stdout.index("scripts/run_checkpointed_unittests.py"),
            )

            for target in ("repository-layout-check", "skills-check"):
                standalone = subprocess.run(
                    [make, "-n", f"PYTHON_ENV={python_environment}", target],
                    cwd=REPO_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(standalone.returncode, 0, standalone.stderr)
                self.assertIn(f'-m venv "{python_environment}"', standalone.stdout)
                self.assertIn(
                    f'"{managed_python_command}" scripts/litai_step.py --name {target}',
                    standalone.stdout,
                )

            reset = subprocess.run(
                [
                    make,
                    "-n",
                    f"PYTHON_ENV={python_environment}",
                    "release-check-reset",
                ],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(reset.returncode, 0, reset.stderr)
            self.assertIn(f'-m venv "{python_environment}"', reset.stdout)
            self.assertEqual(
                reset.stdout.count(
                    f'"{managed_python_command}" scripts/run_checkpointed_gates.py'
                ),
                2,
            )
            self.assertNotIn(
                f'"{sys.executable}" scripts/run_checkpointed_gates.py',
                reset.stdout,
            )

            help_result = subprocess.run(
                [make, "-n", f"PYTHON_ENV={python_environment}", "help"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertNotIn("-m venv", help_result.stdout)

    def test_recursive_make_reuses_the_parent_python_environment(self) -> None:
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            harness = Path(temporary) / "Makefile"
            harness.write_text(
                f"include {REPO_ROOT / 'Makefile'}\n"
                "show-recursive-environment:\n"
                f'\t@$(MAKE) --no-print-directory -s -f "{harness}" '
                'parent-environment="$(PYTHON_ENV)" show-child-environment\n'
                "show-child-environment:\n"
                "\t@echo parent=$(parent-environment)\n"
                "\t@echo child=$(PYTHON_ENV)\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [make, "-s", "-f", str(harness), "show-recursive-environment"],
                cwd=REPO_ROOT,
                env=isolated_make_environment(),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(
                line.split("=", 1)
                for line in result.stdout.splitlines()
                if line.startswith(("parent=", "child="))
            )
            self.assertEqual(values["parent"], values["child"])
            self.assertIn("/_build/python-envs/make-", values["parent"])

    @unittest.skipUnless(os.name == "posix", "concurrent bootstrap uses POSIX fixtures")
    def test_concurrent_sessions_bootstrap_and_clean_without_stomping(self) -> None:
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is unavailable")
        from literate_ai.cache_directories import ensure_cache_directory

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "tools"
            tools.mkdir()
            python = tools / "python3"
            concurrent_fake_python(python)
            object_root = root / "objects"
            ensure_cache_directory(
                object_root,
                kind="object",
                project_root=REPO_ROOT,
            )
            base_environment = isolated_make_environment()
            base_environment["PATH"] = os.pathsep.join((str(tools), "/usr/bin", "/bin"))
            base_environment["OBJ_DIR"] = str(object_root)
            base_environment["BUILD_DIR"] = str(root / "generated")

            processes = []
            for session in ("parallel-agent-a", "parallel-agent-b"):
                environment = dict(base_environment)
                environment["LITAI_SESSION_ID"] = session
                processes.append(
                    (
                        session,
                        environment,
                        subprocess.Popen(
                            [make, "--no-print-directory", "dev-install"],
                            cwd=REPO_ROOT,
                            env=environment,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        ),
                    )
                )

            interpreters: dict[str, Path] = {}
            for session, environment, process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                interpreter = object_root / "python-envs" / session / "bin" / "python"
                probe = subprocess.run(
                    [str(interpreter), "--session-probe"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)
                self.assertEqual(probe.stdout.strip(), session)
                interpreters[session] = interpreter

            transient = object_root / "artifacts" / "sample"
            transient.parent.mkdir()
            transient.write_bytes(b"artifact")
            clean = subprocess.run(
                [
                    make,
                    "--no-print-directory",
                    f"PYTHON={sys.executable}",
                    f"OBJ_DIR={object_root}",
                    f"BUILD_DIR={root / 'generated'}",
                    "clean",
                ],
                cwd=REPO_ROOT,
                env=isolated_make_environment(),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertFalse(transient.exists())
            for session, interpreter in interpreters.items():
                self.assertTrue(interpreter.is_file(), session)

    def test_node_tool_gates_stage_the_pinned_tool_closure_first(self) -> None:
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            object_root = Path(temporary) / "objects"
            for target, invocation in (
                ("openspec-check", "openspec validate"),
                ("documentation-check", "run documentation:check"),
            ):
                with self.subTest(target=target):
                    result = subprocess.run(
                        [
                            make,
                            "-n",
                            f"PYTHON={sys.executable}",
                            f"OBJ_DIR={object_root}",
                            target,
                        ],
                        cwd=REPO_ROOT,
                        env=isolated_make_environment(),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    stage = result.stdout.index("scripts/stage_node_tool.py")
                    install = result.stdout.index("npm", stage)
                    execute = result.stdout.index(invocation, install)
                    self.assertLess(stage, install)
                    self.assertLess(install, execute)

    def test_node_tool_browser_cache_is_isolated_beneath_object_root(self) -> None:
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            object_root = Path(temporary) / "objects"
            result = subprocess.run(
                [
                    make,
                    "-pn",
                    f"PYTHON={sys.executable}",
                    f"OBJ_DIR={object_root}",
                    "tools-install",
                ],
                cwd=REPO_ROOT,
                env=isolated_make_environment(),
                capture_output=True,
                text=True,
                check=False,
            )

        expected = str(object_root / "tools" / "puppeteer").replace("\\", "/")
        self.assertIn(
            f"PUPPETEER_CACHE_DIR := {expected}",
            result.stdout.replace("\\", "/"),
        )

    @unittest.skipUnless(os.name == "posix", "finder fixture requires a POSIX shell")
    def test_path_directory_order_precedes_command_name_preference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            fake_python(first / "python3", compatible=False)
            fake_python(first / "python", compatible=True)
            fake_python(second / "python3", compatible=True)

            result = subprocess.run(
                ["/bin/sh", str(FINDER)],
                env={"PATH": os.pathsep.join((str(first), str(second)))},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(first / "python"))

    @unittest.skipUnless(os.name == "posix", "finder fixture requires a POSIX shell")
    def test_python3_wins_within_one_path_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_python(root / "python3", compatible=True)
            fake_python(root / "python", compatible=True)

            result = subprocess.run(
                ["/bin/sh", str(FINDER)],
                env={"PATH": str(root)},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(root / "python3"))

    @unittest.skipUnless(os.name == "posix", "finder fixture requires a POSIX shell")
    def test_semicolon_path_and_windows_executable_suffix_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            fake_python(first / "python3.exe", compatible=False)
            fake_python(first / "python.exe", compatible=True)
            fake_python(second / "python3.exe", compatible=True)

            result = subprocess.run(
                ["/bin/sh", str(FINDER)],
                env={"PATH": f"{first};{second}"},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(first / "python.exe"))

    @unittest.skipUnless(os.name == "posix", "finder fixture requires a POSIX shell")
    def test_make_uses_discovered_python_and_explicit_python_fails_closed(self) -> None:
        make = shutil.which("make")
        if make is None:
            self.skipTest("make is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            incompatible = first / "python3"
            compatible = second / "python"
            fake_python(incompatible, compatible=False)
            fake_python(compatible, compatible=True)
            harness = root / "Makefile"
            harness.write_text(
                f"include {REPO_ROOT / 'Makefile'}\n"
                "show-python:\n\t@printf '%s\\n' \"$(PYTHON)\"\n",
                encoding="utf-8",
            )
            environment = isolated_make_environment()
            environment["PATH"] = os.pathsep.join((str(first), str(second)))

            discovered = subprocess.run(
                [make, "-s", "-f", str(harness), "show-python"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(discovered.returncode, 0, discovered.stderr)
            self.assertEqual(discovered.stdout.strip(), str(compatible))

            environment["PYTHON"] = str(incompatible)
            explicit = subprocess.run(
                [make, "-s", "python-version"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(explicit.returncode, 0)

            environment["PYTHON"] = ""
            empty = subprocess.run(
                [make, "-s", "python-version"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(empty.returncode, 0)
            self.assertIn("PYTHON is explicitly empty", empty.stderr)


if __name__ == "__main__":
    unittest.main()
