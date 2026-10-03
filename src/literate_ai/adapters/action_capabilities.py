"""Opt-in, bounded observation of command-worker lifecycle capabilities."""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_command_dispatch import command_worker_environment
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_observation_failure import observation_failure_message
from literate_ai.adapters.action_toolchains import MAX_WORKER_TOOLCHAINS
from literate_ai.adapters.action_transport import (
    action_receiver_command,
    supports_action_transport,
)
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import BuildError
from literate_ai.adapters.cache.filesystem import _read_regular_file
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts.execution_dispatch import (
    LIFECYCLE_ACTION_WIRE_PROTOCOL,
    ExecutionWorker,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)

# Bound one receiver observation, including start-up and native dependency
# inspection on slow hosted runners.
ACTION_OBSERVATION_TIMEOUT_SECONDS = 300
CAPABILITY_PROTOCOL = "literate-ai/action-capabilities@1"
MAX_CAPABILITY_BYTES = 32 * 1024
_MAX_PROBE_SECONDS = 60
_REQUEST = "literate-ai/action-capability-request@1"
_RESPONSE = "literate-ai/action-capability-response@1"
_SUPPORTED = (
    LifecycleActionKind.AUTHORIZE,
    LifecycleActionKind.BUILD_INTENT,
    LifecycleActionKind.INDEX,
    LifecycleActionKind.PLAN,
)


def _invalid():
    raise ActionWireError(
        "action_capability.invalid", "invalid action capability document"
    )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _load(content: bytes):
    if not isinstance(content, bytes) or len(content) > MAX_CAPABILITY_BYTES:
        _invalid()
    try:
        value = json.loads(content, object_pairs_hook=_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ActionWireError(
            "action_capability.invalid", "invalid capability JSON"
        ) from exc
    if not isinstance(value, dict):
        _invalid()
    return value


def decode_capability_request(content: bytes):
    value = _load(content)
    try:
        if (
            set(value) != {"schema", "worker_identity", "nonce", "deadline"}
            or value["schema"] != _REQUEST
            or not isinstance(value["nonce"], str)
            or re.fullmatch(r"[0-9a-f]{32}", value["nonce"]) is None
            or not isinstance(value["deadline"], dict)
            or set(value["deadline"]) != {"schema", "expires_at"}
            or value["deadline"]["schema"] != "literate-ai/lifecycle-action-deadline@1"
        ):
            _invalid()
        ContentIdentity.parse_uri(value["worker_identity"])
        deadline = ActionDispatchDeadline(
            datetime.fromisoformat(value["deadline"]["expires_at"])
        )
        if deadline.to_dict() != value["deadline"]:
            _invalid()
        deadline.remaining()
        return value, deadline
    except (ValueError, TypeError, KeyError) as exc:
        raise ActionWireError(
            "action_capability.invalid", "invalid capability request"
        ) from exc


def receiver_code_identity(
    *, package_root: Path | None = None, deadline: ActionDispatchDeadline | None = None
) -> ContentIdentity:
    """Identify Python receiver code, not a native-environment attestation."""
    root = Path(__file__).resolve().parents[1] if package_root is None else package_root
    require_safe_directory(root)
    files = []
    total = 0
    entries = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        require_safe_directory(directory)
        with os.scandir(directory) as children:
            for child in children:
                if deadline is not None:
                    deadline.remaining()
                entries += 1
                if entries > 65536 or child.is_symlink():
                    _invalid()
                path = Path(child.path)
                if child.is_dir(follow_symlinks=False):
                    if child.name != "__pycache__":
                        pending.append(path)
                    continue
                if not child.name.endswith(".py"):
                    continue
                if len(files) >= 4096:
                    _invalid()
                content = _read_regular_file(
                    path,
                    maximum_bytes=64 * 1024 * 1024 - total,
                    code="action_capability.code_invalid",
                )
                total += len(content)
                files.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "identity": record_identity(content).uri,
                    }
                )
    if not files:
        _invalid()
    return canonical_identity(
        {
            "schema": "literate-ai/action-receiver-code@1",
            "files": sorted(files, key=lambda item: item["path"]),
        }
    )


