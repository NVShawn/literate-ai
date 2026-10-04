from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

from literate_ai.adapters.cache import CachedCodingCliSourceGenerator
from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.adapters.models import (
    CODING_CLIS,
    CodingCliSourceGenerator,
    RecipeDocument,
    portable_source_entrypoint,
)
from literate_ai.adapters.specifications import OpenSpecProvider
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.contracts import (
    STANDARD_FULL_REBUILD_EVIDENCE_KINDS,
    ContentReference,
    CycloneDxManagedGraph,
    FlavorAxis,
    FlavorDefinition,
    ProjectTestReceiptPolicy,
    canonical_identity,
    canonical_json_bytes,
    load_current_standard_lifecycle_policy,
)
from literate_ai.generated_tests import (
    generated_test_invocation_signature,
)
from tests.conformance.support.sample_runner import (
    REPORT_SCHEMA,
    _execution_contract,
    _execution_variants,
    _ExecutionVariant,
    _load_sample,
    _sample_runtime_recipe,
    discover,
    execute_standard_sample_variant,
    plan_recipes,
    project_standard_sample_execution_report,
    project_standard_sample_test_receipt,
    run_all,
    sample_harness_manifest,
    sample_harness_oracle,
)
from tests.conformance.support.standard_service_stack import (
    execute_standard_service_stack,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = REPO_ROOT / "samples"
NATIVE_TOOLCHAIN_TEST_ENVIRONMENT = "LITERATE_AI_NATIVE_TOOLCHAIN_TESTS"


def native_toolchain_tests_enabled(
    environment: Mapping[str, str] = os.environ,
) -> bool:
    """Return whether this process explicitly authorizes native toolchain tests."""

    return environment.get(NATIVE_TOOLCHAIN_TEST_ENVIRONMENT) == "1"


def expected_variants(sample_name: str) -> tuple[str, ...]:
    execution = json.loads(
        (
            SAMPLES / "_harness" / sample_name / "acceptance" / "execution.json"
        ).read_text(encoding="utf-8")
    )
    configured = execution["target"][FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value]

    def language(value: str) -> str:
        return value.rsplit("lang-", 1)[-1]

    if isinstance(configured, list):
        return tuple(language(item) for item in configured) or ("python",)
    roles = tuple((slot, language(value)) for slot, value in configured.items())
    if set(roles) == {
        ("backend-language", "rust"),
        ("frontend-language", "javascript"),
    }:
        return ("rust-javascript-full-stack",)
    return ("-".join(f"{slot}-{value}" for slot, value in roles),)


def host_os() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform in {"win32", "cygwin"}:
        return "windows"
    raise AssertionError(f"unsupported test platform: {sys.platform}")


def platform_contracts() -> dict[str, tuple[str, str]]:
    contracts = {}
    for os_name in ("linux", "macos", "windows"):
        root = REPO_ROOT / "flavors" / f"os-{os_name}"
        definition = _flavor_definition(root)
        loaded = OpenSpecProvider().load(
            root,
            tuple(item.uri for item in definition.specification_fragments),
        )
        contracts[os_name] = (
            definition.identity.uri,
            loaded.specification_set.identity.uri,
        )
    return contracts


def _flavor_definition(root: Path) -> FlavorDefinition:
    path = root / "flavor.md"
    return parse_flavor_markdown(path.read_bytes(), source=path.as_posix()).resolve(
        lambda uri: root.joinpath(*Path(uri).parts).read_bytes()
    )


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class NeutralSampleLadderTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence_environment = mock.patch.dict(
            os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
                # Generation is mocked in this suite; skip the live-model preflight
                # so it does not intercept the routing/override paths under test.
                "LITAI_SKIP_MODEL_PREFLIGHT": "1",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

        # This suite composes recipes and mocks generation. Live CUDA discovery
        # belongs to the separately authorized sample execution gates.
        self.cuda_stack_document = RecipeDocument.create(
            "execution/nvidia-accelerated-stack-selection.json",
            canonical_json_bytes(
                {
                    "schema": "literate-ai/test-cuda-stack@1",
                    "scope": "recipe-composition-only",
                }
            ).decode("utf-8"),
        )
        cuda_stack = mock.patch(
            "tests.conformance.support.sample_runner._nvidia_stack_selection_document",
            return_value=self.cuda_stack_document,
        )
        cuda_stack.start()
        self.addCleanup(cuda_stack.stop)

    @unittest.skipUnless(
        os.environ.get("LITERATE_AI_LIVE_SAMPLES") == "1",
        "set LITERATE_AI_LIVE_SAMPLES=1 to invoke the authenticated coding CLI",
    )
    def test_hello_python_runs_only_through_generic_standard_service(self):
        sample = SAMPLES / "hello-component"
        _metadata, definition, loaded, closures = _load_sample(sample)
        contract, _document = _execution_contract(sample, definition, loaded)
        variant = next(
            item
            for item in _execution_variants(definition, contract)
            if item.variant_id == "python"
        )
        authority, catalog = _sample_runtime_recipe(
            sample,
            definition,
            loaded.specification_set,
            contract,
            variant,
            closures.generation,
        )[3:5]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            executed = execute_standard_sample_variant(
                sample_root=sample,
                sample_id="hello-component",
                variant=variant,
                authority=authority,
                catalog=catalog,
                source_generator=CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(source_intelligence_mode="off"),
                    cache_root=temporary / "source-cache",
                    project_root=REPO_ROOT,
                ),
                scratch=temporary / "runtime",
                object_root=temporary / "objects",
            )

        self.assertTrue(executed.lifecycle.successful)
        self.assertEqual(executed.report["node_count"], 1)
        self.assertEqual(executed.report["sample_id"], "hello-component")
        self.assertNotIn("lifecycle_steps", executed.report)
        self.assertNotIn("build_security_profile", executed.report)

    @unittest.skipUnless(
        os.environ.get("LITERATE_AI_LIVE_SAMPLES") == "1",
        "set LITERATE_AI_LIVE_SAMPLES=1 to invoke the authenticated coding CLI",
    )
    def test_hello_cpp_runs_only_through_generic_standard_service(self):
        sample = SAMPLES / "hello-component"
        _metadata, definition, loaded, closures = _load_sample(sample)
        contract, _document = _execution_contract(sample, definition, loaded)
        variant = next(
            item
            for item in _execution_variants(definition, contract)
            if item.variant_id == "cpp"
        )
        authority, catalog = _sample_runtime_recipe(
            sample,
            definition,
            loaded.specification_set,
            contract,
            variant,
            closures.generation,
        )[3:5]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            executed = execute_standard_sample_variant(
                sample_root=sample,
                sample_id="hello-component",
                variant=variant,
                authority=authority,
                catalog=catalog,
                source_generator=CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(source_intelligence_mode="off"),
                    cache_root=temporary / "source-cache",
                    project_root=REPO_ROOT,
                ),
                scratch=temporary / "runtime",
                object_root=temporary / "objects",
            )

        self.assertTrue(executed.lifecycle.successful)
        self.assertEqual(executed.report["node_count"], 1)
        self.assertEqual(executed.report["sample_id"], "hello-component")
        self.assertNotIn("lifecycle_steps", executed.report)
        self.assertNotIn("build_security_profile", executed.report)

    @unittest.skipUnless(
        os.environ.get("LITERATE_AI_LIVE_SAMPLES") == "1",
        "set LITERATE_AI_LIVE_SAMPLES=1 to invoke the authenticated coding CLI",
    )
    def test_dependency_planner_rust_runs_only_through_generic_standard_service(self):
        sample = SAMPLES / "dependency-planner"
        _metadata, definition, loaded, closures = _load_sample(sample)
        contract, _document = _execution_contract(sample, definition, loaded)
        variant = next(
            item
            for item in _execution_variants(definition, contract)
            if item.variant_id == "rust"
        )
        authority, catalog = _sample_runtime_recipe(
            sample,
            definition,
            loaded.specification_set,
            contract,
            variant,
            closures.generation,
        )[3:5]
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            executed = execute_standard_sample_variant(
                sample_root=sample,
                sample_id="dependency-planner",
                variant=variant,
                authority=authority,
                catalog=catalog,
                source_generator=CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(source_intelligence_mode="off"),
                    cache_root=temporary / "source-cache",
                    project_root=REPO_ROOT,
                ),
                scratch=temporary / "runtime",
                object_root=temporary / "objects",
            )

        self.assertTrue(executed.lifecycle.successful)
        self.assertEqual(executed.report["node_count"], 1)
        self.assertEqual(executed.report["sample_id"], "dependency-planner")
        self.assertEqual(executed.report["variant_id"], "rust")
        self.assertNotIn("lifecycle_steps", executed.report)
        self.assertNotIn("build_security_profile", executed.report)

    @unittest.skipUnless(
        native_toolchain_tests_enabled()
        and (shutil.which("bazel") or shutil.which("bazelisk")),
        "set LITERATE_AI_NATIVE_TOOLCHAIN_TESTS=1 with Bazel on PATH",
    )
    def test_service_stack_uses_standard_three_node_lifecycle_with_real_bazel(self):
        sample = SAMPLES / "service-stack"
        _metadata, definition, loaded, closures = _load_sample(sample)
        contract, _document = _execution_contract(sample, definition, loaded)
        variant = _ExecutionVariant(
            "python",
            (
                ("build-system", "bazel"),
                ("language", "python"),
                ("os", "host"),
            ),
            ("python",),
        )
        (
            _base,
            _target,
            _resolved,
            authority,
            catalog,
            _plan,
            _selected,
        ) = _sample_runtime_recipe(
            sample,
            definition,
            loaded.specification_set,
            contract,
            variant,
            closures.generation,
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory).resolve()
            scratch = temporary / "first"
            object_root = temporary / "objects"
            source_cache_root = temporary / "accepted-source-cache"
            result = execute_standard_service_stack(
                authority=authority,
                catalog=catalog,
                scratch=scratch,
                object_root=object_root,
                source_cache_root=source_cache_root,
                include_lifecycle_result=True,
            )
            cached = execute_standard_service_stack(
                authority=authority,
                catalog=catalog,
                scratch=temporary / "second",
                object_root=object_root,
                source_cache_root=source_cache_root,
            )
            lifecycle_result = result["_lifecycle_result"]
            standard_report = project_standard_sample_execution_report(
                lifecycle_result,
                component_lock=authority.lock,
                sample_id="service-stack",
                variant_id="python-bazel",
            )
            self.assertEqual(
                standard_report["schema"],
                "literate-ai/standard-sample-execution-report@1",
            )
            self.assertEqual(standard_report["node_count"], 3)
            self.assertEqual(standard_report["generated_test_count"], 9)
            self.assertEqual(
                standard_report["lifecycle_result_identity"],
                lifecycle_result.identity.uri,
            )
            self.assertEqual(
                standard_report["component_lock_identity"], authority.lock.identity.uri
            )
            self.assertEqual(
                {item["component_revision"] for item in standard_report["nodes"]},
                {node.revision.identity.uri for node in authority.lock.nodes},
            )
            self.assertTrue(
                all(item["stdout_identity"] for item in standard_report["nodes"])
            )
            self.assertIsNotNone(lifecycle_result.root_integration_evidence_identity)
            self.assertEqual(
                lifecycle_result.aggregate_receipt.root_integration_evidence_identity,
                lifecycle_result.root_integration_evidence_identity,
            )
            package_roots = tuple(
                path
                for path in (object_root / "project-packages").iterdir()
                if path.is_dir()
            )
            self.assertTrue(package_roots)
            self.assertTrue(
                all(
                    any(item.is_file() for item in root.rglob("*"))
                    for root in package_roots
                )
            )
            self.assertFalse(
                any(
                    "__pycache__" in path.parts
                    for path in (scratch / "standard-workspaces").rglob("*")
                ),
                "compilation must not mutate admitted generated source",
            )
            self.assertTrue(
                any(
                    path.is_file() and zipfile.is_zipfile(path)
                    for path in object_root.rglob("python-*")
                ),
                "the object projection must contain Bazel-produced bytecode archives",
            )

            lifecycle_policy = load_current_standard_lifecycle_policy()
            runner = canonical_identity({"standard-runner": "conformance"})
            receipt = project_standard_sample_test_receipt(
                result["_lifecycle_result"],
                project_id="standard-service-stack",
                project_revision_identity=canonical_identity(
                    {"project": "standard-service-stack"}
                ),
                lifecycle_policy=lifecycle_policy,
                receipt_policy=ProjectTestReceiptPolicy(
                    lifecycle_policy.policy_id,
                    lifecycle_policy.policy_version,
                    runner,
                    STANDARD_FULL_REBUILD_EVIDENCE_KINDS,
                    lifecycle_policy.minimum_test_count,
                ),
                lifecycle_request_identity=canonical_identity(
                    {"standard-request": "conformance"}
                ),
                lifecycle_invocation_identity=canonical_identity(
                    {"standard-in-process-invocation": "conformance"}
                ),
                runner_identity=runner,
            )
            self.assertEqual(
                {item.kind for item in receipt.evidence},
                set(STANDARD_FULL_REBUILD_EVIDENCE_KINDS),
            )
            self.assertEqual(
                receipt.result_identity,
                result["_lifecycle_result"].identity,
            )
            self.assertEqual(receipt.summary.total, 9)

        self.assertEqual(
            set(result["generation_calls"]),
            {"money-calculation", "invoice-service", "service-stack"},
        )
        self.assertEqual(cached["generation_calls"], ())
        self.assertEqual(
            set(result["source_generation_dispositions"].values()), {"generated"}
        )
        self.assertEqual(
            set(cached["source_generation_dispositions"].values()), {"reused"}
        )
        self.assertTrue(all(result["source_cache_publications"].values()))
        self.assertTrue(
            all(value is None for value in cached["source_cache_publications"].values())
        )
        self.assertEqual(
            set(result["source_index_identities"]),
            set(cached["source_index_identities"]),
        )
        self.assertTrue(
            all(
                result["source_index_identities"][name]
                != cached["source_index_identities"][name]
                for name in result["source_index_identities"]
            ),
            "cache hits must be re-indexed under current final-path custody",
        )
        self.assertEqual(
            result["dependency_artifact_counts"],
            {
                "money-calculation": 0,
                "invoice-service": 1,
                "service-stack": 1,
            },
        )
        self.assertEqual(
            result["tested_components"],
            ("invoice-service", "money-calculation", "service-stack"),
        )
        self.assertEqual(
            result["provider_artifact_edges"],
            {
                "money-calculation": (),
                "invoice-service": (
                    result["artifact_identities"]["money-calculation"],
                ),
                "service-stack": (result["artifact_identities"]["invoice-service"],),
            },
        )
        self.assertEqual(
            result["root_result"],
            {
                "discount_cents": 410,
                "line_count": 2,
                "subtotal_cents": 4095,
                "total_cents": 3685,
                "unit_count": 5,
            },
        )
        self.assertEqual(result["build_cache"]["misses"], 3)
        self.assertEqual(result["build_cache"]["hits"], 0)
        self.assertEqual(cached["build_cache"]["misses"], 0)
        self.assertEqual(cached["build_cache"]["hits"], 3)
        self.assertEqual(
            set(result["post_source_evidence"]),
            {"money-calculation", "invoice-service", "service-stack"},
        )
        for evidence in result["post_source_evidence"].values():
            self.assertEqual(evidence["generated_test_case_count"], 3)
            self.assertNotEqual(evidence["source_sbom"], evidence["resolved_sbom"])

    @unittest.skipUnless(
        native_toolchain_tests_enabled()
        and (shutil.which("bazel") or shutil.which("bazelisk")),
        "set LITERATE_AI_NATIVE_TOOLCHAIN_TESTS=1 with Bazel on PATH",
    )
    def test_service_stack_bazel_target_produces_bytecode_and_hits_cache(self):
        sample = SAMPLES / "service-stack"
        _metadata, definition, loaded, closures = _load_sample(sample)
        contract, _document = _execution_contract(sample, definition, loaded)
        variant = _ExecutionVariant(
            "python",
            (
                ("build-system", "bazel"),
                ("language", "python"),
                ("os", "host"),
            ),
            ("python", "bazel"),
        )
        (
            _base,
            _target,
            _resolved,
            authority,
            catalog,
            _plan,
            _selected,
        ) = _sample_runtime_recipe(
            sample,
            definition,
            loaded.specification_set,
            contract,
            variant,
            closures.generation,
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            first_scratch = temporary / "first"
            object_root = temporary / "objects"
            result = execute_standard_service_stack(
                authority=authority,
                catalog=catalog,
                scratch=first_scratch,
                object_root=object_root,
                build_system="bazel",
            )
            cached = execute_standard_service_stack(
                authority=authority,
                catalog=catalog,
                scratch=temporary / "second",
                object_root=object_root,
                build_system="bazel",
            )

            generated = first_scratch / "standard-workspaces"
            self.assertEqual(len(tuple(generated.rglob("BUILD.bazel"))), 3)
            self.assertFalse(
                any(path.suffix == ".pyc" for path in generated.rglob("*")),
                "Bazel compilation must not mutate admitted generated source",
            )
            archives = [
                path
                for path in object_root.rglob("python-*")
                if path.is_file() and zipfile.is_zipfile(path)
            ]
            # Root integration and immutable packaging deliberately copy provider
            # exports into later custody layers.  Prove that Bazel produced the
            # three distinct Component archives without treating those exact
            # downstream copies as additional compilations.
            self.assertEqual(
                len({hashlib.sha256(path.read_bytes()).digest() for path in archives}),
                3,
            )
            for archive_path in archives:
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertEqual(
                        set(archive.namelist()),
                        {"__main__.pyc", "main.pyc", "test_component.pyc"},
                    )

        self.assertEqual(result["build_system"], "bazel")
        self.assertEqual(len(result["bazel_target_identities"]), 3)
        self.assertEqual(result["root_result"]["total_cents"], 3685)
        self.assertEqual(result["build_cache"]["misses"], 3)
        self.assertEqual(result["build_cache"]["hits"], 0)
        self.assertEqual(cached["build_cache"]["misses"], 0)
        self.assertEqual(cached["build_cache"]["hits"], 3)

    def test_all_host_recipes_compose_from_specs_and_real_flavors(self):
        plans = plan_recipes(SAMPLES)
        self.assertTrue(plans)
        self.assertEqual(
            {(item["sample_id"], item["language"]) for item in plans},
            {
                (sample.name, variant)
                for sample in discover(SAMPLES, platform=host_os())
                for variant in expected_variants(sample.name)
            },
        )
        for item in plans:
            recipe = item["recipe"]
            language = item["language"]
            with self.subTest(sample=item["sample_id"], language=language):
                expected_entrypoints = (
                    ("source/backend/main.rs", "source/frontend/main.js")
                    if language == "rust-javascript-full-stack"
                    else (portable_source_entrypoint(language),)
                )
                self.assertEqual(recipe.all_required_entrypoints, expected_entrypoints)
                _metadata, definition, _loaded, _closures = _load_sample(
                    SAMPLES / item["sample_id"]
                )
                selected_slots = set(item["slot_values"])
                self.assertEqual(
                    {flavor.axis for flavor in recipe.flavors},
                    {
                        slot.axis.value
                        for slot in definition.flavor_slots
                        if slot.slot_id in selected_slots
                    },
                )
                expected_languages = set(item["languages"])
                self.assertTrue(
                    expected_languages <= {flavor.value for flavor in recipe.flavors}
                )
                if item["sample_id"] in {
                    "regenerative-roundtrip",
                    "service-stack",
                }:
                    self.assertIn(
                        "bazel",
                        {flavor.value for flavor in recipe.flavors},
                    )
                language_skills = {
                    "python": "python-portable-application",
                    "cpp": "cpp17-portable-json-application",
                    "rust": "rust-portable-json-application",
                    "javascript": "javascript-portable-json-application",
                    "swift": "swift-portable-json-application",
                }
                ordered_language_flavors = [
                    flavor.value
                    for flavor in recipe.flavors
                    if flavor.axis == FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value
                ]
                build_skill = (
                    "bazel-build-system"
                    if item["sample_id"] in {"regenerative-roundtrip", "service-stack"}
                    else "make-build-system"
                )
                language_leaf_skills = tuple(
                    language_skills[language_name]
                    for language_name in ordered_language_flavors
                )
                if item["sample_id"] == "python-service-example":
                    expected_skills = [
                        "backend-application",
                        "mcp-application",
                        "portable-specification-planning",
                        "python-service-application",
                        build_skill,
                        "portable-application-implementation",
                        *language_leaf_skills,
                    ]
                elif item["sample_id"] == "react-dashboard-example":
                    expected_skills = [
                        "frontend-application",
                        "portable-specification-planning",
                        "react-dashboard-application",
                        build_skill,
                        "react-application",
                        "portable-application-implementation",
                        *language_leaf_skills,
                    ]
                elif item["sample_id"] == "durable-split-service":
                    expected_skills = [
                        "backend-application",
                        "frontend-application",
                        "portable-specification-planning",
                        "durable-split-service",
                        "portable-application-implementation",
                        build_skill,
                        *language_leaf_skills,
                    ]
                else:
                    expected_skills = [
                        "portable-specification-planning",
                        "portable-application-implementation",
                        *(
                            ["generate-nvidia-cuda-application"]
                            if "nvidia-cuda"
                            in {flavor.value for flavor in recipe.flavors}
                            else []
                        ),
                        build_skill,
                        *(
                            ["docker-container-application"]
                            if "docker" in {flavor.value for flavor in recipe.flavors}
                            else []
                        ),
                        *language_leaf_skills,
                    ]
                self.assertEqual(
                    [skill.skill_id for skill in recipe.resolved_skills],
                    expected_skills,
                )
                # Flavor-contributed skills arrive through selected Flavors, never
                # through Component authoring_inputs (ADR 0012: flavor-specific
                # practice stays out of component prose).
                self.assertTrue(
                    all(
                        skill.identity.startswith("sha256:")
                        for skill in recipe.resolved_skills
                    )
                )
                self.assertIn(
                    host_os(),
                    {flavor.value for flavor in recipe.flavors},
                )
                expected_toolchains = (
                    {"python"}
                    if "python" in item["languages"]
                    else (
                        {"node"}
                        if "javascript" in item["languages"]
                        else ({"swift"} if "swift" in item["languages"] else set())
                    )
                )
                if "nvidia-cuda" in {flavor.value for flavor in recipe.flavors}:
                    expected_toolchains = {*expected_toolchains, "nvcc"}
                self.assertEqual(
                    {
                        constraint["constraint"]["toolchain"]
                        for constraint in item["toolchain_constraints"]
                    },
                    expected_toolchains,
                )
                self.assertTrue(all(flavor.documents for flavor in recipe.flavors))
                self.assertTrue(
                    all(
                        flavor.revision_identity is not None
                        and flavor.specification_set_identity is not None
                        for flavor in recipe.flavors
                    )
                )
                self.assertEqual(
                    tuple(label for label, _identity in recipe.resolved_inputs),
                    (
                        "component_lock",
                        "root_revision",
                        "target_profile",
                        "selection_policy",
                    ),
                )
                self.assertEqual(
                    recipe.component_lock_identity.uri,
                    item["component_lock_identity"],
                )
                self.assertEqual(
                    recipe.managed_sbom_graph.resolved_graph_identity,
                    recipe.component_lock_identity,
                )
                document_paths = {document.path for document in recipe.documents}
                self.assertIn("component.md", document_paths)
                self.assertIn("acceptance/execution.json", document_paths)
                self.assertNotIn(
                    "acceptance/execution.json",
                    {document.path for document in recipe.model_documents},
                )
                acceptance = next(
                    document
                    for document in recipe.documents
                    if document.path == "acceptance/execution.json"
                )
                acceptance_value = json.loads(acceptance.content)
                prompt = recipe.prompt()
                self.assertNotIn(acceptance.content, prompt)
                for invocation in acceptance_value["invocations"]:
                    signature = generated_test_invocation_signature(
                        invocation["arguments"]
                    )
                    self.assertIn(
                        canonical_json_bytes(signature).decode("utf-8"),
                        prompt,
                    )
                if item["sample_id"] == "regenerative-roundtrip":
                    self.assertIn(
                        ".literate/specification-context.json", document_paths
                    )
                    self.assertEqual(len(document_paths), 3)
                else:
                    # Composed samples add one public Component boundary document;
                    # playback-controller adds its SCXML trace sidecar. Everything
                    # else stays single-root.
                    expected_documents = (
                        4
                        if item["sample_id"]
                        in {
                            "service-stack",
                            "containerized-log-tally",
                            "durable-split-service",
                            "playback-controller",
                        }
                        else 3
                    )
                    if "nvidia-cuda" in {flavor.value for flavor in recipe.flavors}:
                        expected_documents += 1
                        self.assertIn(
                            "execution/nvidia-accelerated-stack-selection.json",
                            document_paths,
                        )
                    self.assertEqual(len(document_paths), expected_documents)

        stack_plans = tuple(
            item for item in plans if item["sample_id"] == "service-stack"
        )
        self.assertEqual(len(stack_plans), 1)
        for item in stack_plans:
            recipe = item["recipe"]
            graph = recipe.managed_sbom_graph.to_dict()
            self.assertEqual(
                {component["name"] for component in graph["components"]},
                {
                    "component://samples/service-stack",
                    "component://literate-ai/invoice-service",
                    "component://literate-ai/money-calculation",
                },
            )
            self.assertEqual(len(graph["edges"]), 4)
            self.assertFalse(
                any(
                    document.path.startswith("dependency-components/")
                    for document in recipe.documents
                )
            )

    def test_sample_lifecycle_resolves_one_lock_and_never_writes_checkout_authority(
        self,
    ):
        sample_root = SAMPLES / "hello-component"
        metadata, definition, loaded, closures = _load_sample(sample_root)
        execution, _document = _execution_contract(sample_root, definition, loaded)
        variant = _ExecutionVariant(
            "python",
            (("language", "python"), ("os", "host")),
            ("python",),
        )
        resolver = mock.Mock(wraps=ComponentLockResolver())
        with (
            mock.patch(
                "tests.conformance.support.sample_runner.ComponentLockResolver",
                return_value=resolver,
            ),
            mock.patch(
                "tests.conformance.support.sample_runner.ComponentComposer"
            ) as composer,
            mock.patch(
                "tests.conformance.support.sample_runner.FlavorResolver"
            ) as flavor_resolver,
        ):
            parts = _sample_runtime_recipe(
                sample_root,
                definition,
                loaded.specification_set,
                execution,
                variant,
                closures.generation,
            )

        authority = parts[3]
        self.assertEqual(metadata["sample_id"], "hello-component")
        self.assertEqual(resolver.resolve.call_count, 1)
        composer.assert_not_called()
        flavor_resolver.assert_not_called()
        self.assertEqual(
            CycloneDxManagedGraph.from_component_lock(
                authority.lock
            ).resolved_graph_identity,
            authority.lock.identity,
        )
        self.assertFalse((sample_root / "component.lock.json").exists())

    @unittest.skipUnless(
        os.environ.get("LITERATE_AI_LIVE_SAMPLES") == "1",
        "set LITERATE_AI_LIVE_SAMPLES=1 to invoke the authenticated coding CLI",
    )
    def test_live_coding_cli_builds_and_runs_the_complete_matrix(self):
        before = tree_digest(SAMPLES)
        with tempfile.TemporaryDirectory() as directory:
            report = run_all(SAMPLES, Path(directory), allow_host_execution=True)
        self.assertEqual(report["schema"], REPORT_SCHEMA)
        self.assertTrue(report["passed"])
        self.assertEqual(report["recipe_count"], 29)
        self.assertEqual(report["verifier_test_count"], 87)
        self.assertGreaterEqual(report["generated_test_count"], 87)
        self.assertEqual(
            report["invocation_count"],
            report["generated_test_count"] + report["verifier_test_count"],
        )
        self.assertEqual(
            report["test_count"],
            report["invocation_count"] + report["assertion_count"],
        )
        self.assertNotIn('"entropy"', json.dumps(report, sort_keys=True))
        contracts = platform_contracts()
        expected_platform_flavor, expected_platform_specification = contracts[host_os()]
        by_id = {item["sample_id"]: item for item in report["samples"]}
        for item in report["samples"]:
            sample_root = SAMPLES / item["sample_id"]
            interface = json.loads(
                (sample_root / "acceptance" / "execution.json").read_text(
                    encoding="utf-8"
                )
            )
            metadata = json.loads(
                sample_harness_manifest(sample_root).read_text(encoding="utf-8")
            )
            oracle_reference = ContentReference.from_dict(metadata["acceptance_oracle"])
            oracle = json.loads(
                sample_harness_oracle(sample_root, oracle_reference).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                [item["case_id"] for item in interface["invocations"]],
                [item["case_id"] for item in oracle["oracle_results"]],
            )
            self.assertEqual(
                [
                    execution["implementation_language"]
                    for execution in item["executions"]
                ],
                list(expected_variants(item["sample_id"])),
            )
            for execution in item["executions"]:
                with self.subTest(
                    sample=item["sample_id"],
                    language=execution["implementation_language"],
                ):
                    self.assertTrue(execution["generated_from_specification"])
                    self.assertIsNone(execution["source_snapshot_identity"])
                    generation_closure = execution["generation_input_closure"]
                    verifier_closure = execution["verifier_input_closure"]
                    self.assertTrue(
                        generation_closure["identity"].startswith("sha256:")
                    )
                    self.assertTrue(verifier_closure["identity"].startswith("sha256:"))
                    self.assertGreater(
                        verifier_closure["file_count"],
                        generation_closure["file_count"],
                    )
                    self.assertIn(execution["coding_cli"], CODING_CLIS)
                    self.assertEqual(execution["model_stages"], ["plan", "generate"])
                    self.assertEqual(
                        [route["stage_id"] for route in execution["model_routes"]],
                        ["plan", "generate"],
                    )
                    self.assertEqual(
                        execution["model_routes"][0]["endpoint_id"],
                        "sample-deterministic-spec-planner",
                    )
                    self.assertNotEqual(
                        execution["model_routes"][0]["endpoint_id"],
                        execution["model_routes"][1]["endpoint_id"],
                    )
                    self.assertEqual(execution["execution_security_profile"], "yolo")
                    self.assertEqual(
                        execution["source_security_profile"], "constrained"
                    )
                    self.assertEqual(
                        execution["execution_authorization_classification_digest"],
                        execution["source_security_classification_digest"],
                    )
                    self.assertTrue(
                        execution["coding_cli_executable_identity"].startswith(
                            "sha256:"
                        )
                    )
                    self.assertTrue(
                        execution["coding_cli_selection_identity"].startswith("sha256:")
                    )
                    self.assertTrue(execution["request_identity"].startswith("sha256:"))
                    self.assertTrue(execution["command_identity"].startswith("sha256:"))
                    self.assertFalse(execution["coding_cli_isolation"]["hermetic"])
                    self.assertTrue(execution["coding_cli_isolation"]["limitations"])
                    generation_closure = execution["generation_input_closure"]
                    verifier_closure = execution["verifier_input_closure"]
                    self.assertTrue(
                        generation_closure["identity"].startswith("sha256:")
                    )
                    self.assertTrue(verifier_closure["identity"].startswith("sha256:"))
                    self.assertNotEqual(
                        generation_closure["identity"], verifier_closure["identity"]
                    )
                    self.assertEqual(
                        verifier_closure["file_count"],
                        generation_closure["file_count"] + 2,
                    )
                    self.assertEqual(
                        execution["lifecycle_steps"],
                        [
                            "validate",
                            "classify",
                            "authorize-build",
                            "build",
                            "resolve-dependencies",
                            "test-generated",
                            "verify-independent",
                            "prepare-tree",
                            "commit-tree",
                        ],
                    )
                    expected_mode = {
                        "python": "authorized-host-python-bytecode",
                        "cpp": "authorized-host-cpp-executable",
                        "rust": "authorized-host-rust-executable",
                        "swift": "authorized-host-swift-executable",
                        "javascript": "authorized-host-javascript-node",
                        "rust-javascript-full-stack": (
                            "authorized-host-rust-javascript-full-stack"
                        ),
                    }[execution["implementation_language"]]
                    self.assertEqual(execution["execution_mode"], expected_mode)
                    self.assertEqual(
                        len(execution["generation_skills"]),
                        4
                        if execution["implementation_language"]
                        == "rust-javascript-full-stack"
                        else 3,
                    )
                    self.assertTrue(
                        all(
                            skill["identity"].startswith("sha256:")
                            for skill in execution["generation_skills"]
                        )
                    )
                    self.assertTrue(
                        execution["acceptance_oracle_excluded_from_generation_request"]
                    )
                    self.assertEqual(
                        execution["generation_interface_identity"],
                        execution["acceptance_contract_identity"],
                    )
                    self.assertEqual(
                        execution["acceptance_oracle_identity"],
                        oracle_reference.identity.uri,
                    )
                    expected_cases = {
                        item["case_id"]: item["expected_result"]
                        for item in oracle["oracle_results"]
                    }
                    self.assertGreaterEqual(execution["generated_test_case_count"], 3)
                    self.assertEqual(
                        execution["generated_test_categories"],
                        ["boundary", "example", "invariant"],
                    )
                    self.assertTrue(
                        execution["generated_test_suite_identity"].startswith("sha256:")
                    )
                    self.assertEqual(
                        execution["generated_test_execution_profile"],
                        "authorized-execution",
                    )
                    self.assertEqual(
                        execution["independent_acceptance_execution_profile"],
                        "authorized-execution",
                    )
                    self.assertTrue(
                        execution["independent_acceptance_suite_identity"].startswith(
                            "sha256:"
                        )
                    )
                    self.assertEqual(execution["generation_mode"], "major-rebuild")
                    self.assertEqual(
                        execution["execution_case_count"],
                        execution["generated_test_case_count"]
                        + len(expected_cases)
                        + 1,
                    )
                    generated_cases = [
                        case
                        for case in execution["execution_cases"]
                        if case["verification_source"]
                        == "generated-implementation-test"
                    ]
                    self.assertEqual(
                        len(generated_cases), execution["generated_test_case_count"]
                    )
                    pinned_cases = [
                        case
                        for case in execution["execution_cases"]
                        if case["verification_source"] == "pinned-oracle"
                    ]
                    self.assertEqual(
                        {item["case_id"]: item["result"] for item in pinned_cases},
                        expected_cases,
                    )
                    runtime_cases = [
                        case
                        for case in execution["execution_cases"]
                        if case["verification_source"] == "post-build-runtime-oracle"
                    ]
                    self.assertEqual(len(runtime_cases), 1)
                    self.assertEqual(
                        runtime_cases[0]["case_id"], "runtime-generalization"
                    )
                    self.assertEqual(execution["post_build_runtime_probe_count"], 1)
                    self.assertEqual(
                        execution["post_build_runtime_probe_case_id"],
                        runtime_cases[0]["case_id"],
                    )
                    self.assertNotIn(
                        runtime_cases[0]["result"], expected_cases.values()
                    )
                    self.assertEqual(execution["build_security_profile"], "yolo")
                    self.assertTrue(
                        execution["build_toolchain_identity"].startswith("sha256:")
                    )
                    cases = execution["execution_cases"]
                    self.assertEqual(
                        {case["artifact_digest"] for case in cases},
                        {execution["build_artifact_identity"]},
                    )
                    self.assertEqual(
                        {case["artifact_tree_digest"] for case in cases},
                        {execution["execution_artifact_tree_identity"]},
                    )
                    self.assertEqual(
                        {case["entrypoint_digest"] for case in cases},
                        {execution["execution_entrypoint_identity"]},
                    )
                    self.assertEqual(
                        len({case["harness_digest"] for case in cases}), len(cases)
                    )
                    self.assertEqual(
                        len({case["execution_authorization_id"] for case in cases}),
                        len(cases),
                    )
                    self.assertEqual(
                        len({case["observation_request_identity"] for case in cases}),
                        len(cases),
                    )
                    for case in cases:
                        self.assertNotIn("arguments", case)
                        self.assertNotIn("expected_result", case)
                        self.assertTrue(case["passed"])
                        observation_request = case["observation_request"]
                        execution_authorization = case["execution_authorization"]
                        self.assertEqual(
                            canonical_identity(observation_request).uri,
                            execution_authorization["request_digest"],
                        )
                        self.assertEqual(
                            execution_authorization["authorization_id"],
                            case["execution_authorization_id"],
                        )
                        self.assertEqual(
                            case["execution_authorization_classification_digest"],
                            execution["source_security_classification_digest"],
                        )
                        self.assertEqual(case["execution_security_profile"], "yolo")
                        auxiliary_results = case["auxiliary_results"]
                        if (
                            execution["implementation_language"]
                            == "rust-javascript-full-stack"
                        ):
                            self.assertEqual(len(auxiliary_results), 1)
                            self.assertEqual(auxiliary_results[0]["role"], "backend")
                            self.assertTrue(auxiliary_results[0]["result"])
                            self.assertTrue(
                                auxiliary_results[0]["stdout_digest"].startswith(
                                    "sha256:"
                                )
                            )
                        else:
                            self.assertEqual(auxiliary_results, [])
                    self.assertEqual(execution["result"], expected_cases["primary"])
                    self.assertEqual(
                        execution["resolved_target"]["platform.os"], host_os()
                    )
                    self.assertIn(
                        expected_platform_specification,
                        execution["flavor_specification_identities"],
                    )
                    self.assertIn(
                        expected_platform_flavor,
                        execution["selected_flavor_identities"],
                    )
                    self.assertIn(
                        execution["compiled_entrypoint"], execution["compiled_files"]
                    )
                    self.assertTrue(
                        set(execution["compiled_entrypoints"])
                        <= set(execution["compiled_files"])
                    )
                    if execution["implementation_language"] == "rust":
                        self.assertTrue(execution["build_consumed_source_files"])
                        self.assertEqual(execution["build_checked_source_files"], [])
                    elif execution["implementation_language"] == "javascript":
                        self.assertEqual(execution["build_consumed_source_files"], [])
                        self.assertTrue(execution["build_checked_source_files"])
                    elif (
                        execution["implementation_language"]
                        == "rust-javascript-full-stack"
                    ):
                        self.assertTrue(execution["build_consumed_source_files"])
                        self.assertTrue(execution["build_checked_source_files"])
                    self.assertEqual(
                        len(execution["execution_auxiliary_artifacts"]),
                        1
                        if execution["implementation_language"]
                        == "rust-javascript-full-stack"
                        else 0,
                    )
                    self.assertEqual(
                        execution["execution_auxiliary_results"],
                        execution["execution_cases"][0]["auxiliary_results"],
                    )
        self.assertIn(
            "yolo-warning-is-persisted", by_id["security-policies"]["assertions"]
        )
        self.assertIn(
            "flavor-publication-import-roundtrips",
            by_id["flavor-matrix"]["assertions"],
        )
        self.assertIn(
            "cache-and-workspace-survive-restart",
            by_id["empty-cache-restart"]["assertions"],
        )
        self.assertEqual(
            by_id["self-hosting"]["assertions"],
            [
                "generated-readiness-artifact-executes",
                "framework-version-compatibility-is-evaluated",
                "packaged-skill-readiness-is-evaluated",
            ],
        )
        proofs = report["operational_metrics"]["standard_adoption_proofs"]
        self.assertEqual(len(proofs), 1)
        proof = proofs[0]
        self.assertEqual(proof["proof_status"], "passed")
        self.assertFalse(proof["receipt_admissible"])
        self.assertNotIn("generated_test_case_count", proof)
        self.assertNotIn("resolved_sbom_identity", proof)
        self.assertEqual(
            set(proof["generation_calls"]),
            {"money-calculation", "invoice-service", "service-stack"},
        )
        components = proof["component_results"]
        self.assertEqual(
            components["invoice-service"]["provider_artifact_identities"],
            [components["money-calculation"]["artifact_identity"]],
        )
        self.assertEqual(
            components["service-stack"]["provider_artifact_identities"],
            [components["invoice-service"]["artifact_identity"]],
        )
        self.assertEqual(tree_digest(SAMPLES), before)


if __name__ == "__main__":
    unittest.main()
