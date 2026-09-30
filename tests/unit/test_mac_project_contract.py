from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.mac_project_contract import (
    MacProjectContractError,
    project_mac_repository_contract,
    write_mac_repository_contract,
)
from literate_ai.contracts import (
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    ManagedComponentKind,
    canonical_json_bytes,
    component_bom_ref,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.yaml_subset import load_yaml_subset


def _identity(character: str) -> ContentIdentity:
    return ContentIdentity.parse_uri(f"sha256:{character * 64}")


def _resolved_bom() -> bytes:
    root_identity = _identity("a")
    root_ref = component_bom_ref(root_identity)
    managed = CycloneDxManagedGraph(
        root_ref,
        (
            CycloneDxManagedComponent(
                root_ref,
                ManagedComponentKind.ROOT,
                root_identity,
                "component://example/widget",
                "1.0.0",
                (),
            ),
        ),
        (),
        _identity("b"),
    )
    source, _ = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=managed
    )
    tools = (
        {
            "type": "file",
            "bom-ref": "urn:example:python",
            "name": "python",
            "version": "3.14.0",
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": "toolchain"},
                {"name": "literate-ai:dependency-scope", "value": "build"},
                {
                    "name": "literate-ai:launcher-path",
                    "value": "C:\\Python314\\python.exe",
                },
            ],
        },
        {
            "type": "file",
            "bom-ref": "urn:example:make",
            "name": "make",
            "version": "4.4.1",
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": "system"},
                {"name": "literate-ai:dependency-scope", "value": "toolchain"},
                {"name": "literate-ai:elf-path", "value": "/usr/bin/make"},
            ],
        },
        {
            "type": "library",
            "bom-ref": "pkg:pypi/requests@2.32.5",
            "name": "requests",
            "version": "2.32.5",
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": "package"},
                {"name": "literate-ai:dependency-scope", "value": "runtime"},
            ],
        },
    )
    return build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.RESOLVED,
        managed_graph=managed,
        source_bom=source,
        additional_components=tools,
        additional_edges=tuple((root_ref, item["bom-ref"]) for item in tools),
    )[0]


class MacProjectContractTests(unittest.TestCase):
    def test_projects_deterministically_from_only_resolved_bom(self) -> None:
        bom = _resolved_bom()
        first = project_mac_repository_contract(
            bom,
            project="widget",
            platforms=("darwin", "linux", "windows"),
            bootstrap_creates=("_build",),
        )
        second = project_mac_repository_contract(
            bom,
            project="widget",
            platforms=("darwin", "linux", "windows"),
            bootstrap_creates=("_build",),
        )
        self.assertEqual(first, second)
        contract = load_yaml_subset(first[0].decode())
        self.assertEqual(contract["schema"], "mac.repository_contract.v1")
        self.assertEqual(contract["project"], "widget")
        self.assertEqual(contract["platforms"], ["darwin", "linux", "windows"])
        self.assertEqual(
            contract["toolchain"]["required_commands"],
            ["git", "litai", "make", "python"],
        )
        self.assertNotIn("requests", contract["toolchain"]["required_commands"])
        evidence = json.loads(first[1])
        self.assertEqual(
            evidence["resolved_bom_identity"],
            "sha256:" + hashlib.sha256(bom).hexdigest(),
        )
        self.assertRegex(evidence["source_bom_identity"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(evidence["resolved_graph_identity"], r"^sha256:[0-9a-f]{64}$")

    def test_rejects_source_bom_and_missing_identity_binding(self) -> None:
        resolved = json.loads(_resolved_bom())
        resolved["metadata"]["lifecycles"] = [{"phase": "pre-build"}]
        with self.assertRaisesRegex(MacProjectContractError, "post-build"):
            project_mac_repository_contract(
                canonical_json_bytes(resolved), project="widget", platforms=("linux",)
            )

        resolved["metadata"]["lifecycles"] = [{"phase": "post-build"}]
        resolved["metadata"]["properties"] = [
            item
            for item in resolved["metadata"]["properties"]
            if item["name"] != "literate-ai:source-bom-identity"
        ]
        with self.assertRaisesRegex(MacProjectContractError, "requires exactly one"):
            project_mac_repository_contract(
                canonical_json_bytes(resolved), project="widget", platforms=("linux",)
            )

    def test_writer_creates_only_contract_and_identity_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bom = root / "resolved.cdx.json"
            bom.write_bytes(_resolved_bom())
            result = write_mac_repository_contract(
                bom, project_root=root, project="widget", platforms=("linux",)
            )
            self.assertTrue((root / ".mac/project.yaml").is_file())
            self.assertEqual(
                json.loads((root / ".mac/project.contract.json").read_text()), result
            )
            self.assertEqual(
                {
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_file()
                },
                {
                    "resolved.cdx.json",
                    ".mac/project.yaml",
                    ".mac/project.contract.json",
                },
            )


if __name__ == "__main__":
    unittest.main()
