"""Complete input ownership is bound to canonical authoring, never acceptance."""

import unittest
from dataclasses import replace

from literate_ai.contracts.retained_component_ownership import (
    RetainedComponentOwner,
    require_registered_retained_owners,
    retained_owner,
)
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectManifest,
    RetainedProjectMember,
)
from tests.support.fixtures_test_component_lock_contracts import component_authoring


class RetainedComponentOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.shared = component_authoring("shared")
        self.library = component_authoring("library")
        self.catalog = (self.shared, self.library)
        self.owners = (
            self.owner("tree", self.shared),
            self.owner("tree/lib", self.library),
        )
        self.legacy = RetainedProjectManifest(
            ("tree",),
            tuple(
                RetainedProjectMember(p, 1, 0o644, "0" * 64, "source")
                for p in ("tree/lib/file.c", "tree/rules.mk")
            ),
            RetainedProjectLimits(100, 100, 1000),
            ("tree", "tree/empty", "tree/lib"),
        )

    def owner(self, prefix, authoring):
        return RetainedComponentOwner(prefix, authoring.coordinate, authoring.identity)

    def test_specific_prefixes_preserve_shared_infrastructure_and_empty_directories(
        self,
    ):
        manifest = replace(self.legacy, component_ownership=self.owners)
        index = {owner.prefix: owner for owner in manifest.component_ownership}
        self.assertEqual(
            retained_owner("tree/lib/file.c", index).component, self.library.coordinate
        )
        self.assertEqual(
            retained_owner("tree/rules.mk", index).component, self.shared.coordinate
        )
        self.assertEqual(
            retained_owner("tree/empty", index).component, self.shared.coordinate
        )
        self.assertEqual(
            require_registered_retained_owners(self.owners, self.catalog),
            tuple(
                sorted(
                    (self.shared.identity, self.library.identity), key=lambda i: i.uri
                )
            ),
        )
        self.assertEqual(
            RetainedProjectManifest.from_dict(manifest.to_dict()), manifest
        )

    def test_legacy_wire_identity_is_unchanged_and_empty_opt_in_is_invalid(self):
        wire = self.legacy.to_dict()
        self.assertNotIn("component_ownership", wire)
        self.assertEqual(
            RetainedProjectManifest.from_dict(wire).identity, self.legacy.identity
        )
        for bad in ([], None, {}, False):
            with self.assertRaises(ValueError):
                RetainedProjectManifest.from_dict({**wire, "component_ownership": bad})

    def test_uncovered_directory_cannot_be_hidden_by_covering_all_files(self):
        # Files alone do not account for the tree root and empty directory.
        with self.assertRaisesRegex(ValueError, "no canonical Component owner"):
            replace(
                self.legacy,
                component_ownership=(
                    self.owner("tree/lib/file.c", self.library),
                    self.owner("tree/rules.mk", self.shared),
                ),
            )

    def test_duplicate_alias_absent_and_out_of_order_prefixes_refuse(self):
        for owners in (
            (self.owners[0], self.owners[0]),
            tuple(reversed(self.owners)),
            (self.owner("missing", self.shared),),
            (self.owner("TREE", self.shared), self.owners[0]),
        ):
            with self.subTest(owners=owners), self.assertRaises(ValueError):
                replace(self.legacy, component_ownership=owners)

    def test_inconsistent_component_authority_is_not_resolved_by_prefix_order(self):
        with self.assertRaisesRegex(ValueError, "conflicting authorings"):
            replace(
                self.legacy,
                component_ownership=(
                    self.owners[0],
                    replace(self.owners[1], component=self.shared.coordinate),
                ),
            )

    def test_unknown_and_changed_canonical_owners_do_not_register_themselves(self):
        with self.assertRaisesRegex(ValueError, "component-missing-or-stale"):
            require_registered_retained_owners(self.owners, (self.shared,))
        changed = replace(self.library, description="Changed authoring")
        with self.assertRaisesRegex(ValueError, "component-missing-or-stale"):
            require_registered_retained_owners(self.owners, (self.shared, changed))
        with self.assertRaises(TypeError):
            require_registered_retained_owners(self.owners, (self.shared.to_dict(),))

    def test_changed_ownership_and_shared_input_invalidate_manifest_identity(self):
        manifest = replace(self.legacy, component_ownership=self.owners)
        moved = replace(
            manifest, component_ownership=(self.owner("tree", self.library),)
        )
        self.assertNotEqual(manifest.identity, moved.identity)
        changed = replace(
            manifest,
            members=(
                *manifest.members[:1],
                replace(manifest.members[1], sha256="1" * 64),
            ),
        )
        self.assertNotEqual(manifest.identity, changed.identity)

    def test_owner_wire_fields_and_portable_paths_are_strict(self):
        for bad in (
            {**self.owners[0].to_dict(), "acceptance": "passed"},
            {"prefix": "tree"},
        ):
            with self.assertRaises(ValueError):
                RetainedComponentOwner.from_dict(bad)
        for prefix in ("../outside", "C:drive", "tree/CON", "tree\\lib"):
            with self.assertRaises((ValueError, TypeError)):
                self.owner(prefix, self.shared)
        with self.assertRaises(TypeError):
            replace(self.owners[0], authoring_identity="sha256:" + "0" * 64)

    def test_large_inventory_avoids_file_directory_cross_product(
        self,
    ):
        directories = tuple(
            sorted({"tree", *(f"tree/unit{n:05}" for n in range(8000))})
        )
        members = tuple(
            RetainedProjectMember(
                f"tree/unit{n:05}/file{i}.c", 1, 0o644, "0" * 64, "source"
            )
            for n in range(8000)
            for i in range(5)
        )
        owners = (self.owner("tree", self.shared),) + tuple(
            self.owner(directory, self.library)
            for directory in directories
            if directory != "tree"
        )
        manifest = RetainedProjectManifest(
            ("tree",),
            members,
            RetainedProjectLimits(50000, 10, 100000),
            directories,
            owners,
        )
        self.assertEqual(len(manifest.members), 40000)
        self.assertEqual(len(manifest.component_ownership), 8001)

    def test_indexed_directory_validation_still_rejects_collisions_and_missing_parents(
        self,
    ):
        for directories in (
            ("tree", "tree/LIB/FILE.C"),
            ("tree", "tree/lib", "tree/rules.mk/child"),
            ("tree", "tree/empty"),
        ):
            with self.subTest(directories=directories), self.assertRaises(ValueError):
                replace(self.legacy, directories=directories)
        with self.assertRaises(ValueError):
            replace(self.legacy, directories=(*self.legacy.directories, "outside"))


if __name__ == "__main__":
    unittest.main()
