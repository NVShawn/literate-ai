"""Durable user test-matrix configuration contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GLOBAL_TEST_MATRIX_SCHEMA = "literate-ai/global-test-matrix@3"
MAX_GLOBAL_TEST_MATRIX_BYTES = 1024 * 1024
_PLATFORMS = frozenset({"linux", "macos", "windows"})
_SOURCE_MODES = frozenset({"working-tree", "git"})


class TestMatrixConfigError(ValueError):
    """A private test-matrix document is unavailable or invalid."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TestWorkerSelection:
    worker_id: str
    platform_flavor: str
    source_mode: str

    @property
    def platform(self) -> str:
        return self.platform_flavor.removeprefix("flavor://literate-ai/os-")


@dataclass(frozen=True, slots=True)
class GlobalTestMatrix:
    default_samples: tuple[str, ...]
    workers: tuple[TestWorkerSelection, ...]
    coding_cli: str | None = None
    model: str | None = None

    @classmethod
    def from_dict(cls, value: Any) -> GlobalTestMatrix:
        if not isinstance(value, dict):
            raise _invalid("global test matrix must be one JSON object")
        required = {"schema", "default_samples", "workers"}
        allowed = required | {"coding_cli", "model"}
        if not required <= set(value) or not set(value) <= allowed:
            raise _invalid("global test matrix has missing or unknown fields")
        if value["schema"] != GLOBAL_TEST_MATRIX_SCHEMA:
            raise _invalid("global test matrix schema is unsupported")
        samples = value["default_samples"]
        raw_workers = value["workers"]
        if (
            not isinstance(samples, list)
            or not samples
            or any(not isinstance(item, str) or not item for item in samples)
            or not isinstance(raw_workers, list)
            or not raw_workers
        ):
            raise _invalid("global test matrix requires samples and workers")
        workers = tuple(
            _worker_selection(item, index) for index, item in enumerate(raw_workers)
        )
        worker_ids = tuple(item.worker_id for item in workers)
        if len(set(worker_ids)) != len(worker_ids):
            raise _invalid("global test matrix worker IDs must be unique")
        return cls(
            tuple(samples),
            workers,
            _optional_text(value, "coding_cli"),
            _optional_text(value, "model"),
        )


def load_global_test_matrix(path: str | Path) -> GlobalTestMatrix:
    """Load one bounded, regular, non-symlink test-matrix document."""

    source = Path(path).expanduser()
    try:
        metadata = source.lstat()
        if source.is_symlink() or not source.is_file():
            raise TestMatrixConfigError(
                "test_matrix.config_unavailable",
                "global test matrix must be a regular non-symlink file",
            )
        if metadata.st_size > MAX_GLOBAL_TEST_MATRIX_BYTES:
            raise TestMatrixConfigError(
                "test_matrix.config_too_large",
                "global test matrix exceeds the one MiB limit",
            )
        raw = source.read_bytes()
    except TestMatrixConfigError:
        raise
    except OSError as exc:
        raise TestMatrixConfigError(
            "test_matrix.config_unavailable",
            "global test matrix cannot be read",
        ) from exc
    try:
        return GlobalTestMatrix.from_dict(json.loads(raw.decode("utf-8")))
    except TestMatrixConfigError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TestMatrixConfigError(
            "test_matrix.config_invalid",
            "global test matrix must be UTF-8 JSON",
        ) from exc


def _worker_selection(value: Any, index: int) -> TestWorkerSelection:
    if not isinstance(value, dict) or set(value) != {
        "worker_id",
        "platform_flavor",
        "source_mode",
    }:
        raise _invalid(f"test worker selection {index} has missing or unknown fields")
    fields = tuple(
        value[name] for name in ("worker_id", "platform_flavor", "source_mode")
    )
    if any(not isinstance(field, str) or not field for field in fields):
        raise _invalid(
            f"test worker selection {index} fields must be non-empty strings"
        )
    worker_id, platform_flavor, source_mode = fields
    if platform_flavor in _PLATFORMS:
        platform_flavor = f"flavor://literate-ai/os-{platform_flavor}"
    selection = TestWorkerSelection(worker_id, platform_flavor, source_mode)
    if selection.platform not in _PLATFORMS or source_mode not in _SOURCE_MODES:
        raise _invalid(
            f"test worker selection {index} platform or source mode is invalid"
        )
    return selection


def _optional_text(value: dict[str, Any], name: str) -> str | None:
    if name not in value:
        return None
    selected = value[name]
    if not isinstance(selected, str) or not selected.strip():
        raise _invalid(f"global test matrix field {name!r} must be a non-empty string")
    return selected.strip()


def _invalid(message: str) -> TestMatrixConfigError:
    return TestMatrixConfigError("test_matrix.config_invalid", message)


__all__ = [
    "GLOBAL_TEST_MATRIX_SCHEMA",
    "GlobalTestMatrix",
    "MAX_GLOBAL_TEST_MATRIX_BYTES",
    "TestMatrixConfigError",
    "TestWorkerSelection",
    "load_global_test_matrix",
]
