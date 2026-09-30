"""Canonical, passing-only project test receipt contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    fail,
    fields,
    int_value,
    list_value,
    string_tuple,
    string_value,
    unique,
)
from .identity import (
    SCHEMA_PREFIX,
    ContentIdentity,
    VersionedContentRef,
    contract_identity,
)
from .versioning import semantic_version

PROJECT_TEST_RECEIPT_SCHEMA = f"{SCHEMA_PREFIX}project-test-receipt"
PROJECT_TEST_RECEIPT_PROVISIONAL_SCHEMA = (
    f"{SCHEMA_PREFIX}project-test-receipt-provisional"
)
PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA = (
    f"{SCHEMA_PREFIX}project-test-receipt-finalized-candidate"
)
PROJECT_TEST_RECEIPT_POLICY_SCHEMA = f"{SCHEMA_PREFIX}project-test-receipt-policy"
PROJECT_TEST_RECEIPT_FINALIZATION_BOUNDARY = "supported-api-tcb"
PROJECT_TEST_SUITE_KIND = "test-suite"
PROJECT_TEST_OUTCOME_PASSED = "passed"
PROJECT_TEST_RUNNER_EVIDENCE_KIND = "test-runner"
PROJECT_TEST_EVIDENCE_KINDS = frozenset(
    {
        "build-result",
        "acceptance-result",
        "artifact-publication-result",
        "cache-directory-custody",
        "source-intelligence",
        "deployment-result",
        "generation-provenance",
        "lifecycle-command",
        "lifecycle-plan",
        "lifecycle-request",
        "observation-result",
        "package-result",
        "publication-result",
        "resolved-sbom",
        "security-scan-report",
        "source-cache-publication-result",
        "source-cache-decision",
        "source-cache-lifecycle",
        "source-sbom",
        "test-report",
        PROJECT_TEST_RUNNER_EVIDENCE_KIND,
        "workspace-admission",
    }
)
MAX_PROJECT_TEST_EVIDENCE = 32


def _content_identity(value: Any, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


def _content_identity_uri(value: Any, path: str) -> ContentIdentity:
    uri = string_value(value, path)
    try:
        return ContentIdentity.parse_uri(uri)
    except ValueError:
        fail(path, "must be a sha256 content-identity URI")


def _component_lock_identities(
    values: tuple[ContentIdentity, ...], path: str
) -> tuple[ContentIdentity, ...]:
    if not values or len(values) > 4096:
        fail(path, "must contain 1 through 4096 exact Component lock identities")
    for index, identity in enumerate(values):
        _content_identity(identity, f"{path}[{index}]")
    uris = tuple(identity.uri for identity in values)
    unique(uris, path, "Component lock identities")
    if uris != tuple(sorted(uris)):
        fail(path, "must use canonical identity order")
    return values


@dataclass(frozen=True, slots=True)
class ProjectTestReceiptPolicy:
    """Project-owned admission policy for a compact passing test receipt."""

    suite_id: str
    suite_version: str
    runner_identity: ContentIdentity
    required_evidence_kinds: tuple[str, ...]
    minimum_test_count: int

    SCHEMA: ClassVar[str] = PROJECT_TEST_RECEIPT_POLICY_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.suite_id, "ProjectTestReceiptPolicy.suite_id")
        semantic_version(self.suite_version, "ProjectTestReceiptPolicy.suite_version")
        _content_identity(
            self.runner_identity, "ProjectTestReceiptPolicy.runner_identity"
        )
        if not isinstance(self.required_evidence_kinds, tuple):
            fail(
                "ProjectTestReceiptPolicy.required_evidence_kinds",
                "must be a tuple",
            )
        unique(
            self.required_evidence_kinds,
            "ProjectTestReceiptPolicy.required_evidence_kinds",
        )
        if self.required_evidence_kinds != tuple(sorted(self.required_evidence_kinds)):
            fail(
                "ProjectTestReceiptPolicy.required_evidence_kinds",
                "must be sorted by evidence kind",
            )
        unknown = set(self.required_evidence_kinds) - PROJECT_TEST_EVIDENCE_KINDS
        if unknown:
            fail(
                "ProjectTestReceiptPolicy.required_evidence_kinds",
                "contains unsupported evidence kinds: " + ", ".join(sorted(unknown)),
            )
        if PROJECT_TEST_RUNNER_EVIDENCE_KIND not in self.required_evidence_kinds:
            fail(
                "ProjectTestReceiptPolicy.required_evidence_kinds",
                f"must include {PROJECT_TEST_RUNNER_EVIDENCE_KIND!r}",
            )
        int_value(
            self.minimum_test_count,
            "ProjectTestReceiptPolicy.minimum_test_count",
            minimum=1,
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "runner_identity": self.runner_identity.to_dict(),
            "required_evidence_kinds": list(self.required_evidence_kinds),
            "minimum_test_count": self.minimum_test_count,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProjectTestReceiptPolicy"
    ) -> ProjectTestReceiptPolicy:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "suite_id",
                    "suite_version",
                    "runner_identity",
                    "required_evidence_kinds",
                    "minimum_test_count",
                }
            ),
        )
        return cls(
            suite_id=string_value(data["suite_id"], f"{path}.suite_id"),
            suite_version=string_value(data["suite_version"], f"{path}.suite_version"),
            runner_identity=ContentIdentity.from_dict(
                data["runner_identity"], path=f"{path}.runner_identity"
            ),
            required_evidence_kinds=string_tuple(
                data["required_evidence_kinds"],
                f"{path}.required_evidence_kinds",
            ),
            minimum_test_count=int_value(
                data["minimum_test_count"],
                f"{path}.minimum_test_count",
                minimum=1,
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectTestEvidence:
    """The typed identity of one external artifact supporting a test receipt."""

    kind: str
    identity: ContentIdentity

    def __post_init__(self) -> None:
        kind = string_value(self.kind, "ProjectTestEvidence.kind")
        if kind not in PROJECT_TEST_EVIDENCE_KINDS:
            allowed = ", ".join(sorted(PROJECT_TEST_EVIDENCE_KINDS))
            fail("ProjectTestEvidence.kind", f"must be one of: {allowed}")
        _content_identity(self.identity, "ProjectTestEvidence.identity")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "identity": self.identity.to_dict()}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProjectTestEvidence"
    ) -> ProjectTestEvidence:
        data = fields(
            value,
            path=path,
            required=frozenset({"kind", "identity"}),
        )
        return cls(
            kind=string_value(data["kind"], f"{path}.kind"),
            identity=ContentIdentity.from_dict(
                data["identity"], path=f"{path}.identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectTestSummary:
    """A complete all-passing test count summary."""

    total: int
    passed: int
    failed: int
    skipped: int

    def __post_init__(self) -> None:
        total = int_value(self.total, "ProjectTestSummary.total", minimum=1)
        passed = int_value(self.passed, "ProjectTestSummary.passed")
        failed = int_value(self.failed, "ProjectTestSummary.failed")
        skipped = int_value(self.skipped, "ProjectTestSummary.skipped")
        if passed != total or failed != 0 or skipped != 0:
            fail(
                "ProjectTestSummary",
                "must account for a non-empty suite with every test passed and none "
                "failed or skipped",
            )

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProjectTestSummary"
    ) -> ProjectTestSummary:
        data = fields(
            value,
            path=path,
            required=frozenset({"total", "passed", "failed", "skipped"}),
        )
        return cls(
            total=int_value(data["total"], f"{path}.total", minimum=1),
            passed=int_value(data["passed"], f"{path}.passed"),
            failed=int_value(data["failed"], f"{path}.failed"),
            skipped=int_value(data["skipped"], f"{path}.skipped"),
        )


@dataclass(frozen=True, slots=True)
class ProjectTestReceipt:
    """A canonical receipt for one exact, wholly passing project test run."""

    project_id: str
    project_revision_identity: ContentIdentity
    subject_identity: ContentIdentity
    suite: VersionedContentRef
    outcome: str
    summary: ProjectTestSummary
    result_identity: ContentIdentity
    evidence: tuple[ProjectTestEvidence, ...]

    SCHEMA: ClassVar[str] = PROJECT_TEST_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.project_id, "ProjectTestReceipt.project_id")
        _content_identity(
            self.project_revision_identity,
            "ProjectTestReceipt.project_revision_identity",
        )
        _content_identity(self.subject_identity, "ProjectTestReceipt.subject_identity")
        if not isinstance(self.suite, VersionedContentRef):
            fail("ProjectTestReceipt.suite", "must be a VersionedContentRef")
        if self.suite.kind != PROJECT_TEST_SUITE_KIND:
            fail(
                "ProjectTestReceipt.suite.kind",
                f"must be {PROJECT_TEST_SUITE_KIND!r}",
            )
        outcome = string_value(self.outcome, "ProjectTestReceipt.outcome")
        if outcome != PROJECT_TEST_OUTCOME_PASSED:
            fail(
                "ProjectTestReceipt.outcome",
                f"must be {PROJECT_TEST_OUTCOME_PASSED!r}",
            )
        if not isinstance(self.summary, ProjectTestSummary):
            fail("ProjectTestReceipt.summary", "must be a ProjectTestSummary")
        _content_identity(self.result_identity, "ProjectTestReceipt.result_identity")
        if not isinstance(self.evidence, tuple):
            fail("ProjectTestReceipt.evidence", "must be a tuple")
        if len(self.evidence) > MAX_PROJECT_TEST_EVIDENCE:
            fail(
                "ProjectTestReceipt.evidence",
                f"must contain at most {MAX_PROJECT_TEST_EVIDENCE} items",
            )
        keys: list[tuple[str, str]] = []
        for index, item in enumerate(self.evidence):
            if not isinstance(item, ProjectTestEvidence):
                fail(
                    f"ProjectTestReceipt.evidence[{index}]",
                    "must be a ProjectTestEvidence",
                )
            keys.append((item.kind, item.identity.uri))
        kinds = tuple(key[0] for key in keys)
        if tuple(keys) != tuple(sorted(keys)) or len(set(kinds)) != len(kinds):
            fail(
                "ProjectTestReceipt.evidence",
                "must be sorted by kind and contain each evidence kind at most once",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        """Return the intentionally tiny, canonical Git receipt wire shape."""

        return {
            "schema": self.SCHEMA,
            "project": self.project_id,
            "project_revision": self.project_revision_identity.uri,
            "subject": self.subject_identity.uri,
            "suite": {
                "id": self.suite.identifier,
                "version": self.suite.version,
                "revision": self.suite.content_identity.uri,
            },
            "tests": self.summary.total,
            "result": self.result_identity.uri,
            "evidence": {item.kind: item.identity.uri for item in self.evidence},
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProjectTestReceipt"
    ) -> ProjectTestReceipt:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "project",
                    "project_revision",
                    "subject",
                    "suite",
                    "tests",
                    "result",
                    "evidence",
                }
            ),
        )
        suite = fields(
            data["suite"],
            path=f"{path}.suite",
            required=frozenset({"id", "version", "revision"}),
        )
        evidence_value = data["evidence"]
        if not isinstance(evidence_value, dict):
            fail(f"{path}.evidence", "must be an object keyed by evidence kind")
        if len(evidence_value) > MAX_PROJECT_TEST_EVIDENCE:
            fail(
                f"{path}.evidence",
                f"must contain at most {MAX_PROJECT_TEST_EVIDENCE} items",
            )
        evidence = tuple(
            ProjectTestEvidence(
                string_value(kind, f"{path}.evidence kind"),
                _content_identity_uri(identity, f"{path}.evidence.{kind}"),
            )
            for kind, identity in sorted(evidence_value.items())
        )
        total = int_value(data["tests"], f"{path}.tests", minimum=1)
        return cls(
            project_id=string_value(data["project"], f"{path}.project"),
            project_revision_identity=_content_identity_uri(
                data["project_revision"], f"{path}.project_revision"
            ),
            subject_identity=_content_identity_uri(data["subject"], f"{path}.subject"),
            suite=VersionedContentRef(
                PROJECT_TEST_SUITE_KIND,
                string_value(suite["id"], f"{path}.suite.id"),
                string_value(suite["version"], f"{path}.suite.version"),
                _content_identity_uri(suite["revision"], f"{path}.suite.revision"),
            ),
            outcome=PROJECT_TEST_OUTCOME_PASSED,
            summary=ProjectTestSummary(total, total, 0, 0),
            result_identity=_content_identity_uri(data["result"], f"{path}.result"),
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class ProjectTestReceiptProvisional:
    """Unpromotable driver assertion awaiting outer lifecycle finalization."""

    lifecycle_request_identity: ContentIdentity
    lifecycle_command_identity: ContentIdentity
    source_cache_control_identity: ContentIdentity
    component_lock_identities: tuple[ContentIdentity, ...]
    receipt_identity: ContentIdentity
    receipt: ProjectTestReceipt

    SCHEMA: ClassVar[str] = PROJECT_TEST_RECEIPT_PROVISIONAL_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "lifecycle_request_identity",
            "lifecycle_command_identity",
            "source_cache_control_identity",
            "receipt_identity",
        ):
            _content_identity(
                getattr(self, name), f"ProjectTestReceiptProvisional.{name}"
            )
        _component_lock_identities(
            self.component_lock_identities,
            "ProjectTestReceiptProvisional.component_lock_identities",
        )
        if not isinstance(self.receipt, ProjectTestReceipt):
            fail(
                "ProjectTestReceiptProvisional.receipt",
                "must be a ProjectTestReceipt",
            )
        if self.receipt_identity != self.receipt.identity:
            fail(
                "ProjectTestReceiptProvisional.receipt_identity",
                "must identify the exact nested receipt",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def validate_finalization_context(
        self,
        *,
        lifecycle_request_identity: ContentIdentity,
        lifecycle_command_identity: ContentIdentity,
        source_cache_control_identity: ContentIdentity,
        component_lock_identities: tuple[ContentIdentity, ...],
    ) -> None:
        expected = {
            "lifecycle_request_identity": lifecycle_request_identity,
            "lifecycle_command_identity": lifecycle_command_identity,
            "source_cache_control_identity": source_cache_control_identity,
        }
        for name, identity in expected.items():
            _content_identity(identity, f"ProjectTestReceiptProvisional.{name}")
            if getattr(self, name) != identity:
                fail(
                    f"ProjectTestReceiptProvisional.{name}",
                    "does not bind the outer CLI finalization context",
                )
        _component_lock_identities(
            component_lock_identities,
            "ProjectTestReceiptProvisional.component_lock_identities",
        )
        if self.component_lock_identities != component_lock_identities:
            fail(
                "ProjectTestReceiptProvisional.component_lock_identities",
                "does not bind the exact planned Component lock set",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "lifecycle_request_identity": self.lifecycle_request_identity.uri,
            "lifecycle_command_identity": self.lifecycle_command_identity.uri,
            "source_cache_control_identity": self.source_cache_control_identity.uri,
            "component_lock_identities": [
                identity.uri for identity in self.component_lock_identities
            ],
            "receipt_identity": self.receipt_identity.uri,
            "receipt": self.receipt.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProjectTestReceiptProvisional"
    ) -> ProjectTestReceiptProvisional:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "lifecycle_request_identity",
                    "lifecycle_command_identity",
                    "source_cache_control_identity",
                    "component_lock_identities",
                    "receipt_identity",
                    "receipt",
                }
            ),
        )
        return cls(
            lifecycle_request_identity=_content_identity_uri(
                data["lifecycle_request_identity"],
                f"{path}.lifecycle_request_identity",
            ),
            lifecycle_command_identity=_content_identity_uri(
                data["lifecycle_command_identity"],
                f"{path}.lifecycle_command_identity",
            ),
            source_cache_control_identity=_content_identity_uri(
                data["source_cache_control_identity"],
                f"{path}.source_cache_control_identity",
            ),
            component_lock_identities=tuple(
                _content_identity_uri(
                    identity,
                    f"{path}.component_lock_identities[{index}]",
                )
                for index, identity in enumerate(
                    list_value(
                        data["component_lock_identities"],
                        f"{path}.component_lock_identities",
                    )
                )
            ),
            receipt_identity=_content_identity_uri(
                data["receipt_identity"], f"{path}.receipt_identity"
            ),
            receipt=ProjectTestReceipt.from_dict(
                data["receipt"], path=f"{path}.receipt"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectTestReceiptFinalizedCandidate:
    """Outer-finalized candidate accepted by the public promotion API.

    This envelope distinguishes the supported ``litai rebuild`` finalization path from
    a lifecycle-driver assertion.  It is a typed TCB boundary, not a cryptographic
    attestation: deployments that do not trust the project driver and invoking user
    need an operator-owned signing or ledger layer above this contract.
    """

    provisional_identity: ContentIdentity
    lifecycle_request_identity: ContentIdentity
    lifecycle_command_identity: ContentIdentity
    source_cache_control_identity: ContentIdentity
    component_lock_identities: tuple[ContentIdentity, ...]
    source_cache_decision_identity: ContentIdentity
    source_cache_lifecycle_identity: ContentIdentity
    receipt_identity: ContentIdentity
    receipt: ProjectTestReceipt

    SCHEMA: ClassVar[str] = PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "provisional_identity",
            "lifecycle_request_identity",
            "lifecycle_command_identity",
            "source_cache_control_identity",
            "source_cache_decision_identity",
            "source_cache_lifecycle_identity",
            "receipt_identity",
        ):
            _content_identity(
                getattr(self, name),
                f"ProjectTestReceiptFinalizedCandidate.{name}",
            )
        _component_lock_identities(
            self.component_lock_identities,
            "ProjectTestReceiptFinalizedCandidate.component_lock_identities",
        )
        if not isinstance(self.receipt, ProjectTestReceipt):
            fail(
                "ProjectTestReceiptFinalizedCandidate.receipt",
                "must be a ProjectTestReceipt",
            )
        if self.receipt_identity != self.receipt.identity:
            fail(
                "ProjectTestReceiptFinalizedCandidate.receipt_identity",
                "must identify the exact nested receipt",
            )
        reconstructed = ProjectTestReceiptProvisional(
            lifecycle_request_identity=self.lifecycle_request_identity,
            lifecycle_command_identity=self.lifecycle_command_identity,
            source_cache_control_identity=self.source_cache_control_identity,
            component_lock_identities=self.component_lock_identities,
            receipt_identity=self.receipt_identity,
            receipt=self.receipt,
        )
        if self.provisional_identity != reconstructed.identity:
            fail(
                "ProjectTestReceiptFinalizedCandidate.provisional_identity",
                "must identify the provisional assertion reconstructed from the "
                "exact outer context and nested receipt",
            )
        evidence = {item.kind: item.identity for item in self.receipt.evidence}
        expected_evidence = {
            "lifecycle-request": self.lifecycle_request_identity,
            "lifecycle-command": self.lifecycle_command_identity,
            "source-cache-decision": self.source_cache_decision_identity,
            "source-cache-lifecycle": self.source_cache_lifecycle_identity,
        }
        for kind, expected in expected_evidence.items():
            if evidence.get(kind) != expected:
                fail(
                    f"ProjectTestReceiptFinalizedCandidate.{kind}",
                    "must match the exact typed nested receipt evidence",
                )

    @classmethod
    def finalize(
        cls,
        provisional: ProjectTestReceiptProvisional,
        *,
        source_cache_decision_identity: ContentIdentity,
        source_cache_lifecycle_identity: ContentIdentity,
    ) -> ProjectTestReceiptFinalizedCandidate:
        if not isinstance(provisional, ProjectTestReceiptProvisional):
            fail(
                "ProjectTestReceiptFinalizedCandidate.provisional",
                "must be a ProjectTestReceiptProvisional",
            )
        return cls(
            provisional_identity=provisional.identity,
            lifecycle_request_identity=provisional.lifecycle_request_identity,
            lifecycle_command_identity=provisional.lifecycle_command_identity,
            source_cache_control_identity=provisional.source_cache_control_identity,
            component_lock_identities=provisional.component_lock_identities,
            source_cache_decision_identity=source_cache_decision_identity,
            source_cache_lifecycle_identity=source_cache_lifecycle_identity,
            receipt_identity=provisional.receipt_identity,
            receipt=provisional.receipt,
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "finalization_boundary": PROJECT_TEST_RECEIPT_FINALIZATION_BOUNDARY,
            "provisional_identity": self.provisional_identity.uri,
            "lifecycle_request_identity": self.lifecycle_request_identity.uri,
            "lifecycle_command_identity": self.lifecycle_command_identity.uri,
            "source_cache_control_identity": self.source_cache_control_identity.uri,
            "component_lock_identities": [
                identity.uri for identity in self.component_lock_identities
            ],
            "source_cache_decision_identity": self.source_cache_decision_identity.uri,
            "source_cache_lifecycle_identity": self.source_cache_lifecycle_identity.uri,
            "receipt_identity": self.receipt_identity.uri,
            "receipt": self.receipt.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "ProjectTestReceiptFinalizedCandidate",
    ) -> ProjectTestReceiptFinalizedCandidate:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "finalization_boundary",
                    "provisional_identity",
                    "lifecycle_request_identity",
                    "lifecycle_command_identity",
                    "source_cache_control_identity",
                    "component_lock_identities",
                    "source_cache_decision_identity",
                    "source_cache_lifecycle_identity",
                    "receipt_identity",
                    "receipt",
                }
            ),
        )
        boundary = string_value(
            data["finalization_boundary"], f"{path}.finalization_boundary"
        )
        if boundary != PROJECT_TEST_RECEIPT_FINALIZATION_BOUNDARY:
            fail(
                f"{path}.finalization_boundary",
                "must identify the supported outer-CLI TCB finalization boundary",
            )
        return cls(
            provisional_identity=_content_identity_uri(
                data["provisional_identity"], f"{path}.provisional_identity"
            ),
            lifecycle_request_identity=_content_identity_uri(
                data["lifecycle_request_identity"],
                f"{path}.lifecycle_request_identity",
            ),
            lifecycle_command_identity=_content_identity_uri(
                data["lifecycle_command_identity"],
                f"{path}.lifecycle_command_identity",
            ),
            source_cache_control_identity=_content_identity_uri(
                data["source_cache_control_identity"],
                f"{path}.source_cache_control_identity",
            ),
            component_lock_identities=tuple(
                _content_identity_uri(
                    identity,
                    f"{path}.component_lock_identities[{index}]",
                )
                for index, identity in enumerate(
                    list_value(
                        data["component_lock_identities"],
                        f"{path}.component_lock_identities",
                    )
                )
            ),
            source_cache_decision_identity=_content_identity_uri(
                data["source_cache_decision_identity"],
                f"{path}.source_cache_decision_identity",
            ),
            source_cache_lifecycle_identity=_content_identity_uri(
                data["source_cache_lifecycle_identity"],
                f"{path}.source_cache_lifecycle_identity",
            ),
            receipt_identity=_content_identity_uri(
                data["receipt_identity"], f"{path}.receipt_identity"
            ),
            receipt=ProjectTestReceipt.from_dict(
                data["receipt"], path=f"{path}.receipt"
            ),
        )


__all__ = [
    "MAX_PROJECT_TEST_EVIDENCE",
    "PROJECT_TEST_EVIDENCE_KINDS",
    "PROJECT_TEST_OUTCOME_PASSED",
    "PROJECT_TEST_RECEIPT_POLICY_SCHEMA",
    "PROJECT_TEST_RECEIPT_FINALIZATION_BOUNDARY",
    "PROJECT_TEST_RECEIPT_FINALIZED_CANDIDATE_SCHEMA",
    "PROJECT_TEST_RECEIPT_PROVISIONAL_SCHEMA",
    "PROJECT_TEST_RECEIPT_SCHEMA",
    "PROJECT_TEST_RUNNER_EVIDENCE_KIND",
    "PROJECT_TEST_SUITE_KIND",
    "ProjectTestEvidence",
    "ProjectTestReceipt",
    "ProjectTestReceiptFinalizedCandidate",
    "ProjectTestReceiptPolicy",
    "ProjectTestReceiptProvisional",
    "ProjectTestSummary",
]
