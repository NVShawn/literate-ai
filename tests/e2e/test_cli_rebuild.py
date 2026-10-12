from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.cache.rebuild import read_rebuild_source_cache_control
from literate_ai.adapters.generation_preparation import PreparedLockedGeneration
from literate_ai.adapters.lifecycle import LocalStandardLifecycleError
from literate_ai.adapters.project_initialization import initialize_project
from literate_ai.adapters.standard_lifecycle_binding import (
    InstalledFrameworkDistribution,
    InstalledFrameworkDistributionMember,
    ResolvedStandardProjectLifecycleDriver,
)
from literate_ai.adapters.standard_project import (
    FilesystemStandardProjectRuntime,
    PlannedStandardProject,
    StandardProjectRuntimeReadiness,
)
from literate_ai.adapters.standard_rebuild import (
    FilesystemStandardRebuildAdapter,
    _resolved_source_cache,
)
from literate_ai.cli import main
from literate_ai.contracts import (
    BlobRef,
    ComponentCommandPhase,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestSummary,
    RepositoryParentSelection,
    StandardProjectLifecycleDriver,
    VersionedContentRef,
    canonical_identity,
    load_current_standard_lifecycle_policy,
)
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.projects import load_project


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    content = output.getvalue() if status == 0 else errors.getvalue()
    return status, json.loads(content)


DRIVER_SOURCE = r"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path[:0] = __FRAMEWORK_IMPORT_PATHS__
CACHE_AWARE = __CACHE_AWARE__
PLANNING_BEHAVIOR = __PLANNING_BEHAVIOR__

from literate_ai.adapters.cache.rebuild import (
    SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT,
    SOURCE_CACHE_PLANNING_MODE,
    SOURCE_CACHE_PLANNING_MODE_ENVIRONMENT,
    SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT,
    RebuildSourceCacheSession,
    read_rebuild_source_cache_control,
    read_rebuild_source_cache_decision,
    write_rebuild_source_cache_decision,
    write_rebuild_source_cache_derivation_manifest,
    write_rebuild_source_cache_lifecycle,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestReceipt,
    ProjectTestReceiptProvisional,
    RebuildSourceCacheLifecycleBinding,
    RebuildSourceCacheLifecycleMember,
    RebuildSourceCacheDerivationManifest,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    canonical_identity,
    rebuild_project_authority_identity,
)
from literate_ai.projects import discover_project
from literate_ai.test_receipts import write_project_test_receipt_provisional

parser = argparse.ArgumentParser()
parser.add_argument("--project")
parser.add_argument("--specification")
parser.add_argument("--runtime-root")
parser.add_argument("--candidate")
parser.add_argument("--project-revision")
parser.add_argument("--request")
parser.add_argument("--allow-host-execution", action="store_true")
args = parser.parse_args()
digest = "sha256:" + "1" * 64
runner = "sha256:" + "2" * 64
identity = ContentIdentity.parse_uri(digest)
key = SourceDerivationCacheKey(
    recipe_identity=identity,
    execution_plan_identity=identity,
    coding_cli_tool_binding_identity=identity,
    model_binding=SourceCacheModelBinding("fixture", "fixture-model"),
    request_identity=identity,
)
component_lock_identities = tuple(
    sorted(
        (
            canonical_identity({"component-lock": "fixture-a"}),
            canonical_identity({"component-lock": "fixture-b"}),
        ),
        key=lambda item: item.uri,
    )
)
if os.environ.get(SOURCE_CACHE_PLANNING_MODE_ENVIRONMENT) == SOURCE_CACHE_PLANNING_MODE:
    if args.allow_host_execution or PLANNING_BEHAVIOR == "unsupported":
        raise SystemExit(3)
    project = discover_project(Path(args.project))
    assert project is not None and project.definition.lifecycle_driver is not None
    planning_request_identity = ContentIdentity.parse_uri(
        os.environ[SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT]
    )
    lifecycle_driver_identity = project.definition.lifecycle_driver.identity
    lifecycle_plan_identity = canonical_identity(
        {
            "schema": "literate-ai/rebuild-derivation-plan@2",
            "component_lock_identities": [
                item.uri for item in component_lock_identities
            ],
            "cache_key_identities": [key.identity.uri],
        }
    )
    if PLANNING_BEHAVIOR == "tamper-request":
        planning_request_identity = identity
    elif PLANNING_BEHAVIOR == "tamper-driver":
        lifecycle_driver_identity = identity
    elif PLANNING_BEHAVIOR == "tamper-plan":
        lifecycle_plan_identity = identity
    manifest = RebuildSourceCacheDerivationManifest(
        planning_request_identity=planning_request_identity,
        project_revision_identity=rebuild_project_authority_identity(
            ContentIdentity.parse_uri(args.project_revision),
            component_lock_identities,
        ),
        lifecycle_driver_identity=lifecycle_driver_identity,
        lifecycle_plan_identity=lifecycle_plan_identity,
        component_lock_identities=component_lock_identities,
        cache_keys=(key,),
    )
    write_rebuild_source_cache_derivation_manifest(
        Path(os.environ[SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT]), manifest
    )
    raise SystemExit(0)