def encode_capability_response(
    request: dict,
    deadline: ActionDispatchDeadline,
    worker_identity: ContentIdentity,
    *,
    http_source: bool,
    build_profile: ContentIdentity | None = None,
    build_toolchains: tuple[ContentIdentity, ...] = (),
    build_standard_tools: ContentIdentity | None = None,
    test_profile: ContentIdentity | None = None,
    test_toolchains: tuple[ContentIdentity, ...] = (),
    test_standard_tools: ContentIdentity | None = None,
    execute_profile: ContentIdentity | None = None,
    execute_toolchains: tuple[ContentIdentity, ...] = (),
    execute_standard_tools: ContentIdentity | None = None,
    accept_profile: ContentIdentity | None = None,
) -> bytes:
    if request["worker_identity"] != worker_identity.uri:
        raise ActionWireError(
            "action_capability.worker_mismatch", "receiver binds another worker"
        )
    deadline.remaining()
    code_identity = receiver_code_identity(deadline=deadline)
    python_content = _read_regular_file(
        Path(sys.executable).resolve(strict=True),
        maximum_bytes=64 * 1024 * 1024,
        code="action_capability.runtime_invalid",
    )
    build = _phase_facts(build_profile, build_toolchains, build_standard_tools)
    test = _phase_facts(test_profile, test_toolchains, test_standard_tools)
    execute = _phase_facts(execute_profile, execute_toolchains, execute_standard_tools)
    accept = _accept_facts(accept_profile)
    result = canonical_json_bytes(
        {
            "schema": _RESPONSE,
            "protocol": LIFECYCLE_ACTION_WIRE_PROTOCOL,
            "request_identity": canonical_identity(request).uri,
            "worker_identity": worker_identity.uri,
            "receiver_identity": code_identity.uri,
            "python_identity": record_identity(python_content).uri,
            "python_version": list(sys.version_info[:3]),
            "actions": sorted(
                kind.value
                for kind in (
                    *_SUPPORTED,
                    *((LifecycleActionKind.BUILD,) if build else ()),
                    *((LifecycleActionKind.TEST,) if test else ()),
                    *((LifecycleActionKind.EXECUTE,) if execute else ()),
                    *((LifecycleActionKind.ACCEPT,) if accept else ()),
                )
            ),
            **({"build": build} if build else {}),
            **({"test": test} if test else {}),
            **({"execute": execute} if execute else {}),
            **({"accept": accept} if accept else {}),
            "source_handoff": ["filesystem-cas", "http-cas"]
            if http_source
            else ["filesystem-cas"],
        }
    )
    deadline.remaining()
    if len(result) > MAX_CAPABILITY_BYTES:
        _invalid()
    return result


def _accept_facts(profile):
    if profile is None:
        return None
    if not isinstance(profile, ContentIdentity):
        _invalid()
    return {"profile_identity": profile.uri}


def _phase_facts(profile, toolchains, standard_tools=None):
    if profile is None and toolchains == () and standard_tools is None:
        return None
    if (
        not isinstance(profile, ContentIdentity)
        or not isinstance(toolchains, tuple)
        or not 1 <= len(toolchains) <= MAX_WORKER_TOOLCHAINS
        or any(not isinstance(item, ContentIdentity) for item in toolchains)
        or toolchains != tuple(sorted(set(toolchains), key=lambda item: item.uri))
        or (
            standard_tools is not None
            and not isinstance(standard_tools, ContentIdentity)
        )
    ):
        _invalid()
    return {
        "profile_identity": profile.uri,
        "toolchain_identities": [item.uri for item in toolchains],
        **(
            {"standard_tools_identity": standard_tools.uri}
            if standard_tools is not None
            else {}
        ),
    }


