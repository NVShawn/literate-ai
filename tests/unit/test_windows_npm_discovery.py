"""Windows npm command-shim discovery through one selected Node runtime."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.builders import javascript as javascript_builder_module
from literate_ai.adapters.builders.javascript import (
    NodeToolchain,
    discover_npm_toolchain,
)
from literate_ai.adapters.builders.python import BuildError, executable_file_digest
from literate_ai.adapters.dependencies import observe_npm_distribution
from literate_ai.contracts.flavors import ToolchainConstraint

REPO = Path(__file__).resolve().parents[2]


def _npm_constraint_fields() -> dict[str, str]:
    constraint = ToolchainConstraint.from_dict(
        json.loads(
            (REPO / "flavors" / "package-npm" / "toolchain.json").read_text(
                encoding="utf-8"
            )
        )
    )
    return {
        "version_constraint": constraint.version_range(),
        "constraint_identity": constraint.identity.uri,
    }


def _npm_cmd_shim(target: str) -> bytes:
    lines = (
        "@ECHO off",
        "GOTO start",
        ":find_dp0",
        "SET dp0=%~dp0",
        "EXIT /b",
        ":start",
        "SETLOCAL",
        "CALL :find_dp0",
        "",
        'IF EXIST "%dp0%\\node.exe" (',
        '  SET "_prog=%dp0%\\node.exe"',
        ") ELSE (",
        '  SET "_prog=node"',
        "  SET PATHEXT=%PATHEXT:;.JS;=;%",
        ")",
        "",
        "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "
        f'"%_prog%"  "%dp0%\\{target}" %*',
    )
    return ("\r\n".join(lines) + "\r\n").encode()


def _node_toolchain(root: Path) -> NodeToolchain:
    executable = root / "node-prefix" / "node.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"selected-node-fixture")
    resolved = executable.resolve(strict=True)
    digest = executable_file_digest(resolved)
    return NodeToolchain(
        command=(str(resolved),),
        launcher_executable=str(resolved),
        launcher_digest=digest,
        runtime_executable=str(resolved),
        runtime_digest=digest,
        version="v20.0.0",
    )


def _npm_distribution(root: Path):
    prefix = root / "npm-prefix"
    package = prefix / "node_modules" / "npm"
    cli = package / "bin" / "npm-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("module.exports = true;\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "npm",
                "version": "10.0.0",
                "bin": {"npm": "bin/npm-cli.js"},
                "dependencies": {},
            }
        ),
        encoding="utf-8",
    )
    launcher = prefix / "npm.cmd"
    launcher.write_bytes(_npm_cmd_shim("node_modules\\npm\\bin\\npm-cli.js"))
    return launcher, cli.resolve(strict=True), observe_npm_distribution(cli)


class WindowsNpmDiscoveryTests(unittest.TestCase):
    def test_windows_shim_selects_npm_from_a_separate_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            node = _node_toolchain(root)
            launcher, cli, distribution = _npm_distribution(root)

            def which(name: str, *, path: str | None = None) -> str | None:
                del path
                return str(launcher) if name in {"npm", "npm.cmd"} else None

            with (
                mock.patch.object(
                    javascript_builder_module.shutil,
                    "which",
                    side_effect=which,
                ),
                mock.patch.object(
                    javascript_builder_module,
                    "_observe_npm_distribution_with_node",
                    return_value=distribution,
                ) as observe,
                mock.patch.object(
                    javascript_builder_module,
                    "_probe_npm_version",
                    return_value="10.0.0",
                ),
            ):
                selected = discover_npm_toolchain(
                    node,
                    {"PATH": str(launcher.parent)},
                    **_npm_constraint_fields(),
                )

            self.assertEqual(selected.cli_path, str(cli))
            self.assertIs(selected.node, node)
            self.assertEqual(selected.command[0], node.runtime_executable)
            self.assertEqual(selected.command[-1], str(cli))
            self.assertNotIn(str(launcher), selected.command)
            self.assertNotEqual(
                Path(node.runtime_executable).parent, Path(selected.package_root).parent
            )
            self.assertEqual(observe.call_count, 2)
            self.assertTrue(all(call.args[1] == cli for call in observe.call_args_list))

    def test_malformed_windows_shim_is_never_admitted_or_executed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            node = _node_toolchain(root)
            launcher, _cli, _distribution = _npm_distribution(root)
            launcher.write_bytes(launcher.read_bytes() + b"calc.exe\r\n")

            def which(name: str, *, path: str | None = None) -> str | None:
                del path
                return str(launcher) if name in {"npm", "npm.cmd"} else None

            with (
                mock.patch.object(
                    javascript_builder_module.shutil,
                    "which",
                    side_effect=which,
                ),
                mock.patch.object(
                    javascript_builder_module,
                    "_observe_npm_distribution_with_node",
                ) as observe,
                mock.patch.object(
                    javascript_builder_module,
                    "_probe_npm_version",
                ) as probe,
                self.assertRaises(BuildError) as rejected,
            ):
                discover_npm_toolchain(
                    node,
                    {"PATH": str(launcher.parent)},
                    **_npm_constraint_fields(),
                )

            self.assertEqual(
                rejected.exception.code, "builder.npm_toolchain_unavailable"
            )
            observe.assert_not_called()
            probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
