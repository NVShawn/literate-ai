from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath

from literate_ai.adapters.application_settings import (
    APP_SETTINGS_SCHEMA_V1,
    ApplicationSettingsError,
    CredentialEntry,
    ProfileSettings,
    default_application_settings,
    example_application_settings,
    load_application_settings,
    parse_application_settings,
    redact_application_settings,
    resolve_app_settings_path,
    resolve_application_config_root,
    resolve_setting,
    upgrade_application_settings,
    write_application_settings,
)


class ApplicationConfigRootResolutionTests(unittest.TestCase):
    def test_linux_and_macos_share_the_posix_xdg_root_separate_from_literate_ai(
        self,
    ) -> None:
        for family in ("linux", "macos"):
            with self.subTest(platform=family):
                root = resolve_application_config_root(
                    "my-app",
                    platform_family=family,
                    environment={},
                    home=PurePosixPath("/users/operator"),
                )
                self.assertEqual(root, PurePosixPath("/users/operator/.config/my-app"))
                self.assertNotIn("literate-ai", str(root))

    def test_xdg_config_home_override_has_precedence_over_home(self) -> None:
        root = resolve_application_config_root(
            "my-app",
            platform_family="linux",
            environment={"XDG_CONFIG_HOME": "/xdg/config"},
            home=PurePosixPath("/users/operator"),
        )
        self.assertEqual(root, PurePosixPath("/xdg/config/my-app"))

    def test_windows_uses_roaming_appdata_known_folder(self) -> None:
        root = resolve_application_config_root(
            "my-app",
            platform_family="windows",
            environment={},
            windows_known_folder=lambda kind: {
                "roaming": PureWindowsPath("C:/Users/operator/AppData/Roaming")
            }[kind],
        )
        self.assertEqual(
            root, PureWindowsPath("C:/Users/operator/AppData/Roaming/my-app")
        )

    def test_application_namespace_must_be_a_safe_lowercase_identifier(self) -> None:
        with self.assertRaises(ApplicationSettingsError) as caught:
            resolve_application_config_root(
                "My App!", platform_family="linux", environment={}
            )
        self.assertEqual(
            caught.exception.code, "application_settings.application_invalid"
        )


class AppSettingsPathResolutionTests(unittest.TestCase):
    def test_explicit_path_wins_over_environment_and_default(self) -> None:
        path = resolve_app_settings_path(
            "my-app",
            explicit="/explicit/settings.json",
            environment={"MY_APP_SETTINGS_FILE": "/env/settings.json"},
            platform_family="linux",
            home=PurePosixPath("/users/operator"),
        )
        self.assertEqual(path, Path("/explicit/settings.json"))

    def test_application_specific_environment_override_wins_over_default(self) -> None:
        path = resolve_app_settings_path(
            "my-app",
            environment={"MY_APP_SETTINGS_FILE": "/env/settings.json"},
            platform_family="linux",
            home=PurePosixPath("/users/operator"),
        )
        self.assertEqual(path, Path("/env/settings.json"))

    def test_default_settings_path_is_native_config_root_plus_settings_json(
        self,
    ) -> None:
        path = resolve_app_settings_path(
            "my-app",
            environment={},
            platform_family="linux",
            home=PurePosixPath("/users/operator"),
        )
        self.assertEqual(path, Path("/users/operator/.config/my-app/settings.json"))

    def test_relative_explicit_or_override_paths_fail_closed(self) -> None:
        with self.assertRaises(ApplicationSettingsError) as explicit_case:
            resolve_app_settings_path(
                "my-app",
                explicit="relative.json",
                environment={},
                platform_family="linux",
            )
        self.assertEqual(
            explicit_case.exception.code, "application_settings.override_relative"
        )

        with self.assertRaises(ApplicationSettingsError) as env_case:
            resolve_app_settings_path(
                "my-app",
                environment={"MY_APP_SETTINGS_FILE": "relative.json"},
                platform_family="linux",
            )
        self.assertEqual(
            env_case.exception.code, "application_settings.override_relative"
        )


