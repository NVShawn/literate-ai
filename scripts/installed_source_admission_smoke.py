"""Exercise source admission using only an installed Literate AI wheel."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from literate_ai.adapters.cache import (
    FileSystemSourceCache,
    FilesystemStandardSourceAdmissionPublisher,
    FilesystemStandardSourceRestorer,
    SourceCacheMaterializer,
    SourceCacheResolver,
)
from literate_ai.adapters.lifecycle import local_generated_source_tree_identity
from literate_ai.application import (
    StandardSourceAdmissionRequest,
    StandardSourceAdmissionService,
    StandardSourceVerification,
    require_source_admission_for_worker,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    ContentIdentity,
    ContractValidationError,
    GeneratedSourceCandidate,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheModelBinding,
    SourceCacheRootKind,
    SourceCacheTarget,
    SourceDerivationCacheKey,
    SourceGenerationProvenance,
    SourceGenerationResumeCandidate,
    SourceGenerationRunOutput,
    StandardSourceAdmissionMembership,
    StandardSourceAdmissionReceiptComposition,
    StandardSourceSelector,
    StandardSourceSelectorScope,
    StandardSourceSelectorSet,
    StandardSourceTestResult,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.generated_tests import GENERATED_TEST_SUITE_PATH
from literate_ai.storage import FileSystemCAS


def identity(label: str) -> ContentIdentity:
    return canonical_identity({"installed-source-admission-smoke": label})


def target_independent() -> StandardSourceSelectorSet:
    return StandardSourceSelectorSet(StandardSourceSelectorScope.TARGET_INDEPENDENT, ())


def worker_selectors(platform: str) -> StandardSourceSelectorSet:
    return StandardSourceSelectorSet(
        StandardSourceSelectorScope.TARGET_SPECIFIC,
        (StandardSourceSelector("platform.os", platform),),
    )


@dataclass(frozen=True)
class SourceCustody:
    source_bom_content: bytes
    generated_test_suite_content: bytes


class GeneratedTestVerifier:
    def __init__(self, generation: SourceGenerationResumeCandidate, root: Path) -> None:
        self.generation = generation
        self.root = root
        self.calls = 0

    def verify(
        self, generation: SourceGenerationResumeCandidate
    ) -> StandardSourceVerification:
        self.calls += 1
        if generation != self.generation:
            raise RuntimeError(
                "installed-wheel verifier received substituted generation"
            )
        completed = subprocess.run(
            (sys.executable, "-B", "test_generated.py"),
            cwd=self.root,
            check=False,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0 or "Ran 3 tests" not in (
            completed.stdout + completed.stderr
        ):
            raise RuntimeError(
                "installed-wheel generated tests did not run successfully: "
                + (completed.stderr or completed.stdout)[-2000:]
            )
        candidate = generation.output.candidate
        return StandardSourceVerification(
            candidate.source_manifest_identity,
            identity(f"test-plan-{candidate.component_revision.digest}"),
            (
                StandardSourceTestResult(
                    identity(f"oracle-{candidate.component_revision.digest}"),
                    identity(f"test-result-{candidate.component_revision.digest}"),
                    3,
                    3,
                    0,
                    0,
                ),
            ),
            identity("installed-wheel-verifier"),
        )


def cache_key(node: int, *, session: str) -> SourceDerivationCacheKey:
    return SourceDerivationCacheKey(
        identity(f"recipe-{node}"),
        identity("execution-plan"),
        identity("coding-cli-tool"),
        SourceCacheModelBinding("wheel-smoke", "inherited"),
        identity(f"planned-request-{session}-{node}"),
        identity(f"source-semantics-{node}"),
    )


def create_generation(
    node: int,
    *,
    session: str,
    orchestration: ContentIdentity,
    cas: FileSystemCAS,
    source_root: Path,
) -> tuple[
    SourceGenerationResumeCandidate,
    SourceDerivationCacheKey,
    SourceCustody,
]:
    source_root.mkdir(parents=True)
    source_root.joinpath("module.py").write_text(
        f"VALUE = {node}\n",
        encoding="utf-8",
    )
    source_root.joinpath("test_generated.py").write_text(
        """\
import unittest
import module


