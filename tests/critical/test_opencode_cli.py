"""OpenCode version selection must preserve the model-egress boundary."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.models import coding_cli as cli
from tests.support.fixtures_test_coding_cli_generation import (
    flavor,
    recipe,
    write_generated_test_suite,
)


def result(stdout: bytes = b"", *, status: int = 0):
    return SimpleNamespace(returncode=status, stdout=stdout, stderr=b"")


def help_output(interface: str) -> bytes:
    # Preserve the actual option/alias layout of the two released help formats.
    common = b"  --agent string\n  --format choice\n"
    if interface == "v1":
        return common + b"  --pure boolean\n  --dir string\n"
    return common + (
        b"  --model, -m string\n  --standalone\n"
        b"  --print-logs\n  --log-level <all|trace|debug|info|warn|error>\n"
    )


def config_output(environment):
    native = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
    native.pop("mcp")
    return json.dumps(
        [
            {"type": "directory", "path": environment["OPENCODE_CONFIG_DIR"]},
            {"type": "document", "info": native},
        ]
    ).encode()


class OpenCodeCliTests(unittest.TestCase):
    def test_generation_and_json_tasks_use_the_selected_interface(self):
        for interface in ("v1", "v2"):
            for task in (False, True):
                with (
                    self.subTest(interface=interface, json_task=task),
                    tempfile.TemporaryDirectory() as raw,
                ):
                    root = Path(raw)
                    calls = []
                    value = recipe(flavor("cpp"))

                    def execute(
                        command,
                        *,
                        calls=calls,
                        interface=interface,
                        root=root,
                        task=task,
                        value=value,
                        **kwargs,
                    ):
                        env = kwargs["environment"]
                        workspace = Path(kwargs["cwd"])
                        calls.append((command, dict(env), workspace))
                        if command[1:] == ["run", "--help"]:
                            self.assertNotIn("OPENAI_API_KEY", env)
                            self.assertNotIn("UNRELATED_SECRET", env)
                            return result(help_output(interface))
                        if interface == "v2":
                            owned = Path(env["OPENCODE_CONFIG_DIR"])
                            for key in (
                                "XDG_CONFIG_HOME",
                                "XDG_DATA_HOME",
                                "XDG_STATE_HOME",
                                "XDG_CACHE_HOME",
                                "OPENCODE_DB",
                                "TMPDIR",
                                "TMP",
                                "TEMP",
                            ):
                                self.assertTrue(
                                    Path(env[key]).is_relative_to(owned), key
                                )
                        if command[1:3] == ["debug", "config"]:
                            self.assertNotIn("OPENAI_API_KEY", env)
                            return result(config_output(env))
                        self.assertEqual(env["OPENAI_API_KEY"], "test-key")
                        self.assertNotIn("OPENCODE_SERVER", env)
                        self.assertNotIn("UNRELATED_SECRET", env)
                        self.assertNotEqual(workspace, root / "candidate")
                        if task:
                            (workspace / ".literate-ai-task-response.json").write_text(
                                '{"ok":true}'
                            )
                        else:
                            (workspace / "source").mkdir()
                            (workspace / "source/main.cpp").write_text(
                                "int main() { return 0; }\n"
                            )
                            write_generated_test_suite(workspace, value)
                        return result()

                    with (
                        mock.patch.object(
                            cli.shutil, "which", return_value=sys.executable
                        ),
                        mock.patch.object(cli, "_run_bounded", side_effect=execute),
                        mock.patch.object(
                            cli,
                            "opencode_managed_configuration_roots",
                            return_value=(root / "absent", None),
                        ),
                    ):
                        environment = {
                            "CODING_CLI": "opencode",
                            "PATH": "/tools",
                            "OPENAI_API_KEY": "test-key",
                            "UNRELATED_SECRET": "private",
                            "OPENCODE_SERVER": "http://ambient.invalid",
                            "OPENCODE_TEST_HOME": "/ambient/home",
                            "OPENCODE_DB": "/ambient/credentials.db",
                            "XDG_DATA_HOME": "/ambient/data",
                            "XDG_STATE_HOME": "/ambient/state",
                            "XDG_CACHE_HOME": "/ambient/cache",
                        }
                        if task:
                            actual = cli.CodingCliTaskRunner(
                                environment=environment
                            ).run_json_task("Return JSON", model="openai/example")
                            self.assertEqual(actual.response, {"ok": True})
                        else:
                            actual = cli.CodingCliSourceGenerator(
                                environment=environment
                            ).generate(value, output_root=root / "candidate")
                            self.assertIn("source/main.cpp", actual.files)
                    command, env, workspace = calls[-1]
                    if task:
                        self.assertEqual(command[-3:-1], ["--model", "openai/example"])
                    if interface == "v1":
                        self.assertEqual(command[1:3], ["--pure", "run"])
                        self.assertEqual(
                            command[command.index("--dir") + 1], str(workspace)
                        )
                        self.assertNotIn("--standalone", command)
                        self.assertEqual(
                            json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"][
                                "*"
                            ],
                            "deny",
                        )
                        self.assertEqual(len(calls), 2)
                    else:
                        self.assertEqual(command[1:3], ["run", "--standalone"])
                        self.assertNotIn("--pure", command)
                        self.assertNotIn("--dir", command)
                        self.assertEqual(
                            command[command.index("--log-level") + 1], "error"
                        )
                        self.assertEqual(
                            env["OPENCODE_TEST_HOME"], env["OPENCODE_CONFIG_DIR"]
                        )
                        native = json.loads(env["OPENCODE_CONFIG_CONTENT"])
                        self.assertEqual(
                            native["permissions"][0],
                            {"action": "*", "resource": "*", "effect": "deny"},
                        )
                        self.assertIn(
                            {
                                "action": "external_directory",
                                "resource": "*",
                                "effect": "deny",
                            },
                            native["permissions"],
                        )
                        self.assertEqual(
                            {p["action"] for p in native["policies"]},
                            {"plugin.use", "skill.use", "mcp.use"},
                        )
                        self.assertEqual(len(calls), 3)
                    self.assertFalse(Path(env["OPENCODE_CONFIG_DIR"]).exists())

    def test_missing_or_failed_help_never_reaches_a_prompt(self):
        for output, status in (
            (b"--pure --dir --agent --format", 0),
            (b"  --agent\n  --format\n", 0),
            (help_output("v2"), 1),
        ):
            with (
                self.subTest(output=output, status=status),
            ):
                with (
                    mock.patch.object(cli.shutil, "which", return_value=sys.executable),
                    mock.patch.object(
                        cli, "_run_bounded", return_value=result(output, status=status)
                    ) as run,
                ):
                    runner = cli.CodingCliTaskRunner(
                        environment={"CODING_CLI": "opencode"}
                    )
                    with self.assertRaises(cli.CodingCliError) as raised:
                        runner.run_json_task("Do not send this prompt")
                    self.assertEqual(raised.exception.code, "coding_cli.incompatible")
                    self.assertEqual(run.call_count, 1)

    def test_v2_refuses_ambient_or_malformed_configuration_before_prompt(self):
        for change in ("ambient", "permission", "invalid", "status"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)

                def execute(command, *, change=change, **kwargs):
                    if command[1:] == ["run", "--help"]:
                        return result(help_output("v2"))
                    self.assertEqual(command[1:3], ["debug", "config"])
                    entries = json.loads(config_output(kwargs["environment"]))
                    if change == "ambient":
                        entries.insert(
                            0,
                            {
                                "type": "document",
                                "path": "/ambient/opencode.json",
                                "info": {},
                            },
                        )
                    elif change == "permission":
                        entries[1]["info"]["permissions"][0]["effect"] = "allow"
                    return result(
                        b"secret not JSON"
                        if change == "invalid"
                        else json.dumps(entries).encode(),
                        status=1 if change == "status" else 0,
                    )

                with (
                    mock.patch.object(cli.shutil, "which", return_value=sys.executable),
                    mock.patch.object(cli, "_run_bounded", side_effect=execute) as run,
                    mock.patch.object(
                        cli,
                        "opencode_managed_configuration_roots",
                        return_value=(root / "absent", None),
                    ),
                ):
                    with self.assertRaises(cli.CodingCliError) as raised:
                        cli.CodingCliTaskRunner(
                            environment={"CODING_CLI": "opencode"}
                        ).run_json_task("Do not send")
                    self.assertEqual(raised.exception.code, "coding_cli.incompatible")
                    self.assertNotIn("secret", str(raised.exception))
                    self.assertEqual(run.call_count, 2)

    def test_v2_refuses_system_or_mdm_configuration(self):
        for kind in ("system", "machine-mdm", "user-mdm"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                managed, preferences = root / "system", root / "preferences"
                if kind == "system":
                    managed.mkdir()
                else:
                    target = preferences / ("user" if kind == "user-mdm" else "")
                    target.mkdir(parents=True)
                    (target / "ai.opencode.managed.plist").write_text("managed")
                with (
                    mock.patch.object(
                        cli,
                        "opencode_managed_configuration_roots",
                        return_value=(managed, preferences),
                    ),
                    mock.patch.object(cli, "_run_bounded") as run,
                ):
                    with self.assertRaises(cli.CodingCliError):
                        cli._configure_opencode_interface({}, "v2", mock.Mock(), root)
                    run.assert_not_called()

    def test_probe_detects_executable_drift_even_when_help_succeeds(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            executable = root / "opencode"
            executable.write_text("before")
            executable.chmod(0o755)
            selection = cli._pin_coding_cli("opencode", str(executable))

            def execute(*args, **kwargs):
                executable.write_text("after")
                return result(help_output("v2"))

            with mock.patch.object(cli, "_run_bounded", side_effect=execute):
                with self.assertRaises(cli.CodingCliError) as raised:
                    cli._require_coding_cli_compatible(
                        selection, workspace=root, environment={}
                    )
            self.assertEqual(raised.exception.code, "coding_cli.executable_drift")
