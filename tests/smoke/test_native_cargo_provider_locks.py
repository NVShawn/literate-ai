"""Qualified provider registry constraints remain exact after Cargo re-resolution."""

from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from literate_ai.adapters.directory_artifacts import DirectoryExportFile as File
from literate_ai.adapters.lifecycle.native_cargo_inputs import (
    validate_native_cargo_provider_locks,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.native_cargo_provider_graph import (
    NativeCargoProviderGraphError,
)
from tests.smoke import test_native_cargo_projection as fixture


class NativeCargoProviderLockTests(unittest.TestCase):
    def setUp(self):
        source = fixture.source("beta", "pub struct Event;\n")
        manifest = source.original_manifest + b'[dependencies]\nexternal="2"\n'
        node = replace(
            source.node,
            original_manifest_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(manifest).hexdigest()
            ),
        )
        self.provider_lock = (
            '''version=4
[[package]]
name="beta"
version="1.0.0"
dependencies=["external"]
[[package]]
name="external"
version="2.0.0"
source="registry+https://example.invalid/index"
checksum="'''
            + "a" * 64
            + '"\n'
        )
        self.source = replace(
            source,
            node=node,
            original_manifest=manifest,
            files=tuple(
                replace(row, content=manifest) if row.path == source.manifest else row
                for row in source.files
            )
            + (File("source/Cargo.lock", self.provider_lock.encode(), 0o644),),
        )
        self.root = fixture.identity("consumer")
        self.graph = fixture.Graph(
            self.root,
            (node,),
            (fixture.Edge(self.root, node.component_revision, "beta"),),
        )
        self.manifest = fixture.manifest("consumer")
        self.lock = (
            self.provider_lock
            + """[[package]]
name="consumer"
version="1.0.0"
dependencies=["beta"]
"""
        )

    def validate(self, **changes):
        args = dict(
            graph=self.graph,
            sources=(self.source,),
            consumer_manifest=self.manifest,
            consumer_lock=self.lock.encode(),
        )
        args.update(changes)
        return validate_native_cargo_provider_locks(**args)

    def test_exact_provider_registry_inventory_edges_and_checksum_pass(self):
        result = self.validate()
        self.assertEqual(
            result,
            [
                {
                    "component_revision": self.source.node.component_revision.uri,
                    "lock_identity": "sha256:"
                    + hashlib.sha256(self.provider_lock.encode()).hexdigest(),
                }
            ],
        )
        self.assertEqual(
            result, self.validate(consumer_manifest=None, consumer_lock=None)
        )

    def test_changed_registry_version_source_checksum_and_edges_refuse(self):
        for lock in (
            self.lock.replace('version="2.0.0"', 'version="2.1.0"'),
            self.lock.replace("example.invalid", "different.invalid"),
            self.lock.replace("a" * 64, "b" * 64),
            self.lock.replace('dependencies=["external"]', "dependencies=[]"),
        ):
            with (
                self.subTest(lock=lock),
                self.assertRaises(NativeCargoProviderGraphError),
            ):
                self.validate(consumer_lock=lock.encode())

    def test_missing_provider_lock_refuses_before_consumer_solver(self):
        source = replace(
            self.source,
            files=tuple(
                row for row in self.source.files if not row.path.endswith("Cargo.lock")
            ),
        )
        with self.assertRaisesRegex(
            NativeCargoProviderGraphError, "provider_lock_missing"
        ):
            self.validate(sources=(source,), consumer_lock=None, consumer_manifest=None)

    def test_extra_consumer_registry_dependency_preserves_provider_resolution(self):
        lock = self.lock.replace(
            'dependencies=["beta"]', 'dependencies=["beta", "other"]'
        )
        lock += (
            '[[package]]\nname="other"\nversion="3.0.0"\nsource="registry+https://example.invalid/index"\nchecksum="'
            + "b" * 64
            + '"\n'
        )
        self.assertEqual(self.validate(), self.validate(consumer_lock=lock.encode()))