if not args.allow_host_execution:
    raise SystemExit(3)
cache_decision = os.environ.get("LITAI_SOURCE_CACHE_DECISION_IDENTITY")
if cache_decision is None and CACHE_AWARE:
    control = read_rebuild_source_cache_control(
        Path(os.environ["LITAI_SOURCE_CACHE_CONTROL"])
    )
    project = discover_project(Path(args.project))
    assert project is not None
    session = RebuildSourceCacheSession(control, project=project)
    session.resolve(key, destination=Path(args.runtime_root) / "cached-source")
    decision = session.decision()
    write_rebuild_source_cache_decision(
        Path(os.environ["LITAI_SOURCE_CACHE_DECISION"]), decision
    )
    cache_decision = decision.identity.uri
elif cache_decision is not None and CACHE_AWARE:
    control = read_rebuild_source_cache_control(
        Path(os.environ["LITAI_SOURCE_CACHE_CONTROL"])
    )
    decision = read_rebuild_source_cache_decision(
        Path(os.environ["LITAI_SOURCE_CACHE_DECISION"])
    )
if not CACHE_AWARE:
    cache_decision = digest
subject = ContentIdentity.parse_uri("sha256:" + "3" * 64)
if CACHE_AWARE:
    member = RebuildSourceCacheLifecycleMember(
        cache_key=key,
        accepted_entry_identity=None,
        source_tree_identity=identity,
        source_intelligence_identity=identity,
        build_evidence_identity=identity,
        test_evidence_identity=identity,
        acceptance_evidence_identity=identity,
        workspace_admission_identity=identity,
        provenance_evidence_identity=identity,
        source_sbom_identity=identity,
        resolved_sbom_identity=identity,
    )
    lifecycle = RebuildSourceCacheLifecycleBinding(
        control_identity=control.identity,
        decision_identity=decision.identity,
        lifecycle_request_identity=ContentIdentity.parse_uri(args.request),
        lifecycle_plan_identity=control.derivation_manifest.lifecycle_plan_identity,
        receipt_subject_identity=subject,
        component_lock_identities=control.component_lock_identities,
        derivation_key_identities=(key.identity,),
        members=(member,),
    )
    lifecycle_evidence = {
        kind: lifecycle.evidence_identity(kind).uri
        for kind in lifecycle.EVIDENCE_KINDS
    }
    lifecycle_evidence["source-cache-lifecycle"] = lifecycle.identity.uri
    write_rebuild_source_cache_lifecycle(
        Path(os.environ["LITAI_SOURCE_CACHE_LIFECYCLE"]), lifecycle
    )
else:
    lifecycle_evidence = {
        "acceptance-result": digest,
        "build-result": digest,
        "source-intelligence": digest,
        "generation-provenance": digest,
        "resolved-sbom": digest,
        "source-sbom": digest,
        "source-cache-lifecycle": digest,
        "test-report": digest,
        "workspace-admission": digest,
    }
