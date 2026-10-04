"""Specification recipe and coding CLI adapter tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import literate_ai.adapters.models.coding_cli as coding_cli_adapter
from literate_ai.adapters.cache import (
    CachedCodingCliSourceGenerator,
    GeneratedSourceCacheError,
)
from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.models import (
    CodingCliError,
    CodingCliSourceGenerator,
    GenerationRecipe,
    RecipeDocument,
    RecipeFlavor,
    RecipeSkill,
    lifecycle_driver_environment,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    ContentIdentity,
    ContentReference,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    ManagedComponentKind,
    SkillReference,
    SourceIntelligenceArtifact,
    canonical_identity,
    component_bom_ref,
    generated_source_snapshot_identity,
    generated_source_tree_identity,
)
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


class CodingCliSelectionTests(unittest.TestCase):
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

    @unittest.skipIf(os.name == "nt", "POSIX symlink behavior")
    def test_publication_parent_symlink_cannot_escape_generated_cache(self):
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
            try:
                cached.entries.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"host cannot create directory symlinks: {error}")
            try:
                with self.assertRaisesRegex(GeneratedSourceCacheError, "unsafe"):
                    cached.accept(generation)
                self.assertEqual(tuple(outside.iterdir()), ())
            finally:
                cached.entries.unlink()

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


if __name__ == "__main__":
    unittest.main()
