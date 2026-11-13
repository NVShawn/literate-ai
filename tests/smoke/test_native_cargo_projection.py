"""Portable byte projection and ordinary Cargo controls, not lifecycle acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.dependencies.acquisition import _generated_lock_projection
from literate_ai.adapters.dependencies.types import DependencyObservationError
from literate_ai.adapters.directory_artifacts import DirectoryExportFile as File
from literate_ai.adapters.lifecycle.native_cargo_projection import (
    NativeCargoProviderSource as Source,
)
from literate_ai.adapters.lifecycle.native_cargo_projection import (
    project_native_cargo_graph as project,
)
from literate_ai.adapters.lifecycle.native_cargo_projection import (
    validate_native_cargo_metadata,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderEdge as Edge,
)
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderGraph as Graph,
)
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderGraphError,
)
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderNode as Node,
)


def identity(label):
    return canonical_identity({"native-cargo-projection-test": label})


def manifest(name):
    return (
        "[package]\nname=" + json.dumps(name) + '\nversion="1.0.0"\nedition="2021"\n'
    ).encode()


def source(name, code):
    original = manifest(name)
    digest = ContentIdentity.parse_uri("sha256:" + hashlib.sha256(original).hexdigest())
    node = Node(
        identity(name + "revision"),
        identity(name + "artifact"),
        identity(name + "surface"),
        identity(name + "source"),
        digest,
        name,
        "1.0.0",
    )
    return Source(
        node,
        "source/Cargo.toml",
        original,
        (
            File("source/Cargo.toml", original, 0o644),
            File("source/src/lib.rs", code.encode(), 0o644),
        ),
    )


class NativeCargoProjectionTests(unittest.TestCase):
    def setUp(self):
        self.root = identity("consumer")
        self.beta = source("beta", "pub struct Event { pub value: u32 }\n")
        self.alpha = source(
            "alpha", "pub fn read(e: &beta::Event) -> u32 { e.value }\n"
        )
        self.sources = (self.alpha, self.beta)
        edges = (
            Edge(self.root, self.alpha.node.component_revision, "alpha"),
            Edge(self.root, self.beta.node.component_revision, "beta"),
            Edge(
                self.alpha.node.component_revision,
                self.beta.node.component_revision,
                "beta",
            ),
        )
        self.graph = Graph(
            self.root,
            tuple(
                sorted(
                    (row.node for row in self.sources),
                    key=lambda row: row.component_revision.uri,
                )
            ),
            tuple(
                sorted(
                    edges,
                    key=lambda row: (
                        row.consumer_revision.uri,
                        row.alias,
                        row.provider_revision.uri,
                    ),
                )
            ),
        )
        self.original = manifest("consumer")

    def sealed_alpha(self):
        edge = Edge(
            self.alpha.node.component_revision,
            self.beta.node.component_revision,
            "beta",
        )
        graph = Graph(self.alpha.node.component_revision, (self.beta.node,), (edge,))
        projected, additions = project(
            graph=graph,
            root_manifest=self.alpha.original_manifest,
            root_manifest_path=self.alpha.manifest,
            sources=(self.beta,),
        )
        files = (
            tuple(
                replace(row, content=projected)
                if row.path == self.alpha.manifest
                else row
                for row in self.alpha.files
            )
            + additions
        )
        return replace(self.alpha, files=files)

    def projection(self, **changes):
        args = dict(
            graph=self.graph,
            root_manifest=self.original,
            root_manifest_path="source/Cargo.toml",
            sources=self.sources,
        )
        args.update(changes)
        return project(**args)

    def test_clean_projections_equal_and_original_values_unchanged(self):
        before = tuple(row.original_manifest for row in self.sources)
        first = self.projection()
        self.assertEqual(first, self.projection())
        self.assertEqual(before, tuple(row.original_manifest for row in self.sources))
        self.assertEqual(self.original, manifest("consumer"))
        record = next(row for row in first[1] if row.path.endswith("projection.json"))
        self.assertNotIn("/tmp", record.content.decode())
        self.assertEqual(json.loads(record.content)["graph"], self.graph.to_dict())

    def test_absent_or_substituted_node_source_refused(self):
        for sources in (
            (self.alpha,),
            (self.alpha, self.alpha),
            (
                self.alpha,
                replace(
                    self.beta,
                    node=replace(self.beta.node, artifact_identity=identity("other")),
                ),
            ),
        ):
            with self.assertRaisesRegex(NativeCargoProviderGraphError, "source_graph"):
                self.projection(sources=sources)

    def test_conflicting_manifest_and_root_package_refused(self):
        with self.assertRaisesRegex(
            NativeCargoProviderGraphError, "conflicting_dependency"
        ):
            self.projection(
                root_manifest=self.original + b'[dependencies]\nbeta="1.0.0"\n'
            )
        with self.assertRaisesRegex(
            NativeCargoProviderGraphError, "root_package_collision"
        ):
            self.projection(root_manifest=manifest("alpha"))

    def test_ambient_dependency_and_build_script_refused(self):
        for extra in (
            b'[dependencies]\nother={path="/ambient"}\n',
            b'[dependencies]\nother={git="https://example.invalid/x"}\n',
            b"[workspace]\n",
            b'[build-dependencies]\nother="1"\n',
            b'[lib]\npath="../escape.rs"\n',
        ):
            with self.assertRaises(NativeCargoProviderGraphError):
                self.projection(root_manifest=self.original + extra)
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "build_script"):
            replace(
                self.beta,
                files=self.beta.files + (File("source/build.rs", b"", 0o644),),
            )

    def test_original_manifest_drift_and_path_escape_refused(self):
        with self.assertRaisesRegex(
            NativeCargoProviderGraphError, "original_manifest_identity"
        ):
            replace(self.beta, original_manifest=self.beta.original_manifest + b"\n")
        with self.assertRaisesRegex(NativeCargoProviderGraphError, "manifest_path"):
            replace(self.beta, manifest="../source/Cargo.toml")

    def test_reprojection_discards_nested_graph_without_duplicate_nominal_package(self):
        sealed = self.sealed_alpha()
        self.assertGreater(len(sealed.files), len(self.alpha.files))
        original = self.projection()
        self.assertEqual(original, self.projection(sources=(sealed, self.beta)))
        for file in sealed.files:
            if file.path.endswith("original-manifest.toml"):
                changed = tuple(
                    replace(row, content=row.content + b"\n")
                    if row.path == file.path
                    else row
                    for row in sealed.files
                )
                with self.assertRaisesRegex(
                    NativeCargoProviderGraphError, "original_manifest_identity"
                ):
                    replace(sealed, files=changed)
        broken = tuple(
            row for row in sealed.files if not row.path.endswith("projection.json")
        )
        with self.assertRaisesRegex(
            NativeCargoProviderGraphError, "projection_record_missing"
        ):
            replace(sealed, files=broken)

    def test_casefold_build_script_and_reserved_namespace_refused(self):
        for path in ("source/build.rs", "source/Build.rs", "source/BUILD.RS"):
            with self.assertRaisesRegex(NativeCargoProviderGraphError, "build_script"):
                replace(self.beta, files=self.beta.files + (File(path, b"", 0o644),))
        for path in (".LITERATE/native-cargo/other", ".literate/NATIVE-CARGO/other"):
            with self.assertRaisesRegex(
                NativeCargoProviderGraphError, "reserved_namespace_alias"
            ):
                replace(self.beta, files=self.beta.files + (File(path, b"", 0o644),))

    def test_package_workspace_metadata_paths_and_cargo_config_refused(self):
        for extra in (
            b'workspace="/ambient"\n',
            b"authors={workspace=true}\n",
            b'readme="../outside"\n',
            b'license-file="/ambient/LICENSE"\n',
            b"[lints]\nworkspace=true\n",
        ):
            with self.assertRaises(NativeCargoProviderGraphError):
                self.projection(root_manifest=self.original + extra)
        for name in (
            "source/.cargo/config.toml",
            "source/.CARGO/config",
            ".cargo/config",
        ):
            with self.assertRaisesRegex(
                NativeCargoProviderGraphError, "package_cargo_configuration"
            ):
                replace(self.beta, files=self.beta.files + (File(name, b"", 0o644),))

    def test_returned_manifest_and_archive_overhead_are_inside_output_bound(self):
        rewritten, files = self.projection()
        bytes_of_additions = sum(len(row.content) for row in files)
        bytes_with_root = bytes_of_additions + len(rewritten)
        self.assertGreater(bytes_with_root, bytes_of_additions)
        with patch(
            "literate_ai.adapters.lifecycle.native_cargo_projection.MAX_PROJECTION_BYTES",
            bytes_with_root - 1,
        ):
            with self.assertRaisesRegex(
                NativeCargoProviderGraphError, "projection_bound"
            ):
                self.projection()

    def test_dependency_free_projection_preserves_exact_manifest_bytes(self):
        self.assertEqual(
            project(
                graph=Graph(self.root, (), ()),
                root_manifest=self.original,
                root_manifest_path="source/Cargo.toml",
                sources=(),
            ),
            (self.original, ()),
        )

    def test_metadata_unicode_and_tables_roundtrip(self):
        original = self.original + '[package.metadata]\nlabel="emoji 🐙"\n'.encode()
        root, _ = self.projection(root_manifest=original)
        self.assertIn("🐙", root.decode())

    @unittest.skipUnless(shutil.which("cargo"), "native Cargo tool unavailable")
    def test_real_cargo_shared_nominal_type_and_clean_relocated_run(self):
        projected, files = self.projection(sources=(self.sealed_alpha(), self.beta))
        with tempfile.TemporaryDirectory(
            prefix="native-cargo-projection-"
        ) as temporary:
            parent = Path(temporary)
            original = parent / "original"
            (original / "source/src").mkdir(parents=True)
            (original / "source/Cargo.toml").write_bytes(projected)
            (original / "source/src/main.rs").write_text(
                "fn main() { let e=beta::Event{value:7}; "
                "assert_eq!(alpha::read(&e),7); }\n"
            )
            for row in files:
                path = original / row.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(row.content)
            env = dict(os.environ)
            env["CARGO_INCREMENTAL"] = "0"
            cargo = shutil.which("cargo")

            def run(root, target):
                result = subprocess.run(
                    [
                        cargo,
                        "run",
                        "--offline",
                        "--quiet",
                        "--manifest-path",
                        str(root / "source/Cargo.toml"),
                        "--target-dir",
                        str(target),
                    ],
                    env=env,
                    cwd=root,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    result.returncode, 0, result.stderr.decode(errors="replace")
                )

            run(original, parent / "objects-a")
            metadata = subprocess.run(
                [
                    cargo,
                    "metadata",
                    "--offline",
                    "--format-version=1",
                    "--manifest-path",
                    str(original / "source/Cargo.toml"),
                ],
                cwd=original,
                env=env,
                capture_output=True,
                timeout=30,
                check=True,
            )
            document = json.loads(metadata.stdout)

            def validate(value):
                return validate_native_cargo_metadata(
                    graph=self.graph,
                    root_manifest=self.original,
                    root_manifest_path="source/Cargo.toml",
                    sources=(self.sealed_alpha(), self.beta),
                    projection_root=original,
                    metadata=value,
                )

            self.assertEqual(
                validate(document)["graph_identity"], self.graph.identity.uri
            )
            lock_files = {
                "source/Cargo.toml": self.original.decode(),
                "source/Cargo.lock": (original / "source/Cargo.lock").read_text(),
            }
            with self.assertRaises(DependencyObservationError):
                _generated_lock_projection(lock_files)
            lock_binding = _generated_lock_projection(
                lock_files,
                native_cargo_graph=self.graph,
                native_cargo_manifest="source/Cargo.toml",
            )
            self.assertEqual(len(lock_binding.packages), 3)
            self.assertTrue(all(row.managed_ref for row in lock_binding.packages))
            for mutate in (
                lambda value: value["packages"].append(value["packages"][0]),
                lambda value: value["packages"].append(
                    {
                        **next(
                            row for row in value["packages"] if row["name"] == "beta"
                        ),
                        "source": "registry+https://example.invalid/index",
                        "id": "registry+https://example.invalid/index#beta@1.0.0",
                    }
                ),
                lambda value: value["resolve"].update({"root": "wrong"}),
                lambda value: value["packages"][0].update(
                    {"manifest_path": "/ambient/Cargo.toml"}
                ),
                lambda value: next(
                    row for row in value["resolve"]["nodes"] if row["deps"]
                ).update({"deps": []}),
            ):
                changed = deepcopy(document)
                mutate(changed)
                with self.assertRaises(NativeCargoProviderGraphError):
                    validate(changed)
            packages = document["packages"]
            self.assertEqual(
                sorted(row["name"] for row in packages), ["alpha", "beta", "consumer"]
            )
            relocated = parent / "different-root" / "relocated"
            shutil.copytree(original, relocated)
            shutil.rmtree(original)
            run(relocated, parent / "objects-b")
