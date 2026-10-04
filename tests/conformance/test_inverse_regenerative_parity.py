"""Clean spec-to-source rebuilds with observable parity against original programs."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from literate_ai.source_to_specification import (
    LEGACY_QUALIFICATION_BLOCKER,
    ParityOutcome,
    RegenerationOutcome,
    RegenerationRunPlan,
    RegenerativeQualificationPolicy,
    canonical_digest,
    run_regenerative_qualification,
)

_SPECIFICATION = {
    "application": "portable-total",
    "input": "zero or more signed base-10 integers supplied as CLI arguments",
    "output": "one compact JSON object with integer count and sum fields",
    "errors": "invalid integers produce a nonzero exit status",
    "examples": [
        {"arguments": [], "stdout": '{"count":0,"sum":0}'},
        {"arguments": ["2", "-3", "8"], "stdout": '{"count":3,"sum":7}'},
    ],
    "invalid_examples": [{"arguments": ["not-an-integer"]}],
}


def _identity(label: object) -> str:
    return canonical_digest({"inverse-parity-fixture": label})


def _tool(language: str) -> str | None:
    if language == "python":
        return sys.executable
    if language == "javascript":
        return shutil.which("node") or shutil.which("nodejs")
    if language == "rust":
        return shutil.which("rustc")
    configured = os.environ.get("CXX")
    if configured:
        return shutil.which(configured) or configured
    return shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")


def _suffix(language: str) -> str:
    return {
        "python": ".py",
        "javascript": ".js",
        "cpp": ".cpp",
        "rust": ".rs",
    }[language]


def _legacy_source(language: str) -> str:
    sources = {
        "python": """import json, sys
values = tuple(map(int, sys.argv[1:]))
print(json.dumps({"count": len(values), "sum": sum(values)}, separators=(",", ":")))
""",
        "javascript": "\n".join(
            (
                "const values = process.argv.slice(2)"
                ".map((item) => Number.parseInt(item, 10));",
                "if (values.some((item) => !Number.isSafeInteger(item))) "
                "process.exit(2);",
                "const sum = values.reduce((left, right) => left + right, 0);",
                "process.stdout.write(JSON.stringify({count: values.length, sum}) "
                '+ "\\n");',
                "",
            )
        ),
        "cpp": """#include <iostream>
#include <string>
int main(int argc, char** argv) {
  long long result = 0;
  for (int index = 1; index < argc; ++index) result += std::stoll(argv[index]);
  std::cout << "{\\\"count\\\":" << argc - 1 << ",\\\"sum\\\":" << result << "}\\n";
}
""",
        "rust": """use std::env;