receipt = {
    "schema": "urn:literate-ai:schema:v1:project-test-receipt",
    "project": "fixture",
    "project_revision": args.project_revision,
    "subject": subject.uri,
    "suite": {"id": "fixture-e2e", "version": "1.0.0", "revision": digest},
    "tests": 8,
    "result": "sha256:" + "4" * 64,
    "evidence": {
        **lifecycle_evidence,
        "lifecycle-command": os.environ["LITAI_LIFECYCLE_COMMAND_IDENTITY"],
        "lifecycle-plan": (
            control.derivation_manifest.lifecycle_plan_identity.uri
            if CACHE_AWARE
            else digest
        ),
        "lifecycle-request": args.request,
        "observation-result": digest,
        "security-scan-report": digest,
        "source-cache-decision": cache_decision,
        "test-runner": runner,
    },
}
receipt = ProjectTestReceipt.from_dict(receipt)
provisional = ProjectTestReceiptProvisional(
    lifecycle_request_identity=ContentIdentity.parse_uri(args.request),
    lifecycle_command_identity=ContentIdentity.parse_uri(
        os.environ["LITAI_LIFECYCLE_COMMAND_IDENTITY"]
    ),
    source_cache_control_identity=ContentIdentity.parse_uri(
        os.environ["LITAI_SOURCE_CACHE_CONTROL_IDENTITY"]
    ),
    component_lock_identities=(
        control.component_lock_identities
        if CACHE_AWARE
        else component_lock_identities
    ),
    receipt_identity=receipt.identity,
    receipt=receipt,
)
write_project_test_receipt_provisional(Path(args.candidate), provisional)
"""


class RebuildCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Initialize the project once; each test gets a private copy.
        template = tempfile.TemporaryDirectory()
        cls.addClassCleanup(template.cleanup)
        cls.project_template = Path(template.name).resolve() / "project"
        initialize_project(
            cls.project_template,
            parent_selection=RepositoryParentSelection.root(),
            source_intelligence_provider="none",
            flavor_selectors=("+python", "+bazel", "+macos"),
        )

    def setUp(self) -> None:
        self.project_index_preflight = patch(
            "literate_ai.cli.rebuild.require_lifecycle_project_index",
            return_value={
                "schema": "literate-ai/project-source-intelligence-status@1",
                "state": "off",
                "provider_id": "none",
                "artifact_path": None,
            },
        )
        self.project_index_preflight.start()
        self.addCleanup(self.project_index_preflight.stop)

    def _project(
        self,
        root: Path,
        *,
        include_driver: bool = True,
        planning_behavior: str = "normal",
    ) -> Path:
        project = root / "project"
        shutil.copytree(self.project_template, project, symlinks=True)
        driver_script = project / "driver.py"
        driver_script.write_text(
            DRIVER_SOURCE.replace("__FRAMEWORK_IMPORT_PATHS__", repr(list(sys.path)))
            .replace("__CACHE_AWARE__", "True")
            .replace("__PLANNING_BEHAVIOR__", repr(planning_behavior)),
            encoding="utf-8",
        )
        manifest_path = project / "literate.project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        runner_identity = {
            "schema": "urn:literate-ai:schema:v1:content-identity",
            "algorithm": "sha256",
            "digest": "2" * 64,
        }
        manifest["project_id"] = "fixture"
        manifest["test_receipt_policy"] = {
            "schema": "urn:literate-ai:schema:v1:project-test-receipt-policy",
            "suite_id": "fixture-e2e",
            "suite_version": "1.0.0",
            "runner_identity": runner_identity,
            "required_evidence_kinds": [
                "acceptance-result",
                "build-result",
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
            ],
            "minimum_test_count": 8,
        }
        manifest.pop("lifecycle_driver", None)
        if include_driver:
            driver_bytes = driver_script.read_bytes()
            implementation_identity = canonical_identity(
                {
                    "schema": "literate-ai/project-lifecycle-implementation@1",
                    "members": [
                        {
                            "path": "driver.py",
                            "size": len(driver_bytes),
                            "identity": "sha256:"
                            + hashlib.sha256(driver_bytes).hexdigest(),
                        }
                    ],
                }
            )
            manifest["lifecycle_driver"] = {
                "schema": "urn:literate-ai:schema:v1:project-lifecycle-driver",
                "driver_id": "fixture-driver",
                "version": "1.0.0",
                "implementation_paths": ["driver.py"],
                "implementation_identity": implementation_identity.to_dict(),
                "argv": [
                    "{python}",
                    "driver.py",
                    "--project",
                    "{project}",
                    "--specification",
                    "{specification}",
                    "--runtime-root",
                    "{runtime_root}",
                    "--candidate",
                    "{candidate_receipt}",
                    "--project-revision",
                    "{project_revision_identity}",
                    "--request",
                    "{lifecycle_request_identity}",
                    "{allow_host_execution}",
                ],
                "environment_keys": ["PATH"],
                "phases": [
                    "compose-and-plan",
                    "resolve-source-cache",
                    "generate-source-and-tests-or-use-cache",
                    "derive-source-intelligence",
                    "validate-classify-and-authorize",
                    "resolve-and-prepare-dependencies",
                    "native-build",
                    "verify-resolved-sbom",
                    "run-generated-tests",
                    "execute-application",
                    "independent-acceptance",
                    "admit-workspace",
                    "write-candidate-receipt",
                ],
                "specification_scope": "project",
                "timeout_seconds": 30,
            }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return project

    def _configure_standard_driver(self, project: Path) -> None:
        manifest_path = project / "literate.project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["lifecycle_driver"] = {
            "schema": "urn:literate-ai:schema:v2:project-lifecycle-driver",
            "binding": "standard",
            "framework_distribution_identity": canonical_identity(
                {"fixture": "installed-framework"}
            ).to_dict(),
            "policy_identity": canonical_identity(
                {"fixture": "standard-policy"}
            ).to_dict(),
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def _clear_component_locks(self, project: Path) -> None:
        for lock in project.rglob("component.lock.json"):
            lock.unlink()

    def test_standard_project_root_rebuild_fails_without_committed_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(Path(directory))
            self._configure_standard_driver(project)
            self._clear_component_locks(project)
            resolved = Mock()
            with (
                patch(
                    "literate_ai.cli.rebuild.resolve_standard_project_lifecycle_driver",
                    return_value=resolved,
                ),
                patch(
                    "literate_ai.cli.rebuild._standard_rebuild_one_component"
                ) as rebuild_one,
            ):
                status, envelope = invoke(
                    "rebuild",
                    ".",
                    "--project",
                    str(project),
                    "--allow-host-execution",
                )

            self.assertEqual(status, 2, envelope)
            self.assertEqual(
                envelope["error"]["code"], "rebuild.standard_lock_set_empty"
            )
            rebuild_one.assert_not_called()

    def test_public_standard_product_results_agree_with_committed_receipts(self):
        """Accepted-lifecycle fixture; CLI, adapter finalization and commit are real.

        This tests result projection, not provider generation or library acceptance.
        Those have their own lifecycle tests. In particular, never mock the CLI
        handler, Standard rebuild method, or receipt finalizer/commit under test.
        """
        for product in ("library", "executable", "multi-entrypoint"):
            with (
                self.subTest(product=product),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                project_root = self._project(root)
                policy = load_current_standard_lifecycle_policy()
                distribution = InstalledFrameworkDistribution(
                    "literate-ai",
                    "1.1.0",
                    (
                        InstalledFrameworkDistributionMember(
                            "literate_ai/__init__.py",
                            1,
                            "sha256:" + "a" * 64,
                        ),
                    ),
                )
                driver = StandardProjectLifecycleDriver(
                    distribution.identity, policy.identity
                )
                binding = ResolvedStandardProjectLifecycleDriver(
                    driver,
                    distribution,
                    policy,
                    lambda distribution=distribution: distribution,
                    lambda policy=policy: policy,
                )
                manifest_path = project_root / "literate.project.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["lifecycle_driver"] = driver.to_dict()
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                project = load_project(project_root)
                component = project_root / "samples" / "hello-component"
                # Result projection consumes a completed, already accepted graph.
                revision = canonical_identity({"product": product})
                entrypoints = tuple(
                    SimpleNamespace(
                        name=name, kind="command", resolved_deployment_unit=name
                    )
                    for name in (
                        ()
                        if product == "library"
                        else ("run", "inspect")
                        if product == "multi-entrypoint"
                        else ("run",)
                    )
                )
                locked_entrypoints = tuple(
                    SimpleNamespace(
                        deployment_unit=item.name,
                        entrypoint_identity=canonical_identity(item.name),
                    )
                    for item in entrypoints
                )
                lock = SimpleNamespace(
                    identity=canonical_identity({"lock": product}),
                    root_revision=revision,
                    nodes=(
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                identity=revision,
                                coordinate=SimpleNamespace(
                                    uri="component://fixture/product"
                                ),
                                definition=SimpleNamespace(entrypoints=entrypoints),
                            )
                        ),
                    ),
                )
                snapshot = SimpleNamespace(
                    authority=SimpleNamespace(lock=lock), require_unchanged=lambda: None
                )
                prepared = PreparedLockedGeneration(
                    component,
                    project_root,
                    None,
                    None,
                    (),
                    None,
                    (),
                    None,
                    canonical_identity("catalog"),
                    snapshot,
                )
                media = "application/octet-stream"
                artifact_export = ArtifactExport(
                    "product",
                    revision,
                    "library" if product == "library" else "executable",
                    canonical_identity("abi"),
                    canonical_identity("target"),
                    media,
                    canonical_identity("producer"),
                    canonical_identity("tree"),
                    canonical_identity("toolchain"),
                    canonical_identity("grant"),
                    (),
                    BlobRef("b" * 64, 1, media_type=media),
                )
                surface = LibraryImportSurface(
                    "python",
                    "sample_library",
                    (
                        LibraryCapabilityImport(
                            "sample.logic",
                            canonical_identity("interface"),
                            "sample_library.logic",
                            ("calculate",),
                        ),
                    ),
                )
                contract = SimpleNamespace(
                    is_library=product == "library",
                    library_import_surface=surface if product == "library" else None,
                    entrypoint_command_contracts=lambda entries=locked_entrypoints: (
                        entries
                    ),
                )
                artifact = root / "artifact"
                artifact.mkdir()
                command = SimpleNamespace(
                    argv=("fixture-python", "product.py"),
                    cwd=artifact,
                    environment=(("PRODUCT", "fixture"),),
                    to_dict=lambda artifact=artifact: {
                        "argv": ["fixture-python", "product.py"],
                        "cwd": str(artifact),
                        "environment": {"PRODUCT": "fixture"},
                    },
                )
                ports = Mock()
                # This controller runs Component commands itself.
                ports.command_phases = tuple(ComponentCommandPhase)
                ports.contracts = {revision.uri: contract}
                ports.artifact_path.return_value = artifact
                if product == "library":
                    ports.execution_command.side_effect = ValueError(
                        "a library has no executable entrypoint"
                    )
                else:
                    ports.execution_command.return_value = command
                execution_plan = SimpleNamespace(
                    identity=canonical_identity("execution-plan"),
                    action_plans=(
                        SimpleNamespace(
                            phase=SimpleNamespace(value="build"),
                            layers=(
                                SimpleNamespace(
                                    index=0, component_revisions=(revision,)
                                ),
                            ),
                            dependency_edges=(),
                        ),
                    ),
                )
                planned = PlannedStandardProject(
                    SimpleNamespace(name="fixture"), execution_plan
                )
                lifecycle = SimpleNamespace(
                    successful=True,
                    identity=canonical_identity("accepted-lifecycle"),
                    node_results=(
                        SimpleNamespace(
                            component_revision=revision,
                            exports=(artifact_export,),
                            disposition=SimpleNamespace(value="generated"),
                        ),
                    ),
                    project_build_plan=SimpleNamespace(
                        components=(SimpleNamespace(component_revision=revision),)
                    ),
                )
                executed = SimpleNamespace(
                    lifecycle=lifecycle, planned=planned, local_build_cache_report={}
                )
                closure = SimpleNamespace(
                    record=SimpleNamespace(
                        identity=canonical_identity("closure"),
                        toolchain_identities=(),
                    )
                )
                runtime = FilesystemStandardProjectRuntime(
                    None,
                    None,
                    None,
                    ports,
                    None,
                    True,
                    True,
                    closure,
                    object(),
                    object(),
                )
                configuration, _ = _resolved_source_cache(project, root / "cache")
                adapter = FilesystemStandardRebuildAdapter(
                    project=project,
                    binding=binding,
                    runtime=runtime,
                    source_cache_configuration=configuration,
                    authority_validator=lambda _root: canonical_identity(
                        "project-authority"
                    ),
                )

                def project_receipt(
                    _lifecycle,
                    *,
                    project=project,
                    revision=revision,
                    lifecycle=lifecycle,
                    **context,
                ):
                    receipt_policy = project.definition.test_receipt_policy
                    evidence = {
                        kind: canonical_identity(kind)
                        for kind in receipt_policy.required_evidence_kinds
                    }
                    evidence.update(
                        {
                            "lifecycle-command": context[
                                "lifecycle_invocation_identity"
                            ],
                            "lifecycle-request": context["lifecycle_request_identity"],
                            "test-runner": receipt_policy.runner_identity,
                        }
                    )
                    return ProjectTestReceipt(
                        project_id=project.definition.project_id,
                        project_revision_identity=context["project_revision_identity"],
                        subject_identity=revision,
                        suite=VersionedContentRef(
                            "test-suite",
                            receipt_policy.suite_id,
                            receipt_policy.suite_version,
                            canonical_identity("suite"),
                        ),
                        outcome="passed",
                        summary=ProjectTestSummary(8, 8, 0, 0),
                        result_identity=lifecycle.identity,
                        evidence=tuple(
                            ProjectTestEvidence(kind, identity)
                            for kind, identity in sorted(evidence.items())
                        ),
                    )

                candidate = root / "candidate.json"
                with (
                    patch(
                        "literate_ai.cli.rebuild.resolve_standard_project_lifecycle_driver",
                        return_value=binding,
                    ),
                    patch(
                        "literate_ai.cli.rebuild.FilesystemLockedGenerationApplicationAdapter.prepare",
                        return_value=prepared,
                    ),
                    patch(
                        "literate_ai.cli.rebuild.assemble_filesystem_standard_rebuild_adapter",
                        return_value=adapter,
                    ),
                    patch(
                        "literate_ai.cli.rebuild._component_acceptance_oracle",
                        return_value=None,
                    ),
                    patch.object(
                        FilesystemStandardProjectRuntime, "plan", return_value=planned
                    ),
                    patch.object(
                        FilesystemStandardProjectRuntime,
                        "production_readiness",
                        return_value=StandardProjectRuntimeReadiness(True, ()),
                    ),
                    patch.object(
                        FilesystemStandardProjectRuntime,
                        "execute",
                        return_value=executed,
                    ),
                    patch(
                        "literate_ai.adapters.standard_rebuild.project_standard_project_test_receipt",
                        side_effect=project_receipt,
                    ),
                ):
                    status, envelope = invoke(
                        "rebuild",
                        str(component),
                        "--project",
                        str(project_root),
                        "--runtime-root",
                        str(root / "runtime"),
                        "--candidate-receipt",
                        str(candidate),
                        "--update-receipt",
                        "--allow-host-execution",
                    )
                    if product == "library":
                        detail = "Traceback frame\n" * 80 + "missing native session"
                        with patch.object(
                            FilesystemStandardProjectRuntime,
                            "execute",
                            side_effect=LocalStandardLifecycleError(detail),
                        ):
                            failed_status, failed_envelope = invoke(
                                "rebuild",
                                str(component),
                                "--project",
                                str(project_root),
                                "--runtime-root",
                                str(root / "runtime"),
                                "--allow-host-execution",
                            )
                        self.assertEqual(failed_status, 2, failed_envelope)
                        self.assertEqual(
                            failed_envelope["error"]["code"],
                            "standard_rebuild.independent_acceptance_failed",
                        )
                        self.assertIn(
                            "missing native session",
                            failed_envelope["error"]["message"],
                        )
                        self.assertLessEqual(
                            len(failed_envelope["error"]["message"]), 8192
                        )
                self.assertEqual(status, 0, envelope)
                result = envelope["result"]
                self.assertTrue(result["passed"])
                self.assertTrue(result["receipt_committed"])
                tracked = project_root / project.definition.test_receipt
                self.assertEqual(tracked.read_bytes(), candidate.read_bytes())
                finalized = ProjectTestReceiptFinalizedCandidate.from_dict(
                    json.loads(candidate.read_bytes())
                )
                self.assertEqual(
                    result["receipt_identity"], finalized.receipt.identity.uri
                )
                self.assertEqual(
                    result["finalized_candidate_identity"], finalized.identity.uri
                )
                self.assertEqual(
                    result["receipt_update"]["identity"], result["receipt_identity"]
                )
                self.assertEqual(finalized.receipt.outcome, "passed")
                self.assertEqual(
                    result["test_summary"], finalized.receipt.summary.to_dict()
                )
                if product == "library":
                    ports.execution_command.assert_not_called()
                    self.assertNotIn("execution_command", result)
                    self.assertNotIn("execution_entrypoints", result)
                    self.assertEqual(
                        result["library_artifact"]["artifact_export"],
                        artifact_export.to_dict(),
                    )
                    self.assertEqual(
                        result["library_artifact"]["import_surface"], surface.to_dict()
                    )
                else:
                    self.assertNotIn("library_artifact", result)
                    self.assertEqual(result["execution_command"], command.to_dict())
                    if product == "multi-entrypoint":
                        self.assertEqual(
                            result["execution_entrypoints"],
                            [
                                {
                                    "schema": (
                                        "literate-ai/artifact-entrypoint-command@1"
                                    ),
                                    "name": name,
                                    "kind": "command",
                                    "deployment_unit": name,
                                    "argv": list(command.argv),
                                    "environment": dict(command.environment),
                                }
                                for name in ("run", "inspect")
                            ],
                        )
                        self.assertEqual(ports.execution_command.call_count, 3)
                        self.assertEqual(
                            [
                                call.kwargs["entrypoint_identity"]
                                for call in ports.execution_command.call_args_list[1:]
                            ],
                            [item.entrypoint_identity for item in locked_entrypoints],
                        )
                    else:
                        self.assertNotIn("execution_entrypoints", result)
                        ports.execution_command.assert_called_once()

    def test_rebuild_runs_authorized_argv_and_leaves_external_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self._project(root)
            runtime = root / "runtime"
            candidate = root / "candidate.json"
            revision = canonical_identity({"fixture": "project-revision"})

            with patch(
                "literate_ai.cli.rebuild.validated_project_authority_identity",
                return_value=revision,
            ):
                status, envelope = invoke(
                    "rebuild",
                    ".",
                    "--project",
                    str(project),
                    "--runtime-root",
                    str(runtime),
                    "--candidate-receipt",
                    str(candidate),
                    "--allow-host-execution",
                )

            self.assertEqual(status, 0, envelope)
            result = envelope["result"]
            assert isinstance(result, dict)
            self.assertTrue(result["passed"])
            self.assertEqual(len(result["component_lock_identities"]), 2)
            self.assertNotEqual(
                result["project_revision_identity"],
                result["validated_project_authority_identity"],
            )
            self.assertFalse(result["receipt_committed"])
            self.assertEqual(
                Path(str(result["candidate_receipt"])), candidate.resolve()
            )
            self.assertTrue(candidate.is_file())
            provisional = runtime / ".litai" / "project-test-receipt.provisional.json"
            self.assertFalse(provisional.exists())
            self.assertIn("provisional_receipt_identity", result)
            self.assertTrue(runtime.is_dir())
            self.assertFalse((project / "verification" / "current.json").exists())
            receipt = json.loads(candidate.read_text(encoding="utf-8"))
            self.assertEqual(
                receipt["receipt"]["evidence"]["lifecycle-request"],
                result["lifecycle_request_identity"],
            )
            self.assertEqual(
                receipt["receipt"]["evidence"]["lifecycle-command"],
                result["lifecycle_command_identity"],
            )
            self.assertEqual(receipt["finalization_boundary"], "supported-api-tcb")
            self.assertEqual(result["source_cache_mode"], "read-write")
            self.assertTrue(result["source_cache_fresh_generation_required"])
            self.assertEqual(result["source_cache_published_entry_identities"], [])
            protocol_root = runtime / ".litai"
            self.assertTrue((protocol_root / "source-cache-derivations.json").is_file())
            self.assertTrue((protocol_root / "source-cache-control.json").is_file())
            self.assertTrue((protocol_root / "source-cache-decision.json").is_file())
            control = read_rebuild_source_cache_control(
                (protocol_root / "source-cache-control.json").resolve()
            )
            self.assertEqual(
                tuple((item.reference, item.path) for item in control.operator_roots),
                (
                    (
                        "standard-local-source-cache",
                        str(runtime.resolve() / "source-cache"),
                    ),
                ),
            )

    def test_rebuild_fails_closed_without_ack_or_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self._project(root)
            status, envelope = invoke(
                "rebuild",
                ".",
                "--project",
                str(project),
                "--runtime-root",
                str(root / "runtime"),
                "--candidate-receipt",
                str(root / "candidate.json"),
            )
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"],
                "rebuild.host_execution_not_acknowledged",
            )

            project = self._project(root / "without-driver", include_driver=False)
            status, envelope = invoke(
                "rebuild",
                ".",
                "--project",
                str(project),
                "--runtime-root",
                str(root / "runtime-2"),
                "--candidate-receipt",
                str(root / "candidate-2.json"),
                "--allow-host-execution",
            )
            self.assertEqual(status, 2)
            self.assertEqual(envelope["error"]["code"], "rebuild.driver_unconfigured")

    def test_rebuild_fails_closed_on_tampered_derivation_plan_or_driver_drift(self):
        cases = (
            ("unsupported", "rebuild.derivation_planning_failed"),
            ("tamper-request", "rebuild.derivation_manifest_mismatch"),
            ("tamper-driver", "rebuild.derivation_manifest_mismatch"),
            ("tamper-plan", "rebuild.derivation_planning_failed"),
            ("driver-drift", "rebuild.driver_implementation_mismatch"),
        )
        for behavior, expected_code in cases:
            with (
                self.subTest(behavior=behavior),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                if behavior == "driver-drift":
                    project = self._project(root)
                    # The pinned driver changes after authorization.
                    (project / "driver.py").write_text(
                        DRIVER_SOURCE + "\n# changed after authorization\n",
                        encoding="utf-8",
                    )
                else:
                    project = self._project(root, planning_behavior=behavior)
                revision = canonical_identity({"fixture": "project-revision"})
                candidate = root / "candidate.json"
                with patch(
                    "literate_ai.cli.rebuild.validated_project_authority_identity",
                    return_value=revision,
                ):
                    status, envelope = invoke(
                        "rebuild",
                        ".",
                        "--project",
                        str(project),
                        "--runtime-root",
                        str(root / "runtime"),
                        "--candidate-receipt",
                        str(candidate),
                        "--allow-host-execution",
                    )
                self.assertEqual(status, 2, envelope)
                self.assertEqual(envelope["error"]["code"], expected_code)
                self.assertFalse(candidate.exists())


if __name__ == "__main__":
    unittest.main()
