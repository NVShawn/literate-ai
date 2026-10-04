from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.application_settings import (
    APP_SETTINGS_SCHEMA_V1,
    ApplicationSettingsError,
    default_application_settings,
    load_application_settings,
    parse_application_settings,
    redact_application_settings,
    write_application_settings,
)


@unittest.skipIf(os.name == "nt", "POSIX permission bits are not meaningful on Windows")
class PermissionTests(unittest.TestCase):
    def test_literal_credential_requires_owner_only_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": APP_SETTINGS_SCHEMA_V1,
                        "application": "my-app",
                        "active_profile": "default",
                        "profiles": {
                            "default": {
                                "settings": {},
                                "credentials": {"api_key": {"value": "literal-secret"}},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o644)
            with self.assertRaises(ApplicationSettingsError) as caught:
                load_application_settings(path, application="my-app")
            self.assertEqual(
                caught.exception.code, "application_settings.insecure_permissions"
            )

            path.chmod(0o600)
            document = load_application_settings(path, application="my-app")
            self.assertTrue(document.has_literal_credentials())

    def test_write_application_settings_always_uses_owner_only_permissions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "settings.json"
            document = default_application_settings("my-app")
            write_application_settings(path, document)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            reloaded = load_application_settings(path, application="my-app")
            self.assertEqual(reloaded.active_profile, "default")


class RedactionTests(unittest.TestCase):
    def test_literal_credential_values_are_redacted_but_references_are_kept(
        self,
    ) -> None:
        document = parse_application_settings(
            {
                "schema": APP_SETTINGS_SCHEMA_V1,
                "application": "my-app",
                "active_profile": "default",
                "profiles": {
                    "default": {
                        "settings": {"endpoint": "https://example.invalid"},
                        "credentials": {
                            "api_key": {"value": "super-secret-literal"},
                            "cert": {"ref": "keychain:my-app/cert"},
                        },
                    }
                },
            },
            application="my-app",
        )
        redacted = redact_application_settings(document)
        serialized = json.dumps(redacted)
        self.assertNotIn("super-secret-literal", serialized)
        self.assertIn("***redacted***", serialized)
        self.assertIn("keychain:my-app/cert", serialized)
        self.assertEqual(
            redacted["profiles"]["default"]["settings"]["endpoint"],
            "https://example.invalid",
        )


if __name__ == "__main__":
    unittest.main()
