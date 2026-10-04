"""Bounded data observations of worker-owned Standard tools, never local launchers."""

import json
import re
from dataclasses import dataclass

from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)

MAX_TOOL_OBSERVATION_BYTES = 64 * 1024
_ROLES = frozenset(
    (
        "python",
        "node",
        "npm",
        "rust",
        "go",
        "cpp",
        "nvcc",
        "swift",
        "bazel",
        "make",
        "cargo",
        "cmake",
        "zig",
        "zig-cc",
    )
)
_SCHEMA = "literate-ai/standard-worker-tool-observations@1"


def _text(value, maximum, *, empty=False):
    if (
        not isinstance(value, str)
        or (not value and not empty)
        or len(value) > maximum
        or "\0" in value
    ):
        raise ValueError("invalid bounded tool observation text")


@dataclass(frozen=True)
class StandardToolObservation:
    role: str
    toolchain_identity: ContentIdentity
    command: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    version: str
    version_info: tuple[int, int, int] | None
    node_identity: ContentIdentity | None = None

    def __post_init__(self):
        if not isinstance(self.role, str) or self.role not in _ROLES:
            raise ValueError("unknown Standard tool role")
        if not isinstance(self.toolchain_identity, ContentIdentity):
            raise TypeError("tool observation requires a content identity")
        if not isinstance(self.command, tuple) or not 1 <= len(self.command) <= 64:
            raise ValueError("tool observation requires a bounded command")
        for token in self.command:
            _text(token, 4096)
        _text(self.version, 4096)
        if self.version_info is not None and (
            not isinstance(self.version_info, tuple)
            or len(self.version_info) != 3
            or any(
                type(part) is not int or not 0 <= part <= 2**31 - 1
                for part in self.version_info
            )
        ):
            raise ValueError("invalid observed version tuple")
        if not isinstance(self.environment, tuple) or len(self.environment) > 128:
            raise ValueError("invalid tool environment")
        names = []
        for pair in self.environment:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("invalid tool environment pair")
            name, value = pair
            _text(name, 128)
            _text(value, 8192, empty=True)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
                raise ValueError("invalid tool environment name")
            names.append(name.casefold())
        if len(set(names)) != len(names) or self.environment != tuple(
            sorted(self.environment)
        ):
            raise ValueError("tool environment must be unique and canonical")
        if (self.role == "npm") != isinstance(self.node_identity, ContentIdentity):
            raise ValueError("only npm requires a Node tool identity")
        if self.role != "npm" and self.node_identity is not None:
            raise ValueError("non-npm tool cannot bind Node")

    def to_dict(self):
        return dict(
            role=self.role,
            toolchain_identity=self.toolchain_identity.uri,
            command=list(self.command),
            environment=[list(pair) for pair in self.environment],
            version=self.version,
            version_info=None if self.version_info is None else list(self.version_info),
            node_identity=None
            if self.node_identity is None
            else self.node_identity.uri,
        )


