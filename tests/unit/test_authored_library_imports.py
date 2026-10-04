"""Reviewed names survive authoring and drive the actual native import verifier."""

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.component_markdown import (
    parse_component_markdown,
    render_component_markdown,
)
from literate_ai.adapters.generation_preparation import (
    _library_import_surface_for_recipe,
)
from literate_ai.adapters.standard_project import (
    _STANDARD_LIBRARY_IMPORT_DRIVER,
    _library_import_surface,
)
from literate_ai.application.library_artifacts import project_library_import_surface
from literate_ai.contracts.component_locking import ComponentAuthoring
from literate_ai.contracts.flavors import FlavorAxis
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.library_imports import AuthoredLibraryImport
from tests.support import fixtures_test_standard_command_projection as commands
from tests.support.fixtures_test_component_authoring_lock_contracts import (
    authored_component,
    official_validator,
)


class AuthoredLibraryImportTests(unittest.TestCase):
    def setUp(self):
        self.declaration = AuthoredLibraryImport(
            "rust", "import_proof", "invoice-api", "import_proof", ("checked_sum",)
        )
        self.authoring = replace(
            authored_component(),
            entrypoints=(),
            kind="library",
            library_imports=(self.declaration,),
        )
        self.interface = canonical_identity("current reviewed public interface")

    def test_json_markdown_and_schema_preserve_declarations_and_old_defaults(self):
        old = replace(self.authoring, library_imports=())
        self.assertNotIn("library_imports", old.to_dict())
        self.assertNotEqual(old.identity, self.authoring.identity)
        self.assertEqual(
            ComponentAuthoring.from_dict(self.authoring.to_dict()), self.authoring
        )
        official_validator(ComponentAuthoring.SCHEMA).validate(self.authoring.to_dict())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "components/invoice-cli/component.md"
            text = render_component_markdown(self.authoring, path, project_root=root)
            self.assertEqual(
                parse_component_markdown(path, text, project_root=root), self.authoring
            )

    def test_recipe_and_execution_insert_same_current_interface_identity(self):
        node = SimpleNamespace(
            interface_bindings=(
                SimpleNamespace(
                    capability="invoice-api", interface_identity=self.interface
                ),
            )
        )
        selected = (
            SimpleNamespace(
                axis=FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value, value="rust"
            ),
        )
        recipe = _library_import_surface_for_recipe(
            self.authoring, self.authoring, node, selected
        )
        execution = _library_import_surface(self.authoring, node, "rust")
        self.assertEqual(recipe, execution)
        self.assertEqual(execution.package, "import_proof")
        self.assertEqual(execution.capabilities[0].module, "import_proof")
        self.assertEqual(execution.capabilities[0].interface_identity, self.interface)
        node.interface_bindings[0].interface_identity = canonical_identity(
            "changed interface"
        )
        self.assertNotEqual(
            execution.identity,
            _library_import_surface(self.authoring, node, "rust").identity,
        )

    def test_incomplete_foreign_duplicate_or_application_declarations_refuse(self):
        for declarations in (
            (replace(self.declaration, capability="foreign"),),
            (self.declaration, self.declaration),
        ):
            with self.subTest(declarations=declarations), self.assertRaises(ValueError):
                replace(self.authoring, library_imports=declarations)
        with self.assertRaises(ValueError):
            replace(authored_component(), library_imports=(self.declaration,))
        with self.assertRaises(ValueError):
            project_library_import_surface(
                "source",
                "rust",
                (("foreign", self.interface),),
                declarations=(self.declaration,),
            )

    def test_language_groups_are_complete_and_use_one_package(self):
        second = replace(self.authoring.provides[0], name="other-api")
        expanded = replace(
            self.authoring,
            library_imports=(),
            provides=(*self.authoring.provides, second),
        )
        with self.assertRaises(ValueError):
            replace(expanded, library_imports=(self.declaration,))
        other = replace(
            self.declaration,
            capability="other-api",
            package="foreign",
            module="foreign",
        )
        with self.assertRaises(ValueError):
            replace(expanded, library_imports=(self.declaration, other))
        with self.assertRaises(ValueError):
            replace(
                self.authoring,
                provides=(replace(self.authoring.provides[0], interface=None),),
            )
        for language, module in (
            ("python", "import_proof.api"),
            ("javascript", "import_proof/api"),
        ):
            declaration = replace(self.declaration, language=language, module=module)
            surface = project_library_import_surface(
                "source",
                language,
                (("invoice-api", self.interface),),
                declarations=(declaration,),
            )
            self.assertEqual(surface.capabilities[0].module, module)
            self.assertEqual(surface.capabilities[0].symbols, ("checked_sum",))

    def test_unsafe_names_and_resolver_fields_refuse(self):
        for update in (
            {"module": "foreign::checked_sum"},
            {"module": "import_proof::x;panic!()"},
            {"package": "../foreign"},
            {"symbols": ("checked_sum", "checked_sum")},
            {"symbols": ("x();",)},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                replace(self.declaration, **update)
        with self.assertRaises(ValueError):
            AuthoredLibraryImport.from_dict(
                {
                    **self.declaration.to_dict(),
                    "interface_identity": self.interface.to_dict(),
                }
            )

    def test_real_rust_root_function_import_passes_and_wrong_names_refuse(self):
        cargo = shutil.which("cargo")
        if cargo is None:
            self.skipTest("Cargo is unavailable")
        surface = project_library_import_surface(
            "source",
            "rust",
            (("invoice-api", self.interface),),
            declarations=(self.declaration,),
        )
        run = commands.StandardCommandProjectionTests._run_library_driver
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory)
            source = export / "source"
            (source / "src").mkdir(parents=True)
            (source / "Cargo.toml").write_text(
                '[package]\nname="import-proof"\nversion="1.0.0"\nedition="2021"\n'
            )
            library = source / "src/lib.rs"
            library.write_text(
                "pub fn checked_sum(a:i64,b:i64)->Option<i64>{a.checked_add(b)}\n"
            )
            passed = run(
                _STANDARD_LIBRARY_IMPORT_DRIVER,
                "rust",
                cargo,
                export,
                surface,
                test=False,
            )
            self.assertEqual(passed.returncode, 0, passed.stderr)
            old = project_library_import_surface(
                "source", "rust", (("invoice-api", self.interface),)
            )
            rejected = run(
                _STANDARD_LIBRARY_IMPORT_DRIVER, "rust", cargo, export, old, test=False
            )
            self.assertIn("Cargo package name differs", rejected.stderr)
            library.write_text("pub fn other() {}\n")
            rejected = run(
                _STANDARD_LIBRARY_IMPORT_DRIVER,
                "rust",
                cargo,
                export,
                surface,
                test=False,
            )
            self.assertNotEqual(rejected.returncode, 0)


