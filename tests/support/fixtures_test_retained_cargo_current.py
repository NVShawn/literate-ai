"""Shared fixtures extracted from ``tests.unit.test_retained_cargo_current``."""

import hashlib

import json

import tempfile

import unittest

from dataclasses import replace

from pathlib import Path

from literate_ai.adapters.retained_cargo_current import (
    read_retained_cargo_importer_authority,
)

from literate_ai.contracts.blobs import BlobRef

from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes

from literate_ai.contracts.repositories import (
    RepositoryBuildCommand,
)

from literate_ai.contracts.retained_libraries import RetainedLibraryGatePolicy

from literate_ai.projects import serialize_project_configuration

from tests.support import fixtures_test_project_configuration as projects

def blob(content):
    return BlobRef(hashlib.sha256(content).hexdigest(), len(content))

class RetainedCargoCurrentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ia-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "literate.project.json"
        self.project.write_bytes(
            serialize_project_configuration(projects._definition())
        )
        identity = canonical_identity("current retained build authority")
        self.plan = RetainedLibraryGatePolicy(
            projects._definition().project_id,
            (
                RepositoryBuildCommand("build", ("make", "build")),
                RepositoryBuildCommand("test", ("make", "test")),
            ),
            (identity,),
        )
        self.content = canonical_json_bytes(self.plan.to_dict())
        self.path = self.root / "gates.json"
        self.path.write_bytes(self.content)
        self.arguments = dict(
            reviewed_binding_identity=canonical_identity("explicit review"),
            configured_store_id="reviewed-store",
            gate_plan_path="gates.json",
            reviewed_gate_plan=blob(self.content),
        )

    def read(self, **changes):
        return read_retained_cargo_importer_authority(
            self.root, **{**self.arguments, **changes}
        )

    def test_complete_command_order_and_explicit_review_preserved_without_writes(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        current = self.read()
        self.assertEqual(current.gates, self.plan)
        self.assertEqual(current.gates.commands, self.plan.commands)
        self.assertEqual(
            current.reviewed_binding_identity,
            self.arguments["reviewed_binding_identity"],
        )
        self.assertEqual(current.configured_store_id, "reviewed-store")
        current.require_unchanged()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_foreign_gate_and_project_edits_invalidate_custody_and_are_preserved(self):
        for path in (self.path, self.project):
            original = path.read_bytes()
            with self.subTest(path=path.name):
                current = self.read()
                path.write_bytes(original + b" ")
                with self.assertRaises(ValueError):
                    current.require_unchanged()
                self.assertEqual(path.read_bytes(), original + b" ")
                path.write_bytes(original)

    def test_reviewed_policy_for_another_project_refuses(self):
        content = canonical_json_bytes(
            replace(self.plan, importer_project_id="foreign").to_dict()
        )
        self.path.write_bytes(content)
        with self.assertRaisesRegex(ValueError, "gate-project-mismatch"):
            self.read(reviewed_gate_plan=blob(content))

    def test_source_edit_preserves_policy_review(self):
        source = self.root / "consumer.rs"
        source.write_text("// first consumer revision\n")
        current = self.read()
        source.write_text("// next consumer revision\n")
        current.require_unchanged()
        self.assertEqual(self.read().gates.identity, current.gates.identity)

    def test_policy_roundtrip_and_command_order_are_identity_bound(self):
        policy = replace(
            self.plan,
            commands=(
                RepositoryBuildCommand("build", ("make", "build")),
                RepositoryBuildCommand("test", ("make", "test")),
            ),
        )
        self.assertEqual(RetainedLibraryGatePolicy.from_dict(policy.to_dict()), policy)
        for changes in (
            {"commands": tuple(reversed(policy.commands))},
            {"toolchains": (canonical_identity("replacement tool"),)},
            {"importer_project_id": "another-project"},
        ):
            self.assertNotEqual(replace(policy, **changes).identity, policy.identity)

    def test_malformed_policy_refuses_before_current_authority(self):
        wire = self.plan.to_dict()
        for changes in (
            {"commands": wire["commands"] * 129},
            {"commands": [wire["commands"][0]] * 2},
            {"commands": [True]},
            {"toolchains": []},
            {"toolchains": wire["toolchains"] * 65},
            {"toolchains": [wire["toolchains"][0]] * 2},
            {"toolchains": ["claimed-tool"]},
            {"importer_project_id": "bad\nproject"},
            {"schema": "literate-ai/repository-build-plan@1"},
            {"source_lock": canonical_identity("decorative source").to_dict()},
        ):
            content = canonical_json_bytes({**wire, **changes})
            self.path.write_bytes(content)
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "gate-plan-invalid"),
            ):
                self.read(reviewed_gate_plan=blob(content))

    def test_wrong_review_size_and_bound_refuse(self):
        for changes in (
            {"reviewed_gate_plan": replace(blob(self.content), digest="0" * 64)},
            {
                "reviewed_gate_plan": replace(
                    blob(self.content), size=len(self.content) + 1
                )
            },
            {"maximum_file_bytes": 1},
            {"maximum_file_bytes": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.read(**changes)

    def test_duplicate_unknown_fields_and_removed_commands_refuse(self):
        values = [b'{"schema":"duplicate",' + self.content[1:]]
        for changes in ({"commands": []}, {"unknown": True}):
            values.append(canonical_json_bytes({**json.loads(self.content), **changes}))
        for content in values:
            self.path.write_bytes(content)
            with (
                self.subTest(content=content[:40]),
                self.assertRaisesRegex(ValueError, "gate-plan-invalid"),
            ):
                self.read(reviewed_gate_plan=blob(content))

    def test_path_alias_and_store_url_refuse(self):
        for changes in (
            {"gate_plan_path": "../outside.json"},
            {"gate_plan_path": "literate.project.json"},
            {"configured_store_id": "https://user:secret@example.test/archive"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.read(**changes)

    def test_symlinked_gate_file_refuses(self):
        self.path.rename(self.root / "actual.json")
        try:
            self.path.symlink_to(self.root / "actual.json")
        except OSError:
            self.skipTest("host does not permit symbolic links")
        with self.assertRaises(ValueError):
            self.read()

