"""Real authorized source builds retain SDK aliases and current runtime bytes."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import unittest

from literate_ai.adapters.generation_preparation import (
    GenerationPreparationError,
    load_flavor_contributions,
)
from literate_ai.adapters.native_sdk_build import NativeSdkRepositoryBuilder
from literate_ai.adapters.native_sdk_recipes import select_native_sdk_recipes
from literate_ai.adapters.native_sdk_runtime import observe_native_sdk_runtime
from literate_ai.adapters.native_sdk_tree_custody import (
    materialize_native_sdk_tree_snapshot,
)
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.native_sdk_tree_products import (
    NATIVE_SDK_TREE_BUILD_RECIPE_CONTENT_KIND,
    NativeSdkTreeBuildLayout,
    NativeSdkTreeBuildRecipe,
    NativeSdkTreeSnapshot,
)
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.security import DirectBuildAuthorizationVerifier
from tests.support import fixtures_test_native_sdk_build as build_fixture
from tests.support import fixtures_test_native_sdk_recipes as recipe_fixture


@unittest.skipIf(os.name == "nt", "Symbolic SDK producer fixture requires POSIX")
class NativeSdkTreeBuildTests(unittest.TestCase):
    def test_authorized_source_producer_relocates_aliases_and_refuses_topology_drift(
        self,
    ):
        fixture = build_fixture.NativeSdkBuildTests(
            "test_real_repository_pipeline_retains_native_sdk_for_relocation"
        )
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        old = fixture.layout
        original = old.native_libraries[-1]
        hard = original + ".hard"
        symbolic = original + ".symbolic"
        fixture.layout = NativeSdkTreeBuildLayout(
            **{
                field.name: getattr(old, field.name)
                for field in dataclasses.fields(old)
                if field.name != "native_libraries"
            },
            native_libraries=tuple(sorted((old.native_libraries[0], hard, symbolic))),
            limits=RetainedProjectLimits(1000, 64 * 1024 * 1024, 128 * 1024 * 1024),
        )
        fixture.builder = NativeSdkRepositoryBuilder(
            layout=fixture.layout,
            tools=fixture.tools,
            store=fixture.store,
            grant_lookup=lambda identity: fixture.grants[identity.uri],
            environment=dict(os.environ),
            authorization_verifier=DirectBuildAuthorizationVerifier(),
        )
        fixture.plan = dataclasses.replace(
            fixture.plan,
            evidence=(fixture.layout.identity,),
            commands=(
                *fixture.plan.commands,
                RepositoryBuildCommand(
                    "hardlink",
                    (
                        "cmake",
                        "-E",
                        "create_hardlink",
                        old.sdk_root + "/" + original,
                        old.sdk_root + "/" + hard,
                    ),
                ),
                RepositoryBuildCommand(
                    "symlink",
                    (
                        "cmake",
                        "-E",
                        "create_symlink",
                        original.rsplit("/", 1)[-1],
                        old.sdk_root + "/" + symbolic,
                    ),
                ),
            ),
        )
        verification = fixture.verify()
        product = fixture.builder.product(verification)
        snapshot = product.snapshot
        self.assertIsInstance(snapshot, NativeSdkTreeSnapshot)
        self.assertEqual(snapshot.recipe_identity, fixture.plan.identity)
        self.assertEqual(snapshot.source_lock_identity, fixture.lock.identity)
        self.assertEqual(
            {alias.kind for alias in snapshot.tree.aliases}, {"hardlink", "symlink"}
        )
        fixture.remove_source()
        parent = fixture.root / "placements"
        parent.mkdir()
        placed = materialize_native_sdk_tree_snapshot(
            snapshot,
            expected_identity=snapshot.identity,
            store=fixture.store,
            limits=fixture.layout.limits,
            parent=parent,
        )
        self.assertEqual(
            (placed / original).stat().st_ino, (placed / hard).stat().st_ino
        )
        self.assertTrue((placed / symbolic).is_symlink())

        def observe():
            return observe_native_sdk_runtime(
                snapshot,
                expected_identity=snapshot.identity,
                target_identity=fixture.plan.flavor_set,
                operating_system=old.operating_system,
                architecture=old.architecture,
                root=placed,
                store=fixture.store,
            )

        observe()
        executed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                (
                    "import sys;sys.path.insert(0,sys.argv[1]);"
                    "import vendor_math;print(vendor_math.scale(1.5,3))"
                ),
                str(placed / snapshot.import_root),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertEqual(executed.stdout.strip(), "4.5")
        data = (placed / hard).read_bytes()
        (placed / hard).unlink()
        (placed / hard).write_bytes(data)
        with self.assertRaises(ValueError):
            observe()

    def test_locked_tree_recipe_selection_and_invalid_slot_refusal(self):
        fixture = recipe_fixture.NativeSdkRecipeTests(
            "test_selected_flavor_recipe_drives_real_native_build_and_relocation"
        )
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        old = fixture.recipe
        layout = NativeSdkTreeBuildLayout(
            **{
                field.name: getattr(old.layout, field.name)
                for field in dataclasses.fields(old.layout)
            },
            limits=RetainedProjectLimits(1000, 64 * 1024 * 1024, 128 * 1024 * 1024),
        )
        fixture.recipe = NativeSdkTreeBuildRecipe(
            old.dependency_id, layout, old.tools, old.commands
        )
        fixture.write_recipe(fixture.recipe)
        path = fixture.flavors / "lang-python/flavor.md"
        document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
        for contribution in document["contributions"]:
            if contribution["contribution_id"] == "fixture-native-sdk":
                contribution["content"]["kind"] = (
                    NATIVE_SDK_TREE_BUILD_RECIPE_CONTENT_KIND
                )
        path.write_bytes(render_authoring_markdown(document, body))
        snapshot = fixture.snapshot()
        selected = select_native_sdk_recipes(
            snapshot, snapshot.authority.lock.root_revision
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].recipe, fixture.recipe)
        flavor = next(
            value
            for value in snapshot.authority.selected_flavors
            if value.definition.coordinate.name == "lang-python"
        )
        load_flavor_contributions(
            fixture.flavors / "lang-python",
            flavor.definition,
            boundary=fixture.fixture.root / "project",
        )
        for contribution in document["contributions"]:
            if contribution["contribution_id"] == "fixture-native-sdk":
                contribution["slot"] = "native-sdk:wrong-dependency"
        path.write_bytes(render_authoring_markdown(document, body))
        snapshot = fixture.snapshot()
        flavor = next(
            value
            for value in snapshot.authority.selected_flavors
            if value.definition.coordinate.name == "lang-python"
        )
        with self.assertRaises(GenerationPreparationError):
            load_flavor_contributions(
                fixture.flavors / "lang-python",
                flavor.definition,
                boundary=fixture.fixture.root / "project",
            )
