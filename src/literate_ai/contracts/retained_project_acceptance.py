"""Verifier-owned executable cases for retained exports, not release receipts."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import ClassVar

from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_paths
from .retained_project_lifecycle import RetainedProjectProfile, _fields, _name

MAX_ACCEPTANCE_STREAM_BYTES = 64 * 1024


def _decode(value: object) -> bytes:
    if not isinstance(value, str) or len(value) > 4 * (
        (MAX_ACCEPTANCE_STREAM_BYTES + 2) // 3
    ):
        raise ValueError("retained.acceptance.stream-budget")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("retained.acceptance.stream-encoding") from None
    if base64.b64encode(data).decode("ascii") != value:
        raise ValueError("retained.acceptance.stream-encoding")
    return data


@dataclass(frozen=True, slots=True)
class RetainedProjectAcceptanceCase:
    case_id: str
    action_id: str
    executable: str
    argv: tuple[str, ...]
    stdin: bytes
    expected_exit: int
    expected_stdout: bytes
    expected_stderr: bytes
    timeout_seconds: int

    def __post_init__(self):
        _name(self.case_id)
        _name(self.action_id)
        canonical_relative_posix_paths(
            (self.executable,), label="acceptance executable"
        )
        if (
            type(self.argv) is not tuple
            or len(self.argv) > 32
            or any(
                not isinstance(arg, str) or len(arg) > 4096 or "\x00" in arg
                for arg in self.argv
            )
        ):
            raise ValueError("retained.acceptance.argv-invalid")
        for data in (self.stdin, self.expected_stdout, self.expected_stderr):
            if type(data) is not bytes or len(data) > MAX_ACCEPTANCE_STREAM_BYTES:
                raise ValueError("retained.acceptance.stream-budget")
        if type(self.expected_exit) is not int or not 0 <= self.expected_exit <= 255:
            raise ValueError("retained.acceptance.exit-invalid")
        if (
            type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 300
        ):
            raise ValueError("retained.acceptance.timeout-invalid")

    def to_dict(self):
        return {
            "id": self.case_id,
            "action": self.action_id,
            "executable": self.executable,
            "argv": list(self.argv),
            "stdin": base64.b64encode(self.stdin).decode("ascii"),
            "expected_exit": self.expected_exit,
            "expected_stdout": base64.b64encode(self.expected_stdout).decode("ascii"),
            "expected_stderr": base64.b64encode(self.expected_stderr).decode("ascii"),
            "timeout_seconds": self.timeout_seconds,
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        value = _fields(
            value,
            {
                "id",
                "action",
                "executable",
                "argv",
                "stdin",
                "expected_exit",
                "expected_stdout",
                "expected_stderr",
                "timeout_seconds",
            },
        )
        if not isinstance(value["argv"], list):
            raise ValueError("retained.acceptance.argv-invalid")
        return cls(
            value["id"],
            value["action"],
            value["executable"],
            tuple(value["argv"]),
            _decode(value["stdin"]),
            value["expected_exit"],
            _decode(value["expected_stdout"]),
            _decode(value["expected_stderr"]),
            value["timeout_seconds"],
        )


@dataclass(frozen=True, slots=True)
class RetainedProjectAcceptancePolicy:
    component_locks: tuple[ContentIdentity, ...]
    cases: tuple[RetainedProjectAcceptanceCase, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-project-acceptance-policy@1"

    def __post_init__(self):
        if (
            type(self.component_locks) is not tuple
            or not self.component_locks
            or any(
                not isinstance(item, ContentIdentity) for item in self.component_locks
            )
            or len(set(self.component_locks)) != len(self.component_locks)
        ):
            raise ValueError("retained.acceptance.component-locks-required")
        if (
            type(self.cases) is not tuple
            or not 1 <= len(self.cases) <= 256
            or any(
                not isinstance(case, RetainedProjectAcceptanceCase)
                for case in self.cases
            )
            or len({case.case_id for case in self.cases}) != len(self.cases)
        ):
            raise ValueError("retained.acceptance.unique-cases-required")

    def require_profile(self, profile: RetainedProjectProfile, locks):
        if self.component_locks != locks:
            raise ValueError("retained.acceptance.component-locks-changed")
        expected = {
            action.action_id: set(action.inventory)
            for action in profile.actions
            if action.stage == "accept"
        }
        observed = {}
        for case in self.cases:
            observed.setdefault(case.action_id, set()).add(case.case_id)
            if not any(
                case.executable == output or case.executable.startswith(output + "/")
                for action in profile.actions
                if action.stage == "build"
                for output in action.outputs
            ) or not any(
                case.executable == path or case.executable.startswith(path + "/")
                for path in profile.package_inventory
            ):
                raise ValueError("retained.acceptance.executable-outside-build-package")
        if expected != observed:
            raise ValueError("retained.acceptance.case-inventory-mismatch")

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "component_locks": [item.uri for item in self.component_locks],
            "cases": [case.to_dict() for case in self.cases],
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"schema", "component_locks", "cases"})
        if (
            value["schema"] != cls.SCHEMA
            or not isinstance(value["component_locks"], list)
            or not isinstance(value["cases"], list)
        ):
            raise ValueError("retained.acceptance.policy-invalid")
        return cls(
            tuple(ContentIdentity.parse_uri(item) for item in value["component_locks"]),
            tuple(
                RetainedProjectAcceptanceCase.from_dict(case) for case in value["cases"]
            ),
        )
