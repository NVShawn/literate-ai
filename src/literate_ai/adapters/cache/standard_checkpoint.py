"""Filesystem persistence for replay-safe Standard lifecycle checkpoints."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from literate_ai.application.component_generation_preparation import (
    PreparedComponentGenerationNode,
)
from literate_ai.application.standard_project_lifecycle import (
    rebind_standard_source_checkpoint_generation,
)
from literate_ai.contracts import (
    BlobRef,
    CachedSourceFile,
    ComponentExecutionPlan,
    ContentIdentity,
    SourceGenerationResumeCandidate,
    StandardLifecycleAttemptEvidence,
    StandardLifecycleCheckpoint,
    StandardLifecycleCheckpointOutcome,
    StandardLifecycleStage,
    StandardLifecycleStageEvidence,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.storage import FileSystemCAS


class StandardLifecycleCheckpointError(RuntimeError):
    """A checkpoint cannot be trusted, appended, or restored safely."""


_STAGE_INDEX = {stage: index for index, stage in enumerate(StandardLifecycleStage)}


class FilesystemStandardLifecycleCheckpointStore:
    """Append stage lineage and restore only source, never historical authority."""

    def __init__(
        self,
        root: Path,
        *,
        source_root: Callable[[ContentIdentity], Path],
        source_trees: object,
    ) -> None:
        if not isinstance(root, Path):
            raise TypeError("checkpoint root must be a Path")
        if not callable(source_root):
            raise TypeError("source_root must be callable")
        if not callable(getattr(source_trees, "register", None)):
            raise TypeError("source_trees must provide register")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise StandardLifecycleCheckpointError(
                "checkpoint root cannot be a symbolic link"
            )
        self.cas = FileSystemCAS(self.root / "objects")
        self.heads = self.root / "heads"
        self.heads.mkdir(exist_ok=True)
        self.attempt_heads = self.root / "attempt-heads"
        self.attempt_heads.mkdir(exist_ok=True)
        self.source_root = source_root
        self.source_trees = source_trees
        self._attempts: dict[str, StandardLifecycleAttemptEvidence] = {}
        self._source_files: dict[str, tuple[CachedSourceFile, ...]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(
        execution_plan_identity: ContentIdentity,
        component_revision: ContentIdentity,
    ) -> str:
        return canonical_identity(
            {
                "schema": "literate-ai/standard-lifecycle-checkpoint-head@1",
                "execution_plan_identity": execution_plan_identity.uri,
                "component_revision": component_revision.uri,
            }
        ).digest

    def _head_path(
        self,
        execution_plan_identity: ContentIdentity,
        component_revision: ContentIdentity,
    ) -> Path:
        key = self._key(execution_plan_identity, component_revision)
        return self.heads / f"{key}.json"

    def _read_head(
        self,
        execution_plan_identity: ContentIdentity,
        component_revision: ContentIdentity,
    ) -> StandardLifecycleCheckpoint | None:
        path = self._head_path(execution_plan_identity, component_revision)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise StandardLifecycleCheckpointError(
                "checkpoint head is not a regular file"
            )
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
            if canonical_json_bytes(value) != raw:
                raise ValueError("head is not canonical JSON")
            reference = BlobRef.from_dict(value, path="checkpoint_head")
            checkpoint_value = self.cas.get_manifest(reference)
            checkpoint = StandardLifecycleCheckpoint.from_dict(checkpoint_value)
        except Exception as exc:
            raise StandardLifecycleCheckpointError(
                "checkpoint head or immutable record is invalid"
            ) from exc
        if checkpoint.identity.digest != reference.digest:
            raise StandardLifecycleCheckpointError(
                "checkpoint record identity differs from its immutable blob"
            )
        evidence = checkpoint.evidence
        if (
            evidence.execution_plan_identity != execution_plan_identity
            or evidence.component_revision != component_revision
        ):
            raise StandardLifecycleCheckpointError(
                "checkpoint head resolves to another execution node"
            )
        return checkpoint

    def _attempt_head_path(
        self,
        execution_plan_identity: ContentIdentity,
        component_revision: ContentIdentity,
    ) -> Path:
        key = self._key(execution_plan_identity, component_revision)
        return self.attempt_heads / f"{key}.json"

    def _read_attempt_head(
        self,
        execution_plan_identity: ContentIdentity,
        component_revision: ContentIdentity,
    ) -> StandardLifecycleAttemptEvidence | None:
        path = self._attempt_head_path(execution_plan_identity, component_revision)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise StandardLifecycleCheckpointError(
                "checkpoint attempt head is not a regular file"
            )
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
            if canonical_json_bytes(value) != raw:
                raise ValueError("attempt head is not canonical JSON")
            reference = BlobRef.from_dict(value, path="checkpoint_attempt_head")
            attempt_value = self.cas.get_manifest(reference)
            attempt = StandardLifecycleAttemptEvidence.from_dict(attempt_value)
        except Exception as exc:
            raise StandardLifecycleCheckpointError(
                "checkpoint attempt head or immutable record is invalid"
            ) from exc
        if attempt.identity.digest != reference.digest:
            raise StandardLifecycleCheckpointError(
                "checkpoint attempt identity differs from its immutable blob"
            )
        if (
            attempt.execution_plan_identity != execution_plan_identity
            or attempt.component_revision != component_revision
        ):
            raise StandardLifecycleCheckpointError(
                "checkpoint attempt head resolves to another execution node"
            )
        return attempt

    def start_attempt(
        self,
        execution_plan: ComponentExecutionPlan,
        prepared_nodes: Mapping[str, PreparedComponentGenerationNode[object, object]],
    ) -> None:
        """Begin one process attempt after inspecting durable predecessor lineage."""

        if not isinstance(execution_plan, ComponentExecutionPlan):
            raise TypeError("execution_plan must be a ComponentExecutionPlan")
        expected = {
            item.component_revision.uri for item in execution_plan.generation_plans
        }
        if set(prepared_nodes) != expected:
            raise StandardLifecycleCheckpointError(
                "checkpoint attempt requires every and only prepared Component"
            )
        with self._lock:
            for plan in execution_plan.generation_plans:
                key = self._key(execution_plan.identity, plan.component_revision)
                latest = self._read_head(
                    execution_plan.identity, plan.component_revision
                )
                previous_attempt = self._read_attempt_head(
                    execution_plan.identity, plan.component_revision
                )
                prepared = prepared_nodes[plan.component_revision.uri]
                recipe_identity = getattr(prepared.recipe, "identity", None)
                if isinstance(recipe_identity, str):
                    recipe_identity = ContentIdentity.parse_uri(recipe_identity)
                if not isinstance(recipe_identity, ContentIdentity):
                    raise StandardLifecycleCheckpointError(
                        "prepared checkpoint recipe has no content identity"
                    )
                attempt = StandardLifecycleAttemptEvidence(
                    execution_plan.identity,
                    plan.component_revision,
                    plan.identity,
                    plan.generation_key.identity,
                    recipe_identity,
                    1 if previous_attempt is None else previous_attempt.attempt + 1,
                    (None if previous_attempt is None else previous_attempt.identity),
                    None if latest is None else latest.identity,
                )
                reference = self.cas.put_manifest(attempt.to_dict())
                if reference.digest != attempt.identity.digest:
                    raise StandardLifecycleCheckpointError(
                        "immutable attempt storage changed canonical bytes"
                    )
                self._write_head(
                    self._attempt_head_path(
                        execution_plan.identity, plan.component_revision
                    ),
                    reference,
                )
                self._attempts[key] = attempt

    def latest(
        self,
        execution_plan_identity: ContentIdentity,
        component_revision: ContentIdentity,
    ) -> StandardLifecycleCheckpoint | None:
        """Return the verified durable head for audit and restart diagnostics."""

        with self._lock:
            return self._read_head(execution_plan_identity, component_revision)

    def latest_attempt(
        self,
        execution_plan_identity: ContentIdentity,
        component_revision: ContentIdentity,
    ) -> StandardLifecycleAttemptEvidence | None:
        """Return the latest attempt, even when source generation never completed."""

        with self._lock:
            return self._read_attempt_head(execution_plan_identity, component_revision)

    def _capture_source_files(
        self, evidence: StandardLifecycleStageEvidence
    ) -> tuple[CachedSourceFile, ...]:
        root = self.source_root(
            evidence.source_generation.output.candidate.tree_identity
        ).resolve(strict=True)
        files: list[CachedSourceFile] = []
        for path in root.rglob("*"):
            if path.is_symlink():
                raise StandardLifecycleCheckpointError(
                    "checkpoint source contains a symbolic link"
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise StandardLifecycleCheckpointError(
                    "checkpoint source contains a non-regular entry"
                )
            files.append(
                CachedSourceFile(
                    path.relative_to(root).as_posix(), self.cas.put_file(path)
                )
            )
        if not files:
            raise StandardLifecycleCheckpointError("checkpoint source tree is empty")
        # pathlib's native ordering follows host path semantics.  Canonical
        # checkpoint order is defined by the portable POSIX paths on the wire,
        # including on case-insensitive Windows filesystems.
        return tuple(sorted(files, key=lambda item: item.path))

    def record(self, evidence: StandardLifecycleStageEvidence) -> None:
        """Append one exact boundary; a changed predecessor fails closed."""

        if not isinstance(evidence, StandardLifecycleStageEvidence):
            raise TypeError("checkpoint evidence must be typed")
        key = self._key(evidence.execution_plan_identity, evidence.component_revision)
        with self._lock:
            attempt_evidence = self._attempts.get(key)
            if attempt_evidence is None:
                raise StandardLifecycleCheckpointError(
                    "checkpoint attempt was not started"
                )
            attempt = attempt_evidence.attempt
            latest = self._read_head(
                evidence.execution_plan_identity, evidence.component_revision
            )
            if latest is not None and latest.attempt == attempt:
                previous_stage = _STAGE_INDEX[latest.evidence.stage]
                current_stage = _STAGE_INDEX[evidence.stage]
                if current_stage <= previous_stage:
                    raise StandardLifecycleCheckpointError(
                        "checkpoint stages must advance exactly once within an attempt"
                    )
                if latest.evidence.outcome is StandardLifecycleCheckpointOutcome.FAILED:
                    raise StandardLifecycleCheckpointError(
                        "a failed attempt cannot append another stage"
                    )
            first_in_attempt = latest is None or latest.attempt != attempt
            source_files = None if first_in_attempt else self._source_files.get(key)
            if source_files is None:
                source_files = self._capture_source_files(evidence)
                self._source_files[key] = source_files
            checkpoint = StandardLifecycleCheckpoint(
                attempt,
                attempt_evidence,
                (
                    latest.attempt_sequence + 1
                    if latest is not None and latest.attempt == attempt
                    else 1
                ),
                1 if latest is None else latest.sequence + 1,
                None if latest is None else latest.identity,
                evidence,
                source_files,
            )
            reference = self.cas.put_manifest(checkpoint.to_dict())
            if reference.digest != checkpoint.identity.digest:
                raise StandardLifecycleCheckpointError(
                    "immutable checkpoint storage changed canonical bytes"
                )
            self._write_head(
                self._head_path(
                    evidence.execution_plan_identity, evidence.component_revision
                ),
                reference,
            )

    @staticmethod
    def _write_head(path: Path, reference: BlobRef) -> None:
        content = canonical_json_bytes(reference.to_dict())
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def record_failure(
        self,
        evidence: StandardLifecycleStageEvidence,
    ) -> None:
        if evidence.outcome is not StandardLifecycleCheckpointOutcome.FAILED:
            raise TypeError("record_failure requires failed evidence")
        self.record(evidence)

    def restore_prepared(
        self,
        execution_plan: ComponentExecutionPlan,
        prepared: PreparedComponentGenerationNode[object, object],
    ) -> SourceGenerationResumeCandidate | None:
        """Restore source bytes, then bind them to the newly allocated workspace."""

        with self._lock:
            checkpoint = self._read_head(
                execution_plan.identity, prepared.plan.component_revision
            )
            if checkpoint is None:
                return None
            evidence = checkpoint.evidence
            if (
                evidence.source_generation.output.provenance.retained_source_identity
                is not None
            ):
                # Retained input requires a fresh exact operator review. It must
                # never become an implicit substitute for ordinary generation.
                return None
            recipe_identity = getattr(prepared.recipe, "identity", None)
            if isinstance(recipe_identity, str):
                recipe_identity = ContentIdentity.parse_uri(recipe_identity)
            if (
                evidence.generation_plan_identity != prepared.plan.identity
                or evidence.generation_key_identity
                != prepared.plan.generation_key.identity
                or evidence.recipe_identity != recipe_identity
            ):
                return None
            destination = Path(prepared.workspace.locator)
            self._materialize(checkpoint.source_files, destination)
            rebound = rebind_standard_source_checkpoint_generation(
                execution_plan,
                prepared,
                evidence.source_generation,
                checkpoint_identity=checkpoint.identity,
            )
            self.source_trees.register(
                rebound.output.candidate,
                destination,
                source_generation_identity=rebound.output.identity,
                recipe=prepared.recipe,
            )
            return rebound

    def _materialize(
        self, source_files: tuple[CachedSourceFile, ...], destination: Path
    ) -> None:
        destination = destination.resolve()
        if (
            destination.is_symlink()
            or not destination.is_dir()
            or any(destination.iterdir())
        ):
            raise StandardLifecycleCheckpointError(
                "checkpoint destination must be a newly allocated empty directory"
            )
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.checkpoint-", dir=destination.parent
            )
        )
        try:
            for source_file in source_files:
                relative = PurePosixPath(source_file.path)
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.cas.get_bytes(source_file.blob))
            destination.rmdir()
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


__all__ = [
    "FilesystemStandardLifecycleCheckpointStore",
    "StandardLifecycleCheckpointError",
]
