"""Declarative Mix authority rejects executable configuration before admission."""

from __future__ import annotations

import json
import unittest

from literate_ai.contracts.mix_projects import (
    MixDependencyIntent,
    MixProjectIntent,
)


def project(**values):
    return MixProjectIntent(
        **{
            "app": "fixture",
            "version": "1.0.0",
            "description": "A portable fixture",
            "licenses": ("MIT",),
            "links": (("Source", "https://example.invalid/fixture"),),
            "dependencies": (MixDependencyIntent("decimal", "== 2.3.0"),),
            **values,
        }
    )


class MixProjectIntentTests(unittest.TestCase):
    def test_exact_intent_roundtrips_without_evaluating_a_manifest(self):
        value = project()
        self.assertEqual(
            MixProjectIntent.from_bytes(json.dumps(value.to_dict()).encode()), value
        )
        self.assertTrue(value.identity.uri.startswith("sha256:"))
        native = value.native_project(package_files=("mix.exs", "main.exs"))
        self.assertIn(b'{:decimal, "== 2.3.0"}', native)
        self.assertIn(b'elixirc_paths: ["."]', native)
        self.assertNotIn(b"Code.eval", native)

    def test_executable_manifest_and_unknown_project_options_are_rejected(self):
        with self.assertRaises(ValueError):
            MixProjectIntent.from_bytes(b'System.cmd("touch", ["marker"])')
        for key in ("compilers", "aliases", "plugins", "repositories", "umbrella"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                MixProjectIntent.from_bytes(
                    json.dumps({**project().to_dict(), key: []}).encode()
                )

    def test_git_path_and_other_dependency_authority_are_rejected(self):
        for key in ("git", "path", "repo", "optional", "override"):
            value = project().to_dict()
            value["dependencies"][0][key] = "unreviewed"
            with self.subTest(key=key), self.assertRaises(ValueError):
                MixProjectIntent.from_bytes(json.dumps(value).encode())

    def test_duplicate_json_keys_do_not_replace_authority(self):
        raw = json.dumps(project().to_dict()).replace(
            '"app": "fixture"', '"app": "other", "app": "fixture"'
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            MixProjectIntent.from_bytes(raw.encode())

    def test_duplicate_and_self_dependencies_are_rejected(self):
        for values in (
            (MixDependencyIntent("decimal", "1.0"),) * 2,
            (MixDependencyIntent("fixture", "1.0"),),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                project(dependencies=values)

    def test_interpolation_and_injected_names_cannot_enter_project_code(self):
        with self.assertRaises(ValueError):
            project(description='#{System.cmd("touch", ["marker"])}')
        for value in ("../other", "Other", "name;System.cmd", "git://example"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MixDependencyIntent(value, "1.0")
        with self.assertRaises(ValueError):
            MixDependencyIntent("decimal", '#{System.cmd("touch", ["marker"])}')

    def test_metadata_quotes_and_unicode_remain_literal(self):
        value = project(description='Unicode 日本語 😀 and "quotes" with \\slashes')
        native = value.native_project(package_files=("mix.exs", "main.exs"))
        self.assertIn("日本語 😀".encode(), native)
        self.assertIn(b'\\"quotes\\"', native)
        self.assertIn(b"\\\\slashes", native)

    def test_package_paths_cannot_escape_the_selected_project(self):
        for value in ("../outside", "/absolute", "C:\\absolute", "a/../b", "a#{expr}"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                project().native_project(package_files=(value,))

    def test_provider_compiler_preserves_pruning_and_quotes_exact_paths(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory).resolve() / 'provider "quoted"' / "ebin")
            native = project().native_project(
                package_files=("mix.exs", "main.exs"), provider_ebins=(path,)
            )
            self.assertIn(json.dumps(path).encode(), native)
            self.assertIn(b"Mix.Tasks.Compile.LitaiProviders", native)
            self.assertNotIn(b"prune_code_paths: false", native)
            for invalid in (
                "relative",
                directory + "/../escape",
                directory + "/#{expr}",
            ):
                with self.subTest(path=invalid), self.assertRaises(ValueError):
                    project().native_project(
                        package_files=("mix.exs",), provider_ebins=(invalid,)
                    )

    def test_metadata_links_reject_credentials_and_executable_interpolation(self):
        for value in (
            "file:///outside",
            "https://user:secret@example.invalid/source",
            "https://example.invalid/#{System.cmd()}",
            "https://example.invalid/with space",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                project(links=(("Source", value),))
        with self.assertRaises(ValueError):
            project(links=(("Source", "https://example.invalid"),) * 2)

    def test_shape_encoding_and_resource_limits_are_checked(self):
        for value in (b"\xff", b"{}", b"null", b" " * (64 * 1024 + 1)):
            with self.subTest(value=value[:8]), self.assertRaises(ValueError):
                MixProjectIntent.from_bytes(value)
        with self.assertRaises(ValueError):
            project(licenses=("MIT", "MIT"))
        with self.assertRaises(ValueError):
            project(version="01.0.0")


if __name__ == "__main__":
    unittest.main()
