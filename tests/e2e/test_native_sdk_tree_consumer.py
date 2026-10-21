"""Live source-service tree consumers retain aliases, evidence and cleanup."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_dependencies import project_native_sdk_dependencies
from literate_ai.adapters.native_sdk_package import NativeSdkPackageResources
from literate_ai.adapters.native_sdk_tree_package import NativeSdkTreePackageResources
from literate_ai.adapters.retained_project_inputs import (
    capture_retained_project_inputs,
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_sdks import RetainedProjectNativeSdkInputs
from literate_ai.adapters.tree_packaging import (
    TreeTarPackageAdapter,
    verify_tree_package_stage,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.executable_components.packages import (
    PackageEntrypoint,
    PackageFileKind,
    PackageInput,
    PackageKind,
    PackagePlan,
)
from literate_ai.contracts.native_sdk_tree_products import (
    NATIVE_SDK_TREE_BUILD_RECIPE_CONTENT_KIND,
    NativeSdkTreeBuildLayout,
    NativeSdkTreeBuildRecipe,
)
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectTreePolicy,
)
from literate_ai.contracts.retained_project_sdks import (
    RetainedProjectSdkBinding,
    RetainedProjectSdkPolicy,
)
from literate_ai.contracts.sbom import (
    CycloneDxLifecycle,
    project_component_lock_managed_graph,
)
from literate_ai.storage.cas import FileSystemCAS
from tests.support import fixtures_test_native_sdk_source_build as source_fixture
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture


def _configure_tree_recipe(fixture):
    old = fixture.recipe
    original = old.layout.native_libraries[-1]
    layout = NativeSdkTreeBuildLayout(
        **{
            field.name: getattr(old.layout, field.name)
            for field in dataclasses.fields(old.layout)
            if field.name != "native_libraries"
        },
        native_libraries=tuple(
            sorted(
                (
                    old.layout.native_libraries[0],
                    original + ".hard",
                    original + ".symbolic",
                )
            )
        ),
        limits=RetainedProjectLimits(1000, 64 * 1024 * 1024, 128 * 1024 * 1024),
    )
    fixture.recipe = NativeSdkTreeBuildRecipe(
        old.dependency_id,
        layout,
        old.tools,
        (
            *old.commands,
            RepositoryBuildCommand(
                "hardlink",
                (
                    "cmake",
                    "-E",
                    "create_hardlink",
                    layout.sdk_root + "/" + original,
                    layout.sdk_root + "/" + original + ".hard",
                ),
            ),
            RepositoryBuildCommand(
                "symlink",
                (
                    "cmake",
                    "-E",
                    "create_symlink",
                    original.rsplit("/", 1)[-1],
                    layout.sdk_root + "/" + original + ".symbolic",
                ),
            ),
        ),
    )
    fixture.write_recipe(fixture.recipe)
    path = fixture.flavors / "lang-python/flavor.md"
    document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
    for contribution in document["contributions"]:
        if contribution["contribution_id"] == "fixture-native-sdk":
            contribution["content"]["kind"] = NATIVE_SDK_TREE_BUILD_RECIPE_CONTENT_KIND
    path.write_bytes(render_authoring_markdown(document, body))


@unittest.skipIf(os.name == "nt", "Symbolic SDK source fixture requires POSIX")
class NativeSdkTreeConsumerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixture.NativeSdkSourceBuildTests(
            "test_real_policy_build_cache_and_relocated_native_execution"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.configure_recipe_fixture = _configure_tree_recipe
        self.fixture.setUp()
        self.service = self.fixture.service()
        self.built = self.service.build()
        self.owner = NativeSdkConsumerInputs(
            snapshot=self.fixture.snapshot, services=(self.service,)
        )
        self.parent = self.fixture.fixture.root / "consumer"
        self.parent.mkdir()

    def materialize(self):
        return self.owner.materialize(
            self.built.selection.component_revision,
            target_identity=self.built.selection.target_identity,
            parent=self.parent,
        )

    def test_live_consumer_survives_source_retirement_and_projects_alias_evidence(self):
        self.fixture.fixture.remove_source()
        with self.materialize() as values:
            self.assertEqual(len(values), 1)
            value = values[0]
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    (
                        "import sys;sys.path.insert(0,sys.argv[1]);"
                        "import vendor_math;print(vendor_math.scale(1.5,3))"
                    ),
                    str(value.import_root),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "4.5")
            package = next(
                component
                for component in value.dependencies.observation.components
                if any(
                    p["name"] == "literate-ai:native-sdk-tree"
                    for p in component.get("properties", [])
                )
            )
            aliases = [
                json.loads(p["value"])
                for p in package["properties"]
                if p["name"] == "literate-ai:native-sdk-alias"
            ]
            self.assertEqual(
                {alias["kind"] for alias in aliases}, {"hardlink", "symlink"}
            )
            self.assertEqual(
                {alias["path"] for alias in aliases},
                {alias.path for alias in self.built.product.snapshot.tree.aliases},
            )
            managed = project_component_lock_managed_graph(
                self.fixture.snapshot.authority.lock,
                self.built.selection.component_revision,
            )
            observation = value.dependencies.observation
            source, _ = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=managed,
                additional_components=observation.components,
                additional_edges=observation.edges,
            )
            resolved, _ = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.RESOLVED,
                managed_graph=managed,
                additional_components=observation.components,
                additional_edges=observation.edges,
                source_bom=source,
                source_managed_graph=managed,
                repository_resolutions=(value.dependencies.repository_resolution,),
            )
            for document in (source, resolved):
                retained = [
                    json.loads(p["value"])
                    for c in json.loads(document)["components"]
                    for p in c.get("properties", [])
                    if p["name"] == "literate-ai:native-sdk-alias"
                ]
                self.assertEqual(retained, aliases)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_retained_consumer_preserves_product_topology_after_source_retirement(self):
        source = self.parent / "retained-source"
        source.mkdir()
        sdk = self.built.product.snapshot
        vendor = source / "vendor"
        vendor.mkdir()
        for name in sdk.tree.directories:
            (vendor / name).mkdir()
        (binding,) = self.owner.for_scope(
            self.fixture.snapshot.authority.lock.root_revision
        )
        for item in sdk.files:
            path = vendor / item.path
            path.write_bytes(self.owner.read_input_blob(binding, item.blob))
            path.chmod(0o755 if item.executable else 0o644)
        for alias in sdk.tree.aliases:
            if alias.kind == "hardlink":
                os.link(vendor / alias.target, vendor / alias.path)
        for alias in sdk.tree.aliases:
            if alias.kind == "symlink":
                (vendor / alias.path).symlink_to(alias.target)
        lock = self.fixture.snapshot.authority.lock
        policy = RetainedProjectSdkPolicy(
            (RetainedProjectSdkBinding(lock.identity, binding.identity, "vendor"),)
        )
        (source / "sdk-policy.json").write_bytes(canonical_json_bytes(policy.to_dict()))
        manifest = discover_retained_project_inputs(
            source,
            {"vendor": "sdk", "sdk-policy.json": "sdk-policy"},
            RetainedProjectLimits(100, 1048576, 4194304),
            allow_internal_symlinks=True,
            tree_policy=RetainedProjectTreePolicy(sdk.tree.case_sensitive),
        )
        captured = capture_retained_project_inputs(
            source, manifest, self.parent / "retained-capture"
        )
        shutil.rmtree(source)
        self.fixture.fixture.remove_source()
        plan, _ = retained_project_sbom_fixture()
        plan = dataclasses.replace(
            plan, manifest_identity=manifest.identity, component_locks=(lock.identity,)
        )
        execution_parent = self.parent
        retained = RetainedProjectNativeSdkInputs(
            captured, manifest, plan, (lock,), (self.owner,), parent=execution_parent
        )
        retained.require_unchanged()
        (execution,) = retained.executions
        (value,) = execution.values
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                (
                    "import sys;sys.path.insert(0,sys.argv[1]);"
                    "import vendor_math;print(vendor_math.scale(1.5,3))"
                ),
                str(value.import_root),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4.5")
        alias = next(a for a in sdk.tree.aliases if a.kind == "hardlink")
        path = captured / "vendor" / alias.path
        content = path.read_bytes()
        mode = path.stat().st_mode
        path.unlink()
        path.write_bytes(content)
        path.chmod(mode)
        with self.assertRaisesRegex(ValueError, "topology"):
            retained.require_unchanged()

    def test_forged_runtime_schema_or_physical_binding_refuses(self):
        with self.materialize() as values:
            value = values[0]
            runtime = self.service.store.get_manifest(value.runtime_observation)
            wrong = json.loads(json.dumps(runtime))
            wrong["schema"] = "literate-ai/native-sdk-runtime-observation@1"
            with self.assertRaises(ValueError):
                project_native_sdk_dependencies(
                    value.binding, root=value.root, runtime=wrong
                )
            wrong = json.loads(json.dumps(runtime))
            wrong["native_libraries"][0]["physical_path"] = "LICENSE"
            with self.assertRaisesRegex(ValueError, "tree target"):
                project_native_sdk_dependencies(
                    value.binding, root=value.root, runtime=wrong
                )
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_same_bytes_alias_drift_refuses_and_preserves_unrelated_sibling(self):
        sentinel = self.parent / "sentinel"
        sentinel.write_bytes(b"unrelated")
        with self.assertRaises(ValueError):
            with self.materialize() as values:
                value = values[0]
                alias = next(
                    alias
                    for alias in self.built.product.snapshot.tree.aliases
                    if alias.kind == "hardlink"
                )
                path = value.root / alias.path
                data = path.read_bytes()
                path.unlink()
                path.write_bytes(data)
        self.assertEqual(list(self.parent.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"unrelated")

    def test_explicit_tree_tar_retains_real_sdk_after_producer_retirement(self):
        lock = self.fixture.snapshot.authority.lock
        revision = self.built.selection.component_revision
        target = self.built.selection.target_identity
        resources = NativeSdkTreePackageResources(
            self.owner,
            component_lock_identity=lock.identity,
            root_revision=revision,
            target_identity=target,
        )
        store = FileSystemCAS(self.parent / "package-store")
        work = self.parent / "package-work"
        work.mkdir()
        adapter = TreeTarPackageAdapter()
        program = (
            b"import sys;sys.path.insert(0,sys.argv[1]);"
            b"import vendor_math;print(vendor_math.scale(1.5,3))\n"
        )
        blob = store.put_bytes(program)
        artifact = canonical_identity({"fixture consumer": blob.to_dict()})
        # Structural consumer fixture; Standard lifecycle admission remains separate.
        base = PackagePlan(
            revision,
            lock.identity,
            target,
            canonical_identity("fixture graph"),
            canonical_identity("fixture link"),
            artifact,
            PackageKind.ARCHIVE,
            adapter.identity,
            (
                PackageInput(
                    "consumer.py",
                    "application",
                    PackageFileKind.ARTIFACT,
                    artifact,
                    target,
                    blob,
                ),
            ),
            (PackageEntrypoint("consumer", "application", "consumer.py", artifact),),
            (),
        )
        plan, result = resources.package(
            base,
            adapter=adapter,
            store=store,
            object_root=work,
            limits=RetainedProjectLimits(200, 1048576, 4194304),
        )
        resources.require_current()
        with self.assertRaisesRegex(ValueError, "locked scope"):
            resources.make_plan(
                dataclasses.replace(
                    base, component_lock_identity=canonical_identity("foreign lock")
                ),
                limits=plan.limits,
            )
        original = resources.trees
        resources.trees = (
            dataclasses.replace(
                original[0], source_identity=canonical_identity("forged")
            ),
        )
        with self.assertRaisesRegex(ValueError, "projection changed"):
            resources.require_current()
        resources.trees = original
        real_package = adapter.package

        def lose_owner(*args, **kwargs):
            packaged = real_package(*args, **kwargs)
            self.service.builder._products.clear()
            return packaged

        with mock.patch.object(adapter, "package", side_effect=lose_owner):
            with self.assertRaisesRegex(ValueError, "not verified"):
                resources.package(
                    base,
                    adapter=adapter,
                    store=store,
                    object_root=work,
                    limits=plan.limits,
                )
        self.fixture.fixture.remove_source()
        shutil.rmtree(self.service.store.root)
        archive = self.parent / "package.tar"
        store.copy_to(result.archive, archive)
        root = self.parent / "unpacked"
        root.mkdir()
        extracted = subprocess.run(
            [shutil.which("tar"), "-xpf", str(archive), "-C", str(root)],
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(extracted.returncode, 0, extracted.stderr)
        relocated = self.parent / "relocated tree package"
        root.rename(relocated)
        adapter.verify(plan, result, store=store, object_root=work)
        verify_tree_package_stage(plan, relocated)
        index = json.loads((relocated / "native-sdks/tree-inputs.json").read_bytes())
        imported = relocated / index["consumers"][0]["import_root"]
        executed = subprocess.run(
            [sys.executable, "-I", "-B", str(relocated / "consumer.py"), str(imported)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertEqual(executed.stdout.strip(), "4.5")
        alias = next(v for v in plan.trees[0].tree.aliases if v.kind == "hardlink")
        path = relocated / plan.trees[0].prefix / alias.path
        data, mode = path.read_bytes(), path.stat().st_mode
        path.unlink()
        path.write_bytes(data)
        path.chmod(mode)
        with self.assertRaisesRegex(ValueError, "topology"):
            verify_tree_package_stage(plan, relocated)

    def test_packaging_refuses_until_link_aware_inputs_are_available(self):
        with self.assertRaisesRegex(ValueError, "link-aware"):
            NativeSdkPackageResources(
                self.owner,
                component_lock_identity=self.fixture.snapshot.authority.lock.identity,
                root_revision=self.fixture.snapshot.authority.lock.root_revision,
                target_identity=self.built.selection.target_identity,
            )
