from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.builders import BuildError
from literate_ai.adapters.cache import CachedCodingCliSourceGenerator
from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.adapters.dependencies import (
    DependencyObservationError,
    HostDependencyObservation,
    build_cyclonedx_bom,
)
from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.adapters.lifecycle import HostArtifactExecutionError
from literate_ai.adapters.models import (
    CODING_CLIS,
    CodingCliError,
    CodingCliGeneration,
    CodingCliSelection,
    CodingCliSourceGenerator,
    RecipeDocument,
    portable_source_entrypoint,
)
from literate_ai.adapters.specifications import OpenSpecProvider
from literate_ai.adapters.standard_project import (
    project_locked_standard_toolchain_closure,
)
from literate_ai.application import (
    GenerationFailure,
    GenerationStatus,
    compile_generation_execution_plan,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.application.standard_project_services import (
    StandardProjectApplicationService,
)
from literate_ai.contracts import (
    CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR,
    REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA,
    STANDARD_FULL_REBUILD_EVIDENCE_KINDS,
    ContentIdentity,
    ContentReference,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    FlavorAxis,
    FlavorCardinality,
    FlavorDefinition,
    ManagedComponentKind,
    ProjectTestReceiptPolicy,
    ProjectTestReceiptProvisional,
    RebuildSourceCacheControl,
    RebuildSourceCacheDecision,
    RebuildSourceCacheDerivationManifest,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    SourceIntelligenceArtifact,
    canonical_identity,
    canonical_json_bytes,
    component_bom_ref,
    load_current_standard_lifecycle_policy,
)
from literate_ai.generated_tests import (
    generated_test_invocation_signature,
    validate_generated_test_suite,
)
from literate_ai.models import Locality, ModelEndpoint
from literate_ai.projects import PinnedInputClosureError, load_project
from tests.conformance.support.sample_runner import (
    EXECUTION_CONTRACT_SCHEMA,
    HANDLERS,
    REPORT_SCHEMA,
    STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA,
    SampleFailure,
    _accepted_source_files,
    _coding_cli_generation_provenance,
    _DerivationPlanned,
    _DerivationPlanningSourceGenerator,
    _execution_contract,
    _execution_variants,
    _executions_by_language,
    _ExecutionVariant,
    _full_stack_rust_js,
    _generated_application_rejection,
    _generated_candidate_rejection,
    _GeneratedApplicationRejected,
    _GeneratedBehaviorMismatch,
    _GeneratedCandidateRejected,
    _IndependentApplicationRejected,
    _IndependentBehaviorMismatch,
    _IndependentCandidateRejected,
    _load_sample,
    _locked_standard_sample_model_bindings,
    _locked_standard_sample_model_identities,
    _ordered_selected_flavors,
    _outer_bound_execution_cache_key,
    _parser,
    _plan_standard_sample_derivations,
    _prepare_standard_sample_project,
    _primary_pinned_case,
    _receipted_driver_summary,
    _receipted_project_revision,
    _report_component_lock_identities,
    _run_host_e2e,
    _sample_runtime_recipe,
    _service_stack,
    _standard_execution_reports,
    _standard_receipt_executions,
    _StandardSampleAuthoritySnapshot,
    _toolchain_discovery_options,
    _verification_cases,
    _write_candidate_rejection_evidence,
    discover,
    execute_standard_sample_variant,
    main,
    plan_derivation_manifest,
    plan_recipes,
    project_standard_sample_execution_report,
    project_standard_sample_test_receipt,
    project_test_receipt,
    run_all,
    run_sample,
    sample_harness_manifest,
    sample_harness_oracle,
    sample_test_runner_identity,
    standard_sample_execution_selections,
    write_project_test_receipt_candidate,
)
from tests.conformance.support.standard_service_stack import (
    _AuthoritySnapshot,
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


class NativeToolchainSelectionTests(unittest.TestCase):
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

    def test_native_toolchain_tests_require_exact_explicit_authorization(self):
        self.assertFalse(native_toolchain_tests_enabled({}))
        self.assertFalse(
            native_toolchain_tests_enabled({NATIVE_TOOLCHAIN_TEST_ENVIRONMENT: "true"})
        )
        self.assertTrue(
            native_toolchain_tests_enabled({NATIVE_TOOLCHAIN_TEST_ENVIRONMENT: "1"})
        )


def oracle_reference_fixture() -> ContentReference:
    return ContentReference(
        "acceptance-oracle",
        "acceptance/oracle.json",
        canonical_identity({"fixture": "private-oracle"}),
    )


def source_intelligence_fixture(
    tree_identity: str = "sha256:" + "5" * 64,
) -> SourceIntelligenceArtifact:
    return SourceIntelligenceArtifact.create(
        provider_id="fixture-intelligence",
        provider_version="test-fixture-v1",
        runtime_version="1.1.1",
        executable_identity="sha256:" + "d" * 64,
        source_tree_identity=tree_identity,
        source_snapshot_identity="sha256:" + "e" * 64,
        capabilities=("call-graph", "declarations", "references"),
        provider_properties=("built-with-version=1.1.1", "extraction-version=1"),
        artifact_path="source-intelligence.sqlite",
        artifact_media_type="application/vnd.sqlite3",
        artifact_identity="sha256:" + "f" * 64,
        document_count=1,
        symbol_count=1,
        relationship_count=0,
        unresolved_relationship_count=0,
        warning_count=0,
    )


def source_sbom_binding_fixture():
    identity = canonical_identity({"fixture": "source-sbom-root"})
    root_ref = component_bom_ref(identity)
    graph = CycloneDxManagedGraph(
        root_ref=root_ref,
        components=(
            CycloneDxManagedComponent(
                bom_ref=root_ref,
                kind=ManagedComponentKind.ROOT,
                identity=identity,
                name="fixture",
                version=identity.digest,
                scopes=(),
            ),
        ),
        edges=(),
        resolved_graph_identity=canonical_identity(
            {"fixture": "source-sbom-composition"}
        ),
    )
    _, binding = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=graph,
    )
    return binding


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


def copy_sample_project(temporary: Path, sample_name: str) -> Path:
    project = temporary / "project"
    (project / "samples").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "literate.project.json", project)
    for catalog in ("flavors", "routing", "skills", "workflows"):
        shutil.copytree(REPO_ROOT / catalog, project / catalog)
    sample = project / "samples" / sample_name
    shutil.copytree(SAMPLES / sample_name, sample)
    shutil.copytree(
        SAMPLES / "_harness" / sample_name,
        project / "samples" / "_harness" / sample_name,
    )
    return sample


def generated_candidate_failure(
    *,
    failure_code: str = "generation.lifecycle-step-failed",
    failed_stage: str = "test-generated",
    admitted_after_failure: bool = False,
) -> GenerationFailure:
    source_bundle = canonical_identity({"fixture": "source-bundle"}).uri
    artifact = canonical_identity({"fixture": "artifact"}).uri
    suite = canonical_identity({"fixture": "generated-suite"}).uri
    mismatch = _GeneratedBehaviorMismatch(
        case_id="generated-case",
        expected_result={"total": 630},
        observed_result={"total": 620},
    )
    rejected = _GeneratedCandidateRejected(
        mismatch,
        source_bundle_digest=source_bundle,
        artifact_digest=artifact,
        generated_test_suite_identity=suite,
    )
    stage = SimpleNamespace(output_identity=canonical_identity({"stage": "generate"}))
    build = SimpleNamespace(
        output={"source_bundle_digest": source_bundle, "artifact_digest": artifact}
    )
    admitted = SimpleNamespace(output={"tree": "inadmissible"})
    steps = {
        "build": build,
        **({"prepare-tree": admitted} if admitted_after_failure else {}),
    }
    run = SimpleNamespace(
        run_id="generation:test-candidate",
        status=GenerationStatus.FAILED,
        events=(
            SimpleNamespace(
                event_type="lifecycle-step-failed", stage_id=failed_stage, data={}
            ),
            SimpleNamespace(
                event_type="run-failed",
                stage_id=None,
                data={"code": failure_code},
            ),
        ),
        stage=lambda stage_id: stage if stage_id == "generate" else None,
        step=lambda step_id: steps.get(step_id),
    )
    failure = GenerationFailure(failure_code, "generated test execution failed", run)
    failure.__cause__ = rejected
    return failure


def generated_build_failure(
    *, build_failure_code: str = "builder.cpp_generated_source_rejected"
) -> GenerationFailure:
    files = {
        "source/main.cpp": "int main() { invalid generated source }\n",
        "source/tests/manifest.json": "{}",
    }
    stage = SimpleNamespace(
        output_identity=canonical_identity({"stage": "generate-build-rejection"}),
        response={"files": files},
    )
    completed = SimpleNamespace(output={"passed": True})
    steps = {
        "validate": completed,
        "classify": completed,
        "authorize-build": completed,
    }
    run = SimpleNamespace(
        run_id="generation:test-build-candidate",
        status=GenerationStatus.FAILED,
        events=(
            SimpleNamespace(
                event_type="lifecycle-step-failed",
                stage_id="build",
                data={"error_code": build_failure_code},
            ),
            SimpleNamespace(
                event_type="run-failed",
                stage_id=None,
                data={"code": "generation.lifecycle-step-failed"},
            ),
        ),
        stage=lambda stage_id: stage if stage_id == "generate" else None,
        step=lambda step_id: steps.get(step_id),
    )
    failure = GenerationFailure(
        "generation.lifecycle-step-failed", "generated source build failed", run
    )
    failure.__cause__ = BuildError(build_failure_code, "compiler rejected source")
    return failure


def generated_validation_failure(
    *,
    validation_failure_code: str = "dependencies.bzlmod-authority-unsupported",
    admitted_after_failure: bool = False,
) -> GenerationFailure:
    files = {
        "source/MODULE.bazel": 'module(name = "generated_without_version")\n',
        "source/tests/manifest.json": "{}",
    }
    stage = SimpleNamespace(
        output_identity=canonical_identity({"stage": "generate-validation-rejection"}),
        response={"files": files},
    )
    admitted = SimpleNamespace(output={"tree": "inadmissible"})
    steps = {"prepare-tree": admitted} if admitted_after_failure else {}
    run = SimpleNamespace(
        run_id="generation:test-validation-candidate",
        status=GenerationStatus.FAILED,
        events=(
            SimpleNamespace(
                event_type="lifecycle-step-failed",
                stage_id="validate",
                data={"error_code": validation_failure_code},
            ),
            SimpleNamespace(
                event_type="run-failed",
                stage_id=None,
                data={"code": "generation.lifecycle-step-failed"},
            ),
        ),
        stage=lambda stage_id: stage if stage_id == "generate" else None,
        step=lambda step_id: steps.get(step_id),
    )
    failure = GenerationFailure(
        "generation.lifecycle-step-failed", "generated source validation failed", run
    )
    failure.__cause__ = DependencyObservationError(
        validation_failure_code, "generated MODULE.bazel is invalid"
    )
    return failure


