from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.dependencies.python_install import PIP_INSTALLER
from literate_ai.adapters.lifecycle import LocalStandardLifecycleError
from literate_ai.adapters.lifecycle.standard_local import local_tree_identity
from literate_ai.adapters.standard_project import (
    assemble_filesystem_standard_project_runtime,
    project_locked_standard_toolchain_closure,
)
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    realize_manifest,
)
from literate_ai.contracts import (
    BuildPrivilege,
    BuildSubActionKind,
    ComponentCommandPhase,
)
from tests.support import fixtures_test_python_install as wheel_fixtures
from tests.support import (
    fixtures_test_standard_command_projection as projection_fixtures,
)
from tests.support.standard_source_evidence_fixture import register_strict_source


class StandardPythonLifecycleTests(unittest.TestCase):
    library = False

    @classmethod
    def setUpClass(cls):
        wheel_fixtures.PythonInstallerIntegrationTests.setUpClass()

    def setUp(self):
        self.wheels = wheel_fixtures.PythonInstallerIntegrationTests()
        self.wheels.setUp()
        self.addCleanup(self.wheels.doCleanups)
        self.root = self.wheels.fixture.root
        self.wheelhouse = self.root / "wheels"
        self.wheelhouse.mkdir()
        for package in self.wheels.fixture.value["packages"]:
            (self.wheelhouse / package["filename"]).write_bytes(
                self.wheels.fixture.sources[package["name"]]
            )
        shutil.copyfile(
            self.wheels.installer_path, self.wheelhouse / PIP_INSTALLER.filename
        )
        self.component, self.snapshot, self.execution = (
            projection_fixtures._locked_snapshot(
                self.root,
                package_python=True,
                no_entrypoint=self.library,
                generation_ready=True,
                platform=(
                    "windows"
                    if os.name == "nt"
                    else "macos"
                    if sys.platform == "darwin"
                    else "linux"
                ),
            )
        )
        self.generation_plan = self.execution.generation_plans[0]

        def observe(commands):
            observed = projection_fixtures._observation(commands)
            return replace(
                observed,
                components=tuple(
                    {
                        **item,
                        "version": self.wheels.toolchain.version,
                        "properties": [
                            {
                                "name": "literate-ai:dependency-kind",
                                "value": "toolchain",
                            },
                            {"name": "literate-ai:dependency-scope", "value": "build"},
                            {
                                "name": "literate-ai:dependency-scope",
                                "value": "runtime",
                            },
                        ],
                    }
                    for item in observed.components
                ),
            )

        self.closure = project_locked_standard_toolchain_closure(
            self.snapshot,
            self.execution,
            host_platform=(
                "windows"
                if os.name == "nt"
                else "macos"
                if sys.platform == "darwin"
                else "linux"
            ),
            toolchain_discoverer=lambda *_: self.wheels.toolchain,
            dependency_observer=observe,
        )
        self.runtime = self.runtime_instance()
        self.ports = self.runtime.lifecycle_ports
        self.source = self.root / "generated"
        (self.source / "source").mkdir(parents=True)
        (self.source / "source/requirements.txt").write_text(
            "example==1.0\n", encoding="utf-8"
        )
        (self.source / "source/python-wheel-lock.json").write_text(
            json.dumps(self.wheels.fixture.value), encoding="utf-8"
        )
        (self.source / "source/main.py").write_text(
            "import example,helper,json,sys\n"
            "assert example.VALUE == helper.VALUE == 1\n"
            "if '--litai-test' in sys.argv:\n"
            " print(json.dumps(dict(schema='literate-ai/generated-test-results@1', "
            "cases=[dict(case_id='fixture-'+name,outcome='passed') "
            "for name in ('example','boundary','invariant')])))\n"
            "else:\n print(json.dumps(dict(component='verified-wheels')))\n",
            encoding="utf-8",
        )
        if self.library:
            surface = self.closure.contracts[0].library_import_surface
            capability = surface.capabilities[0]
            module = self.source / "source" / Path(*capability.module.split("."))
            module.parent.mkdir(parents=True, exist_ok=True)
            for parent in module.parents:
                if parent == self.source / "source":
                    break
                (parent / "__init__.py").touch()
            module.with_suffix(".py").write_text(
                "import example,helper\n"
                f"def {capability.symbols[0]}(left,right):\n"
                " return left+right+example.VALUE+helper.VALUE\n",
                encoding="utf-8",
            )
            main = self.source / "source/main.py"
            main.write_text(
                f"from {capability.module} import {capability.symbols[0]}\n"
                f"assert {capability.symbols[0]}(2,3)==7\n"
                + main.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        self.components = tuple(
            {
                "type": "library",
                "bom-ref": "source-" + name,
                "name": name,
                "purl": "pkg:pypi/" + name,
                "version": "1.0",
                "properties": [
                    {"name": "literate-ai:dependency-kind", "value": "package"},
                    {"name": "literate-ai:dependency-scope", "value": "runtime"},
                    {"name": "literate-ai:python-top-level-import", "value": name},
                ],
            }
            for name in ("example", "helper")
        )
        self._admit_source()

    def _admit_source(self):
        self.candidate = register_strict_source(
            self.ports.source_trees,
            self.source,
            snapshot=self.snapshot,
            generation_plan=self.generation_plan,
            identity_namespace="python-wheel-lifecycle",
            additional_components=self.components,
            root_dependency_refs=("source-example",),
            additional_edges=(("source-example", "source-helper"),),
        )
        self.intent = self.ports.create(
            self.execution, self.generation_plan, self.candidate, (), ()
        )
        authorization = self.ports.authorize(
            self.intent,
            self.ports.index(
                self.candidate.component_revision, self.candidate.tree_identity
            ),
        )
        self.plan = self.ports.finalize(self.intent, authorization)

    def runtime_instance(self, registry=None):
        return assemble_filesystem_standard_project_runtime(
            generator=mock.Mock(),
            object_root=self.root / "objects",
            toolchain_closure=self.closure,
            python_wheelhouse=self.wheelhouse,
            source_trees=registry,
        )

    def build(self):
        output = self.ports.build(self.plan, ())
        self.artifact = self.ports.artifact_path(output.exports[0]).parent
        return output

    def package(self, output):
        manifest = realize_manifest(self.plan.manifest, output.exports)
        graph = create_artifact_build_graph(
            build_system_driver_identity=manifest.build_system_driver_identity,
            manifests=(manifest,),
            link_roots=(output.exports[0].identity,),
        )
        package_plan, package_result = self.ports.create_project_package(
            self.snapshot.authority.lock,
            self.execution,
            SimpleNamespace(),
            graph,
            graph.link_plans[0],
        )
        return (
            package_plan,
            package_result,
            self.ports.project_package_custody(package_plan, package_result),
        )

    def test_packaged_runtime_is_complete_and_survives_relocation(self):
        output = self.build()
        original_identity = local_tree_identity(self.artifact)
        package_plan, package_result, custody = self.package(output)
        packaged_artifact = custody.artifact_paths[
            output.exports[0].identity.uri
        ].parent
        self.assertEqual(local_tree_identity(packaged_artifact), original_identity)
        paths = {item.path for item in package_plan.inputs}
        self.assertIn("root/python-dependencies.json", paths)
        self.assertIn("root/.literate/resolved-sbom.cdx.json", paths)
        self.assertTrue(
            any(path.startswith("root/python-runtime/site/") for path in paths)
        )

        old_root = custody.root
        relocated = old_root.with_name("relocated Python package")
        old_root.rename(relocated)
        custody = replace(
            custody,
            root=relocated,
            artifact_paths={
                key: relocated / path.relative_to(old_root)
                for key, path in custody.artifact_paths.items()
            },
        )
        self.ports._project_packages[package_result.identity.uri] = custody
        shutil.rmtree(self.artifact)
        shutil.rmtree(self.wheelhouse)
        shutil.rmtree(self.source)
        arguments = (
            self.snapshot.authority.lock,
            self.execution,
            SimpleNamespace(),
            package_plan,
            package_result,
        )
        self.assertIsNotNone(self.ports.test_root_integration(*arguments))
        self.assertIsNotNone(self.ports.execute_packaged_project(*arguments))

    def test_authorized_offline_build_test_launch_and_cache_reuse(self):
        self.assertEqual(
            self.plan.request.requested_privileges,
            (BuildPrivilege.EXECUTE_BUILD_TOOLS,),
        )
        self.assertEqual(
            tuple(item.kind for item in self.plan.request.sub_actions),
            (BuildSubActionKind.RESOLVE_DEPENDENCIES, BuildSubActionKind.COMPILE),
        )
        output = self.build()
        self.assertEqual(self.ports.build_cache_misses, 1)
        tested = self.ports.test(self.plan, output.exports)
        self.assertIsNotNone(tested)
        self.assertIsNotNone(self.ports.execute(self.plan, output.exports))
        command = (
            self.closure.contracts[0].command(ComponentCommandPhase.EXECUTE)
            if self.library
            else self.ports.execution_command(self.plan, output.exports)
        )
        self.assertIn("-I", command.argv)
        reused = self.ports.build(self.plan, ())
        self.assertEqual(reused.exports, output.exports)
        self.assertEqual(self.ports.build_cache_hits, 1)
        self.assertEqual(list((self.root / "objects").glob("standard-python-*")), [])

    def test_changed_installed_payload_rejected_before_execution_and_cache_reuse(self):
        output = self.build()
        (self.artifact / "python-runtime/site/example/__init__.py").write_text(
            "VALUE = 99\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(LocalStandardLifecycleError, "sealed artifact"):
            if self.library:
                self.ports.execute(self.plan, output.exports)
            else:
                self.ports.execution_command(self.plan, output.exports)
        with self.assertRaises(LocalStandardLifecycleError):
            self.ports.build(self.plan, ())


class StandardPythonLibraryLifecycleTests(StandardPythonLifecycleTests):
    library = True

    def oracle(self):
        from literate_ai.adapters.component_acceptance import (
            DeclaredLibraryAcceptanceCase,
            LibraryAcceptance,
        )
        from literate_ai.contracts import ContentIdentity

        lock = self.snapshot.authority.lock
        node = next(
            item for item in lock.nodes if item.revision.identity == lock.root_revision
        )
        surface = self.closure.contracts[0].library_import_surface
        harness = (
            b"import importlib,json,pathlib,sys\n"
            b"export,surface,cases=sys.argv[1],json.loads(sys.argv[2]),json.loads(sys.argv[3])\n"
            b"assert sys.flags.isolated and sys.flags.no_site\n"
            b"sys.path.insert(0,str(pathlib.Path(export)/'source'))\n"
            b"capability=surface['capabilities'][0]\n"
            b"module=importlib.import_module(capability['module'])\n"
            b"origin=pathlib.Path(module.__file__).resolve()\n"
            b"assert pathlib.Path(export).resolve() in origin.parents\n"
            b"function=getattr(module,capability['symbols'][0])\n"
            b"print(json.dumps(dict(schema='literate-ai/library-acceptance-results@1',cases=[\n"
            b" dict(case_id=case['case_id'],capability=case['capability'],\n"
            b" result=function(*case['arguments']))\n"
            b" for case in cases])))\n"
        )
        return LibraryAcceptance(
            node.revision.coordinate.name,
            node.revision.specification_set_identity,
            tuple(
                sorted(
                    (item.identity for item in node.revision.public_interfaces),
                    key=lambda item: item.uri,
                )
            ),
            surface.identity,
            "python",
            ContentIdentity.parse_uri("sha256:" + hashlib.sha256(harness).hexdigest()),
            harness,
            (
                DeclaredLibraryAcceptanceCase(
                    "wheel-library-add", surface.capabilities[0].capability, [2, 3], 7
                ),
            ),
        )

    def accept(self, plan, result):
        from literate_ai.contracts import canonical_identity

        self.ports.independent_acceptance_oracle = self.oracle()
        return self.ports.accept_project_independently(
            self.snapshot.authority.lock,
            self.execution,
            SimpleNamespace(),
            plan,
            result,
            canonical_identity({"root-test": True}),
            canonical_identity({"packaged-execution": True}),
        )

    def test_independent_acceptance_after_source_and_wheelhouse_retirement(self):
        output = self.build()
        self.ports.test(self.plan, output.exports)
        package_plan, package_result, custody = self.package(output)
        old_root = custody.root
        relocated = old_root.with_name("relocated Python library")
        old_root.rename(relocated)
        custody = replace(
            custody,
            root=relocated,
            artifact_paths={
                key: relocated / path.relative_to(old_root)
                for key, path in custody.artifact_paths.items()
            },
        )
        self.ports._project_packages[package_result.identity.uri] = custody
        shutil.rmtree(self.artifact)
        shutil.rmtree(self.source)
        shutil.rmtree(self.wheelhouse)
        # An injected ambient package/startup hook must not satisfy dependencies
        # or execute inside any of the three isolated library phases.
        ambient = self.root / "ambient"
        ambient.mkdir()
        (ambient / "sitecustomize.py").write_text(
            "raise RuntimeError('ambient startup')\n"
        )
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(ambient)}):
            self.assertIsNotNone(self.accept(package_plan, package_result))
        artifact = custody.artifact_paths[output.exports[0].identity.uri].parent
        paths = [
            artifact / "python-runtime/site/example/__init__.py",
            artifact / "python-dependencies.json",
            artifact / ".literate/resolved-sbom.cdx.json",
            custody.artifact_paths[output.exports[0].identity.uri]
            / "source/.literate/sbom.cdx.json",
            custody.artifact_paths[output.exports[0].identity.uri] / "source/main.py",
        ]
        for path in paths:
            with self.subTest(path=path.relative_to(artifact)):
                content = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaises(LocalStandardLifecycleError):
                        self.accept(package_plan, package_result)
                finally:
                    path.write_bytes(content)


