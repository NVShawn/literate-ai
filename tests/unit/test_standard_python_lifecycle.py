from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from dataclasses import replace
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
from literate_ai.contracts import BuildPrivilege, BuildSubActionKind
from tests.support import fixtures_test_python_install as wheel_fixtures
from tests.support import (
    fixtures_test_standard_command_projection as projection_fixtures,
)
from tests.unit.standard_source_evidence_fixture import register_strict_source


class StandardPythonLifecycleTests(unittest.TestCase):
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
        command = self.ports.execution_command(self.plan, output.exports)
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
            self.ports.execution_command(self.plan, output.exports)
        with self.assertRaises(LocalStandardLifecycleError):
            self.ports.build(self.plan, ())
