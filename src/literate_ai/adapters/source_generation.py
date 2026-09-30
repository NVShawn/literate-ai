"""Production coding-CLI adapter that stops at an immutable source candidate."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    path_is_link_or_reparse,
)
from literate_ai.adapters.cache import CachedCodingCliSourceGenerator
from literate_ai.adapters.dependencies import validate_cyclonedx_bom
from literate_ai.adapters.intelligence import generated_source_tree_identity
from literate_ai.adapters.models.coding_cli import (
    CodingCliGeneration,
    GenerationRecipe,
    _acceptance_argument_vectors,
    _acceptance_result_shape,
    _require_recipe_authority_sbom,
)
from literate_ai.adapters.models.generated_source_validation import (
    validate_cpp_bazel_rule_attributes,
    validate_javascript_generation_handoff,
    validate_make_language_tool_quoting,
    validate_rust_bazel_source_closure,
)
from literate_ai.application.component_generation_preparation import (
    PreparedComponentGenerationNode,
)
from literate_ai.application.models import GenerationExecutionPlan
from literate_ai.application.standard_project_lifecycle import (
    StandardSourceCacheMembership,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    AuthoredBinaryAsset,
    ComponentGenerationRuntimeObservation,
    ContentIdentity,
    CycloneDxLifecycle,
    GeneratedSourceCandidate,
    SourceDerivationCacheKey,
    SourceGenerationProvenance,
    SourceGenerationRunOutput,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.executable_components import (
    GENERATED_SOURCE_TREE_RECORD_SCHEMA,
)
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    validate_generated_test_suite,
)
from literate_ai.storage import FileSystemCAS, StorageError

from .retained_source import RetainedSourceInput

_MANIFEST_RECORD_SCHEMA = "literate-ai/generated-source-manifest-record@1"
_STAGE_OUTPUT_RECORD_SCHEMA = "literate-ai/coding-cli-stage-output-record@1"


class CachedCodingCliSourceGenerationError(RuntimeError):
    """The source-only coding-CLI invocation or its evidence was inconsistent."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CodingCliSourceGenerationInvocation:
    """Exact legacy execution inputs absent from Component-node preparation."""

    execution_plan: GenerationExecutionPlan
    stage_request_json: bytes
    application_root_revision_identity: ContentIdentity
    readiness_identity: ContentIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.execution_plan, GenerationExecutionPlan):
            raise TypeError("execution_plan must be a GenerationExecutionPlan")
        if not isinstance(self.stage_request_json, bytes):
            raise TypeError("stage_request_json must be canonical UTF-8 JSON bytes")
        try:
            stage_request = json.loads(self.stage_request_json)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CachedCodingCliSourceGenerationError(
                "source_generation.stage_request_invalid",
                "stage request must be valid canonical UTF-8 JSON",
            ) from error
        if (
            not isinstance(stage_request, dict)
            or canonical_json_bytes(stage_request) != self.stage_request_json
        ):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.stage_request_noncanonical",
                "stage request must be a canonical JSON object",
            )
        for name in (
            "application_root_revision_identity",
            "readiness_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                raise TypeError(f"{name} must be a ContentIdentity")
        final_stage = self.execution_plan.model_stages[-1]
        if (
            stage_request.get("stage_id") != final_stage.stage_id
            or not final_stage.produces_tree
            or "input_identity" not in stage_request
            or not isinstance(stage_request.get("prior_stage_outputs"), dict)
        ):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.stage_request_not_final",
                "stage request must exactly select the final tree-producing stage",
            )

    @classmethod
    def create(
        cls,
        execution_plan: GenerationExecutionPlan,
        stage_request: Mapping[str, object],
        *,
        application_root_revision_identity: ContentIdentity,
        readiness_identity: ContentIdentity,
    ) -> CodingCliSourceGenerationInvocation:
        return cls(
            execution_plan,
            canonical_json_bytes(dict(stage_request)),
            application_root_revision_identity,
            readiness_identity,
        )

    @property
    def stage_request(self) -> dict[str, object]:
        value = json.loads(self.stage_request_json)
        assert isinstance(value, dict)
        return value

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.identity_document())

    def identity_document(self) -> dict[str, object]:
        """The unchanged invocation commitment, available for retained evidence."""

        return {
            "execution_plan_identity": self.execution_plan.identity.to_dict(),
            "stage_request": self.stage_request,
            "application_root_revision_identity": (
                self.application_root_revision_identity.to_dict()
            ),
            "readiness_identity": self.readiness_identity.to_dict(),
        }