class SettingPrecedenceTests(unittest.TestCase):
    def _document(self):
        return parse_application_settings(
            {
                "schema": APP_SETTINGS_SCHEMA_V1,
                "application": "my-app",
                "active_profile": "default",
                "profiles": {
                    "default": {
                        "settings": {"endpoint": "https://from-file.example"},
                        "credentials": {},
                    }
                },
            },
            application="my-app",
        )

    def test_cli_beats_environment_beats_file_beats_default(self) -> None:
        document = self._document()
        self.assertEqual(
            resolve_setting(
                "endpoint",
                cli_value="https://from-cli.example",
                environment={"MY_APP_ENDPOINT": "https://from-env.example"},
                environment_variable="MY_APP_ENDPOINT",
                document=document,
                default="https://from-default.example",
            ),
            "https://from-cli.example",
        )

    def test_environment_beats_file_beats_default(self) -> None:
        document = self._document()
        self.assertEqual(
            resolve_setting(
                "endpoint",
                environment={"MY_APP_ENDPOINT": "https://from-env.example"},
                environment_variable="MY_APP_ENDPOINT",
                document=document,
                default="https://from-default.example",
            ),
            "https://from-env.example",
        )

    def test_file_beats_default(self) -> None:
        document = self._document()
        self.assertEqual(
            resolve_setting(
                "endpoint", document=document, default="https://from-default.example"
            ),
            "https://from-file.example",
        )

    def test_default_used_when_nothing_else_present(self) -> None:
        self.assertEqual(
            resolve_setting("endpoint", default="https://from-default.example"),
            "https://from-default.example",
        )


class MalformedAndVersionMismatchTests(unittest.TestCase):
    def test_missing_file_returns_the_spec_default_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            document = load_application_settings(
                Path(temporary) / "absent" / "settings.json", application="my-app"
            )
        self.assertEqual(document.active_profile, "default")
        self.assertEqual(document.profile().settings, {})

    def test_malformed_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ApplicationSettingsError) as caught:
                load_application_settings(path, application="my-app")
            self.assertEqual(caught.exception.code, "application_settings.malformed")

    def test_unsupported_future_schema_fails_closed(self) -> None:
        with self.assertRaises(ApplicationSettingsError) as caught:
            parse_application_settings(
                {
                    "schema": "literate-ai/app-settings@99",
                    "application": "my-app",
                    "active_profile": "default",
                    "profiles": {"default": {"settings": {}, "credentials": {}}},
                },
                application="my-app",
            )
        self.assertEqual(
            caught.exception.code, "application_settings.schema_unsupported"
        )

    def test_application_namespace_mismatch_fails_closed(self) -> None:
        with self.assertRaises(ApplicationSettingsError) as caught:
            parse_application_settings(
                {
                    "schema": APP_SETTINGS_SCHEMA_V1,
                    "application": "other-app",
                    "active_profile": "default",
                    "profiles": {"default": {"settings": {}, "credentials": {}}},
                },
                application="my-app",
            )
        self.assertEqual(
            caught.exception.code, "application_settings.application_mismatch"
        )

    def test_credential_must_declare_exactly_one_of_ref_or_value(self) -> None:
        with self.assertRaises(ApplicationSettingsError) as neither:
            parse_application_settings(
                {
                    "schema": APP_SETTINGS_SCHEMA_V1,
                    "application": "my-app",
                    "active_profile": "default",
                    "profiles": {
                        "default": {"settings": {}, "credentials": {"api_key": {}}}
                    },
                },
                application="my-app",
            )
        self.assertEqual(
            neither.exception.code, "application_settings.credential_invalid"
        )

        with self.assertRaises(ApplicationSettingsError) as both:
            parse_application_settings(
                {
                    "schema": APP_SETTINGS_SCHEMA_V1,
                    "application": "my-app",
                    "active_profile": "default",
                    "profiles": {
                        "default": {
                            "settings": {},
                            "credentials": {
                                "api_key": {"ref": "keychain:x", "value": "y"}
                            },
                        }
                    },
                },
                application="my-app",
            )
        self.assertEqual(
            both.exception.code, "application_settings.credential_ambiguous"
        )

    def test_unknown_active_profile_fails_closed(self) -> None:
        with self.assertRaises(ApplicationSettingsError) as caught:
            parse_application_settings(
                {
                    "schema": APP_SETTINGS_SCHEMA_V1,
                    "application": "my-app",
                    "active_profile": "staging",
                    "profiles": {"default": {"settings": {}, "credentials": {}}},
                },
                application="my-app",
            )
        self.assertEqual(caught.exception.code, "application_settings.profile_unknown")


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

    def test_reference_only_credentials_do_not_require_owner_only_permissions(
        self,
    ) -> None:
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
                                "credentials": {
                                    "api_key": {"ref": "keychain:my-app/api_key"}
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o644)
            document = load_application_settings(path, application="my-app")
            self.assertFalse(document.has_literal_credentials())

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


