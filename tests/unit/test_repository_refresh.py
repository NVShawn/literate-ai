"""Explicit refresh intent changes only selected commits in portable authority."""

from __future__ import annotations

import copy
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

from literate_ai.contracts.repository_orchestration import (
    RepositoryOrchestration,
    RepositoryPin,
    RepositoryRelationship,
)
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshAuthority,
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)


def authority() -> RepositoryOrchestration:
    return RepositoryOrchestration(
        "sha256:" + "a" * 64,
        (
            RepositoryPin("app", "app", "../app.git", "1" * 40, "."),
            RepositoryPin("lib", "lib", "git@example.test:team/lib.git", "2" * 40),
            RepositoryPin(
                "api", "api", "https://example.test/api.git", "3" * 64, "main"
            ),
        ),
        (
            RepositoryRelationship("app", "lib"),
            RepositoryRelationship("lib", "app"),
        ),
    )


def request(*paths: str) -> RepositoryRefreshRequest:
    return RepositoryRefreshRequest(
        tuple(RepositoryRefreshTarget(path, "4" * 40) for path in paths)
    )


class RepositoryRefreshRequestTests(unittest.TestCase):
    def test_exact_request_roundtrip_and_order_independent_identity(self):
        first, second = request("lib", "app"), request("app", "lib")
        self.assertEqual(first, second)
        self.assertEqual(first.identity, second.identity)
        self.assertEqual(RepositoryRefreshRequest.from_dict(first.to_dict()), first)
        self.assertEqual([item.path for item in first.targets], ["app", "lib"])

    def test_targets_reject_nonexact_commits_and_unsafe_paths(self):
        for commit in ("main", "HEAD", "A" * 40, "0" * 40, "0" * 64, "1" * 39, True):
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                RepositoryRefreshTarget("app", commit)
        for path in ("", "../app", "/app", "a//b", "a\\b", "CON", "a" * 4097):
            with self.subTest(path=path), self.assertRaises((TypeError, ValueError)):
                RepositoryRefreshTarget(path, "4" * 40)

    def test_sha256_targets_are_exact_and_identity_bound(self):
        target = RepositoryRefreshTarget("api", "4" * 64)
        value = RepositoryRefreshRequest((target,))
        self.assertEqual(RepositoryRefreshRequest.from_dict(value.to_dict()), value)
        self.assertNotEqual(
            value.identity,
            RepositoryRefreshRequest((replace(target, commit="5" * 64),)).identity,
        )

    def test_duplicate_alias_and_overlapping_targets_refuse(self):
        for paths in (("app", "app"), ("app", "APP"), ("app", "app/nested")):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                request(*paths)

    def test_mutable_untyped_empty_and_excessive_target_collections_refuse(self):
        target = RepositoryRefreshTarget("app", "4" * 40)
        for targets in ([], [target], (), (target,) * 129, ({"path": "app"},)):
            with (
                self.subTest(targets_type=type(targets)),
                self.assertRaises(ValueError),
            ):
                RepositoryRefreshRequest(targets)
        value = RepositoryRefreshRequest(
            tuple(RepositoryRefreshTarget(f"repo-{i}", "4" * 40) for i in range(128))
        )
        self.assertEqual(len(value.targets), 128)

    def test_wire_rejects_extra_missing_and_wrong_typed_fields(self):
        original = request("app").to_dict()
        variants = [
            {**original, "schema": "other"},
            {**original, "execute": True},
            {"targets": original["targets"]},
            {**original, "targets": ()},
            {**original, "targets": []},
            {**original, "targets": original["targets"] * 129},
        ]
        for field, value in (
            ("url", "../other.git"),
            ("branch", "next"),
            ("dirty", False),
        ):
            wire = copy.deepcopy(original)
            wire["targets"][0][field] = value
            variants.append(wire)
        missing = copy.deepcopy(original)
        del missing["targets"][0]["commit"]
        variants.append(missing)
        for index, wire in enumerate(variants):
            with self.subTest(index=index), self.assertRaises((TypeError, ValueError)):
                RepositoryRefreshRequest.from_dict(wire)