@dataclass(frozen=True)
class StandardToolObservations:
    platform: str
    tools: tuple[StandardToolObservation, ...]

    def __post_init__(self):
        if self.platform not in ("linux", "macos", "windows"):
            raise ValueError("invalid observed target platform")
        if (
            not isinstance(self.tools, tuple)
            or not 1 <= len(self.tools) <= len(_ROLES)
            or any(not isinstance(tool, StandardToolObservation) for tool in self.tools)
        ):
            raise ValueError("invalid bounded Standard tool inventory")
        roles = tuple(tool.role for tool in self.tools)
        if roles != tuple(sorted(set(roles))):
            raise ValueError("tool roles must be unique and canonical")
        by_role = {tool.role: tool for tool in self.tools}
        by_identity = {}
        for tool in self.tools:
            metadata = tool.to_dict()
            del metadata["role"]
            previous = by_identity.setdefault(tool.toolchain_identity, metadata)
            if previous != metadata:
                raise ValueError("one tool identity has inconsistent observations")
        if "npm" in by_role and (
            "node" not in by_role
            or by_role["npm"].node_identity != by_role["node"].toolchain_identity
        ):
            raise ValueError("npm observation differs from selected Node")
        if len(self.to_bytes()) > MAX_TOOL_OBSERVATION_BYTES:
            raise ValueError("tool observation document exceeds bound")

    def to_bytes(self):
        return canonical_json_bytes(
            dict(
                schema=_SCHEMA,
                platform=self.platform,
                tools=[tool.to_dict() for tool in self.tools],
            )
        )

    @property
    def identity(self):
        return canonical_identity(json.loads(self.to_bytes()))

    @classmethod
    def from_bytes(cls, content):
        if not isinstance(content, bytes) or len(content) > MAX_TOOL_OBSERVATION_BYTES:
            raise ValueError("tool observation document exceeds bound")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate observation key")
                result[key] = value
            return result

        try:
            value = json.loads(content, object_pairs_hook=pairs)
            if (
                not isinstance(value, dict)
                or set(value) != {"schema", "platform", "tools"}
                or value["schema"] != _SCHEMA
            ):
                raise ValueError("invalid observation envelope")
            if not isinstance(value["tools"], list) or len(value["tools"]) > len(
                _ROLES
            ):
                raise ValueError("invalid observation inventory")
            tools = []
            for tool in value["tools"]:
                if (
                    not isinstance(tool, dict)
                    or set(tool)
                    != {
                        "role",
                        "toolchain_identity",
                        "command",
                        "environment",
                        "version",
                        "version_info",
                        "node_identity",
                    }
                    or not isinstance(tool["command"], list)
                    or not isinstance(tool["environment"], list)
                ):
                    raise ValueError("invalid observation record")
                if any(not isinstance(pair, list) for pair in tool["environment"]):
                    raise ValueError("invalid environment record")
                if tool["version_info"] is not None and not isinstance(
                    tool["version_info"], list
                ):
                    raise ValueError("invalid version record")
                tools.append(
                    StandardToolObservation(
                        tool["role"],
                        ContentIdentity.parse_uri(tool["toolchain_identity"]),
                        tuple(tool["command"]),
                        tuple(tuple(pair) for pair in tool["environment"]),
                        tool["version"],
                        None
                        if tool["version_info"] is None
                        else tuple(tool["version_info"]),
                        None
                        if tool["node_identity"] is None
                        else ContentIdentity.parse_uri(tool["node_identity"]),
                    )
                )
            result = cls(value["platform"], tuple(tools))
            if result.to_bytes() != content:
                raise ValueError("tool observation must use canonical bytes")
            return result
        except (TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise ValueError("invalid tool observation document") from exc


def capture_standard_tool_observations(platform, tools, *, require_current):
    """Capture private live tools; the transport owns the worker/deadline guard."""
    require_current()
    if not 1 <= len(tools) <= len(_ROLES) or any(role not in _ROLES for role in tools):
        raise ValueError("invalid bounded Standard tool inventory")
    records = []
    for role, tool in sorted(tools.items()):
        require_current()
        tool.require_unchanged()
        version = tool.version
        missing = object()
        numbers = getattr(tool, "version_info", missing)
        if numbers is missing:
            numbers = (
                tuple(map(int, version.split(".")))
                if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
                else None
            )
        elif numbers is not None:
            numbers = tuple(numbers[:3])
        records.append(
            StandardToolObservation(
                role,
                ContentIdentity.parse_uri(tool.identity),
                tuple(tool.command),
                tuple(sorted(getattr(tool, "environment", ()))),
                version,
                numbers,
                ContentIdentity.parse_uri(tool.node.identity)
                if role == "npm"
                else None,
            )
        )
        tool.require_unchanged()
    result = StandardToolObservations(platform, tuple(records))
    for tool in tools.values():
        tool.require_unchanged()
    require_current()
    return result