class NamedProfileTests(unittest.TestCase):
    def test_multiple_named_profiles_are_independently_addressable(self) -> None:
        document = parse_application_settings(
            {
                "schema": APP_SETTINGS_SCHEMA_V1,
                "application": "my-app",
                "active_profile": "production",
                "profiles": {
                    "production": {
                        "settings": {"endpoint": "https://prod.example"},
                        "credentials": {},
                    },
                    "staging": {
                        "settings": {"endpoint": "https://staging.example"},
                        "credentials": {},
                    },
                },
            },
            application="my-app",
        )
        self.assertEqual(
            document.profile().settings["endpoint"], "https://prod.example"
        )
        self.assertEqual(
            document.profile("staging").settings["endpoint"], "https://staging.example"
        )
        with self.assertRaises(ApplicationSettingsError) as caught:
            document.profile("missing")
        self.assertEqual(caught.exception.code, "application_settings.profile_unknown")


class UpdatePreservationTests(unittest.TestCase):
    def test_unversioned_legacy_settings_are_preserved_when_the_schema_is_introduced(
        self,
    ) -> None:
        legacy = {"endpoint": "https://legacy.example", "timeout_seconds": 30}
        upgraded = upgrade_application_settings(legacy, application="my-app")
        self.assertEqual(upgraded["schema"], APP_SETTINGS_SCHEMA_V1)
        self.assertEqual(
            upgraded["profiles"]["default"]["settings"],
            {"endpoint": "https://legacy.example", "timeout_seconds": 30},
        )
        # The upgraded document must itself parse cleanly.
        document = parse_application_settings(upgraded, application="my-app")
        self.assertEqual(
            document.profile().settings["endpoint"], "https://legacy.example"
        )

    def test_loading_an_unversioned_file_upgrades_it_in_place_without_writing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            path.write_text(
                json.dumps({"endpoint": "https://legacy.example"}), encoding="utf-8"
            )
            document = load_application_settings(path, application="my-app")
            self.assertEqual(
                document.profile().settings["endpoint"], "https://legacy.example"
            )
            # Loading is read-only: the on-disk legacy file is untouched.
            self.assertNotIn("schema", json.loads(path.read_text(encoding="utf-8")))


class ExampleFileTests(unittest.TestCase):
    def test_example_settings_contain_no_literal_secret_or_personal_host(self) -> None:
        example = example_application_settings("my-app")
        self.assertNotIn(
            "value",
            json.loads(example)["profiles"]["default"]["credentials"]["api_key"],
        )
        self.assertIn("keychain:my-app/api_key", example)
        self.assertIn("example.invalid", example)
        # Round-trips as a valid document of its own schema.
        document = parse_application_settings(json.loads(example), application="my-app")
        self.assertFalse(document.has_literal_credentials())


class DataclassHelperTests(unittest.TestCase):
    def test_credential_entry_and_profile_serialize_round_trip(self) -> None:
        entry = CredentialEntry(ref="keychain:my-app/api_key")
        self.assertTrue(entry.is_reference())
        profile = ProfileSettings(
            "default", settings={"a": 1}, credentials={"k": entry}
        )
        self.assertFalse(profile.has_literal_credentials())
        self.assertEqual(
            profile.to_dict()["credentials"]["k"], {"ref": "keychain:my-app/api_key"}
        )


if __name__ == "__main__":
    unittest.main()