SourceGenerationInvocationProvider = Callable[
    [PreparedComponentGenerationNode[Any, Any]],
    CodingCliSourceGenerationInvocation,
]


def _content_identity(identity: str, *, code: str, label: str) -> ContentIdentity:
    try:
        return ContentIdentity.parse_uri(identity)
    except (TypeError, ValueError) as error:
        raise CachedCodingCliSourceGenerationError(
            code, f"{label} is not a canonical content identity"
        ) from error


def _media_type(path: str) -> str:
    if path.endswith(".json"):
        return "application/json"
    if path.endswith((".yaml", ".yml")):
        return "application/yaml"
    if path.endswith((".js", ".mjs", ".cjs")):
        return "application/javascript"
    return "text/plain; charset=utf-8"


def _materialize_authored_asset(
    workspace: Path,
    asset: AuthoredBinaryAsset,
    content: bytes,
) -> None:
    """Project one CAS-verified authored asset into the fresh source workspace."""

    target = workspace.joinpath(*PurePosixPath(asset.path).parts)
    try:
        ensure_safe_directory(target.parent)
    except UnsafeFilesystemPathError as error:
        raise CachedCodingCliSourceGenerationError(
            "source_generation.asset_workspace_unsafe",
            f"authored asset parent is unsafe: {asset.path}",
        ) from error
    if target.exists() or path_is_link_or_reparse(target):
        raise CachedCodingCliSourceGenerationError(
            "source_generation.asset_path_collision",
            f"authored asset collides with generated workspace content: {asset.path}",
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as error:
        raise CachedCodingCliSourceGenerationError(
            "source_generation.asset_path_collision",
            f"authored asset collides with generated workspace content: {asset.path}",
        ) from error
    except OSError as error:
        raise CachedCodingCliSourceGenerationError(
            "source_generation.asset_materialization_failed",
            f"authored asset could not be materialized: {asset.path}",
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class CachedCodingCliSourceGenerationRunner:
    """Translate one complete prepared node into immutable source-only evidence."""

    def __init__(
        self,
        generator: CachedCodingCliSourceGenerator,
        *,
        cas: FileSystemCAS,
        invocation_provider: SourceGenerationInvocationProvider,
        assets: tuple[AuthoredBinaryAsset, ...] = (),
        retained_source: RetainedSourceInput | None = None,
    ) -> None:
        if not isinstance(generator, CachedCodingCliSourceGenerator):
            raise TypeError("generator must be a CachedCodingCliSourceGenerator")
        if not isinstance(cas, FileSystemCAS):
            raise TypeError("cas must be a FileSystemCAS")
        if not callable(invocation_provider):
            raise TypeError("invocation_provider must be callable")
        delegate = generator.delegate
        if (
            delegate.source_intelligence_provider is not None
            or delegate.source_intelligence_mode != "off"
        ):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.indexing_enabled",
                "source-only generation requires coding-CLI source indexing to be off",
            )
        self.generator = generator
        self.retained_source = retained_source
        self.cas = cas
        self.invocation_provider = invocation_provider
        self.assets_by_revision: dict[str, tuple[AuthoredBinaryAsset, ...]] = {}
        for asset in assets:
            if not isinstance(asset, AuthoredBinaryAsset):
                raise TypeError("assets must contain AuthoredBinaryAsset values")
            key = asset.component_revision.uri
            self.assets_by_revision[key] = (
                *self.assets_by_revision.get(key, ()),
                asset,
            )
        self._pending_candidates: dict[
            str, tuple[GeneratedSourceCandidate, CodingCliGeneration]
        ] = {}
        self._candidate_cache_keys: dict[str, SourceDerivationCacheKey] = {}

    def planned_cache_key(
        self,
        prepared: PreparedComponentGenerationNode[Any, Any],
    ) -> SourceDerivationCacheKey:
        """Derive the exact durable lookup key without invoking the coding agent."""

        if not isinstance(prepared, PreparedComponentGenerationNode):
            raise TypeError("prepared must be a PreparedComponentGenerationNode")
        if not isinstance(prepared.recipe, GenerationRecipe):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.recipe_invalid",
                "prepared node recipe must be a GenerationRecipe",
            )
        invocation = self.invocation_provider(prepared)
        if not isinstance(invocation, CodingCliSourceGenerationInvocation):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.invocation_invalid",
                "invocation provider must return a typed exact invocation",
            )
        self._require_invocation_matches_node(prepared, invocation)
        bounded_prompt = self._bounded_prompt(prepared)
        key = self.generator.derivation_cache_key(
            prepared.recipe,
            execution_plan=invocation.execution_plan,
            stage_request=invocation.stage_request,
            bounded_prompt=bounded_prompt,
        )
        if self.retained_source is not None:
            self.retained_source.require_unchanged()
            if (
                prepared.recipe.component_lock_identity
                != self.retained_source.component_lock_identity
            ):
                raise CachedCodingCliSourceGenerationError(
                    "retained_source.lock_mismatch",
                    "Retained input does not bind this Component lock",
                )
            # Separate both invocation and reusable lookup from model generation.
            key = replace(
                key,
                request_identity=self.retained_source.identity,
                source_semantics_identity=self.retained_source.identity,
            )
        return key

    def cache_key_for_candidate(
        self, candidate: GeneratedSourceCandidate
    ) -> SourceDerivationCacheKey:
        """Return the exact key captured for this candidate, generated or restored."""

        if not isinstance(candidate, GeneratedSourceCandidate):
            raise TypeError("candidate must be a GeneratedSourceCandidate")
        if self.retained_source is not None:
            self.retained_source.require_unchanged()
        try:
            return self._candidate_cache_keys[candidate.identity.uri]
        except KeyError as exc:
            raise CachedCodingCliSourceGenerationError(
                "source_generation.cache_key_unavailable",
                "candidate has no exact captured derivation cache key",
            ) from exc

    def record_restored_cache_key(
        self,
        candidate: GeneratedSourceCandidate,
        cache_key: SourceDerivationCacheKey,
    ) -> None:
        """Record the key a source-cache restore used, for a later re-publish call.

        A candidate restored from an earlier, separate process's accepted
        source-cache entry never passes through ``__call__`` in this process, so
        it has no entry captured for it. The restorer already knows the exact
        derivation key it used to look the entry up; this lets it hand that key
        back so a subsequent revalidation/publish step in this process can find
        it, instead of treating every cross-process cache hit as unavailable.
        """

        if not isinstance(candidate, GeneratedSourceCandidate):
            raise TypeError("candidate must be a GeneratedSourceCandidate")
        if not isinstance(cache_key, SourceDerivationCacheKey):
            raise TypeError("cache_key must be a SourceDerivationCacheKey")
        self._candidate_cache_keys[candidate.identity.uri] = cache_key

    def __call__(
        self,
        prepared: PreparedComponentGenerationNode[Any, Any],
        /,
    ) -> SourceGenerationRunOutput:
        if not isinstance(prepared, PreparedComponentGenerationNode):
            raise TypeError("prepared must be a PreparedComponentGenerationNode")
        if not isinstance(prepared.recipe, GenerationRecipe):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.recipe_invalid",
                "prepared node recipe must be a GenerationRecipe",
            )
        self._require_fresh_workspace(prepared)
        invocation = self.invocation_provider(prepared)
        if not isinstance(invocation, CodingCliSourceGenerationInvocation):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.invocation_invalid",
                "invocation provider must return a typed exact invocation",
            )
        self._require_invocation_matches_node(prepared, invocation)
        if self.retained_source is not None:
            key = self.planned_cache_key(prepared)
            if self.assets_by_revision:
                raise CachedCodingCliSourceGenerationError(
                    "retained_source.assets_unsupported",
                    "Retained input with separately materialized assets "
                    "is not supported",
                )
            output = self._record(
                prepared,
                invocation,
                None,
                planned_request_identity=key.request_identity,
            )
            workspace = Path(prepared.workspace.locator)
            for name, content in self.retained_source.files:
                destination = workspace / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(content)
            self.retained_source.require_unchanged()
            self._candidate_cache_keys[output.candidate_identity.uri] = key
            return output
        stage_request = invocation.stage_request
        bounded_prompt = self._bounded_prompt(prepared)
        cache_key = self.generator.derivation_cache_key(
            prepared.recipe,
            execution_plan=invocation.execution_plan,
            stage_request=stage_request,
            bounded_prompt=bounded_prompt,
        )
        generation = self.generator.generate(
            prepared.recipe,
            output_root=Path(prepared.workspace.locator),
            execution_plan=invocation.execution_plan,
            stage_request=stage_request,
            bounded_prompt=bounded_prompt,
        )
        output = self._record(
            prepared,
            invocation,
            generation,
            planned_request_identity=cache_key.request_identity,
        )
        self._candidate_cache_keys[output.candidate_identity.uri] = cache_key
        self._pending_candidates[output.candidate_identity.uri] = (
            output.candidate,
            generation,
        )
        return output

    @staticmethod
    def _bounded_prompt(
        prepared: PreparedComponentGenerationNode[Any, Any],
    ) -> bytes:
        prompt = prepared.request.prompt
        expected = prepared.request.request.prompt_identity
        if (
            not isinstance(prompt, bytes)
            or not prompt
            or expected.uri != "sha256:" + hashlib.sha256(prompt).hexdigest()
        ):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.bounded_prompt_mismatch",
                "prepared Component prompt bytes differ from their exact identity",
            )
        return prompt

    @staticmethod
    def _require_fresh_workspace(
        prepared: PreparedComponentGenerationNode[Any, Any],
    ) -> None:
        configured = Path(prepared.workspace.locator)
        if not configured.is_absolute():
            raise CachedCodingCliSourceGenerationError(
                "source_generation.workspace_not_absolute",
                "filesystem generation workspace locator must be absolute",
            )
        try:
            resolved = configured.resolve(strict=True)
        except OSError as error:
            raise CachedCodingCliSourceGenerationError(
                "source_generation.workspace_missing",
                "filesystem generation workspace must already exist",
            ) from error
        if configured != resolved or path_is_link_or_reparse(configured):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.workspace_redirected",
                "filesystem generation workspace cannot use links or redirection",
            )
        if not resolved.is_dir():
            raise CachedCodingCliSourceGenerationError(
                "source_generation.workspace_not_directory",
                "filesystem generation workspace must be a directory",
            )
        if any(resolved.iterdir()):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.workspace_not_empty",
                "filesystem generation workspace must be fresh and empty",
            )
        expected_allocation = canonical_identity(
            {
                "schema": "literate-ai/component-workspace-allocation@1",
                "component_revision": prepared.plan.component_revision.uri,
                "generation_plan_identity": prepared.plan.identity.uri,
                "locator": str(resolved),
            }
        )
        if prepared.workspace.allocation_identity != expected_allocation:
            raise CachedCodingCliSourceGenerationError(
                "source_generation.workspace_allocation_mismatch",
                "filesystem workspace differs from its exact node allocation",
            )

    def publish_accepted_candidate(self, candidate: GeneratedSourceCandidate) -> None:
        """Publish a cache entry only after outer lifecycle acceptance."""

        if not isinstance(candidate, GeneratedSourceCandidate):
            raise TypeError("candidate must be a GeneratedSourceCandidate")
        pending = self._pending_candidates.get(candidate.identity.uri)
        if pending is None:
            raise CachedCodingCliSourceGenerationError(
                "source_generation.candidate_not_pending",
                "candidate has no exact pending coding-CLI generation",
            )
        expected, generation = pending
        if candidate != expected:
            raise CachedCodingCliSourceGenerationError(
                "source_generation.candidate_substituted",
                "candidate differs from the exact pending source candidate",
            )
        self.generator.accept(generation)
        del self._pending_candidates[candidate.identity.uri]

    def publish(self, membership: StandardSourceCacheMembership) -> ContentIdentity:
        """Publish one exact Standard membership after its node accepted."""

        if not isinstance(membership, StandardSourceCacheMembership):
            raise TypeError("membership must be a StandardSourceCacheMembership")
        self.publish_accepted_candidate(membership.generation.output.candidate)
        return membership.identity

    @staticmethod
    def _require_invocation_matches_node(
        prepared: PreparedComponentGenerationNode[Any, Any],
        invocation: CodingCliSourceGenerationInvocation,
    ) -> None:
        definition = prepared.definition
        workflow = getattr(definition, "workflow_definition", None)
        routing = getattr(definition, "routing_policy", None)
        generation_key = prepared.plan.generation_key
        if (
            workflow is None
            or routing is None
            or invocation.execution_plan.workflow_reference != workflow
            or invocation.execution_plan.routing_reference != routing
            or generation_key.workflow_identity != workflow.identity
            or generation_key.routing_identity != routing.identity
        ):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.execution_plan_authority_mismatch",
                "legacy execution plan does not bind the prepared definition authority",
            )

    def _record(
        self,
        prepared: PreparedComponentGenerationNode[Any, Any],
        invocation: CodingCliSourceGenerationInvocation,
        generation: CodingCliGeneration | None,
        *,
        planned_request_identity: ContentIdentity,
    ) -> SourceGenerationRunOutput:
        recipe = prepared.recipe
        stage_request = invocation.stage_request
        final_stage = invocation.execution_plan.model_stages[-1]
        final_route = invocation.execution_plan.route_decisions[-1]
        retained = self.retained_source
        if generation is not None and (
            generation.recipe_identity != recipe.identity
            or generation.execution_plan_identity
            != invocation.execution_plan.identity.uri
            or generation.request_identity != planned_request_identity.uri
            or generation.requested_model_stages != (final_stage.stage_id,)
            or generation.requested_route_decision_digests != (final_route.digest,)
            or generation.source_intelligence is not None
            or generation.source_intelligence_status != "off"
        ):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.delegate_evidence_mismatch",
                "cached coding-CLI result does not bind the exact source invocation",
            )
        files = (
            dict((name, content.decode("utf-8")) for name, content in retained.files)
            if retained is not None
            else generation.files
        )
        if retained is not None:
            missing = set(recipe.all_required_entrypoints) - files.keys()
            if missing:
                raise CachedCodingCliSourceGenerationError(
                    "retained_source.entrypoint_missing",
                    "Retained tree lacks a required entrypoint",
                )
            validate_cpp_bazel_rule_attributes(files)
            validate_javascript_generation_handoff(files)
            validate_make_language_tool_quoting(files)
            validate_rust_bazel_source_closure(files)
        suite_content = files.get(GENERATED_TEST_SUITE_PATH)
        sbom_content = files.get(CYCLONEDX_SOURCE_SBOM_PATH)
        if suite_content is None or sbom_content is None:
            raise CachedCodingCliSourceGenerationError(
                "source_generation.required_artifact_missing",
                "generated source must contain its test manifest and source SBOM",
            )
        suite = validate_generated_test_suite(
            suite_content,
            recipe_identity=recipe.identity,
            specification_references=recipe.non_acceptance_document_paths,
            acceptance_arguments=_acceptance_argument_vectors(recipe),
            result_shape=_acceptance_result_shape(recipe),
        )
        if recipe.managed_sbom_graph is None:
            raise CachedCodingCliSourceGenerationError(
                "source_generation.managed_graph_missing",
                "source generation requires the exact managed dependency graph",
            )
        sbom = validate_cyclonedx_bom(
            sbom_content.encode("utf-8"),
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=recipe.managed_sbom_graph,
        )
        _require_recipe_authority_sbom(sbom_content.encode("utf-8"), recipe)
        if generation is not None and (
            generation.generated_test_suite_identity != suite.content_identity
            or generation.source_sbom != sbom
        ):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.required_artifact_evidence_mismatch",
                "delegate evidence differs from revalidated test or SBOM artifacts",
            )
        encoded = {
            path: content.encode("utf-8") for path, content in sorted(files.items())
        }
        assets = tuple(
            sorted(
                self.assets_by_revision.get(prepared.plan.component_revision.uri, ()),
                key=lambda item: item.path,
            )
        )
        if tuple(sorted(item.identity.uri for item in assets)) != tuple(
            item.uri for item in prepared.plan.generation_key.asset_identities
        ):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.asset_authority_mismatch",
                "materialized assets differ from the exact Component generation key",
            )
        for asset in assets:
            relative = PurePosixPath(asset.path)
            if relative.parts[0] in {".codegraph", ".source-intelligence"} or (
                asset.path
                in {
                    ".literate-tree.json",
                    ".literate-source-index.json",
                    ".literate-source-intelligence.json",
                }
            ):
                raise CachedCodingCliSourceGenerationError(
                    "source_generation.asset_path_reserved",
                    f"authored asset uses reserved metadata path: {asset.path}",
                )
            if asset.path in encoded:
                raise CachedCodingCliSourceGenerationError(
                    "source_generation.asset_path_collision",
                    f"authored asset collides with model output: {asset.path}",
                )
        file_records = []
        assets_by_path = {item.path: item for item in assets}
        blobs_by_path = {
            path: self.cas.put_bytes(content, media_type=_media_type(path))
            for path, content in encoded.items()
        }
        asset_content_by_path: dict[str, bytes] = {}
        for path, asset in assets_by_path.items():
            try:
                asset_content_by_path[path] = self.cas.get_bytes(asset.blob)
            except StorageError as error:
                raise CachedCodingCliSourceGenerationError(
                    "source_generation.asset_blob_unavailable",
                    f"locked authored asset bytes are unavailable from candidate "
                    f"custody: {path}",
                ) from error
            blobs_by_path[path] = asset.blob
        workspace = Path(prepared.workspace.locator)
        for path, asset in assets_by_path.items():
            _materialize_authored_asset(
                workspace,
                asset,
                asset_content_by_path[path],
            )
        tree_manifest = [
            {
                "path": path,
                "size": blob.size,
                "digest": blob.identity,
            }
            for path, blob in sorted(blobs_by_path.items())
        ]
        tree_identity = canonical_identity(tree_manifest)
        if not assets and tree_identity.uri != generated_source_tree_identity(encoded):
            raise CachedCodingCliSourceGenerationError(
                "source_generation.tree_identity_invalid",
                "blob-record tree identity differs from generated-source identity",
            )
        for path, blob in sorted(blobs_by_path.items()):
            file_records.append({"path": path, "blob": blob.to_dict()})
        tree_record = self.cas.put_manifest(
            {
                "schema": GENERATED_SOURCE_TREE_RECORD_SCHEMA,
                "tree_identity": tree_identity.to_dict(),
                "files": file_records,
            },
            media_type="application/vnd.literate-ai.generated-source-tree+json",
        )
        route_identity = _content_identity(
            final_route.digest,
            code="source_generation.route_identity_invalid",
            label="final route decision",
        )
        stage_output_record = self.cas.put_manifest(
            retained.to_dict()
            if retained is not None
            else {
                "schema": _STAGE_OUTPUT_RECORD_SCHEMA,
                "execution_plan_identity": invocation.execution_plan.identity.to_dict(),
                "stage_request_identity": canonical_identity(stage_request).to_dict(),
                "planned_request_identity": planned_request_identity.to_dict(),
                "stage_id": final_stage.stage_id,
                "route_decision_identity": route_identity.to_dict(),
                "tree_identity": tree_identity.to_dict(),
                "tree_record": tree_record.to_dict(),
                "coding_cli": generation.coding_cli,
                "model": generation.model,
                "coding_cli_tool_binding_identity": (
                    generation.coding_cli_tool_binding_identity
                ),
                **(
                    {
                        "provider_evidence_identity": (
                            generation.provider_evidence_identity
                        )
                    }
                    if generation.provider_evidence_identity is not None
                    else {}
                ),
            },
            media_type=(
                "application/vnd.literate-ai.retained-source-input+json"
                if retained is not None
                else "application/vnd.literate-ai.coding-cli-stage-output+json"
            ),
        )
        stage_output_identity = _content_identity(
            stage_output_record.identity,
            code="source_generation.stage_output_identity_invalid",
            label="stored model-stage output",
        )
        for document, expected in (
            (invocation.identity_document(), invocation.identity.uri),
            (
                invocation.execution_plan.to_dict(),
                invocation.execution_plan.identity.uri,
            ),
            (stage_request, canonical_identity(stage_request).uri),
            (final_route.to_dict(), final_route.digest),
        ):
            if self.cas.put_manifest(document).identity != expected:
                raise CachedCodingCliSourceGenerationError(
                    "source_generation.authority_record_identity_invalid",
                    "stored generation authority differs from its existing identity",
                )
        request = prepared.request.request
        recipe_identity = _content_identity(
            recipe.identity,
            code="source_generation.recipe_identity_invalid",
            label="generation recipe",
        )
        manifest_record = self.cas.put_manifest(
            {
                "schema": _MANIFEST_RECORD_SCHEMA,
                "component_revision": prepared.plan.component_revision.to_dict(),
                "source_generation_request_identity": request.identity.to_dict(),
                "planned_coding_cli_request_identity": (
                    planned_request_identity.to_dict()
                ),
                "component_generation_plan_identity": prepared.plan.identity.to_dict(),
                "generation_key_identity": (
                    prepared.plan.generation_key.identity.to_dict()
                ),
                "context_manifest_identity": (
                    request.context_manifest_identity.to_dict()
                ),
                "prompt_identity": request.prompt_identity.to_dict(),
                "recipe_identity": recipe_identity.to_dict(),
                "workspace_allocation_identity": (
                    prepared.workspace.allocation_identity.to_dict()
                ),
                "invocation_identity": invocation.identity.to_dict(),
                "tree_identity": tree_identity.to_dict(),
                "tree_record": tree_record.to_dict(),
                "stage_output_record": stage_output_record.to_dict(),
                "generated_test_suite_identity": suite.content_identity,
                "source_bom": sbom.to_dict(),
            },
            media_type="application/vnd.literate-ai.generated-source-manifest+json",
        )
        candidate = GeneratedSourceCandidate(
            component_revision=prepared.plan.component_revision,
            source_generation_request_identity=request.identity,
            planned_coding_cli_request_identity=planned_request_identity,
            component_generation_plan_identity=prepared.plan.identity,
            generation_key_identity=prepared.plan.generation_key.identity,
            context_manifest_identity=request.context_manifest_identity,
            prompt_identity=request.prompt_identity,
            recipe_identity=recipe_identity,
            workspace_allocation_identity=prepared.workspace.allocation_identity,
            tree_identity=tree_identity,
            # The semantic path/size/content digest remains ``tree_identity``. The
            # bundle identity names the retrievable CAS manifest that closes over every
            # generated file BlobRef, so publication can fetch real bytes rather than
            # treating a semantic tree digest as a blob digest.
            source_bundle_identity=_content_identity(
                tree_record.identity,
                code="source_generation.bundle_identity_invalid",
                label="stored source bundle",
            ),
            source_manifest_identity=_content_identity(
                manifest_record.identity,
                code="source_generation.manifest_identity_invalid",
                label="stored source manifest",
            ),
            source_bom_identity=sbom.bom_identity,
            generated_test_suite_identity=_content_identity(
                suite.content_identity,
                code="source_generation.test_manifest_identity_invalid",
                label="generated test manifest",
            ),
        )
        provider_evidence = (
            ()
            if generation is None or generation.provider_evidence_identity is None
            else (
                _content_identity(
                    generation.provider_evidence_identity,
                    code="source_generation.provider_evidence_identity_invalid",
                    label="provider evidence",
                ),
            )
        )
        provenance = SourceGenerationProvenance(
            source_generation_request_identity=request.identity,
            planned_coding_cli_request_identity=planned_request_identity,
            component_lock_identity=recipe.component_lock_identity,
            application_root_revision_identity=(
                invocation.application_root_revision_identity
            ),
            generated_component_revision_identity=prepared.plan.component_revision,
            component_generation_plan_identity=prepared.plan.identity,
            generation_key_identity=prepared.plan.generation_key.identity,
            context_manifest_identity=request.context_manifest_identity,
            prompt_identity=request.prompt_identity,
            recipe_identity=recipe_identity,
            workspace_allocation_identity=prepared.workspace.allocation_identity,
            readiness_identity=invocation.readiness_identity,
            route_decision_identities=() if retained is not None else (route_identity,),
            model_stage_output_identities=()
            if retained is not None
            else (stage_output_identity,),
            candidate_identity=candidate.identity,
            provider_evidence_identities=provider_evidence,
            retained_source_identity=None if retained is None else retained.identity,
        )
        observation = ComponentGenerationRuntimeObservation(None, None, None, None)
        return SourceGenerationRunOutput(
            candidate,
            candidate.identity,
            provenance,
            provenance.identity,
            observation,
        )


__all__ = [
    "CachedCodingCliSourceGenerationError",
    "CachedCodingCliSourceGenerationRunner",
    "CodingCliSourceGenerationInvocation",
    "SourceGenerationInvocationProvider",
]
