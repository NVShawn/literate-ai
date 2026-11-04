"""Workers align with the repository template and the user's private expectations."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import remote_transport, worker_alignment
from literate_ai.adapters.worker_alignment import (
    UserAlignment,
    WorkerAligner,
    WorkerTemplate,
)
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
)


class LocalRunner:
    """SSH and SCP are the network boundary; run them on this host instead."""

    def run(self, argv, *, cwd, timeout_seconds):
        completed = subprocess.run(
            argv, cwd=cwd, capture_output=True, timeout=timeout_seconds
        )
        return SimpleNamespace(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "POSIX shell")
class WorkerAlignmentTests(unittest.TestCase):
    def test_inspect_reports_gaps_and_apply_closes_only_declared_ones(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            (home / "bin").mkdir(parents=True)
            python = Path(sys.executable)
            template = root / "template.json"
            template.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/worker-template@1",
                        "platforms": {
                            "linux": {
                                "commands": ["git", "litai-missing-tool"],
                                "python": {
                                    "commands": [python.name],
                                    "minimum": "3.11",
                                },
                            }
                        },
                    }
                )
            )
            secret = root / "keys" / "provider.txt"
            secret.parent.mkdir()
            secret.write_text("current-key\n")
            stale = home / ".config" / "provider.txt"
            stale.parent.mkdir(parents=True)
            stale.write_text("revoked-key\n")
            alignment = root / "worker-alignment.json"
            alignment.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/worker-alignment@1",
                        "files": [
                            {
                                "source": str(secret),
                                "destination": "~/.config/provider.txt",
                                "secret": True,
                            }
                        ],
                        "installs": [
                            {
                                "command": "litai-missing-tool",
                                "os_family": "linux",
                                "argv": [
                                    "sh",
                                    "-c",
                                    "printf '#!/bin/sh\\n' > \"$HOME/bin/"
                                    'litai-missing-tool" && chmod +x "$HOME/bin/'
                                    'litai-missing-tool"',
                                ],
                            }
                        ],
                    }
                )
            )
            worker = ExecutionWorker(
                worker_id="linux-worker",
                kind=ExecutionWorkerKind.SSH,
                endpoint="user@worker.invalid",
                workspace="~/ws",
                transport="ssh",
                requirements=ExecutionRequirements(os_family="linux"),
            )
            aligner = WorkerAligner(
                WorkerTemplate.load(template),
                UserAlignment.load(alignment),
                coding_cli=None,
                model=None,
                cwd=root,
                runner_factory=LocalRunner,
            )
            environment = {
                "HOME": str(home),
                "PATH": os.pathsep.join(
                    (str(home / "bin"), str(python.parent), os.environ["PATH"])
                ),
            }
            with (
                patch.dict(os.environ, environment),
                patch.object(
                    worker_alignment,
                    "remote_execute_arguments",
                    lambda _endpoint, command, *_a, **_k: ("bash", "-c", command),
                ),
                patch.object(
                    remote_transport,
                    "scp_arguments",
                    lambda source, _endpoint, destination, _timeout, **_kwargs: (
                        "cp",
                        str(source),
                        str(home / destination),
                    ),
                ),
            ):
                inspected = aligner.align((worker,), apply=False)
                findings = {
                    (item["kind"], item["name"]): item["state"]
                    for item in inspected["workers"][0]["findings"]
                }
                self.assertFalse(inspected["aligned"])
                self.assertEqual(
                    findings,
                    {
                        ("command", "litai-missing-tool"): "missing",
                        ("file", "~/.config/provider.txt"): "stale",
                    },
                )
                # Inspection never changes the worker, and never prints secrets.
                self.assertEqual(stale.read_text(), "revoked-key\n")
                self.assertNotIn("key", json.dumps(inspected).replace("keys", ""))
                applied = aligner.align((worker,), apply=True)
            self.assertTrue(applied["aligned"], applied)
            self.assertEqual(stale.read_text(), "current-key\n")
            self.assertEqual(stale.stat().st_mode & 0o777, 0o600)
            backups = list(stale.parent.glob("provider.txt.bak-*"))
            self.assertEqual([item.read_text() for item in backups], ["revoked-key\n"])
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            self.assertTrue((home / "bin" / "litai-missing-tool").exists())


class WindowsDefenderExclusionTests(unittest.TestCase):
    def test_missing_exclusion_is_reported_then_added_for_the_workspace(self):
        """Windows workers exclude the workspace from Defender real-time scans."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            template = root / "template.json"
            template.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/worker-template@1",
                        "platforms": {
                            "windows": {
                                "commands": ["git"],
                                "python": {"commands": ["python"], "minimum": "3.11"},
                                "defender_exclusions": ["{workspace}"],
                            }
                        },
                    }
                )
            )
            scripts = []

            class Runner:
                """Answers the PowerShell probe as a worker lacking the exclusion."""

                def run(self, argv, *, cwd, timeout_seconds):
                    script = base64.b64decode(argv[-1].split()[-1]).decode("utf-16-le")
                    scripts.append(script)
                    stdout = (
                        "cmd git C:\\git.exe\npython python 3.12.1\n"
                        "excl 0 absent\nfree 999999999\n"
                        if "Get-MpPreference" in script
                        else ""
                    )
                    return SimpleNamespace(
                        returncode=0, stdout=stdout.encode(), stderr=b""
                    )

            worker = SimpleNamespace(
                worker_id="windows-worker",
                endpoint="worker.invalid",
                workspace="~/.litai",
                transport="ssh",
                requirements=SimpleNamespace(
                    os_family="windows", cpu_architecture=None
                ),
            )
            aligner = WorkerAligner(
                WorkerTemplate.load(template),
                UserAlignment.load(None),
                coding_cli=None,
                model=None,
                cwd=root,
                runner_factory=Runner,
            )
            with patch.object(
                worker_alignment,
                "remote_execute_arguments",
                lambda _endpoint, command, *_a, **_k: tuple(command.split()),
            ):
                inspected = aligner.align((worker,), apply=False)
                self.assertEqual(
                    [
                        (item["kind"], item["name"], item["state"])
                        for item in inspected["workers"][0]["findings"]
                    ],
                    [("defender-exclusion", "{workspace}", "missing")],
                )
                self.assertFalse(any("Add-MpPreference" in item for item in scripts))
                aligner.align((worker,), apply=True)
            added = [item for item in scripts if "Add-MpPreference" in item]
            self.assertEqual(len(added), 1)
            self.assertIn("Join-Path $HOME '.litai'", added[0])
            # Exclusions are Windows-only and never name home, a root or `..`.
            original = template.read_text()
            for broken in (
                original.replace('"windows"', '"linux"'),
                *(
                    original.replace('"{workspace}"', json.dumps(entry))
                    for entry in ("~/", "~/../..", "C:\\..", "C:\\", "relative")
                ),
            ):
                template.write_text(broken)
                with self.assertRaises(worker_alignment.WorkerAlignmentError):
                    WorkerTemplate.load(template)

    def test_missing_process_exclusion_is_reported_then_added_for_python(self):
        """Windows workers exclude the template's real base interpreter."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            template = root / "template.json"
            template.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/worker-template@1",
                        "platforms": {
                            "windows": {
                                "commands": ["git"],
                                "python": {"commands": ["python"], "minimum": "3.11"},
                                "defender_process_exclusions": [
                                    "{python}",
                                    "C:\\Tools\\bazel.exe",
                                ],
                            }
                        },
                    }
                )
            )
            scripts = []

            class Runner:
                """Answers the probe: Python lacks it, Bazel does not resolve."""

                def run(self, argv, *, cwd, timeout_seconds):
                    script = base64.b64decode(argv[-1].split()[-1]).decode("utf-16-le")
                    scripts.append(script)
                    stdout = (
                        "cmd git C:\\git.exe\npython python 3.12.1\n"
                        "pexcl 0 absent\npexcl 1 unresolved\nfree 999999999\n"
                        if "ExclusionProcess" in script
                        and "Add-MpPreference" not in script
                        else ""
                    )
                    return SimpleNamespace(
                        returncode=0, stdout=stdout.encode(), stderr=b""
                    )

            worker = SimpleNamespace(
                worker_id="windows-worker",
                endpoint="worker.invalid",
                workspace="~/.litai",
                transport="ssh",
                requirements=SimpleNamespace(
                    os_family="windows", cpu_architecture=None
                ),
            )
            aligner = WorkerAligner(
                WorkerTemplate.load(template),
                UserAlignment.load(None),
                coding_cli=None,
                model=None,
                cwd=root,
                runner_factory=Runner,
            )
            with patch.object(
                worker_alignment,
                "remote_execute_arguments",
                lambda _endpoint, command, *_a, **_k: tuple(command.split()),
            ):
                inspected = aligner.align((worker,), apply=False)
                self.assertEqual(
                    [
                        (item["kind"], item["name"], item["state"])
                        for item in inspected["workers"][0]["findings"]
                    ],
                    [
                        ("defender-process-exclusion", "{python}", "missing"),
                        (
                            "defender-process-exclusion",
                            "C:\\Tools\\bazel.exe",
                            "unchecked",
                        ),
                    ],
                )
                # {python} resolves to the real base interpreter on the worker,
                # isolated from site hooks, and must be an existing non-Store .exe.
                probe = next(item for item in scripts if "ExclusionProcess" in item)
                self.assertIn("-I -S -X utf8", probe)
                self.assertIn("os.path.realpath(sys._base_executable)", probe)
                self.assertIn("-PathType Leaf", probe)
                self.assertIn("-notlike '*\\WindowsApps\\*'", probe)
                self.assertFalse(any("Add-MpPreference" in item for item in scripts))
                aligner.align((worker,), apply=True)
            added = [item for item in scripts if "Add-MpPreference" in item]
            # Only the missing {python} is added; the unresolved literal is not.
            self.assertEqual(len(added), 1)
            self.assertIn("-ExclusionProcess $px", added[0])
            self.assertIn("sys._base_executable", added[0])
            self.assertNotIn("bazel.exe", added[0])
            # Process exclusions are Windows-only and name exactly one executable.
            original = template.read_text()
            for broken in (
                original.replace('"windows"', '"linux"'),
                *(
                    original.replace(
                        json.dumps("C:\\Tools\\bazel.exe"), json.dumps(entry)
                    )
                    for entry in (
                        "C:\\Tools\\*.exe",
                        "C:\\Tools",
                        "%ProgramFiles%\\x.exe",
                        "C:\\..\\x.exe",
                        "bazel.exe",
                        "{workspace}",
                        "C:\\a\\file.exe:stream.exe",
                        "C:\\a/b/c.exe",
                        "C:\\a\\b\n.exe",
                        "\\\\server\\share\\x.exe",
                        "C:\\Windows\\System32\\cmd.exe",
                        "C:\\Windows\\explorer.exe",
                        "C:\\Windows\\System32\\WindowsPowerShell\\powershell.exe",
                    )
                ),
            ):
                template.write_text(broken)
                with self.assertRaises(worker_alignment.WorkerAlignmentError):
                    WorkerTemplate.load(template)


class OpenCodeModelProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from literate_ai.adapters.project_initialization import initialize_project
        from literate_ai.contracts import RepositoryParentSelection
        from tests.support.fixtures_worker_alignment import build_coding_cli

        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name).resolve()
        cls.seed = cls.root / "seed"
        initialize_project(
            cls.seed,
            empty=True,
            bootstrap_tools=False,
            parent_selection=RepositoryParentSelection.root(),
        )
        cls.agent = cls.root / "agent"
        cls.agent.mkdir()
        cls.binary = build_coding_cli(cls.agent)

    def test_public_align_runs_inventory_selected_agent_without_reinstall(self):
        import io

        from literate_ai.cli import main
        from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog

        family = (
            "windows"
            if os.name == "nt"
            else {"darwin": "macos", "freebsd": "freebsd"}.get(sys.platform, "linux")
        )
        powershell = shutil.which("powershell.exe") if os.name == "nt" else None
        if os.name == "nt" and powershell is None:
            self.skipTest("native Windows PowerShell unavailable")
        cases = [("v1", True), ("v2", True), ("wrong", False), ("helpfail", False)]
        if os.name == "nt":
            cases.append(("fallback", True))
        for number, (mode, expected) in enumerate(cases):
            with self.subTest(mode=mode):
                root = self.root / str(number)
                root.mkdir()
                project = root / "project"
                shutil.copytree(self.seed, project)
                on_path = root / "path agent"
                fallback = root / "declared agent's directory"
                on_path.mkdir()
                fallback.mkdir()
                for directory, protocol in (
                    (on_path, "v2" if mode == "fallback" else mode),
                    (fallback, "v2" if mode == "fallback" else "wrong"),
                ):
                    shutil.copy2(self.binary, directory / self.binary.name)
                    (directory / "protocol.txt").write_text(protocol)
                worker = ExecutionWorker(
                    "fixture",
                    ExecutionWorkerKind.SSH,
                    requirements=ExecutionRequirements(os_family=family),
                    endpoint="user@fixture.invalid",
                    workspace="~/litai-model-probe",
                )
                catalog = root / "workers.json"
                catalog.write_text(
                    json.dumps(ExecutionWorkerCatalog((worker,)).to_dict())
                )
                template = root / "template.json"
                template.write_text(
                    json.dumps(
                        {
                            "schema": "literate-ai/worker-template@1",
                            "platforms": {
                                family: {
                                    "commands": [
                                        {
                                            "name": "opencode",
                                            "extra_paths": [str(fallback)],
                                        }
                                    ]
                                    if os.name == "nt"
                                    else ["opencode"],
                                    "python": {
                                        "commands": [Path(sys.executable).name],
                                        "minimum": "3.11",
                                    },
                                }
                            },
                        }
                    )
                )
                alignment = root / "alignment.json"
                alignment.write_text(
                    json.dumps(
                        {"schema": "literate-ai/worker-alignment@1", "files": []}
                    )
                )
                selection = root / "test.json"
                selection.write_text(
                    json.dumps({"coding_cli": "opencode", "model": "fixture/model"})
                )
                system_paths = (
                    [str(Path(os.environ["SystemRoot"]) / "System32")]
                    if os.name == "nt"
                    else ["/usr/bin", "/bin"]
                )
                search = ([] if mode == "fallback" else [str(on_path)]) + [
                    str(Path(sys.executable).parent),
                    *system_paths,
                ]
                environment = {
                    "PATH": os.pathsep.join(search),
                    "LITAI_TEST_CONFIG": str(selection),
                }

                def native_route(_worker, command, *_args, **_kwargs):
                    # Only substitute the external network hop. Inventory and
                    # both agent invocations execute as real native processes.
                    return (
                        (powershell, *command.split()[1:])
                        if os.name == "nt"
                        else ("/bin/bash", "-c", command)
                    )

                before = {
                    str(p): p.read_bytes()
                    for directory in (on_path, fallback)
                    for p in directory.iterdir()
                }
                previous = Path.cwd()
                output, errors = io.StringIO(), io.StringIO()
                try:
                    os.chdir(project)
                    with (
                        patch.dict(os.environ, environment),
                        patch.object(
                            worker_alignment, "remote_execute_arguments", native_route
                        ),
                    ):
                        status = main(
                            (
                                "worker",
                                "align",
                                "--all",
                                "--worker-config",
                                str(catalog),
                                "--template",
                                str(template),
                                "--alignment",
                                str(alignment),
                            ),
                            stdout=output,
                            stderr=errors,
                        )
                finally:
                    os.chdir(previous)
                document = json.loads(output.getvalue() or errors.getvalue())
                self.assertEqual(status, 0 if expected else 1, document)
                report = document["result"]
                self.assertEqual(report["aligned"], expected, report)
                findings = report["workers"][0]["findings"]
                if not expected:
                    self.assertEqual(
                        [(f["kind"], f["state"]) for f in findings],
                        [("model", "failed")],
                    )
                self.assertEqual(
                    before,
                    {
                        str(p): p.read_bytes()
                        for directory in (on_path, fallback)
                        for p in directory.iterdir()
                    },
                )


if __name__ == "__main__":
    unittest.main()


class FreeBSDAlignmentTests(unittest.TestCase):
    def test_native_template_inspection_rejects_wrong_or_missing_os(self):
        from literate_ai.contracts import (
            ExecutionRequirements,
            ExecutionWorker,
            ExecutionWorkerKind,
        )

        root = Path(__file__).resolve().parents[2]
        worker = ExecutionWorker(
            "bsd",
            ExecutionWorkerKind.SSH,
            requirements=ExecutionRequirements(os_family="freebsd"),
            endpoint="user@bsd.invalid",
            workspace="~/litai",
        )
        template = WorkerTemplate.load(root / "literate.worker-template.json")
        self.assertIn(
            "gmake", [item.name for item in template.platforms["freebsd"].commands]
        )
        self.assertNotIn(
            "make", [item.name for item in template.platforms["freebsd"].commands]
        )
        for os_line, expected in (
            ("os FreeBSD amd64", True),
            ("os Linux x86_64", False),
            ("", False),
        ):
            with self.subTest(os_line=os_line):
                output = "\n".join(
                    [
                        "cmd git /usr/local/bin/git",
                        "cmd git-lfs /usr/local/bin/git-lfs",
                        "cmd gmake /usr/local/bin/gmake",
                        "cmd pkg-static /usr/local/sbin/pkg-static",
                        "cmd bsdtar /usr/bin/bsdtar",
                        "cmd readelf /usr/bin/readelf",
                        "cmd ldconfig /sbin/ldconfig",
                        "cmd cc /usr/bin/cc",
                        "cmd node /usr/local/bin/node",
                        "cmd npm /usr/local/bin/npm",
                        "python python3 3.11.14",
                        "free 100000000",
                        os_line,
                    ]
                )
                aligner = WorkerAligner(
                    template, UserAlignment(), coding_cli=None, model=None, cwd=root
                )
                with patch.object(
                    aligner,
                    "_run",
                    return_value=SimpleNamespace(
                        returncode=0, stdout=output.encode(), stderr=b""
                    ),
                ):
                    report = aligner.align((worker,), apply=False, check_model=False)
                self.assertEqual(report["aligned"], expected, report)