class PinnedPipWheelSelectionTests(unittest.TestCase):
    """Repacked bundled pip wheels skip, except where the release gate requires them."""

    def test_bundled_wheel_must_match_the_pinned_digest(self):
        from tests.support import pip_wheel

        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "ensurepip"
            (package / "_bundled").mkdir(parents=True)
            wheel = package / "_bundled" / pip_wheel.PINNED_PIP_WHEEL
            wheel.write_bytes(b"repacked distributor wheel")
            environment = {
                key: value
                for key, value in os.environ.items()
                if key not in {"LITAI_TEST_PIP_WHEEL", "LITAI_REQUIRE_PINNED_PIP_WHEEL"}
            }
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(
                    pip_wheel.ensurepip, "__file__", str(package / "__init__.py")
                ),
            ):
                with self.assertRaisesRegex(unittest.SkipTest, "different bytes"):
                    pip_wheel.pinned_pip_wheel()
                pinned = replace(
                    PIP_INSTALLER,
                    sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                )
                with mock.patch.object(pip_wheel, "PIP_INSTALLER", pinned):
                    self.assertEqual(pip_wheel.pinned_pip_wheel(), wheel.resolve())
                # The release gate refuses to pass these tests by skipping.
                with (
                    mock.patch.dict(
                        os.environ, {"LITAI_REQUIRE_PINNED_PIP_WHEEL": "1"}
                    ),
                    self.assertRaisesRegex(AssertionError, "different bytes"),
                ):
                    pip_wheel.pinned_pip_wheel()
                # An explicit operator wheel must carry the pinned digest.
                with (
                    mock.patch.dict(os.environ, {"LITAI_TEST_PIP_WHEEL": str(wheel)}),
                    self.assertRaisesRegex(AssertionError, "is not the PyPI"),
                ):
                    pip_wheel.pinned_pip_wheel()
                wheel.unlink()
                with self.assertRaisesRegex(unittest.SkipTest, "does not bundle"):
                    pip_wheel.pinned_pip_wheel()
                # An interpreter without the pinned version still skips in the gate.
                with (
                    mock.patch.dict(
                        os.environ, {"LITAI_REQUIRE_PINNED_PIP_WHEEL": "1"}
                    ),
                    self.assertRaisesRegex(unittest.SkipTest, "does not bundle"),
                ):
                    pip_wheel.pinned_pip_wheel()
