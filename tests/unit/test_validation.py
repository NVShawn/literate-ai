"""Validation cannot confuse declared evidence IDs with supported API use."""

from __future__ import annotations

import unittest

from literate_ai.validation import (
    ApiUsageValidator,
    CppPortableLifetimeValidator,
    CppSourceValidator,
    CppTranslationUnitIncludeValidator,
    JavaScriptSourceValidator,
    PythonSyntaxValidator,
    RustSourceValidator,
    SourceContract,
    ValidationPipeline,
)


class ValidationTests(unittest.TestCase):
    def test_cuda_translation_units_cross_the_cpp_validation_boundary(self) -> None:
        valid = ValidationPipeline(
            (CppSourceValidator(),), required_categories=("syntax",)
        ).validate({"source/kernel.cu": b"__global__ void kernel() {}\n"})
        self.assertTrue(valid.passed)
        invalid = ValidationPipeline(
            (CppSourceValidator(),), required_categories=("syntax",)
        ).validate({"source/kernel.cu": b"bad\x00source"})
        self.assertFalse(invalid.passed)

    def setUp(self) -> None:
        self.contract = SourceContract(
            contract_id="contract:math",
            component_revision_digest="sha256:" + "a" * 64,
            modules=("fastmath",),
            symbols=("fastmath.supported",),
            evidence_ids=("evidence:1",),
        )

    def test_unsupported_call_fails_even_when_contract_has_evidence(self) -> None:
        pipeline = ValidationPipeline(
            (PythonSyntaxValidator(), ApiUsageValidator((self.contract,))),
            required_categories=("syntax", "source-contract"),
        )
        report = pipeline.validate(
            {"app.py": b"import fastmath\nfastmath.unsupported()\n"}
        )
        self.assertFalse(report.passed)
        self.assertEqual(
            report.findings[0].code, "validation.api_not_in_source_contract"
        )
        self.assertEqual(report.findings[0].evidence_ids, ("evidence:1",))

    def test_supported_from_import_passes(self) -> None:
        pipeline = ValidationPipeline(
            (PythonSyntaxValidator(), ApiUsageValidator((self.contract,))),
            required_categories=("syntax", "source-contract"),
        )
        report = pipeline.validate(
            {"app.py": b"from fastmath import supported\nsupported()\n"}
        )
        self.assertTrue(report.passed)

    def test_syntax_only_cannot_satisfy_application_acceptance_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "lacks required categories"):
            ValidationPipeline(
                (PythonSyntaxValidator(),),
                required_categories=("syntax", "source-contract"),
            )

    def test_rust_and_javascript_sources_cross_an_inert_utf8_gate(self) -> None:
        for validator, valid_path in (
            (RustSourceValidator(), "source/main.rs"),
            (JavaScriptSourceValidator(), "source/main.js"),
        ):
            with self.subTest(validator=validator.validator_id):
                valid = ValidationPipeline(
                    (validator,), required_categories=("syntax",)
                ).validate({valid_path: b"fn main() {}\n"})
                self.assertTrue(valid.passed)
                invalid = ValidationPipeline(
                    (validator,), required_categories=("syntax",)
                ).validate({valid_path: b"bad\x00source"})
                self.assertFalse(invalid.passed)
                self.assertIn("nul_byte", invalid.findings[0].code)

                missing = ValidationPipeline(
                    (validator,), required_categories=("syntax",)
                ).validate({"README.md": b"not source"})
                self.assertFalse(missing.passed)
                self.assertIn("source_missing", missing.findings[0].code)

    def test_cpp_portable_lifetime_gate_rejects_retained_input_references(self) -> None:
        source = b"""\
#include <string>

class JsonReader {
 public:
  explicit JsonReader(const std::string& input) : input_(input) {}

 private:
  const std::string& input_;

  bool escaped(char character) const { return character == '\\\\'; }
};

int main(int argc, char* argv[]) {
  JsonReader reader(argv[1]);
  return argc;
}
"""
        report = ValidationPipeline(
            (CppPortableLifetimeValidator(),),
            required_categories=("correctness",),
        ).validate({"source/main.cpp": source})

        self.assertFalse(report.passed)
        self.assertEqual(len(report.findings), 1)
        self.assertEqual(
            report.findings[0].code,
            "validation.cpp_nonowning_text_member",
        )
        self.assertEqual(report.findings[0].line, 8)

    def test_cpp_portable_lifetime_gate_accepts_owned_text_and_local_references(
        self,
    ) -> None:
        source = b"""\
#include <string>
#include <string_view>

class JsonReader {
 public:
  explicit JsonReader(std::string input) : input_(input) {}
  void parse(const std::string& request) {
    const std::string& local = request;
    (void)local;
  }

 private:
  std::string input_;
};

// class NotCode { const std::string& input_; };
const char* text = "class AlsoNotCode { std::string_view input_; };";
"""
        report = ValidationPipeline(
            (CppPortableLifetimeValidator(),),
            required_categories=("correctness",),
        ).validate({"source/main.cpp": source})

        self.assertTrue(report.passed)

    def test_cpp_portable_lifetime_gate_rejects_string_view_members(self) -> None:
        source = b"""\
#include <string_view>
struct JsonReader {
  std::string_view input_;
};
"""
        report = CppPortableLifetimeValidator().validate({"source/main.cpp": source})

        self.assertEqual(len(report), 1)
        self.assertEqual(report[0].line, 3)

    def test_cpp_translation_unit_include_gate_rejects_implementation_files(
        self,
    ) -> None:
        source = b"""\
#include "worker.cpp"
# /* direct token comment */ include /* target comment */ <generated/api.CXX>
#include "portable.hpp"
"""

        report = CppTranslationUnitIncludeValidator().validate(
            {"source/main.cpp": source}
        )

        self.assertEqual(len(report), 2)
        self.assertEqual(
            {finding.code for finding in report},
            {"validation.cpp_translation_unit_included"},
        )
        self.assertEqual([finding.line for finding in report], [1, 2])

    def test_cpp_translation_unit_include_gate_ignores_inert_text_and_headers(
        self,
    ) -> None:
        source = b"""\
#include "worker.hpp"
// #include "commented.cpp"
/* #include <also-commented.cc> */
const char* text = "#include \\\"string.cxx\\\"";
const char* raw = R"(#include <raw.cpp>)";
"""

        report = ValidationPipeline(
            (CppTranslationUnitIncludeValidator(),),
            required_categories=("correctness",),
        ).validate({"source/main.cpp": source})

        self.assertTrue(report.passed)

    def test_cpp_translation_unit_include_gate_rejects_missing_companion_header(
        self,
    ) -> None:
        validator = CppTranslationUnitIncludeValidator()

        missing = validator.validate(
            {"source/tests/litai_test.cpp": b'#include "litai_test.hpp"\n'}
        )
        present = validator.validate(
            {
                "source/tests/litai_test.cpp": b'#include "litai_test.hpp"\n',
                "source/include/litai_test.hpp": b"#pragma once\n",
            }
        )

        self.assertEqual(
            [finding.code for finding in missing],
            ["validation.cpp_companion_header_missing"],
        )
        self.assertEqual(missing[0].path, "source/tests/litai_test.cpp")
        self.assertEqual(missing[0].line, 1)
        self.assertEqual(present, ())


if __name__ == "__main__":
    unittest.main()
