"""Isolated retained commands, without generated-source or acceptance authority.

This execution owner shares Standard tool and bounded-process boundaries. Its
observations still require SBOM, independent acceptance and receipt admission;
successful exits alone never authorize packaging, publication or conversion.
The same-user workspace detects input mutation; it is not an OS sandbox.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.retained_project_acceptance import (
    read_retained_acceptance_policy,
    verify_retained_acceptance_action,
)
from literate_ai.adapters.retained_project_boms import (
    RetainedProjectSourceBom,
    resolve_retained_project_bom,
)
from literate_ai.adapters.retained_project_dependencies import (
    RetainedProjectDependencyGuard,
    observe_retained_export_dependencies,
    retained_export_dependency_identity,
)
from literate_ai.adapters.retained_project_environment import (
    captured_runtime_environment,
    retained_command_path,
)
from literate_ai.adapters.retained_project_inputs import (
    _remove_tree,
    capture_retained_project_inputs,
    discover_retained_project_inputs,
    verify_retained_project_inputs,
)
from literate_ai.adapters.retained_project_sdks import RetainedProjectNativeSdkInputs
from literate_ai.adapters.retained_project_testing import (
    RetainedProjectVerifiedTestReport,
    read_retained_project_test_policy,
    read_retained_project_test_report,
)
from literate_ai.contracts import (
    ComponentLock,
    ContentIdentity,
    ProjectTestReceiptPolicy,
    canonical_identity,
)
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectPlan,
)
from literate_ai.contracts.sbom import CycloneDxManagedGraph

# These budgets are framework policy and therefore part of the exact Standard
# distribution binding in the reviewed plan, rather than ambient runner options.
_PROCESS_OUTPUT_BYTES = 4 * 1024 * 1024
_TOTAL_OUTPUT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RetainedProjectCommandObservation:
    plan_identity: ContentIdentity
    action: RetainedProjectAction
    tool_identity: ContentIdentity
    environment_identity: ContentIdentity
    dependency_identity: ContentIdentity
    result: BoundedProcessResult
    output_manifest: RetainedProjectManifest | None
    native_sdk_inputs_identity: ContentIdentity | None = None
    native_sdk_executions: tuple[ContentIdentity, ...] = ()

    def __post_init__(self):
        if (
            (
                self.native_sdk_inputs_identity is not None
                and not isinstance(self.native_sdk_inputs_identity, ContentIdentity)
            )
            or type(self.native_sdk_executions) is not tuple
            or any(
                not isinstance(v, ContentIdentity) for v in self.native_sdk_executions
            )
        ):
            raise TypeError("retained.sdk.typed-command-evidence-required")
        if self.native_sdk_inputs_identity is None and self.native_sdk_executions:
            raise ValueError("retained.sdk.command-input-binding-required")

    def to_dict(self):
        return {
            "schema": "literate-ai/retained-command-observation@2",
            "plan_identity": self.plan_identity.uri,
            "action": self.action.to_dict(),
            "tool_identity": self.tool_identity.uri,
            "environment_identity": self.environment_identity.uri,
            "dependency_identity": self.dependency_identity.uri,
            "exit_code": self.result.returncode,
            "stdout": hashlib.sha256(self.result.stdout).hexdigest(),
            "stderr": hashlib.sha256(self.result.stderr).hexdigest(),
            "output_manifest": self.output_manifest.identity.uri
            if self.output_manifest
            else None,
            "acceptance_receipt": False,
            **(
                {
                    "native_sdk_inputs_identity": self.native_sdk_inputs_identity.uri,
                    "native_sdk_executions": [
                        v.uri for v in self.native_sdk_executions
                    ],
                }
                if self.native_sdk_inputs_identity is not None
                else {}
            ),
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())


class RetainedProjectExecutionError(ValueError):
    def __init__(self, message: str, observations=()) -> None:
        super().__init__(message)
        self.observations = tuple(observations)


class RetainedProjectExecutionWorkspace:
    """Private inputs, scratch, exports and home for one authorized reviewed plan.

    `current_plan` must call the retained planner with current project authority,
    not return a cached plan. The CLI retains that responsibility when integrating
    this owner; it cannot substitute an ordinary generated lifecycle receipt.
    """

    def __init__(
        self,
        source: Path,
        manifest: RetainedProjectManifest,
        plan: RetainedProjectPlan,
        *,
        parent: Path,
        current_plan: Callable[[], RetainedProjectPlan],
        authorization: str,
        allow_host_execution: bool,
        test_receipt_policy: ProjectTestReceiptPolicy,
        managed_graph: CycloneDxManagedGraph | None = None,
        component_locks: tuple[ComponentLock, ...] | None = None,
        native_sdk_owners: tuple[NativeSdkConsumerInputs, ...] | None = None,
        native_sdk_authorize=None,
        native_sdk_record=None,
    ) -> None:
        if allow_host_execution is not True:
            raise RetainedProjectExecutionError(
                "retained.execution.authorization-required"
            )
        plan.require_authorization(authorization)
        if plan.profile.secondary_access is not None:
            raise RetainedProjectExecutionError(
                "retained.secondary.provisioning-owner-required"
            )
        if (managed_graph is None) != (component_locks is None):
            raise RetainedProjectExecutionError(
                "retained.bom.graph-and-locks-required-together"
            )
        if manifest.identity != plan.manifest_identity:
            raise RetainedProjectExecutionError("retained.execution.manifest-mismatch")
        sdk_selected = native_sdk_owners is not None
        sdk_required = managed_graph is not None and (
            any(m.role == "sdk" for m in manifest.members)
            or any(
                node.revision.repository_sources
                for lock in component_locks
                for node in lock.nodes
            )
        )
        if sdk_required and not sdk_selected:
            raise RetainedProjectExecutionError(
                "retained.sdk.producer-bindings-required"
            )
        if sdk_selected != any(m.role == "sdk-policy" for m in manifest.members):
            raise RetainedProjectExecutionError(
                "retained.sdk.policy-and-owners-required-together"
            )
        if (
            sdk_selected
            and (
                managed_graph is None
                or component_locks is None
                or not callable(native_sdk_authorize)
                or not callable(native_sdk_record)
            )
        ) or (
            not sdk_selected
            and (native_sdk_authorize is not None or native_sdk_record is not None)
        ):
            raise RetainedProjectExecutionError(
                "retained.sdk.bound-graph-and-command-authority-required"
            )
        self.source = source
        self.manifest = manifest
        self.plan = plan
        self.current_plan = current_plan
        self._observations: list[RetainedProjectCommandObservation] = []
        self._output_bytes = 0
        self._output_manifest: RetainedProjectManifest | None = None
        self._failed = False
        self._test_reports: list[RetainedProjectVerifiedTestReport] = []
        self._acceptance_observations = []
        self._configured_test_policy = test_receipt_policy
        self._native_sdks = None
        self._native_sdk_authorize = native_sdk_authorize
        self._native_sdk_record = native_sdk_record
        self._require_current_plan()
        self._test_policy = read_retained_project_test_policy(
            source, manifest, plan.profile, test_receipt_policy
        )
        self._acceptance_policy = read_retained_acceptance_policy(
            source, plan.profile, plan.component_locks, manifest
        )
        self._source_bom = (
            RetainedProjectSourceBom(plan, manifest, managed_graph, component_locks)
            if managed_graph is not None
            else None
        )
        self._resolved_bom = None
        require_safe_directory(parent)
        self.root = Path(tempfile.mkdtemp(prefix="retained-run-", dir=parent))
        try:
            self.inputs = capture_retained_project_inputs(
                source, manifest, self.root / "snapshot"
            )
            self.objects = self.root / "objects"
            self.outputs = self.root / "outputs"
            self.home = self.root / "home"
            self.temporary = self.root / "tmp"
            for directory in (self.objects, self.outputs, self.home, self.temporary):
                directory.mkdir(mode=0o700)
            self._tools = {}
            members = {member.path: member for member in manifest.members}
            for name, path, identity in plan.profile.tools:
                member = members.get(path)
                if (
                    member is None
                    or member.role not in {"sdk", "toolchain"}
                    or member.mode != 0o755
                    or ContentIdentity.parse_uri("sha256:" + member.sha256) != identity
                ):
                    raise RetainedProjectExecutionError(
                        "retained.execution.tool-mismatch"
                    )
                self._tools[name] = LocalComponentToolBinding(
                    str(self.inputs / path), authority_identity=identity
                )
            self._dependencies = RetainedProjectDependencyGuard(
                self.inputs,
                self.manifest,
                self.plan,
                tuple(self._tools[name] for name in sorted(self._tools)),
                self._environment(),
            )
            if sdk_selected:
                self._native_sdks = RetainedProjectNativeSdkInputs(
                    self.inputs,
                    manifest,
                    plan,
                    component_locks,
                    native_sdk_owners,
                    parent=self.root,
                )
            self._require_current()
        except BaseException:
            _remove_tree(self.root)
            raise

    def _require_current_plan(self) -> None:
        current = self.current_plan()
        if not isinstance(current, RetainedProjectPlan) or current != self.plan:
            raise RetainedProjectExecutionError("retained.execution.plan-changed")

    @property
    def observations(self) -> tuple[RetainedProjectCommandObservation, ...]:
        return tuple(self._observations)

    @property
    def test_reports(self) -> tuple[RetainedProjectVerifiedTestReport, ...]:
        return tuple(self._test_reports)

    @property
    def acceptance_observations(self):
        return tuple(self._acceptance_observations)

    @property
    def source_bom(self):
        return self._source_bom

    @property
    def resolved_bom(self):
        return self._resolved_bom

    @property
    def dependency_observation(self):
        return self._dependencies.observation

    @property
    def dependency_identity(self) -> ContentIdentity:
        return self._dependencies.identity

    def _sdk_command_scope(
        self, argv, *, tool, cwd, environment, action_identity, phase
    ):
        if self._native_sdks is None:
            return nullcontext(None)
        return self._native_sdks.command_scope(
            tuple(argv),
            tool=tool,
            cwd=cwd,
            environment=environment,
            action_identity=action_identity,
            phase=phase,
            authorize=self._native_sdk_authorize,
            clock=lambda: datetime.now(UTC),
            record=self._native_sdk_record,
        )

    def completed_run(self):
        """Join a complete current run; this grants no receipt or package authority."""
        from literate_ai.adapters.retained_project_completion import (
            RetainedProjectCompletedRun,
        )

        if self._failed or len(self.observations) != len(self.plan.profile.actions):
            raise RetainedProjectExecutionError("retained.completion.run-incomplete")
        if (
            self.source_bom is None
            or self.resolved_bom is None
            or self._acceptance_policy is None
            or self._output_manifest is None
        ):
            raise RetainedProjectExecutionError(
                "retained.completion.boms-and-independent-acceptance-required"
            )
        self._require_current()
        result = RetainedProjectCompletedRun(
            self.source_bom,
            self.resolved_bom,
            self.observations,
            self._test_policy,
            self._configured_test_policy,
            self.test_reports,
            self._acceptance_policy,
            self.acceptance_observations,
            self._output_manifest,
            self._native_sdks.final_checks(self._native_sdk_record)
            if self._native_sdks is not None
            else None,
        )
        if (
            retained_export_dependency_identity(
                self.outputs, self._output_manifest, self.plan, self._environment()
            )
            != self.acceptance_observations[-1].dependency_identity
        ):
            raise RetainedProjectExecutionError(
                "retained.completion.export-dependencies-changed"
            )
        self._require_current()
        return result

    def _require_current(self) -> None:
        for directory in (
            self.root,
            self.objects,
            self.outputs,
            self.home,
            self.temporary,
        ):
            require_safe_directory(directory)
        self._require_current_plan()
        if (
            read_retained_acceptance_policy(
                self.source, self.plan.profile, self.plan.component_locks, self.manifest
            )
            != self._acceptance_policy
        ):
            raise RetainedProjectExecutionError("retained.acceptance.policy-changed")
        verify_retained_project_inputs(self.source, self.manifest)
        verify_retained_project_inputs(self.inputs, self.manifest)
        for binding in self._tools.values():
            binding.require_unchanged()
        self._dependencies.require_unchanged(
            environment=self._environment(),
            tools=tuple(self._tools[name] for name in sorted(self._tools)),
        )
        if self._native_sdks is not None:
            self._native_sdks.require_unchanged()
        if self._output_manifest is not None:
            verify_retained_project_inputs(self.outputs, self._output_manifest)

    def _capture_outputs(self, action: RetainedProjectAction):
        roots = tuple(
            output
            for prior in (*self.plan.profile.actions[: len(self.observations)], action)
            for output in prior.outputs
        )
        # Inspect the complete export tree, including empty and undeclared dirs.
        # Scratch work belongs in objects, not in the admitted export closure.
        top = []
        for path in self.outputs.iterdir():
            top.append(path.name)
            if len(top) > self.manifest.limits.max_entries:
                raise RetainedProjectExecutionError("retained.execution.output-budget")
        if not top:
            if roots:
                raise RetainedProjectExecutionError("retained.execution.missing-output")
            return None
        if not roots:
            raise RetainedProjectExecutionError("retained.execution.undeclared-output")
        actual = discover_retained_project_inputs(
            self.outputs,
            {path: "retained-output" for path in sorted(top)},
            self.manifest.limits,
        )
        for member in actual.members:
            if not any(
                member.path == root or member.path.startswith(root + "/")
                for root in roots
            ):
                raise RetainedProjectExecutionError(
                    "retained.execution.undeclared-output"
                )
        for directory in actual.directories:
            if not any(
                directory == root
                or directory.startswith(root + "/")
                or root.startswith(directory + "/")
                for root in roots
            ):
                raise RetainedProjectExecutionError(
                    "retained.execution.undeclared-output"
                )
        nodes = {m.path for m in actual.members} | set(actual.directories)
        if not set(roots) <= nodes:
            raise RetainedProjectExecutionError("retained.execution.missing-output")
        return actual

    def _environment(self) -> dict[str, str]:
        # Do not inherit credentials, compiler/loader injection, user dotfiles,
        # Python paths or the caller's command search path.
        return {
            **captured_runtime_environment(
                self.inputs, self.manifest, self.plan.profile
            ),
            "PATH": retained_command_path(
                self.outputs, self.plan.profile, self._tools.values()
            ),
            "HOME": str(self.home),
            "TMPDIR": str(self.temporary),
            "TMP": str(self.temporary),
            "TEMP": str(self.temporary),
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def execute_next(self) -> RetainedProjectCommandObservation:
        """Run exactly the next graph action; failed work cannot be resumed as green."""
        if self._failed or len(self.observations) >= len(self.plan.profile.actions):
            raise RetainedProjectExecutionError("retained.execution.no-next-action")
        action = self.plan.profile.actions[len(self.observations)]
        try:
            self._require_current()
            if (
                action.stage in {"test", "docs", "accept"}
                and self.source_bom is not None
                and self.resolved_bom is None
            ):
                raise RetainedProjectExecutionError(
                    "retained.bom.build-resolution-required"
                )
            if action.stage == "accept":
                self._test_policy.require_complete(
                    (item.report for item in self.test_reports),
                    self._configured_test_policy,
                )
            binding = self._tools[action.tool]
            replacements = {
                "{input_root}": str(self.inputs),
                "{object_root}": str(self.objects),
                "{output_root}": str(self.outputs),
            }
            argv = []
            for argument in action.argv:
                for token, value in replacements.items():
                    argument = argument.replace(token, value)
                argv.append(argument)
            environment = self._environment()
            remaining = _TOTAL_OUTPUT_BYTES - self._output_bytes
            if remaining < 2:
                raise RetainedProjectExecutionError("retained.execution.output-budget")
            with self._sdk_command_scope(
                (*binding.command, *argv),
                tool=binding,
                cwd=self.objects,
                environment=environment,
                action_identity=canonical_identity(action.to_dict()),
                phase=action.stage,
            ) as sdk_scope:
                result = run_bounded_process(
                    (*binding.command, *argv),
                    cwd=self.objects,
                    environment=environment,
                    timeout_seconds=action.timeout_seconds,
                    stdout_limit_bytes=min(_PROCESS_OUTPUT_BYTES, remaining // 2),
                    stderr_limit_bytes=min(_PROCESS_OUTPUT_BYTES, remaining // 2),
                    error_prefix="retained.execution",
                    terminate_descendants=True,
                )
            self._output_bytes += len(result.stdout) + len(result.stderr)
            # Revalidate before treating even a zero exit as an observation of
            # the admitted inputs. Previously sealed exports may not be edited.
            self._require_current()
            outputs = None
            if result.returncode == 0:
                outputs = self._capture_outputs(action)
            observation = RetainedProjectCommandObservation(
                self.plan.identity,
                action,
                binding.toolchain_identity,
                canonical_identity(environment),
                self.dependency_identity,
                result,
                outputs,
                self._native_sdks.identity if self._native_sdks is not None else None,
                sdk_scope.evidence_identities if sdk_scope is not None else (),
            )
            self._observations.append(observation)
            if result.returncode != 0 or self._output_bytes > _TOTAL_OUTPUT_BYTES:
                raise RetainedProjectExecutionError(
                    "retained.execution.command-failed", self.observations
                )
            produced = {
                path: "retained-output"
                for prior in self.plan.profile.actions[: len(self.observations)]
                for path in prior.outputs
            }
            self._output_manifest = (
                discover_retained_project_inputs(
                    self.outputs, produced, self.manifest.limits
                )
                if produced
                else None
            )
            if action.stage == "build" and self.source_bom is not None:
                completed = {item.action.action_id for item in self.observations}
                builds = {
                    item.action_id
                    for item in self.plan.profile.actions
                    if item.stage == "build"
                }
                if builds <= completed:
                    if self._output_manifest is None:
                        raise RetainedProjectExecutionError(
                            "retained.bom.build-exports-missing"
                        )
                    self._resolved_bom = resolve_retained_project_bom(
                        self.source_bom,
                        self._output_manifest,
                        self._dependencies.observation,
                        observe_retained_export_dependencies(
                            self.outputs,
                            self._output_manifest,
                            self.plan,
                            self._environment(),
                            allow_empty=True,
                        ),
                        sdk_dependencies=self._native_sdks.dependencies
                        if self._native_sdks is not None
                        else (),
                    )
                    self._require_current()
            if action.stage == "test":
                try:
                    if self._output_manifest is None:
                        raise ValueError("retained.tests.sealed-report-missing")
                    report = read_retained_project_test_report(
                        self.outputs,
                        self._output_manifest,
                        self._test_policy,
                        action_id=action.action_id,
                        plan_identity=self.plan.identity,
                        observation_identity=observation.identity,
                    )
                    self._test_reports.append(report)
                    if len(self.test_reports) == len(self._test_policy.reports):
                        self._test_policy.require_complete(
                            (item.report for item in self.test_reports),
                            self._configured_test_policy,
                        )
                    self._require_current()
                except (ValueError, TypeError, OSError) as exc:
                    raise RetainedProjectExecutionError(
                        "retained.execution.test-report-rejected: " + str(exc),
                        self.observations,
                    ) from exc
            if action.stage == "accept" and self._acceptance_policy is not None:
                if self._output_manifest is None:
                    raise RetainedProjectExecutionError(
                        "retained.acceptance.outputs-missing"
                    )
                self._acceptance_observations.extend(
                    verify_retained_acceptance_action(
                        self._acceptance_policy,
                        self.plan,
                        action.action_id,
                        self.outputs,
                        self._output_manifest,
                        environment=self._environment(),
                        require_current=self._require_current,
                        command_scope=self._sdk_command_scope
                        if self._native_sdks is not None
                        else None,
                        case_parent=self.objects,
                    )
                )
            return observation
        except BaseException:
            self._failed = True
            raise
