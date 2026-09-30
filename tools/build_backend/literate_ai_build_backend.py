"""Setuptools backend that embeds the exact Git origin in built distributions."""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

_ROOT = Path(__file__).resolve().parents[2]
_ORIGIN = _ROOT / "src" / "literate_ai" / "_distribution_origin.json"
_LOCK = _ROOT / ".literate-ai-distribution-origin.lock"
_REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# The isolated PEP 517 environment has only build dependencies. Load the shared,
# stdlib-only URL authority without importing the framework's runtime package.
_canonical_repository_origin = runpy.run_path(
    str(_ROOT / "src" / "literate_ai" / "repository_urls.py")
)["canonical_repository_origin"]


def _backend():
    from setuptools import build_meta

    return build_meta


def _reset_setuptools_wheel_staging() -> None:
    """Remove prior build_py output so deleted package data cannot enter a wheel."""

    staging = _ROOT / "_build" / "setuptools" / "lib"
    if staging.is_symlink():
        raise RuntimeError("setuptools wheel staging directory is a symbolic link")
    if not staging.exists():
        return
    if not staging.is_dir():
        raise RuntimeError("setuptools wheel staging path is not a directory")
    shutil.rmtree(staging)


def _git(*arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(_ROOT), *arguments),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def distribution_origin_bytes(environment: Mapping[str, str]) -> bytes:
    """Resolve and validate exact build provenance without a canonical fallback."""

    repository_url = environment.get("LITAI_BUILD_REPOSITORY_URL")
    revision = environment.get("LITAI_BUILD_GIT_REVISION")
    if (repository_url is None) != (revision is None):
        raise RuntimeError(
            "LITAI_BUILD_REPOSITORY_URL and LITAI_BUILD_GIT_REVISION must be "
            "set together"
        )
    if repository_url is None:
        if _git("status", "--porcelain=v1", "--untracked-files=all"):
            raise RuntimeError(
                "distribution build from Git requires a clean exact checkout"
            )
        repository_url = _git("remote", "get-url", "origin")
        revision = _git("rev-parse", "--verify", "HEAD^{commit}")
    if not repository_url or not revision:
        raise RuntimeError(
            "distribution build requires an exact Git origin and revision; build from "
            "a checkout or set both LITAI_BUILD_* provenance variables"
        )
    revision = revision.casefold()
    parsed = urlsplit(repository_url)
    if (
        any(ord(character) < 32 for character in repository_url)
        or parsed.password is not None
        or (
            parsed.scheme in {"http", "https"}
            and (parsed.username is not None or parsed.query or parsed.fragment)
        )
        or _REVISION.fullmatch(revision) is None
    ):
        raise RuntimeError("distribution build origin is unsafe or not an exact commit")
    return (
        json.dumps(
            {
                "schema": "literate-ai/distribution-origin@1",
                "repository_url": _canonical_repository_origin(repository_url),
                "git_revision": revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _validate_existing_origin(content: bytes) -> None:
    try:
        document = json.loads(content)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("embedded distribution origin is invalid") from exc
    expected = {"schema", "repository_url", "git_revision"}
    if not isinstance(document, dict) or set(document) != expected:
        raise RuntimeError("embedded distribution origin is invalid")
    if document.get("schema") != "literate-ai/distribution-origin@1":
        raise RuntimeError("embedded distribution origin has another schema")
    distribution_origin_bytes(
        {
            "LITAI_BUILD_REPOSITORY_URL": str(document.get("repository_url", "")),
            "LITAI_BUILD_GIT_REVISION": str(document.get("git_revision", "")),
        }
    )


@contextmanager
def _embedded_distribution_origin() -> Iterator[None]:
    """Publish one temporary source resource, preserving an sdist-provided copy."""

    if _ORIGIN.exists():
        if (_ROOT / ".git").exists():
            raise RuntimeError(
                "source checkout contains a stale generated distribution-origin "
                "resource"
            )
        content = _ORIGIN.read_bytes()
        _validate_existing_origin(content)
        yield
        return
    # Resolve clean-checkout provenance before publishing our own untracked lock.
    # Otherwise the cleanliness check observes the lock created by this context
    # manager and every wheel build from Git rejects itself as dirty.
    content = distribution_origin_bytes(os.environ)
    descriptor = os.open(_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    created = False
    try:
        os.close(descriptor)
        _ORIGIN.write_bytes(content)
        created = True
        yield
    finally:
        if created:
            _ORIGIN.unlink(missing_ok=True)
        _LOCK.unlink(missing_ok=True)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    with _embedded_distribution_origin():
        # setuptools' build_py is incremental and otherwise retains package-data files
        # removed from source.  A wheel is release authority, so it must be projected
        # from current inputs rather than an ignored prior build/lib tree.
        _reset_setuptools_wheel_staging()
        return _backend().build_wheel(
            wheel_directory, config_settings, metadata_directory
        )


def build_sdist(sdist_directory, config_settings=None):
    with _embedded_distribution_origin():
        return _backend().build_sdist(sdist_directory, config_settings)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _backend().build_editable(
        wheel_directory, config_settings, metadata_directory
    )


def get_requires_for_build_wheel(config_settings=None):
    return _backend().get_requires_for_build_wheel(config_settings)


def get_requires_for_build_sdist(config_settings=None):
    return _backend().get_requires_for_build_sdist(config_settings)


def get_requires_for_build_editable(config_settings=None):
    return _backend().get_requires_for_build_editable(config_settings)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _backend().prepare_metadata_for_build_wheel(
        metadata_directory, config_settings
    )


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _backend().prepare_metadata_for_build_editable(
        metadata_directory, config_settings
    )
