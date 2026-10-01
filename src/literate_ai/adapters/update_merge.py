"""Verified base custody and deterministic text merging for project updates.

The checkpoint records upstream bytes, never the merged local overlay. It is
committed with the update transaction, so a second update has the correct base.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

from literate_ai.contracts import ContentIdentity, ProjectUpdateClassification

CHECKPOINT = ".literate/update-bases.json"
SCHEMA = "literate-ai/update-bases@1"
MAX_BYTES = 64 * 1024 * 1024


def digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    from .project_updates import ProjectUpdateError, _project_path

    parsed = PurePosixPath(relative)
    if (
        not relative
        or not parsed.parts
        or parsed.is_absolute()
        or parsed.as_posix() != relative
        or ".." in parsed.parts
        or "\\" in relative
        or ":" in relative
        or parsed.parts[0] == ".git"
    ):
        raise ProjectUpdateError("project.update_path_unsafe", "unsafe update path")
    return _project_path(root, relative)


class UpdateBases:
    def __init__(self, root: Path):
        from .project_updates import ProjectUpdateError

        self.root = root
        path = safe_path(root, CHECKPOINT)
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            raise ProjectUpdateError(
                "project.update_base_oversized", "update baseline exceeds byte limit"
            )
        self.original = path.read_bytes() if path.is_file() else None
        self.blobs: dict[str, bytes] = {}
        self.scopes: dict[str, dict[str, str]] = {"framework": {}, "catalog": {}}
        self.sources: dict[str, str] = {}
        self.created_directories: list[Path] = []
        if self.original is None:
            return
        try:
            if len(self.original) > MAX_BYTES:
                raise ValueError("checkpoint exceeds byte limit")
            value = json.loads(self.original)
            if (
                set(value) != {"schema", "blobs", "framework", "catalog", "sources"}
                or value["schema"] != SCHEMA
            ):
                raise ValueError("unsupported checkpoint")
            for key, encoded in value["blobs"].items():
                content = base64.b64decode(encoded, validate=True)
                if digest(content) != key:
                    raise ValueError("base content identity mismatch")
                self.blobs[key] = content
            for scope in self.scopes:
                for relative, key in value[scope].items():
                    safe_path(root, relative)
                    if relative.startswith(".literate/"):
                        raise ValueError("update metadata cannot be a merge target")
                    ContentIdentity.parse_uri(key)
                    self.scopes[scope][relative] = key
            for relative, ref in value["sources"].items():
                safe_path(root, relative)
                if not isinstance(ref, str) or not ref.startswith("git:"):
                    raise ValueError("invalid historical source")
                self.sources[relative] = ref
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ProjectUpdateError("project.update_base_invalid", str(exc)) from exc

    def identities(self, scope: str) -> dict[str, ContentIdentity]:
        return {
            path: ContentIdentity.parse_uri(key)
            for path, key in self.scopes[scope].items()
        }

    def get(self, identity: ContentIdentity | None) -> bytes | None:
        return None if identity is None else self.blobs.get(identity.uri)

    def retain(self, content: bytes) -> None:
        self.blobs[digest(content)] = content

    def remember_catalog(self, imports) -> None:
        """Retain unresolved provenance before the import manifest is advanced."""
        for imported in imports:
            for file in imported.files:
                self.scopes["catalog"].setdefault(file.path, file.identity)
                if imported.source.ref.startswith("git:"):
                    self.sources.setdefault(file.path, imported.source.ref)

    def advance(self, scope: str, path: str, content: bytes | None) -> None:
        safe_path(self.root, path)
        if content is None:
            self.scopes[scope].pop(path, None)
            if scope == "catalog":
                self.sources.pop(path, None)
        else:
            self.retain(content)
            self.scopes[scope][path] = digest(content)

    def write(self) -> None:
        from .project_update_apply import _write_exact
        from .project_updates import ProjectUpdateError

        path = safe_path(self.root, CHECKPOINT)
        observed = path.read_bytes() if path.is_file() else None
        if observed != self.original:
            raise ProjectUpdateError(
                "project.update_base_changed", "update baseline changed; re-plan"
            )
        content = (
            json.dumps(
                {
                    "schema": SCHEMA,
                    **self.scopes,
                    "sources": self.sources,
                    "blobs": {
                        key: base64.b64encode(blob).decode("ascii")
                        for key, blob in sorted(self.blobs.items())
                    },
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode()
        if len(content) > MAX_BYTES:
            raise ProjectUpdateError(
                "project.update_base_oversized", "update baseline exceeds byte limit"
            )
        if content != self.original:
            parent = path.parent
            while parent != self.root and not parent.exists():
                self.created_directories.append(parent)
                parent = parent.parent
            _write_exact(path, content)

    def rollback(self) -> None:
        from .project_update_apply import _write_exact

        path = safe_path(self.root, CHECKPOINT)
        if self.original is None:
            path.unlink(missing_ok=True)
        else:
            _write_exact(path, self.original)
        for directory in self.created_directories:
            try:
                directory.rmdir()
            except OSError:
                pass


def merge_text(base: bytes, ours: bytes, theirs: bytes) -> bytes | None:
    """Return a clean Git diff3 result; never expose conflict markers as output."""
    from .project_updates import ProjectUpdateError

    for content in (base, ours, theirs):
        if len(content) > 16 * 1024 * 1024 or b"\0" in content:
            return None
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return None
    with tempfile.TemporaryDirectory(prefix="litai-merge-") as directory:
        paths = [Path(directory) / name for name in ("ours", "base", "theirs")]
        for path, content in zip(paths, (ours, base, theirs), strict=True):
            path.write_bytes(content)
        try:
            result = subprocess.run(
                ["git", "merge-file", "--stdout", "--diff3", *(str(p) for p in paths)],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectUpdateError(
                "project.update_merge_unavailable", "Git text merge could not run"
            ) from exc
    if result.returncode == 0:
        return result.stdout
    if 1 <= result.returncode <= 127:
        return None
    raise ProjectUpdateError("project.update_merge_failed", "Git text merge failed")


def enrich_merge(item, base: bytes | None, ours: bytes | None, theirs: bytes | None):
    if item.classification is not ProjectUpdateClassification.CONFLICT or base is None:
        return item
    if digest(base) != (item.baseline_identity.uri if item.baseline_identity else None):
        return item
    try:
        base_text = base.decode("utf-8") if b"\0" not in base else None
    except UnicodeDecodeError:
        base_text = None
    if base_text is None:
        return item
    merged = None if ours is None or theirs is None else merge_text(base, ours, theirs)
    return replace(
        item,
        base_text=base_text,
        classification=(
            ProjectUpdateClassification.MERGEABLE
            if merged is not None
            else item.classification
        ),
        merged_text=None if merged is None else merged.decode("utf-8"),
    )


def recover_bases(root, files, bases, reader=None, origin=None):
    """Recover legacy identity-only bases, accepting only exact digest matches."""
    from literate_ai.contracts import CatalogImportsFile, RepositoryParentReference

    from .project_initialization import _STARTER_TEMPLATE_FILES, _TEMPLATE_FILES
    from .repository_lineage import GitRepositoryLineageError

    missing = {
        item.path: item.baseline_identity
        for item in files
        if item.classification is ProjectUpdateClassification.CONFLICT
        and item.baseline_identity is not None
        and bases.get(item.baseline_identity) is None
    }
    if not missing or reader is None:
        return
    groups = {}
    if origin is not None:
        reference = RepositoryParentReference(
            origin.repository_url, origin.git_revision
        )
        resources = {**_TEMPLATE_FILES, **_STARTER_TEMPLATE_FILES}
        paths = set(missing)
        paths.update(
            "src/literate_ai/project_template/" + resources.get(path, path)
            for path in missing
        )
        groups[reference] = paths
    else:
        sources = dict(bases.sources)
        if (root / CatalogImportsFile.PATH).is_file():
            for imported in CatalogImportsFile.load(root).imports:
                for file in imported.files:
                    sources.setdefault(file.path, imported.source.ref)
        for path, ref in sources.items():
            if not ref.startswith("git:"):
                continue
            url, separator, revision = ref[4:].rpartition("@")
            if not separator:
                continue
            paths = {path} if path in missing else set()
            if paths:
                groups.setdefault(
                    RepositoryParentReference(url, revision), set()
                ).update(paths)
    wanted = {identity.uri for identity in missing.values()}
    for reference, paths in groups.items():
        try:
            recovered = reader(reference, tuple(sorted(paths)))
        except GitRepositoryLineageError:
            # Missing historical objects leave an explicit unresolved conflict.
            continue
        for content in recovered.values():
            if digest(content) in wanted:
                bases.retain(content)


def resolution_content(item, ours, theirs, resolutions):
    """Revalidate a clean merge or one explicitly accepted review before writing."""
    from .project_updates import ProjectUpdateError

    if item.classification is ProjectUpdateClassification.MERGEABLE:
        if ours is None or theirs is None or item.base_text is None:
            raise ProjectUpdateError(
                "project.update_merge_invalid", "merge inputs are unavailable"
            )
        merged = merge_text(item.base_text.encode(), ours, theirs)
        if merged is None or merged.decode() != item.merged_text:
            raise ProjectUpdateError(
                "project.update_merge_invalid", "merge no longer matches the plan"
            )
        return merged
    review = resolutions.get(item.path)
    if review is None:
        return theirs
    if (
        item.classification is not ProjectUpdateClassification.CONFLICT
        or review.get("file_identity") != item.identity.to_dict()
    ):
        raise ProjectUpdateError(
            "project.update_resolution_stale", "review inputs changed; review again"
        )
    decision = review.get("decision")
    if decision == "keep-local":
        return ours
    if decision == "take-upstream":
        return theirs
    merged = review.get("merged_text")
    if decision != "merge" or not isinstance(merged, str) or "\0" in merged:
        raise ProjectUpdateError(
            "project.update_resolution_invalid", "review must provide valid merged text"
        )
    return merged.encode("utf-8")


def load_resolutions(path, files):
    from .project_updates import ProjectUpdateError

    if path is None:
        return {}
    try:
        source = Path(path)
        if source.stat().st_size > MAX_BYTES:
            raise ValueError("review exceeds byte limit")
        value = json.loads(source.read_bytes())
        if isinstance(value, dict):
            value = value.get("result", value).get("conflict_reviews")
        if not isinstance(value, list):
            raise ValueError("expected conflict_reviews list or update JSON envelope")
        by_path = {item.path: item for item in files}
        selected = {}
        for review in value:
            if (
                not isinstance(review, dict)
                or review.get("schema")
                != "literate-ai/project-update-conflict-review@1"
            ):
                raise ValueError("unsupported review")
            relative = review.get("path")
            if relative not in by_path or relative in selected:
                raise ValueError("unknown or duplicate review path")
            item = by_path[relative]
            if (
                item.classification is not ProjectUpdateClassification.CONFLICT
                or review.get("file_identity") != item.identity.to_dict()
            ):
                raise ValueError("review inputs changed; review again")
            if review.get("decision") not in {"keep-local", "take-upstream", "merge"}:
                raise ValueError("unknown resolution decision")
            if (
                not isinstance(review.get("rationale"), str)
                or not review["rationale"].strip()
            ):
                raise ValueError("resolution rationale is required")
            if review["decision"] == "merge" and (
                not isinstance(review.get("merged_text"), str)
                or "\0" in review["merged_text"]
            ):
                raise ValueError("valid merged text is required")
            selected[relative] = review
        return selected
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ProjectUpdateError("project.update_resolution_invalid", str(exc)) from exc
