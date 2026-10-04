"""Validation cannot confuse declared evidence IDs with supported API use."""

from __future__ import annotations

import unittest

from literate_ai.validation import (
    ApiUsageValidator,
    CppPortableLifetimeValidator,
    PythonSyntaxValidator,
    SourceContract,
    ValidationPipeline,
)


class ValidationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