@dataclass(frozen=True, slots=True)
class ActionWorkerCapabilities:
    request_identity: ContentIdentity
    worker_identity: ContentIdentity
    receiver_identity: ContentIdentity
    python_identity: ContentIdentity
    python_version: tuple[int, int, int]
    actions: tuple[LifecycleActionKind, ...]
    source_handoff: tuple[str, ...]
    observed_at: datetime
    build_profile: ContentIdentity | None = None
    build_toolchains: tuple[ContentIdentity, ...] = ()
    build_standard_tools: ContentIdentity | None = None
    test_profile: ContentIdentity | None = None
    test_toolchains: tuple[ContentIdentity, ...] = ()
    test_standard_tools: ContentIdentity | None = None
    execute_profile: ContentIdentity | None = None
    execute_toolchains: tuple[ContentIdentity, ...] = ()
    execute_standard_tools: ContentIdentity | None = None
    accept_profile: ContentIdentity | None = None

    def __post_init__(self):
        if (
            any(
                not isinstance(item, ContentIdentity)
                for item in (
                    self.request_identity,
                    self.worker_identity,
                    self.receiver_identity,
                    self.python_identity,
                )
            )
            or not isinstance(self.python_version, tuple)
            or len(self.python_version) != 3
            or any(type(item) is not int or item < 0 for item in self.python_version)
            or self.python_version < (3, 11, 0)
            or not isinstance(self.actions, tuple)
            or not self.actions
            or any(
                not isinstance(item, LifecycleActionKind)
                or item
                not in (
                    *_SUPPORTED,
                    LifecycleActionKind.BUILD,
                    LifecycleActionKind.TEST,
                    LifecycleActionKind.EXECUTE,
                    LifecycleActionKind.ACCEPT,
                )
                for item in self.actions
            )
            or self.actions != tuple(sorted(set(self.actions)))
            or self.source_handoff
            not in (("filesystem-cas",), ("filesystem-cas", "http-cas"))
            or not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
        ):
            _invalid()

        build = _phase_facts(
            self.build_profile, self.build_toolchains, self.build_standard_tools
        )
        if (LifecycleActionKind.BUILD in self.actions) != (build is not None):
            _invalid()
        test = _phase_facts(
            self.test_profile, self.test_toolchains, self.test_standard_tools
        )
        if (LifecycleActionKind.TEST in self.actions) != (test is not None):
            _invalid()
        execute = _phase_facts(
            self.execute_profile, self.execute_toolchains, self.execute_standard_tools
        )
        if (LifecycleActionKind.EXECUTE in self.actions) != (execute is not None):
            _invalid()

        accept = _accept_facts(self.accept_profile)
        if (LifecycleActionKind.ACCEPT in self.actions) != (accept is not None):
            _invalid()

    @property
    def capability_identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/action-capability-facts@1",
                "worker_identity": self.worker_identity.uri,
                "receiver_identity": self.receiver_identity.uri,
                "python_identity": self.python_identity.uri,
                "python_version": list(self.python_version),
                "actions": [item.value for item in self.actions],
                "source_handoff": list(self.source_handoff),
                **(
                    {"accept": _accept_facts(self.accept_profile)}
                    if self.accept_profile is not None
                    else {}
                ),
                **(
                    {
                        "build": _phase_facts(
                            self.build_profile,
                            self.build_toolchains,
                            self.build_standard_tools,
                        )
                    }
                    if self.build_profile is not None
                    else {}
                ),
                **(
                    {
                        "test": _phase_facts(
                            self.test_profile,
                            self.test_toolchains,
                            self.test_standard_tools,
                        )
                    }
                    if self.test_profile is not None
                    else {}
                ),
                **(
                    {
                        "execute": _phase_facts(
                            self.execute_profile,
                            self.execute_toolchains,
                            self.execute_standard_tools,
                        )
                    }
                    if self.execute_profile is not None
                    else {}
                ),
            }
        )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/action-capability-observation@1",
                "request_identity": self.request_identity.uri,
                "capability_identity": self.capability_identity.uri,
                "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            }
        )

    def require_current(
        self,
        worker: ExecutionWorker,
        expected_receiver: ContentIdentity,
        *,
        now: datetime | None = None,
        maximum_age: timedelta = timedelta(minutes=5),
    ) -> None:
        current = datetime.now(UTC) if now is None else now
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or not isinstance(maximum_age, timedelta)
        ):
            _invalid()
        age = current - self.observed_at
        if (
            worker.identity != self.worker_identity
            or self.receiver_identity != expected_receiver
            or maximum_age <= timedelta(0)
            or age < timedelta(0)
            or age > maximum_age
        ):
            raise ActionWireError(
                "action_capability.stale",
                "action capability observation is not current",
            )