def generated_pipeline_validation_failure(
    *,
    finding_code: str = "validation.cpp_companion_header_missing",
    admitted_after_failure: bool = False,
) -> GenerationFailure:
    files = {
        "source/tests/litai_test.cpp": '#include "litai_test.hpp"\n',
        "source/tests/manifest.json": "{}",
    }
    stage = SimpleNamespace(
        output_identity=canonical_identity({"stage": "generate-pipeline-rejection"}),
        response={"files": files},
    )
    finding = {
        "validator_id": "validator:cpp-translation-unit-includes@1",
        "category": "correctness",
        "severity": "error",
        "code": finding_code,
        "message": "generated companion header is absent",
        "path": "source/tests/litai_test.cpp",
        "line": 1,
        "evidence_ids": [],
    }
    validation_output = {"passed": False, "findings": [finding]}
    validation = SimpleNamespace(
        output=validation_output,
        output_identity=canonical_identity(validation_output),
    )
    admitted = SimpleNamespace(output={"tree": "inadmissible"})
    steps = {
        "validate": validation,
        **({"classify": admitted} if admitted_after_failure else {}),
    }
    run = SimpleNamespace(
        run_id="generation:test-pipeline-validation-candidate",
        status=GenerationStatus.FAILED,
        events=(
            SimpleNamespace(
                event_type="tree-rejected",
                stage_id="validate",
                data={"reason": "validation did not pass"},
            ),
            SimpleNamespace(
                event_type="run-failed",
                stage_id=None,
                data={"code": "generation.validation-rejected"},
            ),
        ),
        stage=lambda stage_id: stage if stage_id == "generate" else None,
        step=lambda step_id: steps.get(step_id),
    )
    return GenerationFailure(
        "generation.validation-rejected", "generated source validation failed", run
    )


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

    def test_hello_unpinned_route_defaults_to_python(self):
        def standard_report(**kwargs):
            return SimpleNamespace(
                report={
                    "schema": "literate-ai/standard-sample-execution-report@1",
                    "sample_id": "hello-component",
                    "variant_id": kwargs["variant"].variant_id,
                    "passed": True,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "execute_standard_sample_variant",
                    side_effect=standard_report,
                ) as standard,
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e",
                ) as legacy,
            ):
                result = run_sample(
                    SAMPLES / "hello-component",
                    temporary / "runtime",
                    source_generator=object(),
                    standard_source_generator=object(),
                    cpp_toolchain=object(),
                    object_root=temporary / "objects",
                    allow_host_execution=True,
                )

        self.assertEqual(
            [item["variant_id"] for item in result["executions"]],
            ["python"],
        )
        self.assertEqual(standard.call_count, 1)
        self.assertEqual(
            [call.kwargs["variant"].variant_id for call in standard.call_args_list],
            ["python"],
        )
        legacy.assert_not_called()

    def test_unpinned_route_expands_only_after_explicit_language_wildcard(self):
        def standard_report(**kwargs):
            return SimpleNamespace(
                report={
                    "schema": "literate-ai/standard-sample-execution-report@1",
                    "sample_id": "hello-component",
                    "variant_id": kwargs["variant"].variant_id,
                    "passed": True,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with mock.patch(
                "tests.conformance.support.sample_runner."
                "execute_standard_sample_variant",
                side_effect=standard_report,
            ) as standard:
                result = run_sample(
                    SAMPLES / "hello-component",
                    temporary / "runtime",
                    source_generator=object(),
                    standard_source_generator=object(),
                    cpp_toolchain=object(),
                    object_root=temporary / "objects",
                    allow_host_execution=True,
                    flavor_selectors=("lang.*",),
                )

        expected = ["python"]
        self.assertEqual(
            [item["variant_id"] for item in result["executions"]], expected
        )
        self.assertEqual(
            [call.kwargs["variant"].variant_id for call in standard.call_args_list],
            expected,
        )

    def test_explicit_language_pin_is_not_replaced_by_wildcard(self):
        _metadata, definition, loaded, _closures = _load_sample(
            SAMPLES / "dependency-planner"
        )
        execution, _document = _execution_contract(
            SAMPLES / "dependency-planner", definition, loaded
        )
        self.assertEqual(
            [
                variant.variant_id
                for variant in _execution_variants(definition, execution, ("lang.*",))
            ],
            ["rust"],
        )

    def test_subtractive_selectors_narrow_an_explicit_language_matrix(self):
        _metadata, definition, loaded, _closures = _load_sample(
            SAMPLES / "regenerative-roundtrip"
        )
        execution, _document = _execution_contract(
            SAMPLES / "regenerative-roundtrip", definition, loaded
        )

        variants = _execution_variants(
            definition,
            execution,
            (
                "-lang-python",
                "-lang-rust",
                "-lang-javascript",
                "+lang-cpp",
            ),
        )

        self.assertEqual([variant.variant_id for variant in variants], ["cpp"])

    def test_subtractive_selectors_cannot_remove_every_pinned_language(self):
        _metadata, definition, loaded, _closures = _load_sample(
            SAMPLES / "dependency-planner"
        )
        execution, _document = _execution_contract(
            SAMPLES / "dependency-planner", definition, loaded
        )

        with self.assertRaisesRegex(SampleFailure, "removed every pinned language"):
            _execution_variants(definition, execution, ("-lang-rust",))

    def test_package_requirement_filters_language_build_product(self):
        sample = SAMPLES / "hello-component"
        _metadata, definition, loaded, _closures = _load_sample(sample)
        execution, _document = _execution_contract(sample, definition, loaded)
        variants = _execution_variants(
            definition,
            execution,
            (
                "flavor://literate-ai/lang-*",
                "flavor://literate-ai/build-*",
            ),
        )
        self.assertEqual(len(variants), 2)
        self.assertEqual(
            {variant.value_for("build-system") for variant in variants},
            {"make", "bazel"},
        )
        self.assertEqual(
            {variant.variant_id for variant in variants},
            {"python-make", "python-bazel"},
        )

    def test_dependency_planner_ordinary_route_uses_standard_rust_lifecycle(self):
        def standard_report(**kwargs):
            return SimpleNamespace(
                report={
                    "schema": "literate-ai/standard-sample-execution-report@1",
                    "sample_id": "dependency-planner",
                    "variant_id": kwargs["variant"].variant_id,
                    "passed": True,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "execute_standard_sample_variant",
                    side_effect=standard_report,
                ) as standard,
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e",
                ) as legacy,
            ):
                result = run_sample(
                    SAMPLES / "dependency-planner",
                    temporary / "runtime",
                    source_generator=object(),
                    standard_source_generator=object(),
                    cpp_toolchain=object(),
                    object_root=temporary / "objects",
                    allow_host_execution=True,
                )

        self.assertEqual(
            [item["variant_id"] for item in result["executions"]], ["rust"]
        )
        standard.assert_called_once()
        self.assertEqual(standard.call_args.kwargs["variant"].variant_id, "rust")
        legacy.assert_not_called()

    def test_critical_path_scheduler_unpinned_route_defaults_to_python(
        self,
    ):
        def standard_report(**kwargs):
            return SimpleNamespace(
                report={
                    "schema": "literate-ai/standard-sample-execution-report@1",
                    "sample_id": "critical-path-scheduler",
                    "variant_id": kwargs["variant"].variant_id,
                    "passed": True,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "execute_standard_sample_variant",
                    side_effect=standard_report,
                ) as standard,
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e",
                ) as legacy,
            ):
                result = run_sample(
                    SAMPLES / "critical-path-scheduler",
                    temporary / "runtime",
                    source_generator=object(),
                    standard_source_generator=object(),
                    cpp_toolchain=object(),
                    object_root=temporary / "objects",
                    allow_host_execution=True,
                )

        self.assertEqual(
            [item["variant_id"] for item in result["executions"]],
            ["python"],
        )
        self.assertEqual(standard.call_count, 1)
        self.assertEqual(
            [call.kwargs["variant"].variant_id for call in standard.call_args_list],
            ["python"],
        )
        legacy.assert_not_called()

    def test_javascript_ledger_ordinary_route_uses_standard_lifecycle(self):
        def standard_report(**kwargs):
            return SimpleNamespace(
                report={
                    "schema": "literate-ai/standard-sample-execution-report@1",
                    "sample_id": "javascript-ledger-workbench",
                    "variant_id": kwargs["variant"].variant_id,
                    "passed": True,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "execute_standard_sample_variant",
                    side_effect=standard_report,
                ) as standard,
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e",
                ) as legacy,
            ):
                result = run_sample(
                    SAMPLES / "javascript-ledger-workbench",
                    temporary / "runtime",
                    source_generator=object(),
                    standard_source_generator=object(),
                    cpp_toolchain=object(),
                    object_root=temporary / "objects",
                    allow_host_execution=True,
                )

        self.assertEqual(
            [item["variant_id"] for item in result["executions"]], ["javascript"]
        )
        standard.assert_called_once()
        self.assertEqual(standard.call_args.kwargs["variant"].variant_id, "javascript")
        legacy.assert_not_called()

    def test_publication_import_ordinary_route_uses_javascript_lifecycle(self):
        def standard_report(**kwargs):
            return SimpleNamespace(
                report={
                    "schema": "literate-ai/standard-sample-execution-report@1",
                    "sample_id": "publication-import",
                    "variant_id": kwargs["variant"].variant_id,
                    "passed": True,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "execute_standard_sample_variant",
                    side_effect=standard_report,
                ) as standard,
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e",
                ) as legacy,
            ):
                result = run_sample(
                    SAMPLES / "publication-import",
                    temporary / "runtime",
                    source_generator=object(),
                    standard_source_generator=object(),
                    cpp_toolchain=object(),
                    object_root=temporary / "objects",
                    allow_host_execution=True,
                )

        self.assertEqual(
            [item["variant_id"] for item in result["executions"]], ["javascript"]
        )
        standard.assert_called_once()
        self.assertEqual(standard.call_args.kwargs["variant"].variant_id, "javascript")
        legacy.assert_not_called()

    def test_generated_library_unpinned_route_defaults_to_python(self):
        def standard_report(**kwargs):
            return SimpleNamespace(
                report={
                    "schema": "literate-ai/standard-sample-execution-report@1",
                    "sample_id": "generated-library",
                    "variant_id": kwargs["variant"].variant_id,
                    "passed": True,
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "execute_standard_sample_variant",
                    side_effect=standard_report,
                ) as standard,
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e",
                ) as legacy,
            ):
                result = run_sample(
                    SAMPLES / "generated-library",
                    temporary / "runtime",
                    source_generator=object(),
                    standard_source_generator=object(),
                    cpp_toolchain=object(),
                    object_root=temporary / "objects",
                    allow_host_execution=True,
                )

        self.assertEqual(
            [item["variant_id"] for item in result["executions"]],
            ["python"],
        )
        self.assertEqual(standard.call_count, 1)
        self.assertEqual(
            [call.kwargs["variant"].variant_id for call in standard.call_args_list],
            ["python"],
        )
        legacy.assert_not_called()

    def test_unpinned_framework_scenario_apps_default_to_python(self):
        for sample_id in (
            "empty-cache-restart",
            "flavor-matrix",
            "model-routing",
            "security-policies",
        ):
            with self.subTest(sample_id=sample_id):

                def standard_report(*, _sample_id=sample_id, **kwargs):
                    return SimpleNamespace(
                        report={
                            "schema": (
                                "literate-ai/standard-sample-execution-report@1"
                            ),
                            "sample_id": _sample_id,
                            "variant_id": kwargs["variant"].variant_id,
                            "passed": True,
                        }
                    )

                with tempfile.TemporaryDirectory() as directory:
                    temporary = Path(directory)
                    with (
                        mock.patch(
                            "tests.conformance.support.sample_runner."
                            "execute_standard_sample_variant",
                            side_effect=standard_report,
                        ) as standard,
                        mock.patch(
                            "tests.conformance.support.sample_runner._run_host_e2e",
                        ) as legacy,
                    ):
                        result = run_sample(
                            SAMPLES / sample_id,
                            temporary / "runtime",
                            source_generator=object(),
                            standard_source_generator=object(),
                            cpp_toolchain=object(),
                            object_root=temporary / "objects",
                            allow_host_execution=True,
                        )

                self.assertEqual(
                    [item["variant_id"] for item in result["executions"]],
                    ["python"],
                )
                self.assertEqual(standard.call_count, 1)
                self.assertEqual(
                    [
                        call.kwargs["variant"].variant_id
                        for call in standard.call_args_list
                    ],
                    ["python"],
                )
                legacy.assert_not_called()

    def test_service_stack_python_ordinary_route_is_its_standard_lifecycle(self):
        standard_report = {
            "schema": "literate-ai/standard-sample-execution-report@1",
            "sample_id": "service-stack",
            "variant_id": "python",
            "node_count": 3,
            "project_build_plan_identity": "sha256:" + "1" * 64,
            "root_integration_evidence_identity": "sha256:" + "2" * 64,
            "passed": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "execute_standard_service_stack_variant",
                    return_value=SimpleNamespace(report=standard_report),
                ) as standard,
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e",
                    return_value={"variant_id": "cpp", "passed": True},
                ) as legacy,
            ):
                result = run_sample(
                    SAMPLES / "service-stack",
                    temporary / "runtime",
                    source_generator=object(),
                    standard_source_generator=object(),
                    cpp_toolchain=object(),
                    object_root=temporary / "objects",
                    allow_host_execution=True,
                )

        self.assertEqual(result["executions"][0], standard_report)
        self.assertNotIn("_operational_adoption_proofs", result)
        self.assertEqual(standard.call_count, 1)
        self.assertEqual(standard.call_args.kwargs["variant"].variant_id, "python")
        self.assertEqual(standard.call_args.kwargs["build_system"], "bazel")
        self.assertIsNotNone(standard.call_args.kwargs["invocation_arguments"])
        legacy.assert_not_called()

    def test_suite_aggregation_accepts_standard_portfolio_and_legacy_reports(self):
        source_a = "sha256:" + "a" * 64
        resolved_a = "sha256:" + "b" * 64
        source_b = "sha256:" + "c" * 64
        resolved_b = "sha256:" + "d" * 64
        source_c = "sha256:" + "e" * 64
        resolved_c = "sha256:" + "f" * 64
        source_d = "sha256:" + "1" * 64
        resolved_d = "sha256:" + "2" * 64
        cases = (
            {
                "sample_id": "standard",
                "assertions": [],
                "executions": [
                    {
                        "schema": "literate-ai/standard-sample-execution-report@1",
                        "generated_test_count": 3,
                        "independent_acceptance_case_count": 1,
                        "nodes": [
                            {
                                "source_sbom_identity": source_a,
                                "resolved_sbom_identity": resolved_a,
                            }
                        ],
                    }
                ],
                "passed": True,
            },
            {
                "sample_id": "legacy",
                "assertions": [],
                "executions": [
                    {
                        "execution_case_count": 5,
                        "generated_test_case_count": 3,
                        "source_sbom_identity": source_b,
                        "resolved_sbom_identity": resolved_b,
                    }
                ],
                "passed": True,
            },
            {
                "sample_id": "durable-split-service",
                "assertions": [],
                "executions": [
                    {
                        "schema": "literate-ai/durable-split-service-execution@1",
                        "proof_status": "passed",
                        "component_lock_identities": [source_c, source_d],
                        "frontend_root": {
                            "schema": "literate-ai/standard-sample-execution-report@1",
                            "passed": True,
                            "component_lock_identity": source_c,
                            "generated_test_count": 4,
                            "independent_acceptance_case_count": 2,
                            "nodes": [
                                {
                                    "source_sbom_identity": source_c,
                                    "resolved_sbom_identity": resolved_c,
                                }
                            ],
                        },
                        "collector_root": {
                            "schema": "literate-ai/standard-sample-execution-report@1",
                            "passed": True,
                            "component_lock_identity": source_d,
                            "generated_test_count": 5,
                            "independent_acceptance_case_count": 1,
                            "nodes": [
                                {
                                    "source_sbom_identity": source_d,
                                    "resolved_sbom_identity": resolved_d,
                                }
                            ],
                        },
                    }
                ],
                "passed": True,
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner.discover",
                    return_value=(
                        Path("standard"),
                        Path("legacy"),
                        Path("durable-split-service"),
                    ),
                ),
                mock.patch(
                    "tests.conformance.support.sample_runner.discover_cpp_toolchain",
                    side_effect=AssertionError(
                        "a selected non-C++ sample must not discover C++"
                    ),
                ) as cpp_discovery,
                mock.patch(
                    "tests.conformance.support.sample_runner.run_sample",
                    side_effect=cases,
                ),
            ):
                report = run_all(
                    SAMPLES,
                    temporary / "runtime",
                    source_generator=object(),
                    allow_host_execution=True,
                    jobs=1,
                )

        cpp_discovery.assert_not_called()
        self.assertEqual(report["generated_test_count"], 15)
        self.assertEqual(report["invocation_count"], 21)
        self.assertEqual(report["verifier_test_count"], 6)
        self.assertEqual(
            report["source_sbom_members"],
            [source_d, source_a, source_b, source_c],
        )
        self.assertEqual(
            report["resolved_sbom_members"],
            [resolved_d, resolved_a, resolved_b, resolved_c],
        )

    def test_derivation_manifest_plans_both_durable_standard_roots(self):
        selection = SimpleNamespace(
            name="codex",
            tool_binding_identity=canonical_identity({"fixture": "tool"}).uri,
        )

        class DelegateFixture:
            def __init__(self, *args, **kwargs):
                del args, kwargs
                self.selection = selection

            def planned_request_identity(self, recipe, **kwargs):
                return canonical_identity(
                    {
                        "recipe": recipe.identity,
                        "execution": kwargs["execution_plan"].identity.uri,
                        "stage": kwargs["stage_request"],
                    }
                )

        class CachedGeneratorFixture:
            def __init__(self, *args, **kwargs):
                del args, kwargs
                self.selection = selection

            def derivation_cache_key(
                self,
                recipe,
                *,
                execution_plan,
                stage_request,
                bounded_prompt,
            ):
                return SourceDerivationCacheKey(
                    recipe_identity=ContentIdentity.parse_uri(recipe.identity),
                    execution_plan_identity=execution_plan.identity,
                    coding_cli_tool_binding_identity=ContentIdentity.parse_uri(
                        selection.tool_binding_identity
                    ),
                    model_binding=SourceCacheModelBinding(
                        selection.name, recipe.model_for(selection.name)
                    ),
                    request_identity=canonical_identity(
                        {
                            "stage": stage_request,
                            "prompt": hashlib.sha256(bounded_prompt).hexdigest(),
                        }
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner.discover",
                    return_value=(SAMPLES / "durable-split-service",),
                ),
                mock.patch(
                    "tests.conformance.support.sample_runner.CodingCliSourceGenerator",
                    DelegateFixture,
                ),
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "CachedCodingCliSourceGenerator",
                    CachedGeneratorFixture,
                ),
                mock.patch(
                    "tests.conformance.support.sample_runner.discover_cpp_toolchain"
                ),
            ):
                manifest = plan_derivation_manifest(
                    SAMPLES,
                    temporary,
                    planning_request_identity=canonical_identity(
                        {"fixture": "planning-request"}
                    ),
                    project_revision_identity=canonical_identity(
                        {"fixture": "project"}
                    ),
                    lifecycle_driver_identity=canonical_identity({"fixture": "driver"}),
                    pipeline_model="fixture-model",
                )

        self.assertEqual(len(manifest.cache_keys), 5)
        self.assertEqual(len(manifest.component_lock_identities), 2)
        self.assertEqual(len({key.recipe_identity for key in manifest.cache_keys}), 5)
        self.assertEqual(
            {key.model_binding.model_selector for key in manifest.cache_keys},
            {"fixture-model"},
        )

    def test_standard_sample_model_selection_uses_bound_adapter(self):
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
        snapshot = _StandardSampleAuthoritySnapshot(authority, catalog)

        identities = _locked_standard_sample_model_identities(
            snapshot,
            coding_cli="codex",
        )

        self.assertEqual(
            set(identities),
            {node.revision.identity.uri for node in authority.lock.nodes},
        )
        self.assertTrue(
            all(
                isinstance(identity, ContentIdentity)
                for identity in identities.values()
            )
        )

    def test_standard_sample_pipeline_model_changes_recipe_and_cache_identity(self):
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
        snapshot = _StandardSampleAuthoritySnapshot(authority, catalog)

        first = _locked_standard_sample_model_bindings(
            snapshot, coding_cli="codex", pipeline_model="model-a"
        )
        second = _locked_standard_sample_model_bindings(
            snapshot, coding_cli="codex", pipeline_model="model-b"
        )
        revision = authority.lock.root_revision.uri

        self.assertEqual(first[revision].model_selector, "model-a")
        self.assertEqual(second[revision].model_selector, "model-b")
        self.assertNotEqual(first[revision].identity, second[revision].identity)
        first_plan = StandardProjectApplicationService.plan(
            authority.lock,
            model_identities={revision: first[revision].identity},
        )
        second_plan = StandardProjectApplicationService.plan(
            authority.lock,
            model_identities={revision: second[revision].identity},
        )
        self.assertNotEqual(
            first_plan.generation_plans[0].generation_key.identity,
            second_plan.generation_plans[0].generation_key.identity,
        )

    def test_sample_parser_accepts_explicit_pipeline_model(self):
        parsed = _parser().parse_args(["--model", "provider/model-v2"])

        self.assertEqual(parsed.model, "provider/model-v2")

    def test_live_sample_entry_requires_explicit_coding_cli_and_model(self):
        from literate_ai.adapters.live_test_selection import (
            LIVE_MODEL_ENVIRONMENT,
            TEST_CONFIG_ENVIRONMENT,
        )

        with tempfile.TemporaryDirectory() as temporary:
            overlay = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "CODING_CLI",
                    LIVE_MODEL_ENVIRONMENT,
                    TEST_CONFIG_ENVIRONMENT,
                }
            }
            overlay[TEST_CONFIG_ENVIRONMENT] = str(
                Path(temporary) / "absent-literate.test.json"
            )
            with mock.patch.dict(os.environ, overlay, clear=True):
                with self.assertRaisesRegex(
                    SampleFailure, "coding_cli.test_selection_unconfigured"
                ):
                    main(["--allow-host-execution"], require_live_selection=True)

    def test_live_sample_cli_override_is_used_without_rewriting_test_config(self):
        from literate_ai.adapters.live_test_selection import (
            LIVE_MODEL_ENVIRONMENT,
            TEST_CONFIG_ENVIRONMENT,
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "literate.test.json"
            original = (
                json.dumps(
                    {
                        "schema": "literate-ai/global-test-matrix@3",
                        "coding_cli": "opencode",
                        "model": "from-file",
                        "default_samples": ["hello-component"],
                        "workers": [{"worker_id": "local"}],
                    }
                )
                + "\n"
            )
            path.write_text(original, encoding="utf-8")
            captured: dict[str, object] = {}

            def fake_run_all(*_args, **kwargs):
                captured["pipeline_model"] = kwargs.get("pipeline_model")
                captured["coding_cli"] = os.environ.get("CODING_CLI")
                captured["live_model"] = os.environ.get(LIVE_MODEL_ENVIRONMENT)
                return {"passed": True}

            overlay = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "CODING_CLI",
                    LIVE_MODEL_ENVIRONMENT,
                    "LITAI_SKIP_MODEL_PREFLIGHT",
                    TEST_CONFIG_ENVIRONMENT,
                }
            }
            overlay[TEST_CONFIG_ENVIRONMENT] = str(path)
            with (
                mock.patch.dict(os.environ, overlay, clear=True),
                mock.patch(
                    "tests.conformance.support.sample_runner.run_all",
                    side_effect=fake_run_all,
                ),
                mock.patch(
                    "tests.conformance.support.sample_runner.verify_live_model_resolves"
                ) as verify_model,
                mock.patch("sys.stdout", io.StringIO()),
            ):
                status = main(
                    [
                        "--allow-host-execution",
                        "--coding-cli",
                        "cursor-agent",
                        "--model",
                        "gpt-5.6-sol-high",
                        "--sample",
                        "hello-component",
                    ],
                    require_live_selection=True,
                )
            self.assertEqual(status, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(captured["coding_cli"], "cursor-agent")
            self.assertEqual(captured["live_model"], "gpt-5.6-sol-high")
            self.assertEqual(captured["pipeline_model"], "gpt-5.6-sol-high")
            verify_model.assert_called_once()
            selection = verify_model.call_args.args[0]
            self.assertEqual(selection.coding_cli, "cursor-agent")
            self.assertEqual(selection.model, "gpt-5.6-sol-high")

    def test_derivation_planning_resolves_model_without_running_preflight(self):
        from literate_ai.adapters.cache.rebuild import (
            SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT,
            SOURCE_CACHE_PLANNING_MODE,
            SOURCE_CACHE_PLANNING_MODE_ENVIRONMENT,
            SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT,
        )
        from literate_ai.adapters.live_test_selection import (
            LIVE_MODEL_ENVIRONMENT,
            TEST_CONFIG_ENVIRONMENT,
        )

        project_revision = canonical_identity({"fixture": "project-revision"})
        planning_request = canonical_identity({"fixture": "planning-request"})
        driver_identity = canonical_identity({"fixture": "driver"})
        manifest = SimpleNamespace(
            identity=canonical_identity({"fixture": "manifest"}), cache_keys=()
        )
        project = SimpleNamespace(
            root=REPO_ROOT,
            definition=SimpleNamespace(
                lifecycle_driver=SimpleNamespace(identity=driver_identity)
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            runtime = temporary / "runtime"
            protocol = runtime / ".litai"
            protocol.mkdir(parents=True)
            manifest_path = protocol / "source-cache-derivations.json"
            test_config = temporary / "test.json"
            test_config.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/global-test-matrix@3",
                        "coding_cli": "codex",
                        "model": "configured-model",
                        "default_samples": ["hello-component"],
                        "workers": [{"worker_id": "local"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            environment = {
                SOURCE_CACHE_PLANNING_MODE_ENVIRONMENT: (SOURCE_CACHE_PLANNING_MODE),
                SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT: str(manifest_path),
                SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT: (
                    planning_request.uri
                ),
                TEST_CONFIG_ENVIRONMENT: str(test_config),
            }
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch(
                    "tests.conformance.support.sample_runner.discover_project",
                    return_value=project,
                ),
                mock.patch(
                    "tests.conformance.support.sample_runner._validated_project_revision",
                    return_value=project_revision,
                ),
                mock.patch(
                    "tests.conformance.support.sample_runner.plan_derivation_manifest",
                    return_value=manifest,
                ) as plan,
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "write_rebuild_source_cache_derivation_manifest"
                ) as write_manifest,
                mock.patch(
                    "tests.conformance.support.sample_runner.verify_live_model_resolves"
                ) as preflight,
                mock.patch(
                    "tests.conformance.support.sample_runner.log_live_session_models"
                ) as log_session,
                mock.patch("sys.stdout", io.StringIO()),
            ):
                status = main(
                    [
                        "--runtime-root",
                        str(runtime),
                        "--lifecycle-project",
                        str(REPO_ROOT),
                        "--lifecycle-specification",
                        str(REPO_ROOT),
                        "--lifecycle-project-revision",
                        project_revision.uri,
                    ],
                    require_live_selection=True,
                )
                self.assertEqual(os.environ["CODING_CLI"], "codex")
                self.assertEqual(os.environ[LIVE_MODEL_ENVIRONMENT], "configured-model")

        self.assertEqual(status, 0)
        self.assertEqual(plan.call_args.kwargs["pipeline_model"], "configured-model")
        write_manifest.assert_called_once_with(manifest_path, manifest)
        preflight.assert_not_called()
        log_session.assert_not_called()

    def test_standard_sample_recipe_carries_the_exact_pipeline_model_scope(self):
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
        snapshot = _StandardSampleAuthoritySnapshot(authority, catalog)
        bindings = _locked_standard_sample_model_bindings(
            snapshot, coding_cli="codex", pipeline_model="provider/model-v2"
        )
        execution = StandardProjectApplicationService.plan(
            authority.lock,
            model_identities={
                revision: binding.identity for revision, binding in bindings.items()
            },
        )

        with tempfile.TemporaryDirectory() as temporary:
            prepared = _prepare_standard_sample_project(
                snapshot=snapshot,
                execution_plan=execution,
                source_root=Path(temporary),
                coding_cli="codex",
                pipeline_model="provider/model-v2",
            )

        recipe = prepared.nodes[0].recipe
        self.assertEqual(recipe.model_for("codex"), "provider/model-v2")
        self.assertEqual(
            recipe.model_scope.identity,
            bindings[authority.lock.root_revision.uri].identity,
        )
        self.assertIn('"model_selector":"provider/model-v2"', recipe.prompt())

    def test_structured_provider_samples_project_their_public_contract_once(self):
        expected_contracts = {
            "loan-risk-gate": b"# Loan Risk Gate Public Interface 1.0\n",
            "playback-controller": b"# Playback Controller Public Interface 1.0\n",
        }
        for sample_name, contract_heading in expected_contracts.items():
            with self.subTest(sample=sample_name):
                sample = SAMPLES / sample_name
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
                snapshot = _StandardSampleAuthoritySnapshot(authority, catalog)
                bindings = _locked_standard_sample_model_bindings(
                    snapshot,
                    coding_cli="codex",
                    pipeline_model="provider/model-v2",
                )
                execution = StandardProjectApplicationService.plan(
                    authority.lock,
                    model_identities={
                        revision: binding.identity
                        for revision, binding in bindings.items()
                    },
                )

                with tempfile.TemporaryDirectory() as temporary:
                    prepared = _prepare_standard_sample_project(
                        snapshot=snapshot,
                        execution_plan=execution,
                        source_root=Path(temporary),
                        coding_cli="codex",
                        pipeline_model="provider/model-v2",
                    )

                locked_node = authority.lock.nodes[0]
                self.assertEqual(len(locked_node.revision.public_interfaces), 1)
                interface = locked_node.revision.public_interfaces[0]
                interface_bytes = catalog.component_content(
                    locked_node.revision.authoring_identity, interface
                )
                prompt = prepared.nodes[0].request.prompt
                self.assertEqual(
                    interface_bytes[: len(contract_heading)], contract_heading
                )
                self.assertEqual(prompt.count(interface_bytes), 1)

    def test_standard_sample_reports_bounded_redacted_generation_failure(self):
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
        credential = "test-only-provider-credential"
        messages = []
        keys = []
        for tail in ("provider temporarily unavailable", "provider overloaded"):
            with (
                self.subTest(diagnostic=tail),
                tempfile.TemporaryDirectory() as directory,
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.select_coding_cli",
                    return_value=CodingCliSelection(
                        "codex",
                        str(Path(sys.executable).resolve()),
                        "sha256:"
                        + hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
                    ),
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    return_value=SimpleNamespace(
                        returncode=7,
                        stdout=b"",
                        stderr=("noise " * 4000 + f"{credential} {tail}").encode(),
                    ),
                ) as provider,
                mock.patch(
                    "literate_ai.adapters.models.coding_cli."
                    "_require_codex_linux_workspace_write_prerequisite"
                ),
            ):
                temporary = Path(directory)
                generator = CachedCodingCliSourceGenerator(
                    CodingCliSourceGenerator(
                        environment={
                            "CODING_CLI": "codex",
                            "PATH": os.environ.get("PATH", ""),
                            "OPENAI_API_KEY": credential,
                        }
                    ),
                    cache_root=temporary / "source-cache",
                    project_root=REPO_ROOT,
                )
                keys.append(
                    _plan_standard_sample_derivations(
                        sample_root=sample,
                        sample_id="hello-component",
                        definition=definition,
                        loaded=loaded,
                        contract=contract,
                        variant=variant,
                        input_closure=closures.generation,
                        generator=generator,
                        scratch=temporary / "plan",
                        pipeline_model="provider/model-v2",
                    )
                )
                with self.assertRaises(SampleFailure) as raised:
                    execute_standard_sample_variant(
                        sample_root=sample,
                        sample_id="hello-component",
                        variant=variant,
                        authority=authority,
                        catalog=catalog,
                        source_generator=generator,
                        scratch=temporary / "runtime",
                        object_root=temporary / "objects",
                        pipeline_model="provider/model-v2",
                    )
                message = str(raised.exception)
                messages.append(message)
                provider.assert_called()
                self.assertIn("coding_cli.generation_failed", message)
                self.assertIn("exit status 7", message)
                self.assertIn(tail, message)
                self.assertNotIn("diagnostics={}", message)
                self.assertNotIn(credential, message)
                self.assertLess(len(message), 9000)
                self.assertNotIn(str(temporary), message)
        self.assertNotEqual(messages[0], messages[1])
        self.assertEqual(keys[0], keys[1])

    def test_standard_sample_runner_receives_the_planned_locked_assets(self):
        sample = SAMPLES / "loan-risk-gate"
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
        snapshot = _StandardSampleAuthoritySnapshot(authority, catalog)
        bindings = _locked_standard_sample_model_bindings(
            snapshot, coding_cli="codex", pipeline_model="provider/model-v2"
        )
        expected_plan = StandardProjectApplicationService.plan(
            authority.lock,
            model_identities={
                revision: binding.identity for revision, binding in bindings.items()
            },
        )

        class RuntimeCompositionReached(Exception):
            pass

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            executable = temporary / "codex"
            executable.write_bytes(b"#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            source_generator = SimpleNamespace(
                selection=CodingCliSelection(
                    "codex",
                    str(executable.resolve()),
                    "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
                )
            )
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "CachedCodingCliSourceGenerationRunner"
                ) as runner,
                mock.patch(
                    "tests.conformance.support.sample_runner."
                    "assemble_filesystem_standard_project_runtime",
                    side_effect=RuntimeCompositionReached,
                ),
                self.assertRaises(RuntimeCompositionReached),
            ):
                execute_standard_sample_variant(
                    sample_root=sample,
                    sample_id="loan-risk-gate",
                    variant=variant,
                    authority=authority,
                    catalog=catalog,
                    source_generator=source_generator,
                    scratch=temporary / "runtime",
                    object_root=temporary / "objects",
                    pipeline_model="provider/model-v2",
                )
            runner_assets = runner.call_args.kwargs["assets"]
            runner_cas = runner.call_args.kwargs["cas"]
            runner_asset_content = runner_cas.get_bytes(runner_assets[0].blob)

        self.assertEqual(len(runner_assets), 1)
        self.assertEqual(runner_assets[0].asset_id, "score-bands")
        self.assertEqual(runner_assets[0].path, "source/data/score-bands.json")
        self.assertEqual(
            runner_asset_content,
            (sample / "assets/score-bands.json").read_bytes(),
        )
        expected_assets = expected_plan.generation_plans[
            0
        ].generation_key.asset_identities
        self.assertEqual(
            tuple(sorted(item.identity.uri for item in runner_assets)),
            tuple(item.uri for item in expected_assets),
        )

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

    def test_service_stack_handler_consumes_the_ordinary_standard_report(self):
        report = {
            "schema": "literate-ai/standard-sample-execution-report@1",
            "sample_id": "service-stack",
            "variant_id": "python",
            "node_count": 3,
            "project_build_plan_identity": "sha256:" + "1" * 64,
            "root_integration_evidence_identity": "sha256:" + "2" * 64,
        }
        with tempfile.TemporaryDirectory() as directory:
            assertions = _service_stack(
                SAMPLES / "service-stack", Path(directory), (report,)
            )

        self.assertEqual(
            assertions, ("service-stack-resolves", "transitive-library-is-exact")
        )

    def test_only_exact_attributable_application_nonzero_is_candidate_rejection(
        self,
    ) -> None:
        stdout_digest = f"sha256:{hashlib.sha256(b'partial output').hexdigest()}"
        stderr_digest = f"sha256:{hashlib.sha256(b'subtotal overflow').hexdigest()}"
        error = HostArtifactExecutionError(
            "host_execution.nonzero_exit",
            "generated application failed",
            role="application",
            returncode=1,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
        )

        rejection = _generated_application_rejection(
            verification_source="generated-implementation-test",
            case_id="generated-overflow",
            expected_result={"total": 630},
            execution_error=error,
        )

        self.assertIsInstance(rejection, _GeneratedApplicationRejected)
        assert rejection is not None
        self.assertEqual(rejection.rejection_kind, "generated-application-nonzero-exit")
        self.assertEqual(
            rejection.case_evidence,
            {
                "case_id": "generated-overflow",
                "expected_result_identity": canonical_identity({"total": 630}).uri,
                "execution_failure_code": "host_execution.nonzero_exit",
                "execution_role": "application",
                "returncode": 1,
                "stdout_digest": stdout_digest,
                "stderr_digest": stderr_digest,
            },
        )

        independent = _generated_application_rejection(
            verification_source="post-build-runtime-oracle",
            case_id="runtime-generalization",
            expected_result={"total": 630},
            execution_error=error,
        )
        self.assertIsInstance(independent, _IndependentApplicationRejected)
        assert independent is not None
        self.assertEqual(
            independent.rejection_kind,
            "independent-verifier-application-nonzero-exit",
        )
        self.assertEqual(
            independent.case_evidence["verification_source"],
            "post-build-runtime-oracle",
        )

        terminal_cases = (
            ("independent-verifier", error),
            (
                "generated-implementation-test",
                HostArtifactExecutionError(
                    "host_execution.timeout",
                    "generated application timed out",
                    role="application",
                ),
            ),
            (
                "generated-implementation-test",
                HostArtifactExecutionError(
                    "host_execution.nonzero_exit",
                    "untyped process failure",
                ),
            ),
            (
                "generated-implementation-test",
                HostArtifactExecutionError(
                    "host_execution.nonzero_exit",
                    "invalid digest evidence",
                    role="application",
                    returncode=1,
                    stdout_digest="sha256:not-a-digest",
                    stderr_digest=stderr_digest,
                ),
            ),
        )
        for verification_source, terminal_error in terminal_cases:
            with self.subTest(
                verification_source=verification_source,
                code=terminal_error.code,
            ):
                self.assertIsNone(
                    _generated_application_rejection(
                        verification_source=verification_source,
                        case_id="generated-overflow",
                        expected_result={"total": 630},
                        execution_error=terminal_error,
                    )
                )

    def test_generated_application_nonzero_retries_with_process_evidence(self) -> None:
        source_bundle = canonical_identity({"fixture": "source-bundle"}).uri
        artifact = canonical_identity({"fixture": "artifact"}).uri
        suite = canonical_identity({"fixture": "generated-suite"}).uri
        application_rejection = _GeneratedApplicationRejected(
            case_id="generated-overflow",
            expected_result={"total": 630},
            execution_error=HostArtifactExecutionError(
                "host_execution.nonzero_exit",
                "subtotal overflow",
                role="application",
                returncode=1,
                stdout_digest=f"sha256:{hashlib.sha256(b'').hexdigest()}",
                stderr_digest=(
                    f"sha256:{hashlib.sha256(b'subtotal overflow').hexdigest()}"
                ),
            ),
        )
        marker = _GeneratedCandidateRejected(
            application_rejection,
            source_bundle_digest=source_bundle,
            artifact_digest=artifact,
            generated_test_suite_identity=suite,
        )
        failure = generated_candidate_failure()
        failure.__cause__ = marker
        variant = _ExecutionVariant("cpp", (), ("cpp",))

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch(
                "tests.conformance.support.sample_runner._run_host_e2e_once",
                side_effect=(failure, {"passed": True}),
            ),
        ):
            result = _run_host_e2e(
                Path("sample"),
                Path(temporary),
                "sample",
                oracle_reference_fixture(),
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                variant=variant,
                source_generator=object(),
                allow_host_execution=True,
            )

        rejection = result["rejected_generation_candidates"][0]
        self.assertEqual(
            rejection["rejection_kind"], "generated-application-nonzero-exit"
        )
        self.assertEqual(rejection["execution_role"], "application")
        self.assertEqual(rejection["returncode"], 1)
        self.assertIn("stdout_digest", rejection)
        self.assertIn("stderr_digest", rejection)

    def test_generated_behavior_rejection_retries_in_a_fresh_attempt_root(self) -> None:
        failure = generated_candidate_failure()
        variant = _ExecutionVariant("cpp", (), ("cpp",))

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch(
                "tests.conformance.support.sample_runner._run_host_e2e_once",
                side_effect=(failure, {"passed": True}),
            ) as execute,
        ):
            result = _run_host_e2e(
                Path("sample"),
                Path(temporary),
                "sample",
                oracle_reference_fixture(),
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                variant=variant,
                source_generator=object(),
                allow_host_execution=True,
            )

        self.assertEqual(result["generation_candidate_attempt_count"], 2)
        self.assertEqual(len(result["rejected_generation_candidates"]), 1)
        rejection = result["rejected_generation_candidates"][0]
        self.assertEqual(rejection["attempt"], 1)
        self.assertEqual(
            rejection["schema"], "literate-ai/generated-candidate-rejection@1"
        )
        self.assertEqual(
            rejection["rejection_kind"], "generated-test-behavior-mismatch"
        )
        self.assertEqual(execute.call_count, 2)
        attempts = [call.args[1] for call in execute.call_args_list]
        self.assertNotEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[0].name, "01")
        self.assertEqual(attempts[1].name, "02")
        for index in range(len(execute.call_args_list[0].args)):
            if index != 1:
                self.assertIs(
                    execute.call_args_list[0].args[index],
                    execute.call_args_list[1].args[index],
                )

    def test_generated_test_contract_failure_authorizes_candidate_replacement(
        self,
    ) -> None:
        cause = CodingCliError(
            "generated_tests.acceptance_signature_missing",
            "generated suite used another callable signature",
        )
        run = SimpleNamespace(
            run_id="generation:bad-signature",
            status=GenerationStatus.FAILED,
            events=(
                SimpleNamespace(
                    event_type="model-stage-failed", stage_id="generate", data={}
                ),
                SimpleNamespace(
                    event_type="run-failed",
                    stage_id=None,
                    data={"code": "generation.model-stage-failed"},
                ),
            ),
            step=lambda _step_id: None,
        )
        failure = GenerationFailure(
            "generation.model-stage-failed",
            "generation candidate failed its value-free invocation contract",
            run,
        )
        failure.__cause__ = cause

        rejection = _generated_candidate_rejection(failure, attempt=1)

        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(
            rejection["rejection_kind"], "generated-test-contract-rejected"
        )
        self.assertEqual(
            rejection["contract_failure_code"],
            "generated_tests.acceptance_signature_missing",
        )
        self.assertEqual(
            rejection["message"],
            "generated suite used another callable signature",
        )
        self.assertNotIn("artifact_digest", rejection)
        self.assertNotIn("source_bundle_digest", rejection)

    def test_generated_cpp_bazel_contract_failures_return_actionable_feedback(
        self,
    ) -> None:
        failures = (
            (
                "coding_cli.generated_cpp_bazel_output_collision",
                "generated genrule output collides with cc_binary",
            ),
            (
                "coding_cli.generated_cpp_bazel_hdrs_unsupported",
                "generated cc_binary uses unsupported hdrs",
            ),
            (
                "coding_cli.generated_cpp_bazel_workspace_include_unsupported",
                'generated cc_binary uses includes = ["."]',
            ),
        )
        for code, message in failures:
            with self.subTest(code=code):
                cause = CodingCliError(code, message)
                run = SimpleNamespace(
                    run_id="generation:bad-rules-cc-attributes",
                    status=GenerationStatus.FAILED,
                    events=(
                        SimpleNamespace(
                            event_type="model-stage-failed",
                            stage_id="generate",
                            data={},
                        ),
                        SimpleNamespace(
                            event_type="run-failed",
                            stage_id=None,
                            data={"code": "generation.model-stage-failed"},
                        ),
                    ),
                    step=lambda _step_id: None,
                )
                failure = GenerationFailure(
                    "generation.model-stage-failed",
                    "generation candidate violated the generated-source contract",
                    run,
                )
                failure.__cause__ = cause

                rejection = _generated_candidate_rejection(failure, attempt=1)

                self.assertIsNotNone(rejection)
                assert rejection is not None
                self.assertEqual(
                    rejection["rejection_kind"],
                    "generated-source-contract-rejected",
                )
                self.assertEqual(rejection["contract_failure_code"], code)
                self.assertEqual(rejection["message"], message)

    def test_generated_behavior_rejection_without_exact_failed_run_is_not_retried(
        self,
    ) -> None:
        for failure in (
            GenerationFailure("generation.lifecycle-step-failed", "missing run"),
            generated_candidate_failure(failed_stage="resolve-dependencies"),
            generated_candidate_failure(failure_code="generation.validation-rejected"),
            generated_candidate_failure(admitted_after_failure=True),
        ):
            with self.subTest(message=str(failure)):
                self.assertIsNone(_generated_candidate_rejection(failure, attempt=1))

    def test_independent_verifier_mismatch_authorizes_candidate_replacement(
        self,
    ) -> None:
        source_bundle = canonical_identity({"fixture": "source-bundle"}).uri
        artifact = canonical_identity({"fixture": "artifact"}).uri
        acceptance_suite = canonical_identity({"fixture": "acceptance-suite"}).uri
        mismatch = _IndependentBehaviorMismatch(
            case_id="runtime-generalization",
            verification_source="post-build-runtime-oracle",
            expected_result={"word_count": 4},
            observed_result={"word_count": 22},
        )
        rejected = _IndependentCandidateRejected(
            mismatch,
            source_bundle_digest=source_bundle,
            artifact_digest=artifact,
            independent_acceptance_suite_identity=acceptance_suite,
        )
        stage = SimpleNamespace(output_identity=canonical_identity("generate"))
        completed = SimpleNamespace(
            output={
                "source_bundle_digest": source_bundle,
                "artifact_digest": artifact,
            }
        )
        steps = {"build": completed, "test-generated": completed}
        run = SimpleNamespace(
            run_id="generation:independently-rejected",
            status=GenerationStatus.FAILED,
            events=(
                SimpleNamespace(
                    event_type="lifecycle-step-failed",
                    stage_id="verify-independent",
                    data={},
                ),
                SimpleNamespace(
                    event_type="run-failed",
                    stage_id=None,
                    data={"code": "generation.lifecycle-step-failed"},
                ),
            ),
            stage=lambda stage_id: stage if stage_id == "generate" else None,
            step=lambda step_id: steps.get(step_id),
        )
        failure = GenerationFailure(
            "generation.lifecycle-step-failed",
            "independent verifier rejected generated source",
            run,
        )
        failure.__cause__ = rejected

        rejection = _generated_candidate_rejection(failure, attempt=2)

        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(
            rejection["rejection_kind"],
            "independent-verifier-behavior-mismatch",
        )
        self.assertEqual(rejection["verification_source"], "post-build-runtime-oracle")
        self.assertEqual(rejection["artifact_digest"], artifact)
        self.assertEqual(
            rejection["independent_acceptance_suite_identity"], acceptance_suite
        )
        self.assertNotIn("word_count", canonical_json_bytes(rejection).decode("utf-8"))

    def test_generated_candidate_cause_cycle_fails_closed(self) -> None:
        failure = generated_candidate_failure()
        cyclic = SampleFailure("cyclic cause")
        cyclic.__cause__ = cyclic
        failure.__cause__ = cyclic

        self.assertIsNone(_generated_candidate_rejection(failure, attempt=1))

    def test_real_orchestrator_failure_tail_authorizes_candidate_replacement(
        self,
    ) -> None:
        from tests.unit.test_application_generation import (
            GenerationOrchestratorTests,
        )

        class RejectingGeneratedTestRunner:
            runner_id = "test-generated-candidate-rejector"

            def run(
                self,
                artifact,
                build,
                classification,
                dependency_resolution,
                test_suite,
            ):
                del classification, dependency_resolution
                mismatch = _GeneratedBehaviorMismatch(
                    case_id="orchestrator-case",
                    expected_result={"value": 2},
                    observed_result={"value": 1},
                )
                raise _GeneratedCandidateRejected(
                    mismatch,
                    source_bundle_digest=str(artifact["source_bundle_digest"]),
                    artifact_digest=str(build["artifact_digest"]),
                    generated_test_suite_identity=test_suite.content_identity,
                ) from mismatch

        fixture = GenerationOrchestratorTests(methodName="runTest")
        request, orchestrator, _, _, _, _ = fixture.make_system()
        orchestrator.generated_test_runner = RejectingGeneratedTestRunner()

        with self.assertRaises(GenerationFailure) as raised:
            orchestrator.execute(request)

        run = raised.exception.run
        assert run is not None
        self.assertEqual(
            [(event.event_type, event.stage_id) for event in run.events[-2:]],
            [("lifecycle-step-failed", "test-generated"), ("run-failed", None)],
        )
        rejection = _generated_candidate_rejection(raised.exception, attempt=1)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection["case_id"], "orchestrator-case")

    def test_real_orchestrator_compile_failure_authorizes_candidate_replacement(
        self,
    ) -> None:
        from tests.unit.test_application_generation import (
            GenerationOrchestratorTests,
        )

        class RejectingBuilder:
            def __init__(self, builder_id: str) -> None:
                self.builder_id = builder_id

            def build(self, request, authorization):
                del request, authorization
                raise BuildError(
                    "builder.cpp_generated_source_rejected",
                    "healthy compiler rejected generated source",
                )

        fixture = GenerationOrchestratorTests(methodName="runTest")
        request, orchestrator, _, _, _, _ = fixture.make_system()
        orchestrator.builder = RejectingBuilder(orchestrator.builder.builder_id)

        with self.assertRaises(GenerationFailure) as raised:
            orchestrator.execute(request)

        rejection = _generated_candidate_rejection(raised.exception, attempt=1)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection["rejection_kind"], "generated-source-build-rejected")
        self.assertEqual(
            rejection["build_failure_code"],
            "builder.cpp_generated_source_rejected",
        )
        self.assertIn("candidate_tree_identity", rejection)

    def test_bazel_generated_graph_failure_authorizes_candidate_replacement(
        self,
    ) -> None:
        from tests.unit.test_application_generation import (
            GenerationOrchestratorTests,
        )

        class RejectingBuilder:
            def __init__(self, builder_id: str) -> None:
                self.builder_id = builder_id

            def build(self, request, authorization):
                del request, authorization
                raise BuildError(
                    "builder.bazel_analyze_failed",
                    "generated Bazel graph is internally inconsistent",
                )

        fixture = GenerationOrchestratorTests(methodName="runTest")
        request, orchestrator, _, _, _, _ = fixture.make_system()
        orchestrator.builder = RejectingBuilder(orchestrator.builder.builder_id)

        with self.assertRaises(GenerationFailure) as raised:
            orchestrator.execute(request)

        rejection = _generated_candidate_rejection(raised.exception, attempt=1)
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(rejection["rejection_kind"], "generated-source-build-rejected")
        self.assertEqual(
            rejection["build_failure_code"], "builder.bazel_analyze_failed"
        )

    def test_static_bzlmod_validation_rejection_retries_fail_closed(self) -> None:
        rejection = _generated_candidate_rejection(
            generated_validation_failure(), attempt=1
        )
        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(
            rejection["rejection_kind"], "generated-source-validation-rejected"
        )
        self.assertEqual(
            rejection["validation_failure_code"],
            "dependencies.bzlmod-authority-unsupported",
        )
        self.assertIn("candidate_tree_identity", rejection)

        source_intent_rejection = _generated_candidate_rejection(
            generated_validation_failure(
                validation_failure_code="dependencies.bzlmod-source-intent-invalid"
            ),
            attempt=1,
        )
        self.assertIsNotNone(source_intent_rejection)
        assert source_intent_rejection is not None
        self.assertEqual(
            source_intent_rejection["validation_failure_code"],
            "dependencies.bzlmod-source-intent-invalid",
        )

        import_mismatch_rejection = _generated_candidate_rejection(
            generated_validation_failure(
                validation_failure_code="dependencies.import-bom-mismatch"
            ),
            attempt=1,
        )
        self.assertIsNotNone(import_mismatch_rejection)
        assert import_mismatch_rejection is not None
        self.assertEqual(
            import_mismatch_rejection["validation_failure_code"],
            "dependencies.import-bom-mismatch",
        )
        self.assertEqual(
            import_mismatch_rejection["validation_failure_summary"],
            "generated source imports a module absent from both generated source "
            "paths and declared dependency metadata",
        )

        for terminal_failure in (
            generated_validation_failure(
                validation_failure_code="dependencies.bzlmod-evidence-missing"
            ),
            generated_validation_failure(admitted_after_failure=True),
        ):
            with self.subTest(message=str(terminal_failure)):
                self.assertIsNone(
                    _generated_candidate_rejection(terminal_failure, attempt=1)
                )

    def test_static_bzlmod_validation_rejection_uses_a_fresh_attempt(self) -> None:
        variant = _ExecutionVariant("rust", (), ("rust",))
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch(
                "tests.conformance.support.sample_runner._run_host_e2e_once",
                side_effect=(generated_validation_failure(), {"passed": True}),
            ) as execute,
        ):
            result = _run_host_e2e(
                Path("sample"),
                Path(temporary),
                "sample",
                oracle_reference_fixture(),
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                variant=variant,
                source_generator=object(),
                allow_host_execution=True,
            )

        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result["generation_candidate_attempt_count"], 2)
        self.assertEqual(
            result["rejected_generation_candidates"][0]["rejection_kind"],
            "generated-source-validation-rejected",
        )

    def test_import_bom_mismatch_uses_bounded_fresh_attempts(self) -> None:
        variant = _ExecutionVariant("python", (), ("python",))

        def mismatch() -> GenerationFailure:
            return generated_validation_failure(
                validation_failure_code="dependencies.import-bom-mismatch"
            )

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch(
                "tests.conformance.support.sample_runner._run_host_e2e_once",
                side_effect=(mismatch(), {"passed": True}),
            ) as execute,
        ):
            result = _run_host_e2e(
                Path("sample"),
                Path(temporary),
                "sample",
                oracle_reference_fixture(),
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                variant=variant,
                source_generator=object(),
                allow_host_execution=True,
            )

        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result["generation_candidate_attempt_count"], 2)
        rejection = result["rejected_generation_candidates"][0]
        self.assertEqual(
            rejection["validation_failure_code"],
            "dependencies.import-bom-mismatch",
        )
        second_feedback = execute.call_args_list[1].kwargs["candidate_feedback"]
        self.assertEqual(second_feedback, [rejection])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e_once",
                    side_effect=(mismatch(), mismatch(), mismatch()),
                ) as execute,
                self.assertRaises(GenerationFailure) as raised,
            ):
                _run_host_e2e(
                    Path("sample"),
                    root,
                    "sample",
                    oracle_reference_fixture(),
                    object(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    variant=variant,
                    source_generator=object(),
                    allow_host_execution=True,
                )
            evidence = json.loads(
                (
                    root / "candidate-attempts" / "python" / "rejections-03.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(execute.call_count, 3)
        self.assertEqual(
            raised.exception.code,
            "generation.generated-candidate-attempts-exhausted",
        )
        self.assertEqual(
            [item["validation_failure_code"] for item in evidence["rejections"]],
            ["dependencies.import-bom-mismatch"] * 3,
        )

    def test_pipeline_validation_rejection_retries_with_exact_findings(self) -> None:
        failure = generated_pipeline_validation_failure()

        rejection = _generated_candidate_rejection(failure, attempt=1)

        self.assertIsNotNone(rejection)
        assert rejection is not None
        self.assertEqual(
            rejection["rejection_kind"], "generated-source-validation-rejected"
        )
        self.assertEqual(
            rejection["validation_finding_codes"],
            ["validation.cpp_companion_header_missing"],
        )
        self.assertEqual(
            rejection["validation_findings"][0]["path"],
            "source/tests/litai_test.cpp",
        )
        self.assertIn("validation_output_identity", rejection)
        self.assertIn("candidate_tree_identity", rejection)

        lifetime_rejection = _generated_candidate_rejection(
            generated_pipeline_validation_failure(
                finding_code="validation.cpp_nonowning_text_member"
            ),
            attempt=1,
        )
        self.assertIsNotNone(lifetime_rejection)
        assert lifetime_rejection is not None
        self.assertEqual(
            lifetime_rejection["validation_finding_codes"],
            ["validation.cpp_nonowning_text_member"],
        )

        for terminal_failure in (
            generated_pipeline_validation_failure(
                finding_code="validation.python_syntax"
            ),
            generated_pipeline_validation_failure(admitted_after_failure=True),
        ):
            with self.subTest(message=str(terminal_failure)):
                self.assertIsNone(
                    _generated_candidate_rejection(terminal_failure, attempt=1)
                )

    def test_pipeline_validation_rejection_uses_a_fresh_attempt(self) -> None:
        variant = _ExecutionVariant("python", (), ("python",))
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch(
                "tests.conformance.support.sample_runner._run_host_e2e_once",
                side_effect=(
                    generated_pipeline_validation_failure(),
                    {"passed": True},
                ),
            ) as execute,
        ):
            result = _run_host_e2e(
                Path("sample"),
                Path(temporary),
                "sample",
                oracle_reference_fixture(),
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                variant=variant,
                source_generator=object(),
                allow_host_execution=True,
            )

        self.assertEqual(execute.call_count, 2)
        self.assertEqual(result["generation_candidate_attempt_count"], 2)
        self.assertEqual(
            result["rejected_generation_candidates"][0]["validation_finding_codes"],
            ["validation.cpp_companion_header_missing"],
        )

    def test_generated_source_build_rejection_retries_but_analysis_failure_stops(
        self,
    ) -> None:
        variant = _ExecutionVariant("cpp", (), ("cpp",))
        for build_failure_code in (
            "builder.cpp_generated_source_rejected",
            "builder.rust_compile_failed",
            "builder.swift_generated_source_rejected",
        ):
            with (
                self.subTest(build_failure_code=build_failure_code),
                tempfile.TemporaryDirectory() as temporary,
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e_once",
                    side_effect=(
                        generated_build_failure(build_failure_code=build_failure_code),
                        {"passed": True},
                    ),
                ) as execute,
            ):
                result = _run_host_e2e(
                    Path("sample"),
                    Path(temporary),
                    "sample",
                    oracle_reference_fixture(),
                    object(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    variant=variant,
                    source_generator=object(),
                    allow_host_execution=True,
                )
            self.assertEqual(execute.call_count, 2)
            self.assertEqual(
                result["rejected_generation_candidates"][0]["rejection_kind"],
                "generated-source-build-rejected",
            )

        framework_failure = generated_build_failure(
            build_failure_code="builder.bazel_build_failed"
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch(
                "tests.conformance.support.sample_runner._run_host_e2e_once",
                side_effect=framework_failure,
            ) as execute,
            self.assertRaises(GenerationFailure),
        ):
            _run_host_e2e(
                Path("sample"),
                Path(temporary),
                "sample",
                oracle_reference_fixture(),
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                variant=variant,
                source_generator=object(),
                allow_host_execution=True,
            )
        self.assertEqual(execute.call_count, 1)

    def test_two_generated_rejections_then_success_records_both(self) -> None:
        variant = _ExecutionVariant("cpp", (), ("cpp",))
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch(
                "tests.conformance.support.sample_runner._run_host_e2e_once",
                side_effect=(
                    generated_candidate_failure(),
                    generated_candidate_failure(),
                    {"passed": True},
                ),
            ) as execute,
        ):
            result = _run_host_e2e(
                Path("sample"),
                Path(temporary),
                "sample",
                oracle_reference_fixture(),
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                variant=variant,
                source_generator=object(),
                allow_host_execution=True,
            )

        self.assertEqual(execute.call_count, 3)
        self.assertEqual(result["generation_candidate_attempt_count"], 3)
        self.assertEqual(
            [item["attempt"] for item in result["rejected_generation_candidates"]],
            [1, 2],
        )

    def test_three_generated_rejections_exhaust_with_versioned_evidence(self) -> None:
        variant = _ExecutionVariant("cpp", (), ("cpp",))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e_once",
                    side_effect=(
                        generated_candidate_failure(),
                        generated_candidate_failure(),
                        generated_candidate_failure(),
                    ),
                ) as execute,
                self.assertRaises(GenerationFailure) as raised,
            ):
                _run_host_e2e(
                    Path("sample"),
                    root,
                    "sample",
                    oracle_reference_fixture(),
                    object(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    variant=variant,
                    source_generator=object(),
                    allow_host_execution=True,
                )

            evidence = json.loads(
                (root / "candidate-attempts" / "cpp" / "rejections-03.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(execute.call_count, 3)
        self.assertEqual(
            raised.exception.code,
            "generation.generated-candidate-attempts-exhausted",
        )
        self.assertEqual(
            evidence["schema"], "literate-ai/generated-candidate-attempts@1"
        )
        self.assertEqual(
            [item["attempt"] for item in evidence["rejections"]], [1, 2, 3]
        )
        self.assertIn(evidence["identity"], str(raised.exception))

    def test_framework_failure_after_rejection_is_not_retried(self) -> None:
        framework_failure = GenerationFailure(
            "generation.lifecycle-step-failed", "dependency resolver failed"
        )
        variant = _ExecutionVariant("javascript", (), ("javascript",))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch(
                    "tests.conformance.support.sample_runner._run_host_e2e_once",
                    side_effect=(generated_candidate_failure(), framework_failure),
                ) as execute,
                self.assertRaises(GenerationFailure) as raised,
            ):
                _run_host_e2e(
                    Path("sample"),
                    root,
                    "sample",
                    oracle_reference_fixture(),
                    object(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    variant=variant,
                    source_generator=object(),
                    allow_host_execution=True,
                )
            retained = json.loads(
                (
                    root / "candidate-attempts" / "javascript" / "rejections-01.json"
                ).read_text(encoding="utf-8")
            )

        self.assertIs(raised.exception, framework_failure)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(len(retained["rejections"]), 1)

    def test_candidate_rejection_evidence_rejects_existing_leaf_nodes(self) -> None:
        envelope = {
            "schema": "literate-ai/generated-candidate-attempts@1",
            "identity": canonical_identity({"fixture": "rejections"}).uri,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = (Path(temporary) / "candidate").resolve()
            root.mkdir()
            outside = Path(temporary) / "outside.json"
            outside.write_text("sentinel", encoding="utf-8")
            target = root / "rejections-01.json"
            try:
                target.symlink_to(outside)
            except OSError:
                self.skipTest("host cannot create the adversarial symlink fixture")
            with self.assertRaises(SampleFailure):
                _write_candidate_rejection_evidence(
                    root, "rejections-01.json", envelope
                )
            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")
            target.unlink()
            target.write_text("preexisting", encoding="utf-8")
            with self.assertRaises(SampleFailure):
                _write_candidate_rejection_evidence(
                    root, "rejections-01.json", envelope
                )
            self.assertEqual(target.read_text(encoding="utf-8"), "preexisting")

    def test_framework_failure_is_not_retried_as_a_generated_candidate(self) -> None:
        failure = GenerationFailure(
            "generation.lifecycle-step-failed",
            "dependency resolver failed",
        )
        failure.__cause__ = SampleFailure("framework evidence failure")
        variant = _ExecutionVariant("javascript", (), ("javascript",))

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch(
                "tests.conformance.support.sample_runner._run_host_e2e_once",
                side_effect=failure,
            ) as execute,
            self.assertRaises(GenerationFailure),
        ):
            _run_host_e2e(
                Path("sample"),
                Path(temporary),
                "sample",
                oracle_reference_fixture(),
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                variant=variant,
                source_generator=object(),
                allow_host_execution=True,
            )

        self.assertEqual(execute.call_count, 1)

    def test_accepted_source_comparison_excludes_workspace_index_sidecars(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source" / "main.py"
            source.parent.mkdir()
            source.write_text("print('portable')\n", encoding="utf-8")
            (root / ".literate-tree.json").write_text("{}", encoding="utf-8")
            (root / ".literate-source-index.json").write_text("{}", encoding="utf-8")
            database = root / ".codegraph" / "codegraph.db"
            database.parent.mkdir()
            database.write_text("derived", encoding="utf-8")

            self.assertEqual(
                _accepted_source_files(root),
                {"source/main.py": "print('portable')\n"},
            )

    def test_derivation_planning_binds_an_omitted_model_without_invoking_cli(self):
        selection = SimpleNamespace(
            name="codex",
            tool_binding_identity=canonical_identity("tool-binding").uri,
        )

        class PlanningFixture:
            def __init__(self):
                self.selection = selection

            def planned_request_identity(
                self, recipe, *, execution_plan, stage_request
            ):
                del recipe, execution_plan, stage_request
                return canonical_identity("request")

        planning = _DerivationPlanningSourceGenerator(PlanningFixture())
        recipe = SimpleNamespace(
            identity=canonical_identity("recipe").uri,
            component_lock_identity=canonical_identity("component-lock"),
            model_for=lambda _provider: None,
        )
        execution_plan = SimpleNamespace(identity=canonical_identity("plan"))

        with self.assertRaises(_DerivationPlanned) as raised:
            planning.generate(
                recipe,
                output_root=Path("unused"),
                execution_plan=execution_plan,
                stage_request={"stage_id": "generate"},
            )

        self.assertEqual(
            raised.exception.cache_key.model_binding.model_selector,
            CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR,
        )

    def test_generated_cases_execute_before_independent_verifier_cases(self):
        recipe_identity = "sha256:" + "1" * 64
        suite = validate_generated_test_suite(
            json.dumps(
                {
                    "schema": "urn:literate-ai:schema:v1:generated-test-suite",
                    "recipe_identity": recipe_identity,
                    "generation_mode": "major-rebuild",
                    "cases": [
                        {
                            "case_id": f"generated-{category}",
                            "category": category,
                            "specification_refs": ["openspec/spec.md"],
                            "arguments": [{"value": index}],
                            "expected_result": {"value": index * 2},
                        }
                        for index, category in enumerate(
                            ("example", "boundary", "invariant"), 1
                        )
                    ],
                }
            ),
            recipe_identity=recipe_identity,
            specification_references=("openspec/spec.md",),
            acceptance_arguments=([{"value": 10}], [{"value": 20}]),
        )
        invocations = [
            {"case_id": "primary", "arguments": [{"value": 10}]},
            {"case_id": "generalization", "arguments": [{"value": 20}]},
        ]
        expected = [
            {"expected_result": {"value": 20}},
            {"expected_result": {"value": 40}},
        ]
        probe = SimpleNamespace(
            case_id="runtime-generalization",
            arguments=[{"value": 30}],
            expected_result={"value": 60},
        )

        cases = _verification_cases(suite, invocations, expected, probe)

        self.assertEqual(
            [item[3] for item in cases],
            [
                "generated-implementation-test",
                "generated-implementation-test",
                "generated-implementation-test",
                "pinned-oracle",
                "pinned-oracle",
                "post-build-runtime-oracle",
            ],
        )
        evidence = [
            {
                "case_id": case_id,
                "verification_source": source,
                "result": result,
            }
            for case_id, _arguments, result, source in cases
        ]
        self.assertEqual(
            _primary_pinned_case(evidence, "primary")["result"],
            {"value": 20},
        )
        probe.case_id = "generated-example"
        with self.assertRaisesRegex(SampleFailure, "disjoint"):
            _verification_cases(suite, invocations, expected, probe)

    def test_passing_report_collapses_to_a_minified_versioned_receipt(self):
        execution = {
            "passed": True,
            "execution_variant": "python",
            "generated_test_suite_identity": "sha256:" + "1" * 64,
            "acceptance_contract_identity": "sha256:" + "2" * 64,
            "acceptance_oracle_identity": "sha256:" + "3" * 64,
            "post_build_runtime_probe_case_id": "runtime-generalization",
            "recipe_identity": "sha256:" + "4" * 64,
            "component_lock_identity": "sha256:" + "c" * 64,
            "accepted_tree_identity": "sha256:" + "5" * 64,
            "build_artifact_identity": "sha256:" + "6" * 64,
            "build_toolchain_identity": "sha256:" + "7" * 64,
            "generation_input_closure": {"identity": "sha256:" + "8" * 64},
            "verifier_input_closure": {"identity": "sha256:" + "9" * 64},
            "request_identity": "sha256:" + "a" * 64,
            "execution_plan_identity": "sha256:" + "f" * 64,
            "coding_cli_tool_binding_identity": "sha256:" + "0" * 64,
            "coding_cli": "codex",
            "coding_model": "fixture-model",
            "source_sbom_identity": "sha256:" + "d" * 64,
            "resolved_sbom_identity": "sha256:" + "e" * 64,
            "workspace_tree_digest": "sha256:" + "f" * 64,
            "execution_artifact_tree_identity": "sha256:" + "b" * 64,
            "execution_cases": [{"case_id": "generated", "result": {"ok": True}}],
            "source_security_classification_digest": "sha256:" + "c" * 64,
            "source_intelligence_status": "current",
            "source_intelligence_reason_code": None,
            "accepted_source_intelligence_status": "current",
            "accepted_source_intelligence_reason_code": None,
        }
        execution["generated_source_intelligence"] = source_intelligence_fixture(
            execution["workspace_tree_digest"]
        ).to_dict()
        execution["accepted_source_intelligence"] = execution[
            "generated_source_intelligence"
        ]
        report = {
            "schema": REPORT_SCHEMA,
            "passed": True,
            "recipe_count": 1,
            "generated_test_count": 3,
            "verifier_test_count": 3,
            "assertion_count": 1,
            "test_count": 7,
            "invocation_count": 6,
            "source_sbom_identity": "sha256:" + "d" * 64,
            "resolved_sbom_identity": "sha256:" + "e" * 64,
            "samples": [
                {
                    "sample_id": "fixture",
                    "assertions": ["fixture-assertion"],
                    "executions": [execution],
                    "passed": True,
                }
            ],
        }
        project = load_project(REPO_ROOT)
        policy = project.definition.test_receipt_policy
        self.assertIsNotNone(policy)
        assert policy is not None
        policy = replace(policy, runner_identity=sample_test_runner_identity())
        project_definition = replace(
            project.definition,
            test_receipt_policy=replace(policy, minimum_test_count=1),
        )

        project_revision_identity = canonical_identity({"fixture": "project-authority"})
        with self.assertRaisesRegex(SampleFailure, "policy is required"):
            project_test_receipt(
                report,
                replace(project_definition, test_receipt_policy=None),
                project_revision_identity,
            )
        with self.assertRaisesRegex(SampleFailure, "runner does not match"):
            project_test_receipt(
                report,
                replace(
                    project_definition,
                    test_receipt_policy=replace(
                        policy,
                        runner_identity=canonical_identity({"fixture": "wrong-runner"}),
                        minimum_test_count=1,
                    ),
                ),
                project_revision_identity,
            )
        with self.assertRaisesRegex(SampleFailure, "fewer tests"):
            project_test_receipt(
                report,
                replace(
                    project_definition,
                    test_receipt_policy=replace(policy, minimum_test_count=8),
                ),
                project_revision_identity,
            )
        lifecycle_request_identity = canonical_identity({"fixture": "request"})
        lifecycle_command_identity = canonical_identity({"fixture": "command"})
        mismatched = json.loads(json.dumps(report))
        mismatched["samples"][0]["executions"][0]["generated_source_intelligence"] = (
            source_intelligence_fixture(execution["accepted_tree_identity"]).to_dict()
        )
        with self.assertRaisesRegex(SampleFailure, "another workspace tree"):
            project_test_receipt(
                mismatched,
                project_definition,
                project_revision_identity,
                lifecycle_request_identity=lifecycle_request_identity,
                lifecycle_command_identity=lifecycle_command_identity,
            )
        receipt = project_test_receipt(
            report,
            project_definition,
            project_revision_identity,
            lifecycle_request_identity=lifecycle_request_identity,
            lifecycle_command_identity=lifecycle_command_identity,
        )

        self.assertEqual(receipt.summary.total, 7)
        self.assertEqual(receipt.outcome, "passed")
        self.assertEqual(receipt.suite.identifier, "sample-host-e2e")
        self.assertEqual(receipt.project_revision_identity, project_revision_identity)
        driver_summary = _receipted_driver_summary(
            {**report, "operational_metrics": {"build_seconds": 0.125}}, receipt
        )
        self.assertNotIn("samples", driver_summary)
        self.assertLess(len(json.dumps(driver_summary).encode("utf-8")), 1024)
        evidence = {item.kind: item.identity for item in receipt.evidence}
        # Exact kind set: any new evidence kind must update this contract deliberately.
        self.assertEqual(
            set(evidence),
            {
                "acceptance-result",
                "build-result",
                "cache-directory-custody",
                "generation-provenance",
                "lifecycle-command",
                "lifecycle-plan",
                "lifecycle-request",
                "observation-result",
                "resolved-sbom",
                "security-scan-report",
                "source-cache-decision",
                "source-cache-lifecycle",
                "source-intelligence",
                "source-sbom",
                "test-report",
                "test-runner",
                "workspace-admission",
            },
        )
        self.assertEqual(len(receipt.evidence), 17)
        self.assertEqual(
            evidence["test-runner"],
            sample_test_runner_identity(),
        )
        self.assertEqual(evidence["lifecycle-request"], lifecycle_request_identity)
        self.assertEqual(evidence["lifecycle-command"], lifecycle_command_identity)
        inactive_report = json.loads(json.dumps(report))
        inactive_execution = inactive_report["samples"][0]["executions"][0]
        inactive_execution.update(
            {
                "generated_source_intelligence": None,
                "source_intelligence_status": "off",
                "source_intelligence_reason_code": None,
                "accepted_source_intelligence": None,
                "accepted_source_intelligence_status": "off",
                "accepted_source_intelligence_reason_code": None,
            }
        )
        inactive_receipt = project_test_receipt(
            inactive_report,
            project_definition,
            project_revision_identity,
            lifecycle_request_identity=lifecycle_request_identity,
            lifecycle_command_identity=lifecycle_command_identity,
        )
        self.assertEqual(inactive_receipt.outcome, "passed")
        self.assertEqual(
            {item.kind for item in inactive_receipt.evidence}
            - {item.kind for item in receipt.evidence},
            set(),
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "receipt.json"
            provisional = ProjectTestReceiptProvisional(
                lifecycle_request_identity=lifecycle_request_identity,
                lifecycle_command_identity=lifecycle_command_identity,
                source_cache_control_identity=canonical_identity(
                    {"fixture": "source-cache-control"}
                ),
                component_lock_identities=(
                    canonical_identity({"fixture": "component-lock"}),
                ),
                receipt_identity=receipt.identity,
                receipt=receipt,
            )
            write_project_test_receipt_candidate(
                target, provisional, repository_root=REPO_ROOT
            )
            content = target.read_bytes()
        self.assertTrue(content.endswith(b"\n"))
        self.assertNotIn(b"\n ", content)
        self.assertEqual(len(content.splitlines()), 1)

        report["passed"] = False
        with self.assertRaisesRegex(SampleFailure, "passing"):
            project_test_receipt(report, project_definition, project_revision_identity)

    def test_retry_receipt_preserves_the_outer_planned_derivation_key(self):
        recipe = canonical_identity({"fixture": "retry-recipe"})
        plan = canonical_identity({"fixture": "retry-plan"})
        tool = canonical_identity({"fixture": "retry-tool"})
        planned_request = canonical_identity({"fixture": "planned-request"})
        accepted_attempt_request = canonical_identity(
            {"fixture": "accepted-attempt-request"}
        )
        planned_key = SourceDerivationCacheKey(
            recipe_identity=recipe,
            execution_plan_identity=plan,
            coding_cli_tool_binding_identity=tool,
            model_binding=SourceCacheModelBinding("codex", "fixture-model"),
            request_identity=planned_request,
        )
        lock = canonical_identity({"fixture": "retry-lock"})
        project_revision = canonical_identity({"fixture": "retry-project"})
        lifecycle_request = canonical_identity({"fixture": "retry-lifecycle"})
        lifecycle_plan = canonical_identity(
            {
                "schema": REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA,
                "component_lock_identities": [lock.uri],
                "cache_key_identities": [planned_key.identity.uri],
            }
        )
        manifest = RebuildSourceCacheDerivationManifest(
            planning_request_identity=canonical_identity(
                {"fixture": "retry-planning-request"}
            ),
            project_revision_identity=project_revision,
            lifecycle_driver_identity=canonical_identity({"fixture": "retry-driver"}),
            lifecycle_plan_identity=lifecycle_plan,
            component_lock_identities=(lock,),
            cache_keys=(planned_key,),
        )
        control = RebuildSourceCacheControl(
            lifecycle_request_identity=lifecycle_request,
            project_revision_identity=project_revision,
            configuration=None,
            derivation_manifest=manifest,
            component_lock_identities=(lock,),
        )
        execution = {
            "recipe_identity": recipe.uri,
            "execution_plan_identity": plan.uri,
            "coding_cli_tool_binding_identity": tool.uri,
            "coding_cli": "codex",
            "coding_model": "fixture-model",
            "request_identity": accepted_attempt_request.uri,
            "generation_candidate_attempt_count": 2,
            "rejected_generation_candidates": [
                {
                    "schema": "literate-ai/generated-candidate-rejection@1",
                    "attempt": 1,
                }
            ],
        }

        self.assertIs(
            _outer_bound_execution_cache_key(execution, 0, control),
            planned_key,
        )

        other_plan_key = SourceDerivationCacheKey(
            recipe_identity=recipe,
            execution_plan_identity=canonical_identity({"fixture": "other-plan"}),
            coding_cli_tool_binding_identity=tool,
            model_binding=SourceCacheModelBinding("codex", "fixture-model"),
            request_identity=canonical_identity({"fixture": "other-request"}),
        )
        multi_plan_manifest = replace(
            manifest,
            lifecycle_plan_identity=canonical_identity(
                {
                    "schema": REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA,
                    "component_lock_identities": [lock.uri],
                    "cache_key_identities": sorted(
                        (planned_key.identity.uri, other_plan_key.identity.uri)
                    ),
                }
            ),
            cache_keys=tuple(
                sorted((planned_key, other_plan_key), key=lambda key: key.identity.uri)
            ),
        )
        multi_plan_control = replace(control, derivation_manifest=multi_plan_manifest)
        self.assertIs(
            _outer_bound_execution_cache_key(execution, 0, multi_plan_control),
            planned_key,
        )

        semantic_key = SourceDerivationCacheKey(
            recipe_identity=recipe,
            execution_plan_identity=plan,
            coding_cli_tool_binding_identity=tool,
            model_binding=SourceCacheModelBinding("codex", "fixture-model"),
            request_identity=planned_request,
            source_semantics_identity=canonical_identity(
                {"fixture": "standard-source-semantics"}
            ),
        )
        semantic_manifest = replace(
            manifest,
            lifecycle_plan_identity=canonical_identity(
                {
                    "schema": REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA,
                    "component_lock_identities": [lock.uri],
                    "cache_key_identities": sorted(
                        (planned_key.identity.uri, semantic_key.identity.uri)
                    ),
                }
            ),
            cache_keys=tuple(
                sorted((planned_key, semantic_key), key=lambda key: key.identity.uri)
            ),
        )
        semantic_control = replace(control, derivation_manifest=semantic_manifest)
        standard_execution = {**execution, "_receipt_cache_key": semantic_key}
        self.assertIs(
            _outer_bound_execution_cache_key(
                standard_execution,
                0,
                semantic_control,
            ),
            semantic_key,
        )

        missing_retry = dict(execution)
        missing_retry["rejected_generation_candidates"] = []
        with self.assertRaisesRegex(SampleFailure, "exact candidate retry chain"):
            _outer_bound_execution_cache_key(missing_retry, 0, control)

        changed_plan = dict(execution)
        changed_plan["execution_plan_identity"] = canonical_identity(
            {"fixture": "changed-plan"}
        ).uri
        with self.assertRaisesRegex(SampleFailure, "stable outer"):
            _outer_bound_execution_cache_key(changed_plan, 0, control)

        accepted_tree = canonical_identity({"fixture": "retry-tree"})
        generated_suite = canonical_identity({"fixture": "retry-suite"})
        accepted_execution = {
            **execution,
            "passed": True,
            "execution_variant": "python",
            "generated_test_suite_identity": generated_suite.uri,
            "acceptance_contract_identity": canonical_identity(
                {"fixture": "retry-acceptance-contract"}
            ).uri,
            "acceptance_oracle_identity": canonical_identity(
                {"fixture": "retry-acceptance-oracle"}
            ).uri,
            "post_build_runtime_probe_case_id": "retry-runtime-probe",
            "component_lock_identity": lock.uri,
            "accepted_tree_identity": accepted_tree.uri,
            "build_artifact_identity": canonical_identity(
                {"fixture": "retry-build"}
            ).uri,
            "build_toolchain_identity": canonical_identity(
                {"fixture": "retry-toolchain"}
            ).uri,
            "generation_input_closure": {
                "identity": canonical_identity(
                    {"fixture": "retry-generation-inputs"}
                ).uri
            },
            "verifier_input_closure": {
                "identity": canonical_identity({"fixture": "retry-verifier-inputs"}).uri
            },
            "source_sbom_identity": canonical_identity(
                {"fixture": "retry-source-sbom"}
            ).uri,
            "resolved_sbom_identity": canonical_identity(
                {"fixture": "retry-resolved-sbom"}
            ).uri,
            "workspace_tree_digest": accepted_tree.uri,
            "execution_artifact_tree_identity": canonical_identity(
                {"fixture": "retry-artifact-tree"}
            ).uri,
            "execution_cases": [{"case_id": "retry-generated", "result": {}}],
            "source_security_classification_digest": canonical_identity(
                {"fixture": "retry-classification"}
            ).uri,
            "generated_source_intelligence": None,
            "source_intelligence_status": "off",
            "source_intelligence_reason_code": None,
            "accepted_source_intelligence": None,
            "accepted_source_intelligence_status": "off",
            "accepted_source_intelligence_reason_code": None,
        }
        report = {
            "schema": REPORT_SCHEMA,
            "passed": True,
            "test_count": 1,
            "samples": [
                {
                    "sample_id": "retry-fixture",
                    "passed": True,
                    "assertions": ["retry accepted"],
                    "executions": [accepted_execution],
                }
            ],
        }
        project = load_project(REPO_ROOT)
        policy = project.definition.test_receipt_policy
        self.assertIsNotNone(policy)
        assert policy is not None
        definition = replace(
            project.definition,
            test_receipt_policy=replace(
                policy,
                runner_identity=sample_test_runner_identity(),
                minimum_test_count=1,
            ),
        )
        expected_provenance = canonical_identity(
            {
                "schema": "literate-ai/sample-provenance-member@1",
                "recipe": recipe.uri,
                "request": accepted_attempt_request.uri,
                "tree": accepted_tree.uri,
                "suite": generated_suite.uri,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            lifecycle_path = Path(directory).resolve() / "source-cache-lifecycle.json"
            project_test_receipt(
                report,
                definition,
                project_revision,
                lifecycle_request_identity=lifecycle_request,
                lifecycle_command_identity=canonical_identity(
                    {"fixture": "retry-command"}
                ),
                source_cache_control=control,
                source_cache_decision=RebuildSourceCacheDecision.fresh(control),
                source_cache_lifecycle_path=lifecycle_path,
            )
            lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))

        self.assertEqual(len(lifecycle["members"]), 1)
        self.assertEqual(
            SourceDerivationCacheKey.from_dict(
                lifecycle["members"][0]["cache_key"]
            ).identity,
            planned_key.identity,
        )
        self.assertEqual(
            ContentIdentity.from_dict(
                lifecycle["members"][0]["provenance_evidence_identity"]
            ),
            expected_provenance,
        )

    def test_receipted_project_revision_binds_the_outer_planned_lock_set(self):
        base = canonical_identity({"fixture": "validated-project"})
        first_lock = canonical_identity({"fixture": "first-lock"})
        second_lock = canonical_identity({"fixture": "second-lock"})
        with mock.patch(
            "tests.conformance.support.sample_runner._validated_project_revision",
            return_value=base,
        ):
            first = _receipted_project_revision(REPO_ROOT, (first_lock,))
            second = _receipted_project_revision(REPO_ROOT, (second_lock,))

        self.assertNotEqual(first, base)
        self.assertNotEqual(first, second)

    def test_standard_receipt_projection_expands_each_typed_node(self):
        recipe = canonical_identity({"fixture": "standard-recipe"})
        cache_key = SourceDerivationCacheKey(
            recipe_identity=recipe,
            execution_plan_identity=canonical_identity({"fixture": "plan"}),
            coding_cli_tool_binding_identity=canonical_identity({"fixture": "tool"}),
            model_binding=SourceCacheModelBinding("codex", "fixture-model"),
            request_identity=canonical_identity({"fixture": "request"}),
        )
        identity = canonical_identity({"fixture": "evidence"}).uri
        execution = {
            "schema": STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA,
            "component_lock_identity": canonical_identity({"fixture": "lock"}).uri,
            "variant_id": "python",
            "nodes": [
                {
                    "recipe_identity": recipe.uri,
                    "source_tree_identity": identity,
                    "source_index_identity": identity,
                    "build_evidence_identity": identity,
                    "generated_test_evidence_identity": identity,
                    "acceptance_evidence_identity": identity,
                    "workspace_admission_identity": identity,
                    "provenance_evidence_identity": identity,
                    "source_sbom_identity": identity,
                    "resolved_sbom_identity": identity,
                }
            ],
        }
        control = SimpleNamespace(
            derivation_manifest=SimpleNamespace(cache_keys=(cache_key,))
        )

        members = _standard_receipt_executions(execution, source_cache_control=control)

        self.assertEqual(len(members), 1)
        self.assertIs(members[0]["_receipt_cache_key"], cache_key)
        self.assertEqual(members[0]["recipe_identity"], recipe.uri)
        with self.assertRaisesRegex(SampleFailure, "uniquely bind"):
            _standard_receipt_executions(
                execution,
                source_cache_control=SimpleNamespace(
                    derivation_manifest=SimpleNamespace(
                        cache_keys=(cache_key, cache_key)
                    )
                ),
            )

    def test_standard_report_produces_a_complete_outer_bound_receipt(self):
        project = load_project(REPO_ROOT)
        policy = project.definition.test_receipt_policy
        self.assertIsNotNone(policy)
        assert policy is not None
        definition = replace(
            project.definition,
            test_receipt_policy=replace(
                policy,
                runner_identity=sample_test_runner_identity(),
                minimum_test_count=1,
            ),
        )
        lock = canonical_identity({"fixture": "standard-lock"})
        project_revision = canonical_identity({"fixture": "standard-project"})
        lifecycle_request = canonical_identity({"fixture": "standard-request"})
        lifecycle_command = canonical_identity({"fixture": "standard-command"})
        recipe = canonical_identity({"fixture": "standard-recipe"})
        cache_key = SourceDerivationCacheKey(
            recipe_identity=recipe,
            execution_plan_identity=canonical_identity({"fixture": "plan"}),
            coding_cli_tool_binding_identity=canonical_identity({"fixture": "tool"}),
            model_binding=SourceCacheModelBinding("codex", "fixture-model"),
            request_identity=canonical_identity({"fixture": "generation-request"}),
        )
        lifecycle_plan = canonical_identity(
            {
                "schema": REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA,
                "component_lock_identities": [lock.uri],
                "cache_key_identities": [cache_key.identity.uri],
            }
        )
        manifest = RebuildSourceCacheDerivationManifest(
            planning_request_identity=canonical_identity(
                {"fixture": "planning-request"}
            ),
            project_revision_identity=project_revision,
            lifecycle_driver_identity=canonical_identity({"fixture": "driver"}),
            lifecycle_plan_identity=lifecycle_plan,
            component_lock_identities=(lock,),
            cache_keys=(cache_key,),
        )
        control = RebuildSourceCacheControl(
            lifecycle_request_identity=lifecycle_request,
            project_revision_identity=project_revision,
            configuration=None,
            derivation_manifest=manifest,
            component_lock_identities=(lock,),
        )
        decision = RebuildSourceCacheDecision.fresh(control)
        evidence = {
            name: canonical_identity({"fixture": name}).uri
            for name in (
                "source-tree",
                "source-index",
                "build",
                "test",
                "acceptance",
                "workspace",
                "provenance",
                "source-sbom",
                "resolved-sbom",
            )
        }
        report = {
            "schema": REPORT_SCHEMA,
            "passed": True,
            "test_count": 1,
            "samples": [
                {
                    "sample_id": "standard-fixture",
                    "passed": True,
                    "assertions": ["accepted"],
                    "executions": [
                        {
                            "schema": STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA,
                            "passed": True,
                            "component_lock_identity": lock.uri,
                            "variant_id": "python",
                            "nodes": [
                                {
                                    "recipe_identity": recipe.uri,
                                    "source_tree_identity": evidence["source-tree"],
                                    "source_index_identity": evidence["source-index"],
                                    "build_evidence_identity": evidence["build"],
                                    "generated_test_evidence_identity": evidence[
                                        "test"
                                    ],
                                    "acceptance_evidence_identity": evidence[
                                        "acceptance"
                                    ],
                                    "workspace_admission_identity": evidence[
                                        "workspace"
                                    ],
                                    "provenance_evidence_identity": evidence[
                                        "provenance"
                                    ],
                                    "source_sbom_identity": evidence["source-sbom"],
                                    "resolved_sbom_identity": evidence["resolved-sbom"],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            lifecycle_path = Path(directory).resolve() / "source-cache-lifecycle.json"
            receipt = project_test_receipt(
                report,
                definition,
                project_revision,
                lifecycle_request_identity=lifecycle_request,
                lifecycle_command_identity=lifecycle_command,
                source_cache_control=control,
                source_cache_decision=decision,
                source_cache_lifecycle_path=lifecycle_path,
            )

            self.assertTrue(lifecycle_path.is_file())
        self.assertEqual(receipt.outcome, "passed")
        self.assertEqual(receipt.summary.total, 1)

    def test_durable_portfolio_produces_two_root_outer_bound_receipt(self):
        project = load_project(REPO_ROOT)
        policy = project.definition.test_receipt_policy
        self.assertIsNotNone(policy)
        assert policy is not None
        definition = replace(
            project.definition,
            test_receipt_policy=replace(
                policy,
                runner_identity=sample_test_runner_identity(),
                minimum_test_count=1,
            ),
        )
        project_revision = canonical_identity({"fixture": "durable-project"})
        lifecycle_request = canonical_identity({"fixture": "durable-request"})
        lifecycle_command = canonical_identity({"fixture": "durable-command"})
        locks = tuple(
            sorted(
                (
                    canonical_identity({"fixture": "frontend-lock"}),
                    canonical_identity({"fixture": "collector-lock"}),
                ),
                key=lambda item: item.uri,
            )
        )
        recipes = (
            canonical_identity({"fixture": "frontend-recipe"}),
            canonical_identity({"fixture": "collector-recipe"}),
        )
        cache_keys = tuple(
            sorted(
                (
                    SourceDerivationCacheKey(
                        recipe_identity=recipe,
                        execution_plan_identity=canonical_identity(
                            {"fixture": f"plan-{index}"}
                        ),
                        coding_cli_tool_binding_identity=canonical_identity(
                            {"fixture": "tool"}
                        ),
                        model_binding=SourceCacheModelBinding("codex", "fixture-model"),
                        request_identity=canonical_identity(
                            {"fixture": f"request-{index}"}
                        ),
                    )
                    for index, recipe in enumerate(recipes)
                ),
                key=lambda item: item.identity.uri,
            )
        )
        lifecycle_plan = canonical_identity(
            {
                "schema": REBUILD_SOURCE_CACHE_DERIVATION_PLAN_SCHEMA,
                "component_lock_identities": [item.uri for item in locks],
                "cache_key_identities": [item.identity.uri for item in cache_keys],
            }
        )
        manifest = RebuildSourceCacheDerivationManifest(
            planning_request_identity=canonical_identity(
                {"fixture": "durable-planning-request"}
            ),
            project_revision_identity=project_revision,
            lifecycle_driver_identity=canonical_identity({"fixture": "driver"}),
            lifecycle_plan_identity=lifecycle_plan,
            component_lock_identities=locks,
            cache_keys=cache_keys,
        )
        control = RebuildSourceCacheControl(
            lifecycle_request_identity=lifecycle_request,
            project_revision_identity=project_revision,
            configuration=None,
            derivation_manifest=manifest,
            component_lock_identities=locks,
        )
        evidence = {
            name: canonical_identity({"fixture": f"durable-{name}"}).uri
            for name in (
                "source-tree",
                "source-index",
                "build",
                "test",
                "acceptance",
                "workspace",
                "provenance",
                "source-sbom",
                "resolved-sbom",
            )
        }

        def root_report(index: int, sample_id: str) -> dict[str, object]:
            return {
                "schema": STANDARD_SAMPLE_EXECUTION_REPORT_SCHEMA,
                "sample_id": sample_id,
                "passed": True,
                "component_lock_identity": locks[index].uri,
                "variant_id": "host",
                "nodes": [
                    {
                        "recipe_identity": recipes[index].uri,
                        "source_tree_identity": evidence["source-tree"],
                        "source_index_identity": evidence["source-index"],
                        "build_evidence_identity": evidence["build"],
                        "generated_test_evidence_identity": evidence["test"],
                        "acceptance_evidence_identity": evidence["acceptance"],
                        "workspace_admission_identity": evidence["workspace"],
                        "provenance_evidence_identity": evidence["provenance"],
                        "source_sbom_identity": evidence["source-sbom"],
                        "resolved_sbom_identity": evidence["resolved-sbom"],
                    }
                ],
            }

        portfolio = {
            "schema": "literate-ai/durable-split-service-execution@1",
            "proof_status": "passed",
            "component_lock_identities": [item.uri for item in reversed(locks)],
            "frontend_root": root_report(0, "durable-split-frontend"),
            "collector_root": root_report(1, "durable-split-collector"),
        }
        report = {
            "schema": REPORT_SCHEMA,
            "passed": True,
            "test_count": 2,
            "samples": [
                {
                    "sample_id": "durable-split-service",
                    "passed": True,
                    "assertions": ["cross-process-proof"],
                    "executions": [portfolio],
                }
            ],
        }
        self.assertEqual(
            _report_component_lock_identities(report),
            locks,
        )
        with tempfile.TemporaryDirectory() as directory:
            lifecycle_path = Path(directory).resolve() / "source-cache-lifecycle.json"
            receipt = project_test_receipt(
                report,
                definition,
                project_revision,
                lifecycle_request_identity=lifecycle_request,
                lifecycle_command_identity=lifecycle_command,
                source_cache_control=control,
                source_cache_decision=RebuildSourceCacheDecision.fresh(control),
                source_cache_lifecycle_path=lifecycle_path,
            )
            lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))

        self.assertEqual(receipt.outcome, "passed")
        self.assertEqual(receipt.summary.total, 2)
        self.assertEqual(len(lifecycle["members"]), 2)

        portfolio["component_lock_identities"] = [locks[0].uri]
        with self.assertRaisesRegex(SampleFailure, "inconsistent Component locks"):
            _standard_execution_reports(portfolio)
        portfolio["component_lock_identities"] = [item.uri for item in locks]
        portfolio["proof_status"] = "failed"
        with self.assertRaisesRegex(SampleFailure, "passing proof"):
            _standard_execution_reports(portfolio)

    def test_verifier_authority_is_separate_and_temporally_closed(self):
        for authority_name in ("sample-manifest", "acceptance-oracle"):
            with self.subTest(authority=authority_name):
                with tempfile.TemporaryDirectory() as directory:
                    sample = copy_sample_project(Path(directory), "hello-component")
                    metadata, _definition, _loaded, closures = _load_sample(sample)
                    generation_evidence = json.dumps(
                        closures.generation.to_dict(), sort_keys=True
                    )
                    verifier_evidence = json.dumps(
                        closures.verifier.to_dict(), sort_keys=True
                    )
                    self.assertNotIn("sample-manifest", generation_evidence)
                    self.assertNotIn("acceptance-oracle", generation_evidence)
                    self.assertIn("sample-manifest", verifier_evidence)
                    self.assertIn("acceptance-oracle", verifier_evidence)
                    reference = ContentReference.from_dict(
                        metadata["acceptance_oracle"]
                    )
                    authority = (
                        sample_harness_manifest(sample)
                        if authority_name == "sample-manifest"
                        else sample_harness_oracle(sample, reference)
                    )
                    authority.write_bytes(authority.read_bytes() + b"\n")

                    closures.generation.require_unchanged()
                    with self.assertRaises(PinnedInputClosureError):
                        closures.verifier.require_unchanged()

    def test_generation_provenance_is_exact_and_explicitly_non_hermetic(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex"
            executable.write_bytes(b"#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            executable_identity = (
                "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
            )
            selection = CodingCliSelection(
                "codex", str(executable.resolve()), executable_identity
            )
            command = (selection.executable, "exec")
            recipe_identity = canonical_identity("sample-recipe").uri
            generation = CodingCliGeneration(
                files={"source/main.py": "print('ok')\n"},
                coding_cli="codex",
                executable=selection.executable,
                model="sample-model",
                recipe_identity=recipe_identity,
                command=command,
                request_identity=canonical_identity("request").uri,
                execution_plan_identity=canonical_identity("execution-plan").uri,
                requested_model_stages=("generate",),
                requested_route_decision_digests=(canonical_identity("route").uri,),
                executable_identity=selection.executable_identity,
                command_identity=canonical_identity(command).uri,
                coding_cli_selection_identity=selection.identity,
                coding_cli_tool_binding_identity=selection.tool_binding_identity,
                isolation_profile=selection.isolation.profile,
                hermetic=selection.isolation.hermetic,
                environment_keys=("PATH",),
                generated_test_suite_identity=canonical_identity(
                    "generated-test-suite"
                ).uri,
                source_intelligence_status="current",
                source_intelligence=source_intelligence_fixture(),
                source_sbom=source_sbom_binding_fixture(),
            )
            model = SimpleNamespace(
                generation=generation,
                source_generator=SimpleNamespace(selection=selection),
                recipe=SimpleNamespace(identity=recipe_identity),
            )

            provenance = _coding_cli_generation_provenance(model)
            default_model_provenance = _coding_cli_generation_provenance(
                SimpleNamespace(
                    generation=replace(generation, model=None),
                    source_generator=model.source_generator,
                    recipe=model.recipe,
                )
            )

        self.assertEqual(provenance["coding_cli_executable"], selection.executable)
        self.assertEqual(
            default_model_provenance["coding_model"],
            CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR,
        )
        self.assertEqual(
            provenance["coding_cli_executable_identity"], executable_identity
        )
        self.assertEqual(
            provenance["coding_cli_selection_identity"], selection.identity
        )
        self.assertEqual(
            provenance["coding_cli_tool_binding_identity"],
            selection.tool_binding_identity,
        )
        self.assertEqual(provenance["request_identity"], generation.request_identity)
        isolation = provenance["coding_cli_isolation"]
        self.assertIsInstance(isolation, dict)
        self.assertFalse(isolation["hermetic"])
        self.assertTrue(isolation["limitations"])
        self.assertTrue(isolation["identity"].startswith("sha256:"))

    def test_generation_provenance_rejects_a_fabricated_isolation_claim(self):
        selection = SimpleNamespace(
            name="codex",
            executable="/tools/codex",
            executable_identity="sha256:" + "1" * 64,
            identity="sha256:" + "2" * 64,
            tool_binding_identity="sha256:" + "6" * 64,
            isolation=SimpleNamespace(
                profile="provider-policy",
                hermetic=False,
            ),
        )
        command = (selection.executable, "exec")
        generation = CodingCliGeneration(
            files={},
            coding_cli="codex",
            executable=selection.executable,
            model=None,
            recipe_identity="recipe",
            command=command,
            request_identity="sha256:" + "3" * 64,
            executable_identity=selection.executable_identity,
            command_identity=canonical_identity(command).uri,
            coding_cli_selection_identity=selection.identity,
            coding_cli_tool_binding_identity=selection.tool_binding_identity,
            isolation_profile=selection.isolation.profile,
            hermetic=True,
            generated_test_suite_identity="sha256:" + "4" * 64,
            source_intelligence_status="current",
            source_intelligence=source_intelligence_fixture(),
            source_sbom=source_sbom_binding_fixture(),
        )
        model = SimpleNamespace(
            generation=generation,
            source_generator=SimpleNamespace(selection=selection),
            recipe=SimpleNamespace(identity="recipe"),
        )

        with self.assertRaisesRegex(SampleFailure, "exact selected invocation"):
            _coding_cli_generation_provenance(model)

    def test_generation_provenance_records_disabled_source_intelligence_status(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex"
            executable.write_bytes(b"#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            selection = CodingCliSelection(
                "codex",
                str(executable.resolve()),
                "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
            )
            generation = CodingCliGeneration(
                files={"source/main.py": "print('ok')\n"},
                coding_cli="codex",
                executable=selection.executable,
                model="sample-model",
                recipe_identity=canonical_identity("sample-recipe").uri,
                command=(selection.executable, "exec"),
                request_identity=canonical_identity("request").uri,
                execution_plan_identity=canonical_identity("execution-plan").uri,
                requested_model_stages=("generate",),
                requested_route_decision_digests=(canonical_identity("route").uri,),
                executable_identity=selection.executable_identity,
                command_identity=canonical_identity((selection.executable, "exec")).uri,
                coding_cli_selection_identity=selection.identity,
                isolation_profile=selection.isolation.profile,
                hermetic=selection.isolation.hermetic,
                environment_keys=("PATH",),
                generated_test_suite_identity=canonical_identity(
                    "generated-test-suite"
                ).uri,
                coding_cli_tool_binding_identity=selection.tool_binding_identity,
                source_intelligence_status="unavailable",
                source_intelligence_reason_code="provider-not-configured",
                source_sbom=source_sbom_binding_fixture(),
            )
            model = SimpleNamespace(
                generation=generation,
                source_generator=SimpleNamespace(selection=selection),
                recipe=SimpleNamespace(
                    identity=canonical_identity("sample-recipe").uri
                ),
            )

            for status, reason_code in (
                ("off", None),
                ("unavailable", "provider-not-configured"),
            ):
                with self.subTest(status=status):
                    candidate = replace(
                        generation,
                        source_intelligence_status=status,
                        source_intelligence_reason_code=reason_code,
                    )
                    provenance = _coding_cli_generation_provenance(
                        SimpleNamespace(
                            generation=candidate,
                            source_generator=model.source_generator,
                            recipe=model.recipe,
                        )
                    )
                    self.assertIsNone(provenance["generated_source_intelligence"])
                    self.assertEqual(provenance["source_intelligence_status"], status)
                    self.assertEqual(
                        provenance["source_intelligence_reason_code"],
                        reason_code,
                    )

    def test_generation_provenance_rejects_unknown_source_intelligence_status(self):
        generation = CodingCliGeneration(
            files={},
            coding_cli="codex",
            executable="/tools/codex",
            model=None,
            recipe_identity="recipe",
            command=("/tools/codex", "exec"),
            request_identity="sha256:" + "1" * 64,
            executable_identity="sha256:" + "2" * 64,
            command_identity=canonical_identity(("/tools/codex", "exec")).uri,
            coding_cli_selection_identity="sha256:" + "3" * 64,
            isolation_profile="provider-policy",
            source_intelligence_status="current",
            generated_test_suite_identity="sha256:" + "4" * 64,
            source_intelligence=source_intelligence_fixture(),
            source_sbom=source_sbom_binding_fixture(),
        )
        object.__setattr__(generation, "source_intelligence_status", "unknown")
        model = SimpleNamespace(
            generation=generation,
            source_generator=SimpleNamespace(
                selection=SimpleNamespace(
                    name="codex",
                    executable="/tools/codex",
                    executable_identity=generation.executable_identity,
                    identity=generation.coding_cli_selection_identity,
                    tool_binding_identity=generation.coding_cli_tool_binding_identity,
                    isolation=SimpleNamespace(
                        profile="provider-policy",
                        hermetic=False,
                        to_dict=lambda: {
                            "profile": "provider-policy",
                            "hermetic": False,
                            "limitations": [],
                        },
                    ),
                    require_unchanged=lambda: None,
                )
            ),
            recipe=SimpleNamespace(identity="recipe"),
        )

        with self.assertRaisesRegex(
            SampleFailure, "observed source-intelligence status"
        ):
            _coding_cli_generation_provenance(model)

    def test_full_stack_metadata_declares_host_observed_two_stage_protocol(self):
        sample = SAMPLES / "full-stack-rust-js"
        metadata = json.loads(
            sample_harness_manifest(sample).read_text(encoding="utf-8")
        )
        specification = (sample / "component.md").read_text(encoding="utf-8")
        self.assertEqual(
            metadata["expected_assertions"],
            [
                "both-language-flavors-are-pinned",
                "rust-backend-performs-risk-analysis",
                "trusted-runner-invokes-backend-then-frontend",
                "frontend-receives-canonical-backend-json",
                "full-stack-result-is-deterministic",
            ],
        )
        self.assertIn(
            "host runner SHALL invoke the exact compiled Rust backend", specification
        )
        self.assertIn("passing the canonical backend JSON", specification)
        self.assertNotIn("process.argv[3]", specification)
        self.assertNotIn("Frontend invokes compiled backend", specification)

    def test_full_stack_verifier_accepts_canonical_language_coordinates(self):
        self.assertEqual(
            _full_stack_rust_js(SAMPLES / "full-stack-rust-js", SAMPLES, ()),
            (
                "both-language-flavors-are-pinned",
                "rust-backend-performs-risk-analysis",
                "trusted-runner-invokes-backend-then-frontend",
                "frontend-receives-canonical-backend-json",
                "full-stack-result-is-deterministic",
            ),
        )

    def test_multi_repository_verifier_accepts_the_bounded_default_matrix(self):
        execution = {"implementation_language": "python"}

        self.assertEqual(_executions_by_language((execution,)), {"python": execution})
        with self.assertRaisesRegex(SampleFailure, "incomplete"):
            _executions_by_language(())
        with self.assertRaisesRegex(SampleFailure, "unique languages"):
            _executions_by_language((execution, execution))

    def test_generated_artifacts_cannot_be_retained_in_the_checkout(self):
        forbidden = SAMPLES / "generated-runtime-must-not-exist"
        with self.assertRaisesRegex(SampleFailure, "outside the repository"):
            main(["--runtime-root", str(forbidden)])
        with self.assertRaisesRegex(SampleFailure, "outside the repository"):
            run_all(SAMPLES, forbidden, allow_host_execution=True)
        self.assertFalse(forbidden.exists())

    def test_every_sample_is_a_strict_spec_driven_application(self):
        samples = discover(SAMPLES)
        self.assertTrue(
            samples, "repository must declare at least one sample component"
        )
        self.assertEqual(len(samples), len({sample.name for sample in samples}))
        for sample in samples:
            with self.subTest(sample=sample.name):
                metadata, definition, loaded, _closures = _load_sample(sample)
                self.assertIn(metadata["scenario"], HANDLERS)
                authoring = parse_component_markdown(
                    sample / "component.md",
                    (sample / "component.md").read_text(encoding="utf-8"),
                    project_root=REPO_ROOT,
                )
                self.assertTrue(definition.sample)
                self.assertEqual(definition.coordinate.name, sample.name)
                self.assertEqual(
                    authoring.inheritable,
                    sample.name == "hello-component",
                    "only the hello sample may inherit into derived projects",
                )
                self.assertEqual(definition.acceptance_contracts, ())
                provider = definition.specification_provider
                if provider == "literate-markdown":
                    self.assertEqual(definition.specification_roots[0], "component.md")
                elif provider in {"dmn", "scxml"}:
                    self.assertTrue(definition.specification_roots)
                    self.assertNotIn("component.md", definition.specification_roots)
                else:
                    self.fail(f"unexpected specification provider: {provider}")
                for spec_root in definition.specification_roots:
                    self.assertTrue((sample / spec_root).is_file(), spec_root)
                self.assertTrue(
                    {
                        "sample.portable-app",
                        "sample.persistent-service-app",
                        "sample.web-dashboard-app",
                    }
                    & {item.name for item in definition.provides}
                )
                os_slots = [
                    item
                    for item in definition.flavor_slots
                    if item.axis is FlavorAxis.PLATFORM_OS
                ]
                language_slots = [
                    item
                    for item in definition.flavor_slots
                    if item.axis is FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM
                ]
                self.assertEqual(len(os_slots), 1)
                self.assertEqual(
                    len(language_slots),
                    2 if sample.name == "full-stack-rust-js" else 1,
                )
                self.assertTrue(
                    all(
                        slot.cardinality is FlavorCardinality.EXACTLY_ONE
                        for slot in language_slots
                    )
                )
                if sample.name == "python-service-example":
                    self.assertEqual(
                        definition.entrypoints[0].kind, "persistent-service"
                    )
                    self.assertEqual(definition.entrypoints[0].path, "service")
                elif sample.name in {
                    "durable-split-service",
                    "react-dashboard-example",
                }:
                    self.assertEqual(definition.entrypoints[0].kind, "web-application")
                    self.assertEqual(
                        definition.entrypoints[0].path,
                        (
                            "page"
                            if sample.name == "durable-split-service"
                            else "dashboard"
                        ),
                    )
                else:
                    self.assertEqual(
                        definition.entrypoints[0].kind, "portable-application"
                    )
                    self.assertEqual(definition.entrypoints[0].path, "run")
                self.assertTrue(loaded.specification_set.requirements)
                self.assertNotIn("execution", metadata)
                execution, _document = _execution_contract(sample, definition, loaded)
                if sample.name == "python-service-example":
                    self.assertEqual(
                        execution["requirement_id"],
                        "component-md.sample-host-json-probe",
                    )
                    self.assertEqual(
                        execution["scenario_id"],
                        "sample-host-json-probe.first-page-of-three-clusters-is-two-items",
                    )
                self.assertEqual(execution["schema"], EXECUTION_CONTRACT_SCHEMA)
                language_target = execution["target"][
                    FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM.value
                ]
                if sample.name == "full-stack-rust-js":
                    self.assertEqual(
                        language_target,
                        {
                            "backend-language": "flavor://literate-ai/lang-rust",
                            "frontend-language": (
                                "flavor://literate-ai/lang-javascript"
                            ),
                        },
                    )
                else:
                    explicitly_pinned = bool(language_target)
                    self.assertEqual(
                        language_target,
                        (
                            [
                                f"flavor://literate-ai/lang-{language}"
                                for language in expected_variants(sample.name)
                            ]
                            if explicitly_pinned
                            else []
                        ),
                    )
                    self.assertEqual(
                        [
                            variant.variant_id
                            for variant in _execution_variants(definition, execution)
                        ],
                        list(expected_variants(sample.name)),
                    )
                source_root = sample / "source"
                cached_files = (
                    [path for path in source_root.rglob("*") if path.is_file()]
                    if source_root.exists()
                    else []
                )
                self.assertEqual(cached_files, [], "generated source was checked in")

    def test_every_sample_has_a_human_facing_story_and_catalog_role(self):
        catalog = (SAMPLES / "README.md").read_text(encoding="utf-8")
        review = (
            REPO_ROOT / "docs" / "architecture" / "sample-portfolio-review.md"
        ).read_text(encoding="utf-8")
        for sample in discover(SAMPLES):
            with self.subTest(sample=sample.name):
                _metadata, definition, _loaded, _closures = _load_sample(sample)
                specification = (sample / "component.md").read_text(encoding="utf-8")
                self.assertGreaterEqual(len(definition.description), 80)
                self.assertIn("# ", specification)
                self.assertIn("```mermaid", specification)
                self.assertIn(f"]({sample.name}/)", catalog)
                self.assertIn(f"| {definition.display_name} |", review)

        self.assertFalse(tuple(SAMPLES.rglob("component.json")))

    def test_cuda_recipes_compose_with_a_bound_fixture_stack_on_cpu_hosts(self):
        samples = ("cuda-vector-transform-cpp", "cuda-vector-transform-python")
        with (
            mock.patch(
                "tests.conformance.support.sample_runner._host_os",
                return_value="linux",
            ),
            mock.patch(
                "tests.conformance.support.sample_runner.discover",
                return_value=tuple(SAMPLES / name for name in samples),
            ),
        ):
            plans = plan_recipes(SAMPLES)
        self.assertEqual({plan["sample_id"] for plan in plans}, set(samples))
        for plan in plans:
            self.assertIn(self.cuda_stack_document, plan["recipe"].documents)
            self.assertIn(
                "nvcc",
                {
                    item["constraint"]["toolchain"]
                    for item in plan["toolchain_constraints"]
                },
            )

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

    def test_every_sample_variant_selects_the_public_standard_service(self):
        selections = standard_sample_execution_selections(SAMPLES)
        self.assertTrue(selections)
        self.assertEqual(
            {(item.sample_id, item.variant_id) for item in selections},
            {
                (sample.name, variant)
                for sample in discover(SAMPLES, platform=host_os())
                for variant in expected_variants(sample.name)
            },
        )
        self.assertEqual(
            {item.execution_service for item in selections},
            {"filesystem-standard-project-runtime"},
        )
        for item in selections:
            with self.subTest(sample=item.sample_id, variant=item.variant_id):
                self.assertEqual(
                    item.component_count,
                    (
                        4
                        if item.sample_id == "durable-split-service"
                        else 3
                        if item.sample_id == "service-stack"
                        else 1
                    ),
                )
                self.assertEqual(
                    item.dependency_edge_count,
                    (
                        3
                        if item.sample_id == "durable-split-service"
                        else 4
                        if item.sample_id == "service-stack"
                        else 0
                    ),
                )

    def test_standard_sample_report_rejects_identity_only_lifecycle_fixture(self):
        from tests.unit.test_standard_project_lifecycle import (
            LifecyclePorts,
            _decision,
            _diamond_lock,
            _names,
            _prepared_execution,
            _prepared_nodes,
            _service,
            _single_component_lock,
        )

        lock = _diamond_lock()
        execution, requests = _prepared_execution(lock)
        nodes = _prepared_nodes(execution, requests)
        names = _names(lock)
        result = _service(LifecyclePorts(execution, names)).execute(
            execution,
            component_lock=lock,
            invalidation=_decision(
                execution,
                names,
                "money",
                tuple(names.values()),
            ),
            prepared_nodes=nodes,
            max_parallelism=2,
        )

        with self.assertRaisesRegex(SampleFailure, "strict typed node evidence"):
            project_standard_sample_execution_report(
                result,
                component_lock=lock,
                sample_id="typed-standard-fixture",
                variant_id="host",
            )
        with self.assertRaisesRegex(SampleFailure, "Component lock"):
            project_standard_sample_execution_report(
                result,
                component_lock=_single_component_lock(lock, "money"),
                sample_id="typed-standard-fixture",
                variant_id="host",
            )

    def test_service_stack_variants_project_runtime_provider_edges(self):
        root = SAMPLES / "service-stack"
        _metadata, definition, loaded, closures = _load_sample(root)
        contract, _document = _execution_contract(root, definition, loaded)

        class ObservedTool:
            def __init__(self, name):
                self.command = (sys.executable,)
                self.identity = canonical_identity({"sample-projection-tool": name}).uri

            def require_unchanged(self):
                return None

        for variant in _execution_variants(definition, contract):
            with self.subTest(variant=variant.variant_id):
                authority, catalog = _sample_runtime_recipe(
                    root,
                    definition,
                    loaded.specification_set,
                    contract,
                    variant,
                    closures.generation.fork(),
                )[3:5]
                models = {
                    node.revision.identity.uri: canonical_identity(
                        {
                            "sample": "service-stack",
                            "variant": variant.variant_id,
                            "component": node.revision.identity.uri,
                        }
                    )
                    for node in authority.lock.nodes
                }
                execution = StandardProjectApplicationService.plan(
                    authority.lock, model_identities=models
                )
                closure = project_locked_standard_toolchain_closure(
                    _AuthoritySnapshot(authority, catalog),
                    execution,
                    toolchain_discoverer=lambda name, _constraint, _environment: (
                        ObservedTool(name)
                    ),
                    dependency_observer=lambda _commands: HostDependencyObservation(
                        (
                            {
                                "type": "application",
                                "bom-ref": "toolchain:sample-projection",
                                "name": "sample-projection",
                            },
                        ),
                        (),
                    ),
                )
                contracts = {
                    item.component_revision.uri: item for item in closure.contracts
                }
                artifact_edges = {
                    (
                        edge.consumer_revision.uri,
                        edge.provider_revision.uri,
                        edge.requirement_id,
                    ): edge
                    for action in execution.action_plans
                    for edge in action.dependency_edges
                    if edge.semantics.consumed_input.value == "artifact-export"
                }
                expected_exports = {
                    contracts[edge.provider_revision.uri].artifact_export.export_id
                    for edge in artifact_edges.values()
                }
                self.assertEqual(len(authority.lock.edges), 4)
                self.assertEqual(
                    {
                        (
                            edge.requirement_id,
                            edge.capability,
                            edge.semantics.consumer_phase.value,
                            edge.semantics.consumed_input.value,
                            edge.semantics.visible_to_generation,
                            edge.public_interface_identity is not None,
                        )
                        for edge in authority.lock.edges
                    },
                    {
                        (
                            "invoice-service",
                            "literate-ai.invoice-service",
                            "run",
                            "artifact-export",
                            False,
                            False,
                        ),
                        (
                            "invoice-service-interface",
                            "literate-ai.invoice-service",
                            "generate",
                            "public-interface",
                            True,
                            True,
                        ),
                        (
                            "money-calculation",
                            "literate-ai.money-calculation",
                            "run",
                            "artifact-export",
                            False,
                            False,
                        ),
                        (
                            "money-calculation-interface",
                            "literate-ai.money-calculation",
                            "generate",
                            "public-interface",
                            True,
                            True,
                        ),
                    },
                )
                self.assertEqual(len(artifact_edges), 2)
                self.assertEqual(set(closure.provider_environment), expected_exports)
                self.assertEqual(
                    {binding.export_id for binding in closure.record.provider_bindings},
                    expected_exports,
                )

    def test_sample_globs_select_a_stable_subset_and_reject_empty_matches(self):
        self.assertEqual(
            tuple(item.name for item in discover(SAMPLES, ("*roundtrip",))),
            ("regenerative-roundtrip",),
        )
        with self.assertRaisesRegex(SampleFailure, "matched no applications"):
            discover(SAMPLES, ("does-not-exist",))

    def test_platform_pinned_samples_are_selected_only_on_their_host_os(self):
        pinned = {
            "linux": "linux-cgroup-budget",
            "macos": "macos-launch-agent",
            "windows": "windows-path-auditor",
        }
        catalog = {item.name for item in discover(SAMPLES)}
        self.assertTrue(set(pinned.values()) <= catalog)
        for platform, sample_name in pinned.items():
            selected = {item.name for item in discover(SAMPLES, platform=platform)}
            self.assertIn(sample_name, selected)
            self.assertTrue(
                all(
                    other_name not in selected
                    for other_platform, other_name in pinned.items()
                    if other_platform != platform
                )
            )

    def test_cuda_samples_are_excluded_before_generation_without_a_healthy_gpu(self):
        cuda = {"cuda-vector-transform-cpp", "cuda-vector-transform-python"}
        with mock.patch(
            "tests.conformance.support.sample_runner._host_supports_nvidia_cuda",
            return_value=False,
        ):
            selected = {item.name for item in discover(SAMPLES, platform="linux")}
        self.assertTrue(cuda.isdisjoint(selected))
        with mock.patch(
            "tests.conformance.support.sample_runner._host_supports_nvidia_cuda",
            return_value=True,
        ):
            selected = {item.name for item in discover(SAMPLES, platform="linux")}
        self.assertTrue(cuda <= selected)

    def test_sample_package_flavors_cover_portable_and_os_specific_providers(self):
        providers: dict[str, set[str]] = {}
        for sample in discover(SAMPLES):
            execution = json.loads(
                (
                    SAMPLES / "_harness" / sample.name / "acceptance" / "execution.json"
                ).read_text(encoding="utf-8")
            )
            selected = execution["target"].get(FlavorAxis.PACKAGING.value)
            if selected is not None:
                providers.setdefault(selected, set()).add(sample.name)
        self.assertGreaterEqual(
            len(providers.get("flavor://literate-ai/package-pip", set())), 2
        )
        self.assertGreaterEqual(
            len(providers.get("flavor://literate-ai/package-conan", set())), 2
        )
        self.assertEqual(
            providers.get("flavor://literate-ai/package-brew"),
            {"macos-launch-agent"},
        )

    def test_local_sample_catalog_executes_independent_samples_in_parallel(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            sample_root = temporary_root / "samples"
            scratch = temporary_root / "runtime"
            for name in ("alpha", "beta"):
                harness = sample_root / "_harness" / name
                (harness / "acceptance").mkdir(parents=True)
                (harness / "sample.json").write_text("{}\n", encoding="utf-8")
                (harness / "acceptance" / "execution.json").write_text(
                    '{"target":{"platform.os":"host"}}\n', encoding="utf-8"
                )
                (sample_root / name).mkdir()
                (sample_root / name / "component.md").write_text(
                    f"# {name}\n", encoding="utf-8"
                )
            barrier = threading.Barrier(2)

            def execute(sample, *_args, **_kwargs):
                barrier.wait(timeout=2)
                return {
                    "sample_id": sample.name,
                    "assertions": [],
                    "executions": [
                        {
                            "execution_case_count": 0,
                            "generated_test_case_count": 0,
                            "source_sbom_identity": "sha256:" + "a" * 64,
                            "resolved_sbom_identity": "sha256:" + "b" * 64,
                        }
                    ],
                    "passed": True,
                }

            with (
                mock.patch(
                    "tests.conformance.support.sample_runner.discover_cpp_toolchain",
                    return_value=object(),
                ),
                mock.patch(
                    "tests.conformance.support.sample_runner.run_sample",
                    side_effect=execute,
                ),
            ):
                report = run_all(
                    sample_root,
                    scratch,
                    source_generator=object(),
                    allow_host_execution=True,
                    jobs=2,
                )

        self.assertTrue(report["passed"])
        self.assertEqual(
            [sample["sample_id"] for sample in report["samples"]],
            ["alpha", "beta"],
        )

    def test_runner_reuses_one_exact_flavor_for_two_compatible_roles(self):
        sample_root = SAMPLES / "full-stack-rust-js"
        _metadata, definition, loaded, closures = _load_sample(sample_root)
        execution, _document = _execution_contract(sample_root, definition, loaded)
        variant = _ExecutionVariant(
            "two-rust-roles",
            (
                ("backend-language", "rust"),
                ("frontend-language", "rust"),
                ("os", "host"),
            ),
            ("rust", "rust"),
        )

        *_, selected = _sample_runtime_recipe(
            sample_root,
            definition,
            loaded.specification_set,
            execution,
            variant,
            closures.generation,
        )
        bound = _ordered_selected_flavors(definition, selected, variant)
        recipes = tuple(item.recipe_flavor() for item in bound)
        rust = next(item for item in recipes if item.value == "rust")

        self.assertEqual(len([item for item in recipes if item.value == "rust"]), 1)
        self.assertEqual(rust.slot_ids, ("backend-language", "frontend-language"))
        self.assertEqual(
            len({skill.identity for skill in rust.skills}), len(rust.skills)
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

    def test_operator_toolchain_command_remains_authoritative(self):
        from literate_ai.contracts import ToolchainConstraint

        constraint = ToolchainConstraint(
            "python",
            command=("python-from-component",),
            minimum_version=(3, 12),
        )
        previous = os.environ.get("PYTHON")
        os.environ["PYTHON"] = "operator-python"
        try:
            options = _toolchain_discovery_options(
                constraint, operator_environment="PYTHON"
            )
        finally:
            if previous is None:
                os.environ.pop("PYTHON", None)
            else:
                os.environ["PYTHON"] = previous

        self.assertNotIn("pinned_command", options)
        self.assertEqual(options["minimum_version"], (3, 12))

    def test_every_sample_pin_compiles_into_the_full_execution_flow(self):
        endpoint = ModelEndpoint(
            "fixture-coding-cli",
            "coding-cli-fixture",
            "fixture-model",
            "unix:///fixture-coding-cli",
            Locality.LOCAL,
            ("structured-output", "source-generation"),
            1_000_000,
        )
        for sample in discover(SAMPLES):
            with self.subTest(sample=sample.name):
                _metadata, definition, _loaded, _closures = _load_sample(sample)
                plan = compile_generation_execution_plan(
                    component=definition,
                    workflow_content=(
                        sample / definition.workflow_definition.uri
                    ).read_bytes(),
                    routing_content=(
                        sample / definition.routing_policy.uri
                    ).read_bytes(),
                    endpoint=endpoint,
                    generation_prompt=f"Recipe for {sample.name}",
                )
                self.assertEqual(
                    [stage.stage_id for stage in plan.model_stages],
                    ["plan", "generate"],
                )
                self.assertEqual(
                    plan.lifecycle_steps,
                    (
                        "validate",
                        "classify",
                        "authorize-build",
                        "build",
                        "resolve-dependencies",
                        "test-generated",
                        "verify-independent",
                        "prepare-tree",
                        "commit-tree",
                    ),
                )

    def test_host_execution_requires_explicit_acknowledgement_before_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SampleFailure, "explicit.*acknowledgement"):
                run_all(
                    SAMPLES,
                    Path(directory),
                    allow_host_execution=False,
                )

    def test_os_requirements_are_content_pinned_flavors(self):
        contracts = platform_contracts()
        self.assertEqual(set(contracts), {"linux", "macos", "windows"})
        for os_name in contracts:
            with self.subTest(platform=os_name):
                root = REPO_ROOT / "flavors" / f"os-{os_name}"
                definition = _flavor_definition(root)
                self.assertIs(definition.primary_axis, FlavorAxis.PLATFORM_OS)
                self.assertEqual(definition.supported_targets, (os_name,))
                self.assertIn("sample.portable-app", definition.applicable_capabilities)
                loaded = OpenSpecProvider().load(
                    root,
                    tuple(item.uri for item in definition.specification_fragments),
                )
                self.assertTrue(loaded.specification_set.requirements)
                self.assertEqual(
                    tuple(item.identity for item in loaded.specification_set.artifacts),
                    tuple(item.identity for item in definition.specification_fragments),
                )

    def test_language_toolchains_are_content_pinned_flavors(self):
        definitions = {}
        for language in ("python", "cpp", "rust", "javascript"):
            with self.subTest(language=language):
                root = REPO_ROOT / "flavors" / f"lang-{language}"
                definition = _flavor_definition(root)
                definitions[language] = definition
                self.assertIs(
                    definition.primary_axis,
                    FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
                )
                self.assertEqual(definition.supported_targets, (language,))
                self.assertIn("sample.portable-app", definition.applicable_capabilities)
                skill_refs = tuple(
                    item
                    for item in definition.authoring_inputs
                    if item.kind == "specification-to-source-skill"
                )
                self.assertEqual(len(skill_refs), 1)
                loaded = OpenSpecProvider().load(
                    root,
                    tuple(item.uri for item in definition.specification_fragments),
                )
                self.assertTrue(loaded.specification_set.requirements)
                self.assertEqual(
                    tuple(item.identity for item in loaded.specification_set.artifacts),
                    tuple(item.identity for item in definition.specification_fragments),
                )
        self.assertIn(
            definitions["cpp"].coordinate.uri, definitions["python"].conflicts
        )
        self.assertIn(
            definitions["python"].coordinate.uri, definitions["cpp"].conflicts
        )
        self.assertNotIn(
            definitions["javascript"].coordinate.uri,
            definitions["rust"].conflicts,
        )
        self.assertNotIn(
            definitions["rust"].coordinate.uri,
            definitions["javascript"].conflicts,
        )

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
