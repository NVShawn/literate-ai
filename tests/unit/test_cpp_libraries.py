"""Native product declarations preserve exact cross-platform file roles."""

import unittest
from dataclasses import replace

from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.cpp_libraries import CppLibraryLayout


class CppLibraryLayoutTests(unittest.TestCase):
    def setUp(self):
        self.static = CppLibraryLayout(
            "static", ("include/sample/math.hpp",), ("lib/libsample.a",)
        )

    def test_static_and_shared_platform_layouts_round_trip(self):
        layouts = (
            self.static,
            replace(self.static, link_files=("lib/sample.lib",)),
            CppLibraryLayout(
                "shared",
                self.static.headers,
                ("lib/libsample.so",),
                ("lib/libsample.so",),
            ),
            CppLibraryLayout(
                "shared",
                self.static.headers,
                ("lib/libsample.dylib",),
                ("lib/libsample.dylib",),
            ),
            CppLibraryLayout(
                "shared", self.static.headers, ("lib/sample.lib",), ("bin/sample.dll",)
            ),
        )
        for layout in layouts:
            with self.subTest(layout=layout):
                self.assertEqual(CppLibraryLayout.from_dict(layout.to_dict()), layout)
                self.assertEqual(len(layout.files), len(set(layout.files)))
        self.assertEqual(len({item.identity for item in layouts}), len(layouts))

    def test_rejects_missing_products_and_wrong_runtime_role(self):
        for changes in (
            {"headers": ()},
            {"link_files": ()},
            {"kind": "header-only"},
            {"kind": "shared"},
            {"runtime_files": ("bin/sample.dll",)},
            {"headers": ["include/sample/math.hpp"]},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(ContractValidationError),
            ):
                replace(self.static, **changes)

    def test_rejects_nonportable_and_noncanonical_file_trees(self):
        for paths in (
            ("include/../escape.hpp",),
            ("include/CON.hpp",),
            ("/include/math.hpp",),
            ("include\\math.hpp",),
            ("include/a.hpp", "include/A.hpp"),
            ("include/a.hpp", "include/a.hpp"),
            ("include/a", "include/a/b.hpp"),
            ("include/z.hpp", "include/a.hpp"),
        ):
            with self.subTest(paths=paths), self.assertRaises(ContractValidationError):
                replace(self.static, headers=paths)

    def test_rejects_aliases_across_link_and_runtime_roles(self):
        with self.assertRaises(ContractValidationError):
            CppLibraryLayout(
                "shared", self.static.headers, ("lib/sample.so",), ("lib/Sample.so",)
            )
        with self.assertRaises(ContractValidationError):
            CppLibraryLayout(
                "shared",
                self.static.headers,
                ("lib/sample",),
                ("lib/sample/runtime.so",),
            )

    def test_rejects_wrong_file_roots(self):
        for changes in (
            {"headers": ("source/sample.hpp",)},
            {"link_files": ("source/main.cpp",)},
            {"kind": "shared", "runtime_files": ("source/main.cpp",)},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(ContractValidationError),
            ):
                replace(self.static, **changes)

    def test_wire_requires_explicit_fields_and_schema(self):
        wire = self.static.to_dict()
        for key in wire:
            changed = dict(wire)
            del changed[key]
            with self.subTest(key=key), self.assertRaises(ContractValidationError):
                CppLibraryLayout.from_dict(changed)
        for extra in ({"compiler_flags": "-L/ambient"}, {"schema": "unknown"}):
            with self.subTest(extra=extra), self.assertRaises(ContractValidationError):
                CppLibraryLayout.from_dict({**wire, **extra})


