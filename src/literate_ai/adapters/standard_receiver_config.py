"""Private operator configuration for the reference Standard action receiver.

One bounded JSON document names everything a request may not select: worker
storage, enabled phases, exact host tools, the child environment and, for
FINALIZE, the grant path and verifier-owned oracle. Its byte identity binds the
measured child launchers, so any change requires a fresh receiver startup.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from literate_ai.contracts import ContentIdentity, canonical_identity

STANDARD_RECEIVER_SCHEMA = "literate-ai/standard-receiver@1"
MAX_STANDARD_RECEIVER_CONFIG_BYTES = 65536
RECEIVER_PHASES = ("BUILD", "TEST", "EXECUTE", "ACCEPT", "PACKAGE", "FINALIZE")
CONTRACT_POLICIES = ("portable-starter@1",)
_TOOLS = ("python", "make")


class StandardReceiverConfigError(ValueError):
    """The private receiver configuration is missing, malformed or unsafe."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StandardReceiverConfigError("duplicate configuration field")
        result[key] = value
    return result


def _absolute(value, field):
    if not isinstance(value, str) or not value:
        raise StandardReceiverConfigError(f"{field} must be an absolute path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise StandardReceiverConfigError(f"{field} must be an absolute path")
    return path


def _argv(value, field):
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise StandardReceiverConfigError(f"{field} must be a non-empty argv")
    _absolute(value[0], field)
    return tuple(value)


@dataclass(frozen=True)
class StandardReceiverFinalize:
    grant_path: Path
    oracle_component: str
    oracle_path: Path


@dataclass(frozen=True)
class StandardReceiverConfig:
    path: Path
    identity: ContentIdentity
    cas: Path
    workspace: Path
    phases: frozenset
    tools: MappingProxyType
    child_environment: MappingProxyType
    contract_policy: str
    finalize: StandardReceiverFinalize | None
    source_cas_url: str | None
    source_token_env: str | None
    allow_http: bool
    dependency_cache: Path | None = None

    def require_unchanged(self):
        """Refuse a configuration file that changed after startup."""
        if load_standard_receiver_config(self.path).identity != self.identity:
            raise StandardReceiverConfigError("receiver configuration changed")


def load_standard_receiver_config(path):
    path = _absolute(str(path), "configuration path")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise StandardReceiverConfigError("configuration is unreadable") from exc
    if not 0 < len(content) <= MAX_STANDARD_RECEIVER_CONFIG_BYTES:
        raise StandardReceiverConfigError("configuration size is out of bounds")
    try:
        document = json.loads(content, object_pairs_hook=_unique)
    except (ValueError, RecursionError) as exc:
        raise StandardReceiverConfigError("configuration is not JSON") from exc
    required = {
        "schema",
        "cas",
        "workspace",
        "phases",
        "tools",
        "child_environment",
        "contract_policy",
    }
    optional = {"finalize", "source", "dependency_cache"}
    if (
        not isinstance(document, dict)
        or not required <= set(document) <= required | optional
        or document["schema"] != STANDARD_RECEIVER_SCHEMA
    ):
        raise StandardReceiverConfigError("configuration fields are invalid")
    phases = document["phases"]
    if (
        not isinstance(phases, list)
        or not phases
        or len(set(phases)) != len(phases)
        or any(item not in RECEIVER_PHASES for item in phases)
    ):
        raise StandardReceiverConfigError("phases must name receiver phases once")
    tools = document["tools"]
    if not isinstance(tools, dict) or set(tools) != set(_TOOLS):
        raise StandardReceiverConfigError("tools must name exactly python and make")
    environment = document["child_environment"]
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and key and isinstance(value, str)
        for key, value in environment.items()
    ):
        raise StandardReceiverConfigError("child_environment must map text to text")
    if document["contract_policy"] not in CONTRACT_POLICIES:
        raise StandardReceiverConfigError("unsupported contract policy")
    finalize = document.get("finalize")
    if ("FINALIZE" in phases) != (finalize is not None):
        raise StandardReceiverConfigError("FINALIZE requires exactly its settings")
    if finalize is not None:
        if (
            not isinstance(finalize, dict)
            or set(finalize) != {"grant_path", "oracle"}
            or not isinstance(finalize["oracle"], dict)
            or set(finalize["oracle"]) != {"component", "path"}
            or not isinstance(finalize["oracle"]["component"], str)
            or not finalize["oracle"]["component"]
        ):
            raise StandardReceiverConfigError("finalize settings are invalid")
        finalize = StandardReceiverFinalize(
            _absolute(finalize["grant_path"], "finalize.grant_path"),
            finalize["oracle"]["component"],
            _absolute(finalize["oracle"]["path"], "finalize.oracle.path"),
        )
    source = document.get("source")
    if source is not None and (
        not isinstance(source, dict)
        or not {"url"} <= set(source) <= {"url", "token_env", "allow_http"}
        or not isinstance(source["url"], str)
        or not isinstance(source.get("token_env", ""), str)
        or not isinstance(source.get("allow_http", False), bool)
    ):
        raise StandardReceiverConfigError("source settings are invalid")
    cas = _absolute(document["cas"], "cas")
    workspace = _absolute(document["workspace"], "workspace")

    def inside_action_storage(path: Path) -> bool:
        # Compare resolved paths too, so a symlink cannot reach action storage.
        return any(
            candidate.is_relative_to(root)
            for candidate in (path, path.resolve())
            for storage in (workspace, cas)
            for root in (storage, storage.resolve())
        )

    # Grants, the acceptance oracle and cached dependency facts decide what runs
    # and what passes, so no action may be able to write them.
    if finalize is not None:
        if inside_action_storage(finalize.grant_path):
            raise StandardReceiverConfigError(
                "grants must be outside the workspace and CAS"
            )
        if inside_action_storage(finalize.oracle_path):
            raise StandardReceiverConfigError(
                "the FINALIZE oracle must be outside the workspace and CAS"
            )
    dependency_cache = document.get("dependency_cache")
    if dependency_cache is not None:
        dependency_cache = _absolute(dependency_cache, "dependency_cache")
        if inside_action_storage(dependency_cache):
            raise StandardReceiverConfigError(
                "dependency_cache must be outside the workspace and CAS"
            )
    return StandardReceiverConfig(
        path=path,
        identity=canonical_identity(
            {"schema": STANDARD_RECEIVER_SCHEMA, "bytes": content.hex()}
        ),
        cas=cas,
        workspace=workspace,
        phases=frozenset(phases),
        tools=MappingProxyType(
            {name: _argv(tools[name], f"tools.{name}") for name in _TOOLS}
        ),
        child_environment=MappingProxyType(dict(environment)),
        contract_policy=document["contract_policy"],
        finalize=finalize,
        source_cas_url=None if source is None else source["url"],
        source_token_env=None if source is None else source.get("token_env") or None,
        allow_http=False if source is None else source.get("allow_http", False),
        dependency_cache=dependency_cache,
    )
