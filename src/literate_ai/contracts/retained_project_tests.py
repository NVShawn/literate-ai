"""Reviewed original test coverage, not lifecycle or conversion acceptance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import ClassVar

from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_paths
from .retained_project_lifecycle import RetainedProjectProfile, _fields, _name
from .retained_test_cases import validate_retained_test_cases
from .testing import ProjectTestReceiptPolicy


@dataclass(frozen=True, slots=True)
class RetainedProjectExpectedCase:
    case_id: str
    action_id: str
    skip_reason: str | None = None

    def __post_init__(self):
        validate_retained_test_cases((self.case_id,))
        _name(self.action_id)
        if self.skip_reason is not None:
            _name(self.skip_reason)
            if not self.skip_reason.strip():
                raise ValueError("retained.tests.skip-reason-required")

    def to_dict(self):
        return dict(
            id=self.case_id, action=self.action_id, skip_reason=self.skip_reason
        )

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"id", "action", "skip_reason"})
        return cls(value["id"], value["action"], value["skip_reason"])


@dataclass(frozen=True, slots=True)
class RetainedProjectCaseOutcome:
    case_id: str
    outcome: str
    reason: str | None = None

    def __post_init__(self):
        validate_retained_test_cases((self.case_id,))
        if self.outcome not in {"passed", "failed", "skipped"}:
            raise ValueError("retained.tests.outcome-invalid")
        if self.outcome == "skipped":
            _name(self.reason)
            if not self.reason.strip():
                raise ValueError("retained.tests.skip-reason-required")
        elif self.reason is not None:
            raise ValueError("retained.tests.unexpected-skip-reason")

    def to_dict(self):
        return dict(id=self.case_id, outcome=self.outcome, reason=self.reason)

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"id", "outcome", "reason"})
        return cls(value["id"], value["outcome"], value["reason"])


@dataclass(frozen=True, slots=True)
class RetainedProjectTestReport:
    action_id: str
    cases: tuple[RetainedProjectCaseOutcome, ...]

    SCHEMA: ClassVar[str] = "literate-ai/retained-project-test-report@1"

    def __post_init__(self):
        _name(self.action_id)
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or any(not isinstance(c, RetainedProjectCaseOutcome) for c in self.cases)
        ):
            raise ValueError("retained.tests.typed-outcomes-required")
        validate_retained_test_cases(tuple(c.case_id for c in self.cases))

    @property
    def passed(self):
        return sum(c.outcome == "passed" for c in self.cases)

    @property
    def skipped(self):
        return sum(c.outcome == "skipped" for c in self.cases)

    def to_dict(self):
        return dict(
            schema=self.SCHEMA,
            action=self.action_id,
            cases=[case.to_dict() for case in self.cases],
        )

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"schema", "action", "cases"})
        if value["schema"] != cls.SCHEMA or not isinstance(value["cases"], list):
            raise ValueError("retained.tests.report-schema-invalid")
        if not 1 <= len(value["cases"]) <= 100000:
            raise ValueError("retained.tests.report-budget")
        return cls(
            value["action"],
            tuple(RetainedProjectCaseOutcome.from_dict(c) for c in value["cases"]),
        )


@dataclass(frozen=True, slots=True)
class RetainedProjectTestPolicy:
    project_policy_identity: ContentIdentity
    cases: tuple[RetainedProjectExpectedCase, ...]
    reports: tuple[tuple[str, str], ...]
    _by_action: Mapping[str, tuple[RetainedProjectExpectedCase, ...]] = field(
        init=False, repr=False, compare=False
    )

    SCHEMA: ClassVar[str] = "literate-ai/retained-project-test-policy@1"

    def __post_init__(self):
        if not isinstance(self.project_policy_identity, ContentIdentity):
            raise TypeError("retained.tests.project-policy-required")
        if (
            not isinstance(self.cases, tuple)
            or not self.cases
            or any(not isinstance(c, RetainedProjectExpectedCase) for c in self.cases)
        ):
            raise ValueError("retained.tests.reviewed-cases-required")
        validate_retained_test_cases(tuple(c.case_id for c in self.cases))
        grouped = {}
        for case in self.cases:
            grouped.setdefault(case.action_id, []).append(case)
        object.__setattr__(
            self,
            "_by_action",
            MappingProxyType(
                {action: tuple(cases) for action, cases in grouped.items()}
            ),
        )
        if not isinstance(self.reports, tuple) or not 1 <= len(self.reports) <= len(
            self.cases
        ):
            raise ValueError("retained.tests.report-owners-required")
        names = []
        for item in self.reports:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("retained.tests.typed-report-owners-required")
            action, _path = item
            _name(action)
            names.append(action)
        if tuple(names) != tuple(sorted(set(names))):
            raise ValueError("retained.tests.report-owners-duplicated-or-unordered")
        canonical_relative_posix_paths(
            (path for _, path in self.reports), label="retained test reports"
        )

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def require_profile(
        self, profile: RetainedProjectProfile, configured: ProjectTestReceiptPolicy
    ):
        if not isinstance(profile, RetainedProjectProfile):
            raise TypeError("retained.tests.typed-profile-required")
        if (
            not isinstance(configured, ProjectTestReceiptPolicy)
            or self.project_policy_identity != configured.identity
        ):
            raise ValueError("retained.tests.project-policy-mismatch")
        actions = {a.action_id: a for a in profile.actions if a.stage == "test"}
        reports = dict(self.reports)
        if set(reports) != set(actions) or set(self._by_action) != set(actions):
            raise ValueError("retained.tests.action-inventory-mismatch")
        if len(self.cases) < configured.minimum_test_count:
            raise ValueError("retained.tests.inventory-below-project-minimum")
        for action_id, action in actions.items():
            if set(action.inventory) != {c.case_id for c in self._by_action[action_id]}:
                raise ValueError("retained.tests.case-inventory-mismatch")
            if not any(
                reports[action_id] == path or reports[action_id].startswith(path + "/")
                for path in action.outputs
            ):
                raise ValueError("retained.tests.report-not-owned-by-test-action")

    def require_report(self, report: RetainedProjectTestReport):
        if not isinstance(report, RetainedProjectTestReport):
            raise TypeError("retained.tests.typed-report-required")
        expected = {c.case_id: c for c in self._by_action.get(report.action_id, ())}
        if not expected or set(expected) != {c.case_id for c in report.cases}:
            raise ValueError("retained.tests.report-inventory-mismatch")
        for case in report.cases:
            if case.outcome == "failed":
                raise ValueError("retained.tests.case-failed: " + case.case_id)
            if case.outcome == "skipped" and (
                expected[case.case_id].skip_reason is None
                or case.reason != expected[case.case_id].skip_reason
            ):
                raise ValueError("retained.tests.skip-not-authorized: " + case.case_id)

    def require_complete(self, reports, configured: ProjectTestReceiptPolicy):
        reports = tuple(reports)
        if not isinstance(configured, ProjectTestReceiptPolicy) or any(
            not isinstance(report, RetainedProjectTestReport) for report in reports
        ):
            raise TypeError("retained.tests.typed-reports-and-policy-required")
        names = tuple(report.action_id for report in reports)
        if len(set(names)) != len(names) or set(names) != dict(self.reports).keys():
            raise ValueError("retained.tests.reports-incomplete-or-duplicated")
        if self.project_policy_identity != configured.identity:
            raise ValueError("retained.tests.project-policy-mismatch")
        for report in reports:
            self.require_report(report)
        if sum(report.passed for report in reports) < configured.minimum_test_count:
            raise ValueError("retained.tests.passes-below-project-minimum")

    def to_dict(self):
        return dict(
            schema=self.SCHEMA,
            project_policy_identity=self.project_policy_identity.uri,
            cases=[case.to_dict() for case in self.cases],
            reports=[dict(action=action, path=path) for action, path in self.reports],
        )

    @classmethod
    def from_dict(cls, value):
        value = _fields(
            value, {"schema", "project_policy_identity", "cases", "reports"}
        )
        if value["schema"] != cls.SCHEMA:
            raise ValueError("retained.tests.policy-schema-invalid")
        for key in ("cases", "reports"):
            if not isinstance(value[key], list) or not 1 <= len(value[key]) <= 100000:
                raise ValueError("retained.tests.policy-budget")
        reports = []
        for item in value["reports"]:
            item = _fields(item, {"action", "path"})
            reports.append((item["action"], item["path"]))
        return cls(
            ContentIdentity.parse_uri(value["project_policy_identity"]),
            tuple(RetainedProjectExpectedCase.from_dict(c) for c in value["cases"]),
            tuple(reports),
        )