def decode_capability_response(
    content: bytes, request_content: bytes, expected_receiver: ContentIdentity
) -> ActionWorkerCapabilities:
    request, deadline = decode_capability_request(request_content)
    value = _load(content)
    try:
        if (
            set(value) - {"build", "test", "execute", "accept"}
            != {
                "schema",
                "protocol",
                "request_identity",
                "worker_identity",
                "receiver_identity",
                "python_identity",
                "python_version",
                "actions",
                "source_handoff",
            }
            or value["schema"] != _RESPONSE
            or value["protocol"] != LIFECYCLE_ACTION_WIRE_PROTOCOL
        ):
            _invalid()
        if (
            value["request_identity"] != record_identity(request_content).uri
            or value["worker_identity"] != request["worker_identity"]
            or value["receiver_identity"] != expected_receiver.uri
        ):
            raise ActionWireError(
                "action_capability.mismatch",
                "capability response differs from the exact request or receiver",
            )
        if (
            not isinstance(value["python_version"], list)
            or not isinstance(value["actions"], list)
            or not isinstance(value["source_handoff"], list)
        ):
            _invalid()
        build = value.get("build")
        test = value.get("test")
        execute = value.get("execute")
        for phase in ("build", "test", "execute"):
            facts = value.get(phase)
            if phase in value and (
                not isinstance(facts, dict)
                or set(facts) - {"standard_tools_identity"}
                != {"profile_identity", "toolchain_identities"}
                or not isinstance(facts["toolchain_identities"], list)
            ):
                _invalid()
        accept = value.get("accept")
        if "accept" in value and (
            not isinstance(accept, dict) or set(accept) != {"profile_identity"}
        ):
            _invalid()
        observed = ActionWorkerCapabilities(
            ContentIdentity.parse_uri(value["request_identity"]),
            ContentIdentity.parse_uri(value["worker_identity"]),
            ContentIdentity.parse_uri(value["receiver_identity"]),
            ContentIdentity.parse_uri(value["python_identity"]),
            tuple(value["python_version"]),
            tuple(LifecycleActionKind(item) for item in value["actions"]),
            tuple(value["source_handoff"]),
            datetime.now(UTC),
            ContentIdentity.parse_uri(build["profile_identity"]) if build else None,
            tuple(
                ContentIdentity.parse_uri(item)
                for item in build["toolchain_identities"]
            )
            if build
            else (),
            ContentIdentity.parse_uri(build["standard_tools_identity"])
            if build and "standard_tools_identity" in build
            else None,
            test_profile=ContentIdentity.parse_uri(test["profile_identity"])
            if test
            else None,
            test_toolchains=tuple(
                ContentIdentity.parse_uri(item) for item in test["toolchain_identities"]
            )
            if test
            else (),
            test_standard_tools=ContentIdentity.parse_uri(
                test["standard_tools_identity"]
            )
            if test and "standard_tools_identity" in test
            else None,
            accept_profile=ContentIdentity.parse_uri(accept["profile_identity"])
            if accept
            else None,
            execute_profile=ContentIdentity.parse_uri(execute["profile_identity"])
            if execute
            else None,
            execute_toolchains=tuple(
                ContentIdentity.parse_uri(item)
                for item in execute["toolchain_identities"]
            )
            if execute
            else (),
            execute_standard_tools=ContentIdentity.parse_uri(
                execute["standard_tools_identity"]
            )
            if execute and "standard_tools_identity" in execute
            else None,
        )
        deadline.remaining()
        return observed
    except (ValueError, TypeError, KeyError) as exc:
        raise ActionWireError(
            "action_capability.invalid", "invalid capability response"
        ) from exc