class CppLibraryProductTests(unittest.TestCase):
    def test_product_binds_layout_and_public_header_mapping(self):
        from literate_ai.contracts.library_products import LibraryArtifactProduct
        from tests.unit.test_library_products import library_product

        ordinary = library_product()
        capability = replace(
            ordinary.import_surface.capabilities[0], module="sample/math.hpp"
        )
        surface = replace(
            ordinary.import_surface,
            language="cpp",
            package="sample",
            capabilities=(capability,),
        )
        layout = CppLibraryLayout(
            "static", ("include/sample/math.hpp",), ("lib/sample.a",)
        )
        product = LibraryArtifactProduct(ordinary.artifact_export, surface, layout)
        self.assertEqual(LibraryArtifactProduct.from_dict(product.to_dict()), product)
        from tests.unit.test_executable_component_v2_schemas import _official_validator

        validator = _official_validator(
            "urn:literate-ai:schema:v2:execution-dispatch-contracts#/$defs/library_product"
        )
        validator.validate(product.to_dict())
        validator.validate(ordinary.to_dict())
        invalid = product.to_dict()
        del invalid["native_layout"]
        self.assertFalse(validator.is_valid(invalid))
        self.assertFalse(
            validator.is_valid(
                {**ordinary.to_dict(), "native_layout": layout.to_dict()}
            )
        )

        self.assertNotEqual(
            product.identity,
            replace(
                product, native_layout=replace(layout, link_files=("lib/changed.a",))
            ).identity,
        )
        with self.assertRaises(ContractValidationError):
            replace(product, native_layout=None)
        with self.assertRaises(ContractValidationError):
            replace(
                product, native_layout=replace(layout, headers=("include/other.hpp",))
            )
        with self.assertRaises(ContractValidationError):
            replace(ordinary, native_layout=layout)
        with self.assertRaises(ContractValidationError):
            LibraryArtifactProduct.from_dict(
                {**ordinary.to_dict(), "native_layout": None}
            )


class CppCommandLayoutTests(unittest.TestCase):
    def test_command_round_trip_binds_layout_and_requires_public_header_closure(self):
        from types import SimpleNamespace

        from literate_ai.cli.rebuild import _standard_root_product_result
        from literate_ai.contracts import ComponentCommandContract
        from tests.unit.test_component_command_contracts import contract
        from tests.unit.test_executable_component_v2_schemas import _official_validator
        from tests.unit.test_library_products import library_product

        base = contract()
        product = library_product()
        capability = replace(
            product.import_surface.capabilities[0],
            module="sample/api.hpp",
            symbols=("sample::add",),
        )
        surface = replace(
            product.import_surface,
            language="cpp",
            package="sample",
            capabilities=(capability,),
        )
        layout = CppLibraryLayout(
            "static", ("include/sample/api.hpp",), ("lib/sample.a",)
        )
        native = replace(
            base,
            library_import_surface=surface,
            native_layout=layout,
            artifact_export=replace(base.artifact_export, role="library"),
        )
        self.assertEqual(ComponentCommandContract.from_dict(native.to_dict()), native)
        validator = _official_validator(ComponentCommandContract.SCHEMA)
        validator.validate(native.to_dict())
        self.assertNotEqual(
            native.identity,
            replace(
                native, native_layout=replace(layout, link_files=("lib/other.a",))
            ).identity,
        )
        for update in (
            {"native_layout": None},
            {"native_layout": replace(layout, headers=("include/other.hpp",))},
        ):
            with (
                self.subTest(update=update),
                self.assertRaises(ContractValidationError),
            ):
                replace(native, **update)
        with self.assertRaises(ContractValidationError):
            replace(base, native_layout=layout)
        absent = native.to_dict()
        del absent["native_layout"]
        self.assertFalse(validator.is_valid(absent))
        export = replace(
            product.artifact_export, component_revision=native.component_revision
        )
        ports = SimpleNamespace(contracts={native.component_revision.uri: native})
        invocation, result = _standard_root_product_result(
            ports,
            SimpleNamespace(component_revision=native.component_revision),
            (export,),
            export,
        )
        self.assertIsNone(invocation)
        self.assertEqual(result["native_layout"], layout.to_dict())
