from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path

from literate_ai.adapters.models import CodingCliSemanticComparator
from literate_ai.cli import main
from literate_ai.source_to_specification import (
    DraftScenario,
    DraftStatement,
    NormalizedRequirementGraph,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPONENT = REPO_ROOT / "samples" / "regenerative-roundtrip"
EXECUTION = (
    REPO_ROOT
    / "samples"
    / "_harness"
    / "regenerative-roundtrip"
    / "acceptance"
    / "execution.json"
)
ORACLE = (
    REPO_ROOT
    / "samples"
    / "_harness"
    / "regenerative-roundtrip"
    / "acceptance"
    / "oracle.json"
)
REQUIREMENTS = (
    REPO_ROOT
    / "samples"
    / "_harness"
    / "regenerative-roundtrip"
    / "acceptance"
    / "requirements.json"
)
LANGUAGES = ("python", "cpp", "rust", "javascript", "elixir")


def selected_languages() -> tuple[str, ...]:
    configured = os.environ.get("LITERATE_AI_LIVE_ROUNDTRIP_LANGUAGES")
    if configured is None:
        return LANGUAGES
    selected = tuple(item.strip() for item in configured.split(",") if item.strip())
    if (
        not selected
        or len(selected) != len(set(selected))
        or any(item not in LANGUAGES for item in selected)
    ):
        raise ValueError(
            "LITERATE_AI_LIVE_ROUNDTRIP_LANGUAGES must be a unique comma-separated "
            "subset of " + ", ".join(LANGUAGES)
        )
    return selected


def invoke_with_status(expected_status: int, *arguments: str) -> dict[str, object]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    status = main(arguments, stdout=stdout, stderr=stderr)
    if status != expected_status:
        raise AssertionError(
            f"litai {' '.join(arguments[:3])} failed ({status}): {stderr.getvalue()}"
        )
    return json.loads(stdout.getvalue())["result"]


def invoke(*arguments: str) -> dict[str, object]:
    return invoke_with_status(0, *arguments)


def run(command: list[str], *, cwd: Path, timeout: int = 1_800) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {command!r}\n"
            f"{completed.stderr[-4000:]}"
        )
    return completed.stdout


def host_flavor() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    if system == "windows":
        return "windows"
    raise unittest.SkipTest(f"unsupported live round-trip host: {system}")


def locked_sample_copy(
    root: Path, language: str, *, component: Path = COMPONENT
) -> Path:
    """Create disposable lock authority without writing generated files to samples/."""

    project = root / "forward-authority"
    if not project.exists():
        project.mkdir()
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        for encoded in tracked:
            if not encoded:
                continue
            relative = Path(encoded.decode("utf-8"))
            source = REPO_ROOT / relative
            if not source.is_file():
                continue
            destination = project / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    component = project / component.relative_to(REPO_ROOT)
    invoke(
        "lock",
        str(component),
        "--target",
        "host",
        "--flavor",
        f"+{host_flavor()}",
        "--flavor",
        f"+{language}",
    )
    invoke("project", "source-intelligence", "sync", str(project))
    return component


def draft_statements(
    bundle: dict[str, object], *, language: str
) -> tuple[DraftStatement, ...]:
    base = bundle["result"]["draft"]["statements"]
    selected = tuple(
        item
        for item in bundle["flavor_drafts"]
        if item.get("flavor_id") == language
        and item.get("axis") == "implementation.language-ecosystem"
    )
    if len(selected) != 1:
        raise AssertionError(
            f"inverse draft must contain one selected {language} language Flavor"
        )
    raw = [*base, *selected[0]["statements"]]
    return tuple(
        DraftStatement(
            item["statement_id"],
            item["capability"],
            item["requirement"],
            tuple(DraftScenario(**scenario) for scenario in item["scenarios"]),
            tuple(item["observation_ids"]),
        )
        for item in raw
    )