class GeneratedTests(unittest.TestCase):
    def test_value_is_integer(self):
        self.assertIsInstance(module.VALUE, int)

    def test_value_is_nonnegative(self):
        self.assertGreaterEqual(module.VALUE, 0)

    def test_value_is_bounded(self):
        self.assertLess(module.VALUE, 12)


if __name__ == "__main__":
    unittest.main()
""",
        encoding="utf-8",
    )
    source_bom = canonical_json_bytes(
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.7",
            "serialNumber": f"urn:uuid:00000000-0000-4000-8000-{node:012d}",
            "version": 1,
        }
    )
    generated_suite = canonical_json_bytes(
        {
            "schema": "literate-ai/generated-test-suite@1",
            "node": node,
            "tests": [
                "test_value_is_integer",
                "test_value_is_nonnegative",
                "test_value_is_bounded",
            ],
        }
    )
    source_bom_path = source_root.joinpath(*Path(CYCLONEDX_SOURCE_SBOM_PATH).parts)
    source_bom_path.parent.mkdir(parents=True, exist_ok=True)
    source_bom_path.write_bytes(source_bom)
    generated_suite_path = source_root.joinpath(*Path(GENERATED_TEST_SUITE_PATH).parts)
    generated_suite_path.parent.mkdir(parents=True, exist_ok=True)
    generated_suite_path.write_bytes(generated_suite)
    manifest = cas.put_manifest(
        {
            "schema": "literate-ai/generated-source-manifest@1",
            "node": node,
            "files": [
                "module.py",
                CYCLONEDX_SOURCE_SBOM_PATH,
                GENERATED_TEST_SUITE_PATH,
                "test_generated.py",
            ],
        }
    )
    source_bom_blob = cas.put_bytes(
        source_bom, media_type="application/vnd.cyclonedx+json"
    )
    generated_suite_blob = cas.put_bytes(generated_suite, media_type="application/json")
    transcript = identity(f"coding-cli-transcript-{node}")
    candidate = GeneratedSourceCandidate(
        component_revision=identity(f"component-{node}"),
        source_generation_request_identity=orchestration,
        planned_coding_cli_request_identity=cache_key(
            node, session=session
        ).request_identity,
        component_generation_plan_identity=identity(f"generation-plan-{node}"),
        generation_key_identity=identity(f"generation-key-{node}"),
        context_manifest_identity=identity(f"context-{node}"),
        prompt_identity=identity(f"prompt-{node}"),
        recipe_identity=identity(f"recipe-{node}"),
        workspace_allocation_identity=identity(f"workspace-{session}-{node}"),
        tree_identity=local_generated_source_tree_identity(source_root),
        source_bundle_identity=identity(f"source-bundle-{node}"),
        source_manifest_identity=ContentIdentity.parse_uri(manifest.identity),
        source_bom_identity=ContentIdentity.parse_uri(source_bom_blob.identity),
        generated_test_suite_identity=ContentIdentity.parse_uri(
            generated_suite_blob.identity
        ),
    )
    provenance = SourceGenerationProvenance(
        source_generation_request_identity=orchestration,
        planned_coding_cli_request_identity=(
            candidate.planned_coding_cli_request_identity
        ),
        component_lock_identity=identity("component-lock"),
        application_root_revision_identity=identity("application-root"),
        generated_component_revision_identity=candidate.component_revision,
        component_generation_plan_identity=(
            candidate.component_generation_plan_identity
        ),
        generation_key_identity=candidate.generation_key_identity,
        context_manifest_identity=candidate.context_manifest_identity,
        prompt_identity=candidate.prompt_identity,
        recipe_identity=candidate.recipe_identity,
        workspace_allocation_identity=candidate.workspace_allocation_identity,
        readiness_identity=identity(f"readiness-{node}"),
        route_decision_identities=(identity(f"route-{node}"),),
        model_stage_output_identities=(transcript,),
        candidate_identity=candidate.identity,
    )
    output = SourceGenerationRunOutput(
        candidate,
        candidate.identity,
        provenance,
        provenance.identity,
    )
    generation = SourceGenerationResumeCandidate(
        output,
        output.identity,
        identity(f"budget-{node}"),
        identity(f"complexity-{node}"),
    )
    key = cache_key(node, session=session)
    return generation, key, SourceCustody(source_bom, generated_suite)


def restore_in_fresh_process(
    root: Path, framework_distribution: ContentIdentity
) -> dict[str, object]:
    """Restore session-A admission through session-B keys with no generator port."""

    reader = FileSystemSourceCache(
        "wheel-source-admission", root / "cache", writable=False
    )
    resolver = SourceCacheResolver(
        SourceCacheConfiguration(
            mode=SourceCacheMode.READ_ONLY,
            targets=(
                SourceCacheTarget(
                    "wheel-source-admission",
                    SourceCacheRootKind.OPERATOR_BOUND,
                    "installed-wheel-smoke",
                ),
            ),
            write_target_id=None,
            require_unique=True,
        ),
        {"wheel-source-admission": reader},
    )
    restorer = FilesystemStandardSourceRestorer(
        resolver=resolver,
        materializer=SourceCacheMaterializer(),
    )
    missing_destination = root / "restored" / "missing-membership"
    if (
        restorer.restore(
            cache_key(99, session="session-b"),
            missing_destination,
            component_lock_identity=identity("component-lock"),
        )
        is not None
        or missing_destination.exists()
    ):
        raise RuntimeError("absent membership did not remain fail-closed")
    restored_entry_identities: set[str] = set()
    restored_by_platform = {"linux": 0, "windows": 0}
    matrix_cells = tuple(range(27))
    work = (
        *(("linux", cell, cell % 12) for cell in matrix_cells),
        *(("windows", node, node) for node in range(12)),
    )
    for platform, cell, node in work:
        session_b_key = cache_key(node, session="session-b")
        destination = root / "restored" / platform / f"cell-{cell}-node-{node}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        restored = restorer.restore(
            session_b_key,
            destination,
            component_lock_identity=identity("component-lock"),
        )
        if restored is None or not isinstance(
            restored.membership, StandardSourceAdmissionMembership
        ):
            raise RuntimeError(
                "source_cache.runtime_absent: fresh-session accepted source did not "
                "restore"
            )
        evidence = restored.membership.evidence
        if (
            evidence.planned_coding_cli_request_identity
            == session_b_key.request_identity
        ):
            raise RuntimeError(
                "fresh-session probe reused admission transaction identity"
            )
        require_source_admission_for_worker(
            restored.membership,
            expected_component_lock_identity=identity("component-lock"),
            expected_generation_plan_identity=identity(f"generation-plan-{node}"),
            expected_generation_key_identity=identity(f"generation-key-{node}"),
            expected_flavor_set_identity=identity("flavor-set"),
            expected_skill_closure_identity=identity("skill-closure"),
            expected_framework_distribution_identity=framework_distribution,
            worker_selectors=worker_selectors(platform),
        )
        restored_by_platform[platform] += 1
        restored_entry_identities.add(restored.entry_identity.uri)
    if restored_by_platform != {"linux": 27, "windows": 12}:
        raise RuntimeError("installed-wheel matrix restore did not cover every cell")
    if len(restored_entry_identities) != 12:
        raise RuntimeError(
            "installed-wheel matrix restore did not consume exactly twelve admissions"
        )
    return {
        "generator_fallbacks": 0,
        "linux_cache_only_restores": restored_by_platform["linux"],
        "missing_membership_fail_closed": True,
        "runtime_absent_resolved": True,
        "windows_cache_only_restores": restored_by_platform["windows"],
        "restored_workers": sum(restored_by_platform.values()),
        "unique_cache_entries": len(restored_entry_identities),
    }


def main() -> int:
    if len(sys.argv) not in {3, 4}:
        raise SystemExit(
            "usage: installed_source_admission_smoke.py ROOT DISTRIBUTION_IDENTITY "
            "[restore]"
        )
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    framework_distribution = ContentIdentity.parse_uri(sys.argv[2])
    if len(sys.argv) == 4:
        if sys.argv[3] != "restore":
            raise SystemExit("installed source-admission phase must be 'restore'")
        print(json.dumps(restore_in_fresh_process(root, framework_distribution)))
        return 0
    cas = FileSystemCAS(root / "cas")
    cache = FileSystemSourceCache("wheel-source-admission", root / "cache")
    orchestration = identity("shared-orchestration")
    generations: list[SourceGenerationResumeCandidate] = []
    keys: list[SourceDerivationCacheKey] = []
    roots: dict[ContentIdentity, Path] = {}
    custody: dict[ContentIdentity, SourceCustody] = {}
    key_by_request: dict[ContentIdentity, SourceDerivationCacheKey] = {}
    verifier_calls = 0
    memberships: list[StandardSourceAdmissionMembership] = []

    publisher = FilesystemStandardSourceAdmissionPublisher(
        cache=cache,
        caller_cas=cas,
        source_root=lambda tree: roots[tree],
        source_custody=lambda tree: custody[tree],
        cache_key=lambda membership: key_by_request[
            membership.evidence.planned_coding_cli_request_identity
        ],
    )
    for node in range(12):
        generation, key, node_custody = create_generation(
            node,
            session="session-a",
            orchestration=orchestration,
            cas=cas,
            source_root=root / "generated" / f"node-{node}",
        )
        candidate = generation.output.candidate
        generations.append(generation)
        keys.append(key)
        key_by_request[candidate.planned_coding_cli_request_identity] = key
        roots[candidate.tree_identity] = root / "generated" / f"node-{node}"
        custody[candidate.tree_identity] = node_custody
        request = StandardSourceAdmissionRequest(
            generation,
            key,
            identity("flavor-set"),
            identity("skill-closure"),
            identity("coding-cli-tool"),
            identity(f"coding-cli-transcript-{node}"),
            target_independent(),
            framework_distribution,
        )
        verifier = GeneratedTestVerifier(generation, roots[candidate.tree_identity])
        membership = StandardSourceAdmissionService(
            verifier,
            publisher,
            clock=lambda: datetime(2026, 8, 15, tzinfo=UTC),
        ).admit(request)
        verifier_calls += verifier.calls
        memberships.append(membership)

    if (
        verifier_calls != 12
        or sum(
            result.passed_count
            for membership in memberships
            for result in membership.evidence.test_results
        )
        != 36
    ):
        raise RuntimeError("installed-wheel admission did not execute all 36 tests")

    with tempfile.TemporaryDirectory(
        prefix="litai-installed-source-workspace-b-"
    ) as temporary:
        fresh_workspace = Path(temporary) / "repository-copy"
        fresh_workspace.mkdir()
        shutil.copytree(root / "cache", fresh_workspace / "cache")
        restored_process = subprocess.run(
            (
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                str(fresh_workspace),
                framework_distribution.uri,
                "restore",
            ),
            check=False,
            text=True,
            capture_output=True,
        )
    if restored_process.returncode != 0:
        raise RuntimeError(
            "installed-wheel fresh-process restore failed: "
            + (restored_process.stderr or restored_process.stdout)[-4000:]
        )
    restore_report = json.loads(restored_process.stdout)

    early_receipt_rejected = False
    try:
        StandardSourceAdmissionReceiptComposition(
            identity("execution-plan"),
            tuple(membership.identity for membership in memberships),
            (),
            identity("project-admission"),
        )
    except ContractValidationError:
        early_receipt_rejected = True
    if not early_receipt_rejected:
        raise RuntimeError("source admission fabricated downstream receipt success")
    receipt = StandardSourceAdmissionReceiptComposition(
        identity("execution-plan"),
        tuple(membership.identity for membership in memberships),
        tuple(identity(f"downstream-result-{node}") for node in range(12)),
        identity("project-admission"),
    )
    print(
        json.dumps(
            {
                "admissions": len(memberships),
                "downstream_receipt_identity": receipt.identity.uri,
                "early_receipt_rejected": early_receipt_rejected,
                "generator_fallbacks": restore_report["generator_fallbacks"],
                "linux_cache_only_restores": restore_report[
                    "linux_cache_only_restores"
                ],
                "missing_membership_fail_closed": restore_report[
                    "missing_membership_fail_closed"
                ],
                "runtime_absent_resolved": restore_report["runtime_absent_resolved"],
                "windows_cache_only_restores": restore_report[
                    "windows_cache_only_restores"
                ],
                "orchestration_identity": orchestration.uri,
                "planned_request_identities": [
                    generation.output.candidate.planned_coding_cli_request_identity.uri
                    for generation in generations
                ],
                "restored_workers": restore_report["restored_workers"],
                "unique_cache_entries": restore_report["unique_cache_entries"],
                "tests_executed": 36,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