def probe_command_action_capabilities(
    worker: ExecutionWorker,
    deadline: ActionDispatchDeadline,
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> ActionWorkerCapabilities:
    if not supports_action_transport(worker):
        raise ActionWireError(
            "action_capability.not_declared",
            "worker has not opted into command action probing",
        )
    deadline.remaining()
    deadline = ActionDispatchDeadline(
        min(
            deadline.expires_at,
            datetime.now(UTC) + timedelta(seconds=_MAX_PROBE_SECONDS),
        )
    )
    request_content = canonical_json_bytes(
        {
            "schema": _REQUEST,
            "worker_identity": worker.identity.uri,
            "nonce": secrets.token_hex(16),
            "deadline": deadline.to_dict(),
        }
    )
    expected = receiver_code_identity(deadline=deadline)
    response = run_command_observation(
        worker,
        deadline,
        request_content,
        cwd=cwd,
        environment=environment,
        mode="--describe",
        protocol=CAPABILITY_PROTOCOL,
    )
    return decode_capability_response(response, request_content, expected)


def run_command_observation(
    worker: ExecutionWorker,
    deadline: ActionDispatchDeadline,
    request_content: bytes,
    *,
    cwd: Path,
    environment: Mapping[str, str] | None,
    mode: str,
    protocol: str,
    stdout_limit_bytes: int = MAX_CAPABILITY_BYTES,
) -> bytes:
    """Execute one explicitly opted-in read-only receiver operation within bounds."""
    if (
        not supports_action_transport(worker)
        or not isinstance(request_content, bytes)
        or len(request_content) > MAX_CAPABILITY_BYTES
        or mode
        not in (
            "--describe",
            "--describe-hardware",
            "--describe-tools",
            "--describe-tool-dependencies",
            "--verify-tool-selectors",
        )
        or type(stdout_limit_bytes) is not int
        or not 1
        <= stdout_limit_bytes
        <= (
            4 * 1024 * 1024 + 65536 if mode == "--describe-tool-dependencies" else 65536
        )
    ):
        raise ActionWireError(
            "action_capability.not_declared",
            "worker observation is not declared or bounded",
        )
    bindings = command_worker_environment(
        worker,
        os.environ if environment is None else environment,
        protocol=protocol,
    )
    with tempfile.TemporaryDirectory(prefix="litai-capability-") as temporary:
        command = action_receiver_command(worker, deadline, mode=mode)
        stdin = request_content
        if "{request_file}" in command:
            request_file = Path(temporary) / "request.json"
            request_file.write_bytes(request_content)
            request_file.chmod(0o600)
            command = tuple(
                str(request_file) if item == "{request_file}" else item
                for item in command
            )
            stdin = None
        try:
            completed = run_bounded_process(
                command,
                cwd=cwd,
                environment=bindings,
                timeout_seconds=deadline.remaining(),
                input_bytes=stdin,
                input_limit_bytes=MAX_CAPABILITY_BYTES,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=4096,
                interrupt_guard=deadline.remaining,
                terminate_descendants=True,
                poll_interval_seconds=0.1,
                error_prefix="action_capability",
                trace=False,
            )
        except (BuildError, OSError) as exc:
            raise ActionWireError(
                "action_capability.probe_failed",
                "worker capability probe failed its bounds",
            ) from exc
    if completed.returncode != 0:
        raise ActionWireError(
            "action_capability.probe_failed",
            observation_failure_message(completed.stdout)
            or "worker capability probe refused the request",
        )
    return completed.stdout
