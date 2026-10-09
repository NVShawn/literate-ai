"""Elixir import authority and real retained-library execution.

Dependency observation is a metadata fixture; Elixir parsing, compilation, native
tests and independent acceptance execute the real selected runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.builders import discover_elixir_toolchain
from literate_ai.adapters.component_acceptance import (
    DeclaredLibraryAcceptanceCase,
    LibraryAcceptance,
)
from literate_ai.adapters.directory_artifacts import directory_export_bytes
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
    local_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.adapters.standard_project import (
    _encoded_library_import_surface,
    project_locked_standard_toolchain_closure,
)
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    create_package_plan,
)
from literate_ai.application.library_artifacts import project_library_import_surface
from literate_ai.contracts import (
    ArtifactExport,
    BlobRef,
    ComponentCommandPhase,
    ContentIdentity,
    PackageKind,
    canonical_identity,
)
from literate_ai.contracts.component_locking import ComponentAuthoring
from literate_ai.contracts.library_imports import AuthoredLibraryImport
from tests.unit import test_artifact_graph_contracts as graph_fixtures
from tests.unit import test_package_release_contracts as package_fixtures
from tests.unit.test_component_authoring_lock_contracts import (
    authored_component,
    official_validator,
)
from tests.unit.test_standard_command_projection import _locked_snapshot, _observation

HOST = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
_HARNESS = b"""[artifact, surface_json, cases_json] = System.argv()
artifact = Path.expand(artifact)
surface = JSON.decode!(surface_json)
files = Path.wildcard(Path.join([artifact, "source", surface["package"], "**", "*.ex"]))
{:ok, modules, _} = Kernel.ParallelCompiler.compile(files)
cases = Enum.map(JSON.decode!(cases_json), fn item ->
  capability = Enum.find(surface["capabilities"], fn value ->
    value["capability"] == item["capability"]
  end)
  module = Enum.find(modules, fn value ->
    Atom.to_string(value) == "Elixir." <> capability["module"]
  end)
  {name, _} = Enum.find(module.__info__(:functions), fn {name, arity} ->
    Atom.to_string(name) == hd(capability["symbols"]) and
      arity == length(item["arguments"])
  end)
  %{case_id: item["case_id"], capability: item["capability"],
    result: apply(module, name, item["arguments"])}
end)
IO.puts(JSON.encode!(%{schema: "literate-ai/library-acceptance-results@1",
  cases: cases}))
