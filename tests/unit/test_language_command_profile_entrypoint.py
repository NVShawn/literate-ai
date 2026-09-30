"""Project-local language Flavors contribute source entrypoints via command profiles."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from literate_ai.adapters.models import (
    PortableApplicationError,
    portable_source_entrypoint,
)
from literate_ai.contracts import (
    STANDARD_LANGUAGE_COMMAND_PROFILE_SCHEMA,
    StandardLanguageCommandProfile,
    parse_standard_command_profile,
)
from tests.unit.test_coding_cli_generation import flavor, recipe


def _posix_profile() -> dict[str, object]:
    return {
        "schema": STANDARD_LANGUAGE_COMMAND_PROFILE_SCHEMA,
        "target": "cpp-posix",
        "toolchain": "cpp",
        "source_entrypoint": "source/main.cpp",
        "generated_test_entrypoint": "source/tests/litai_test.cpp",
        "artifact_layout": "file",
        "artifact_entrypoint": "hdTest",
        "bazel_output_path": "hdTest",
        "build_strategy": "cpp-executable",
        "runtime_strategy": "native-executable",
        "media_type": "application/vnd.literate-ai.native-executable",
    }


class LanguageCommandProfileEntrypointTests(unittest.TestCase):
    def test_contributed_profile_unlocks_non_catalog_language_target(self) -> None:
        profile = parse_standard_command_profile(_posix_profile())

        self.assertIsInstance(profile, StandardLanguageCommandProfile)
        self.assertEqual(profile.target, "cpp-posix")
        self.assertEqual(profile.source_entrypoint, "source/main.cpp")
        self.assertEqual(
            json.loads(json.dumps(profile.to_dict())),
            _posix_profile(),
        )

    def test_unknown_language_without_profile_fails_closed(self) -> None:
        with self.assertRaises(PortableApplicationError) as raised:
            portable_source_entrypoint("cpp-posix")

        self.assertIn("unsupported implementation language", str(raised.exception))
        self.assertIn("cpp-posix", str(raised.exception))

    def test_catalog_cpp_prompt_injects_json_argv_posix_does_not(self) -> None:
        cpp = replace(recipe(flavor("cpp")), required_entrypoint="source/main.cpp")
        posix = replace(
            recipe(flavor("cpp-posix")), required_entrypoint="source/main.cpp"
        )
        cpp_prompt = cpp.prompt()
        posix_prompt = posix.prompt()

        self.assertIn("complete JSON", cpp_prompt)
        self.assertIn("`argv[1]`", cpp_prompt)
        self.assertNotIn("complete JSON", posix_prompt)
        self.assertNotIn("one and only command-line argument", posix_prompt)