class AuthoredCppLibraryImportTests(unittest.TestCase):
    setUp = AuthoredLibraryImportTests.setUp

    def test_cpp_headers_and_qualified_names_survive_authoring_and_projection(self):
        declaration = AuthoredLibraryImport(
            "cpp",
            "import_proof",
            "invoice-api",
            "import_proof/math.hpp",
            ("import_proof::CheckedSum", "import_proof::checked_sum"),
        )
        authoring = replace(self.authoring, library_imports=(declaration,))
        official_validator(ComponentAuthoring.SCHEMA).validate(authoring.to_dict())
        self.assertEqual(ComponentAuthoring.from_dict(authoring.to_dict()), authoring)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "components/invoice-cli/component.md"
            text = render_component_markdown(authoring, path, project_root=root)
            self.assertEqual(
                parse_component_markdown(path, text, project_root=root), authoring
            )
        surface = project_library_import_surface(
            "invoice-cli",
            "cpp",
            (("invoice-api", self.interface),),
            declarations=(declaration,),
        )
        official_validator(surface.SCHEMA).validate(surface.to_dict())
        self.assertEqual(surface.capabilities[0].module, declaration.module)
        self.assertEqual(surface.capabilities[0].symbols, declaration.symbols)
        self.assertEqual(surface.capabilities[0].interface_identity, self.interface)
        node = SimpleNamespace(
            interface_bindings=(
                SimpleNamespace(
                    capability="invoice-api", interface_identity=self.interface
                ),
            )
        )
        selected = (
            SimpleNamespace(
                axis=FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value, value="cpp"
            ),
        )
        self.assertEqual(
            _library_import_surface_for_recipe(authoring, authoring, node, selected),
            surface,
        )
        self.assertEqual(_library_import_surface(authoring, node, "cpp"), surface)

    def test_cpp_unsafe_headers_and_symbol_expressions_refuse(self):
        declaration = AuthoredLibraryImport(
            "cpp",
            "import_proof",
            "invoice-api",
            "import_proof/math.hpp",
            ("import_proof::checked_sum",),
        )
        for update in (
            {"module": "foreign/math.hpp"},
            {"module": "import_proof/../math.hpp"},
            {"module": 'import_proof/a.hpp"'},
            {"module": "import_proof/CON.hpp"},
            {"module": "import_proof/line\n.hpp"},
            {"symbols": ("import_proof::checked_sum()",)},
            {"symbols": ("import_proof::checked_sum;",)},
            {"symbols": ("import_proof::$function",)},
            {"symbols": ("import_proof::::checked_sum",)},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                replace(declaration, **update)
        with self.assertRaises(ValueError):
            replace(self.declaration, symbols=("import_proof::checked_sum",))
        with self.assertRaises(ValueError):
            project_library_import_surface(
                "invoice-cli", "cpp", (("invoice-api", self.interface),)
            )

    def test_cpp_command_projection_refuses_until_native_lifecycle_is_implemented(self):
        from literate_ai.adapters.standard_project import (
            StandardCommandProjectionError,
            _command_contract,
        )

        with self.assertRaises(StandardCommandProjectionError) as raised:
            _command_contract(
                None,
                SimpleNamespace(resolved_kind="library"),
                None,
                self.interface,
                SimpleNamespace(target="cpp"),
                None,
                None,
                {},
            )
        self.assertEqual(
            raised.exception.code, "standard_command.cpp_library_lifecycle_incomplete"
        )
