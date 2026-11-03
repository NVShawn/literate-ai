"""Reviewed Cargo test protocols; serialized declarations grant no permission.

An execution owner must reopen independent gate authority for exclusions and
guard measured executable/input bytes for commands. These values alone cannot
authorize either operation or assert that a test passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._validation import fail, fields, list_value, string_value
from .identity import ContentIdentity
from .paths import canonical_relative_posix_paths
from .repositories import RepositoryBuildCommand, RepositoryBuildOutput
from .retained_test_cases import validate_retained_test_cases


def _reason(value, label):
    string_value(value, label, max_length=2048)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail(label, "requires a printable review reason")


@dataclass(frozen=True, slots=True)
class RetainedCargoIgnoredCase:
    name: str
    reason: str | None

    def __post_init__(self):
        validate_retained_test_cases((self.name,))
        if self.reason is not None:
            _reason(self.reason, "retained.tests.ignored.reason")

    def to_dict(self):
        return {"name": self.name, "reason": self.reason}

    @classmethod
    def from_dict(cls, value):
        data = fields(value, path=cls.__name__, required=frozenset({"name", "reason"}))
        return cls(data["name"], data["reason"])


@dataclass(frozen=True, slots=True)
class RetainedCargoLibtestProtocol:
    must_run: tuple[str, ...]
    ignored: tuple[RetainedCargoIgnoredCase, ...]

    def __post_init__(self):
        validate_retained_test_cases(self.must_run)
        if (
            not isinstance(self.ignored, tuple)
            or len(self.ignored) > 100000
            or any(not isinstance(x, RetainedCargoIgnoredCase) for x in self.ignored)
        ):
            fail("retained.tests.ignored", "requires bounded reviewed ignored cases")
        names = tuple(x.name for x in self.ignored)
        validate_retained_test_cases(names)
        if set(names) & set(self.must_run) or len(names) + len(self.must_run) > 100000:
            fail("retained.tests.cases", "requires disjoint bounded case outcomes")

    @property
    def case_count(self):
        return len(self.must_run) + len(self.ignored)

    def to_dict(self):
        return {
            "kind": "libtest",
            "must_run": list(self.must_run),
            "ignored": [x.to_dict() for x in self.ignored],
        }

    @classmethod
    def from_dict(cls, value):
        data = fields(
            value,
            path=cls.__name__,
            required=frozenset({"kind", "must_run", "ignored"}),
        )
        if data["kind"] != "libtest":
            fail(cls.__name__, "requires the libtest protocol")
        run = list_value(data["must_run"], cls.__name__)
        ignored = list_value(data["ignored"], cls.__name__)
        if len(run) + len(ignored) > 100000:
            fail(cls.__name__, "case allowance exceeded")
        return cls(
            tuple(run), tuple(RetainedCargoIgnoredCase.from_dict(x) for x in ignored)
        )


@dataclass(frozen=True, slots=True)
class RetainedCargoCustomProtocol:
    command: RepositoryBuildCommand
    executable_identity: ContentIdentity
    inputs: tuple[RepositoryBuildOutput, ...]
    timeout_seconds: int
    maximum_output_bytes: int

    def __post_init__(self):
        if type(self.command) is not RepositoryBuildCommand or not isinstance(
            self.executable_identity, ContentIdentity
        ):
            fail(
                self.__class__.__name__,
                "requires exact command and executable identity",
            )
        if self.command.network:
            fail(self.__class__.__name__, "custom retained tests must remain offline")
        # Whole-token substitution only, by the owner of the attributed binary.
        if self.command.argv.count("{target_executable}") != 1 or any(
            ("{" in arg or "}" in arg) and arg != "{target_executable}"
            for arg in self.command.argv
        ):
            fail(self.__class__.__name__, "requires exactly one whole target token")
        if (
            not isinstance(self.inputs, tuple)
            or len(self.inputs) > 4096
            or any(type(x) is not RepositoryBuildOutput for x in self.inputs)
        ):
            fail(self.__class__.__name__, "requires bounded reviewed input identities")
        paths = tuple(x.path for x in self.inputs)
        if paths != tuple(sorted(paths)):
            fail(self.__class__.__name__, "requires inputs in canonical path order")
        canonical_relative_posix_paths(paths, label="custom Cargo test inputs")
        if (
            type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 10800
            or type(self.maximum_output_bytes) is not int
            or not 1 <= self.maximum_output_bytes <= 64 * 1024 * 1024
        ):
            fail(self.__class__.__name__, "requires finite command bounds")

    def to_dict(self):
        return {
            "kind": "custom-harness",
            "command": self.command.to_dict(),
            "executable_identity": self.executable_identity.to_dict(),
            "inputs": [x.to_dict() for x in self.inputs],
            "timeout_seconds": self.timeout_seconds,
            "maximum_output_bytes": self.maximum_output_bytes,
        }

    @classmethod
    def from_dict(cls, value):
        data = fields(
            value,
            path=cls.__name__,
            required=frozenset(
                {
                    "kind",
                    "command",
                    "executable_identity",
                    "inputs",
                    "timeout_seconds",
                    "maximum_output_bytes",
                }
            ),
        )
        if data["kind"] != "custom-harness":
            fail(cls.__name__, "requires the custom command protocol")
        inputs = list_value(data["inputs"], cls.__name__)
        if len(inputs) > 4096:
            fail(cls.__name__, "input allowance exceeded")
        return cls(
            RepositoryBuildCommand.from_dict(data["command"]),
            ContentIdentity.from_dict(data["executable_identity"]),
            tuple(RepositoryBuildOutput.from_dict(x) for x in inputs),
            data["timeout_seconds"],
            data["maximum_output_bytes"],
        )


@dataclass(frozen=True, slots=True)
class RetainedCargoExcludedProtocol:
    reason: str
    permission_identity: ContentIdentity

    def __post_init__(self):
        _reason(self.reason, "retained.tests.excluded.reason")
        if not isinstance(self.permission_identity, ContentIdentity):
            fail(self.__class__.__name__, "requires independently reopened permission")

    def to_dict(self):
        return {
            "kind": "excluded",
            "reason": self.reason,
            "permission_identity": self.permission_identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, value):
        data = fields(
            value,
            path=cls.__name__,
            required=frozenset({"kind", "reason", "permission_identity"}),
        )
        if data["kind"] != "excluded":
            fail(cls.__name__, "requires the exclusion protocol")
        return cls(
            data["reason"], ContentIdentity.from_dict(data["permission_identity"])
        )


def retained_cargo_protocol_from_dict(value: Any):
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        fail("retained.tests.protocol", "requires a tagged reviewed protocol")
    parser = {
        "libtest": RetainedCargoLibtestProtocol,
        "custom-harness": RetainedCargoCustomProtocol,
        "excluded": RetainedCargoExcludedProtocol,
    }.get(value["kind"])
    if parser is None:
        fail("retained.tests.protocol", "unsupported test protocol")
    return parser.from_dict(value)
