"""Typed repair-checkpoint annotations and honest non-passing test reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    fields,
    int_value,
    list_value,
    optional_string,
    string_value,
    unique,
)
from .channel_events import reject_secret_shaped
from .identity import ContentIdentity, contract_identity

KNOWN_TEST_FAILURE_ANNOTATION_SCHEMA = (
    "urn:literate-ai:schema:v2:known-test-failure-annotation"
)
KNOWN_TEST_FAILURE_REPORT_SCHEMA = "urn:literate-ai:schema:v2:known-test-failure-report"
KNOWN_TEST_FAILURE_OUTCOME_SCHEMA = (
    "urn:literate-ai:schema:v2:known-test-failure-outcome"
)


class KnownTestFailureCause(StrEnum):
    AUTHENTICATION_REQUIRED = "environment.authentication-required"
    SERVICE_UNAVAILABLE = "environment.service-unavailable"
    KNOWN_PRODUCT_DEFECT = "product.known-defect"


class KnownTestFailurePolicy(StrEnum):
    MANUAL_REVALIDATION = "manual-revalidation"
    EXPIRES_AT = "expires-at"


class TestOutcomeStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    KNOWN_FAILURE = "known-failure"
    SKIPPED = "skipped"


def _identity(value: Any, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


def _optional_identity(value: Any, path: str) -> ContentIdentity | None:
    if value is None:
        return None
    return ContentIdentity.from_dict(value, path=path)


def _expiry(value: str | None, path: str) -> str | None:
    if value is None:
        return None
    text = string_value(value, path, max_length=64)
    if not text.endswith("Z"):
        fail(path, "must be a UTC RFC3339 timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        fail(path, "must be a UTC RFC3339 timestamp ending in Z")
    if parsed.tzinfo != UTC:
        fail(path, "must be a UTC RFC3339 timestamp ending in Z")
    return text


def _tracker(value: str | None, path: str) -> str | None:
    if value is None:
        return None
    text = reject_secret_shaped(string_value(value, path, max_length=2048), path)
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        fail(path, "must be a public credential-free HTTPS URL")
    return text


@dataclass(frozen=True, slots=True)
class KnownTestFailureAnnotation:
    """One explicit known failure, reusable only at its exact pin and context."""

    pin_identity: ContentIdentity
    test_key: str
    cause: KnownTestFailureCause
    context_identity: ContentIdentity | None
    universal_authority: ContentIdentity | None
    failure_identity: ContentIdentity
    evidence_run_identity: ContentIdentity
    evidence_node_id: str
    reason: str
    policy: KnownTestFailurePolicy
    expires_at: str | None = None
    tracker: str | None = None

    SCHEMA: ClassVar[str] = KNOWN_TEST_FAILURE_ANNOTATION_SCHEMA

    def __post_init__(self) -> None:
        _identity(self.pin_identity, "KnownTestFailureAnnotation.pin_identity")
        string_value(
            self.test_key, "KnownTestFailureAnnotation.test_key", max_length=2048
        )
        if not isinstance(self.cause, KnownTestFailureCause):
            fail("KnownTestFailureAnnotation.cause", "must be a known cause code")
        if (self.context_identity is None) == (self.universal_authority is None):
            fail(
                "KnownTestFailureAnnotation",
                "must bind exactly one context identity or universal authority",
            )
        if self.context_identity is not None:
            _identity(
                self.context_identity, "KnownTestFailureAnnotation.context_identity"
            )
        if self.universal_authority is not None:
            _identity(
                self.universal_authority,
                "KnownTestFailureAnnotation.universal_authority",
            )
        _identity(self.failure_identity, "KnownTestFailureAnnotation.failure_identity")
        _identity(
            self.evidence_run_identity,
            "KnownTestFailureAnnotation.evidence_run_identity",
        )
        node_id = string_value(
            self.evidence_node_id,
            "KnownTestFailureAnnotation.evidence_node_id",
            max_length=128,
        )
        if any(character.isspace() for character in node_id) or "/" in node_id:
            fail(
                "KnownTestFailureAnnotation.evidence_node_id",
                "must be one bounded evidence-node identifier",
            )
        reason = reject_secret_shaped(
            string_value(
                self.reason, "KnownTestFailureAnnotation.reason", max_length=1024
            ),
            "KnownTestFailureAnnotation.reason",
        )
        if "\n" in reason or "\r" in reason:
            fail("KnownTestFailureAnnotation.reason", "must be one line")
        if not isinstance(self.policy, KnownTestFailurePolicy):
            fail("KnownTestFailureAnnotation.policy", "must be a known policy")
        expiry = _expiry(self.expires_at, "KnownTestFailureAnnotation.expires_at")
        if (self.policy is KnownTestFailurePolicy.EXPIRES_AT) != (expiry is not None):
            fail(
                "KnownTestFailureAnnotation.expires_at",
                "is required only for the expires-at policy",
            )
        _tracker(self.tracker, "KnownTestFailureAnnotation.tracker")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def is_universal(self) -> bool:
        return self.universal_authority is not None

    def matches(
        self,
        *,
        pin_identity: ContentIdentity,
        test_key: str,
        context_identity: ContentIdentity | None,
        now: datetime | None = None,
    ) -> bool:
        if self.pin_identity != pin_identity or self.test_key != test_key:
            return False
        if not self.is_universal and self.context_identity != context_identity:
            return False
        if self.expires_at is None:
            return True
        current = now or datetime.now(UTC)
        expiry = datetime.fromisoformat(self.expires_at[:-1] + "+00:00")
        return current < expiry

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "pin_identity": self.pin_identity.to_dict(),
            "test_key": self.test_key,
            "cause": self.cause.value,
            "context_identity": (
                None
                if self.context_identity is None
                else self.context_identity.to_dict()
            ),
            "universal_authority": (
                None
                if self.universal_authority is None
                else self.universal_authority.to_dict()
            ),
            "failure_identity": self.failure_identity.to_dict(),
            "evidence_run_identity": self.evidence_run_identity.to_dict(),
            "evidence_node_id": self.evidence_node_id,
            "reason": self.reason,
            "policy": self.policy.value,
            "expires_at": self.expires_at,
            "tracker": self.tracker,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "KnownTestFailureAnnotation"
    ) -> KnownTestFailureAnnotation:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "pin_identity",
                    "test_key",
                    "cause",
                    "context_identity",
                    "universal_authority",
                    "failure_identity",
                    "evidence_run_identity",
                    "evidence_node_id",
                    "reason",
                    "policy",
                    "expires_at",
                    "tracker",
                }
            ),
        )
        return cls(
            pin_identity=ContentIdentity.from_dict(
                data["pin_identity"], path=f"{path}.pin_identity"
            ),
            test_key=string_value(data["test_key"], f"{path}.test_key"),
            cause=enum_value(KnownTestFailureCause, data["cause"], f"{path}.cause"),
            context_identity=_optional_identity(
                data["context_identity"], f"{path}.context_identity"
            ),
            universal_authority=_optional_identity(
                data["universal_authority"], f"{path}.universal_authority"
            ),
            failure_identity=ContentIdentity.from_dict(
                data["failure_identity"], path=f"{path}.failure_identity"
            ),
            evidence_run_identity=ContentIdentity.from_dict(
                data["evidence_run_identity"], path=f"{path}.evidence_run_identity"
            ),
            evidence_node_id=string_value(
                data["evidence_node_id"], f"{path}.evidence_node_id"
            ),
            reason=string_value(data["reason"], f"{path}.reason"),
            policy=enum_value(KnownTestFailurePolicy, data["policy"], f"{path}.policy"),
            expires_at=optional_string(data["expires_at"], f"{path}.expires_at"),
            tracker=_tracker(data["tracker"], f"{path}.tracker"),
        )


@dataclass(frozen=True, slots=True)
class KnownTestFailureOutcome:
    test_key: str
    status: TestOutcomeStatus
    failure_identity: ContentIdentity | None = None
    annotation_identity: ContentIdentity | None = None

    SCHEMA: ClassVar[str] = KNOWN_TEST_FAILURE_OUTCOME_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.test_key, "KnownTestFailureOutcome.test_key", max_length=2048)
        if not isinstance(self.status, TestOutcomeStatus):
            fail("KnownTestFailureOutcome.status", "must be a known outcome")
        if self.failure_identity is not None:
            _identity(self.failure_identity, "KnownTestFailureOutcome.failure_identity")
        if self.annotation_identity is not None:
            _identity(
                self.annotation_identity,
                "KnownTestFailureOutcome.annotation_identity",
            )
        if self.status in {TestOutcomeStatus.FAILED, TestOutcomeStatus.KNOWN_FAILURE}:
            if self.failure_identity is None:
                fail(
                    "KnownTestFailureOutcome.failure_identity",
                    "is required for failed and known-failure outcomes",
                )
        elif self.failure_identity is not None:
            fail(
                "KnownTestFailureOutcome.failure_identity",
                "is allowed only for failed and known-failure outcomes",
            )
        if (self.status is TestOutcomeStatus.KNOWN_FAILURE) != (
            self.annotation_identity is not None
        ):
            fail(
                "KnownTestFailureOutcome.annotation_identity",
                "is required only for known-failure outcomes",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "test_key": self.test_key,
            "status": self.status.value,
            "failure_identity": (
                None
                if self.failure_identity is None
                else self.failure_identity.to_dict()
            ),
            "annotation_identity": (
                None
                if self.annotation_identity is None
                else self.annotation_identity.to_dict()
            ),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "KnownTestFailureOutcome"
    ) -> KnownTestFailureOutcome:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"test_key", "status", "failure_identity", "annotation_identity"}
            ),
        )
        return cls(
            test_key=string_value(data["test_key"], f"{path}.test_key"),
            status=enum_value(TestOutcomeStatus, data["status"], f"{path}.status"),
            failure_identity=_optional_identity(
                data["failure_identity"], f"{path}.failure_identity"
            ),
            annotation_identity=_optional_identity(
                data["annotation_identity"], f"{path}.annotation_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class KnownTestFailureSummary:
    selected: int
    executed: int
    passed: int
    failed: int
    known_failed: int
    skipped: int

    def __post_init__(self) -> None:
        counts = {
            name: int_value(getattr(self, name), f"KnownTestFailureSummary.{name}")
            for name in (
                "selected",
                "executed",
                "passed",
                "failed",
                "known_failed",
                "skipped",
            )
        }
        if counts["executed"] != counts["passed"] + counts["failed"]:
            fail(
                "KnownTestFailureSummary.executed",
                "must equal passed plus failed",
            )
        accounted = (
            counts["passed"]
            + counts["failed"]
            + counts["known_failed"]
            + counts["skipped"]
        )
        if accounted > counts["selected"]:
            fail("KnownTestFailureSummary", "outcomes exceed selected tests")

    def to_dict(self) -> dict[str, int]:
        return {
            "selected": self.selected,
            "executed": self.executed,
            "passed": self.passed,
            "failed": self.failed,
            "known_failed": self.known_failed,
            "skipped": self.skipped,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "KnownTestFailureSummary"
    ) -> KnownTestFailureSummary:
        data = fields(
            value,
            path=path,
            required=frozenset(
                {"selected", "executed", "passed", "failed", "known_failed", "skipped"}
            ),
        )
        return cls(
            selected=int_value(data["selected"], f"{path}.selected"),
            executed=int_value(data["executed"], f"{path}.executed"),
            passed=int_value(data["passed"], f"{path}.passed"),
            failed=int_value(data["failed"], f"{path}.failed"),
            known_failed=int_value(data["known_failed"], f"{path}.known_failed"),
            skipped=int_value(data["skipped"], f"{path}.skipped"),
        )


@dataclass(frozen=True, slots=True)
class KnownTestFailureReport:
    """A repair report; unlike ProjectTestReceipt, it may be non-passing."""

    suite: str
    pin_identity: ContentIdentity
    context_identity: ContentIdentity | None
    release_evidence: bool
    summary: KnownTestFailureSummary
    outcomes: tuple[KnownTestFailureOutcome, ...]

    SCHEMA: ClassVar[str] = KNOWN_TEST_FAILURE_REPORT_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.suite, "KnownTestFailureReport.suite", max_length=256)
        _identity(self.pin_identity, "KnownTestFailureReport.pin_identity")
        if self.context_identity is not None:
            _identity(self.context_identity, "KnownTestFailureReport.context_identity")
        if not isinstance(self.release_evidence, bool):
            fail("KnownTestFailureReport.release_evidence", "must be a boolean")
        if not isinstance(self.summary, KnownTestFailureSummary):
            fail("KnownTestFailureReport.summary", "must be a summary")
        if not isinstance(self.outcomes, tuple):
            fail("KnownTestFailureReport.outcomes", "must be a tuple")
        for index, outcome in enumerate(self.outcomes):
            if not isinstance(outcome, KnownTestFailureOutcome):
                fail(f"KnownTestFailureReport.outcomes[{index}]", "must be an outcome")
        keys = tuple(item.test_key for item in self.outcomes)
        unique(keys, "KnownTestFailureReport.outcomes", "test keys")
        counts = {status: 0 for status in TestOutcomeStatus}
        for outcome in self.outcomes:
            counts[outcome.status] += 1
        if (
            self.summary.passed != counts[TestOutcomeStatus.PASSED]
            or self.summary.failed != counts[TestOutcomeStatus.FAILED]
            or self.summary.known_failed != counts[TestOutcomeStatus.KNOWN_FAILURE]
            or self.summary.skipped != counts[TestOutcomeStatus.SKIPPED]
        ):
            fail("KnownTestFailureReport.summary", "must match exact outcome counts")
        if self.release_evidence and self.summary.known_failed:
            fail(
                "KnownTestFailureReport",
                "release evidence cannot reuse known failures",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def passing(self) -> bool:
        accounted = (
            self.summary.passed
            + self.summary.failed
            + self.summary.known_failed
            + self.summary.skipped
        )
        return (
            self.summary.selected > 0
            and accounted == self.summary.selected
            and self.summary.failed == 0
            and self.summary.known_failed == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "suite": self.suite,
            "pin_identity": self.pin_identity.to_dict(),
            "context_identity": (
                None
                if self.context_identity is None
                else self.context_identity.to_dict()
            ),
            "release_evidence": self.release_evidence,
            "summary": self.summary.to_dict(),
            "outcomes": [item.to_dict() for item in self.outcomes],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "KnownTestFailureReport"
    ) -> KnownTestFailureReport:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "suite",
                    "pin_identity",
                    "context_identity",
                    "release_evidence",
                    "summary",
                    "outcomes",
                }
            ),
        )
        release = data["release_evidence"]
        if not isinstance(release, bool):
            fail(f"{path}.release_evidence", "must be a boolean")
        return cls(
            suite=string_value(data["suite"], f"{path}.suite"),
            pin_identity=ContentIdentity.from_dict(
                data["pin_identity"], path=f"{path}.pin_identity"
            ),
            context_identity=_optional_identity(
                data["context_identity"], f"{path}.context_identity"
            ),
            release_evidence=release,
            summary=KnownTestFailureSummary.from_dict(
                data["summary"], path=f"{path}.summary"
            ),
            outcomes=tuple(
                KnownTestFailureOutcome.from_dict(
                    item, path=f"{path}.outcomes[{index}]"
                )
                for index, item in enumerate(
                    list_value(data["outcomes"], f"{path}.outcomes")
                )
            ),
        )


__all__ = [
    "KNOWN_TEST_FAILURE_ANNOTATION_SCHEMA",
    "KNOWN_TEST_FAILURE_OUTCOME_SCHEMA",
    "KNOWN_TEST_FAILURE_REPORT_SCHEMA",
    "KnownTestFailureAnnotation",
    "KnownTestFailureCause",
    "KnownTestFailureOutcome",
    "KnownTestFailurePolicy",
    "KnownTestFailureReport",
    "KnownTestFailureSummary",
    "TestOutcomeStatus",
]
