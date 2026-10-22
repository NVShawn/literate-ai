"""Run withheld verifier cases against sealed retained exports, never issue receipts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.retained_project_dependencies import (
    retained_export_dependency_identity,
)
from literate_ai.adapters.retained_project_filesystem import (
    RetainedFilesystemObservation,
    RetainedFilesystemWorkspace,
)
from literate_ai.adapters.retained_project_metadata import (
    read_retained_project_document,
)
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.retained_project import (
    RetainedProjectManifest,
    RetainedProjectMember,
)
from literate_ai.contracts.retained_project_acceptance import (
    MAX_ACCEPTANCE_STREAM_BYTES,
    RetainedProjectAcceptancePolicy,
)
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectPlan,
    RetainedProjectProfile,
)

ACCEPTANCE_POLICY_PATH = "verification/retained-project-acceptance.json"


class RetainedProjectAcceptanceError(ValueError):
    def __init__(self, message, observations=()):
        super().__init__(message)
        self.observations = tuple(observations)


def read_retained_acceptance_policy(
    root: Path,
    profile: RetainedProjectProfile,
    locks,
    manifest: RetainedProjectManifest,
):
    if profile.acceptance_policy_identity is None:
        return None
    if any(
        ACCEPTANCE_POLICY_PATH == path or ACCEPTANCE_POLICY_PATH.startswith(path + "/")
        for path in manifest.roots
    ):
        raise ValueError("retained.acceptance.verifier-policy-must-be-withheld")
    path = root / ACCEPTANCE_POLICY_PATH
    member = RetainedProjectMember(
        ACCEPTANCE_POLICY_PATH,
        path.lstat().st_size,
        0o644,
        profile.acceptance_policy_identity.uri.removeprefix("sha256:"),
        "verifier-policy",
    )
    document = read_retained_project_document(
        root,
        member,
        maximum_bytes=16 * 1024 * 1024,
        error_prefix="retained.acceptance",
    )
    policy = RetainedProjectAcceptancePolicy.from_dict(document)
    policy.require_profile(profile, locks)
    return policy


@dataclass(frozen=True, slots=True)
class RetainedProjectAcceptanceObservation:
    plan_identity: ContentIdentity
    policy_identity: ContentIdentity
    case_identity: ContentIdentity
    command_identity: ContentIdentity
    dependency_identity: ContentIdentity
    output_manifest_identity: ContentIdentity
    stdout_identity: ContentIdentity
    stderr_identity: ContentIdentity
    exit_code: int
    result: BoundedProcessResult
    native_sdk_executions: tuple[ContentIdentity, ...] = ()
    filesystem_observation: RetainedFilesystemObservation | None = None
    filesystem_dependency_identity: ContentIdentity | None = None
    filesystem_environment_identity: ContentIdentity | None = None

    def __post_init__(self):
        for field in self.__slots__:
            if field not in {
                "exit_code",
                "result",
                "native_sdk_executions",
                "filesystem_observation",
                "filesystem_dependency_identity",
                "filesystem_environment_identity",
            } and not isinstance(getattr(self, field), ContentIdentity):
                raise TypeError("retained.acceptance.typed-observation-required")
        if type(self.exit_code) is not int or not -(2**31) <= self.exit_code < 2**32:
            raise ValueError("retained.acceptance.exit-invalid")
        if (
            not isinstance(self.result, BoundedProcessResult)
            or self.result.returncode != self.exit_code
            or self.stdout_identity.uri
            != "sha256:" + hashlib.sha256(self.result.stdout).hexdigest()
            or self.stderr_identity.uri
            != "sha256:" + hashlib.sha256(self.result.stderr).hexdigest()
        ):
            raise ValueError("retained.acceptance.result-identity-mismatch")
        if type(self.native_sdk_executions) is not tuple or any(
            not isinstance(v, ContentIdentity) for v in self.native_sdk_executions
        ):
            raise TypeError("retained.acceptance.typed-sdk-executions-required")
        if self.filesystem_observation is not None and not isinstance(
            self.filesystem_observation, RetainedFilesystemObservation
        ):
            raise TypeError("retained.acceptance.typed-filesystem-observation-required")
        file_context = (
            self.filesystem_dependency_identity,
            self.filesystem_environment_identity,
        )
        if (
            self.filesystem_observation is None
            and any(v is not None for v in file_context)
        ) or (
            self.filesystem_observation is not None
            and any(not isinstance(v, ContentIdentity) for v in file_context)
        ):
            raise TypeError("retained.acceptance.complete-filesystem-context-required")

    def to_dict(self):
        return {
            "schema": "literate-ai/retained-independent-acceptance-observation@1",
            **{
                field: getattr(self, field).uri
                for field in self.__slots__
                if field
                not in {
                    "exit_code",
                    "result",
                    "native_sdk_executions",
                    "filesystem_observation",
                    "filesystem_dependency_identity",
                    "filesystem_environment_identity",
                }
            },
            "exit_code": self.exit_code,
            "lifecycle_receipt": False,
            **(
                {"native_sdk_executions": [v.uri for v in self.native_sdk_executions]}
                if self.native_sdk_executions
                else {}
            ),
            **(
                {
                    "filesystem_observation": self.filesystem_observation.to_dict(),
                    "filesystem_dependency_identity": (
                        self.filesystem_dependency_identity.uri
                    ),
                    "filesystem_environment_identity": (
                        self.filesystem_environment_identity.uri
                    ),
                }
                if self.filesystem_observation is not None
                else {}
            ),
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())


def verify_retained_acceptance_action(
    policy: RetainedProjectAcceptancePolicy,
    plan: RetainedProjectPlan,
    action_id: str,
    outputs: Path,
    manifest: RetainedProjectManifest,
    *,
    environment: Mapping[str, str],
    require_current: Callable[[], None],
    command_scope=None,
    case_parent: Path | None = None,
):
    policy.require_profile(plan.profile, plan.component_locks)
    cases = tuple(case for case in policy.cases if case.action_id == action_id)
    if not cases:
        raise ValueError("retained.acceptance.action-not-declared")
    members = {member.path: member for member in manifest.members}
    observations = []
    for case in cases:
        require_current()
        member = members.get(case.executable)
        if member is None or member.mode != 0o755 or member.symlink_target is not None:
            raise ValueError("retained.acceptance.sealed-executable-required")
        executable = outputs / case.executable
        if executable.absolute() != executable.resolve(strict=True):
            raise ValueError("retained.acceptance.executable-redirected")
        with ExitStack() as cleanup:
            filesystem = None
            cwd, case_environment = outputs, environment
            if case.filesystem is not None:
                if case_parent is None:
                    raise ValueError("retained.acceptance.private-case-parent-required")
                filesystem = RetainedFilesystemWorkspace(
                    case.filesystem, case_parent, outputs
                )
                cleanup.callback(filesystem.close)
                cwd = filesystem.root
                case_environment = {
                    **environment,
                    **{key: str(cwd) for key in ("HOME", "TMPDIR", "TMP", "TEMP")},
                }
            before = retained_export_dependency_identity(
                outputs, manifest, plan, environment
            )
            case_before = (
                before
                if filesystem is None
                else retained_export_dependency_identity(
                    outputs, manifest, plan, case_environment
                )
            )
            arguments = tuple(
                arg.replace("{output_root}", str(outputs)) for arg in case.argv
            )
            if filesystem is not None:
                arguments = tuple(
                    arg.replace("{case_root}", str(cwd)) for arg in arguments
                )
            command = (str(executable), *arguments)
            context = (
                nullcontext(None)
                if command_scope is None
                else command_scope(
                    command,
                    tool=LocalComponentToolBinding(
                        str(executable),
                        authority_identity=ContentIdentity.parse_uri(
                            "sha256:" + member.sha256
                        ),
                    ),
                    cwd=cwd,
                    environment=case_environment,
                    action_identity=case.identity,
                    phase="accept",
                )
            )
            with context as sdk_scope:
                if (
                    sdk_scope is not None
                    and getattr(sdk_scope, "environment", None) is not None
                ):
                    command = sdk_scope.argv
                    case_environment = dict(sdk_scope.environment)
                if filesystem is not None:
                    filesystem.require_initial()
                result = run_bounded_process(
                    command,
                    cwd=cwd,
                    environment=case_environment,
                    timeout_seconds=case.timeout_seconds,
                    stdout_limit_bytes=MAX_ACCEPTANCE_STREAM_BYTES,
                    stderr_limit_bytes=MAX_ACCEPTANCE_STREAM_BYTES,
                    input_bytes=case.stdin,
                    input_limit_bytes=MAX_ACCEPTANCE_STREAM_BYTES,
                    error_prefix="retained.acceptance",
                    terminate_descendants=True,
                )
            require_current()
            after = retained_export_dependency_identity(
                outputs, manifest, plan, environment
            )
            case_after = (
                after
                if filesystem is None
                else retained_export_dependency_identity(
                    outputs, manifest, plan, case_environment
                )
            )
            if before != after or case_before != case_after:
                raise ValueError("retained.acceptance.native-dependencies-changed")
            try:
                file_observation = (
                    filesystem.observe() if filesystem is not None else None
                )
            except ValueError as exc:
                raise RetainedProjectAcceptanceError(
                    "retained.acceptance.filesystem-refused: " + str(exc), observations
                ) from exc
            observations.append(
                RetainedProjectAcceptanceObservation(
                    plan.identity,
                    policy.identity,
                    case.identity,
                    canonical_identity(command),
                    before,
                    manifest.identity,
                    ContentIdentity.parse_uri(
                        "sha256:" + hashlib.sha256(result.stdout).hexdigest()
                    ),
                    ContentIdentity.parse_uri(
                        "sha256:" + hashlib.sha256(result.stderr).hexdigest()
                    ),
                    result.returncode,
                    result,
                    sdk_scope.evidence_identities if sdk_scope is not None else (),
                    file_observation,
                    case_before if filesystem is not None else None,
                    canonical_identity(dict(case_environment))
                    if filesystem is not None
                    else None,
                )
            )
            if (result.returncode, result.stdout, result.stderr) != (
                case.expected_exit,
                case.expected_stdout,
                case.expected_stderr,
            ) or (filesystem is not None and not filesystem.matches(file_observation)):
                raise RetainedProjectAcceptanceError(
                    "retained.acceptance.result-mismatch: " + case.case_id, observations
                )
    return tuple(observations)