@unittest.skipUnless(
    os.environ.get("LITERATE_AI_RUN_LIVE_ROUNDTRIP") == "1",
    "set LITERATE_AI_RUN_LIVE_ROUNDTRIP=1 for live coding-agent round-trip",
)
class LiveBidirectionalRoundTripTests(unittest.TestCase):
    def qualify_language(
        self,
        *,
        language: str,
        bundle: dict[str, object],
        source: Path,
        source_build_root: Path,
        source_symlinks: Path,
        root: Path,
        key: Path,
        bazel: str,
        execution: dict[str, object],
        expected: dict[str, object],
    ) -> dict[str, object]:
        bundle_path = root / f"{language}-bundle.json"
        bundle_path.write_text(
            json.dumps(
                {
                    "schema": "literate-ai/cli-result@1",
                    "ok": True,
                    "command": "spec.derive",
                    "result": bundle,
                }
            )
        )
        result = bundle["result"]
        review_arguments = [
            "spec",
            "review",
            str(bundle_path),
            "--actor",
            "live-roundtrip",
            "--key",
            str(key),
        ]
        for uncertainty in result["uncertainty"]["items"]:
            review_arguments.extend(("--resolve", uncertainty["uncertainty_id"]))
        review_path = root / f"{language}-review.json"
        review_path.write_text(
            json.dumps(
                {
                    "schema": "literate-ai/cli-result@1",
                    "ok": True,
                    "command": "spec.review",
                    "result": invoke(*review_arguments),
                }
            )
        )
        profile_path = root / f"qualification-profile-{language}.json"
        profile_path.write_text(
            json.dumps(
                {
                    "schema": (
                        "urn:literate-ai:schema:v2:"
                        "local-regenerative-qualification-profile"
                    ),
                    "profile_id": f"regenerative-roundtrip-{language}@1",
                    "build_command": [
                        bazel,
                        "--output_base={build_root}",
                        "build",
                        "--symlink_prefix={build_root}/links/",
                        "//:run",
                    ],
                    "test_commands": [
                        [
                            bazel,
                            "--output_base={build_root}",
                            "run",
                            "--symlink_prefix={build_root}/links/",
                            "//:run",
                            "--",
                            "--litai-test",
                        ]
                    ],
                    "source_command": [
                        bazel,
                        f"--output_base={source_build_root}",
                        "run",
                        f"--symlink_prefix={source_symlinks}/",
                        "//:run",
                        "--",
                    ],
                    "generated_command": [
                        bazel,
                        "--output_base={build_root}",
                        "run",
                        "--symlink_prefix={build_root}/links/",
                        "//:run",
                        "--",
                    ],
                    "cases": [
                        {
                            "case_id": item["case_id"],
                            "arguments": item["arguments"],
                            "expected_result": expected[item["case_id"]],
                        }
                        for item in execution["invocations"]
                    ],
                    "covered_surface_ids": [
                        "manifest.validation",
                        "manifest.aggregation",
                        "manifest.totals",
                        "manifest.dominant-sku",
                        "manifest.identifier",
                        "manifest.portable-json-interface",
                    ],
                    "generated_root": "source",
                    "minimum_clean_runs": 2,
                    "timeout_seconds": 1800,
                    "maximum_output_bytes": 1048576,
                }
            )
        )
        accepted = root / f"accepted-{language}-spec"
        promoted = root / f"promoted-{language}-project"
        acceptance = invoke(
            "spec",
            "accept",
            str(source),
            str(bundle_path),
            "--review",
            str(review_path),
            "--target",
            str(accepted),
            "--project-target",
            str(promoted),
            "--qualification-profile",
            str(profile_path),
            "--trust-key",
            str(key),
        )
        component = promoted / acceptance["promoted_project"]["component"]
        qualification = invoke(
            "spec",
            "qualify",
            str(component),
            "--source",
            str(source),
            "--profile",
            str(component.parent / "qualification" / "profile.json"),
            "--output",
            str(root / f"qualification-{language}.json"),
            "--key",
            str(key),
            "--signer",
            "live-roundtrip",
            "--scratch-root",
            str(root),
            "--allow-host-execution",
        )
        self.assertTrue(qualification["qualified"])
        self.assertEqual(
            qualification["release_implementation_authority"], "specification"
        )
        self.assertEqual(qualification["blockers"], [])
        return qualification

    def test_selected_languages_reach_semantic_and_regenerative_qualification(
        self,
    ) -> None:
        bazel = shutil.which("bazel") or shutil.which("bazelisk")
        if bazel is None:
            self.skipTest("Bazel is required for the live round-trip")
        execution = json.loads(EXECUTION.read_text())
        requirement_graph = NormalizedRequirementGraph.from_dict(
            json.loads(REQUIREMENTS.read_text())
        )
        expected = {
            item["case_id"]: item["expected_result"]
            for item in json.loads(ORACLE.read_text())["oracle_results"]
        }
        retained_root = os.environ.get("LITERATE_AI_LIVE_ROUNDTRIP_ROOT")
        if retained_root:
            retention_directory = Path(retained_root).expanduser().resolve()
            retention_directory.mkdir(parents=True, exist_ok=True)
            workspace = tempfile.mkdtemp(
                prefix="literate-ai-live-roundtrip-",
                dir=retention_directory,
            )
            workspace_context = nullcontext(workspace)
        else:
            workspace_context = tempfile.TemporaryDirectory(
                prefix="literate-ai-live-roundtrip-"
            )
        with workspace_context as temporary:
            root = Path(temporary)
            key = root / "trust.key"
            key.write_bytes(b"live-roundtrip-local-trust-key-material")
            reports: dict[str, object] = {}
            for language in selected_languages():
                locked_component = locked_sample_copy(root, language)
                output = root / f"forward-{language}"
                invoke(
                    "generate",
                    str(locked_component),
                    "--output",
                    str(output),
                    "--flavor",
                    f"+{host_flavor()}",
                    "--flavor",
                    f"+{language}",
                )
                module_files = sorted(output.rglob("MODULE.bazel"))
                self.assertEqual(len(module_files), 1, (language, module_files))
                source = module_files[0].parent
                build_root = root / f"bazel-{language}"
                symlinks = root / f"bazel-links-{language}"
                common = [bazel, f"--output_base={build_root}"]
                generated_tests = json.loads(
                    run(
                        [
                            *common,
                            "run",
                            f"--symlink_prefix={symlinks}/",
                            "//:run",
                            "--",
                            "--litai-test",
                        ],
                        cwd=source,
                    )
                )
                self.assertEqual(
                    generated_tests["schema"],
                    "literate-ai/generated-test-results@1",
                )
                self.assertGreaterEqual(len(generated_tests["cases"]), 3)
                self.assertTrue(
                    all(
                        item["outcome"] == "passed" for item in generated_tests["cases"]
                    )
                )
                observed: dict[str, object] = {}
                for invocation in execution["invocations"]:
                    argument = json.dumps(
                        invocation["arguments"],
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    stdout = run(
                        [
                            *common,
                            "run",
                            f"--symlink_prefix={symlinks}/",
                            "//:run",
                            "--",
                            argument,
                        ],
                        cwd=source,
                    )
                    observed[invocation["case_id"]] = json.loads(stdout)
                self.assertEqual(observed, expected, language)

                attestation_path = root / f"attestation-{language}.json"
                attestation_path.write_text(
                    json.dumps(
                        {
                            "schema": "literate-ai/cli-result@1",
                            "ok": True,
                            "command": "spec.attest",
                            "result": invoke(
                                "spec",
                                "attest",
                                str(source),
                                "--signer",
                                "live-roundtrip",
                                "--machine",
                                platform.node() or "local-host",
                                "--key",
                                str(key),
                            ),
                        }
                    )
                )
                bundle = invoke(
                    "spec",
                    "derive",
                    str(source),
                    "--translator",
                    "coding-cli",
                    "--allow-model-egress",
                    "--attestation",
                    str(attestation_path),
                    "--trust-key",
                    str(key),
                )
                diagnostic_bundle = root / f"{language}-bundle.json"
                diagnostic_bundle.write_text(
                    json.dumps(
                        {
                            "schema": "literate-ai/cli-result@1",
                            "ok": True,
                            "command": "spec.derive",
                            "result": bundle,
                        }
                    )
                )
                draft = bundle["result"]["draft"]
                semantic_result = CodingCliSemanticComparator().compare(
                    graph=requirement_graph,
                    inverse_draft_identity=draft["draft_id"],
                    inverse_statements=draft_statements(bundle, language=language),
                )
                semantic_result.comparison.require_complete()
                reports[language] = {
                    "comparison": semantic_result.comparison.to_dict(),
                    "request_identity": semantic_result.task.request_identity,
                    "response_identity": semantic_result.task.response_identity,
                    "tool_binding_identity": semantic_result.task.tool_binding_identity,
                }
                reports[language]["qualification"] = self.qualify_language(
                    language=language,
                    bundle=bundle,
                    source=source,
                    source_build_root=build_root,
                    source_symlinks=symlinks,
                    root=root,
                    key=key,
                    bazel=bazel,
                    execution=execution,
                    expected=expected,
                )
            (root / "semantic-report.json").write_text(json.dumps(reports, indent=2))


if __name__ == "__main__":
    unittest.main()
