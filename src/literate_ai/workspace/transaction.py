"""Whole-tree acceptance with immutable trees and atomic reference updates."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai.contracts import (
    SourceIntelligenceArtifact,
    generated_source_snapshot_identity,
    generated_source_tree_identity,
)
from literate_ai.contracts.paths import (
    canonical_relative_posix_path,
    canonical_relative_posix_paths,
)


class WorkspaceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _require_digest(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise WorkspaceError(
            "workspace.identity_invalid", f"{label} must be an exact sha256 identity"
        )
    return value


def _safe_path(value: str) -> PurePosixPath:
    try:
        return canonical_relative_posix_path(value, label="workspace path")
    except (TypeError, ValueError) as error:
        raise WorkspaceError(
            "workspace.path_invalid", f"Unsafe project path: {value!r}"
        ) from error


def _safe_paths(values: Iterable[str]) -> tuple[PurePosixPath, ...]:
    try:
        return canonical_relative_posix_paths(values, label="workspace tree path")
    except (TypeError, ValueError) as error:
        raise WorkspaceError(
            "workspace.path_invalid",
            "Workspace tree paths must be canonical, portable, and non-aliasing",
        ) from error


def _tree_digest(files: Mapping[str, bytes]) -> str:
    try:
        return generated_source_tree_identity(files)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError(
            "workspace.path_invalid",
            "Workspace source paths collide with reserved derived metadata",
        ) from exc


@dataclass(frozen=True, slots=True)
class PreparedTree:
    tree_digest: str
    staging_path: Path
    manifest: tuple[tuple[str, str], ...]


class WorkspaceTreeStore:
    """Commit staged files as a set; readers resolve one atomic reference."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.trees = self.root / "trees"
        self.staging = self.root / "staging"
        self.references = self.root / "references"
        self.locks = self.root / "locks"
        for directory in (self.trees, self.staging, self.references, self.locks):
            directory.mkdir(parents=True, exist_ok=True)

    def prepare(
        self,
        files: Mapping[str, bytes],
        *,
        precondition: Callable[[Path], None] | None = None,
    ) -> PreparedTree:
        if not files:
            raise WorkspaceError("workspace.empty_tree", "Cannot prepare an empty tree")
        paths = _safe_paths(files.keys())
        normalized = {
            path: content for path, content in zip(paths, files.values(), strict=True)
        }
        tree_digest = _tree_digest(
            {str(path): data for path, data in normalized.items()}
        )
        directory = Path(tempfile.mkdtemp(prefix="tree-", dir=self.staging))
        try:
            for relative, content in normalized.items():
                target = directory.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            if precondition is not None:
                precondition(directory)
            manifest = tuple(
                (str(relative), f"sha256:{hashlib.sha256(content).hexdigest()}")
                for relative, content in sorted(
                    normalized.items(), key=lambda item: str(item[0])
                )
            )
            (directory / ".literate-tree.json").write_text(
                json.dumps(
                    {"tree_digest": tree_digest, "files": manifest},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            return PreparedTree(tree_digest, directory, manifest)
        except Exception:
            self._remove_staging(directory)
            raise

    def commit(
        self,
        prepared: PreparedTree,
        reference: str,
        *,
        expected_tree_digest: str | None = None,
        pre_reference: Callable[[Path], Mapping[str, object]] | None = None,
    ) -> Path:
        _safe_path(reference)
        _require_digest(prepared.tree_digest, label="prepared workspace tree identity")
        if not prepared.staging_path.is_relative_to(self.staging):
            raise WorkspaceError(
                "workspace.staging_invalid", "Prepared tree is not staged"
            )
        lock = self.locks / f"{prepared.tree_digest.removeprefix('sha256:')}.lock"
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise WorkspaceError(
                    "workspace.tree_busy", "Workspace tree publication is in progress"
                ) from exc
            final = self.trees / prepared.tree_digest.removeprefix("sha256:")
            if final.exists():
                self._verify_tree(final, prepared)
                self._remove_staging(prepared.staging_path)
            else:
                os.replace(prepared.staging_path, final)
            # Source files are immutable authority. Mutable derived sidecars are
            # repaired at the final path before the atomic reference is published.
            reference_metadata = (
                dict(pre_reference(final)) if pre_reference is not None else {}
            )
            intelligence_identity = reference_metadata.get(
                "source_intelligence_identity"
            )
            artifact_identity = reference_metadata.get(
                "source_intelligence_artifact_identity"
            )
            if (intelligence_identity is None) != (artifact_identity is None):
                raise WorkspaceError(
                    "workspace.intelligence_reference_invalid",
                    "Workspace source-intelligence reference is incomplete",
                )
            if intelligence_identity is not None:
                intelligence_identity = _require_digest(
                    intelligence_identity,
                    label="workspace source-intelligence identity",
                )
                artifact_identity = _require_digest(
                    artifact_identity,
                    label="workspace source-intelligence artifact identity",
                )
            dependency_resolution_identity = reference_metadata.get(
                "dependency_resolution_identity"
            )
            source_bom_binding_identity = reference_metadata.get(
                "source_bom_binding_identity"
            )
            resolved_bom_binding_identity = reference_metadata.get(
                "resolved_bom_binding_identity"
            )
            dependency_values = (
                dependency_resolution_identity,
                source_bom_binding_identity,
                resolved_bom_binding_identity,
            )
            if any(value is None for value in dependency_values) and any(
                value is not None for value in dependency_values
            ):
                raise WorkspaceError(
                    "workspace.dependency_reference_invalid",
                    "Workspace dependency reference is incomplete",
                )
            if dependency_resolution_identity is not None:
                dependency_resolution_identity = _require_digest(
                    dependency_resolution_identity,
                    label="workspace dependency resolution identity",
                )
                source_bom_binding_identity = _require_digest(
                    source_bom_binding_identity,
                    label="workspace source BOM binding identity",
                )
                resolved_bom_binding_identity = _require_digest(
                    resolved_bom_binding_identity,
                    label="workspace resolved BOM binding identity",
                )
            reference_path = self.references.joinpath(*PurePosixPath(reference).parts)
            current = (
                self._reference_document(reference_path)
                if reference_path.exists()
                else None
            )
            if expected_tree_digest is not None and (
                current is None or current["tree_digest"] != expected_tree_digest
            ):
                raise WorkspaceError(
                    "workspace.reference_conflict",
                    "Workspace reference changed before commit",
                )
            reference_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = reference_path.with_name(
                f".{reference_path.name}.tmp-{os.getpid()}"
            )
            document = {
                "schema": "literate-ai/workspace-reference@5",
                "tree_digest": prepared.tree_digest,
                "source_intelligence_identity": intelligence_identity,
                "source_intelligence_artifact_identity": artifact_identity,
                "dependency_resolution_identity": dependency_resolution_identity,
                "source_bom_binding_identity": source_bom_binding_identity,
                "resolved_bom_binding_identity": resolved_bom_binding_identity,
            }
            temporary.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, reference_path)
            return final
        finally:
            if descriptor is not None:
                os.close(descriptor)
                lock.unlink(missing_ok=True)

    def resolve(self, reference: str) -> Path | None:
        _safe_path(reference)
        target = self.references.joinpath(*PurePosixPath(reference).parts)
        if not target.exists():
            return None
        document = self._reference_document(target)
        digest = _require_digest(
            document["tree_digest"], label="referenced workspace tree identity"
        )
        tree = self.trees / digest.removeprefix("sha256:")
        if not tree.is_dir():
            raise WorkspaceError("workspace.reference_broken", f"Missing tree {digest}")
        metadata = tree / ".literate-tree.json"
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
            manifest = tuple((str(item[0]), str(item[1])) for item in value["files"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                "workspace.reference_broken", "Referenced tree manifest is invalid"
            ) from exc
        self._verify_tree(
            tree, PreparedTree(digest, self.staging / ".resolve", manifest)
        )
        intelligence_identity = document.get("source_intelligence_identity")
        if intelligence_identity is not None:
            source_files = {
                name: tree.joinpath(*PurePosixPath(name).parts).read_bytes()
                for name, _digest in manifest
            }
            self._verify_source_intelligence(
                tree,
                str(intelligence_identity),
                expected_tree_identity=digest,
                expected_snapshot_identity=generated_source_snapshot_identity(
                    source_files
                ),
                expected_artifact_identity=str(
                    document["source_intelligence_artifact_identity"]
                ),
            )
        return tree

    def recover(self) -> tuple[Path, ...]:
        return tuple(sorted(path for path in self.staging.iterdir() if path.is_dir()))

    @staticmethod
    def _remove_staging(directory: Path) -> None:
        if not directory.exists():
            return
        for path in sorted(directory.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        directory.rmdir()

    @staticmethod
    def _verify_tree(path: Path, prepared: PreparedTree) -> None:
        metadata = path / ".literate-tree.json"
        if metadata.is_symlink() or not metadata.is_file():
            raise WorkspaceError(
                "workspace.tree_collision", "Existing tree lacks manifest"
            )
        data = json.loads(metadata.read_text(encoding="utf-8"))
        if data.get("tree_digest") != prepared.tree_digest:
            raise WorkspaceError(
                "workspace.tree_collision", "Existing tree digest differs"
            )
        manifest = tuple((str(item[0]), str(item[1])) for item in data.get("files", ()))
        if manifest != prepared.manifest:
            raise WorkspaceError(
                "workspace.tree_collision", "Existing tree manifest differs"
            )
        paths = _safe_paths(path for path, _digest in manifest)
        expected = {item.as_posix() for item in paths}
        actual: set[str] = set()
        observed_files: dict[str, bytes] = {}
        for current, directories, files in os.walk(path, followlinks=False):
            current_path = Path(current)
            kept: list[str] = []
            for name in directories:
                directory = current_path / name
                if directory.is_symlink():
                    raise WorkspaceError(
                        "workspace.tree_collision", "Workspace tree contains a symlink"
                    )
                if directory.relative_to(path).as_posix() == ".codegraph":
                    continue
                kept.append(name)
            directories[:] = kept
            for name in files:
                file = current_path / name
                relative = file.relative_to(path).as_posix()
                if relative in {
                    ".literate-tree.json",
                    ".literate-source-index.json",
                    ".literate-source-intelligence.json",
                }:
                    continue
                if file.is_symlink() or not file.is_file():
                    raise WorkspaceError(
                        "workspace.tree_collision",
                        "Workspace tree contains a non-regular file",
                    )
                actual.add(relative)
        if actual != expected:
            raise WorkspaceError(
                "workspace.tree_collision", "Workspace source file set differs"
            )
        for (_name, expected_digest), relative in zip(manifest, paths, strict=True):
            content = path.joinpath(*relative.parts).read_bytes()
            if f"sha256:{hashlib.sha256(content).hexdigest()}" != expected_digest:
                raise WorkspaceError(
                    "workspace.tree_collision", "Workspace source content differs"
                )
            observed_files[relative.as_posix()] = content
        if _tree_digest(observed_files) != prepared.tree_digest:
            raise WorkspaceError(
                "workspace.tree_collision",
                "Workspace source content does not match its tree identity",
            )

    @staticmethod
    def _reference_document(path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                "workspace.reference_invalid", "Workspace reference is malformed"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != "literate-ai/workspace-reference@5"
            or set(value)
            != {
                "schema",
                "tree_digest",
                "source_intelligence_identity",
                "source_intelligence_artifact_identity",
                "dependency_resolution_identity",
                "source_bom_binding_identity",
                "resolved_bom_binding_identity",
            }
        ):
            raise WorkspaceError(
                "workspace.reference_invalid", "Workspace reference is malformed"
            )
        _require_digest(value["tree_digest"], label="workspace reference tree")
        intelligence_identity = value["source_intelligence_identity"]
        artifact_identity = value["source_intelligence_artifact_identity"]
        if (intelligence_identity is None) != (artifact_identity is None):
            raise WorkspaceError(
                "workspace.reference_invalid",
                "Workspace source-intelligence reference is incomplete",
            )
        if intelligence_identity is not None:
            _require_digest(
                intelligence_identity,
                label="workspace reference source intelligence",
            )
            _require_digest(
                artifact_identity,
                label="workspace reference source-intelligence artifact",
            )
        dependency_values = (
            value["dependency_resolution_identity"],
            value["source_bom_binding_identity"],
            value["resolved_bom_binding_identity"],
        )
        if any(item is None for item in dependency_values) and any(
            item is not None for item in dependency_values
        ):
            raise WorkspaceError(
                "workspace.reference_invalid",
                "Workspace dependency reference is incomplete",
            )
        for label, item in zip(
            (
                "dependency resolution",
                "source BOM binding",
                "resolved BOM binding",
            ),
            dependency_values,
            strict=True,
        ):
            if item is not None:
                _require_digest(item, label=f"workspace reference {label}")
        return value

    @staticmethod
    def _verify_source_intelligence(
        tree: Path,
        expected_identity: str,
        *,
        expected_tree_identity: str,
        expected_snapshot_identity: str,
        expected_artifact_identity: str,
    ) -> None:
        marker = tree / ".literate-source-intelligence.json"
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise TypeError
            intelligence = SourceIntelligenceArtifact.from_dict(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                "workspace.source_intelligence_invalid",
                "Referenced workspace source intelligence is invalid",
            ) from exc
        if intelligence.intelligence_identity != expected_identity:
            raise WorkspaceError(
                "workspace.source_intelligence_mismatch",
                "Workspace reference names another source-intelligence observation",
            )
        if intelligence.source_tree_identity != expected_tree_identity:
            raise WorkspaceError(
                "workspace.source_intelligence_mismatch",
                "Workspace source intelligence names another source tree",
            )
        if intelligence.source_snapshot_identity != expected_snapshot_identity:
            raise WorkspaceError(
                "workspace.source_intelligence_mismatch",
                "Workspace source intelligence names another source snapshot",
            )
        if intelligence.artifact_identity != expected_artifact_identity:
            raise WorkspaceError(
                "workspace.source_intelligence_mismatch",
                "Workspace reference names another source-intelligence artifact",
            )
        relative = canonical_relative_posix_path(
            intelligence.artifact_path, label="workspace intelligence artifact"
        )
        artifact = tree.joinpath(*relative.parts)
        if artifact.is_symlink() or not artifact.is_file():
            raise WorkspaceError(
                "workspace.source_intelligence_invalid",
                "Workspace source-intelligence artifact is missing",
            )
        actual = f"sha256:{hashlib.sha256(artifact.read_bytes()).hexdigest()}"
        if actual != intelligence.artifact_identity:
            raise WorkspaceError(
                "workspace.source_intelligence_mismatch",
                "Workspace source-intelligence artifact changed",
            )