"""


class ElixirLibraryContractTests(unittest.TestCase):
    def test_native_predicate_bang_exports_roundtrip_and_validate_schema(self):
        declaration = AuthoredLibraryImport(
            "elixir",
            "invoice_api",
            "invoice.add",
            "InvoiceApi.Math",
            ("add!", "valid?"),
        )
        surface = project_library_import_surface(
            "invoice-api",
            "elixir",
            ((declaration.capability, canonical_identity("interface")),),
            declarations=(declaration,),
        )
        self.assertEqual(surface.capabilities[0].symbols, ("add!", "valid?"))
        official_validator(surface.SCHEMA).validate(surface.to_dict())
        self.assertEqual(type(surface).from_dict(surface.to_dict()), surface)
        authoring = replace(
            authored_component(),
            entrypoints=(),
            kind="library",
            library_imports=(replace(declaration, capability="invoice-api"),),
        )
        official_validator(ComponentAuthoring.SCHEMA).validate(authoring.to_dict())
        self.assertEqual(ComponentAuthoring.from_dict(authoring.to_dict()), authoring)

    def test_namespace_escape_and_non_native_identifiers_refuse(self):
        for package, module, symbols in (
            ("invoice_api", "Other.Math", ("add",)),
            ("invoice_api", "InvoiceApiInjected.Math", ("add",)),
            ("invoice_api", "InvoiceApi.math", ("add",)),
            ("InvoiceApi", "InvoiceApi.Math", ("add",)),
            ("invoice_api", "InvoiceApi.Math", ("add/2",)),
            ("invoice_api", "InvoiceApi.Math", ("Add",)),
        ):
            with (
                self.subTest(module=module, symbols=symbols),
                self.assertRaises(ValueError),
            ):
                AuthoredLibraryImport("elixir", package, "invoice.add", module, symbols)

    def test_existing_languages_do_not_admit_elixir_only_symbols(self):
        for language in ("python", "javascript", "rust", "cpp"):
            module = (
                "invoice_api/add"
                if language in {"javascript", "cpp"}
                else "invoice_api"
            )
            with self.subTest(language=language), self.assertRaises(ValueError):
                AuthoredLibraryImport(
                    language, "invoice_api", "invoice.add", module, ("add!",)
                )


class ElixirLibraryNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if (
            shutil.which("elixir") is None
            and os.environ.get("LITERATE_AI_ELIXIR_QUALIFICATION") != "1"
        ):
            raise unittest.SkipTest("Elixir is unavailable")
        cls.tool = discover_elixir_toolchain()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="litai elixir library ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _, self.snapshot, self.execution = _locked_snapshot(
            self.root / "authority",
            language="elixir",
            platform=HOST,
            no_entrypoint=True,
        )
        self.closure = project_locked_standard_toolchain_closure(
            self.snapshot,
            self.execution,
            host_platform=HOST,
            dependency_observer=_observation,
        )
        self.contract = self.closure.contracts[0]
        self.surface = self.contract.library_import_surface
        self.capability = self.surface.capabilities[0]
        self.candidate = self.root / "candidate"
        self.module = self.candidate / "source" / self.surface.package / "api.ex"
        self.module.parent.mkdir(parents=True)
        self.write_module(self.module, "a + b")
        tests = self.candidate / "source/tests"
        tests.mkdir()
        (tests / "litai_test.exs").write_text(
            f"unless {self.capability.module}.{self.capability.symbols[0]}(2, 3) == 5, "
            'do: raise("bad addition")\n'
            'IO.puts(JSON.encode!(%{schema: "literate-ai/generated-test-results@1", '
            'cases: [%{case_id: "adds-values", outcome: "passed"}]}))\n',
            encoding="utf-8",
        )
        (self.candidate / "source/main.exs").write_text(
            "files = Path.wildcard(Path.join([__DIR__, "
            f'"{self.surface.package}", "**", "*.ex"]))\n'
            "{:ok, _, _} = Kernel.ParallelCompiler.compile(files)\n"
            '["--litai-test"] = System.argv()\n'
            'Code.require_file("tests/litai_test.exs", __DIR__)\n',
            encoding="utf-8",
        )
        self.custody_root = self.root / "sealed package"
        self.export = self.custody_root / self.contract.artifact_export.export_id
        self.custody_root.mkdir()

    def write_module(self, path, implementation):
        path.write_text(
            f"defmodule {self.capability.module} do\n"
            f"  def {self.capability.symbols[0]}(a, b), do: {implementation}\nend\n",
            encoding="utf-8",
        )

    def command(self, phase, *, environment=None, surface=None):
        binding = next(
            item
            for item in self.closure.tool_bindings
            if item.toolchain_identity
            == self.contract.tool_binding(phase).toolchain_identity
        )
        replacements = {
            "{source_root}": str(self.candidate),
            "{object_root}": str(self.root / "objects"),
            "{artifact_root}": str(self.custody_root),
            "{export_path}": str(self.export),
            "{provider_artifacts}": "[]",
        }
        argv = []
        for value in self.contract.command(phase).argv:
            if surface is not None and value == _encoded_library_import_surface(
                self.surface
            ):
                value = _encoded_library_import_surface(surface)
            argv.extend(
                binding.command
                if value == "{tool}"
                else (replacements.get(value, value),)
            )
        return subprocess.run(
            argv,
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )

    def build(self):
        completed = self.command(ComponentCommandPhase.BUILD)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_retained_library_build_test_and_import_after_source_retirement(self):
        self.build()
        shutil.rmtree(self.candidate)
        tested = self.command(ComponentCommandPhase.TEST)
        self.assertEqual(tested.returncode, 0, tested.stderr)
        self.assertEqual(json.loads(tested.stdout)["cases"][0]["outcome"], "passed")
        imported = self.command(ComponentCommandPhase.EXECUTE)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(
            json.loads(imported.stdout)["capabilities"], [self.capability.capability]
        )

    def test_missing_module_or_export_refuses_native_import(self):
        self.build()
        path = self.export / "source" / self.surface.package / "api.ex"
        for source in (
            "defmodule Ambient do\nend\n",
            f"defmodule {self.capability.module} do\n"
            "  def other(a, b), do: a + b\nend\n",
        ):
            with self.subTest(source=source):
                path.write_text(source, encoding="utf-8")
                self.assertNotEqual(
                    self.command(ComponentCommandPhase.EXECUTE).returncode, 0
                )

    def test_native_predicate_bang_and_macro_exports(self):
        self.build()
        path = self.export / "source" / self.surface.package / "api.ex"
        path.write_text(
            f"defmodule {self.capability.module} do\n"
            "  def add!(a, b), do: a + b\n"
            "  def valid?(value), do: is_integer(value)\n"
            "  defmacro literal(value), do: value\nend\n",
            encoding="utf-8",
        )
        surface = replace(
            self.surface,
            capabilities=(
                replace(self.capability, symbols=("add!", "literal", "valid?")),
            ),
        )
        imported = self.command(ComponentCommandPhase.EXECUTE, surface=surface)
        self.assertEqual(imported.returncode, 0, imported.stderr)

    def test_duplicate_module_definitions_refuse_native_import(self):
        self.build()
        module = self.export / "source" / self.surface.package / "api.ex"
        original = module.read_bytes()
        duplicate = module.parent / "duplicate.ex"
        duplicate.write_bytes(original)
        with self.subTest(duplicate="separate files"):
            self.assertNotEqual(
                self.command(ComponentCommandPhase.EXECUTE).returncode, 0
            )
        duplicate.unlink()
        module.write_bytes(original + original)
        with self.subTest(duplicate="same file"):
            self.assertNotEqual(
                self.command(ComponentCommandPhase.EXECUTE).returncode, 0
            )

    def test_struct_dependencies_compile_independently_of_filename_order(self):
        namespace = self.capability.module.rsplit(".", 1)[0]
        (self.module.parent / "z_box.ex").write_text(
            f"defmodule {namespace}.Box do\n  defstruct [:value]\nend\n",
            encoding="utf-8",
        )
        self.write_module(self.module, f"%{namespace}.Box{{value: a + b}}.value")
        self.build()
        shutil.rmtree(self.candidate)
        for phase in (ComponentCommandPhase.TEST, ComponentCommandPhase.EXECUTE):
            with self.subTest(phase=phase):
                completed = self.command(phase)
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_application_without_its_entrypoint_cannot_publish_a_tree(self):
        _, snapshot, execution = _locked_snapshot(
            self.root / "application authority",
            language="elixir",
            platform=HOST,
        )
        self.closure = project_locked_standard_toolchain_closure(
            snapshot,
            execution,
            host_platform=HOST,
            dependency_observer=_observation,
        )
        self.contract = self.closure.contracts[0]
        self.export = self.custody_root / self.contract.artifact_export.export_id
        (self.candidate / "source/main.exs").unlink()
        self.assertNotEqual(self.command(ComponentCommandPhase.BUILD).returncode, 0)
        self.assertFalse(self.export.exists())

    def artifact(self):
        content = directory_export_bytes(self.export)
        shape = self.contract.artifact_export
        return ArtifactExport(
            **{field.name: getattr(shape, field.name) for field in fields(shape)},
            component_revision=self.contract.component_revision,
            source_tree_identity=local_tree_identity(self.export),
            toolchain_identity=self.contract.language_compiler_identity,
            authorization_identity=canonical_identity("native-oracle-fixture"),
            dependency_artifact_identities=(),
            blob=BlobRef(
                hashlib.sha256(content).hexdigest(),
                len(content),
                media_type=shape.media_type,
            ),
        )

    def test_consuming_application_uses_packaged_provider_after_source_retirement(self):
        self.build()
        library = self.artifact()
        provider = self.export
        shutil.rmtree(self.candidate)
        _, snapshot, execution = _locked_snapshot(
            self.root / "consumer authority",
            language="elixir",
            platform=HOST,
        )
        self.closure = project_locked_standard_toolchain_closure(
            snapshot,
            execution,
            host_platform=HOST,
            dependency_observer=_observation,
        )
        self.contract = self.closure.contracts[0]
        self.candidate = self.root / "consumer source"
        (self.candidate / "source").mkdir(parents=True)
        (self.candidate / "source/main.exs").write_text(
            'root = System.fetch_env!("LITAI_LIBRARY_ROOT")\n'
            'Code.require_file(Path.join([root, "source", '
            f'"{self.surface.package}", "api.ex"]))\n'
            '["--litai-smoke"] = System.argv()\n'
            f"result = {self.capability.module}.{self.capability.symbols[0]}(2, 3)\n"
            "IO.puts(JSON.encode!(result))\n",
            encoding="utf-8",
        )
        self.export = self.custody_root / self.contract.artifact_export.export_id
        self.build()
        shutil.rmtree(self.candidate)
        ports = LocalStandardLifecyclePorts(
            source_trees=LocalSourceTreeRegistry(),
            object_root=self.root / "objects",
            contracts=self.closure.contracts,
            tool_bindings=self.closure.tool_bindings,
            provider_environment={
                library.export_id: ("LITAI_LIBRARY_ROOT", library.export_id)
            },
        )
        ports._exports_by_identity[library.identity.uri] = library
        custody = SimpleNamespace(artifact_paths={library.identity.uri: provider})
        environment = ports._packaged_environment(custody)
        before = local_tree_identity(self.custody_root)
        executed = self.command(ComponentCommandPhase.EXECUTE, environment=environment)
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertEqual(json.loads(executed.stdout), 5)
        self.assertEqual(local_tree_identity(self.custody_root), before)

    def accept(self, *, implementation="a + b", expected=5):
        self.build()
        self.write_module(
            self.export / "source" / self.surface.package / "api.ex", implementation
        )
        content = directory_export_bytes(self.export)
        shape = self.contract.artifact_export
        artifact = ArtifactExport(
            **{field.name: getattr(shape, field.name) for field in fields(shape)},
            component_revision=self.contract.component_revision,
            source_tree_identity=local_tree_identity(self.export),
            toolchain_identity=self.contract.language_compiler_identity,
            authorization_identity=canonical_identity("native-oracle-fixture"),
            dependency_artifact_identities=(),
            blob=BlobRef(
                hashlib.sha256(content).hexdigest(),
                len(content),
                media_type=shape.media_type,
            ),
        )
        root = next(
            node
            for node in self.snapshot.authority.lock.nodes
            if node.revision.identity == self.snapshot.authority.lock.root_revision
        )
        oracle = LibraryAcceptance(
            root.revision.coordinate.name,
            root.revision.specification_set_identity,
            tuple(
                sorted(
                    (item.identity for item in root.revision.public_interfaces),
                    key=lambda item: item.uri,
                )
            ),
            self.surface.identity,
            "elixir",
            ContentIdentity.parse_uri("sha256:" + hashlib.sha256(_HARNESS).hexdigest()),
            _HARNESS,
            (
                DeclaredLibraryAcceptanceCase(
                    "adds-values", self.capability.capability, [2, 3], expected
                ),
            ),
        )
        ports = LocalStandardLifecyclePorts(
            source_trees=LocalSourceTreeRegistry(),
            object_root=self.root / "objects",
            contracts=self.closure.contracts,
            tool_bindings=self.closure.tool_bindings,
            independent_acceptance_oracle=oracle,
        )
        fixture = graph_fixtures.ArtifactGraphTests()
        graph = create_artifact_build_graph(
            build_system_driver_identity=fixture.driver,
            manifests=(fixture.manifest(artifact),),
            link_roots=(artifact.identity,),
        )
        plan = create_package_plan(
            graph,
            root_component_revision=artifact.component_revision,
            component_lock_identity=self.execution.component_lock_identity,
            target_identity=artifact.target_identity,
            root_artifact_identity=artifact.identity,
            package_kind=PackageKind.DIRECTORY,
            packager_identity=canonical_identity("native-oracle-fixture-packager"),
            destinations={artifact.identity.uri: artifact.export_id},
            entrypoints=(),
            runtime_requirements=(),
        )
        package = package_fixtures.PackageReleaseContractTests().result(plan)
        ports._planned_exports[self.snapshot.authority.lock.root_revision.uri] = (
            artifact
        )
        custody = SimpleNamespace(
            root=self.custody_root,
            artifact_paths={artifact.identity.uri: self.export},
            tree_identity=local_tree_identity(self.custody_root),
        )
        with mock.patch.object(ports, "project_package_custody", return_value=custody):
            return ports.accept_project_independently(
                self.snapshot.authority.lock,
                self.execution,
                None,
                plan,
                package,
                canonical_identity("generated-test-fixture"),
                canonical_identity("execution-fixture"),
            )

    def test_independent_harness_accepts_native_function(self):
        self.assertTrue(self.accept().uri.startswith("sha256:"))

    def test_independent_harness_rejects_incorrect_native_function(self):
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "differs for 'adds-values'"
        ):
            self.accept(implementation="a - b")
