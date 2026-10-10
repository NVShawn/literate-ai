"""BUILD-220 adapter-neutral artifact graph and immutable assembly tests."""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from literate_ai.application.artifact_graph import (
    ArtifactAssemblyError,
    assemble_source_tree,
    create_artifact_build_graph,
)
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.artifacts import (
    ArtifactBuildGraph,
    GeneratedTextFile,
    GeneratedTextTree,
    artifact_driver_composition_identity,
)
from literate_ai.contracts.executable_components.assets import AuthoredBinaryAsset
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from tests.support import fixtures_test_artifact_graph_contracts as graph_fixtures

ROOT = Path(__file__).resolve().parents[2]


def identity(label: str) -> ContentIdentity:
    return canonical_identity({"fixture": label})


def _official_validator(resource_id: str) -> Draft202012Validator:
    registry = Registry()
    for directory in (ROOT / "schemas" / "v1", ROOT / "schemas" / "v2"):
        for path in sorted(directory.glob("*.schema.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            registry = registry.with_resource(
                document["$id"], Resource.from_contents(document)
            )
    return Draft202012Validator({"$ref": resource_id}, registry=registry.crawl())


def blob(content: bytes, media_type: str) -> BlobRef:
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type=media_type
    )


class SourceAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.component = identity("component")
        self.target = identity("target")
        self.authorization = identity("authorization")
        self.text = b"def answer():\n    return 42\n"
        self.icon = b"\x89PNG\r\n\x1a\n"
        self.text_blob = blob(self.text, "text/x-python")
        self.icon_blob = blob(self.icon, "image/png")
        self.contents = {
            self.text_blob.identity: self.text,
            self.icon_blob.identity: self.icon,
        }

    def generated(self, path: str = "src/app.py") -> GeneratedTextTree:
        return GeneratedTextTree(
            self.component,
            self.target,
            self.authorization,
            (GeneratedTextFile(path, self.text_blob),),
        )

    def asset(self, path: str = "assets/icon.png") -> AuthoredBinaryAsset:
        return AuthoredBinaryAsset(
            component_revision=self.component,
            asset_id="icon",
            path=path,
            role="runtime-asset",
            target_identity=self.target,
            blob=self.icon_blob,
            authorization_identity=self.authorization,
        )

    def read(self, reference: BlobRef) -> bytes:
        return self.contents[reference.identity]

    def test_model_cannot_overwrite_content_locked_asset(self) -> None:
        with self.assertRaisesRegex(ArtifactAssemblyError, "cannot overwrite"):
            assemble_source_tree(
                self.generated("assets/icon.png"), (self.asset(),), read_blob=self.read
            )

    def test_rejects_wrong_blob_bytes_and_non_utf8_model_output(self) -> None:
        with self.assertRaisesRegex(ArtifactAssemblyError, "digest mismatch"):
            assemble_source_tree(
                self.generated(),
                (),
                read_blob=lambda _: b"same-length-wrong-bytes........."[
                    : len(self.text)
                ],
            )
        bad = b"\xff"
        bad_blob = blob(bad, "text/plain")
        generated = GeneratedTextTree(
            self.component,
            self.target,
            self.authorization,
            (GeneratedTextFile("bad.txt", bad_blob),),
        )
        with self.assertRaisesRegex(ArtifactAssemblyError, "not UTF-8"):
            assemble_source_tree(generated, (), read_blob=lambda _: bad)


class MixedDriverGraphTests(unittest.TestCase):
    """A graph never silently mixes build drivers (contributed in PR #45).

    Invariant: a multi-driver graph binds every and only measured manifest
    driver through an explicit composition identity, and single-driver graphs
    keep their existing wire identity.
    """

    def setUp(self) -> None:
        self.fixture = graph_fixtures.ArtifactGraphTests()

    def test_mixed_drivers_require_explicit_exact_composition(self) -> None:
        fixture = self.fixture
        library = fixture.export("source-library")
        root = fixture.export("mix-root", (library,))
        manifests = (
            fixture.manifest(library),
            replace(
                fixture.manifest(root, (library,)),
                build_system_driver_identity=identity("native-mix-driver"),
            ),
        )
        drivers = tuple(
            sorted(
                {item.build_system_driver_identity for item in manifests},
                key=lambda item: item.uri,
            )
        )
        with self.assertRaisesRegex(ContractValidationError, "selected driver"):
            create_artifact_build_graph(
                build_system_driver_identity=fixture.driver,
                manifests=manifests,
                link_roots=(root.identity,),
            )
        graph = create_artifact_build_graph(
            build_system_driver_identity=artifact_driver_composition_identity(drivers),
            driver_composition=drivers,
            manifests=manifests,
            link_roots=(root.identity,),
        )
        self.assertEqual(ArtifactBuildGraph.from_dict(graph.to_dict()), graph)
        self.assertEqual(len(graph.link_plans[0].ordered_artifact_identities), 2)
        for composition in (
            drivers[:1],
            tuple(reversed(drivers)),
            tuple(
                sorted((*drivers, identity("unplanned-driver")), key=lambda i: i.uri)
            ),
        ):
            with (
                self.subTest(composition=composition),
                self.assertRaises(ContractValidationError),
            ):
                replace(graph, driver_composition=composition)
        with self.assertRaises(ContractValidationError):
            replace(graph, build_system_driver_identity=fixture.driver)
        with self.assertRaises(ContractValidationError):
            replace(graph, driver_composition=())

    def test_single_driver_graph_retains_legacy_wire_identity(self) -> None:
        fixture = self.fixture
        root = fixture.export("legacy-root")
        graph = create_artifact_build_graph(
            build_system_driver_identity=fixture.driver,
            manifests=(fixture.manifest(root),),
            link_roots=(root.identity,),
        )
        wire = graph.to_dict()
        self.assertNotIn("driver_composition", wire)
        self.assertEqual(graph.identity, canonical_identity(wire))
        with self.assertRaises(ContractValidationError):
            ArtifactBuildGraph.from_dict({**wire, "driver_composition": []})

    def test_mixed_driver_graph_wire_declares_bounded_unique_composition(self) -> None:
        fixture = self.fixture
        manifests = fixture.diamond()
        manifests = (
            replace(
                manifests[0],
                build_system_driver_identity=identity("mix"),
            ),
            *manifests[1:],
        )
        drivers = tuple(
            sorted(
                {item.build_system_driver_identity for item in manifests},
                key=lambda item: item.uri,
            )
        )
        graph = create_artifact_build_graph(
            build_system_driver_identity=artifact_driver_composition_identity(drivers),
            driver_composition=drivers,
            manifests=manifests,
            link_roots=(manifests[0].exports[0].identity,),
        )
        validator = _official_validator(graph.SCHEMA)
        validator.validate(graph.to_dict())
        for drivers_wire in ([], [drivers[0].to_dict()], [drivers[0].to_dict()] * 2):
            with self.subTest(drivers=drivers_wire):
                self.assertFalse(
                    validator.is_valid(
                        {**graph.to_dict(), "driver_composition": drivers_wire}
                    )
                )


if __name__ == "__main__":
    unittest.main()
