"""Exact inert metadata transitions, separate from live application authority."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters import repository_refresh_metadata as metadata
from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh import (
    RefreshFileObservation,
    RefreshReferenceObservation,
)
from literate_ai.adapters.repository_refresh_worktrees import RegisteredRefreshWorktree
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshAuthority,
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)
from literate_ai.projects import parse_project_configuration
from tests.unit import test_repository_refresh_inputs as fixtures


class RefreshMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = fixtures.RepositoryRefreshInputTests()
        cls.addClassCleanup(fixture.doCleanups)
        fixture.setUp()
        cls.prepared = fixture.prepare()

    def prepare(self, **changes):
        prepared = replace(self.prepared, **changes)
        if "children" in changes:
            # These pure metadata fixtures replace captured checkout observations;
            # keep their synthetic registry rows coherent with those observations.
            observations = (prepared.root_git, *prepared.children)
            prepared = replace(
                prepared,
                worktrees=tuple(
                    replace(
                        registry,
                        worktrees=tuple(
                            RegisteredRefreshWorktree(
                                item.root,
                                item.commit,
                                item.symbolic_reference,
                                False,
                                False,
                                False,
                            )
                            for item in observations
                            if item.common_directory == registry.common_directory
                        ),
                    )
                    for registry in prepared.worktrees
                ),
            )
        return prepared

    def request(self, *targets):
        return RepositoryRefreshAuthority(
            self.prepared.authority.previous,
            RepositoryRefreshRequest(
                tuple(RepositoryRefreshTarget(*item) for item in targets)
            ),
        )

    def transitions(self, prepared=None):
        return metadata.refresh_metadata_transitions(prepared or self.prepared)

    def attached(self, *, packed=False):
        child = self.prepared.children[0]
        name = "refs/heads/topic"
        reference = RefreshReferenceObservation(
            name,
            RefreshFileObservation(
                child.common_directory / name,
                None if packed else (1, 2, 3),
                None if packed else (child.commit + "\n").encode(),
            ),
        )
        return replace(
            child,
            head=replace(child.head, content=("ref: " + name + "\n").encode()),
            symbolic_reference=name,
            references=(reference,),
        )

    def test_changed_binding_only_changes_selected_manifest_commits(self):
        transitions = self.transitions()
        manifest = transitions[-1]
        self.assertEqual(manifest.kind, "manifest")
        before = parse_project_configuration(self.prepared.manifest.content)
        after = parse_project_configuration(manifest.prospective)
        self.assertEqual(
            after,
            replace(
                before, repository_orchestration=self.prepared.authority.prospective
            ),
        )
        self.assertEqual(manifest.before.content, self.prepared.manifest.content)
        self.assertEqual(manifest.to_dict()["operation"], "replace")
        heads = {item.repository: item for item in transitions if item.kind == "head"}
        self.assertEqual(heads["app"].prospective, b"4" * 40 + b"\n")
        self.assertEqual(heads["."].prospective, heads["."].before.content)
        self.assertEqual(heads["lib"].prospective, heads["lib"].before.content)

    def test_transition_identity_preserves_oversized_os_node_identifiers(self):
        transition = self.transitions()[0]
        changed = replace(
            transition,
            before=replace(
                transition.before,
                signature=(2**63, *transition.before.signature[1:]),
            ),
        )

        wire = changed.to_dict()

        self.assertEqual(
            wire["before_signature"][0],
            {"integer_decimal": str(2**63)},
        )
        self.assertTrue(canonical_identity(wire).uri.startswith("sha256:"))

    def test_changed_binding_invalidates_existing_lock_but_does_not_create_one(self):
        for content, operation in (
            (b"retained lock bytes\n", "remove"),
            (None, "retain"),
        ):
            prepared = self.prepare(
                repository_lock=replace(self.prepared.repository_lock, content=content)
            )
            transition = next(
                item for item in self.transitions(prepared) if item.kind == "lock"
            )
            self.assertEqual(transition.before.content, content)
            self.assertIsNone(transition.prospective)
            self.assertEqual(transition.to_dict()["operation"], operation)

    def test_noop_preserves_noncanonical_manifest_and_lock_bytes_exactly(self):
        pin = next(
            item
            for item in self.prepared.authority.previous.repositories
            if item.path == "app"
        )
        before = self.prepared.manifest.content + b" \n"
        prepared = self.prepare(
            authority=self.request(("app", pin.commit)),
            manifest=replace(self.prepared.manifest, content=before),
            repository_lock=replace(
                self.prepared.repository_lock, content=b"retained stale lock\n"
            ),
        )
        for item in self.transitions(prepared):
            self.assertEqual(item.prospective, item.before.content)
            self.assertEqual(item.to_dict()["operation"], "retain")

    def test_attached_updates_preserve_head_symbolic_hops_and_packed_bytes(self):
        for packed in (False, True):
            child = self.attached(packed=packed)
            alias = RefreshReferenceObservation(
                "refs/heads/alias",
                RefreshFileObservation(
                    child.common_directory / "refs/heads/alias",
                    (4, 5, 6),
                    child.head.content,
                ),
            )
            child = replace(
                child,
                head=replace(child.head, content=b"ref: refs/heads/alias\n"),
                references=(alias, *child.references),
            )
            prepared = self.prepare(children=(child, self.prepared.children[1]))
            changes = [
                item for item in self.transitions(prepared) if item.repository == "app"
            ]
            terminal = next(item for item in changes if item.name == "refs/heads/topic")
            self.assertEqual(terminal.prospective, b"4" * 40 + b"\n")
            self.assertEqual(
                terminal.to_dict()["operation"], "create" if packed else "replace"
            )
            for item in changes:
                if item is not terminal:
                    self.assertEqual(item.prospective, item.before.content)

    def test_packed_noop_keeps_loose_reference_absent(self):
        child = self.attached(packed=True)
        prepared = self.prepare(
            children=(child, self.prepared.children[1]),
            authority=self.request(("app", child.commit)),
        )
        terminal = next(
            item
            for item in self.transitions(prepared)
            if item.name == "refs/heads/topic"
        )
        self.assertIsNone(terminal.prospective)
        self.assertEqual(terminal.to_dict()["operation"], "retain")

    def test_shared_ref_conflict_with_unselected_or_different_selected_target_refuses(
        self,
    ):
        app = self.attached()
        lib = replace(
            self.prepared.children[1],
            references=app.references,
            symbolic_reference=app.symbolic_reference,
            common_directory=app.common_directory,
            packed_references=app.packed_references,
        )
        for authority in (
            self.prepared.authority,
            self.request(("app", "4" * 40), ("lib", "5" * 40)),
        ):
            prepared = self.prepare(children=(app, lib), authority=authority)
            with self.assertRaises(OrchestrationInventoryError) as caught:
                self.transitions(prepared)
            self.assertEqual(
                caught.exception.code, "orchestration.refresh_shared_ref_conflict"
            )
        prepared = self.prepare(
            children=(app, lib),
            authority=self.request(("app", "4" * 40), ("lib", "4" * 40)),
        )
        shared = [
            item
            for item in self.transitions(prepared)
            if item.before.path == app.references[0].file.path
        ]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0].prospective, b"4" * 40 + b"\n")

    def test_format_mismatch_manifest_drift_and_byte_limit_refuse(self):
        with self.assertRaises(OrchestrationInventoryError):
            self.transitions(self.prepare(authority=self.request(("app", "4" * 64))))
        wire = json.loads(self.prepared.manifest.content)
        wire["repository_orchestration"] = self.prepared.authority.prospective.to_dict()
        with self.assertRaises(OrchestrationInventoryError):
            self.transitions(
                self.prepare(
                    manifest=replace(
                        self.prepared.manifest, content=json.dumps(wire).encode()
                    )
                )
            )
        with patch.object(metadata, "DEFAULT_MAXIMUM_PROJECT_CONFIGURATION_BYTES", 1):
            with self.assertRaises(OrchestrationInventoryError):
                self.transitions()

    def test_derivation_does_not_perform_io_or_grant_saved_record_authority(self):
        with (
            patch("builtins.open", side_effect=AssertionError("file access")),
            patch("io.open", side_effect=AssertionError("file access")),
            patch("os.open", side_effect=AssertionError("descriptor access")),
            patch("subprocess.Popen", side_effect=AssertionError("process access")),
        ):
            transitions = self.transitions()
        self.assertTrue(transitions)
        with self.assertRaises(TypeError):
            self.transitions(transitions[-1].to_dict())
