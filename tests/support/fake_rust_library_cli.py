"""External model boundary for a real public Rust library lifecycle journey.

No framework imports or authority/results are replaced. The ordinary generator
request supplies its exact recipe, allowed specification references and source
SBOM; this stand-in emits the source a coding CLI would produce.
"""

import json
import re
import sys
from pathlib import Path

LIBRARY = """#[cfg(feature="checked")]
pub fn answer(input: &str) -> String {
    format!(r#"{{"answer":42,"echo":{}}}"#, input)
}
"""


def generate(workspace):
    request = (workspace / ".literate-ai-generation-request.md").read_text("utf-8")
    recipe = re.search(r"Recipe identity: `([^`]+)`", request).group(1)
    marker = request.index(
        '"$schema": "http://cyclonedx.org/schema/bom-1.7.schema.json"'
    )
    bom, _ = json.JSONDecoder().raw_decode(request, request.rindex("{", 0, marker))
    lines = request.splitlines()
    start = next(i for i, line in enumerate(lines) if "chosen only from:" in line)
    references = []
    for line in lines[start + 1 :]:
        match = re.match(r"\s*- `([^`]+)`", line)
        if match:
            references.append(match.group(1))
        elif references:
            break
    if not references:
        raise ValueError("actual generation request specification references required")
    cases = []
    assertions = []
    for category, payload in (
        ("example", {"label": "fixture", "values": [1, True, None]}),
        ("boundary", {}),
        ("invariant", {"nested": {"values": [2, 3]}, "text": "<&>"}),
    ):
        expected = {"answer": 42, "echo": payload}
        cases.append(
            {
                "case_id": category + "-answer",
                "category": category,
                "specification_refs": references[:1],
                "arguments": [payload],
                "expected_result": expected,
            }
        )
        encoded = json.dumps(payload, separators=(",", ":"))
        result = '{"answer":42,"echo":' + encoded + "}"
        # serde_json is intentionally NOT a candidate dependency. The native
        # attributable driver tests the std-only library; the independent
        # verifier separately consumes the real reviewed serde_json closure.
        assertions.append(
            "assert_eq!(qualified_fixture::answer("
            + json.dumps(encoded)
            + "), "
            + json.dumps(result)
            + ");"
        )
    source = workspace / "source"
    (source / "tests").mkdir(parents=True)
    (source / ".literate").mkdir()
    (source / "Cargo.toml").write_text(
        '[package]\nname="qualified-fixture"\nversion="1.0.0"\nedition="2021"\n'
        '[lib]\nname="qualified_fixture"\npath="lib.rs"\n'
        '[[bin]]\nname="litai_artifact"\npath="main.rs"\n'
        '[features]\ndefault=["checked"]\nchecked=[]\n',
        encoding="utf-8",
    )
    (source / "lib.rs").write_text(LIBRARY, encoding="utf-8")
    passed = json.dumps(
        {
            "schema": "literate-ai/generated-test-results@1",
            "cases": [
                {"case_id": case["case_id"], "outcome": "passed"} for case in cases
            ],
        },
        separators=(",", ":"),
    )
    driver = "fn main() {\n" + "\n".join(assertions)
    driver += '\nprintln!("{}", ' + json.dumps(passed) + ");\n}\n"
    (source / "main.rs").write_text(driver, encoding="utf-8")
    native_test = (
        "#[test] fn actual_library_json_contract() {\n"
        + "\n".join(assertions)
        + "\n}\n"
    )
    (source / "tests/litai_test.rs").write_text(native_test, encoding="utf-8")
    (source / "tests/manifest.json").write_text(
        json.dumps(
            {
                "schema": "urn:literate-ai:schema:v1:generated-test-suite",
                "recipe_identity": recipe,
                "generation_mode": "major-rebuild",
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )
    (source / ".literate/sbom.cdx.json").write_text(json.dumps(bom), encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] == ["--version"]:
        print("claude 2.0.0 (external Rust source fixture)")
    else:
        generate(Path.cwd())
        print("done")
