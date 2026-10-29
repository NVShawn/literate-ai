"""Rejected generated candidates carry a bounded, redacted diagnostic excerpt.

CANDIDATE-DIAG-001: build rejections used to keep only a diagnostic identity, so
neither retained evidence nor the next attempt's repair feedback showed the
compiler error.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.builders import BuildError
from literate_ai.application import GenerationStatus
from literate_ai.application.orchestrator import GenerationFailure
from literate_ai.generated_tests import GENERATED_TEST_SUITE_PATH
from tests.conformance.support import sample_runner
from tests.conformance.support.sample_runner import (
    _candidate_diagnostic_excerpt,
    _generated_candidate_rejection,
    _GeneratedBehaviorMismatch,
    _SpecificationCompilerModel,
)

_SECRET = "sk-candidate-diag-secret-0123456789"
_BROKEN_SOURCE = "int main() { return undeclared_candidate_symbol; }\n"


def _compiler() -> str | None:
    for name in ("c++", "clang++", "g++"):
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def _failed_build_run(source_files: dict[str, str], *, code: str):
    present = {"validate", "classify", "authorize-build"}
    return SimpleNamespace(
        run_id="generation:candidate-diag",
        status=GenerationStatus.FAILED,
        events=[
            SimpleNamespace(
                event_type="lifecycle-step-failed",
                stage_id="build",
                data={"error_code": code},
            ),
            SimpleNamespace(
                event_type="run-failed",
                stage_id=None,
                data={"code": "generation.lifecycle-step-failed"},
            ),
        ],
        step=lambda step_id: object() if step_id in present else None,
        stage=lambda stage_id: (
            SimpleNamespace(
                output_identity=SimpleNamespace(uri="sha256:" + "a" * 64),
                response={"files": source_files},
            )
            if stage_id == "generate"
            else None
        ),
    )


class CandidateDiagnosticExcerptTests(unittest.TestCase):
    def test_excerpt_is_bounded_and_redacts_secrets_and_host_paths(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": _SECRET}):
            excerpt = _candidate_diagnostic_excerpt(
                "/tmp/literate-ai-conformance-x/sample/source/main.cpp:12:3: "
                f"error: token {_SECRET}\n"
                "C:\\Users\\builder\\src\\main.cpp(4): error C2065\n"
                "see https://example.com/a/b and src/relative.cpp:1\n"
            )
        self.assertNotIn(_SECRET, excerpt)
        self.assertNotIn("/tmp/", excerpt)
        self.assertNotIn("C:\\Users", excerpt)
        self.assertIn("<host-path>/main.cpp:12:3: error:", excerpt)
        self.assertIn("<host-path>/main.cpp(4): error C2065", excerpt)
        self.assertIn("https://example.com/a/b", excerpt)
        self.assertIn("src/relative.cpp:1", excerpt)
        tail = _candidate_diagnostic_excerpt("é" * 5000)
        self.assertLessEqual(len(tail.encode("utf-8")), 4096)
        self.assertTrue(tail)

    def test_directories_under_an_exact_temp_root_collapse(self) -> None:
        """Linux's temp root is `/tmp`; its replacement must not keep the rest."""

        with mock.patch.object(
            sample_runner.tempfile, "gettempdir", return_value="/tmp"
        ):
            excerpt = _candidate_diagnostic_excerpt(
                "/tmp/literate-ai-conformance-x/sample/source/main.cpp:12:3: error\n"
                "C:\\tmp\\run\\main.cpp(4): error\n"
            )
        self.assertNotIn("literate-ai-conformance-x", excerpt)
        self.assertIn("<host-path>/main.cpp:12:3: error", excerpt)
        self.assertIn("<host-path>/main.cpp(4): error", excerpt)

    def test_excerpt_redacts_flag_unc_file_url_home_and_cut_head(self) -> None:
        home = str(Path.home())
        excerpt = _candidate_diagnostic_excerpt(
            f"{home}/proj with space/main.cpp:3: error\n"
            "cc -I/opt/include/foo -L/usr/lib/x86_64 x.c\n"
            "cl /Fo:C:\\tmp\\x\\a.obj main.cpp\n"
            "\\\\server\\share\\dir\\main.cpp(3): error\n"
            "see file:///home/u/proj/a.cpp\n"
        )
        self.assertNotIn(home, excerpt)
        self.assertIn("<home>/proj with space/main.cpp:3: error", excerpt)
        self.assertIn("-I<host-path>/foo -L<host-path>/x86_64", excerpt)
        self.assertIn("/Fo:<host-path>/a.obj", excerpt)
        self.assertIn("<host-path>/main.cpp(3): error", excerpt)
        self.assertIn("file://<host-path>", excerpt)
        for leaked in ("/opt/include", "C:\\tmp", "\\\\server", "/home/u"):
            self.assertNotIn(leaked, excerpt)
        # A producer that kept only its tail may have cut a path mid-way.
        cut = _candidate_diagnostic_excerpt(
            "s/builder/.cache/bazel/fragment/BUILD:3\nERROR: kept line", cut_head=True
        )
        self.assertEqual(cut, "ERROR: kept line")

    def test_generated_behavior_mismatch_shows_both_generated_values(self) -> None:
        mismatch = _GeneratedBehaviorMismatch(
            case_id="fractional-cpu-floor",
            expected_result={"cpu_quota": 1},
            observed_result={"cpu_quota": 0},
        )
        excerpt = json.loads(mismatch.case_evidence["diagnostic_excerpt"])
        self.assertEqual(
            excerpt, {"expected": {"cpu_quota": 1}, "observed": {"cpu_quota": 0}}
        )

    def test_rust_js_type_error_feedback_keeps_source_identity_and_redaction(
        self,
    ) -> None:
        # E0308 is the retained release incident; the operator qualification also
        # reproduces it with rustc. Keep this unit contract compiler-independent.
        broken = (
            "fn main() { let _: Option<u16> = Some(1u16).and_then("
            "|_| { return Err(()); }); }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            detail = (
                "error[E0308]: mismatched types\n"
                f" --> {directory}/main.rs:147:33\n"
                "expected Option<_>, found Result<_, String>\n"
            )
            code = "builder.rust_javascript_compile_failed"
            failure = GenerationFailure(
                "generation.lifecycle-step-failed",
                "generated Rust rejected",
                _failed_build_run(
                    {"backend/main.rs": broken, GENERATED_TEST_SUITE_PATH: "{}"},
                    code=code,
                ),
            )
            failure.__cause__ = BuildError(
                code, "Rust backend compilation failed: " + detail
            )
            rejection = _generated_candidate_rejection(failure, attempt=1)
        self.assertIsNotNone(rejection)
        self.assertEqual(rejection["build_failure_code"], code)
        self.assertEqual(rejection["rejection_kind"], "generated-source-build-rejected")
        self.assertIn("E0308", rejection["diagnostic_excerpt"])
        self.assertNotIn(directory, rejection["diagnostic_excerpt"])
        self.assertTrue(rejection["candidate_tree_identity"].startswith("sha256:"))
        self.assertTrue(rejection["source_bundle_digest"].startswith("sha256:"))

    def test_real_compiler_error_reaches_record_and_repair_feedback(self) -> None:
        compiler = _compiler()
        if compiler is None:
            self.skipTest("no C++ compiler on PATH")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source" / "main.cpp"
            source.parent.mkdir()
            source.write_text(_BROKEN_SOURCE, encoding="utf-8")
            completed = subprocess.run(
                (compiler, "-c", str(source), "-o", os.devnull),
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(completed.returncode, 0)
        detail = (completed.stdout + completed.stderr)[-4000:]
        self.assertIn("undeclared_candidate_symbol", detail)
        code = "builder.cpp_generated_source_rejected"
        failure = GenerationFailure(
            "generation.lifecycle-step-failed",
            "generated candidate failed to build",
            _failed_build_run(
                {"source/main.cpp": _BROKEN_SOURCE, GENERATED_TEST_SUITE_PATH: "{}"},
                code=code,
            ),
        )
        failure.__cause__ = BuildError(
            code, "C++ compiler rejected the generated source: " + detail
        )

        rejection = _generated_candidate_rejection(failure, attempt=1)

        assert rejection is not None
        self.assertEqual(rejection["rejection_kind"], "generated-source-build-rejected")
        self.assertIn("undeclared_candidate_symbol", rejection["diagnostic_excerpt"])
        self.assertNotIn(directory, rejection["diagnostic_excerpt"])
        envelope = sample_runner._candidate_rejection_envelope("cpp", [rejection])
        self.assertIn(
            "undeclared_candidate_symbol", json.dumps(envelope, sort_keys=True)
        )

        endpoint = sample_runner._SAMPLE_PLANNER_ENDPOINT_ID
        model = _SpecificationCompilerModel(
            recipe=SimpleNamespace(
                identity="sha256:recipe", all_required_entrypoints=()
            ),
            source_generator=SimpleNamespace(),
            generation_root=Path(tempfile.gettempdir()),
            specification=SimpleNamespace(identity=SimpleNamespace(uri="spec:1")),
            flavor_specification_ids=(),
            skill_ids=(),
            execution_contract_id="contract:1",
            requirement_id="requirement:1",
            scenario_id="scenario:1",
            execution_plan=SimpleNamespace(
                model_stages=(SimpleNamespace(stage_id="plan"),),
                route_decisions=(SimpleNamespace(selected_endpoint_id=endpoint),),
            ),
            candidate_feedback=[rejection],
        )
        plan = model.complete_structured(
            {
                "stage_id": "plan",
                "source_snapshot_ids": [],
                "evidence_ids": ["spec:1", "contract:1"],
                "instructions": "recipe sha256:recipe",
                "route_decision": {"selected_endpoint_id": endpoint},
            }
        )
        (feedback,) = plan["previous_candidate_rejections"]
        self.assertIn("undeclared_candidate_symbol", feedback["diagnostic_excerpt"])


if __name__ == "__main__":
    unittest.main()
