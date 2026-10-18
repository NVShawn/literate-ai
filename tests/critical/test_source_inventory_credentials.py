"""Inert classification and derivation of credential-shaped Python bindings."""

import tempfile
import unittest
from pathlib import Path

from literate_ai.source_to_specification.errors import SourceToSpecificationError
from literate_ai.source_to_specification.inventory import (
    SourceFileClassification,
    inventory_source,
)
from literate_ai.source_to_specification.skills import (
    builtin_skill_set,
    load_builtin_skill_catalog,
)
from literate_ai.source_to_specification.static_workflow import derive_static_checkout

FIXTURE = """def configuration():
    return None, None, None

def request():
    endpoint, model, api_key = configuration()
    return endpoint, model
"""


class SourceInventoryCredentialTests(unittest.TestCase):
    def test_safe_binding_does_not_hide_other_credential_literals(self):
        for extra in (
            "credentials = {'api_key': 'credential_literal_12345'}\n",
            "credentials = {'api_key': 'x'}\n",
            "configure(api_key='x')\n",
            "def configure(api_key='x'): pass\n",
            "def configure(*, api_key='x'): pass\n",
            "api_key: str = 'x'\n",
            "password = 'fixture-password'\n",
            'data = \'{"api_key":"x"}\'\n',
            "material = '-----BEGIN OPENSSH PRIVATE KEY-----'\n",
        ):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "backend.py"
                path.write_text(FIXTURE + extra, encoding="utf-8")
                self.assertEqual(
                    inventory_source(path).entries[0].classification,
                    SourceFileClassification.SENSITIVE,
                )

    def test_nonliteral_mapping_and_empty_defaults_remain_eligible(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backend.py"
            path.write_text(
                FIXTURE
                + "credentials = {'api_key': configuration()}\n"
                + "def configure(api_key=None, *, password=''): pass\n",
                encoding="utf-8",
            )
            self.assertEqual(
                inventory_source(path).entries[0].classification,
                SourceFileClassification.SOURCE,
            )

    def derive(self, root, inventory):
        catalog = load_builtin_skill_catalog()
        return derive_static_checkout(
            source=root,
            inventory=inventory,
            origin_attestation_id="fixture-origin",
            skill_set=builtin_skill_set(catalog, languages=("python",)),
            skill_catalog=catalog,
        )

    def test_ordinary_assignment_and_rename_control_derive_without_execution(self):
        for text in (FIXTURE, FIXTURE.replace("api_key", "key")):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "backend.py").write_text(
                    text + "\nraise AssertionError('source must never execute')\n",
                    encoding="utf-8",
                )
                inventory = inventory_source(root)
                self.assertEqual(
                    inventory.entries[0].classification, SourceFileClassification.SOURCE
                )
                self.assertIsNotNone(self.derive(root, inventory))

    def test_literal_uncertain_and_private_key_controls_remain_excluded(self):
        for name, text in (
            ("literal.py", "api_key='credential_literal_12345'\n"),
            ("number.py", "api_key=12345678901234567890\n"),
            ("call.py", "api_key=configuration('credential_literal_12345')\n"),
            ("broken.py", "api_key=configuration(\n"),
            ("unknown.js", "api_key=configuration();\n"),
            ("id_rsa", FIXTURE),
            ("quoted.py", "text='api_key=credential_literal_12345'\n"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / name).write_text(text, encoding="utf-8")
                inventory = inventory_source(root)
                self.assertEqual(
                    inventory.entries[0].classification,
                    SourceFileClassification.SENSITIVE,
                )
                with self.assertRaises(SourceToSpecificationError) as caught:
                    self.derive(root, inventory)
                self.assertEqual(caught.exception.code, "workflow.inventory_excluded")
                self.assertNotIn("credential_literal_12345", str(caught.exception))

    def test_non_ascii_offsets_do_not_exempt_a_later_literal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "backend.py"
            path.write_text("title='Ω'; api_key=configuration()\n", encoding="utf-8")
            self.assertEqual(
                inventory_source(path).entries[0].classification,
                SourceFileClassification.SOURCE,
            )
            path.write_text(
                path.read_text() + "api_key='credential_literal_12345'\n",
                encoding="utf-8",
            )
            self.assertEqual(
                inventory_source(path).entries[0].classification,
                SourceFileClassification.SENSITIVE,
            )
