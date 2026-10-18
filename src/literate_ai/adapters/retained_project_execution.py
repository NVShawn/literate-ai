"""Isolated retained commands, without generated-source or acceptance authority.

This execution owner shares Standard tool and bounded-process boundaries. Its
observations still require SBOM, independent acceptance and receipt admission;
successful exits alone never authorize packaging, publication or conversion.
The same-user workspace detects input mutation; it is not an OS sandbox.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.retained_project_inputs import (
    _remove_tree,
    capture_retained_project_inputs,
    discover_retained_project_inputs,
    verify_retained_project_inputs,
)
from literate_ai.adapters.retained_project_testing import (
    RetainedProjectVerifiedTestReport,
    read_retained_project_test_policy,
    read_retained_project_test_report,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestReceiptPolicy,
    canonical_identity,
)
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectPlan,
)

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
    result: BoundedProcessResult
    output_manifest: RetainedProjectManifest | None

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/retained-command-observation@1",
                "plan_identity": self.plan_identity.uri,
                "action": self.action.to_dict(),
                "tool_identity": self.tool_identity.uri,
                "environment_identity": self.environment_identity.uri,
                "exit_code": self.result.returncode,
                "stdout": hashlib.sha256(self.result.stdout).hexdigest(),
                "stderr": hashlib.sha256(self.result.stderr).hexdigest(),
                "output_manifest": (
                    self.output_manifest.identity.uri if self.output_manifest else None
                ),
                "acceptance_receipt": False,
            }
        )


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
    ) -> None:
        if allow_host_execution is not True:
            raise RetainedProjectExecutionError(
                "retained.execution.authorization-required"
            )
        plan.require_authorization(authorization)
        if manifest.identity != plan.manifest_identity:
            raise RetainedProjectExecutionError("retained.execution.manifest-mismatch")
        self.source = source
        self.manifest = manifest
        self.plan = plan
        self.current_plan = current_plan
        self._observations: list[RetainedProjectCommandObservation] = []
        self._output_bytes = 0
        self._output_manifest: RetainedProjectManifest | None = None
        self._failed = False
        self._test_reports: list[RetainedProjectVerifiedTestReport] = []
        self._configured_test_policy = test_receipt_policy
        self._require_current_plan()
        self._test_policy = read_retained_project_test_policy(
            source, manifest, plan.profile, test_receipt_policy
        )
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
        verify_retained_project_inputs(self.source, self.manifest)
        verify_retained_project_inputs(self.inputs, self.manifest)
        for binding in self._tools.values():
            binding.require_unchanged()
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
            "PATH": os.pathsep.join(
                sorted(
                    {str(Path(tool.executable).parent) for tool in self._tools.values()}
                )
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
                result,
                outputs,
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
            return observation
        except BaseException:
            self._failed = True
            raise
