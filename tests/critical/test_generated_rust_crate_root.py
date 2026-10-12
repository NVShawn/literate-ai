"""Generated rules_rust targets must name a crate root Bazel can infer."""

from __future__ import annotations

import unittest

from literate_ai.adapters.models.generated_source_validation import (
    GeneratedSourceValidationError,
    validate_rust_bazel_source_closure,
)

_MAIN = '#[path = "tests/litai_test.rs"]\nmod litai_test;\n\nfn main() {}\n'
_TEST_MODULE = "pub fn run() {}\n"
_AMBIGUITY = "coding_cli.generated_rust_bazel_crate_root_ambiguous"


def _tree(build: str, **extra: str) -> dict[str, str]:
    files = {
        "source/BUILD.bazel": build,
        "source/main.rs": _MAIN,
        "source/tests/litai_test.rs": _TEST_MODULE,
    }
    files.update({f"source/{path}": content for path, content in extra.items()})
    return files


class GeneratedRustCrateRootTests(unittest.TestCase):
    def test_multi_source_rust_test_without_crate_root_is_rejected(self) -> None:
        build = (
            "rust_test(\n"
            '    name = "behavior_test",\n'
            '    srcs = ["main.rs", "tests/litai_test.rs"],\n'
            ")\n"
        )

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_rust_bazel_source_closure(_tree(build))

        self.assertEqual(raised.exception.code, _AMBIGUITY)
        self.assertIn("'behavior_test'", raised.exception.message)
        self.assertIn('crate_root = "main.rs"', raised.exception.message)

    def test_explicit_crate_root_is_accepted(self) -> None:
        build = (
            "rust_test(\n"
            '    name = "behavior_test",\n'
            '    crate_root = "main.rs",\n'
            '    srcs = ["main.rs", "tests/litai_test.rs"],\n'
            ")\n"
        )

        validate_rust_bazel_source_closure(_tree(build))

    def test_single_source_is_accepted(self) -> None:
        build = 'rust_test(\n    name = "behavior_test",\n    srcs = ["only.rs"],\n)\n'

        validate_rust_bazel_source_closure(_tree(build, **{"only.rs": "fn x() {}\n"}))

    def test_rust_test_with_lib_rs_is_accepted(self) -> None:
        build = (
            "rust_test(\n"
            '    name = "behavior_test",\n'
            '    srcs = ["lib.rs", "helper.rs"],\n'
            ")\n"
        )

        validate_rust_bazel_source_closure(
            _tree(build, **{"lib.rs": "mod helper;\n", "helper.rs": "\n"})
        )

    def test_sources_named_after_the_target_are_accepted(self) -> None:
        for attributes, root in (
            ('name = "behavior-test"', "behavior-test.rs"),
            ('name = "t", crate_name = "behavior"', "behavior.rs"),
            ('name = "t", crate_name = "behavior"', "t.rs"),
        ):
            with self.subTest(root=root):
                build = f'rust_test({attributes}, srcs = ["{root}", "helper.rs"])\n'

                validate_rust_bazel_source_closure(
                    _tree(build, **{root: "mod helper;\n", "helper.rs": "\n"})
                )

    def test_dashes_are_not_rewritten_when_inferring_the_root(self) -> None:
        build = (
            'rust_test(name = "behavior-test", srcs = ["behavior_test.rs", "h.rs"])\n'
        )

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_rust_bazel_source_closure(
                _tree(build, **{"behavior_test.rs": "mod h;\n", "h.rs": "\n"})
            )

        self.assertEqual(raised.exception.code, _AMBIGUITY)

    def test_harness_free_rust_test_defaults_to_main_rs(self) -> None:
        build = (
            "rust_test(\n"
            '    name = "behavior_test",\n'
            "    use_libtest_harness = False,\n"
            '    srcs = ["main.rs", "tests/litai_test.rs"],\n'
            ")\n"
        )

        validate_rust_bazel_source_closure(_tree(build))
        with self.assertRaises(GeneratedSourceValidationError):
            validate_rust_bazel_source_closure(_tree(build.replace("False", "True")))

    def test_product_generation_retries_an_ambiguous_crate_root(self) -> None:
        from literate_ai.adapters.models import coding_cli

        self.assertIn(_AMBIGUITY, coding_cli._TRANSIENT_GENERATION_ERROR_CODES)

    def test_rust_binary_with_main_rs_is_accepted(self) -> None:
        build = (
            "rust_binary(\n"
            '    name = "run",\n'
            '    srcs = ["main.rs", "tests/litai_test.rs"],\n'
            ")\n"
        )

        validate_rust_bazel_source_closure(_tree(build))

    def test_rust_binary_without_default_root_is_rejected(self) -> None:
        build = (
            'rust_binary(\n    name = "run",\n    srcs = ["app.rs", "helper.rs"],\n)\n'
        )

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_rust_bazel_source_closure(
                _tree(build, **{"app.rs": "mod helper;\n", "helper.rs": "\n"})
            )

        self.assertEqual(raised.exception.code, _AMBIGUITY)

    def test_non_literal_name_is_not_judged(self) -> None:
        build = (
            "rust_test(\n"
            "    name = TEST_NAME,\n"
            '    srcs = ["main.rs", "tests/litai_test.rs"],\n'
            ")\n"
        )

        validate_rust_bazel_source_closure(_tree(build))


if __name__ == "__main__":
    unittest.main()
