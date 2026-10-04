"""Authored Flavor recipes drive original-source native builds."""

from __future__ import annotations

import dataclasses
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.native_sdk_recipes import (
    select_native_sdk_recipes,
)
from literate_ai.contracts import RepositoryRevisionKind, RepositoryRevisionSelector
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    HashAlgorithm,
)
from literate_ai.contracts.native_sdks import (
    NativeSdkBuildRecipe,
)
from tests.support import fixtures_test_native_sdk_build as test_native_sdk_build
from tests.support.fixtures_test_cli_component_locks import _run
from tests.support.fixtures_test_component_lock_planning import _fixture
from tests.support.fixtures_test_repository_sources import dependency


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

    def test_changed_recipe_content_is_rejected_by_locked_input_custody(self):
        snapshot = self.snapshot()
        path = self.flavors / "lang-python/sdk-recipe.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(LockedGenerationAuthorityReaderError):
            select_native_sdk_recipes(
                snapshot, snapshot.authority.lock.nodes[0].revision.identity
            )
