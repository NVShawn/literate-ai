"""Specification recipe and coding CLI adapter tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import literate_ai.adapters.models.coding_cli as coding_cli_adapter
from literate_ai.adapters.cache import (
    CachedCodingCliSourceGenerator,
    GeneratedSourceCacheError,
)
from literate_ai.adapters.dependencies import CycloneDxBomError, build_cyclonedx_bom
from literate_ai.adapters.lifecycle import local_generated_source_tree_identity
from literate_ai.adapters.models import (
    CodingCliError,
    CodingCliSourceGenerator,
    CodingCliTaskRunner,
    GenerationRecipe,
    RecipeDeploymentUnit,
    RecipeDocument,
    RecipeFlavor,
    RecipeLibraryDependency,
    RecipeSkill,
    apply_flavor_selectors,
    lifecycle_driver_environment,
    select_coding_cli,
)
from literate_ai.adapters.models.generated_source_validation import (
    GeneratedSourceValidationError,
    validate_cpp_bazel_rule_attributes,
    validate_make_language_tool_quoting,
    validate_rust_bazel_source_closure,
)
from literate_ai.contracts import (
    CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR,
    CYCLONEDX_SOURCE_SBOM_PATH,
    ContentIdentity,
    ContentReference,
    ContractValidationError,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedEdge,
    CycloneDxManagedGraph,
    DependencyKind,
    LibraryCapabilityImport,
    LibraryImportSurface,
    ManagedComponentKind,
    ModelScopeKind,
    SkillReference,
    SourceIntelligenceArtifact,
    canonical_identity,
    canonical_json_bytes,
    component_bom_ref,
    generated_source_snapshot_identity,
    generated_source_tree_identity,
    resolve_model_scope,
)
from literate_ai.diagnostics import debug_diagnostics
from literate_ai.evidence_ledger import open_run
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    GENERATED_TEST_SUITE_SCHEMA,
)

TEST_COMPONENT_LOCK_IDENTITY = ContentIdentity.parse_uri("sha256:" + "c" * 64)


def _is_opencode_compatibility_probe(command) -> bool:
    return tuple(command[1:]) == ("--pure", "run", "--help")


def _compatible_opencode_help_result() -> SimpleNamespace:
    return SimpleNamespace(
        returncode=0,
        stdout=(
            b"  --pure  run without external plugins\n"
            b"  --dir PATH  workspace\n"
            b"  --agent NAME  agent\n"
            b"  --format NAME  output format\n"
        ),
        stderr=b"",
    )


def _create_windows_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.decode(errors="replace"))


def planned_execution(value: GenerationRecipe):
    workflow = ContentReference(
        "workflow",
        "workflows/test.json",
        ContentIdentity.parse_uri("sha256:" + "1" * 64),
    )
    routing = ContentReference(
        "routing-policy",
        "routing/test.json",
        ContentIdentity.parse_uri("sha256:" + "2" * 64),
    )
    plan_identity = ContentIdentity.parse_uri("sha256:" + "3" * 64)
    stages = (
        SimpleNamespace(
            stage_id="plan",
            dependencies=(),
            content_kind="metadata",
            response_schema_name="implementation_plan",
            instructions="Plan every exact requirement.\n\n" + value.prompt(),
        ),
        SimpleNamespace(
            stage_id="generate",
            dependencies=("plan",),
            content_kind="source",
            response_schema_name="source_tree",
            instructions="Generate only from the approved plan.\n\n" + value.prompt(),
        ),
    )
    routes = (
        SimpleNamespace(digest="sha256:" + "4" * 64),
        SimpleNamespace(digest="sha256:" + "5" * 64),
    )
    return SimpleNamespace(
        identity=plan_identity,
        workflow_reference=workflow,
        routing_reference=routing,
        model_stages=stages,
        route_decisions=routes,
    )


def generation_skill(
    skill_id: str = "specification-planning",
    *,
    stages: tuple[str, ...] = ("plan",),
    dependencies: tuple[RecipeSkill | SkillReference, ...] = (),
    instructions: str = "Plan the exact specified behavior.",
) -> RecipeSkill:
    content = json.dumps(
        {
            "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
            "skill_id": skill_id,
            "version": "1.0.0",
            "title": skill_id.replace("-", " ").title(),
            "stages": list(stages),
            "dependencies": [
                (item.ref if isinstance(item, RecipeSkill) else item).to_dict()
                for item in dependencies
            ],
            "instructions": instructions,
            "limitations": ["Do not invent behavior."],
            "trust": "fixture-reviewed",
        },
        sort_keys=True,
    ).encode()
    identity = f"sha256:{hashlib.sha256(content).hexdigest()}"
    reference = ContentReference(
        "specification-to-source-skill",
        f"skills/{skill_id}.json",
        ContentIdentity.parse_uri(identity),
    )
    return RecipeSkill.from_reference(reference, content, source="test fixture")


def flavor(value: str, *, models=(), skills=()) -> RecipeFlavor:
    revision = canonical_identity({"fixture_flavor": value}).uri
    specification_set = canonical_identity({"fixture_flavor_spec": value}).uri
    return RecipeFlavor(
        f"implementation-{value}",
        "implementation.language-ecosystem",
        value,
        (RecipeDocument.create(f"{value}/spec.md", f"Generate {value}.\n"),),
        tuple(models),
        skills=tuple(skills),
        revision_identity=revision,
        specification_set_identity=specification_set,
    )


def recipe(selected: RecipeFlavor) -> GenerationRecipe:
    suffix = "py" if selected.value == "python" else "cpp"
    root_identity = ContentIdentity.parse_uri("sha256:" + "a" * 64)
    root_ref = component_bom_ref(root_identity)
    managed_graph = CycloneDxManagedGraph(
        root_ref,
        (
            CycloneDxManagedComponent(
                root_ref,
                ManagedComponentKind.ROOT,
                root_identity,
                "urn:literate-ai:component:test/hello",
                "1.0.0",
                (),
            ),
        ),
        (),
        ContentIdentity.parse_uri("sha256:" + "c" * 64),
    )
    return GenerationRecipe(
        "hello-recipe",
        "hello",
        (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
        TEST_COMPONENT_LOCK_IDENTITY,
        (selected,),
        f"source/main.{suffix}",
        (("codex", "base-codex-model"),),
        (generation_skill(),),
        managed_sbom_graph=managed_graph,
    )


def generated_test_suite(
    value: GenerationRecipe, *, first_arguments: list[object] | None = None
) -> str:
    arguments = first_arguments or [{"value": 7}]
    return json.dumps(
        {
            "schema": GENERATED_TEST_SUITE_SCHEMA,
            "recipe_identity": value.identity,
            "generation_mode": "major-rebuild",
            "cases": [
                {
                    "case_id": "ordinary-example",
                    "category": "example",
                    "specification_refs": ["openspec/spec.md"],
                    "arguments": arguments,
                    "expected_result": {"value": 14},
                },
                {
                    "case_id": "empty-boundary",
                    "category": "boundary",
                    "specification_refs": ["openspec/spec.md"],
                    "arguments": [],
                    "expected_result": {"value": 0},
                },
                {
                    "case_id": "repeat-invariant",
                    "category": "invariant",
                    "specification_refs": ["openspec/spec.md"],
                    "arguments": [{"value": 19}],
                    "expected_result": {"value": 38},
                },
            ],
        },
        sort_keys=True,
    )


def write_generated_test_suite(workspace: Path, value: GenerationRecipe) -> None:
    path = workspace / GENERATED_TEST_SUITE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated_test_suite(value), encoding="utf-8")
    assert value.managed_sbom_graph is not None
    authority_components, authority_edges = coding_cli_adapter._recipe_authority_sbom(
        value
    )
    sbom, _binding = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=value.managed_sbom_graph,
        additional_components=authority_components,
        additional_edges=authority_edges,
    )
    sbom_path = workspace / CYCLONEDX_SOURCE_SBOM_PATH
    sbom_path.parent.mkdir(parents=True, exist_ok=True)
    sbom_path.write_bytes(sbom)


def write_duplicate_argument_generated_test_suite(
    workspace: Path, value: GenerationRecipe
) -> None:
    write_generated_test_suite(workspace, value)
    payload = json.loads(generated_test_suite(value))
    payload["cases"][1]["arguments"] = payload["cases"][0]["arguments"]
    path = workspace / GENERATED_TEST_SUITE_PATH
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


class GeneratedCppBazelValidationTests(unittest.TestCase):
    def test_rejects_hdrs_on_rules_cc_executable_rules(self):
        for rule_kind in ("cc_binary", "cc_test"):
            with self.subTest(rule_kind=rule_kind):
                files = {
                    "source/BUILD.bazel": (
                        f'{rule_kind}(name = "run", srcs = ["main.cpp"], '
                        'hdrs = ["app.hpp"])\n'
                    ),
                    "source/main.cpp": "int main() { return 0; }\n",
                    "source/app.hpp": "#pragma once\n",
                }

                with self.assertRaises(GeneratedSourceValidationError) as raised:
                    validate_cpp_bazel_rule_attributes(files)

                self.assertEqual(
                    raised.exception.code,
                    "coding_cli.generated_cpp_bazel_hdrs_unsupported",
                )
                self.assertIn(rule_kind, raised.exception.message)

    def test_accepts_headers_owned_by_a_cc_library(self):
        files = {
            "source/BUILD.bazel": (
                'cc_library(name = "app", srcs = ["app.cpp"], '
                'hdrs = ["app.hpp"])\n'
                'cc_binary(name = "run", srcs = ["main.cpp"], deps = [":app"])\n'
            ),
            "source/main.cpp": "int main() { return 0; }\n",
            "source/app.cpp": '#include "app.hpp"\n',
            "source/app.hpp": "#pragma once\n",
        }

        validate_cpp_bazel_rule_attributes(files)

    def test_rejects_workspace_root_include_on_rules_cc_executable_rules(self):
        for rule_kind in ("cc_binary", "cc_test"):
            with self.subTest(rule_kind=rule_kind):
                files = {
                    "source/BUILD.bazel": (
                        f'{rule_kind}(name = "run", srcs = ["main.cpp"], '
                        'includes = ["."])\n'
                    ),
                    "source/main.cpp": "int main() { return 0; }\n",
                }

                with self.assertRaises(GeneratedSourceValidationError) as raised:
                    validate_cpp_bazel_rule_attributes(files)

                self.assertEqual(
                    raised.exception.code,
                    "coding_cli.generated_cpp_bazel_workspace_include_unsupported",
                )
                self.assertIn('includes = ["."]', raised.exception.message)

    def test_accepts_package_relative_rules_cc_include_directory(self):
        files = {
            "source/BUILD.bazel": (
                'cc_binary(name = "run", srcs = ["main.cpp"], includes = ["include"])\n'
            ),
            "source/main.cpp": "int main() { return 0; }\n",
        }

        validate_cpp_bazel_rule_attributes(files)

    def test_rejects_genrule_output_that_collides_with_windows_binary(self):
        for output in ("run", "run.exe"):
            with self.subTest(output=output):
                files = {
                    "source/BUILD.bazel": (
                        'cc_binary(name = "run", srcs = ["main.cpp"])\n'
                        'genrule(name = "copy", srcs = [":run"], '
                        f'outs = ["{output}"], cmd = "copy")\n'
                    ),
                    "source/main.cpp": "int main() { return 0; }\n",
                }

                with self.assertRaises(GeneratedSourceValidationError) as raised:
                    validate_cpp_bazel_rule_attributes(files)

                self.assertEqual(
                    raised.exception.code,
                    "coding_cli.generated_cpp_bazel_output_collision",
                )
                self.assertIn(
                    "use the cc_binary target directly", raised.exception.message
                )

    def test_generation_rejects_unsupported_hdrs_before_index_or_cache(self):
        value = replace(recipe(flavor("cpp")), required_entrypoint="source/main.cpp")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source"
                source.mkdir()
                (source / "main.cpp").write_text(
                    "int main() { return 0; }\n", encoding="utf-8"
                )
                (source / "app.hpp").write_text("#pragma once\n", encoding="utf-8")
                (source / "BUILD.bazel").write_text(
                    'cc_binary(name = "run", srcs = ["main.cpp"], '
                    'hdrs = ["app.hpp"])\n',
                    encoding="utf-8",
                )
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli."
                    "_require_codex_linux_workspace_write_prerequisite"
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_mode="off",
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=output)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_cpp_bazel_hdrs_unsupported",
        )


class GeneratedMakeValidationTests(unittest.TestCase):
    def test_rejects_unquoted_framework_language_tool_in_recipes(self) -> None:
        files = {
            "source/Makefile": (
                "LITAI_LANGUAGE_TOOL ?= node\n"
                ".PHONY: all\n"
                "all:\n"
                "\t$(LITAI_LANGUAGE_TOOL) tools/build.js\n"
            )
        }

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_make_language_tool_quoting(files)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_make_language_tool_unquoted",
        )
        self.assertIn("may contain spaces on Windows", raised.exception.message)

    def test_accepts_double_quoted_framework_language_tool_in_recipes(self) -> None:
        validate_make_language_tool_quoting(
            {
                "source/Makefile": (
                    "LITAI_LANGUAGE_TOOL ?= node\n"
                    ".PHONY: all clean\n"
                    "all:\n"
                    '\t"$(LITAI_LANGUAGE_TOOL)" tools/build.js\n'
                    "clean:\n"
                    '\t@"$(LITAI_LANGUAGE_TOOL)" tools/clean.js\n'
                )
            }
        )


class GeneratedRustBazelValidationTests(unittest.TestCase):
    def test_rejects_path_module_omitted_from_rule_sources(self):
        files = {
            "source/BUILD.bazel": ('rust_binary(name = "run", srcs = ["main.rs"])\n'),
            "source/main.rs": (
                '#[path = "tests/litai_test.rs"]\nmod litai_test;\nfn main() {}\n'
            ),
            "source/tests/litai_test.rs": "pub fn run() {}\n",
        }

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_rust_bazel_source_closure(files)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_rust_bazel_source_closure_incomplete",
        )
        self.assertIn("source/tests/litai_test.rs", raised.exception.message)

    def test_accepts_complete_transitive_module_closure(self):
        files = {
            "source/BUILD.bazel": (
                'rust_binary(name = "run", srcs = '
                '["main.rs", "engine.rs", "engine/parser.rs"])\n'
            ),
            "source/main.rs": "mod engine;\nfn main() {}\n",
            "source/engine.rs": "mod parser;\n",
            "source/engine/parser.rs": "pub fn parse() {}\n",
        }

        validate_rust_bazel_source_closure(files)

    def test_rejects_a_rust_rule_whose_sources_cannot_be_proved(self):
        files = {
            "source/BUILD.bazel": (
                'rust_binary(name = "run", srcs = COMMON_SOURCES)\n'
            ),
            "source/main.rs": "fn main() {}\n",
        }

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_rust_bazel_source_closure(files)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_rust_bazel_source_closure_unverifiable",
        )

    def test_generation_rejects_incomplete_closure_before_index_or_cache(self):
        value = replace(recipe(flavor("rust")), required_entrypoint="source/main.rs")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source"
                (source / "tests").mkdir(parents=True)
                (source / "main.rs").write_text(
                    '#[path = "tests/litai_test.rs"]\nmod litai_test;\nfn main() {}\n',
                    encoding="utf-8",
                )
                (source / "tests" / "litai_test.rs").write_text(
                    "pub fn run() {}\n", encoding="utf-8"
                )
                (source / "BUILD.bazel").write_text(
                    'rust_binary(name = "run", srcs = ["main.rs"])\n',
                    encoding="utf-8",
                )
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli."
                    "_require_codex_linux_workspace_write_prerequisite"
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_mode="off",
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=output)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_rust_bazel_source_closure_incomplete",
        )


class TestSourceIntelligenceProvider:
    """Deterministic boundary fake with a genuine frozen SQLite artifact."""

    def preflight(self) -> str:
        return "1.1.1"

    def verify(
        self,
        source_root: Path,
        files: dict[str, bytes],
        evidence: SourceIntelligenceArtifact,
    ) -> SourceIntelligenceArtifact:
        if evidence.source_tree_identity != generated_source_tree_identity(files):
            raise ValueError("test source-intelligence evidence names another tree")
        if not (source_root / evidence.artifact_path).is_file():
            raise ValueError("test source-intelligence artifact is missing")
        return evidence

    def index(
        self,
        source_root: Path,
        files: dict[str, bytes],
        *,
        source_tree_identity: str | None = None,
    ) -> SourceIntelligenceArtifact:
        actual_tree_identity = generated_source_tree_identity(files)
        if source_tree_identity not in {None, actual_tree_identity}:
            raise ValueError("test source-index request names another tree")
        sidecar = source_root / ".source-intelligence"
        sidecar.mkdir()
        database = sidecar / "index.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY)")
            connection.commit()
        database_identity = (
            f"sha256:{hashlib.sha256(database.read_bytes()).hexdigest()}"
        )
        return SourceIntelligenceArtifact.create(
            provider_id="fixture-intelligence",
            provider_version="test-fixture-v1",
            runtime_version="1.1.1",
            executable_identity="sha256:" + "e" * 64,
            source_tree_identity=actual_tree_identity,
            source_snapshot_identity=generated_source_snapshot_identity(files),
            capabilities=("call-graph", "declarations", "references"),
            provider_properties=(
                "built-with-version=1.1.1",
                "extraction-version=24",
            ),
            artifact_path=".source-intelligence/index.db",
            artifact_media_type="application/vnd.sqlite3",
            artifact_identity=database_identity,
            document_count=len(files),
            symbol_count=len(files),
            relationship_count=0,
            unresolved_relationship_count=0,
            warning_count=0,
        )


class CodingCliModelSelectionTests(unittest.TestCase):
    def test_recipe_prompt_and_model_bind_exact_lexical_scope(self) -> None:
        owner = canonical_identity({"pipeline": "test"})
        scope = resolve_model_scope(
            scope_kind=ModelScopeKind.PIPELINE,
            owner_identity=owner,
            provider_id="codex",
            candidates=((owner, "pipeline-model"),),
        )
        value = replace(recipe(flavor("python")), model_scope=scope)

        self.assertEqual(value.model_for("codex"), "pipeline-model")
        self.assertIn(scope.identity.uri, value.prompt())
        self.assertIn('"model_selector":"pipeline-model"', value.prompt())

    def test_reserved_default_selector_cannot_be_explicit_recipe_or_flavor_input(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "reserved for an omitted"):
            flavor(
                "cpp",
                models=(("codex", CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR),),
            )

        with self.assertRaisesRegex(ValueError, "reserved for an omitted"):
            GenerationRecipe(
                "reserved-model",
                "hello",
                (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
                TEST_COMPONENT_LOCK_IDENTITY,
                models=(("codex", CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR),),
                skills=(generation_skill(),),
            )

    def test_generation_provenance_preserves_omission_as_none(self) -> None:
        omitted = coding_cli_adapter.CodingCliGeneration(
            {},
            "codex",
            "/tools/codex",
            None,
            "sha256:" + "a" * 64,
            ("/tools/codex",),
        )
        self.assertIsNone(omitted.model)

        with self.assertRaisesRegex(ValueError, "reserved for an omitted"):
            coding_cli_adapter.CodingCliGeneration(
                {},
                "codex",
                "/tools/codex",
                CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR,
                "sha256:" + "a" * 64,
                ("/tools/codex",),
            )


class CodingCliSelectionTests(unittest.TestCase):
    def test_linux_codex_fails_before_generation_when_bwrap_policy_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            restriction = root / "restrict"
            restriction.write_text("1\n", encoding="ascii")
            selection = coding_cli_adapter.CodingCliSelection(
                "codex", sys.executable, "sha256:" + "a" * 64
            )
            with (
                mock.patch.object(coding_cli_adapter.sys, "platform", "linux"),
                self.assertRaises(CodingCliError) as raised,
            ):
                coding_cli_adapter._require_codex_linux_workspace_write_prerequisite(
                    selection,
                    restriction_path=restriction,
                    bwrap_profile_path=root / "missing-profile",
                )

        self.assertEqual(
            raised.exception.code,
            "coding_cli.workspace_write_prerequisite_missing",
        )
        self.assertIn("bwrap-userns-restrict", raised.exception.message)

    def test_linux_codex_accepts_present_regular_bwrap_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            restriction = root / "restrict"
            profile = root / "bwrap-userns-restrict"
            restriction.write_text("1\n", encoding="ascii")
            profile.write_text("profile bwrap {}\n", encoding="utf-8")
            selection = coding_cli_adapter.CodingCliSelection(
                "codex", sys.executable, "sha256:" + "a" * 64
            )
            with mock.patch.object(coding_cli_adapter.sys, "platform", "linux"):
                coding_cli_adapter._require_codex_linux_workspace_write_prerequisite(
                    selection,
                    restriction_path=restriction,
                    bwrap_profile_path=profile,
                )

    def test_portable_tool_binding_excludes_machine_local_launcher_path(self):
        digest = "sha256:" + "a" * 64
        fixture_root = Path(__file__).resolve().parents[2]
        first_executable = str(fixture_root / ".machine-one" / "bin" / "codex")
        second_executable = str(fixture_root / ".machine-two" / "bin" / "codex")
        first = coding_cli_adapter.CodingCliSelection("codex", first_executable, digest)
        second = coding_cli_adapter.CodingCliSelection(
            "codex", second_executable, digest
        )

        self.assertNotEqual(first.identity, second.identity)
        self.assertEqual(first.tool_binding_identity, second.tool_binding_identity)
        value = recipe(flavor("cpp"))
        first_prompt = coding_cli_adapter._coding_cli_binding_prompt(value, first)
        second_prompt = coding_cli_adapter._coding_cli_binding_prompt(value, second)
        self.assertEqual(first_prompt, second_prompt)
        self.assertNotIn(first.executable, first_prompt)
        self.assertNotIn(second.executable, second_prompt)

    def test_environment_selection_is_exact_and_does_not_fall_back(self):
        with mock.patch(
            "literate_ai.adapters.models.coding_cli.shutil.which",
            return_value=sys.executable,
        ) as which:
            selected = select_coding_cli({"CODING_CLI": "claude", "PATH": "/tools"})
        self.assertEqual(selected.name, "claude")
        self.assertEqual(selected.executable, str(Path(sys.executable).resolve()))
        self.assertTrue(selected.executable_identity.startswith("sha256:"))
        which.assert_called_once_with("claude", path="/tools")

    def test_path_discovery_stops_at_the_first_available_cli(self):
        calls: list[str] = []

        def locate(name, *, path):
            del path
            calls.append(name)
            return sys.executable if name == "claude" else None

        with mock.patch(
            "literate_ai.adapters.models.coding_cli.shutil.which",
            side_effect=locate,
        ):
            selected = select_coding_cli({"PATH": "/tools"})
        self.assertEqual(selected.name, "claude")
        self.assertEqual(calls, ["codex", "claude"])

    def test_path_discovery_includes_opencode_after_existing_providers(self):
        calls: list[str] = []

        def locate(name, *, path):
            del path
            calls.append(name)
            return sys.executable if name == "opencode" else None

        with mock.patch(
            "literate_ai.adapters.models.coding_cli.shutil.which",
            side_effect=locate,
        ):
            selected = select_coding_cli({"PATH": "/tools"})
        self.assertEqual(selected.name, "opencode")
        self.assertEqual(calls, ["codex", "claude", "cursor-agent", "opencode"])

    def test_priority_order_is_configurable_and_overrides_the_default(self):
        calls: list[str] = []

        def locate(name, *, path):
            del path
            calls.append(name)
            return sys.executable if name == "cursor-agent" else None

        with mock.patch(
            "literate_ai.adapters.models.coding_cli.shutil.which",
            side_effect=locate,
        ):
            selected = select_coding_cli(
                {
                    "PATH": "/tools",
                    "LITERATE_AI_CODING_CLI_PRIORITY": (
                        "claude,cursor-agent,codex,opencode"
                    ),
                }
            )
        self.assertEqual(selected.name, "cursor-agent")
        self.assertEqual(calls, ["claude", "cursor-agent"])

    def test_an_invalid_priority_configuration_fails_closed(self):
        with self.assertRaises(coding_cli_adapter.CodingCliError) as raised:
            select_coding_cli(
                {
                    "PATH": "/tools",
                    "LITERATE_AI_CODING_CLI_PRIORITY": "claude,codex",
                }
            )
        self.assertEqual(
            raised.exception.code, "coding_cli.priority_configuration_invalid"
        )

    def test_opencode_compatibility_probe_uses_the_exact_bounded_help_surface(self):
        selection = coding_cli_adapter._pin_coding_cli("opencode", sys.executable)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                return_value=_compatible_opencode_help_result(),
            ) as invoked,
            mock.patch.object(
                coding_cli_adapter.CodingCliSelection,
                "require_unchanged",
            ) as unchanged,
        ):
            coding_cli_adapter._require_coding_cli_compatible(
                selection,
                workspace=Path(directory),
                environment={
                    "PATH": "/tools",
                    "OPENCODE_API_KEY": "must-not-cross-preflight",
                },
            )

        self.assertEqual(
            tuple(invoked.call_args.args[0]),
            (str(Path(sys.executable).resolve()), "--pure", "run", "--help"),
        )
        self.assertEqual(invoked.call_args.kwargs["timeout_seconds"], 15)
        self.assertEqual(invoked.call_args.kwargs["maximum_stdout_bytes"], 256 * 1024)
        self.assertEqual(invoked.call_args.kwargs["maximum_stderr_bytes"], 256 * 1024)
        self.assertNotIn("OPENCODE_API_KEY", invoked.call_args.kwargs["environment"])
        self.assertEqual(unchanged.call_count, 2)

    def test_opencode_1_2_6_shaped_help_is_incompatible(self):
        selection = coding_cli_adapter._pin_coding_cli("opencode", sys.executable)
        old_help = b"Commands: run\nOptions: --agent\n"
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=old_help,
                    stderr=b"",
                ),
            ),
            self.assertRaises(CodingCliError) as raised,
        ):
            coding_cli_adapter._require_coding_cli_compatible(
                selection,
                workspace=Path(directory),
                environment={"PATH": "/tools"},
            )

        self.assertEqual(raised.exception.code, "coding_cli.incompatible")
        self.assertIn("--pure run", raised.exception.message)
        self.assertIn("upgrade OpenCode", raised.exception.message)
        self.assertNotIn(old_help.decode(), raised.exception.message)

    def test_opencode_probe_failures_are_secret_free_compatibility_errors(self):
        selection = coding_cli_adapter._pin_coding_cli("opencode", sys.executable)
        secret = "probe-output-secret"
        for code in (
            "coding_cli.timeout",
            "coding_cli.output_limit",
            "coding_cli.execution_failed",
        ):
            with (
                self.subTest(code=code),
                tempfile.TemporaryDirectory() as directory,
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=CodingCliError(code, secret),
                ),
                self.assertRaises(CodingCliError) as raised,
            ):
                coding_cli_adapter._require_coding_cli_compatible(
                    selection,
                    workspace=Path(directory),
                    environment={"PATH": "/tools"},
                )

            self.assertEqual(raised.exception.code, "coding_cli.incompatible")
            self.assertNotIn(secret, raised.exception.message)

    def test_invalid_explicit_cli_fails_closed(self):
        with self.assertRaisesRegex(CodingCliError, "must be codex"):
            select_coding_cli({"CODING_CLI": "other", "PATH": "/tools"})

    def test_missing_explicit_cli_does_not_fall_back(self):
        with mock.patch(
            "literate_ai.adapters.models.coding_cli.shutil.which",
            return_value=None,
        ) as which:
            with self.assertRaisesRegex(CodingCliError, "not on PATH"):
                select_coding_cli({"CODING_CLI": "codex", "PATH": "/tools"})
        which.assert_called_once_with("codex", path="/tools")

    def test_windows_vm_runner_must_explicitly_select_full_access(self):
        with (
            mock.patch.object(
                coding_cli_adapter, "_is_windows_host", return_value=True
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
        ):
            selected = select_coding_cli(
                {
                    "CODING_CLI": "codex",
                    "PATH": "/tools",
                    "LITAI_CODEX_SANDBOX": "danger-full-access",
                }
            )

        self.assertEqual(selected.effective_codex_sandbox_mode, "danger-full-access")
        self.assertEqual(selected.isolation.filesystem_boundary, "external-runner-vm")

    def test_full_access_is_rejected_outside_a_windows_vm_runner(self):
        with (
            mock.patch.object(
                coding_cli_adapter, "_is_windows_host", return_value=False
            ),
            self.assertRaisesRegex(CodingCliError, "reserved.*Windows VM"),
        ):
            select_coding_cli(
                {
                    "CODING_CLI": "codex",
                    "PATH": "/tools",
                    "LITAI_CODEX_SANDBOX": "danger-full-access",
                }
            )

    def test_lifecycle_driver_receives_only_selected_provider_credentials(self):
        environment = {
            "CODING_CLI": "claude",
            "PATH": "/tools",
            "ANTHROPIC_API_KEY": "claude-secret",
            "OPENAI_API_KEY": "codex-secret",
            "CURSOR_API_KEY": "cursor-secret",
        }
        result = lifecycle_driver_environment(
            environment,
            tuple(environment),
            workspace=Path("/workspace"),
        )

        self.assertEqual(result["CODING_CLI"], "claude")
        self.assertEqual(result["ANTHROPIC_API_KEY"], "claude-secret")
        self.assertNotIn("OPENAI_API_KEY", result)
        self.assertNotIn("CURSOR_API_KEY", result)

    def test_lifecycle_driver_isolates_inherited_session_from_coding_cli(self):
        environment = {
            "LITAI_CODING_PROVIDER": "inherited-session",
            "LITAI_INHERITED_SESSION_PROVIDER_IDENTITY": "sha256:" + "a" * 64,
            "LITAI_INHERITED_SESSION_IDENTITY": "sha256:" + "b" * 64,
            "LITAI_INHERITED_SESSION_AUTH_KEY_ID": "run-key",
            "LITAI_INHERITED_SESSION_AUTH_KEY": "cc" * 32,
            "LITAI_INHERITED_SESSION_CHANNEL": "/private/channel",
            "LITAI_INHERITED_SESSION_TIMEOUT_SECONDS": "30",
            "CODING_CLI": "claude",
            "ANTHROPIC_API_KEY": "must-not-cross",
            "PATH": "/tools",
        }
        result = lifecycle_driver_environment(
            environment,
            tuple(environment),
            workspace=Path("/workspace"),
        )

        self.assertEqual(
            result["LITAI_INHERITED_SESSION_AUTH_KEY"],
            environment["LITAI_INHERITED_SESSION_AUTH_KEY"],
        )
        self.assertNotIn("CODING_CLI", result)
        self.assertNotIn("ANTHROPIC_API_KEY", result)

    def test_lifecycle_driver_admits_opencode_model_provider_credentials(self):
        environment = {
            "CODING_CLI": "opencode",
            "PATH": "/tools",
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "OPENAI_API_KEY": "openai-secret",
            "OPENCODE_API_KEY": "opencode-secret",
            "CURSOR_API_KEY": "cursor-secret",
        }
        result = lifecycle_driver_environment(
            environment,
            tuple(environment),
            workspace=Path("/workspace"),
        )

        self.assertEqual(result["CODING_CLI"], "opencode")
        self.assertEqual(result["ANTHROPIC_API_KEY"], "anthropic-secret")
        self.assertEqual(result["OPENAI_API_KEY"], "openai-secret")
        self.assertEqual(result["OPENCODE_API_KEY"], "opencode-secret")
        self.assertNotIn("CURSOR_API_KEY", result)

    def test_lifecycle_driver_admits_declared_cache_bazel_and_sandbox_controls(self):
        environment = {
            "PATH": "/tools",
            "BAZEL_SH": "/tools/bash",
            "BUILD_DIR": "/workspace/generated",
            "OBJ_DIR": "/workspace/_build",
            "LITAI_CODEX_SANDBOX": "workspace-write",
            "SystemDrive": "C:",
        }

        result = lifecycle_driver_environment(
            environment,
            tuple(environment),
            workspace=Path("/workspace"),
        )

        self.assertEqual(result, {**environment, "PWD": str(Path("/workspace"))})

    def test_lifecycle_driver_rejects_pythonpath_injection(self):
        with self.assertRaises(CodingCliError) as raised:
            lifecycle_driver_environment(
                {"PATH": "/tools", "PYTHONPATH": "/untrusted"},
                ("PATH", "PYTHONPATH"),
                workspace=Path("/workspace"),
            )
        self.assertEqual(
            raised.exception.code, "coding_cli.environment_key_unauthorized"
        )


class FlavorSelectorTests(unittest.TestCase):
    def test_library_recipe_binds_import_surface_and_generation_guidance(self):
        base = recipe(flavor("python"))
        surface = LibraryImportSurface(
            "python",
            "hello",
            (
                LibraryCapabilityImport(
                    "hello.render",
                    canonical_identity({"interface": "hello-render"}),
                    "hello.render",
                    ("render",),
                ),
            ),
        )
        library = replace(base, library_import_surface=surface)

        self.assertNotEqual(library.identity, base.identity)
        self.assertIn("This Component is a library", library.prompt())
        self.assertIn('"module":"hello.render"', library.prompt())
        self.assertIn("no product entrypoint", library.prompt())

    def test_consumer_recipe_binds_exact_direct_library_import(self):
        base = recipe(flavor("python"))
        interface = canonical_identity({"interface": "hello-render"})
        surface = LibraryImportSurface(
            "python",
            "hello",
            (
                LibraryCapabilityImport(
                    "hello.render", interface, "hello.render", ("render",)
                ),
            ),
        )
        dependency = RecipeLibraryDependency(
            canonical_identity({"provider": "hello"}),
            "hello.render",
            interface,
            surface,
        )
        consumer = replace(base, library_dependencies=(dependency,))

        self.assertNotEqual(consumer.identity, base.identity)
        self.assertIn("Required direct library imports", consumer.prompt())
        self.assertIn('"provider_component_revision"', consumer.prompt())
        self.assertIn('"module":"hello.render"', consumer.prompt())

        with self.assertRaisesRegex(ValueError, "interface differs"):
            RecipeLibraryDependency(
                dependency.provider_component_revision,
                dependency.capability,
                canonical_identity({"interface": "other"}),
                surface,
            )

    def test_explicit_selection_replaces_a_weaker_initial_preference(self):
        python = flavor("python")
        cpp = flavor("cpp")

        selected = apply_flavor_selectors(
            (python, cpp),
            ("+cpp",),
            initial=(python,),
            initial_is_preferences=True,
        )

        self.assertEqual(selected, (cpp,))

    def test_reselecting_a_preference_upgrades_it_to_explicit(self):
        python = flavor("python")
        cpp = flavor("cpp")

        with self.assertRaisesRegex(CodingCliError, "subtract"):
            apply_flavor_selectors(
                (python, cpp),
                ("+python", "+cpp"),
                initial=(python,),
                initial_is_preferences=True,
            )

    def test_replacing_one_preferred_slot_retains_its_other_binding(self):
        bazel = RecipeFlavor(
            "bazel",
            "build.system",
            "bazel",
            (RecipeDocument.create("bazel/spec.md", "Prefer Bazel.\n"),),
            slot_ids=("backend-build", "frontend-build"),
        )
        native = RecipeFlavor(
            "native",
            "build.system",
            "native",
            (RecipeDocument.create("native/spec.md", "Use native build.\n"),),
        )
        slot_axes = {
            "backend-build": "build.system",
            "frontend-build": "build.system",
        }

        selected = apply_flavor_selectors(
            (bazel, native),
            ("+backend-build:native",),
            initial=(bazel,),
            initial_is_preferences=True,
            slot_axes=slot_axes,
        )

        self.assertEqual(
            [(item.flavor_id, item.slot_ids) for item in selected],
            [
                ("native", ("backend-build",)),
                ("bazel", ("frontend-build",)),
            ],
        )

    def test_singleton_slot_qualified_and_unqualified_selectors_are_equivalent(self):
        bazel = RecipeFlavor(
            "bazel",
            "build.system",
            "bazel",
            (RecipeDocument.create("bazel/spec.md", "Prefer Bazel.\n"),),
        )
        native = RecipeFlavor(
            "native",
            "build.system",
            "native",
            (RecipeDocument.create("native/spec.md", "Use native build.\n"),),
        )
        slot_axes = {"build-system": "build.system"}

        selected = apply_flavor_selectors(
            (bazel, native),
            ("+build-system:native",),
            initial=(bazel,),
            initial_is_preferences=True,
            slot_axes=slot_axes,
        )
        self.assertEqual(selected[0].flavor_id, "native")
        self.assertEqual(selected[0].slot_ids, ("build-system",))

        removed = apply_flavor_selectors(
            (bazel, native),
            ("-build-system:bazel",),
            initial=(bazel,),
            initial_is_preferences=True,
            slot_axes=slot_axes,
        )
        self.assertEqual(removed, ())

    def test_plus_and_minus_replace_one_mutually_exclusive_language(self):
        python = flavor("python")
        cpp = flavor("cpp")
        selected = apply_flavor_selectors(
            (python, cpp),
            ("-python", "+cpp"),
            initial=(python,),
        )
        self.assertEqual(selected, (cpp,))

    def test_two_positive_values_on_one_axis_require_explicit_subtraction(self):
        with self.assertRaisesRegex(CodingCliError, "subtract"):
            apply_flavor_selectors(
                (flavor("python"), flavor("cpp")),
                ("+python", "+cpp"),
            )

    def test_declared_multi_value_axis_keeps_multiple_exact_flavors(self):
        first = RecipeFlavor(
            "logging-json",
            "runtime.feature",
            "enabled",
            (RecipeDocument.create("logging/spec.md", "Add JSON logs.\n"),),
        )
        second = RecipeFlavor(
            "metrics",
            "runtime.feature",
            "enabled",
            (RecipeDocument.create("metrics/spec.md", "Add metrics.\n"),),
        )
        selected = apply_flavor_selectors(
            (first, second),
            ("+logging-json", "+metrics"),
            multi_value_axes=frozenset({"runtime.feature"}),
        )
        self.assertEqual(selected, (first, second))
        value = GenerationRecipe(
            "multi-flavor",
            "hello",
            (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
            TEST_COMPONENT_LOCK_IDENTITY,
            selected,
            skills=(generation_skill(),),
        )
        self.assertEqual(value.flavors, selected)

    def test_declared_cross_axis_conflict_requires_explicit_subtraction(self):
        linux = RecipeFlavor(
            "linux",
            "platform.os",
            "linux",
            (RecipeDocument.create("linux/spec.md", "Use Linux.\n"),),
            conflicts=("flavor://samples/cpu",),
            coordinate_uri="flavor://samples/linux",
        )
        cpu = RecipeFlavor(
            "cpu",
            "accelerator",
            "cpu",
            (RecipeDocument.create("cpu/spec.md", "Use the CPU.\n"),),
            coordinate_uri="flavor://samples/cpu",
        )
        with self.assertRaisesRegex(CodingCliError, "conflicts.*subtract"):
            apply_flavor_selectors((linux, cpu), ("+linux", "+cpu"))

    def test_slot_qualified_selectors_bind_two_roles_on_one_axis(self):
        javascript = flavor("javascript")
        rust = flavor("rust")
        slot_axes = {
            "backend-language": "implementation.language-ecosystem",
            "frontend-language": "implementation.language-ecosystem",
        }

        selected = apply_flavor_selectors(
            (javascript, rust),
            (
                "+backend-language:rust",
                "+frontend-language:javascript",
            ),
            slot_axes=slot_axes,
        )

        self.assertEqual(
            [(item.slot_ids, item.value) for item in selected],
            [
                (("backend-language",), "rust"),
                (("frontend-language",), "javascript"),
            ],
        )
        correct = GenerationRecipe(
            "full-stack",
            "dashboard",
            (RecipeDocument.create("openspec/spec.md", "Build dashboard.\n"),),
            TEST_COMPONENT_LOCK_IDENTITY,
            selected,
            skills=(generation_skill(),),
            required_entrypoints=(
                "source/backend/main.rs",
                "source/frontend/main.js",
            ),
        )
        inverted = GenerationRecipe(
            "full-stack",
            "dashboard",
            (RecipeDocument.create("openspec/spec.md", "Build dashboard.\n"),),
            TEST_COMPONENT_LOCK_IDENTITY,
            apply_flavor_selectors(
                (javascript, rust),
                (
                    "+backend-language:javascript",
                    "+frontend-language:rust",
                ),
                slot_axes=slot_axes,
            ),
            skills=(generation_skill(),),
            required_entrypoints=(
                "source/backend/main.js",
                "source/frontend/main.rs",
            ),
        )
        self.assertNotEqual(correct.identity, inverted.identity)
        prompt = correct.prompt()
        self.assertIn("`backend-language`: `implementation.language", prompt)
        self.assertIn("ecosystem=rust`", prompt)
        self.assertIn("`frontend-language`: `implementation.language", prompt)
        self.assertIn("ecosystem=javascript`", prompt)
        # The framework envelope must stay domain-neutral: role wiring, output
        # field shapes, and per-role file layout belong to Component specification
        # authority (see samples/full-stack-rust-js/component.md), never to a
        # framework prompt special case.
        for domain_vocabulary in (
            "top_risk_service",
            "pass_rate_basis_points",
            "runner records that object",
            "backend executable path",
        ):
            self.assertNotIn(domain_vocabulary, prompt)

    def test_one_flavor_revision_can_bind_multiple_component_slots(self):
        rust = flavor("rust")
        slot_axes = {
            "backend-language": "implementation.language-ecosystem",
            "worker-language": "implementation.language-ecosystem",
        }

        selected = apply_flavor_selectors(
            (rust,),
            (
                "+backend-language:rust",
                "+worker-language:rust",
            ),
            slot_axes=slot_axes,
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(
            selected[0].slot_ids,
            ("backend-language", "worker-language"),
        )
        self.assertIs(selected[0].documents, rust.documents)
        value = GenerationRecipe(
            "rust-services",
            "services",
            (RecipeDocument.create("openspec/spec.md", "Build services.\n"),),
            TEST_COMPONENT_LOCK_IDENTITY,
            selected,
            skills=(generation_skill(),),
            required_entrypoints=(
                "source/backend/main.rs",
                "source/worker/main.rs",
            ),
        )
        self.assertEqual(len(value.flavors), 1)
        self.assertIn("`backend-language`", value.prompt())
        self.assertIn("`worker-language`", value.prompt())

        remaining = apply_flavor_selectors(
            (rust,),
            ("-worker-language:rust",),
            initial=selected,
            slot_axes=slot_axes,
        )
        self.assertEqual(remaining[0].slot_ids, ("backend-language",))

        removed = apply_flavor_selectors(
            (rust,),
            ("-rust",),
            initial=selected,
            slot_axes=slot_axes,
        )
        self.assertEqual(removed, ())

    def test_repeated_axis_requires_slot_qualification(self):
        with self.assertRaises(CodingCliError) as error:
            apply_flavor_selectors(
                (flavor("rust"),),
                ("+rust",),
                slot_axes={
                    "backend-language": "implementation.language-ecosystem",
                    "frontend-language": "implementation.language-ecosystem",
                },
            )
        self.assertEqual(error.exception.code, "coding_cli.flavor_slot_required")

    def test_slot_qualified_selectors_reject_malformed_bindings(self):
        javascript = flavor("javascript")
        rust = flavor("rust")
        macos = RecipeFlavor(
            "platform-macos",
            "platform.os",
            "macos",
            (RecipeDocument.create("macos/spec.md", "Use macOS.\n"),),
        )
        slot_axes = {
            "backend-language": "implementation.language-ecosystem",
            "frontend-language": "implementation.language-ecosystem",
            "os": "platform.os",
        }
        cases = (
            (
                ("+missing:rust",),
                "coding_cli.unknown_flavor_slot",
            ),
            (
                ("+backend-language:macos",),
                "coding_cli.flavor_slot_axis_mismatch",
            ),
            (
                ("+backend-language:",),
                "coding_cli.invalid_flavor_selector",
            ),
            (
                ("+backend-language:rust:extra",),
                "coding_cli.invalid_flavor_selector",
            ),
            (
                (
                    "+backend-language:rust",
                    "+backend-language:javascript",
                ),
                "coding_cli.mutually_exclusive_flavors",
            ),
        )
        for selectors, code in cases:
            with self.subTest(selectors=selectors):
                with self.assertRaises(CodingCliError) as error:
                    apply_flavor_selectors(
                        (javascript, rust, macos),
                        selectors,
                        slot_axes=slot_axes,
                    )
                self.assertEqual(error.exception.code, code)


class CodingCliInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        host_prerequisite = mock.patch.object(
            coding_cli_adapter,
            "_require_codex_linux_workspace_write_prerequisite",
        )
        host_prerequisite.start()
        self.addCleanup(host_prerequisite.stop)
        evidence_environment = mock.patch.dict(
            os.environ,
            {
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

    def test_python_bytecode_cache_is_not_admitted_as_generated_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                bytecode = workspace / "source" / "__pycache__" / "probe.pyc"
                bytecode.parent.mkdir()
                bytecode.write_bytes(b"\xcb\x00host-specific-bytecode")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generated = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                ).generate(value, output_root=root / "candidate")

            self.assertNotIn("source/__pycache__/probe.pyc", generated.files)
            self.assertFalse((root / "candidate/source/__pycache__").exists())

    def test_debug_instrumentation_is_bound_into_generated_tree_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("python"))

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.py"
                source.parent.mkdir(parents=True)
                source.write_text(
                    "# litai:spec openspec/spec.md:1 [entrypoint]\n"
                    "def main(value):\n"
                    "    return value\n",
                    encoding="utf-8",
                )
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
                debug_diagnostics("-", json_mode=True, stderr=io.StringIO()),
            ):
                output = root / "candidate"
                generated = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_mode="off",
                ).generate(value, output_root=output)

            self.assertIn("source/.literate/spec-map.json", generated.files)
            self.assertIn("source/litai_debug.py", generated.files)
            self.assertEqual(
                local_generated_source_tree_identity(output).uri,
                generated_source_tree_identity(
                    {
                        path: content.encode("utf-8")
                        for path, content in generated.files.items()
                    }
                ),
            )

    def test_cursor_agent_conversation_metadata_is_not_generated_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                conversation = workspace / ".vibe" / "conversations" / "session.json"
                conversation.parent.mkdir(parents=True)
                conversation.write_text("{}\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generated = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "cursor-agent", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                ).generate(value, output_root=root / "candidate")

            self.assertIn("source/main.cpp", generated.files)
            self.assertFalse((root / "candidate/.vibe").exists())

    def test_accepted_generated_source_cache_skips_second_coding_cli_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            value = recipe(flavor("cpp"))
            execution_plan = planned_execution(value)
            stage_request = {
                "stage_id": "generate",
                "prior_stage_outputs": {},
                "input_identity": {"fixture": "input"},
            }

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ) as invoked,
            ):
                cached = CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(
                        environment={"CODING_CLI": "codex", "PATH": "/tools"},
                        source_intelligence_provider=TestSourceIntelligenceProvider(),
                    ),
                    cache_root=project / "generated",
                    project_root=project,
                )
                first = cached.generate(
                    value,
                    output_root=root / "first",
                    execution_plan=execution_plan,
                    stage_request=stage_request,
                )
                # Reuse begins only after the outer lifecycle accepts the candidate.
                self.assertEqual(cached.report()["hits"], 0)
                cached.accept(first)
                second = cached.generate(
                    value,
                    output_root=root / "second",
                    execution_plan=execution_plan,
                    stage_request=stage_request,
                )
                self.assertEqual(invoked.call_count, 1)
                forced = CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(
                        environment={"CODING_CLI": "codex", "PATH": "/tools"},
                        source_intelligence_provider=(TestSourceIntelligenceProvider()),
                    ),
                    cache_root=project / "generated",
                    project_root=project,
                    force_regeneration=True,
                )
                third = forced.generate(
                    value,
                    output_root=root / "third",
                    execution_plan=execution_plan,
                    stage_request=stage_request,
                )
                forced.accept(third)

            self.assertEqual(invoked.call_count, 2)
            self.assertEqual(first.files, second.files)
            self.assertEqual(first.files, third.files)
            self.assertEqual(cached.report()["hits"], 1)
            self.assertEqual(cached.report()["misses"], 1)
            self.assertEqual(forced.report()["hits"], 0)
            self.assertEqual(forced.report()["misses"], 1)
            self.assertTrue(
                next((project / "generated" / "sources" / "sha256").iterdir())
                .joinpath(".litai-generation-prompt.md")
                .is_file()
            )

    def test_accepted_lookup_ignores_session_custody_but_binds_node_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            value = recipe(flavor("cpp"))
            execution_plan = planned_execution(value)

            def lookup_identity(label: str) -> ContentIdentity:
                return canonical_identity({"accepted-lookup-test": label})

            stable_plan = {
                "component_revision": lookup_identity("component").uri,
                "component_generation_plan_identity": lookup_identity("plan").uri,
                "context_manifest_identity": lookup_identity("context").uri,
                "recipe_identity": value.identity,
                "generation_key_identity": lookup_identity("generation-key").uri,
                "project_authority_identity": lookup_identity(
                    "repository-lineage-and-project-authority"
                ).uri,
                "required_entrypoints": ["source/main.cpp"],
                "direct_public_interface_identities": [
                    lookup_identity("interface").uri
                ],
            }

            def stage_request(session: str, **semantic_changes):
                plan = {
                    **stable_plan,
                    **semantic_changes,
                    "source_generation_request_identity": lookup_identity(
                        f"orchestration-{session}"
                    ).uri,
                    "workspace_allocation_identity": lookup_identity(
                        f"workspace-{session}"
                    ).uri,
                }
                return {
                    "stage_id": "generate",
                    "prior_stage_outputs": {"plan": plan},
                    "input_identity": lookup_identity(f"input-{session}").to_dict(),
                }

            with mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ):
                cached = CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(
                        environment={"CODING_CLI": "codex", "PATH": "/tools"},
                        source_intelligence_provider=(TestSourceIntelligenceProvider()),
                    ),
                    cache_root=project / "generated",
                    project_root=project,
                )
                first = cached.derivation_cache_key(
                    value,
                    execution_plan=execution_plan,
                    stage_request=stage_request("a"),
                    bounded_prompt=b"stable bounded Component prompt",
                )
                second = cached.derivation_cache_key(
                    value,
                    execution_plan=execution_plan,
                    stage_request=stage_request("b"),
                    bounded_prompt=b"stable bounded Component prompt",
                )
                changed = cached.derivation_cache_key(
                    value,
                    execution_plan=execution_plan,
                    stage_request=stage_request(
                        "b",
                        context_manifest_identity=lookup_identity(
                            "changed-context"
                        ).uri,
                    ),
                    bounded_prompt=b"stable bounded Component prompt",
                )
                changed_project = cached.derivation_cache_key(
                    value,
                    execution_plan=execution_plan,
                    stage_request=stage_request(
                        "b",
                        project_authority_identity=lookup_identity(
                            "changed-repository-lineage"
                        ).uri,
                    ),
                    bounded_prompt=b"stable bounded Component prompt",
                )

            self.assertNotEqual(first.request_identity, second.request_identity)
            self.assertEqual(
                first.accepted_source_lookup_identity,
                second.accepted_source_lookup_identity,
            )
            self.assertNotEqual(
                first.accepted_source_lookup_identity,
                changed.accepted_source_lookup_identity,
            )
            # Whole-project review authority remains in the exact transaction key,
            # but it is not one target's reusable source semantics.  Actual target
            # authority is independently bound by the Component revision, plan/key,
            # recipe, context, interfaces, and prompt.
            self.assertNotEqual(
                first.request_identity, changed_project.request_identity
            )
            self.assertEqual(
                first.accepted_source_lookup_identity,
                changed_project.accepted_source_lookup_identity,
            )

            changed_component = cached.derivation_cache_key(
                value,
                execution_plan=execution_plan,
                stage_request=stage_request(
                    "b",
                    component_revision=lookup_identity(
                        "changed-component-or-repository-lineage"
                    ).uri,
                ),
                bounded_prompt=b"stable bounded Component prompt",
            )
            self.assertNotEqual(
                first.accepted_source_lookup_identity,
                changed_component.accepted_source_lookup_identity,
            )

    def test_accepted_source_lookup_reconstructs_tool_without_executable(self):
        tool_binding = canonical_identity({"tool": "inherited-provider"})
        runtime = CodingCliSourceGenerator.for_accepted_source_lookup(
            "inherited-session", tool_binding
        )

        self.assertEqual(runtime.selection.name, "inherited-session")
        self.assertEqual(runtime.selection.tool_binding_identity, tool_binding.uri)
        with self.assertRaises(CodingCliError) as forbidden:
            runtime.generate(None, output_root=Path("."))
        self.assertEqual(forbidden.exception.code, "coding_cli.accepted_source_only")

    def test_component_lock_identity_partitions_cache_and_same_lock_replays(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            first_recipe = recipe(flavor("cpp"))
            second_lock_identity = ContentIdentity.parse_uri("sha256:" + "d" * 64)
            assert first_recipe.managed_sbom_graph is not None
            second_recipe = replace(
                first_recipe,
                component_lock_identity=second_lock_identity,
                managed_sbom_graph=replace(
                    first_recipe.managed_sbom_graph,
                    resolved_graph_identity=second_lock_identity,
                ),
            )
            self.assertNotEqual(first_recipe.identity, second_recipe.identity)
            stage_request = {
                "stage_id": "generate",
                "prior_stage_outputs": {},
                "input_identity": {"fixture": "input"},
            }
            generated_recipes = iter((first_recipe, second_recipe))

            def execute(command, **kwargs):
                del command
                selected_recipe = next(generated_recipes)
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, selected_recipe)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ) as invoked,
                mock.patch(
                    "literate_ai.adapters.models.coding_cli."
                    "_require_codex_linux_workspace_write_prerequisite"
                ),
            ):
                cached = CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(
                        environment={"CODING_CLI": "codex", "PATH": "/tools"},
                        source_intelligence_provider=TestSourceIntelligenceProvider(),
                    ),
                    cache_root=project / "generated",
                    project_root=project,
                )
                first = cached.generate(
                    first_recipe,
                    output_root=root / "first",
                    execution_plan=planned_execution(first_recipe),
                    stage_request=stage_request,
                )
                cached.accept(first)
                second = cached.generate(
                    second_recipe,
                    output_root=root / "second",
                    execution_plan=planned_execution(second_recipe),
                    stage_request=stage_request,
                )
                cached.accept(second)
                replay = cached.generate(
                    second_recipe,
                    output_root=root / "replay",
                    execution_plan=planned_execution(second_recipe),
                    stage_request=stage_request,
                )

            self.assertEqual(invoked.call_count, 2)
            self.assertEqual(cached.report()["misses"], 2)
            self.assertEqual(cached.report()["hits"], 1)
            self.assertEqual(second.files, replay.files)
            self.assertEqual(len(tuple(cached.entries.iterdir())), 2)

    def _assert_publication_parent_redirect_is_rejected(self, *, junction: bool):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / "project"
            project.mkdir()
            value = recipe(flavor("cpp"))
            execution_plan = planned_execution(value)
            stage_request = {
                "stage_id": "generate",
                "prior_stage_outputs": {},
                "input_identity": {"fixture": "redirect-input"},
            }

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                cached = CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(
                        environment={"CODING_CLI": "codex", "PATH": "/tools"},
                        source_intelligence_provider=TestSourceIntelligenceProvider(),
                    ),
                    cache_root=project / "generated",
                    project_root=project,
                )
                generation = cached.generate(
                    value,
                    output_root=root / "candidate",
                    execution_plan=execution_plan,
                    stage_request=stage_request,
                )

            outside = root / "outside-entries"
            outside.mkdir()
            shutil.rmtree(cached.entries)
            if junction:
                _create_windows_junction(cached.entries, outside)
            else:
                try:
                    cached.entries.symlink_to(outside, target_is_directory=True)
                except OSError as error:
                    self.skipTest(f"host cannot create directory symlinks: {error}")
            try:
                with self.assertRaisesRegex(GeneratedSourceCacheError, "unsafe"):
                    cached.accept(generation)
                self.assertEqual(tuple(outside.iterdir()), ())
            finally:
                cached.entries.rmdir() if junction else cached.entries.unlink()

    @unittest.skipIf(os.name == "nt", "POSIX symlink behavior")
    def test_publication_parent_symlink_cannot_escape_generated_cache(self):
        self._assert_publication_parent_redirect_is_rejected(junction=False)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_publication_parent_junction_cannot_escape_generated_cache(self):
        self._assert_publication_parent_redirect_is_rejected(junction=True)

    def test_concurrent_acceptance_publishes_one_generated_source_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            value = recipe(flavor("cpp"))
            execution_plan = planned_execution(value)
            stage_request = {
                "stage_id": "generate",
                "prior_stage_outputs": {},
                "input_identity": {"fixture": "concurrent-input"},
            }

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ) as invoked,
            ):
                adapters = tuple(
                    CachedCodingCliSourceGenerator(
                        CodingCliSourceGenerator(
                            environment={"CODING_CLI": "codex", "PATH": "/tools"},
                            source_intelligence_provider=(
                                TestSourceIntelligenceProvider()
                            ),
                        ),
                        cache_root=project / "generated",
                        project_root=project,
                    )
                    for _ in range(2)
                )
                generations = tuple(
                    adapter.generate(
                        value,
                        output_root=root / f"candidate-{index}",
                        execution_plan=execution_plan,
                        stage_request=stage_request,
                    )
                    for index, adapter in enumerate(adapters)
                )
                barrier = threading.Barrier(2)

                def accept(index: int) -> None:
                    barrier.wait(timeout=10)
                    adapters[index].accept(generations[index])

                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = tuple(
                        executor.submit(accept, index) for index in range(2)
                    )
                    for future in futures:
                        future.result(timeout=20)

                replay = CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(
                        environment={"CODING_CLI": "codex", "PATH": "/tools"},
                        source_intelligence_provider=TestSourceIntelligenceProvider(),
                    ),
                    cache_root=project / "generated",
                    project_root=project,
                ).generate(
                    value,
                    output_root=root / "replay",
                    execution_plan=execution_plan,
                    stage_request=stage_request,
                )

            entries = project / "generated" / "sources" / "sha256"
            self.assertEqual(invoked.call_count, 2)
            self.assertEqual(len(tuple(entries.iterdir())), 1)
            self.assertEqual(replay.files, generations[0].files)

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_generated_source_cache_rejects_nested_windows_junction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            value = recipe(flavor("cpp"))
            execution_plan = planned_execution(value)
            stage_request = {
                "stage_id": "generate",
                "prior_stage_outputs": {},
                "input_identity": {"fixture": "junction-input"},
            }

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                cached = CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(
                        environment={"CODING_CLI": "codex", "PATH": "/tools"},
                        source_intelligence_provider=TestSourceIntelligenceProvider(),
                    ),
                    cache_root=project / "generated",
                    project_root=project,
                )
                generation = cached.generate(
                    value,
                    output_root=root / "candidate",
                    execution_plan=execution_plan,
                    stage_request=stage_request,
                )
                cached.accept(generation)

            entry = next((project / "generated" / "sources" / "sha256").iterdir())
            source = entry / "source"
            outside = root / "outside-source"
            source.rename(outside)
            _create_windows_junction(source, outside)
            try:
                with self.assertRaisesRegex(GeneratedSourceCacheError, "reparse point"):
                    cached.generate(
                        value,
                        output_root=root / "replay",
                        execution_plan=execution_plan,
                        stage_request=stage_request,
                    )
            finally:
                source.rmdir()

    def test_required_intelligence_without_provider_fails_before_model_work(self):
        with mock.patch(
            "literate_ai.adapters.models.coding_cli.shutil.which",
            return_value=sys.executable,
        ):
            with self.assertRaisesRegex(ValueError, "needs a configured provider"):
                CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_mode="required",
                    use_default_source_intelligence_provider=False,
                )

    def test_incompatible_opencode_stops_before_model_invocation(self):
        old_help = SimpleNamespace(
            returncode=0,
            stdout=b"Commands: run\nOptions: --agent\n",
            stderr=b"",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ) as which,
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                return_value=old_help,
            ) as invoked,
        ):
            generator = CodingCliSourceGenerator(
                environment={"CODING_CLI": "opencode", "PATH": "/tools"},
                source_intelligence_mode="off",
            )
            with self.assertRaises(CodingCliError) as raised:
                generator.generate(
                    recipe(flavor("cpp")),
                    output_root=Path(directory) / "generated",
                )

        self.assertEqual(raised.exception.code, "coding_cli.incompatible")
        self.assertEqual(invoked.call_count, 1)
        self.assertTrue(_is_opencode_compatibility_probe(invoked.call_args.args[0]))
        which.assert_called_once_with("opencode", path="/tools")

    def test_incompatible_opencode_stops_json_task_before_model_invocation(self):
        old_help = SimpleNamespace(
            returncode=0,
            stdout=b"Commands: run\nOptions: --agent\n",
            stderr=b"",
        )
        with (
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                return_value=old_help,
            ) as invoked,
        ):
            runner = CodingCliTaskRunner(
                environment={"CODING_CLI": "opencode", "PATH": "/tools"}
            )
            with self.assertRaises(CodingCliError) as raised:
                runner.run_json_task("Return JSON.")

        self.assertEqual(raised.exception.code, "coding_cli.incompatible")
        self.assertEqual(invoked.call_count, 1)
        self.assertTrue(_is_opencode_compatibility_probe(invoked.call_args.args[0]))

    def test_each_cli_receives_its_documented_noninteractive_and_model_flags(self):
        expected_flags = {
            "codex": ("exec", "--ephemeral", "--model", "flavor-codex"),
            "claude": (
                "--print",
                "--safe-mode",
                "--no-session-persistence",
                "--tools",
                "--model",
                "flavor-claude",
            ),
            "cursor-agent": (
                "--print",
                "--yolo",
                "--sandbox",
                "--model",
                "flavor-cursor",
            ),
            "opencode": (
                "--pure",
                "run",
                "--print-logs",
                "--log-level",
                "ERROR",
                "--dir",
                "--agent",
                "build",
                "--format",
                "default",
                "--model",
                "flavor-opencode",
            ),
        }
        models = (
            ("codex", "flavor-codex"),
            ("claude", "flavor-claude"),
            ("cursor-agent", "flavor-cursor"),
            ("opencode", "flavor-opencode"),
        )
        for name, required in expected_flags.items():
            with (
                self.subTest(coding_cli=name),
                tempfile.TemporaryDirectory() as directory,
            ):
                output = Path(directory) / "generated"
                value = recipe(flavor("cpp", models=models))

                def execute(command, value=value, **kwargs):
                    if _is_opencode_compatibility_probe(command):
                        return _compatible_opencode_help_result()
                    workspace = Path(kwargs["cwd"])
                    source = workspace / "source" / "main.cpp"
                    source.parent.mkdir(parents=True)
                    source.write_text("int main() { return 0; }\n", encoding="utf-8")
                    write_generated_test_suite(workspace, value)
                    return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

                with (
                    mock.patch(
                        "literate_ai.adapters.models.coding_cli.shutil.which",
                        return_value=sys.executable,
                    ),
                    mock.patch(
                        "literate_ai.adapters.models.coding_cli._run_bounded",
                        side_effect=execute,
                    ) as invoked,
                ):
                    generator = CodingCliSourceGenerator(
                        environment={"CODING_CLI": name, "PATH": "/tools"}
                    )
                    result = generator.generate(
                        value,
                        output_root=output,
                    )
                command = tuple(invoked.call_args.args[0])
                for flag in required:
                    self.assertIn(flag, command)
                if name == "codex":
                    approval_position = command.index("--ask-for-approval")
                    self.assertEqual(command[approval_position + 1], "never")
                    self.assertLess(approval_position, command.index("exec"))
                self.assertEqual(
                    invoked.call_args.kwargs["maximum_stdout_bytes"],
                    coding_cli_adapter.DEFAULT_MAXIMUM_CLI_STDOUT_BYTES,
                )
                self.assertEqual(
                    invoked.call_args.kwargs["maximum_stderr_bytes"],
                    coding_cli_adapter.DEFAULT_MAXIMUM_GENERATION_CLI_STDERR_BYTES,
                )
                self.assertEqual(result.model, dict(models)[name])
                self.assertFalse(result.hermetic)
                self.assertIn("source/main.cpp", result.files)
                self.assertFalse(
                    (output / ".literate-ai-generation-request.md").exists()
                )

    def test_opencode_uses_detached_workspace_and_litai_owned_configuration(self):
        observed: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"
            value = recipe(flavor("cpp", models=(("opencode", "openai/gpt-fixture"),)))

            def execute(command, **kwargs):
                if _is_opencode_compatibility_probe(command):
                    return _compatible_opencode_help_result()
                workspace = Path(kwargs["cwd"])
                environment = dict(kwargs["environment"])
                observed.update(
                    command=tuple(command),
                    workspace=workspace,
                    environment=environment,
                    configuration_directory=Path(environment["OPENCODE_CONFIG_DIR"]),
                )
                self.assertTrue(observed["configuration_directory"].is_dir())
                self.assertTrue(output.is_dir())
                self.assertFalse(any(output.iterdir()))
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ) as invoked,
            ):
                result = CodingCliSourceGenerator(
                    environment={
                        "CODING_CLI": "opencode",
                        "PATH": "/tools",
                        "HOME": "/test/home",
                        "OPENCODE_API_KEY": "test-provider-secret",
                        "OPENCODE_CONFIG": "/untrusted/opencode.json",
                        "OPENCODE_PERMISSION": '{"bash":"allow"}',
                    }
                ).generate(value, output_root=output)
            materialized_source = (output / "source/main.cpp").read_text()

        workspace = observed["workspace"]
        environment = observed["environment"]
        command = observed["command"]
        self.assertIsInstance(workspace, Path)
        self.assertIsInstance(environment, dict)
        self.assertIsInstance(command, tuple)
        self.assertNotEqual(workspace, output.resolve())
        self.assertFalse(workspace.exists())
        self.assertFalse(observed["configuration_directory"].exists())
        self.assertEqual(environment["PWD"], str(workspace))
        self.assertEqual(environment["OPENCODE_API_KEY"], "test-provider-secret")
        self.assertNotIn("OPENCODE_CONFIG", environment)
        self.assertEqual(environment["OPENCODE_DISABLE_PROJECT_CONFIG"], "true")
        self.assertEqual(environment["OPENCODE_DISABLE_DEFAULT_PLUGINS"], "true")
        self.assertEqual(environment["OPENCODE_DISABLE_LSP_DOWNLOAD"], "true")
        permission = json.loads(environment["OPENCODE_PERMISSION"])
        self.assertEqual(permission["*"], "deny")
        self.assertEqual(permission["read"], "allow")
        self.assertEqual(permission["edit"], "allow")
        self.assertEqual(permission["external_directory"], "deny")
        config = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"], permission)
        self.assertEqual(config["plugin"], [])
        self.assertEqual(config["mcp"], {})
        self.assertEqual(config["provider"], {})
        self.assertEqual(command[command.index("--dir") + 1], str(workspace))
        self.assertIn("--pure", command)
        self.assertEqual(command[command.index("--agent") + 1], "build")
        self.assertEqual(command[command.index("--model") + 1], "openai/gpt-fixture")
        self.assertEqual(
            result.isolation_profile, "opencode-pure-detached-workspace-tools-v1"
        )
        self.assertEqual(invoked.call_count, 2)
        self.assertTrue(
            _is_opencode_compatibility_probe(invoked.call_args_list[0].args[0])
        )
        self.assertFalse(result.hermetic)
        self.assertEqual(materialized_source, "int main() { return 0; }\n")

    def test_json_task_runner_records_exact_transcript_in_an_ephemeral_workspace(self):
        response = {"schema": "test-response@1", "value": 7}

        def execute(command, **kwargs):
            workspace = Path(kwargs["cwd"])
            request = workspace / ".literate-ai-task-request.md"
            self.assertEqual(request.read_text(), "Return the fixture response.\n")
            (workspace / ".literate-ai-task-response.json").write_text(
                json.dumps(response), encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout=b"task done", stderr=b"")

        with (
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                side_effect=execute,
            ) as invoked,
        ):
            result = CodingCliTaskRunner(
                environment={"CODING_CLI": "codex", "PATH": "/tools"}
            ).run_json_task("Return the fixture response.", model="test-model")

        self.assertEqual(result.response, response)
        self.assertEqual(result.stdout, "task done")
        self.assertEqual(result.model, "test-model")
        self.assertIn("--model", invoked.call_args.args[0])
        self.assertEqual(
            invoked.call_args.kwargs["maximum_stdout_bytes"],
            coding_cli_adapter.DEFAULT_MAXIMUM_CLI_STDOUT_BYTES,
        )
        self.assertEqual(
            invoked.call_args.kwargs["maximum_stderr_bytes"],
            coding_cli_adapter.DEFAULT_MAXIMUM_CLI_STDERR_BYTES,
        )
        self.assertTrue(result.request_identity.startswith("sha256:"))
        self.assertTrue(result.response_identity.startswith("sha256:"))

    def test_json_task_reports_codex_workspace_write_bootstrap_failure(self):
        with (
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=(
                        b"warning: Codex's Linux sandbox uses bubblewrap and needs "
                        b"access to create user namespaces.\n"
                        b"bwrap: loopback: Failed RTM_NEWADDR: "
                        b"Operation not permitted\n"
                    ),
                    stderr=b"",
                ),
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli."
                "_require_codex_linux_workspace_write_prerequisite"
            ),
        ):
            runner = CodingCliTaskRunner(
                environment={"CODING_CLI": "codex", "PATH": "/tools"}
            )
            with self.assertRaises(CodingCliError) as raised:
                runner.run_json_task("Return JSON.")

        self.assertEqual(
            raised.exception.code, "coding_cli.workspace_write_unavailable"
        )
        self.assertIn("user namespaces", raised.exception.message)

    def test_json_task_without_response_or_known_failure_stays_generic(self):
        with (
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=b"task completed without an output file",
                    stderr=b"",
                ),
            ),
        ):
            runner = CodingCliTaskRunner(
                environment={"CODING_CLI": "codex", "PATH": "/tools"}
            )
            with self.assertRaises(CodingCliError) as raised:
                runner.run_json_task("Return JSON.")

        self.assertEqual(raised.exception.code, "coding_cli.task_response_missing")

    def test_opencode_json_task_uses_the_bounded_transport(self):
        observed: dict[str, object] = {}

        def execute(command, **kwargs):
            if _is_opencode_compatibility_probe(command):
                return _compatible_opencode_help_result()
            workspace = Path(kwargs["cwd"])
            environment = dict(kwargs["environment"])
            observed.update(
                command=tuple(command),
                workspace=workspace,
                configuration_directory=Path(environment["OPENCODE_CONFIG_DIR"]),
            )
            self.assertTrue(observed["configuration_directory"].is_dir())
            (workspace / ".literate-ai-task-response.json").write_text(
                '{"schema":"test-response@1","value":9}', encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout=b"task done", stderr=b"")

        with (
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                side_effect=execute,
            ) as invoked,
        ):
            result = CodingCliTaskRunner(
                environment={"CODING_CLI": "opencode", "PATH": "/tools"}
            ).run_json_task("Return the fixture response.", model="openai/gpt-fixture")

        command = observed["command"]
        workspace = observed["workspace"]
        self.assertIsInstance(command, tuple)
        self.assertIsInstance(workspace, Path)
        self.assertEqual(
            command[:-1],
            (
                str(Path(sys.executable).resolve()),
                "--pure",
                "run",
                "--print-logs",
                "--log-level",
                "ERROR",
                "--dir",
                str(workspace),
                "--agent",
                "build",
                "--format",
                "default",
                "--model",
                "openai/gpt-fixture",
            ),
        )
        self.assertEqual(result.response["value"], 9)
        self.assertEqual(result.coding_cli, "opencode")
        self.assertEqual(invoked.call_count, 2)
        self.assertTrue(
            _is_opencode_compatibility_probe(invoked.call_args_list[0].args[0])
        )
        self.assertEqual(
            result.isolation.profile, "opencode-pure-detached-workspace-tools-v1"
        )
        self.assertFalse(workspace.exists())
        self.assertFalse(observed["configuration_directory"].exists())

    def test_json_task_runner_rejects_workspace_pollution_and_reports_auth(self):
        def pollute(command, **kwargs):
            del command
            workspace = Path(kwargs["cwd"])
            (workspace / ".literate-ai-task-response.json").write_text(
                '{"ok":true}', encoding="utf-8"
            )
            (workspace / "unrequested.txt").write_text("no", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with (
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                side_effect=pollute,
            ),
        ):
            runner = CodingCliTaskRunner(
                environment={"CODING_CLI": "codex", "PATH": "/tools"}
            )
            with self.assertRaises(CodingCliError) as polluted:
                runner.run_json_task("Return JSON.")
        self.assertEqual(polluted.exception.code, "coding_cli.task_workspace_polluted")

        with (
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                return_value=SimpleNamespace(
                    returncode=1,
                    stdout=b"Not logged in. Run `codex login` to continue.",
                    stderr=b"",
                ),
            ),
        ):
            runner = CodingCliTaskRunner(
                environment={"CODING_CLI": "codex", "PATH": "/tools"}
            )
            with self.assertRaises(CodingCliError) as authentication:
                runner.run_json_task("Return JSON.")
        self.assertEqual(
            authentication.exception.code, "coding_cli.authentication_required"
        )

    def test_each_cli_reports_recognizable_authentication_failures(self):
        cases = (
            (
                "codex",
                "stdout",
                b"\x1b[31mNot logged in. Run `codex login` to continue.\x1b[0m",
                "codex login",
                "CODEX_API_KEY",
            ),
            (
                "codex",
                "stderr",
                b"Authentication required: CODEX_ACCESS_TOKEN is missing.",
                "codex login",
                "CODEX_ACCESS_TOKEN",
            ),
            (
                "codex",
                "stderr",
                b"OPENAI_API_KEY is missing.",
                "codex login",
                "OPENAI_API_KEY",
            ),
            (
                "codex",
                "stderr",
                (
                    b"unexpected status 401 Unauthorized: Missing bearer or basic "
                    b"authentication in header"
                ),
                "codex login",
                "OPENAI_API_KEY",
            ),
            (
                "codex",
                "stderr",
                (
                    b"codex_api::endpoint::responses_websocket: failed to connect "
                    b"to websocket: HTTP error: 401 Unauthorized, url: "
                    b"wss://api.openai.com/v1/responses"
                ),
                "codex login",
                "OPENAI_API_KEY",
            ),
            (
                "codex",
                "stderr",
                b"CODEX_ACCESS_TOKEN is invalid: test-only-secret.",
                "codex login",
                "CODEX_ACCESS_TOKEN",
            ),
            (
                "codex",
                "stderr",
                (
                    b"Your access token could not be refreshed because your refresh "
                    b"token was already used. Please log out and sign in again."
                ),
                "codex login",
                "CODEX_ACCESS_TOKEN",
            ),
            (
                "claude",
                "stdout",
                b"Invalid API key. Please run /login.",
                "claude auth login",
                "ANTHROPIC_API_KEY",
            ),
            (
                "claude",
                "stderr",
                b"OAuth token has expired; authentication is required.",
                "claude auth login",
                "CLAUDE_CODE_OAUTH_TOKEN",
            ),
            (
                "claude",
                "stderr",
                b"ANTHROPIC_API_KEY is invalid: test-only-secret.",
                "claude auth login",
                "ANTHROPIC_API_KEY",
            ),
            (
                "cursor-agent",
                "stdout",
                b"Not authenticated. Run `cursor-agent login`.",
                "cursor-agent login",
                "CURSOR_API_KEY",
            ),
            (
                "opencode",
                "stderr",
                b'No API key found for provider "opencode".',
                "opencode auth login",
                "OPENCODE_API_KEY",
            ),
            (
                "cursor-agent",
                "stderr",
                b"Authentication required: CURSOR_API_KEY is missing.",
                "cursor-agent login",
                "CURSOR_API_KEY",
            ),
            (
                "cursor-agent",
                "stderr",
                b"CURSOR_API_KEY is invalid: test-only-secret.",
                "cursor-agent login",
                "CURSOR_API_KEY",
            ),
        )
        for name, stream, output_text, login, credential in cases:

            def execute(
                command,
                *,
                name=name,
                stream=stream,
                output_text=output_text,
                **_kwargs,
            ):
                if name == "opencode" and _is_opencode_compatibility_probe(command):
                    return _compatible_opencode_help_result()
                return SimpleNamespace(
                    returncode=1,
                    stdout=output_text if stream == "stdout" else b"",
                    stderr=output_text if stream == "stderr" else b"",
                )

            with (
                self.subTest(coding_cli=name, stream=stream),
                tempfile.TemporaryDirectory() as directory,
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={
                        "CODING_CLI": name,
                        "PATH": "/tools",
                        credential: "test-only-secret",
                    }
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(
                        recipe(flavor("cpp")),
                        output_root=Path(directory) / "generated",
                    )

            self.assertEqual(
                raised.exception.code, "coding_cli.authentication_required"
            )
            self.assertIn(login, raised.exception.message)
            self.assertIn(credential, raised.exception.message)
            self.assertIn("this machine", raised.exception.message)
            self.assertIn("environment", raised.exception.message)
            self.assertNotIn("test-only-secret", raised.exception.message)

    def test_exit_zero_authentication_output_without_source_is_still_explicit(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=b"Not authenticated. Run `cursor-agent login`.",
                    stderr=b"",
                ),
            ),
        ):
            generator = CodingCliSourceGenerator(
                environment={"CODING_CLI": "cursor-agent", "PATH": "/tools"}
            )
            with self.assertRaises(CodingCliError) as raised:
                generator.generate(
                    recipe(flavor("cpp")),
                    output_root=Path(directory) / "generated",
                )

        self.assertEqual(raised.exception.code, "coding_cli.authentication_required")

    def test_opaque_codex_failure_uses_login_status_as_authentication_fallback(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                side_effect=(
                    SimpleNamespace(returncode=1, stdout=b"", stderr=b"opaque failure"),
                    SimpleNamespace(
                        returncode=1,
                        stdout=b"",
                        stderr=b"",
                    ),
                ),
            ) as run_bounded,
        ):
            generator = CodingCliSourceGenerator(
                environment={"CODING_CLI": "codex", "PATH": "/tools"}
            )
            with self.assertRaises(CodingCliError) as raised:
                generator.generate(
                    recipe(flavor("cpp")),
                    output_root=Path(directory) / "generated",
                )

        self.assertEqual(raised.exception.code, "coding_cli.authentication_required")
        self.assertEqual(run_bounded.call_count, 2)
        self.assertEqual(
            run_bounded.call_args_list[1].args[0][-2:], ("login", "status")
        )

    def test_reused_codex_refresh_token_is_direct_authentication_failure(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=sys.executable,
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli._run_bounded",
                return_value=SimpleNamespace(
                    returncode=1,
                    stdout=b"",
                    stderr=(
                        b"Your access token could not be refreshed because your "
                        b"refresh token was already used. Please log out and sign "
                        b"in again."
                    ),
                ),
            ) as run_bounded,
        ):
            generator = CodingCliSourceGenerator(
                environment={"CODING_CLI": "codex", "PATH": "/tools"}
            )
            with self.assertRaises(CodingCliError) as raised:
                generator.generate(
                    recipe(flavor("cpp")),
                    output_root=Path(directory) / "generated",
                )

        self.assertEqual(raised.exception.code, "coding_cli.authentication_required")
        self.assertEqual(run_bounded.call_count, 1)

    def test_successful_tree_is_not_rejected_for_authentication_words_in_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"
            value = recipe(flavor("cpp"))

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(
                    returncode=0,
                    stdout=b"Implemented the authentication required by the spec.",
                    stderr=b"",
                )

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                result = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"}
                ).generate(value, output_root=output)

        self.assertIn("source/main.cpp", result.files)

    def test_framework_canonicalizes_generated_metadata_before_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"
            value = recipe(flavor("cpp"))

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                suite_path = workspace / GENERATED_TEST_SUITE_PATH
                suite_path.write_text(
                    json.dumps(json.loads(suite_path.read_text()), indent=2) + "\n",
                    encoding="utf-8",
                )
                sbom_path = workspace / CYCLONEDX_SOURCE_SBOM_PATH
                sbom_path.write_text(
                    json.dumps(json.loads(sbom_path.read_text()), indent=2) + "\n",
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                result = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                ).generate(value, output_root=output)

            canonical = result.files[CYCLONEDX_SOURCE_SBOM_PATH]
            self.assertEqual(
                canonical.encode("utf-8"),
                canonical_json_bytes(json.loads(canonical)),
            )
            self.assertEqual(
                (output / CYCLONEDX_SOURCE_SBOM_PATH).read_text(encoding="utf-8"),
                canonical,
            )
            canonical_suite = result.files[GENERATED_TEST_SUITE_PATH]
            self.assertEqual(
                canonical_suite.encode("utf-8"),
                canonical_json_bytes(json.loads(canonical_suite)),
            )
            self.assertEqual(
                (output / GENERATED_TEST_SUITE_PATH).read_text(encoding="utf-8"),
                canonical_suite,
            )

    def test_framework_authors_managed_source_sbom_when_model_reinterprets_it(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"
            value = recipe(flavor("cpp"))
            assert value.managed_sbom_graph is not None
            authority_components, authority_edges = (
                coding_cli_adapter._recipe_authority_sbom(value)
            )
            expected_bytes, _binding = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=value.managed_sbom_graph,
                additional_components=authority_components,
                additional_edges=authority_edges,
                composition_aggregate="complete",
            )

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                # Emulate a coding model that reinterprets the framework-authoritative
                # managed projection: keep the same components but rewrite the managed
                # dependency-kind classification the model must not invent.
                sbom_path = workspace / CYCLONEDX_SOURCE_SBOM_PATH
                corrupted = json.loads(sbom_path.read_text(encoding="utf-8"))
                for component in corrupted.get("components", ()):
                    for prop in component.get("properties", ()):
                        if prop.get("name") == "literate-ai:dependency-kind":
                            prop["value"] = "build"
                sbom_path.write_text(json.dumps(corrupted), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                result = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                ).generate(value, output_root=output)

            self.assertEqual(
                result.files[CYCLONEDX_SOURCE_SBOM_PATH],
                expected_bytes.decode("utf-8"),
            )
            self.assertEqual(
                (output / CYCLONEDX_SOURCE_SBOM_PATH).read_text(encoding="utf-8"),
                expected_bytes.decode("utf-8"),
            )

    def test_framework_repairs_authority_around_extra_cargo_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            value = recipe(flavor("cpp"))
            assert value.managed_sbom_graph is not None
            authority_components, authority_edges = (
                coding_cli_adapter._recipe_authority_sbom(value)
            )
            base_bytes, _binding = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=value.managed_sbom_graph,
                additional_components=authority_components,
                additional_edges=authority_edges,
                composition_aggregate="complete",
            )
            document = json.loads(base_bytes)
            root_ref = value.managed_sbom_graph.root_ref
            cargo_components = (
                {
                    "type": "library",
                    "bom-ref": "pkg:cargo/serde@1.0.219",
                    "name": "serde",
                    "version": "1.0.219",
                    "purl": "pkg:cargo/serde@1.0.219",
                    "properties": [
                        {"name": "literate-ai:dependency-kind", "value": "package"},
                        {"name": "literate-ai:dependency-scope", "value": "runtime"},
                    ],
                },
                {
                    "type": "library",
                    "bom-ref": "pkg:cargo/serde_derive@1.0.219",
                    "name": "serde_derive",
                    "version": "1.0.219",
                    "purl": "pkg:cargo/serde_derive@1.0.219",
                    "properties": [
                        {"name": "literate-ai:dependency-kind", "value": "package"},
                        {"name": "literate-ai:dependency-scope", "value": "build"},
                    ],
                },
                {
                    "type": "library",
                    "bom-ref": "pkg:generic/rules_cc",
                    "name": "rules_cc",
                    "purl": "pkg:generic/rules_cc",
                    "versionRange": "vers:generic/>=0.2.22",
                    "isExternal": True,
                    "properties": [
                        {"name": "literate-ai:dependency-kind", "value": "build"},
                        {"name": "literate-ai:dependency-scope", "value": "build"},
                        {
                            "name": "literate-ai:bzlmod-requested-version",
                            "value": "0.2.22",
                        },
                    ],
                },
            )
            document["components"].extend(cargo_components)
            document["metadata"]["component"]["type"] = "library"
            document["metadata"]["component"]["properties"] = []
            authority_ref = authority_components[0]["bom-ref"]
            document["components"] = [
                component
                for component in document["components"]
                if component["bom-ref"] != authority_ref
            ]
            dependencies = {
                item["ref"]: item["dependsOn"] for item in document["dependencies"]
            }
            dependencies.pop(authority_ref)
            dependencies[root_ref] = [
                "pkg:cargo/serde@1.0.219",
                "pkg:generic/rules_cc",
            ]
            dependencies["pkg:cargo/serde@1.0.219"] = ["pkg:cargo/serde_derive@1.0.219"]
            dependencies["pkg:cargo/serde_derive@1.0.219"] = []
            dependencies["pkg:generic/rules_cc"] = []
            document["dependencies"] = [
                {"ref": ref, "dependsOn": targets}
                for ref, targets in sorted(dependencies.items())
            ]
            model_text = json.dumps(document)
            sbom_path = workspace / CYCLONEDX_SOURCE_SBOM_PATH
            sbom_path.parent.mkdir(parents=True, exist_ok=True)
            sbom_path.write_text(model_text, encoding="utf-8")
            files = {CYCLONEDX_SOURCE_SBOM_PATH: model_text}

            coding_cli_adapter._reconcile_authoritative_source_sbom(
                value, workspace, files
            )

            repaired = json.loads(files[CYCLONEDX_SOURCE_SBOM_PATH])
            self.assertEqual(repaired["metadata"]["component"]["type"], "application")
            inventory = {
                component["bom-ref"]: component for component in repaired["components"]
            }
            for component in cargo_components:
                self.assertEqual(inventory[component["bom-ref"]], component)
            for component in authority_components:
                self.assertEqual(inventory[component["bom-ref"]], component)
            graph = {
                item["ref"]: item["dependsOn"] for item in repaired["dependencies"]
            }
            self.assertIn("pkg:cargo/serde@1.0.219", graph[root_ref])
            self.assertEqual(
                graph["pkg:cargo/serde@1.0.219"],
                ["pkg:cargo/serde_derive@1.0.219"],
            )
            for source, target in authority_edges:
                self.assertIn(target, graph[source])
            self.assertEqual(
                sbom_path.read_text(encoding="utf-8"), files[CYCLONEDX_SOURCE_SBOM_PATH]
            )

    def test_framework_repairs_model_authored_source_composition(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            value = recipe(flavor("cpp"))
            assert value.managed_sbom_graph is not None
            authority_components, authority_edges = (
                coding_cli_adapter._recipe_authority_sbom(value)
            )
            base_bytes, _binding = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=value.managed_sbom_graph,
                additional_components=authority_components,
                additional_edges=authority_edges,
                composition_aggregate="complete",
            )
            document = json.loads(base_bytes)
            document["compositions"] = [
                {
                    "aggregate": "incomplete_third_party_only",
                    "dependencies": [value.managed_sbom_graph.root_ref],
                }
            ]
            model_text = json.dumps(document)
            sbom_path = workspace / CYCLONEDX_SOURCE_SBOM_PATH
            sbom_path.parent.mkdir(parents=True, exist_ok=True)
            sbom_path.write_text(model_text, encoding="utf-8")
            files = {CYCLONEDX_SOURCE_SBOM_PATH: model_text}

            coding_cli_adapter._reconcile_authoritative_source_sbom(
                value, workspace, files
            )

            repaired = json.loads(files[CYCLONEDX_SOURCE_SBOM_PATH])
            self.assertEqual(
                repaired["compositions"],
                [
                    {
                        "aggregate": "complete",
                        "dependencies": [value.managed_sbom_graph.root_ref],
                    }
                ],
            )
            self.assertEqual(
                sbom_path.read_text(encoding="utf-8"),
                files[CYCLONEDX_SOURCE_SBOM_PATH],
            )

    def test_framework_preserves_deferred_bzlmod_source_composition(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            value = recipe(flavor("cpp"))
            assert value.managed_sbom_graph is not None
            authority_components, authority_edges = (
                coding_cli_adapter._recipe_authority_sbom(value)
            )
            base_bytes, _binding = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=value.managed_sbom_graph,
                additional_components=authority_components,
                additional_edges=authority_edges,
                composition_aggregate="incomplete_third_party_only",
            )
            model_text = base_bytes.decode("utf-8")
            sbom_path = workspace / CYCLONEDX_SOURCE_SBOM_PATH
            sbom_path.parent.mkdir(parents=True, exist_ok=True)
            sbom_path.write_text(model_text, encoding="utf-8")
            files = {
                CYCLONEDX_SOURCE_SBOM_PATH: model_text,
                "source/MODULE.bazel": (
                    'module(name = "example", version = "1.0.0")\n'
                    'bazel_dep(name = "rules_cc", version = "0.2.22")\n'
                ),
            }

            coding_cli_adapter._reconcile_authoritative_source_sbom(
                value, workspace, files
            )

            repaired = json.loads(files[CYCLONEDX_SOURCE_SBOM_PATH])
            self.assertEqual(
                repaired["compositions"],
                [
                    {
                        "aggregate": "incomplete_third_party_only",
                        "dependencies": [value.managed_sbom_graph.root_ref],
                    }
                ],
            )

    def test_framework_rejects_conflicting_or_unsafe_third_party_sbom_changes(self):
        value = recipe(flavor("cpp"))
        assert value.managed_sbom_graph is not None
        authority_components, authority_edges = (
            coding_cli_adapter._recipe_authority_sbom(value)
        )
        base_bytes, _binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=value.managed_sbom_graph,
            additional_components=authority_components,
            additional_edges=authority_edges,
            composition_aggregate="complete",
        )

        def add_package(document):
            document["components"].append(
                {
                    "type": "library",
                    "bom-ref": "pkg:cargo/serde@1.0.219",
                    "name": "serde",
                    "version": "1.0.219",
                    "properties": [
                        {"name": "literate-ai:dependency-kind", "value": "package"},
                        {"name": "literate-ai:dependency-scope", "value": "runtime"},
                    ],
                }
            )
            document["dependencies"].append(
                {"ref": "pkg:cargo/serde@1.0.219", "dependsOn": []}
            )
            root = next(
                item
                for item in document["dependencies"]
                if item["ref"] == value.managed_sbom_graph.root_ref
            )
            root["dependsOn"].append("pkg:cargo/serde@1.0.219")

        for unsafe_change in (
            "duplicate-edge-record",
            "managed-authority-claim",
            "third-party-reparents-authority",
        ):
            with (
                self.subTest(unsafe_change=unsafe_change),
                tempfile.TemporaryDirectory() as directory,
            ):
                workspace = Path(directory)
                document = json.loads(base_bytes)
                add_package(document)
                if unsafe_change == "duplicate-edge-record":
                    document["dependencies"].append(
                        {"ref": "pkg:cargo/serde@1.0.219", "dependsOn": []}
                    )
                elif unsafe_change == "managed-authority-claim":
                    document["components"][-1]["properties"].append(
                        {
                            "name": "literate-ai:component-revision",
                            "value": "sha256:" + "f" * 64,
                        }
                    )
                else:
                    package_edge = next(
                        item
                        for item in document["dependencies"]
                        if item["ref"] == "pkg:cargo/serde@1.0.219"
                    )
                    package_edge["dependsOn"].append(authority_components[0]["bom-ref"])
                model_text = json.dumps(document)
                sbom_path = workspace / CYCLONEDX_SOURCE_SBOM_PATH
                sbom_path.parent.mkdir(parents=True, exist_ok=True)
                sbom_path.write_text(model_text, encoding="utf-8")
                files = {CYCLONEDX_SOURCE_SBOM_PATH: model_text}

                with self.assertRaises(CodingCliError) as raised:
                    coding_cli_adapter._reconcile_authoritative_source_sbom(
                        value, workspace, files
                    )

                self.assertEqual(
                    raised.exception.code, "coding_cli.generated_metadata_invalid"
                )
                self.assertEqual(files[CYCLONEDX_SOURCE_SBOM_PATH], model_text)
                self.assertEqual(sbom_path.read_text(encoding="utf-8"), model_text)

    def test_framework_authors_managed_sbom_with_component_dependency(self):
        # A Component that declares a `requires` edge has a managed graph with more
        # than the root: the depended-on Component is itself a managed component. The
        # reconciler must still author that multi-component projection deterministically
        # rather than treating the dependency as an unexpected extra ref.
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root_identity = ContentIdentity.parse_uri("sha256:" + "a" * 64)
            root_ref = component_bom_ref(root_identity)
            dependency_identity = ContentIdentity.parse_uri("sha256:" + "b" * 64)
            dependency_ref = component_bom_ref(dependency_identity)
            managed_graph = CycloneDxManagedGraph(
                root_ref,
                (
                    CycloneDxManagedComponent(
                        root_ref,
                        ManagedComponentKind.ROOT,
                        root_identity,
                        "urn:literate-ai:component:test/root",
                        "1.0.0",
                        (),
                    ),
                    CycloneDxManagedComponent(
                        dependency_ref,
                        ManagedComponentKind.COMPONENT,
                        dependency_identity,
                        "urn:literate-ai:component:test/dependency",
                        "1.0.0",
                        ("runtime",),
                    ),
                ),
                (
                    CycloneDxManagedEdge(
                        root_ref,
                        dependency_ref,
                        DependencyKind.RUNTIME,
                    ),
                ),
                ContentIdentity.parse_uri("sha256:" + "c" * 64),
            )
            value = GenerationRecipe(
                "hello-recipe",
                "hello",
                (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
                TEST_COMPONENT_LOCK_IDENTITY,
                (flavor("cpp"),),
                "source/main.cpp",
                (("codex", "base-codex-model"),),
                (generation_skill(),),
                managed_sbom_graph=managed_graph,
            )
            authority_components, authority_edges = (
                coding_cli_adapter._recipe_authority_sbom(value)
            )
            expected_bytes, _binding = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=managed_graph,
                additional_components=authority_components,
                additional_edges=authority_edges,
                composition_aggregate="complete",
            )
            # Emulate a model that keeps the managed component set (root + dependency +
            # authority) but rewrites the managed classification it must not invent.
            corrupted = json.loads(expected_bytes)
            for component in corrupted.get("components", ()):
                for prop in component.get("properties", ()):
                    if prop.get("name") == "literate-ai:dependency-kind":
                        prop["value"] = "build"
            model_text = json.dumps(corrupted)
            sbom_path = workspace / CYCLONEDX_SOURCE_SBOM_PATH
            sbom_path.parent.mkdir(parents=True, exist_ok=True)
            sbom_path.write_text(model_text, encoding="utf-8")
            files = {CYCLONEDX_SOURCE_SBOM_PATH: model_text}

            coding_cli_adapter._reconcile_authoritative_source_sbom(
                value, workspace, files
            )

            self.assertEqual(
                files[CYCLONEDX_SOURCE_SBOM_PATH],
                expected_bytes.decode("utf-8"),
            )
            self.assertEqual(
                sbom_path.read_text(encoding="utf-8"),
                expected_bytes.decode("utf-8"),
            )

    def test_framework_metadata_normalization_never_collapses_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            files = {CYCLONEDX_SOURCE_SBOM_PATH: '{"version":1,"version":2}'}
            with self.assertRaises(CodingCliError) as raised:
                coding_cli_adapter._canonicalize_generated_framework_metadata(
                    Path(directory), files, CYCLONEDX_SOURCE_SBOM_PATH
                )
        self.assertEqual(raised.exception.code, "coding_cli.generated_metadata_invalid")

    def test_manifest_numeric_guidance_preserves_canonical_refusal(self):
        prompt = recipe(flavor("python")).prompt()
        self.assertIn("`-9223372036854775808` through `9223372036854775807`", prompt)
        self.assertIn("These bounds apply to framework metadata", prompt)
        self.assertIn("according to the specifications", prompt)
        for number in (-(2**63) - 1, 2**63, 100000000000000000001):
            with (
                self.subTest(number=number),
                tempfile.TemporaryDirectory() as directory,
            ):
                content = json.dumps({"cases": [{"arguments": [number]}]})
                files = {GENERATED_TEST_SUITE_PATH: content}
                with self.assertRaises(CodingCliError) as raised:
                    coding_cli_adapter._canonicalize_generated_framework_metadata(
                        Path(directory), files, GENERATED_TEST_SUITE_PATH
                    )
                self.assertEqual(
                    raised.exception.code, "coding_cli.generated_metadata_invalid"
                )
                self.assertEqual(files[GENERATED_TEST_SUITE_PATH], content)
                self.assertFalse((Path(directory) / GENERATED_TEST_SUITE_PATH).exists())

    def test_manifest_canonical_integer_boundaries_remain_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / GENERATED_TEST_SUITE_PATH
            path.parent.mkdir(parents=True)
            value = {"cases": [{"arguments": [-(2**63), 2**63 - 1]}]}
            content = json.dumps(value)
            path.write_text(content, encoding="utf-8")
            files = {GENERATED_TEST_SUITE_PATH: content}
            coding_cli_adapter._canonicalize_generated_framework_metadata(
                root, files, GENERATED_TEST_SUITE_PATH
            )
            self.assertEqual(files[GENERATED_TEST_SUITE_PATH], path.read_text())
            self.assertEqual(json.loads(path.read_text()), value)

    def test_framework_metadata_normalization_sorts_bom_edges_without_repair(self):
        value = {
            "dependencies": [
                {"ref": "z", "dependsOn": ["c", "a"]},
                {"ref": "a", "dependsOn": []},
            ]
        }
        coding_cli_adapter._normalize_generated_cyclonedx_order(value)
        self.assertEqual(
            value["dependencies"],
            [
                {"ref": "a", "dependsOn": []},
                {"ref": "z", "dependsOn": ["a", "c"]},
            ],
        )

    def test_arbitrary_nonzero_exit_remains_generation_failed_for_each_cli(self):
        credential_names = {
            "codex": "OPENAI_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
            "cursor-agent": "CURSOR_API_KEY",
            "opencode": "OPENCODE_API_KEY",
        }
        for name, credential_name in credential_names.items():
            credential = f"test-only-forwarded-secret-{name}"

            def execute(command, *, name=name, credential=credential, **_kwargs):
                if name == "opencode" and _is_opencode_compatibility_probe(command):
                    return _compatible_opencode_help_result()
                return SimpleNamespace(
                    returncode=7,
                    stdout=(f"safe provider failure; credential={credential}").encode(),
                    stderr=(f"safe diagnostic tail; credential={credential}").encode(),
                )

            with (
                self.subTest(coding_cli=name),
                tempfile.TemporaryDirectory() as directory,
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli."
                    "_require_codex_linux_workspace_write_prerequisite"
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={
                        "CODING_CLI": name,
                        "PATH": "/tools",
                        credential_name: credential,
                    }
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(
                        recipe(flavor("cpp")),
                        output_root=Path(directory) / "generated",
                    )

            self.assertEqual(raised.exception.code, "coding_cli.generation_failed")
            self.assertIn(name, raised.exception.message)
            self.assertIn("exit status 7", raised.exception.message)
            self.assertIn("local diagnostics", raised.exception.message)
            self.assertIn("safe diagnostic tail", raised.exception.message)
            self.assertNotIn(credential, raised.exception.message)
            self.assertNotIn(credential, str(raised.exception))

    def test_generation_failure_retains_redacted_full_transcript_in_ambient_run(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            run = open_run(project, operation="samples")
            assert run is not None
            credential = "test-only-coding-cli-secret"

            def execute(command, **_kwargs):
                return SimpleNamespace(
                    returncode=7,
                    stdout=f"safe stdout; credential={credential}\n".encode(),
                    stderr=f"safe stderr; credential={credential}\n".encode(),
                )

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "LITAI_EVIDENCE_RUN": str(run.root),
                        "LITAI_EVIDENCE_PARENT": "",
                    },
                    clear=False,
                ),
                tempfile.TemporaryDirectory() as output_directory,
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli."
                    "_require_codex_linux_workspace_write_prerequisite"
                ),
            ):
                with self.assertRaises(CodingCliError) as raised:
                    CodingCliSourceGenerator(
                        environment={
                            "CODING_CLI": "codex",
                            "PATH": "/tools",
                            "CODEX_API_KEY": credential,
                        }
                    ).generate(
                        recipe(flavor("cpp")),
                        output_root=Path(output_directory) / "generated",
                    )

            self.assertIn("safe stderr", raised.exception.message)
            self.assertNotIn(credential, raised.exception.message)
            nodes = run.reduced()["nodes"]
            failure = next(
                node for node in nodes if node["path"] == "coding-cli/codex/failure"
            )
            transcript = next(
                output
                for output in failure["outputs"]
                if output["role"] == "transcript"
            )
            transcript_path = run.root / transcript["path"]
            self.assertEqual(
                transcript_path.read_text(encoding="utf-8"),
                "safe stdout; credential=<redacted>\n\n"
                "safe stderr; credential=<redacted>\n",
            )
            self.assertNotIn(credential, transcript_path.read_text(encoding="utf-8"))

    def test_supported_provider_credentials_cross_the_scrubbed_boundary(self):
        credentials = {
            "codex": {
                "CODEX_API_KEY": "test-codex-api-key",
                "CODEX_ACCESS_TOKEN": "test-codex-access-token",
                "CODEX_CA_CERTIFICATE": "/test/codex-ca.pem",
                "OPENAI_API_KEY": "test-openai-api-key",
            },
            "claude": {
                "CLAUDE_CODE_OAUTH_TOKEN": "test-claude-oauth-token",
                "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "test-claude-refresh-token",
                "CLAUDE_CODE_OAUTH_SCOPES": "user:inference",
                "CLAUDE_CONFIG_DIR": "/test/claude-config",
                "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "300000",
                "ANTHROPIC_API_KEY": "test-anthropic-api-key",
                "ANTHROPIC_AUTH_TOKEN": "test-anthropic-auth-token",
                "AWS_BEARER_TOKEN_BEDROCK": "test-bedrock-token",
            },
            "cursor-agent": {"CURSOR_API_KEY": "test-cursor-api-key"},
            "opencode": {
                "ANTHROPIC_API_KEY": "test-opencode-anthropic-api-key",
                "GEMINI_API_KEY": "test-opencode-gemini-api-key",
                "NVIDIA_API_KEY": "test-opencode-nvidia-api-key",
                "OPENCODE_API_KEY": "test-opencode-api-key",
                "OPENROUTER_API_KEY": "test-opencode-openrouter-api-key",
            },
        }
        for name, provider_environment in credentials.items():
            observed: dict[str, str] = {}
            with (
                self.subTest(coding_cli=name),
                tempfile.TemporaryDirectory() as directory,
            ):
                output = Path(directory) / "generated"
                value = recipe(flavor("cpp"))

                def execute(command, *, observed=observed, value=value, **kwargs):
                    if _is_opencode_compatibility_probe(command):
                        return _compatible_opencode_help_result()
                    observed.update(kwargs["environment"])
                    source = Path(kwargs["cwd"]) / "source" / "main.cpp"
                    source.parent.mkdir(parents=True)
                    source.write_text("int main() { return 0; }\n", encoding="utf-8")
                    write_generated_test_suite(Path(kwargs["cwd"]), value)
                    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

                with (
                    mock.patch(
                        "literate_ai.adapters.models.coding_cli.shutil.which",
                        return_value=sys.executable,
                    ),
                    mock.patch(
                        "literate_ai.adapters.models.coding_cli._run_bounded",
                        side_effect=execute,
                    ),
                ):
                    CodingCliSourceGenerator(
                        environment={
                            "CODING_CLI": name,
                            "PATH": "/tools",
                            "UNRELATED_SECRET": "must-not-cross-boundary",
                            **provider_environment,
                        }
                    ).generate(value, output_root=output)

            for key, value in provider_environment.items():
                self.assertEqual(observed[key], value)
            self.assertNotIn("UNRELATED_SECRET", observed)

    def test_subprocess_environment_is_scrubbed_and_pwd_is_the_workspace(self):
        observed: dict[str, str] = {}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"
            value = recipe(flavor("cpp"))

            def execute(command, **kwargs):
                del command
                observed.update(kwargs["environment"])
                source = Path(kwargs["cwd"]) / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(Path(kwargs["cwd"]), value)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                result = CodingCliSourceGenerator(
                    environment={
                        "CODING_CLI": "codex",
                        "PATH": "/tools",
                        "PWD": "/private/source-repository",
                        "USER": "test-user",
                        "LOGNAME": "test-logname",
                        "OPENAI_API_KEY": "credential",
                        "UNRELATED_SECRET": "must-not-cross-boundary",
                    }
                ).generate(value, output_root=output)

        self.assertEqual(observed["PWD"], str(output.resolve()))
        self.assertEqual(observed["USER"], "test-user")
        self.assertEqual(observed["LOGNAME"], "test-logname")
        self.assertEqual(observed["OPENAI_API_KEY"], "credential")
        self.assertNotIn("CODING_CLI", observed)
        self.assertNotIn("UNRELATED_SECRET", observed)
        self.assertFalse(result.hermetic)
        self.assertIn("PWD", result.environment_keys)

    def test_windows_environment_selection_preserves_required_launcher_keys(self):
        from literate_ai.adapters.models import coding_cli

        selection = coding_cli_adapter.CodingCliSelection(
            "codex",
            sys.executable,
            "sha256:" + "a" * 64,
        )
        workspace = Path("C:/workspace")
        with mock.patch.object(coding_cli.os, "name", "nt"):
            selected = coding_cli._coding_cli_environment(
                {
                    "PATH": "C:\\tools",
                    "SYSTEMDRIVE": "C:",
                    "SYSTEMROOT": "C:\\Windows",
                    "COMSPEC": "C:\\Windows\\System32\\cmd.exe",
                    "unrelated_secret": "must-not-cross-boundary",
                },
                selection,
                workspace=workspace,
            )

        self.assertEqual(selected["SystemDrive"], "C:")
        self.assertEqual(selected["SystemRoot"], "C:\\Windows")
        self.assertEqual(selected["ComSpec"], "C:\\Windows\\System32\\cmd.exe")
        self.assertNotIn("unrelated_secret", selected)

    def test_codex_read_only_downgrade_is_an_explicit_capability_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout=b"sandbox: read-only\nI cannot create the files.\n",
                        stderr=b"",
                    ),
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli."
                    "_require_codex_linux_workspace_write_prerequisite"
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"}
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(
                        recipe(flavor("cpp")),
                        output_root=Path(directory) / "generated",
                    )

        self.assertEqual(
            raised.exception.code, "coding_cli.workspace_write_unavailable"
        )
        self.assertNotIn("I cannot create", str(raised.exception))

    def test_codex_linux_namespace_failure_is_an_explicit_capability_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout=(
                            b"warning: Codex's Linux sandbox uses bubblewrap and needs "
                            b"access to create user namespaces.\n"
                            b"bwrap: loopback: Failed RTM_NEWADDR: "
                            b"Operation not permitted\n"
                        ),
                        stderr=b"",
                    ),
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli."
                    "_require_codex_linux_workspace_write_prerequisite"
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"}
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(
                        recipe(flavor("cpp")),
                        output_root=Path(directory) / "generated",
                    )

        self.assertEqual(
            raised.exception.code, "coding_cli.workspace_write_unavailable"
        )
        self.assertIn("LITAI_CODEX_SANDBOX", str(raised.exception))

    def test_codex_vm_isolated_windows_command_disables_host_sandbox(self):
        selection = coding_cli_adapter.CodingCliSelection(
            "codex",
            sys.executable,
            "sha256:" + "a" * 64,
            "danger-full-access",
        )
        workspace = Path("C:/workspace")
        with mock.patch.object(coding_cli_adapter.os, "name", "nt"):
            command = coding_cli_adapter._coding_cli_command(
                selection,
                workspace,
                model=None,
                prompt="Generate source.",
            )

        sandbox_position = command.index("--sandbox")
        self.assertEqual(command[sandbox_position + 1], "danger-full-access")
        approval_position = command.index("--ask-for-approval")
        self.assertEqual(command[approval_position + 1], "never")
        self.assertLess(approval_position, command.index("exec"))
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(selection.isolation.filesystem_boundary, "external-runner-vm")

    def test_bounded_stream_reader_kills_on_stdout_or_stderr_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            for stream in ("stdout", "stderr"):
                with self.subTest(stream=stream):
                    script = (
                        "import sys; "
                        f"sys.{stream}.buffer.write(b'x' * 4096); "
                        f"sys.{stream}.flush()"
                    )
                    with self.assertRaises(CodingCliError) as raised:
                        coding_cli_adapter._run_bounded(
                            (sys.executable, "-c", script),
                            cwd=Path(directory),
                            environment={},
                            timeout_seconds=5,
                            maximum_stdout_bytes=32,
                            maximum_stderr_bytes=32,
                        )
                    self.assertEqual(raised.exception.code, "coding_cli.output_limit")

    def test_bounded_stream_reader_accepts_normal_source_generation_progress(self):
        progress_bytes = (
            coding_cli_adapter.DEFAULT_MAXIMUM_GENERATION_CLI_STDERR_BYTES + 1
        )
        configured_limit = 64 * 1024 * 1024
        with tempfile.TemporaryDirectory() as directory:
            completed = coding_cli_adapter._run_bounded(
                (
                    sys.executable,
                    "-c",
                    "import sys; "
                    f"sys.stderr.buffer.write(b'x' * {progress_bytes}); "
                    "sys.stderr.flush()",
                ),
                cwd=Path(directory),
                environment={},
                timeout_seconds=5,
                maximum_stdout_bytes=(
                    coding_cli_adapter.DEFAULT_MAXIMUM_CLI_STDOUT_BYTES
                ),
                maximum_stderr_bytes=configured_limit,
            )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(len(completed.stderr), progress_bytes)

    def test_bounded_stream_reader_rejects_configured_stderr_overflow(self):
        configured_limit = 64 * 1024
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CodingCliError) as raised:
                coding_cli_adapter._run_bounded(
                    (
                        sys.executable,
                        "-c",
                        "import sys; "
                        f"sys.stderr.buffer.write(b'x' * {configured_limit + 1}); "
                        "sys.stderr.flush()",
                    ),
                    cwd=Path(directory),
                    environment={},
                    timeout_seconds=5,
                    maximum_stdout_bytes=(
                        coding_cli_adapter.DEFAULT_MAXIMUM_CLI_STDOUT_BYTES
                    ),
                    maximum_stderr_bytes=configured_limit,
                )

        self.assertEqual(raised.exception.code, "coding_cli.output_limit")

    def test_timeout_kills_coding_cli_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "descendant-survived"
            child = root / "child.py"
            child.write_text(
                "import pathlib, sys, time\n"
                "time.sleep(1)\n"
                "pathlib.Path(sys.argv[1]).write_text('alive')\n",
                encoding="utf-8",
            )
            parent = (
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
                "time.sleep(30)\n"
            )
            with self.assertRaises(CodingCliError) as raised:
                coding_cli_adapter._run_bounded(
                    (sys.executable, "-c", parent, str(child), str(sentinel)),
                    cwd=root,
                    environment={},
                    timeout_seconds=0.2,
                    maximum_stdout_bytes=1024,
                    maximum_stderr_bytes=1024,
                )
            self.assertEqual(raised.exception.code, "coding_cli.timeout")
            time.sleep(1.1)
            self.assertFalse(sentinel.exists(), "coding CLI descendant escaped timeout")

    def test_executable_drift_after_invocation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            executable = temporary / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            output = temporary / "generated"

            def execute(command, **kwargs):
                del command
                source = Path(kwargs["cwd"]) / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=str(executable),
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": str(temporary)}
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(recipe(flavor("cpp")), output_root=output)
        self.assertEqual(raised.exception.code, "coding_cli.executable_drift")

    def test_prompt_contains_base_and_selected_flavor_specs(self):
        value = recipe(flavor("cpp"))
        prompt = value.prompt()
        self.assertIn("Say hello.", prompt)
        self.assertIn("Generate cpp.", prompt)
        self.assertIn("implementation.language-ecosystem=cpp", prompt)
        self.assertIn("source/tests/manifest.json", prompt)
        self.assertIn("Generation mode: `major-rebuild`", prompt)
        self.assertIn(
            f"Component lock identity: `{value.component_lock_identity.uri}`",
            prompt,
        )
        self.assertIn("`openspec/spec.md`", prompt)
        self.assertIn("`cpp/spec.md`", prompt)
        self.assertIn("`source/tests/litai_test.cpp`", prompt)
        self.assertIn("linked into the runnable artifact", prompt)
        self.assertIn('`#include "litai_test.hpp"`', prompt)
        self.assertIn("never prefix it with `tests/`", prompt)
        self.assertIn("`*_test.cpp`", prompt)
        self.assertIn("Recheck all arithmetic", prompt)
        self.assertIn("Never guess a hash", prompt)
        self.assertIn(
            "Every manifest case must have a corresponding language-native behavior",
            prompt,
        )
        self.assertIn("it is lifecycle metadata", prompt)
        self.assertIn(
            "Any appended `authored-binary-asset` metadata identifies a",
            prompt,
        )
        self.assertIn("Do not create, infer, copy, replace,", prompt)
        self.assertIn("framework adds the verified bytes after", prompt)
        self.assertIn("`literate-ai:resolved-graph-identity`", prompt)
        self.assertIn(
            value.managed_sbom_graph.resolved_graph_identity.uri,
            prompt,
        )
        self.assertIn(
            canonical_json_bytes(value.managed_sbom_graph.to_dict()).decode("utf-8"),
            prompt,
        )

    def test_recipe_rejects_managed_graph_from_another_component_lock(self):
        value = recipe(flavor("cpp"))

        with self.assertRaisesRegex(ValueError, "managed SBOM graph identity"):
            replace(
                value,
                component_lock_identity=ContentIdentity.parse_uri("sha256:" + "d" * 64),
            )

    def test_prompt_embeds_complete_four_component_diamond_relationships(self):
        identities = {
            name: ContentIdentity.parse_uri("sha256:" + digit * 64)
            for name, digit in (
                ("root", "1"),
                ("left", "2"),
                ("right", "3"),
                ("leaf", "4"),
            )
        }
        refs = {
            name: component_bom_ref(identity) for name, identity in identities.items()
        }
        relationships = {
            name: ContentIdentity.parse_uri("sha256:" + digit * 64)
            for name, digit in (
                ("root-left", "5"),
                ("root-right", "6"),
                ("left-leaf", "7"),
                ("right-leaf", "8"),
            )
        }
        components = tuple(
            sorted(
                (
                    CycloneDxManagedComponent(
                        refs["root"],
                        ManagedComponentKind.ROOT,
                        identities["root"],
                        "urn:literate-ai:component:test/root",
                        "1.0.0",
                        (),
                    ),
                    CycloneDxManagedComponent(
                        refs["left"],
                        ManagedComponentKind.COMPONENT,
                        identities["left"],
                        "urn:literate-ai:component:test/left",
                        "1.0.0",
                        ("runtime",),
                    ),
                    CycloneDxManagedComponent(
                        refs["right"],
                        ManagedComponentKind.COMPONENT,
                        identities["right"],
                        "urn:literate-ai:component:test/right",
                        "1.0.0",
                        ("validation",),
                    ),
                    CycloneDxManagedComponent(
                        refs["leaf"],
                        ManagedComponentKind.COMPONENT,
                        identities["leaf"],
                        "urn:literate-ai:component:test/leaf",
                        "1.0.0",
                        ("build", "runtime"),
                    ),
                ),
                key=lambda item: item.bom_ref,
            )
        )
        edges = tuple(
            sorted(
                (
                    CycloneDxManagedEdge(
                        refs["root"],
                        refs["left"],
                        DependencyKind.RUNTIME,
                        False,
                        relationships["root-left"],
                    ),
                    CycloneDxManagedEdge(
                        refs["root"],
                        refs["right"],
                        DependencyKind.VALIDATION,
                        False,
                        relationships["root-right"],
                    ),
                    CycloneDxManagedEdge(
                        refs["left"],
                        refs["leaf"],
                        DependencyKind.RUNTIME,
                        False,
                        relationships["left-leaf"],
                    ),
                    CycloneDxManagedEdge(
                        refs["right"],
                        refs["leaf"],
                        DependencyKind.BUILD,
                        True,
                        relationships["right-leaf"],
                    ),
                )
            )
        )
        managed = CycloneDxManagedGraph(
            refs["root"],
            components,
            edges,
            ContentIdentity.parse_uri("sha256:" + "9" * 64),
        )
        value = recipe(flavor("cpp"))
        value = GenerationRecipe(
            value.recipe_id,
            value.application_id,
            value.documents,
            managed.resolved_graph_identity,
            value.flavors,
            value.required_entrypoint,
            value.models,
            value.skills,
            value.resolved_inputs,
            value.required_entrypoints,
            value.generation_mode,
            managed,
        )
        prompt = value.prompt()

        self.assertIn(canonical_json_bytes(managed.to_dict()).decode("utf-8"), prompt)
        authority_components, authority_edges = (
            coding_cli_adapter._recipe_authority_sbom(value)
        )
        minimal_source_bom, _binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=managed,
            additional_components=authority_components,
            additional_edges=authority_edges,
        )
        self.assertIn(minimal_source_bom.decode("utf-8"), prompt)
        for relationship in relationships.values():
            self.assertIn(relationship.digest, prompt)

    def test_prompt_sbom_contains_exact_selected_flavor_and_skill(self):
        value = recipe(flavor("cpp"))
        components, edges = coding_cli_adapter._recipe_authority_sbom(value)

        self.assertEqual(
            {item["properties"][1]["value"] for item in components},
            {"flavor", "skill"},
        )
        self.assertEqual(
            set(edges),
            {
                (value.managed_sbom_graph.root_ref, item["bom-ref"])
                for item in components
            },
        )
        expected, _binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=value.managed_sbom_graph,
            additional_components=components,
            additional_edges=edges,
        )
        self.assertIn(expected.decode(), value.prompt())

        document = json.loads(expected)
        document["components"] = [
            item
            for item in document["components"]
            if item["properties"][1]["value"] != "skill"
        ]
        document["dependencies"] = [
            {
                **item,
                "dependsOn": [
                    target
                    for target in item["dependsOn"]
                    if not target.startswith("urn:literate-ai:skill:")
                ],
            }
            for item in document["dependencies"]
            if not item["ref"].startswith("urn:literate-ai:skill:")
        ]
        with self.assertRaisesRegex(CycloneDxBomError, "selected Flavor or skill"):
            coding_cli_adapter._require_recipe_authority_sbom(
                canonical_json_bytes(document), value
            )

    def test_multi_entrypoint_recipe_and_sbom_bind_every_deployment_unit(self):
        base = recipe(flavor("python"))
        units = (
            RecipeDeploymentUnit(
                "api",
                "portable-application",
                "api-unit",
                "source/main.py",
                canonical_identity({"entrypoint": "api"}).uri,
            ),
            RecipeDeploymentUnit(
                "worker",
                "portable-application",
                "worker-unit",
                "source/worker.py",
                canonical_identity({"entrypoint": "worker"}).uri,
            ),
        )
        value = replace(
            base,
            required_entrypoint=None,
            required_entrypoints=("source/main.py", "source/worker.py"),
            deployment_units=units,
        )

        components, edges = coding_cli_adapter._recipe_authority_sbom(value)
        unit_components = tuple(
            item
            for item in components
            if str(item["bom-ref"]).startswith("urn:literate-ai:deployment-unit:")
        )

        self.assertEqual(
            {item["name"] for item in unit_components},
            {"api-unit", "worker-unit"},
        )
        self.assertTrue(
            all(
                (value.managed_sbom_graph.root_ref, item["bom-ref"]) in edges
                for item in unit_components
            )
        )
        self.assertIn("## Required deployment units", value.prompt())
        self.assertIn('"deployment_unit":"worker-unit"', value.prompt())
        expected, _binding = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=value.managed_sbom_graph,
            additional_components=components,
            additional_edges=edges,
        )
        coding_cli_adapter._require_recipe_authority_sbom(expected, value)
        changed = replace(
            value,
            deployment_units=(
                units[0],
                replace(units[1], source_entrypoint="source/changed.py"),
            ),
            required_entrypoints=("source/main.py", "source/changed.py"),
        )
        self.assertNotEqual(value.identity, changed.identity)

    def test_generation_requires_the_disposable_test_suite(self):
        value = recipe(flavor("cpp"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"

            def execute(command, **kwargs):
                del command
                source = Path(kwargs["cwd"]) / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"}
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=output)
        self.assertEqual(raised.exception.code, "generated_tests.missing")

    def test_generation_rejects_candidate_with_another_callable_signature(self):
        acceptance_arguments = [{"name": "Ada", "messages": ["Hello"]}]
        value = GenerationRecipe(
            "acceptance-signature-recipe",
            "hello",
            (
                RecipeDocument.create("openspec/spec.md", "Build a greeting card.\n"),
                RecipeDocument.create(
                    "acceptance/execution.json",
                    json.dumps(
                        {
                            "invocations": [{"arguments": acceptance_arguments}],
                            "result_shape": {"greeting": "string"},
                        }
                    ),
                ),
            ),
            TEST_COMPONENT_LOCK_IDENTITY,
            (flavor("cpp"),),
            "source/main.cpp",
            skills=(generation_skill(),),
            managed_sbom_graph=recipe(flavor("cpp")).managed_sbom_graph,
        )
        suite = json.loads(generated_test_suite(value))
        for index, case in enumerate(suite["cases"]):
            case["arguments"] = [f"recipient-{index}", [f"message-{index}"]]
            case["expected_result"] = {"greeting": f"Hello {index}"}

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                manifest = workspace / GENERATED_TEST_SUITE_PATH
                manifest.parent.mkdir(parents=True)
                manifest.write_text(json.dumps(suite), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"}
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=output)

        self.assertEqual(
            raised.exception.code, "generated_tests.acceptance_signature_missing"
        )

    def test_prompt_exposes_only_exact_dependency_interfaces(self):
        value = GenerationRecipe(
            "dependency-bound",
            "hello",
            (
                RecipeDocument.create("openspec/spec.md", "Build the app.\n"),
                RecipeDocument.create(
                    "dependency-components/" + "a" * 64 + "/specifications/api.md",
                    "Private dependency implementation.\n",
                ),
                RecipeDocument.create(
                    "dependency-interfaces/" + "a" * 64 + "/api.md",
                    "Exact dependency API.\n",
                ),
            ),
            TEST_COMPONENT_LOCK_IDENTITY,
            skills=(generation_skill(),),
        )
        prompt = value.prompt()
        self.assertIn("## Exact direct dependency interfaces", prompt)
        self.assertIn("Exact dependency API.", prompt)
        self.assertNotIn("Private dependency implementation.", prompt)

    def test_exact_execution_plan_is_declared_by_the_subprocess_request(self):
        value = recipe(flavor("cpp"))
        plan = planned_execution(value)
        captured = ""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"

            def execute(command, **kwargs):
                nonlocal captured
                workspace = Path(kwargs["cwd"])
                captured = (workspace / ".literate-ai-generation-request.md").read_text(
                    encoding="utf-8"
                )
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"}
                )
                result = generator.generate(
                    value, output_root=output, execution_plan=plan
                )

        self.assertIn(plan.identity.uri, captured)
        self.assertIn("Plan every exact requirement.", captured)
        self.assertIn("Generate only from the approved plan.", captured)
        self.assertEqual(result.execution_plan_identity, plan.identity.uri)
        self.assertEqual(result.generation_mode, "major-rebuild")
        self.assertEqual(
            result.generated_test_suite_identity,
            "sha256:"
            + hashlib.sha256(
                canonical_json_bytes(json.loads(generated_test_suite(value)))
            ).hexdigest(),
        )
        self.assertIn(result.executable_identity, captured)
        self.assertIn(generator.selection.tool_binding_identity, captured)
        self.assertNotIn(result.coding_cli_selection_identity, captured)
        self.assertNotIn(generator.selection.executable, captured)
        self.assertEqual(result.requested_model_stages, ("plan", "generate"))
        self.assertEqual(
            result.requested_route_decision_digests,
            tuple(item.digest for item in plan.route_decisions),
        )

    def test_exact_bounded_component_prompt_reaches_the_coding_cli(self):
        value = recipe(flavor("cpp"))
        plan = planned_execution(value)
        bounded = (
            b"# Exact prepared Component context\n\n"
            b"## Bounded candidate-repair evidence\n"
            b"Failure: generated tests omitted attributable case results\n"
        )
        captured = ""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"

            def execute(command, **kwargs):
                nonlocal captured
                workspace = Path(kwargs["cwd"])
                captured = (workspace / ".literate-ai-generation-request.md").read_text(
                    encoding="utf-8"
                )
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_mode="off",
                )
                planned = generator.planned_request_identity(
                    value,
                    execution_plan=plan,
                    stage_request={},
                    bounded_prompt=bounded,
                )
                different = generator.planned_request_identity(
                    value,
                    execution_plan=plan,
                    stage_request={},
                    bounded_prompt=bounded + b"Different replacement.\n",
                )
                result = generator.generate(
                    value,
                    output_root=output,
                    execution_plan=plan,
                    stage_request={},
                    bounded_prompt=bounded,
                )

        self.assertIn(bounded.decode("utf-8"), captured)
        self.assertIn("Bounded candidate-repair evidence", captured)
        self.assertEqual(result.request_identity, planned.uri)
        self.assertNotEqual(planned, different)

    def test_generated_output_byte_budget_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"

            def execute(command, **kwargs):
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("x" * 17, encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    maximum_generated_bytes=16,
                )
                with self.assertRaisesRegex(CodingCliError, "too much source"):
                    generator.generate(recipe(flavor("cpp")), output_root=output)

    def test_prompt_and_recipe_identity_include_exact_ordered_skills(self):
        planner = generation_skill()
        implementation = generation_skill(
            "portable-implementation",
            stages=("generate",),
            dependencies=(planner,),
            instructions="Implement the plan portably.",
        )
        language = generation_skill(
            "cpp-implementation",
            stages=("generate",),
            dependencies=(implementation,),
            instructions="Use portable C++17.",
        )
        value = GenerationRecipe(
            "skill-bound-recipe",
            "hello",
            (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
            TEST_COMPONENT_LOCK_IDENTITY,
            (flavor("cpp", skills=(language,)),),
            "source/main.cpp",
            skills=(planner, implementation),
        )
        prompt = value.prompt()
        self.assertEqual(
            [item.skill_id for item in value.resolved_skills],
            [
                "specification-planning",
                "portable-implementation",
                "cpp-implementation",
            ],
        )
        for item in value.resolved_skills:
            self.assertIn(item.identity, prompt)
            self.assertIn(item.instructions, prompt)
        changed_planner = generation_skill(instructions="Plan every exact requirement.")
        changed_implementation = generation_skill(
            "portable-implementation",
            stages=("generate",),
            dependencies=(changed_planner,),
            instructions="Implement the plan portably.",
        )
        changed_language = generation_skill(
            "cpp-implementation",
            stages=("generate",),
            dependencies=(changed_implementation,),
            instructions="Use portable C++17.",
        )
        changed = GenerationRecipe(
            "skill-bound-recipe",
            "hello",
            (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
            TEST_COMPONENT_LOCK_IDENTITY,
            (flavor("cpp", skills=(changed_language,)),),
            "source/main.cpp",
            skills=(
                changed_planner,
                changed_implementation,
            ),
        )
        self.assertNotEqual(value.identity, changed.identity)

    def test_recipe_closes_catalog_dependencies_before_prompt_assembly(self):
        parent = generation_skill("layout", stages=("plan", "generate"))
        child = generation_skill(
            "implementation",
            stages=("generate",),
            dependencies=(parent,),
        )
        value = GenerationRecipe(
            "closed-recipe",
            "hello",
            (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
            TEST_COMPONENT_LOCK_IDENTITY,
            required_entrypoint="source/main.py",
            skills=(child,),
            skill_catalog=(parent, child),
        )
        self.assertEqual(
            [item.skill_id for item in value.resolved_skills],
            ["layout", "implementation"],
        )
        prompt = value.prompt()
        self.assertIn(parent.identity, prompt)
        self.assertIn(child.identity, prompt)
        selected_only = GenerationRecipe(
            "closed-recipe",
            "hello",
            (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
            TEST_COMPONENT_LOCK_IDENTITY,
            required_entrypoint="source/main.py",
            skills=(parent, child),
        )
        self.assertNotEqual(value.identity, selected_only.identity)

    def test_prompt_gives_bzlmod_source_nodes_one_exact_unresolved_shape(self):
        prompt = recipe(flavor("python")).prompt()

        self.assertIn("CycloneDX `type` is exactly `library`", prompt)
        self.assertIn("`isExternal` value is the JSON boolean `true`", prompt)
        self.assertIn("It MUST\nomit `version` and hashes", prompt)
        self.assertIn("`literate-ai:bzlmod-requested-version`", prompt)

    def test_selected_bazel_flavor_requires_complete_build_surface(self):
        language = flavor("cpp")
        bazel = RecipeFlavor(
            "bazel",
            "build.system",
            "bazel",
            (RecipeDocument.create("bazel/spec.md", "Prefer Bazel.\n"),),
            revision_identity=canonical_identity({"fixture_flavor": "bazel"}).uri,
            specification_set_identity=canonical_identity(
                {"fixture_flavor_spec": "bazel"}
            ).uri,
        )
        value = GenerationRecipe(
            "bazel-build-surface",
            "hello",
            (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
            TEST_COMPONENT_LOCK_IDENTITY,
            (language, bazel),
            "source/main.cpp",
            skills=(generation_skill(),),
            managed_sbom_graph=recipe(language).managed_sbom_graph,
        )

        prompt = value.prompt()

        self.assertIn("## Required generated Bazel source boundary", prompt)
        self.assertIn("MUST create both `source/MODULE.bazel`", prompt)
        self.assertIn("`source/BUILD.bazel`", prompt)
        self.assertIn("complete runnable `//:run` target", prompt)
        self.assertIn("real test targets", prompt)
        self.assertIn('"aggregate":"complete"', prompt)
        self.assertIn(
            "literal\nBzlmod `bazel_dep`, use `incomplete_third_party_only`", prompt
        )

    def test_rust_prompt_keeps_lifecycle_manifest_out_of_compiler_inputs(self):
        prompt = recipe(flavor("rust")).prompt()

        self.assertIn("`source/tests/litai_test.rs`", prompt)
        self.assertIn("list both files", prompt)
        self.assertIn("every `rust_binary` or `rust_test`", prompt)
        self.assertIn("sandboxes expose only declared inputs", prompt)
        self.assertIn("Do not hide the runtime test", prompt)
        self.assertIn("not Rust compile-time test data", prompt)
        self.assertIn("do not use `include_str!`", prompt)

    def test_skill_content_drift_and_unresolved_dependencies_fail_closed(self):
        skill = generation_skill()
        reference = ContentReference(
            "specification-to-source-skill",
            "skills/specification-planning.json",
            ContentIdentity.parse_uri(skill.identity),
        )
        with self.assertRaisesRegex(ContractValidationError, "identity changed"):
            RecipeSkill.from_reference(
                reference,
                b'{"schema":"urn:literate-ai:schema:v1:specification-to-source-skill"}\n',
                source="test fixture",
            )
        with self.assertRaisesRegex(CodingCliError, "requires 'missing-skill'"):
            GenerationRecipe(
                "missing-dependency",
                "hello",
                (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
                TEST_COMPONENT_LOCK_IDENTITY,
                skills=(
                    generation_skill(
                        "implementation",
                        stages=("generate",),
                        dependencies=(
                            SkillReference(
                                "missing-skill",
                                "1.0.0",
                                ContentIdentity.parse_uri("sha256:" + "0" * 64),
                            ),
                        ),
                    ),
                ),
            )

    def test_generation_recipe_rejects_an_unpinned_conversion(self):
        with self.assertRaisesRegex(ValueError, "pinned specification-to-source skill"):
            GenerationRecipe(
                "unpinned-recipe",
                "hello",
                (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
                TEST_COMPONENT_LOCK_IDENTITY,
            )

    def test_generation_recipe_requires_a_typed_component_lock_identity(self):
        with self.assertRaisesRegex(ValueError, "exact Component lock identity"):
            GenerationRecipe(
                "untyped-lock-recipe",
                "hello",
                (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
                "sha256:" + "c" * 64,  # type: ignore[arg-type]
                skills=(generation_skill(),),
            )

    def test_selected_flavors_cannot_disagree_on_provider_model(self):
        cpp = flavor("cpp", models=(("codex", "model-a"),))
        linux = RecipeFlavor(
            "linux",
            "platform.os",
            "linux",
            (RecipeDocument.create("linux/spec.md", "Use Linux.\n"),),
            (("codex", "model-b"),),
        )
        value = GenerationRecipe(
            "conflicting-models",
            "hello",
            (RecipeDocument.create("openspec/spec.md", "Say hello.\n"),),
            TEST_COMPONENT_LOCK_IDENTITY,
            (cpp, linux),
            skills=(generation_skill(),),
        )
        with self.assertRaisesRegex(CodingCliError, "disagree"):
            value.model_for("codex")

    def test_generation_rejects_even_empty_output_outside_source(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"
            attempts: list[int] = []

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                (workspace / "notes").mkdir()
                attempts.append(1)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"}
                )
                with self.assertRaisesRegex(CodingCliError, "outside source"):
                    generator.generate(recipe(flavor("cpp")), output_root=output)

            self.assertEqual(
                len(attempts), 1 + coding_cli_adapter._METADATA_INVALID_RETRY_ATTEMPTS
            )

    def test_unexpected_output_recovers_on_a_bounded_fresh_workspace_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))
            attempts: list[int] = []

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                attempts.append(1)
                if len(attempts) == 1:
                    unexpected = workspace / "_build" / "application"
                    unexpected.parent.mkdir()
                    unexpected.write_bytes(b"derived")
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generated = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                ).generate(value, output_root=root / "candidate")

            self.assertEqual(len(attempts), 2)
            self.assertEqual(generated.metadata_retry_count, 1)
            self.assertFalse((root / "candidate" / "_build").exists())

    def test_non_utf8_derived_output_recovers_on_a_bounded_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))
            attempts: list[int] = []

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                attempts.append(1)
                if len(attempts) == 1:
                    derived = workspace / "source" / "out" / "main.pyc"
                    derived.parent.mkdir()
                    derived.write_bytes(b"\xff\xfe")
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generated = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                ).generate(value, output_root=root / "candidate")

            self.assertEqual(len(attempts), 2)
            self.assertEqual(generated.metadata_retry_count, 1)
            self.assertFalse((root / "candidate" / "source" / "out").exists())

    def test_generated_metadata_invalid_recovers_on_a_bounded_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))
            attempts: list[int] = []

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                attempts.append(1)
                if len(attempts) == 1:
                    path = workspace / GENERATED_TEST_SUITE_PATH
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("{not valid json", encoding="utf-8")
                else:
                    write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generated = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                ).generate(value, output_root=root / "candidate")

            self.assertEqual(len(attempts), 2)
            self.assertEqual(generated.metadata_retry_count, 1)

    def test_incomplete_javascript_bundle_recovers_on_a_bounded_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))
            attempts: list[int] = []

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source"
                source.mkdir(parents=True)
                (source / "main.cpp").write_text(
                    "int main() { return 0; }\n", encoding="utf-8"
                )
                (source / "main.js").write_text(
                    "module.exports = 1;\n", encoding="utf-8"
                )
                bundle = source / "tools" / "bundle.js"
                bundle.parent.mkdir()
                attempts.append(1)
                module_id = "./missing.js" if len(attempts) == 1 else "./main.js"
                bundle.write_text(
                    "const __litaiModules = {"
                    f"'{module_id}': function(module) {{ module.exports = 1; }}"
                    "};\n",
                    encoding="utf-8",
                )
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generated = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                ).generate(value, output_root=root / "candidate")

            self.assertEqual(len(attempts), 2)
            self.assertEqual(generated.metadata_retry_count, 1)

    def test_generated_metadata_invalid_fails_closed_after_exhausting_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))
            attempts: list[int] = []

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                attempts.append(1)
                path = workspace / GENERATED_TEST_SUITE_PATH
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{not valid json", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=root / "candidate")

            self.assertEqual(
                raised.exception.code, "coding_cli.generated_metadata_invalid"
            )
            # One initial attempt plus the bounded retry count.
            self.assertEqual(
                len(attempts), 1 + coding_cli_adapter._METADATA_INVALID_RETRY_ATTEMPTS
            )

    def test_duplicate_generated_test_arguments_recover_on_a_bounded_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))
            attempts: list[int] = []

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                attempts.append(1)
                if len(attempts) == 1:
                    write_duplicate_argument_generated_test_suite(workspace, value)
                else:
                    write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generated = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                ).generate(value, output_root=root / "candidate")

            self.assertEqual(len(attempts), 2)
            self.assertEqual(generated.metadata_retry_count, 1)

    def test_duplicate_generated_test_arguments_fail_closed_after_exhausting_retries(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))
            attempts: list[int] = []

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                attempts.append(1)
                write_duplicate_argument_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=root / "candidate")

            self.assertEqual(
                raised.exception.code, "generated_tests.duplicate_arguments"
            )
            self.assertEqual(
                len(attempts), 1 + coding_cli_adapter._METADATA_INVALID_RETRY_ATTEMPTS
            )

    def test_quota_denial_falls_back_to_the_next_available_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))
            invoked_clis: list[str] = []

            def which(name, path=None):
                del path
                return sys.executable if name in ("codex", "claude") else None

            def execute(command, **kwargs):
                if tuple(command[-2:]) == ("login", "status"):
                    # The codex authentication-status probe: report healthy so
                    # the real spend-cap failure isn't misclassified as an
                    # authentication problem.
                    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
                cli = "codex" if len(invoked_clis) == 0 else "claude"
                invoked_clis.append(cli)
                workspace = Path(kwargs["cwd"])
                if cli == "codex":
                    return SimpleNamespace(
                        returncode=1,
                        stdout=b"",
                        stderr=(
                            b"You hit your spend cap set by the owner of your "
                            b"workspace. Ask an owner to increase your spend cap "
                            b"to continue."
                        ),
                    )
                source = workspace / "source" / "main.cpp"
                source.parent.mkdir(parents=True)
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    side_effect=which,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                )
                self.assertEqual(generator.selection.name, "codex")
                generated = generator.generate(value, output_root=root / "candidate")

            self.assertEqual(invoked_clis, ["codex", "claude"])
            self.assertEqual(generator.selection.name, "claude")
            self.assertIsNotNone(generated)

    def test_resolved_model_scope_disables_cross_provider_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owner = canonical_identity({"pipeline": "fallback-boundary"})
            scope = resolve_model_scope(
                scope_kind=ModelScopeKind.PIPELINE,
                owner_identity=owner,
                provider_id="codex",
                candidates=((owner, "pipeline-model"),),
            )
            value = replace(recipe(flavor("cpp")), model_scope=scope)

            def which(name, path=None):
                del path
                return sys.executable if name in ("codex", "claude") else None

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    side_effect=which,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    return_value=SimpleNamespace(
                        returncode=1,
                        stdout=b"",
                        stderr=(
                            b"Your access token could not be refreshed because your "
                            b"refresh token was already used. Please log out and sign "
                            b"in again."
                        ),
                    ),
                ) as run_bounded,
            ):
                generator = CodingCliSourceGenerator(
                    environment={"PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=root / "candidate")

            self.assertEqual(
                raised.exception.code, "coding_cli.authentication_required"
            )
            self.assertEqual(generator.selection.name, "codex")
            self.assertEqual(run_bounded.call_count, 1)

    def test_explicit_coding_cli_pin_disables_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))

            def which(name, path=None):
                del path
                return sys.executable if name in ("codex", "claude") else None

            def execute(command, **kwargs):
                del kwargs
                if tuple(command[-2:]) == ("login", "status"):
                    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
                return SimpleNamespace(
                    returncode=1,
                    stdout=b"",
                    stderr=(
                        b"You hit your spend cap set by the owner of your "
                        b"workspace. Ask an owner to increase your spend cap "
                        b"to continue."
                    ),
                )

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    side_effect=which,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"}
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=root / "candidate")

            self.assertEqual(raised.exception.code, "coding_cli.quota_denied")
            self.assertEqual(generator.selection.name, "codex")

    def test_quota_denial_reraises_original_error_when_fallback_is_exhausted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))

            def which(name, path=None):
                del path
                # Only codex is "installed" -- there is nothing to fall back to.
                return sys.executable if name == "codex" else None

            def execute(command, **kwargs):
                del kwargs
                if tuple(command[-2:]) == ("login", "status"):
                    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
                return SimpleNamespace(
                    returncode=1,
                    stdout=b"",
                    stderr=(
                        b"You hit your spend cap set by the owner of your "
                        b"workspace. Ask an owner to increase your spend cap "
                        b"to continue."
                    ),
                )

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    side_effect=which,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"PATH": "/tools"},
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=root / "candidate")

            self.assertEqual(raised.exception.code, "coding_cli.quota_denied")
            self.assertEqual(generator.selection.name, "codex")

    def test_a_different_error_code_is_never_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = recipe(flavor("cpp"))
            attempts: list[int] = []

            def execute(command, **kwargs):
                del command
                attempts.append(1)
                return SimpleNamespace(returncode=0, stdout=b"done", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_provider=TestSourceIntelligenceProvider(),
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=root / "candidate")

            self.assertEqual(raised.exception.code, "coding_cli.empty_generation")
            self.assertEqual(len(attempts), 1)


if __name__ == "__main__":
    unittest.main()
