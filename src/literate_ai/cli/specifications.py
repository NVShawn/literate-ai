"""Thin filesystem adapter for layered specification-corpus services."""

from __future__ import annotations

import os
import tempfile
from argparse import Namespace
from pathlib import Path
from typing import Any

from literate_ai.adapters.specifications import (
    LiterateMarkdownProvider,
    ScxmlError,
    ScxmlProvider,
    format_literate_markdown_document,
)
from literate_ai.application import (
    FormattedSpecificationCorpus,
    SpecificationCorpusError,
    SpecificationCorpusService,
)


class SpecificationCliError(ValueError):
    """Stable adapter error suitable for the top-level JSON error envelope."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def specification_corpus_from_args(args: Namespace) -> tuple[dict[str, Any], int]:
    """Execute format, validate, or explain through the public application service."""

    root, paths = _discover_corpus(Path(args.corpus))
    service = SpecificationCorpusService(
        LiterateMarkdownProvider(), format_literate_markdown_document
    )
    try:
        if args.spec_command == "validate":
            report = service.validate(root, paths, id_prefix=args.id_prefix)
            return report.to_dict(), 0
        if args.spec_command == "explain":
            explanation = service.explain(
                root,
                paths,
                id_prefix=args.id_prefix,
                node_id=args.node,
            )
            return {
                "schema": "literate-ai/specification-corpus-explanation@1",
                "provider": "literate-markdown@1",
                "explanation": explanation.to_dict(),
            }, 0
        formatted = service.format(root, paths, id_prefix=args.id_prefix)
    except SpecificationCorpusError as exc:
        raise SpecificationCliError(exc.code, str(exc)) from exc

    if args.check:
        status = 1 if formatted.changed_paths else 0
        return _format_result(formatted, written=()), status
    written = _write_formatted(root, formatted) if args.write else ()
    return _format_result(formatted, written=written), 0


def scxml_review_from_args(args: Namespace) -> dict[str, Any]:
    """Run bounded structural and trace review without changing authored files."""

    configured = (Path(args.chart), *(Path(item) for item in args.trace))
    try:
        resolved = tuple(path.resolve(strict=True) for path in configured)
    except OSError as exc:
        raise SpecificationCliError(
            "scxml.review_unavailable", "SCXML review artifact is unavailable"
        ) from exc
    if any(path.is_symlink() or not path.is_file() for path in configured):
        raise SpecificationCliError(
            "scxml.review_path_invalid", "SCXML review requires regular files"
        )
    root = Path(os.path.commonpath(tuple(str(path.parent) for path in resolved)))
    common = Path(os.path.commonpath(tuple(str(path) for path in resolved)))
    if common.is_dir():
        root = common
    paths = tuple(path.relative_to(root).as_posix() for path in resolved)
    try:
        loaded = ScxmlProvider().load(root, paths)
        loaded.require_unchanged(root)
    except (ScxmlError, ValueError) as exc:
        raise SpecificationCliError(
            getattr(exc, "code", "scxml.review_invalid"), str(exc)
        ) from exc
    return {
        "schema": "literate-ai/scxml-review@1",
        "provider": ScxmlProvider.provider_id,
        "state": "passed",
        "artifacts": [item.to_dict() for item in loaded.specification_set.artifacts],
        "specification_set_identity": loaded.specification_set.identity.uri,
        "requirement_count": len(loaded.specification_set.requirements),
    }


def _discover_corpus(requested: Path) -> tuple[Path, tuple[str, ...]]:
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise SpecificationCliError(
            "specification_corpus.unavailable", "Specification corpus is unavailable"
        ) from exc
    if requested.is_symlink() or resolved.is_symlink():
        raise SpecificationCliError(
            "specification_corpus.unsafe_path",
            "Specification corpus cannot be selected through a symbolic link",
        )
    if resolved.is_file():
        if resolved.name != "spec.md":
            raise SpecificationCliError(
                "specification_corpus.root_invalid",
                "Specification corpus file must be its root spec.md",
            )
        root = resolved.parent
        root_document = resolved
    elif resolved.is_dir():
        root = resolved
        root_document = root / "spec.md"
        if root_document.is_symlink() or not root_document.is_file():
            raise SpecificationCliError(
                "specification_corpus.root_missing",
                "Specification corpus directory requires a regular spec.md",
            )
    else:
        raise SpecificationCliError(
            "specification_corpus.root_invalid",
            "Specification corpus must be a directory or root spec.md",
        )

    artifacts: list[str] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        retained: list[str] = []
        for name in sorted(directories):
            directory = current_path / name
            if directory.is_symlink():
                raise SpecificationCliError(
                    "specification_corpus.unsafe_path",
                    "Specification corpus cannot contain symbolic-link directories",
                )
            if name != ".literate":
                retained.append(name)
        directories[:] = retained
        for name in sorted(files):
            path = current_path / name
            if path.is_symlink():
                raise SpecificationCliError(
                    "specification_corpus.unsafe_path",
                    "Specification corpus cannot contain symbolic-link artifacts",
                )
            if path.suffix in {".md", ".json"}:
                artifacts.append(path.relative_to(root).as_posix())
    root_path = root_document.relative_to(root).as_posix()
    if root_path not in artifacts:  # pragma: no cover - checked above
        raise SpecificationCliError(
            "specification_corpus.root_missing", "Specification root disappeared"
        )
    return root, (root_path, *(path for path in artifacts if path != root_path))


def _write_formatted(
    root: Path, formatted: FormattedSpecificationCorpus
) -> tuple[str, ...]:
    changed = tuple(item for item in formatted.artifacts if item.changed)
    originals = {item.path: (root / item.path).read_bytes() for item in changed}
    modes = {item.path: (root / item.path).stat().st_mode for item in changed}
    written: list[str] = []
    try:
        for artifact in changed:
            target = root / artifact.path
            if target.is_symlink() or target.read_bytes() != originals[artifact.path]:
                raise SpecificationCliError(
                    "specification_corpus.changed",
                    "Specification corpus changed after its formatting preview",
                )
            _replace_bytes(target, artifact.content, modes[artifact.path])
            written.append(artifact.path)
    except Exception:
        for relative in reversed(written):
            _replace_bytes(root / relative, originals[relative], modes[relative])
        raise
    return tuple(written)


def _replace_bytes(target: Path, content: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _format_result(
    formatted: FormattedSpecificationCorpus, *, written: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "schema": "literate-ai/specification-corpus-format@1",
        "provider": "literate-markdown@1",
        "changed_paths": list(formatted.changed_paths),
        "written_paths": list(written),
        "report": formatted.report.to_dict(),
    }


__all__ = [
    "SpecificationCliError",
    "scxml_review_from_args",
    "specification_corpus_from_args",
]
