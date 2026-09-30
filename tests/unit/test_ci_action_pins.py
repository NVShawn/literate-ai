"""Release CI must not change when a third-party action tag moves."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from fnmatch import fnmatch
from pathlib import Path
from unittest import mock

from literate_ai.contracts.yaml_subset import load_yaml_subset

ROOT = Path(__file__).resolve().parents[2]
USES = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*([^\n#]+)")
PIN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}")


class CiActionPinTests(unittest.TestCase):
    def test_all_external_actions_use_full_commit_ids(self) -> None:
        references = []
        for path in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
            for match in USES.finditer(path.read_text()):
                reference = match[1].strip().strip("\"'")
                if reference.startswith("./"):
                    continue
                references.append(reference)
                with self.subTest(workflow=path.name, reference=reference):
                    self.assertRegex(reference, "^" + PIN.pattern + "$")
        self.assertTrue(references, "pin check must inspect actual action references")

    def test_policy_rejects_tags_short_hashes_and_expressions(self) -> None:
        for reference in (
            "actions/checkout@v6",
            "actions/checkout@abcdef0",
            "${{ matrix.action }}",
        ):
            self.assertIsNone(PIN.fullmatch(reference))

    def test_action_updates_remain_reviewable(self) -> None:
        config = (ROOT / ".github/dependabot.yml").read_text()
        self.assertRegex(config, r'package-ecosystem: ["\']?github-actions["\']?')
        self.assertRegex(config, r'interval: ["\']?weekly["\']?')

    def test_all_required_checks_share_one_native_cancellation_scope(self) -> None:
        workflow = load_yaml_subset((ROOT / ".github/workflows/ci.yml").read_text())
        self.assertEqual(set(workflow["jobs"]), {"checks"})
        job = workflow["jobs"]["checks"]
        self.assertIs(job["strategy"]["fail-fast"], True)
        self.assertIs(job["continue-on-error"], False)
        self.assertIs(workflow["concurrency"]["cancel-in-progress"], True)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(job["name"], "${{ matrix.name }}")
        self.assertEqual(job["runs-on"], "${{ matrix.os }}")
        expected = {("Changed skill admission", "ubuntu-latest")}
        for os_name in ("ubuntu-latest", "macos-latest", "windows-latest"):
            for prefix in ("Documentation", "Sample composition"):
                expected.add((f"{prefix} / {os_name}", os_name))
        for os_name in ("ubuntu-latest", "macos-latest"):
            for python in ("3.11", "3.14"):
                expected.add((f"{os_name} / Python {python}", os_name))
        for group in (2, 3):
            expected.add((f"macos-latest / Python 3.11 ({group} of 3)", "macos-latest"))
        expected.add(
            ("Windows / Python 3.12 / lint, OpenSpec, wheel", "windows-latest")
        )
        for group in (1, 2, 3):
            expected.add(
                (f"Windows / Python 3.12 / tests ({group} of 3)", "windows-latest")
            )
        for os_name in ("ubuntu-latest", "windows-latest"):
            expected.add((f"Native C++ library / {os_name}", os_name))
        rows = job["strategy"]["matrix"]["include"]
        self.assertEqual(len(rows), 19)
        self.assertEqual({(row["name"], row["os"]) for row in rows}, expected)
        for row in rows:
            if row["task"] == "conformance":
                self.assertIn(
                    row["name"],
                    {
                        f"{row['os']} / Python {row['python']}",
                        "macos-latest / Python 3.11 (2 of 3)",
                        "macos-latest / Python 3.11 (3 of 3)",
                    },
                )
            if row["task"] == "windows-tests":
                self.assertIn(f"({row['group']} of 3)", row["name"])

    def test_macos_python311_checkpoint_groups_cover_every_test_module_once(
        self,
    ) -> None:
        workflow = load_yaml_subset((ROOT / ".github/workflows/ci.yml").read_text())
        rows = workflow["jobs"]["checks"]["strategy"]["matrix"]["include"]
        patterns = [
            row["test_pattern"]
            for row in rows
            if row["task"] == "conformance"
            and row["os"] == "macos-latest"
            and row["python"] == "3.11"
        ]
        self.assertEqual(
            patterns,
            ["test_[a-p]*.py", "test_[q-s]*.py", "test_[t-z]*.py"],
        )
        modules = sorted((ROOT / "tests").rglob("test*.py"))
        self.assertTrue(modules)
        for module in modules:
            with self.subTest(module=module.relative_to(ROOT)):
                self.assertEqual(
                    sum(fnmatch(module.name, pattern) for pattern in patterns),
                    1,
                )

    def test_matrix_dispatch_preserves_failure_evidence_and_required_gates(
        self,
    ) -> None:
        workflow = load_yaml_subset((ROOT / ".github/workflows/ci.yml").read_text())
        job = workflow["jobs"]["checks"]
        tasks = {row["task"] for row in job["strategy"]["matrix"]["include"]}
        selected = {task: [] for task in tasks}
        for step in job["steps"]:
            match = re.fullmatch(
                r"matrix.task == '([^']+)'(?: && \((.+)\))?", step["if"]
            )
            self.assertIsNotNone(match, step)
            task, original = match.groups()
            self.assertIn(task, tasks)
            selected[task].append(step)
            if step.get("name") in (
                "Upload release evidence",
                "Upload the profiling trace and hotspot report",
            ):
                self.assertEqual(original, "always()")
            if step.get("name") == "Explain release evidence":
                self.assertEqual(original, "failure()")
            elif "continue-on-error" in step:
                self.assertIs(step["continue-on-error"], False)
        required = {
            "skill-evaluation": "make skills-check PYTHON=python",
            "documentation": "npm --prefix tools/openspec run documentation:check",
            "sample-composition": (
                "test_all_host_recipes_compose_from_specs_and_real_flavors"
            ),
            "conformance": "make validate PYTHON=python RUFF=ruff",
            "windows-gates": "python scripts/wheel_smoke.py",
            "windows-tests": "--splits 3 --group ${{ matrix.group }}",
            "cpp-native": (
                "test_locked_cpp_library_runs_through_complete_filesystem_runtime"
            ),
        }
        self.assertEqual(tasks, set(required))
        for task, command in required.items():
            self.assertTrue(selected[task], task)
            self.assertIn(
                command, "\n".join(str(step.get("run", "")) for step in selected[task])
            )

    def _skill_steps(self) -> list[str]:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        steps = workflow.split("\n      - ")[1:]
        selected = []
        guard = "matrix.task == 'skill-evaluation'"
        for step in steps:
            condition = re.search(r"(?m)^        if: (.+)$", step)
            if condition is None or not condition[1].startswith(guard):
                continue
            if condition[1] == guard:
                step = step[: condition.start()] + step[condition.end() + 1 :]
            else:
                self.assertTrue(condition[1].startswith(guard + " && ("))
                self.assertTrue(condition[1].endswith(")"))
                original = condition[1][len(guard + " && (") : -1]
                step = (
                    step[: condition.start()]
                    + "        if: "
                    + original
                    + step[condition.end() :]
                )
            selected.append(step)
        self.assertTrue(selected, "the matrix must execute skill admission")
        return selected

    def _uv_probe(self) -> str:
        steps = self._skill_steps()
        probe = next(
            step for step in steps if "name: Verify pinned uv bootstrap" in step
        )
        self.assertNotRegex(probe, r"(?m)^        (?:if|continue-on-error):")
        self.assertIn("shell: bash", probe)
        command = textwrap.dedent(probe.split("run: |\n", 1)[1]).strip()
        lines = command.splitlines()
        self.assertEqual(lines[0], "python - <<'PY'")
        self.assertEqual(lines[-1], "PY")
        return "\n".join(lines[1:-1])

    def test_framework_is_installed_before_skill_detection(self) -> None:
        steps = self._skill_steps()
        bootstrap = next(
            (
                step
                for step in steps
                if "name: Install framework for skill detection" in step
            ),
            None,
        )
        self.assertIsNotNone(bootstrap)
        self.assertNotRegex(bootstrap, r"(?m)^        (?:if|continue-on-error):")
        self.assertIn("run: python -m pip install -e .", bootstrap)
        self.assertLess(
            steps.index(bootstrap),
            next(i for i, step in enumerate(steps) if "id: changed-skills" in step),
        )

    @unittest.skipUnless(shutil.which("bash"), "workflow regression requires Bash")
    def test_actual_skill_detection_shell_propagates_errors_before_classifying(
        self,
    ) -> None:
        step = next(
            step for step in self._skill_steps() if "id: changed-skills" in step
        )
        self.assertNotRegex(step, r"(?m)^        (?:if|continue-on-error):")
        self.assertIn("shell: bash", step)
        command = textwrap.dedent(step.split("run: |\n", 1)[1]).strip()
        for event, before, base in (
            ("pull_request", "push-base", "pr-base"),
            ("push", "push-base", "push-base"),
            ("push", "0" * 40, "default-branch-revision"),
        ):
            selected = command.replace("${{ github.event_name }}", event)
            selected = selected.replace(
                "${{ github.event.pull_request.base.sha }}", "pr-base"
            )
            selected = selected.replace("${{ github.event.before }}", before)
            selected = selected.replace(
                "${{ github.event.repository.default_branch }}", "main"
            )
            for status, output in (
                (0, ""),
                (0, "skills/cargo\n"),
                (23, ""),
                (23, "partial\n"),
            ):
                with self.subTest(event=event, status=status, output=output):
                    with tempfile.TemporaryDirectory() as temporary:
                        result_file = Path(temporary) / "output"
                        # Execute the actual workflow body; replace only its external
                        # Git/Python processes, including failures with partial stdout.
                        stub = (
                            "git() {\n"
                            '  [[ "$*" == "rev-parse --verify '
                            'refs/remotes/origin/main^{commit}" ]] || return 96\n'
                            '  printf "%s\\n" "default-branch-revision"\n'
                            "}\n"
                            "python() {\n"
                            '  [[ "$*" == "scripts/evaluate_changed_skills.py --base '
                            + base
                            + ' --list" ]] || return 97\n'
                            '  printf "%s" "$LITAI_TEST_SKILL_OUTPUT"\n'
                            '  return "$LITAI_TEST_SKILL_STATUS"\n'
                            "}\n"
                        )
                        result = subprocess.run(
                            [
                                shutil.which("bash"),
                                "--noprofile",
                                "--norc",
                                "-e",
                                "-o",
                                "pipefail",
                                "-c",
                                stub + selected,
                            ],
                            cwd=temporary,
                            env={
                                **os.environ,
                                "GITHUB_OUTPUT": result_file.name,
                                "LITAI_TEST_SKILL_OUTPUT": output,
                                "LITAI_TEST_SKILL_STATUS": str(status),
                            },
                            capture_output=True,
                            text=True,
                        )
                        recorded = (
                            result_file.read_text() if result_file.exists() else ""
                        )
                        self.assertEqual(result.returncode, status, result.stderr)
                        if status:
                            self.assertNotIn("changed=", recorded)
                        else:
                            changed = "true" if output else "false"
                            self.assertEqual(
                                recorded,
                                f"base={base}\nchanged={changed}\n",
                            )

    def test_uv_bootstrap_runs_without_skill_changes_and_keeps_admission_conditional(
        self,
    ) -> None:
        steps = self._skill_steps()
        uv = next(
            step for step in steps if step.startswith("uses: astral-sh/setup-uv@")
        )
        self.assertNotRegex(uv, r"(?m)^        (?:if|continue-on-error):")
        self.assertIn('version: "0.12.4"', uv)
        self.assertIn("enable-cache: false", uv)
        probe = self._uv_probe()
        self.assertTrue(probe)
        for name in ("Install pinned NVIDIA SkillEvaluator", "Admit changed skills"):
            step = next(step for step in steps if step.startswith(f"name: {name}\n"))
            self.assertIn("if: steps.changed-skills.outputs.changed == 'true'", step)
        self.assertLess(
            steps.index(uv),
            next(
                i
                for i, step in enumerate(steps)
                if "name: Verify pinned uv bootstrap" in step
            ),
        )

    def test_actual_workflow_probe_accepts_only_the_pinned_uv_version(self) -> None:
        probe = compile(self._uv_probe(), "ci.yml:uv-bootstrap", "exec")
        for output, accepted in (
            ("uv 0.12.4\n", True),
            ("uv 0.12.4 (build metadata)\n", True),
            ("uv 0.12.40\n", False),
            ("uv 0.12.3\n", False),
            ("other 0.12.4\n", False),
            ("", False),
        ):
            with (
                self.subTest(output=output),
                mock.patch("subprocess.check_output", return_value=output) as run,
                mock.patch("builtins.print"),
            ):
                if accepted:
                    exec(probe, {})
                else:
                    with self.assertRaises(SystemExit):
                        exec(probe, {})
                run.assert_called_once_with(["uv", "--version"], text=True)

    def test_actual_workflow_probe_propagates_missing_or_failed_uv(self) -> None:
        probe = compile(self._uv_probe(), "ci.yml:uv-bootstrap", "exec")
        for failure in (
            FileNotFoundError("uv"),
            subprocess.CalledProcessError(1, "uv"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch("subprocess.check_output", side_effect=failure),
                self.assertRaises(type(failure)),
            ):
                exec(probe, {})
