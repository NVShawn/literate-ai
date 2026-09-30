"""Local immutable generated-source cache for fast iterative rebuilds.

This cache is deliberately non-authoritative.  A hit skips one coding-CLI call, then
the normal lifecycle re-indexes, validates, classifies, builds, tests, executes, and
accepts the materialized tree again.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

from literate_ai._cache_lock import (
    CacheLockError,
    cache_lock_path,
    exclusive_cache_lock,
)
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    path_is_link_or_reparse,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.dependencies import validate_cyclonedx_bom
from literate_ai.adapters.intelligence import generated_source_tree_identity
from literate_ai.adapters.models.coding_cli import (
    CodingCliGeneration,
    CodingCliSourceGenerator,
    GenerationRecipe,
    _acceptance_argument_vectors,
    _acceptance_result_shape,
    _coding_cli_environment,
    _planned_generation_prompt,
    _requested_route_digests,
    _requested_stage_ids,
)
from literate_ai.application.models import GenerationExecutionPlan
from literate_ai.cache_directories import (
    CacheDirectoryError,
    ensure_cache_directory,
    require_cache_directory_current,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    ContentIdentity,
    CycloneDxLifecycle,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
    canonical_identity,
    canonical_json_bytes,
    source_cache_model_selector,
)
from literate_ai.contracts.executable_components.context_journal import (
    ComponentContextBenchmarkRecord,
    ForwardGenerationContextCacheReport,
)
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    validate_generated_test_suite,
)

_SCHEMA = "literate-ai/local-generated-source-cache-entry@1"
_MANIFEST = ".litai-generated-source.json"
_PROMPT = ".litai-generation-prompt.md"
_NON_REUSABLE_STAGE_PLAN_FIELDS = frozenset(
    {
        # Whole-project review authority belongs to the exact coding transaction and
        # its provenance, but is broader than one target's source semantics.  The
        # target's Component revision, generation plan/key, recipe, context, direct
        # interfaces, and bounded prompt below already close over its effective
        # authority, including repository lineage through the Component lock.  Keeping
        # this aggregate here made an unrelated receipt-policy or sibling-catalog edit
        # invalidate accepted source and needlessly invoke the model again.
        "project_authority_identity",
        "source_generation_request_identity",
        "workspace_allocation_identity",
    }
)


def _accepted_source_semantics_identity(
    stage_request: Mapping[str, object],
    *,
    bounded_prompt: bytes | None,
) -> ContentIdentity:
    """Identify source-affecting stage input without session transaction custody."""

    semantic_request = json.loads(canonical_json_bytes(dict(stage_request)))
    if not isinstance(semantic_request, dict):
        raise GeneratedSourceCacheError(
            "accepted-source stage request must be a canonical object"
        )
    # This field is the digest of the unsanitized request and therefore carries the
    # transaction identities removed below. The sanitized object is hashed again.
    semantic_request.pop("input_identity", None)
    prior = semantic_request.get("prior_stage_outputs", {})
    if not isinstance(prior, dict):
        raise GeneratedSourceCacheError(
            "accepted-source stage outputs must be a canonical object"
        )
    plan = prior.get("plan")
    if isinstance(plan, dict):
        prior["plan"] = {
            key: value
            for key, value in plan.items()
            if key not in _NON_REUSABLE_STAGE_PLAN_FIELDS
        }
    semantic_request["prior_stage_outputs"] = prior
    prompt_identity = (
        None
        if bounded_prompt is None
        else "sha256:" + hashlib.sha256(bounded_prompt).hexdigest()
    )
    return canonical_identity(
        {
            "schema": "literate-ai/accepted-source-request-semantics@2",
            "stage_request": semantic_request,
            "bounded_prompt_identity": prompt_identity,
        }
    )


class GeneratedSourceCacheError(RuntimeError):
    """A local generated-source entry is malformed, stale, or unsafe."""


class CachedCodingCliSourceGenerator:
    """Cache exact coding-CLI results while preserving current lifecycle evidence."""

    def __init__(
        self,
        delegate: CodingCliSourceGenerator,
        *,
        cache_root: Path,
        project_root: Path,
        force_regeneration: bool = False,
    ) -> None:
        if not isinstance(delegate, CodingCliSourceGenerator):
            raise TypeError("generated-source cache requires CodingCliSourceGenerator")
        self.delegate = delegate
        self.selection = delegate.selection
        self.project_root = Path(project_root).resolve(strict=True)
        self.cache_root = ensure_cache_directory(
            cache_root,
            kind="generated-source",
            project_root=project_root,
            required_subdirectories=(Path("sources") / "sha256", Path("staging")),
        )
        self.entries = self.cache_root / "sources" / "sha256"
        if not isinstance(force_regeneration, bool):
            raise TypeError("force_regeneration must be boolean")
        self.force_regeneration = force_regeneration
        self.hits = 0
        self.misses = 0
        self.hit_seconds = 0.0
        self.generation_seconds = 0.0
        self._state_lock = threading.Lock()
        self._pending: dict[
            int, tuple[Path, SourceDerivationCacheKey, str, CodingCliGeneration]
        ] = {}

    def planned_request_identity(self, *args, **kwargs):
        return self.delegate.planned_request_identity(*args, **kwargs)

    def derivation_cache_key(
        self,
        recipe: GenerationRecipe,
        *,
        execution_plan: GenerationExecutionPlan,
        stage_request: Mapping[str, object],
        bounded_prompt: bytes | None = None,
    ) -> SourceDerivationCacheKey:
        """Return the exact predictable key used by this generator invocation."""

        key, _prompt = self._key(
            recipe, execution_plan, stage_request, bounded_prompt=bounded_prompt
        )
        return key

    def generate(
        self,
        recipe: GenerationRecipe,
        *,
        output_root: Path,
        execution_plan: GenerationExecutionPlan | None = None,
        stage_request: Mapping[str, object] | None = None,
        bounded_prompt: bytes | None = None,
    ) -> CodingCliGeneration:
        if execution_plan is None or stage_request is None:
            if bounded_prompt is not None:
                raise ValueError(
                    "a cached bounded prompt requires an exact execution plan and stage"
                )
            started = time.monotonic()
            generated = self.delegate.generate(
                recipe,
                output_root=output_root,
                execution_plan=execution_plan,
                stage_request=stage_request,
            )
            with self._state_lock:
                self.generation_seconds += time.monotonic() - started
                self.misses += 1
            return generated

        key, prompt = self._key(
            recipe, execution_plan, stage_request, bounded_prompt=bounded_prompt
        )
        entry = self.entries / key.identity.digest
        started = time.monotonic()
        cacheable = getattr(self.delegate, "cacheable", True)
        if entry.exists() and not self.force_regeneration and cacheable:
            generated = self._load(
                entry,
                key=key,
                prompt=prompt,
                recipe=recipe,
                output_root=output_root,
                execution_plan=execution_plan,
                stage_request=stage_request,
            )
            with self._state_lock:
                self.hits += 1
                self.hit_seconds += time.monotonic() - started
            return generated

        generated = self.delegate.generate(
            recipe,
            output_root=output_root,
            execution_plan=execution_plan,
            stage_request=stage_request,
            bounded_prompt=bounded_prompt,
        )
        # Forced work bypasses a valid local entry without mutating it.  A fresh
        # result may still seed an empty key after the outer lifecycle accepts it.
        with self._state_lock:
            if cacheable and not entry.exists():
                self._pending[id(generated)] = (entry, key, prompt, generated)
            self.misses += 1
            self.generation_seconds += time.monotonic() - started
        return generated

    def accept(self, generation: CodingCliGeneration) -> None:
        """Publish one fresh result only after the caller completed acceptance."""

        with self._state_lock:
            pending = self._pending.pop(id(generation), None)
        if pending is None:
            return
        entry, key, prompt, expected = pending
        if generation is not expected:
            raise GeneratedSourceCacheError("accepted generation object changed")
        self._publish(entry, key=key, prompt=prompt, generation=generation)

    def report(
        self,
        *,
        context_report: ForwardGenerationContextCacheReport | None = None,
        context_benchmarks: tuple[ComponentContextBenchmarkRecord, ...] = (),
        context_prompt_journal_identities: tuple[ContentIdentity, ...] = (),
    ) -> dict[str, object]:
        if context_report is None and (
            context_benchmarks or context_prompt_journal_identities
        ):
            raise GeneratedSourceCacheError(
                "context cache report is required with context projections"
            )
        if (
            context_report is not None
            and len(
                {
                    len(context_report.entries),
                    len(context_benchmarks),
                    len(context_prompt_journal_identities),
                }
            )
            != 1
        ):
            raise GeneratedSourceCacheError(
                "context cache projections must cover the same Component set"
            )
        with self._state_lock:
            return {
                "schema": "literate-ai/local-generated-source-cache-report@2",
                "root": str(self.cache_root),
                "hits": self.hits,
                "misses": self.misses,
                "hit_seconds": round(self.hit_seconds, 6),
                "generation_seconds": round(self.generation_seconds, 6),
                "component_context": (
                    None if context_report is None else context_report.to_dict()
                ),
                "context_benchmarks": [item.to_dict() for item in context_benchmarks],
                "context_prompt_journal_identities": [
                    item.to_dict() for item in context_prompt_journal_identities
                ],
            }

    def _key(
        self,
        recipe: GenerationRecipe,
        execution_plan: GenerationExecutionPlan,
        stage_request: Mapping[str, object],
        *,
        bounded_prompt: bytes | None = None,
    ) -> tuple[SourceDerivationCacheKey, str]:
        self.selection.require_unchanged()
        prompt = _planned_generation_prompt(
            recipe,
            execution_plan,
            stage_request,
            self.selection,
            bounded_prompt=bounded_prompt,
        )
        request_identity = canonical_identity(prompt)
        planned = self.delegate.planned_request_identity(
            recipe,
            execution_plan=execution_plan,
            stage_request=stage_request,
            bounded_prompt=bounded_prompt,
        )
        if planned != request_identity:
            raise GeneratedSourceCacheError(
                "coding-CLI planned request changed during cache-key construction"
            )
        key = SourceDerivationCacheKey(
            recipe_identity=ContentIdentity.parse_uri(recipe.identity),
            execution_plan_identity=execution_plan.identity,
            coding_cli_tool_binding_identity=ContentIdentity.parse_uri(
                self.selection.tool_binding_identity
            ),
            model_binding=SourceCacheModelBinding(
                self.selection.name,
                source_cache_model_selector(recipe.model_for(self.selection.name)),
            ),
            request_identity=request_identity,
            source_semantics_identity=_accepted_source_semantics_identity(
                stage_request,
                bounded_prompt=bounded_prompt,
            ),
        )
        return key, prompt

    def _publish(
        self,
        destination: Path,
        *,
        key: SourceDerivationCacheKey,
        prompt: str,
        generation: CodingCliGeneration,
    ) -> None:
        root_lock = cache_lock_path(
            self.project_root, self.cache_root, "lifecycle.lock"
        )
        try:
            with exclusive_cache_lock(root_lock):
                require_cache_directory_current(
                    self.cache_root,
                    kind="generated-source",
                    project_root=self.project_root,
                )
                self._publish_locked(
                    destination, key=key, prompt=prompt, generation=generation
                )
        except (CacheLockError, CacheDirectoryError) as exc:
            raise GeneratedSourceCacheError(
                "generated-source cache publication lock is unavailable"
            ) from exc

    def _publish_locked(
        self,
        destination: Path,
        *,
        key: SourceDerivationCacheKey,
        prompt: str,
        generation: CodingCliGeneration,
    ) -> None:
        files = {
            path: content.encode("utf-8") for path, content in generation.files.items()
        }
        source_tree_identity = generated_source_tree_identity(files)
        records = [
            {
                "path": path,
                "size": len(content),
                "identity": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
            for path, content in sorted(files.items())
        ]
        manifest = {
            "schema": _SCHEMA,
            "cache_key": key.to_dict(),
            "source_tree_identity": source_tree_identity,
            "files": records,
            "prompt_identity": canonical_identity(prompt).uri,
            "generation": {
                "generation_mode": generation.generation_mode,
                "generated_test_suite_identity": (
                    generation.generated_test_suite_identity
                ),
                "requested_model_stages": list(generation.requested_model_stages),
                "requested_route_decision_digests": list(
                    generation.requested_route_decision_digests
                ),
                "environment_keys": list(generation.environment_keys),
                "source_intelligence_status": (generation.source_intelligence_status),
                "source_intelligence_reason_code": (
                    generation.source_intelligence_reason_code
                ),
            },
        }
        try:
            ensure_safe_directory(self.cache_root / "staging")
            ensure_safe_directory(destination.parent)
        except UnsafeFilesystemPathError as exc:
            raise GeneratedSourceCacheError(
                "generated-source cache publication path is unsafe"
            ) from exc
        staging = Path(
            tempfile.mkdtemp(prefix="source-", dir=self.cache_root / "staging")
        )
        try:
            for path, content in sorted(files.items()):
                target = staging.joinpath(*path.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            (staging / _PROMPT).write_text(prompt, encoding="utf-8", newline="\n")
            (staging / _MANIFEST).write_bytes(canonical_json_bytes(manifest))
            self._verify_entry(staging, key=key, prompt=prompt)
            lock_path = cache_lock_path(
                self.project_root,
                self.cache_root,
                "generated-source",
                f"{destination.name}.lock",
            )
            try:
                with exclusive_cache_lock(lock_path):
                    if destination.exists() or path_is_link_or_reparse(destination):
                        self._verify_entry(destination, key=key, prompt=prompt)
                        shutil.rmtree(staging)
                    else:
                        os.replace(staging, destination)
            except CacheLockError as exc:
                raise GeneratedSourceCacheError(
                    "generated-source cache publication lock is unavailable"
                ) from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _load(
        self,
        entry: Path,
        *,
        key: SourceDerivationCacheKey,
        prompt: str,
        recipe: GenerationRecipe,
        output_root: Path,
        execution_plan: GenerationExecutionPlan,
        stage_request: Mapping[str, object],
    ) -> CodingCliGeneration:
        manifest, files = self._verify_entry(entry, key=key, prompt=prompt)
        supplied_root = Path(output_root)
        if path_is_link_or_reparse(supplied_root):
            raise GeneratedSourceCacheError(
                "generated-source cache output root cannot be a link or reparse point"
            )
        root = supplied_root.resolve()
        if path_is_link_or_reparse(root) or (
            root.exists() and (not root.is_dir() or any(root.iterdir()))
        ):
            raise GeneratedSourceCacheError(
                "generated-source cache output root must be new or empty"
            )
        root.mkdir(parents=True, exist_ok=True)
        for path, content in files.items():
            target = root.joinpath(*path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        suite = validate_generated_test_suite(
            files[GENERATED_TEST_SUITE_PATH].decode("utf-8"),
            recipe_identity=recipe.identity,
            specification_references=recipe.non_acceptance_document_paths,
            acceptance_arguments=_acceptance_argument_vectors(recipe),
            result_shape=_acceptance_result_shape(recipe),
        )
        source_sbom = validate_cyclonedx_bom(
            files[CYCLONEDX_SOURCE_SBOM_PATH],
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=recipe.managed_sbom_graph,
        )
        source_tree_identity = generated_source_tree_identity(files)
        provider = self.delegate.source_intelligence_provider
        source_intelligence = None
        status = self.delegate.source_intelligence_mode
        reason = None
        if provider is not None:
            source_intelligence = provider.index(
                root,
                files,
                source_tree_identity=source_tree_identity,
            )
            source_intelligence = provider.verify(root, files, source_intelligence)
            status = "current"
        elif status == "preferred":
            status = "unavailable"
            reason = "coding_cli.source_intelligence_provider_unsupported"
        elif status == "off":
            status = "off"

        generation = manifest["generation"]
        assert isinstance(generation, dict)
        expected_stages = _requested_stage_ids(execution_plan, stage_request)
        expected_routes = _requested_route_digests(execution_plan, stage_request)
        if (
            generation.get("generation_mode") != suite.generation_mode
            or generation.get("generated_test_suite_identity") != suite.content_identity
            or generation.get("requested_model_stages") != list(expected_stages)
            or generation.get("requested_route_decision_digests")
            != list(expected_routes)
        ):
            raise GeneratedSourceCacheError(
                "generated-source cache provenance differs from the current plan"
            )
        model = recipe.model_for(self.selection.name)
        command = tuple(self.delegate._command(root, model))
        process_environment = _coding_cli_environment(
            self.delegate.environment,
            self.selection,
            workspace=root,
        )
        self.selection.require_unchanged()
        return CodingCliGeneration(
            files={path: content.decode("utf-8") for path, content in files.items()},
            coding_cli=self.selection.name,
            executable=self.selection.executable,
            model=model,
            recipe_identity=recipe.identity,
            command=command,
            request_identity=key.request_identity.uri,
            execution_plan_identity=execution_plan.identity.uri,
            requested_model_stages=expected_stages,
            requested_route_decision_digests=expected_routes,
            executable_identity=self.selection.executable_identity,
            command_identity=canonical_identity(command).uri,
            coding_cli_selection_identity=self.selection.identity,
            isolation_profile=self.selection.isolation.profile,
            hermetic=self.selection.isolation.hermetic,
            environment_keys=tuple(sorted(process_environment)),
            generation_mode=suite.generation_mode,
            generated_test_suite_identity=suite.content_identity,
            source_intelligence=source_intelligence,
            coding_cli_tool_binding_identity=self.selection.tool_binding_identity,
            source_sbom=source_sbom,
            source_intelligence_status=status,
            source_intelligence_reason_code=reason,
        )

    def _verify_entry(
        self,
        entry: Path,
        *,
        key: SourceDerivationCacheKey,
        prompt: str,
    ) -> tuple[dict[str, object], dict[str, bytes]]:
        try:
            require_safe_directory(entry)
        except UnsafeFilesystemPathError as exc:
            raise GeneratedSourceCacheError(
                "generated-source cache entry is unsafe"
            ) from exc
        if path_is_link_or_reparse(entry) or not entry.is_dir():
            raise GeneratedSourceCacheError("generated-source cache entry is unsafe")
        manifest_path = entry / _MANIFEST
        prompt_path = entry / _PROMPT
        if any(path_is_link_or_reparse(path) for path in (manifest_path, prompt_path)):
            raise GeneratedSourceCacheError("generated-source cache metadata is unsafe")
        try:
            manifest_metadata = manifest_path.lstat()
            prompt_metadata = prompt_path.lstat()
            if not all(
                stat.S_ISREG(metadata.st_mode)
                for metadata in (manifest_metadata, prompt_metadata)
            ):
                raise GeneratedSourceCacheError(
                    "generated-source cache metadata is unsafe"
                )
            manifest_bytes = _read_unchanged_cache_file(
                manifest_path, manifest_metadata
            )
            prompt_bytes = _read_unchanged_cache_file(prompt_path, prompt_metadata)
            prompt_text = prompt_bytes.decode("utf-8")
            manifest = json.loads(manifest_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GeneratedSourceCacheError(
                "generated-source cache manifest is invalid"
            ) from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema") != _SCHEMA
            or SourceDerivationCacheKey.from_dict(manifest.get("cache_key")) != key
            or manifest.get("prompt_identity") != canonical_identity(prompt).uri
            or prompt_text != prompt
        ):
            raise GeneratedSourceCacheError(
                "generated-source cache entry does not match its derivation"
            )
        raw_records = manifest.get("files")
        if not isinstance(raw_records, list) or not raw_records:
            raise GeneratedSourceCacheError("generated-source cache has no files")
        expected: dict[str, tuple[int, str]] = {}
        for item in raw_records:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "size", "identity"}
                or not isinstance(item.get("path"), str)
                or not str(item["path"]).startswith("source/")
                or ".." in str(item["path"]).split("/")
                or type(item.get("size")) is not int
                or not isinstance(item.get("identity"), str)
            ):
                raise GeneratedSourceCacheError(
                    "generated-source cache file record is invalid"
                )
            path = str(item["path"])
            if path in expected:
                raise GeneratedSourceCacheError(
                    "generated-source cache repeats a file path"
                )
            expected[path] = (int(item["size"]), str(item["identity"]))
        files: dict[str, bytes] = {}
        actual_metadata = {_MANIFEST, _PROMPT}
        for path, metadata in _cache_entry_files(entry):
            relative = path.relative_to(entry).as_posix()
            if relative in actual_metadata:
                expected_metadata = (
                    manifest_bytes if relative == _MANIFEST else prompt_bytes
                )
                if _read_unchanged_cache_file(path, metadata) != expected_metadata:
                    raise GeneratedSourceCacheError(
                        "generated-source cache metadata changed"
                    )
                continue
            if relative not in expected:
                raise GeneratedSourceCacheError(
                    "generated-source cache contains an undeclared file"
                )
            content = _read_unchanged_cache_file(path, metadata)
            size, identity = expected[relative]
            if len(content) != size or (
                "sha256:" + hashlib.sha256(content).hexdigest() != identity
            ):
                raise GeneratedSourceCacheError(
                    "generated-source cache file bytes changed"
                )
            files[relative] = content
        if set(files) != set(expected):
            raise GeneratedSourceCacheError("generated-source cache is incomplete")
        if generated_source_tree_identity(files) != manifest.get(
            "source_tree_identity"
        ):
            raise GeneratedSourceCacheError(
                "generated-source cache tree identity changed"
            )
        return manifest, files


def _cache_entry_files(entry: Path) -> Iterator[tuple[Path, os.stat_result]]:
    def walk(directory: Path) -> Iterator[tuple[Path, os.stat_result]]:
        before = directory.lstat()
        if stat_is_link_or_reparse(before) or not stat.S_ISDIR(before.st_mode):
            raise GeneratedSourceCacheError(
                "generated-source cache contains a link or reparse point"
            )
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise GeneratedSourceCacheError(
                "generated-source cache tree is unavailable"
            ) from exc
        for path in children:
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise GeneratedSourceCacheError(
                    "generated-source cache changed while being inspected"
                ) from exc
            if stat_is_link_or_reparse(metadata):
                raise GeneratedSourceCacheError(
                    "generated-source cache contains a link or reparse point"
                )
            if stat.S_ISDIR(metadata.st_mode):
                yield from walk(path)
            elif stat.S_ISREG(metadata.st_mode):
                yield path, metadata
            else:
                raise GeneratedSourceCacheError(
                    "generated-source cache contains a special node"
                )
        after = directory.lstat()
        if stat_is_link_or_reparse(after) or _node_signature(before) != _node_signature(
            after
        ):
            raise GeneratedSourceCacheError(
                "generated-source cache changed while being inspected"
            )

    return walk(entry)


def _read_unchanged_cache_file(path: Path, before: os.stat_result) -> bytes:
    try:
        content = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise GeneratedSourceCacheError(
            "generated-source cache changed while being inspected"
        ) from exc
    if (
        stat_is_link_or_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or _node_signature(before) != _node_signature(after)
    ):
        raise GeneratedSourceCacheError(
            "generated-source cache changed while being inspected"
        )
    return content


def _node_signature(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


__all__ = ["CachedCodingCliSourceGenerator", "GeneratedSourceCacheError"]
