"""Authored Flavor recipes drive original-source native builds."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.generation_preparation import (
    GenerationPreparationError,
    load_flavor_contributions,
)
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.native_sdk_custody import materialize_native_sdk
from literate_ai.adapters.native_sdk_recipes import (
    NativeSdkRecipePlanner,
    select_native_sdk_recipes,
)
from literate_ai.adapters.source.repository_git import (
    GitRepositorySourceAcquirer,
    GitRepositorySourceCapturer,
)
from literate_ai.application.repository_sources import (
    RepositorySourceIndexBinding,
    RepositorySourceResolver,
)
from literate_ai.contracts import RepositoryRevisionKind, RepositoryRevisionSelector
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    HashAlgorithm,
    canonical_identity,
)
from literate_ai.contracts.native_sdks import (
    NativeSdkBuildRecipe,
    NativeSdkSnapshot,
)
from tests.support import fixtures_test_native_sdk_build as test_native_sdk_build
from tests.support.fixtures_test_cli_component_locks import _run
from tests.support.fixtures_test_component_lock_planning import _fixture
from tests.support.fixtures_test_repository_sources import dependency
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


class NativeSdkRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = test_native_sdk_build.NativeSdkBuildTests(
            "test_real_repository_pipeline_retains_native_sdk_for_relocation"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.component, self.flavors = _fixture(self.fixture.root / "project")
        host_os = self.fixture.layout.operating_system
        if host_os != "macos":
            shutil.copytree(
                Path(__file__).resolve().parents[2] / "flavors" / f"os-{host_os}",
                self.flavors / f"os-{host_os}",
            )
        self.cli = [
            "lock",
            str(self.component),
            "--target",
            "host",
            "--flavor",
            "+python",
            "--flavor",
            f"+{host_os}",
            "--flavor-root",
            str(self.flavors),
        ]
        contract = (
            b"The vendor.math public scale(value, factor) returns their product.\n"
        )
        (self.component / "integration.md").write_bytes(contract)
        import hashlib

        interface = ContentIdentity(
            HashAlgorithm.SHA256, hashlib.sha256(contract).hexdigest()
        )
        surface = self.fixture.layout.import_surface
        surface = dataclasses.replace(
            surface,
            capabilities=tuple(
                dataclasses.replace(item, interface_identity=interface)
                for item in surface.capabilities
            ),
        )
        self.recipe = NativeSdkBuildRecipe(
            dependency().dependency_id,
            dataclasses.replace(self.fixture.layout, import_surface=surface),
            tuple(self.fixture.tools),
            self.fixture.plan.commands,
        )
        self.component_document = self.component / "component.md"
        document, body = parse_authoring_markdown(
            self.component_document.read_bytes(), source=str(self.component_document)
        )
        selected = dataclasses.replace(
            dependency(),
            revision_selector=RepositoryRevisionSelector(
                RepositoryRevisionKind.COMMIT, self.fixture.checkout.resolved_commit
            ),
        ).to_dict()
        selected.pop("schema")
        selected["integration_contract"] = {
            "kind": "integration-contract",
            "uri": "integration.md",
            "pin": None,
        }
        document["source_dependencies"] = [selected]
        if getattr(self, "entrypoint_kind", None) is not None:
            document["entrypoints"] = [
                {"name": "app", "kind": self.entrypoint_kind, "path": "app"}
            ]
        self.component_document.write_bytes(render_authoring_markdown(document, body))
        self.write_recipe(self.recipe)

        def local_transport(command, **kwargs):
            command = tuple(
                self.fixture.source.as_uri()
                if item == dependency().repository_url
                else item
                for item in command
            )
            return run_bounded_process(command, **kwargs)

        transport = patch(
            "literate_ai.adapters.source.repository_git.run_bounded_process",
            side_effect=local_transport,
        )
        transport.start()
        self.addCleanup(transport.stop)

    def write_recipe(self, recipe, *, flavor="lang-python", slot=None) -> None:
        root = self.flavors / flavor
        (root / "sdk-recipe.json").write_text(
            json.dumps(recipe.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        path = root / "flavor.md"
        document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
        document["contributions"] = [
            item
            for item in document["contributions"]
            if item["content"]["kind"] != "native-sdk-build-recipe"
        ] + [
            {
                "contribution_id": "fixture-native-sdk",
                "kind": "builder",
                "merge_operator": "exact-singleton",
                "slot": slot or "native-sdk:" + recipe.dependency_id,
                "content": {
                    "kind": "native-sdk-build-recipe",
                    "uri": "sdk-recipe.json",
                },
            }
        ]
        path.write_bytes(render_authoring_markdown(document, body))

    def snapshot(self):
        status, result, _ = _run(self.cli)
        self.assertEqual(status, 0, result)
        return FilesystemLockedGenerationAuthorityReader().read(
            self.component,
            target_name="host",
            flavor_selectors=("+python", "+" + self.recipe.layout.operating_system),
            flavor_roots=(self.flavors,),
        )

    def test_recipe_and_layout_schema_reject_extra_fields_and_unbound_tools(self):
        schemas = SchemaCatalog()
        for value in (self.recipe, self.recipe.layout):
            document = value.to_dict()
            schemas.validate(value.SCHEMA, document)
            self.assertEqual(type(value).from_dict(document), value)
            document["execution_authorized"] = True
            with self.assertRaises(ValueError):
                type(value).from_dict(document)
            with self.assertRaises(AssertionError):
                schemas.validate(value.SCHEMA, document)
        with self.assertRaisesRegex(ValueError, "named tool"):
            dataclasses.replace(self.recipe, tools=("unrelated",))
        with self.assertRaisesRegex(ValueError, "unique"):
            dataclasses.replace(self.recipe, tools=("cmake", "cmake"))

    def test_selected_flavor_recipe_drives_real_native_build_and_relocation(self):
        snapshot = self.snapshot()
        component = snapshot.authority.lock.nodes[0].revision.identity
        (selected,) = select_native_sdk_recipes(snapshot, component)
        fixture = self.fixture
        fixture.layout = selected.recipe.layout
        fixture.lock = selected.source_lock
        fixture.builder.layout = selected.recipe.layout
        planner = NativeSdkRecipePlanner(
            snapshot=snapshot,
            selection=selected,
            tools=fixture.tools,
            store=fixture.store,
        )

        class TestAuthorities:
            def index(self, _checkout, capture):
                return RepositorySourceIndexBinding(
                    capture.snapshot.identity,
                    capture.snapshot.tree_identity,
                    canonical_identity("explicit fixture indexer"),
                    canonical_identity("explicit fixture index"),
                )

            def authorize(self, _lock, _index, plan):
                return fixture.approve(plan)

            def admit(self, _checkout, _capture, admission):
                return ContentIdentity.parse_uri(
                    fixture.store.put_manifest(admission.to_dict()).identity
                )

        authorities = TestAuthorities()
        resolution = RepositorySourceResolver(
            acquirer=GitRepositorySourceAcquirer(),
            capturer=GitRepositorySourceCapturer(),
            indexer=authorities,
            planner=planner,
            authorizer=authorities,
            builder=fixture.builder,
            cache=authorities,
        ).resolve(
            selected.source_lock.dependency,
            expected_lock=selected.source_lock,
            effective_revision=selected.component_revision,
            flavor_set=selected.target_identity,
            toolchains=tuple(
                tool.toolchain_identity for tool in fixture.tools.values()
            ),
        )
        self.assertIn(self.recipe.identity, resolution.build_plan.evidence)
        self.assertEqual(resolution.build_plan.commands, self.recipe.commands)
        sdk = NativeSdkSnapshot.from_dict(
            fixture.manifest(resolution.admission.build_outputs[0].identity)
        )
        self.assertEqual(sdk.import_surface, self.recipe.layout.import_surface)
        fixture.remove_source()
        parent = fixture.root / "consumer"
        parent.mkdir()
        relocated = materialize_native_sdk(
            sdk, expected_identity=sdk.identity, store=fixture.store, parent=parent
        )
        result = subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys;sys.path.insert(0,sys.argv[1]);import vendor_math;"
                "print(vendor_math.scale(1.5,3))",
                str(relocated / sdk.import_root),
            ),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4.5")

    def test_public_contract_mismatch_and_conflicting_target_refuse_selection(self):
        surface = self.recipe.layout.import_surface
        wrong_surface = dataclasses.replace(
            surface,
            capabilities=tuple(
                dataclasses.replace(
                    item, interface_identity=canonical_identity("other contract")
                )
                for item in surface.capabilities
            ),
        )
        for layout, message in (
            (
                dataclasses.replace(self.recipe.layout, import_surface=wrong_surface),
                "integration contract",
            ),
            (
                dataclasses.replace(
                    self.recipe.layout,
                    operating_system="linux"
                    if self.recipe.layout.operating_system != "linux"
                    else "macos",
                ),
                "target differs",
            ),
        ):
            with self.subTest(message=message):
                self.write_recipe(dataclasses.replace(self.recipe, layout=layout))
                snapshot = self.snapshot()
                with self.assertRaisesRegex(ValueError, message):
                    select_native_sdk_recipes(
                        snapshot, snapshot.authority.lock.nodes[0].revision.identity
                    )

    def test_changed_recipe_content_is_rejected_by_locked_input_custody(self):
        snapshot = self.snapshot()
        path = self.flavors / "lang-python/sdk-recipe.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(LockedGenerationAuthorityReaderError):
            select_native_sdk_recipes(
                snapshot, snapshot.authority.lock.nodes[0].revision.identity
            )

    def test_project_validation_rejects_invalid_recipe_slots(self):
        snapshot = self.snapshot()
        flavor = next(
            item
            for item in snapshot.authority.selected_flavors
            if item.definition.coordinate.name == "lang-python"
        )
        load_flavor_contributions(
            self.flavors / "lang-python",
            flavor.definition,
            boundary=self.fixture.root / "project",
        )
        self.write_recipe(self.recipe, slot="native-sdk:wrong-dependency")
        snapshot = self.snapshot()
        flavor = next(
            item
            for item in snapshot.authority.selected_flavors
            if item.definition.coordinate.name == "lang-python"
        )
        with self.assertRaises(GenerationPreparationError) as raised:
            load_flavor_contributions(
                self.flavors / "lang-python",
                flavor.definition,
                boundary=self.fixture.root / "project",
            )
        self.assertEqual(
            raised.exception.code, "generate.invalid_native_sdk_build_recipe"
        )

    def test_replayed_source_target_and_tool_inputs_refuse_planning(self):
        snapshot = self.snapshot()
        component = snapshot.authority.lock.nodes[0].revision.identity
        (selected,) = select_native_sdk_recipes(snapshot, component)
        planner = NativeSdkRecipePlanner(
            snapshot=snapshot,
            selection=selected,
            tools=self.fixture.tools,
            store=self.fixture.store,
        )
        arguments = dict(
            lock=selected.source_lock,
            index_binding=canonical_identity("fixture index"),
            effective_revision=selected.component_revision,
            flavor_set=selected.target_identity,
            toolchains=tuple(
                tool.toolchain_identity for tool in self.fixture.tools.values()
            ),
        )
        for change in (
            {
                "lock": dataclasses.replace(
                    selected.source_lock,
                    source_tree=canonical_identity("changed source"),
                )
            },
            {"effective_revision": canonical_identity("another consumer")},
            {"flavor_set": canonical_identity("another target")},
            {"toolchains": (canonical_identity("another tool"),)},
        ):
            with self.subTest(change=tuple(change)):
                with self.assertRaises(ValueError):
                    planner.plan(**{**arguments, **change})