fn main() {
    let values: Vec<i64> = env::args().skip(1).map(|v| v.parse().unwrap()).collect();
    let total: i64 = values.iter().copied().sum();
    println!("{{\\\"count\\\":{},\\\"sum\\\":{}}}", values.len(), total);
}
""",
    }
    return sources[language]


def _render_specification(language: str, specification: dict[str, object]) -> str:
    if specification != _SPECIFICATION:
        raise ValueError("unknown specification")
    renderers = {
        "python": lambda: "\n".join(
            (
                "import json",
                "import sys",
                "",
                "def main(arguments):",
                "    numbers = [int(value, 10) for value in arguments]",
                "    payload = {'count': len(numbers), 'sum': sum(numbers)}",
                "    print(json.dumps(payload, separators=(',', ':')))",
                "",
                "if __name__ == '__main__':",
                "    main(sys.argv[1:])",
                "",
            )
        ),
        "javascript": lambda: "\n".join(
            (
                "const numbers = [];",
                "for (const value of process.argv.slice(2)) {",
                "  if (!/^-?\\d+$/.test(value)) process.exit(2);",
                "  numbers.push(Number(value));",
                "}",
                "const sum = numbers.reduce((total, value) => total + value, 0);",
                "console.log(JSON.stringify({count: numbers.length, sum}));",
                "",
            )
        ),
        "cpp": lambda: "\n".join(
            (
                "#include <iostream>",
                "#include <numeric>",
                "#include <string>",
                "#include <vector>",
                "int main(int argc, char** argv) {",
                "  std::vector<long long> numbers;",
                "  for (int i = 1; i < argc; ++i) "
                "numbers.push_back(std::stoll(argv[i]));",
                "  const auto sum = "
                "std::accumulate(numbers.begin(), numbers.end(), 0LL);",
                '  std::cout << "{\\"count\\":" << numbers.size()',
                '            << ",\\"sum\\":" << sum << "}\\n";',
                "}",
                "",
            )
        ),
        "rust": lambda: "\n".join(
            (
                "use std::env;",
                "fn main() {",
                "    let mut count: usize = 0;",
                "    let mut sum: i64 = 0;",
                "    for value in env::args().skip(1) {",
                "        count += 1;",
                '        sum += value.parse::<i64>().expect("signed integer");',
                "    }",
                '    println!("{{\\"count\\":{count},\\"sum\\":{sum}}}");',
                "}",
                "",
            )
        ),
    }
    return renderers[language]()


def _compile(language: str, tool: str, source: Path, output: Path) -> tuple[str, ...]:
    if language == "python":
        command = (tool, "-m", "py_compile", str(source))
    elif language == "javascript":
        command = (tool, "--check", str(source))
    elif language == "rust":
        command = (tool, "--edition=2021", "-O", str(source), "-o", str(output))
    elif Path(tool).name.lower() in {"cl", "cl.exe"}:
        command = (tool, "/nologo", "/EHsc", "/std:c++17", str(source), f"/Fe:{output}")
    else:
        command = (tool, "-std=c++17", "-O2", str(source), "-o", str(output))
    completed = subprocess.run(
        command,
        cwd=source.parent,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"{language} compilation failed: "
            + completed.stderr.decode("utf-8", errors="replace")
        )
    return command


def _runtime_command(language: str, tool: str, source: Path, output: Path):
    if language in {"python", "javascript"}:
        return (tool, str(source))
    return (str(output),)


def _execute(command: tuple[str, ...], arguments: list[str]) -> tuple[int, str]:
    completed = subprocess.run(
        (*command, *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return completed.returncode, completed.stdout.decode("utf-8").strip()


def _verification_cases(specification: dict[str, object]):
    return (
        *(
            {
                "arguments": item["arguments"],
                "expected_stdout": item["stdout"],
                "expected_success": True,
            }
            for item in specification["examples"]
        ),
        *(
            {
                "arguments": item["arguments"],
                "expected_stdout": "",
                "expected_success": False,
            }
            for item in specification["invalid_examples"]
        ),
    )


@dataclass
class SpecDrivenRegenerator:
    language: str
    tool: str
    specification: dict[str, object]

    @property
    def provider_identity(self) -> str:
        return _identity(f"spec-renderer:{self.language}@1")

    def regenerate(self, request, workspace):
        source = workspace / f"application{_suffix(self.language)}"
        executable = workspace / (
            "application.exe" if os.name == "nt" else "application"
        )
        generated = _render_specification(self.language, self.specification)
        source.write_text(generated, encoding="utf-8", newline="\n")
        command = _compile(self.language, self.tool, source, executable)
        succeeded = 0
        results = []
        runtime = _runtime_command(self.language, self.tool, source, executable)
        for example in _verification_cases(self.specification):
            code, stdout = _execute(runtime, example["arguments"])
            passed = (code == 0) is example["expected_success"] and stdout == example[
                "expected_stdout"
            ]
            succeeded += int(passed)
            results.append({"code": code, "stdout": stdout, "passed": passed})
        return RegenerationOutcome(
            generation_input_ids=request.declared_generation_inputs,
            generated_tree_id=canonical_digest(
                {"language": self.language, "source": generated}
            ),
            build_result_id=canonical_digest(
                {"command": command, "language": self.language}
            ),
            generated_test_result_id=canonical_digest(results),
            generated_source_cache_hit=False,
            build_passed=True,
            generated_tests_total=len(results),
            generated_tests_succeeded=succeeded,
            generated_tests_failed=len(results) - succeeded,
            generated_tests_skipped=0,
        )


@dataclass
class BinaryParityVerifier:
    language: str
    tool: str
    expected: tuple[tuple[int, str], ...]

    @property
    def provider_identity(self) -> str:
        return _identity(f"independent-runtime-parity:{self.language}@1")

    def verify(self, request, generated_workspace):
        source = generated_workspace / f"application{_suffix(self.language)}"
        executable = generated_workspace / (
            "application.exe" if os.name == "nt" else "application"
        )
        runtime = _runtime_command(self.language, self.tool, source, executable)
        actual = tuple(
            _execute(runtime, example["arguments"])
            for example in _verification_cases(_SPECIFICATION)
        )
        return ParityOutcome(
            canonical_digest(
                {
                    "request": request.to_dict(),
                    "expected": self.expected,
                    "actual": actual,
                }
            ),
            actual == self.expected,
            request.covered_surface_ids,
        )


@dataclass
class FixtureAttestor:
    provider_identity: str

    def attest(self, payload):
        return canonical_digest(payload)

    def verify(self, payload, attestation_id):
        return canonical_digest(payload) == attestation_id


class InverseRegenerativeParityTests(unittest.TestCase):
    def _qualify_language(self, language: str) -> None:
        tool = _tool(language)
        if tool is None:
            self.skipTest(f"{language} toolchain is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            baseline = Path(temporary) / "baseline"
            baseline.mkdir()
            source = baseline / f"original{_suffix(language)}"
            executable = baseline / ("original.exe" if os.name == "nt" else "original")
            original = _legacy_source(language)
            source.write_text(original, encoding="utf-8", newline="\n")
            _compile(language, tool, source, executable)
            runtime = _runtime_command(language, tool, source, executable)
            expected = tuple(
                _execute(runtime, example["arguments"])
                for example in _verification_cases(_SPECIFICATION)
            )
            self.assertTrue(all(code == 0 for code, _stdout in expected[:-1]))
            self.assertNotEqual(expected[-1][0], 0)
            generated = _render_specification(language, _SPECIFICATION)
            self.assertNotEqual(canonical_digest(original), canonical_digest(generated))

            source_snapshot = canonical_digest(
                {"language": language, "original_source": original}
            )
            specification_set = canonical_digest(_SPECIFICATION)
            target = _identity(f"target:{sys.platform}:{language}")
            surfaces = ("cli-input", "json-output", "invalid-input")
            policy = RegenerativeQualificationPolicy(
                "clean-binary-parity@1", 2, (target,), surfaces
            )
            plans = tuple(
                RegenerationRunPlan(
                    run_id=_identity(f"run:{language}:{number}"),
                    specification_set_id=specification_set,
                    target_profile_id=target,
                    flavor_lock_id=_identity(f"flavor:{language}"),
                    generation_recipe_id=_identity(f"recipe:{language}"),
                    covered_surface_ids=surfaces,
                )
                for number in range(2)
            )
            result = run_regenerative_qualification(
                source_snapshot_id=source_snapshot,
                specification_set_id=specification_set,
                policy=policy,
                plans=plans,
                regenerator=SpecDrivenRegenerator(language, tool, _SPECIFICATION),
                parity_verifier=BinaryParityVerifier(language, tool, expected),
                attestor=FixtureAttestor(_identity(f"attestor:{language}@1")),
                scratch_root=Path(temporary),
            )
        self.assertFalse(result.decision.qualified)
        self.assertIn(LEGACY_QUALIFICATION_BLOCKER, result.decision.blockers)
        self.assertEqual(len(result.evidence), 2)

    def test_python_clean_regenerative_parity(self):
        self._qualify_language("python")

    def test_cpp_clean_regenerative_binary_parity(self):
        self._qualify_language("cpp")


if __name__ == "__main__":
    unittest.main()