class RepositoryRefreshAuthorityTests(unittest.TestCase):
    def test_single_target_preserves_all_noncommit_authority(self):
        before = authority()
        prepared = RepositoryRefreshAuthority(before, request("app"))
        self.assertEqual(prepared.previous, before)
        self.assertNotEqual(prepared.prospective.identity, before.identity)
        expected = before.to_dict()
        next(pin for pin in expected["repositories"] if pin["path"] == "app")[
            "commit"
        ] = "4" * 40
        self.assertEqual(prepared.prospective.to_dict(), expected)
        self.assertEqual(
            prepared.to_dict()["changes"],
            [
                {
                    "path": "app",
                    "previous_commit": "1" * 40,
                    "prospective_commit": "4" * 40,
                }
            ],
        )

    def test_multiple_selected_commits_change_together_in_one_delta(self):
        before = authority()
        prepared = RepositoryRefreshAuthority(before, request("lib", "app"))
        commits = {pin.path: pin.commit for pin in prepared.prospective.repositories}
        self.assertEqual(commits, {"api": "3" * 64, "app": "4" * 40, "lib": "4" * 40})
        self.assertEqual(
            [item["path"] for item in prepared.to_dict()["changes"]], ["app", "lib"]
        )
        self.assertEqual(prepared.prospective.relationships, before.relationships)

    def test_noop_intent_is_bound_without_fabricating_changes(self):
        before = authority()
        app = RepositoryRefreshRequest((RepositoryRefreshTarget("app", "1" * 40),))
        lib = RepositoryRefreshRequest((RepositoryRefreshTarget("lib", "2" * 40),))
        first = RepositoryRefreshAuthority(before, app)
        second = RepositoryRefreshAuthority(before, lib)
        self.assertEqual(first.prospective, before)
        self.assertEqual(second.prospective, before)
        self.assertEqual(first.to_dict()["changes"], [])
        self.assertNotEqual(first.identity, second.identity)

    def test_unknown_nonexact_and_nested_child_paths_refuse(self):
        for path in ("missing", "APP", "app/nested", ".git"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                RepositoryRefreshAuthority(authority(), request(path))

    def test_request_and_every_previous_authority_field_bind_the_delta(self):
        before = authority()
        original = RepositoryRefreshAuthority(before, request("app"))
        pin = before.repositories[0]
        variants = [
            replace(before, gitmodules_identity="sha256:" + "b" * 64),
            replace(before, relationships=()),
        ]
        for update in (
            {"name": "api-renamed"},
            {"path": "api-renamed"},
            {"url": "../other.git"},
            {"branch": "next"},
            {"commit": "5" * 64},
        ):
            variants.append(
                replace(
                    before,
                    repositories=(replace(pin, **update), *before.repositories[1:]),
                )
            )
        for changed in variants:
            with self.subTest(identity=changed.identity):
                self.assertNotEqual(
                    RepositoryRefreshAuthority(changed, request("app")).identity,
                    original.identity,
                )
        self.assertNotEqual(
            RepositoryRefreshAuthority(before, request("lib")).identity,
            original.identity,
        )
        changed_selected = replace(
            before,
            repositories=tuple(
                replace(item, commit="6" * 40) if item.path == "app" else item
                for item in before.repositories
            ),
        )
        changed = RepositoryRefreshAuthority(changed_selected, request("app"))
        self.assertEqual(changed.prospective, original.prospective)
        self.assertNotEqual(changed.identity, original.identity)

    def test_types_and_computed_replacement_authority_cannot_be_overridden(self):
        with self.assertRaises(TypeError):
            RepositoryRefreshAuthority(authority().to_dict(), request("app"))
        with self.assertRaises(TypeError):
            RepositoryRefreshAuthority(authority(), request("app").to_dict())
        with self.assertRaises(TypeError):
            RepositoryRefreshAuthority(
                authority(), request("app"), prospective=authority()
            )

    def test_immutable_records_and_detached_wire_values(self):
        prepared = RepositoryRefreshAuthority(authority(), request("app"))
        identity = prepared.identity
        for value, field, replacement in (
            (prepared, "prospective", authority()),
            (prepared.request, "targets", ()),
            (prepared.request.targets[0], "commit", "6" * 40),
        ):
            with self.subTest(field=field), self.assertRaises(FrozenInstanceError):
                setattr(value, field, replacement)
        wire = prepared.to_dict()
        wire["prospective_authority"]["repositories"].clear()
        wire["request"]["targets"].clear()
        self.assertEqual(prepared.identity, identity)

    def test_preparation_neither_performs_io_nor_claims_publication_or_apply(self):
        with (
            patch(
                "builtins.open", side_effect=AssertionError("unexpected file access")
            ),
            patch("io.open", side_effect=AssertionError("unexpected file access")),
            patch("os.open", side_effect=AssertionError("unexpected descriptor")),
            patch("subprocess.Popen", side_effect=AssertionError("unexpected process")),
        ):
            prepared = RepositoryRefreshAuthority(authority(), request("app"))
            wire = prepared.to_dict()
        self.assertEqual(wire["publication"], "not-checked")
        for key in ("apply_supported", "writes", "execution"):
            self.assertIs(wire[key], False)
        self.assertEqual(
            wire["previous_authority_identity"], prepared.previous.identity
        )
        self.assertEqual(
            wire["prospective_authority_identity"], prepared.prospective.identity
        )
        self.assertEqual(wire["request_identity"], prepared.request.identity)
