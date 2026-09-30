"""Native CLI acceptance is verifier-owned, bounded, and package-custodied."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.component_acceptance import (
    NATIVE_CLI_SCHEMA,
    ComponentAcceptanceError,
    NativeCliAcceptance,
    load_native_cli_acceptance,
)
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
    local_tree_identity,
)
from literate_ai.contracts import (
    NATIVE_CLI_ENTRYPOINT_KIND,
    ContentIdentity,
    Entrypoint,
    canonical_identity,
)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


class NativeCliAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "native.json"
        self.entrypoint = Entrypoint("run", NATIVE_CLI_ENTRYPOINT_KIND, "bin/native")
        self.specification = canonical_identity("specification")
        self.target = canonical_identity("target")

    def _content(
        self, *, exact: bytes | None = None, contains: tuple[str, ...] = ()
    ) -> dict[str, object]:
        return {
            "exact_base64": None if exact is None else _b64(exact),
            "utf8_contains": list(contains),
            "utf8_not_contains": [],
        }

    def _document(self) -> dict[str, object]:
        return {
            "schema": NATIVE_CLI_SCHEMA,
            "component": "native-tool",
            "entrypoint": "run",
            "specification_set_identity": self.specification.uri,
            "environment": {"NATIVE_MODE": "acceptance"},
            "cases": [
                {
                    "case_id": "render",
                    "arguments": [
                        "{file:input.bin}",
                        "{work}/results/out.bin",
                    ],
                    "stdin_base64": _b64(b"-stdin"),
                    "expected_exit_code": 0,
                    "stdout": self._content(contains=("done",)),
                    "stderr": self._content(exact=b""),
                    "fixtures": [
                        {
                            "path": "input.bin",
                            "mode": 0o600,
                            "content_base64": _b64(b"fixture"),
                        }
                    ],
                    "filesystem": [
                        {
                            "path": "results",
                            "kind": "directory",
                            "mode": 0o755,
                            "maximum_bytes": 0,
                            "content": None,
                        },
                        {
                            "path": "results/out.bin",
                            "kind": "file",
                            "mode": 0o640,
                            "maximum_bytes": 64,
                            "content": self._content(exact=b"fixture-stdin"),
                        },
                    ],
                    "timeout_seconds": 10,
                }
            ],
            "stdout_limit_bytes": 4096,
            "stderr_limit_bytes": 4096,
            "filesystem_limit_bytes": 65536,
        }

    def _load(self, value: dict[str, object]) -> NativeCliAcceptance:
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return load_native_cli_acceptance(
            self.path,
            "native-tool",
            self.entrypoint,
            specification_set_identity=self.specification,
            target_identity=self.target,
        )

    def test_contract_binds_entrypoint_target_cases_and_binary_content(self) -> None:
        contract = self._load(self._document())
        self.assertEqual(
            contract.entrypoint_identity, canonical_identity(self.entrypoint.to_dict())
        )
        self.assertEqual(contract.target_identity, self.target)
        self.assertEqual(contract.cases[0].stdin, b"-stdin")
        self.assertEqual(contract.cases[0].fixtures[0].content, b"fixture")
        self.assertTrue(contract.identity.uri.startswith("sha256:"))

    def test_contract_rejects_unknown_placeholders_secrets_and_partial_content(
        self,
    ) -> None:
        document = self._document()
        document["cases"][0]["arguments"] = ["{project}/input.bin"]
        with self.assertRaisesRegex(ComponentAcceptanceError, "unknown placeholder"):
            self._load(document)

        document = self._document()
        document["environment"] = {"API_TOKEN": "not-authority"}
        with self.assertRaisesRegex(ComponentAcceptanceError, "unsafe"):
            self._load(document)

        document = self._document()
        document["cases"][0]["stdout"] = self._content()
        with self.assertRaisesRegex(ComponentAcceptanceError, "must choose"):
            self._load(document)

    def _execute(self, *, rogue: bool = False) -> ContentIdentity:
        contract = self._load(self._document())
        package_root = self.root / "package"
        package_root.mkdir()
        executable = package_root / "native-tool.py"
        executable.write_text("# package-owned native entrypoint\n", encoding="utf-8")
        executable.chmod(0o755)
        source_identity = canonical_identity("native-executable")
        packaged_entrypoint = SimpleNamespace(
            name="run",
            kind=NATIVE_CLI_ENTRYPOINT_KIND,
            source_identity=source_identity,
        )
        plan = SimpleNamespace(
            identity=canonical_identity("package-plan"),
            entrypoints=(packaged_entrypoint,),
            target_identity=self.target,
        )
        result = SimpleNamespace(
            identity=canonical_identity("package-result"),
            entrypoints=plan.entrypoints,
        )
        custody = SimpleNamespace(
            root=package_root,
            tree_identity=local_tree_identity(package_root),
            artifact_paths={source_identity.uri: executable},
            native_sdk_resources=None,
        )
        object_root = self.root / "objects"
        object_root.mkdir()
        ports = LocalStandardLifecyclePorts(
            source_trees=LocalSourceTreeRegistry(),
            object_root=object_root,
            contracts=(),
            independent_acceptance_oracle=contract,
        )
        script = (
            "import os,pathlib,sys; "
            "source=pathlib.Path(sys.argv[1]).read_bytes(); "
            "target=pathlib.Path(sys.argv[2]); target.parent.mkdir(mode=0o755); "
            "target.write_bytes(source+sys.stdin.buffer.read()); target.chmod(0o640); "
            + ("pathlib.Path('rogue').write_text('undeclared'); " if rogue else "")
            + "print('done '+os.environ['NATIVE_MODE'])"
        )
        with (
            patch.object(
                ports, "_packaged_argv", return_value=(sys.executable, "-c", script)
            ),
            patch.object(ports, "_packaged_environment", return_value={}),
        ):
            return ports._accept_native_cli(
                custody,
                plan,
                result,
                contract,
                canonical_identity("root-test"),
                canonical_identity("packaged-execution"),
                None,
            )

    @unittest.skipIf(os.name == "nt", "POSIX fixture modes are qualified separately")
    def test_packaged_execution_accepts_exact_complete_filesystem_delta(self) -> None:
        self.assertTrue(self._execute().uri.startswith("sha256:"))

    @unittest.skipIf(os.name == "nt", "POSIX fixture modes are qualified separately")
    def test_packaged_execution_rejects_undeclared_filesystem_delta(self) -> None:
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "complete declared projection"
        ):
            self._execute(rogue=True)


if __name__ == "__main__":
    unittest.main()
