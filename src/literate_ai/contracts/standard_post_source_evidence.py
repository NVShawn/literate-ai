"""Strict, provider-neutral evidence for successful Standard post-source stages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    fail,
    int_value,
    parse_tuple,
    string_value,
    unique,
)
from .executable_components import ArtifactExport
from .identity import ContentIdentity, contract_identity
from .sbom import CycloneDxBomBinding, CycloneDxLifecycle
from .standard_execution_inputs import StandardExecutionAuthority

STANDARD_BUILD_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-component-build-evidence"
)
STANDARD_GENERATED_TEST_CASE_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-generated-test-case-evidence"
)
STANDARD_GENERATED_TEST_EXECUTION_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-generated-test-execution-evidence"
)
STANDARD_ENTRYPOINT_GENERATED_TEST_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-entrypoint-generated-test-evidence"
)
STANDARD_EXECUTION_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-component-execution-evidence"
)
STANDARD_ENTRYPOINT_EXECUTION_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-entrypoint-execution-evidence"
)
STANDARD_COMPONENT_ACCEPTANCE_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-component-acceptance-evidence"
)

MAX_STANDARD_EXPORTS = 16384
MAX_STANDARD_GENERATED_TEST_CASES = 256
MAX_STANDARD_GENERATED_TEST_OBSERVATIONS = 16384


class StandardEvidenceOutcome(StrEnum):
    PASSED = "passed"


def _identity(value: object, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


def _identity_tuple(
    values: tuple[ContentIdentity, ...],
    path: str,
    *,
    nonempty: bool = True,
    maximum: int = MAX_STANDARD_EXPORTS,
    canonical: bool = True,
) -> tuple[ContentIdentity, ...]:
    if nonempty and not values:
        fail(path, "must not be empty")
    if len(values) > maximum:
        fail(path, f"must contain at most {maximum} identities")
    for index, value in enumerate(values):
        _identity(value, f"{path}[{index}]")
    uris = tuple(value.uri for value in values)
    unique(uris, path, "identities")
    if canonical and uris != tuple(sorted(uris)):
        fail(path, "must use canonical identity order")
    return values


def _outcome(value: object, path: str) -> StandardEvidenceOutcome:
    if value is not StandardEvidenceOutcome.PASSED:
        fail(path, "must be the passed outcome")
    return StandardEvidenceOutcome.PASSED


@dataclass(frozen=True, slots=True)
class StandardBuildEvidence:
    """A successful build, its custody, exact exports, and resolved SBOM transition."""

    component_revision: ContentIdentity
    build_plan_identity: ContentIdentity
    source_tree_identity: ContentIdentity
    source_custody_identity: ContentIdentity
    source_sbom: CycloneDxBomBinding
    resolved_sbom: CycloneDxBomBinding
    exports: tuple[ArtifactExport, ...]
    resolved_sbom_export_identities: tuple[ContentIdentity, ...]
    build_observation_identity: ContentIdentity
    artifact_custody_identity: ContentIdentity
    outcome: StandardEvidenceOutcome = StandardEvidenceOutcome.PASSED

    SCHEMA: ClassVar[str] = STANDARD_BUILD_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "build_plan_identity",
            "source_tree_identity",
            "source_custody_identity",
            "build_observation_identity",
            "artifact_custody_identity",
        ):
            _identity(getattr(self, name), f"StandardBuildEvidence.{name}")
        if not isinstance(self.source_sbom, CycloneDxBomBinding):
            fail("StandardBuildEvidence.source_sbom", "must be a CycloneDxBomBinding")
        if not isinstance(self.resolved_sbom, CycloneDxBomBinding):
            fail("StandardBuildEvidence.resolved_sbom", "must be a CycloneDxBomBinding")
        if self.source_sbom.lifecycle is not CycloneDxLifecycle.SOURCE:
            fail("StandardBuildEvidence.source_sbom", "must be a source lifecycle BOM")
        if self.resolved_sbom.lifecycle is not CycloneDxLifecycle.RESOLVED:
            fail(
                "StandardBuildEvidence.resolved_sbom",
                "must be a resolved lifecycle BOM",
            )
        if (
            self.resolved_sbom.source_bom_identity != self.source_sbom.bom_identity
            or self.resolved_sbom.managed_graph_identity
            != self.source_sbom.managed_graph_identity
            or self.resolved_sbom.resolved_graph_identity
            != self.source_sbom.resolved_graph_identity
            or self.resolved_sbom.root_ref != self.source_sbom.root_ref
        ):
            fail(
                "StandardBuildEvidence.resolved_sbom",
                "must bind the exact source BOM, managed graph, resolved graph, "
                "and root",
            )
        if not self.exports or len(self.exports) > MAX_STANDARD_EXPORTS:
            fail(
                "StandardBuildEvidence.exports",
                f"must contain between 1 and {MAX_STANDARD_EXPORTS} exports",
            )
        for index, export in enumerate(self.exports):
            if not isinstance(export, ArtifactExport):
                fail(
                    f"StandardBuildEvidence.exports[{index}]",
                    "must be an ArtifactExport",
                )
            if (
                export.component_revision != self.component_revision
                or export.source_tree_identity != self.source_tree_identity
            ):
                fail(
                    f"StandardBuildEvidence.exports[{index}]",
                    "must bind the exact Component revision and source tree",
                )
        export_ids = tuple(export.export_id for export in self.exports)
        unique(export_ids, "StandardBuildEvidence.exports", "export IDs")
        if export_ids != tuple(sorted(export_ids)):
            fail("StandardBuildEvidence.exports", "must use canonical export ID order")
        export_identities = tuple(export.identity for export in self.exports)
        _identity_tuple(
            export_identities, "StandardBuildEvidence.exports", canonical=False
        )
        _identity_tuple(
            self.resolved_sbom_export_identities,
            "StandardBuildEvidence.resolved_sbom_export_identities",
            canonical=False,
        )
        if self.resolved_sbom_export_identities != export_identities:
            fail(
                "StandardBuildEvidence.resolved_sbom_export_identities",
                "must contain every and only exact built export",
            )
        _outcome(self.outcome, "StandardBuildEvidence.outcome")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def export_identities(self) -> tuple[ContentIdentity, ...]:
        return tuple(export.identity for export in self.exports)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "build_plan_identity": self.build_plan_identity.to_dict(),
            "source_tree_identity": self.source_tree_identity.to_dict(),
            "source_custody_identity": self.source_custody_identity.to_dict(),
            "source_sbom": self.source_sbom.to_dict(),
            "resolved_sbom": self.resolved_sbom.to_dict(),
            "exports": [item.to_dict() for item in self.exports],
            "resolved_sbom_export_identities": [
                item.to_dict() for item in self.resolved_sbom_export_identities
            ],
            "build_observation_identity": self.build_observation_identity.to_dict(),
            "artifact_custody_identity": self.artifact_custody_identity.to_dict(),
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardBuildEvidence"
    ) -> StandardBuildEvidence:
        names = frozenset(
            {
                "component_revision",
                "build_plan_identity",
                "source_tree_identity",
                "source_custody_identity",
                "source_sbom",
                "resolved_sbom",
                "exports",
                "resolved_sbom_export_identities",
                "build_observation_identity",
                "artifact_custody_identity",
                "outcome",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            build_plan_identity=ContentIdentity.from_dict(
                data["build_plan_identity"], path=f"{path}.build_plan_identity"
            ),
            source_tree_identity=ContentIdentity.from_dict(
                data["source_tree_identity"], path=f"{path}.source_tree_identity"
            ),
            source_custody_identity=ContentIdentity.from_dict(
                data["source_custody_identity"], path=f"{path}.source_custody_identity"
            ),
            source_sbom=CycloneDxBomBinding.from_dict(
                data["source_sbom"], path=f"{path}.source_sbom"
            ),
            resolved_sbom=CycloneDxBomBinding.from_dict(
                data["resolved_sbom"], path=f"{path}.resolved_sbom"
            ),
            exports=parse_tuple(
                data["exports"], f"{path}.exports", ArtifactExport.from_dict
            ),
            resolved_sbom_export_identities=parse_tuple(
                data["resolved_sbom_export_identities"],
                f"{path}.resolved_sbom_export_identities",
                ContentIdentity.from_dict,
            ),
            build_observation_identity=ContentIdentity.from_dict(
                data["build_observation_identity"],
                path=f"{path}.build_observation_identity",
            ),
            artifact_custody_identity=ContentIdentity.from_dict(
                data["artifact_custody_identity"],
                path=f"{path}.artifact_custody_identity",
            ),
            outcome=_parse_outcome(data["outcome"], f"{path}.outcome"),
        )


@dataclass(frozen=True, slots=True)
class StandardGeneratedTestCaseEvidence:
    """One selected generated test and the exact observation of its execution."""

    case_id: str
    case_identity: ContentIdentity
    observation_identity: ContentIdentity
    outcome: StandardEvidenceOutcome = StandardEvidenceOutcome.PASSED

    SCHEMA: ClassVar[str] = STANDARD_GENERATED_TEST_CASE_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        string_value(
            self.case_id, "StandardGeneratedTestCaseEvidence.case_id", max_length=255
        )
        _identity(self.case_identity, "StandardGeneratedTestCaseEvidence.case_identity")
        _identity(
            self.observation_identity,
            "StandardGeneratedTestCaseEvidence.observation_identity",
        )
        _outcome(self.outcome, "StandardGeneratedTestCaseEvidence.outcome")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "case_id": self.case_id,
            "case_identity": self.case_identity.to_dict(),
            "observation_identity": self.observation_identity.to_dict(),
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardGeneratedTestCaseEvidence"
    ) -> StandardGeneratedTestCaseEvidence:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"case_id", "case_identity", "observation_identity", "outcome"}
            ),
        )
        return cls(
            string_value(data["case_id"], f"{path}.case_id", max_length=255),
            ContentIdentity.from_dict(
                data["case_identity"], path=f"{path}.case_identity"
            ),
            ContentIdentity.from_dict(
                data["observation_identity"], path=f"{path}.observation_identity"
            ),
            _parse_outcome(data["outcome"], f"{path}.outcome"),
        )


@dataclass(frozen=True, slots=True)
class StandardEntrypointGeneratedTestEvidence:
    """Passing generated-test evidence attributable to one deployment unit."""

    entrypoint_identity: ContentIdentity
    deployment_unit: str
    export_identity: ContentIdentity
    command_identity: ContentIdentity
    runner_identity: ContentIdentity
    process_observation_identity: ContentIdentity
    test_custody_identity: ContentIdentity
    selected_case_identities: tuple[ContentIdentity, ...]
    cases: tuple[StandardGeneratedTestCaseEvidence, ...]
    selected_count: int
    executed_count: int
    passed_count: int
    outcome: StandardEvidenceOutcome = StandardEvidenceOutcome.PASSED

    SCHEMA: ClassVar[str] = STANDARD_ENTRYPOINT_GENERATED_TEST_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "entrypoint_identity",
            "export_identity",
            "command_identity",
            "runner_identity",
            "process_observation_identity",
            "test_custody_identity",
        ):
            _identity(
                getattr(self, name),
                f"StandardEntrypointGeneratedTestEvidence.{name}",
            )
        string_value(
            self.deployment_unit,
            "StandardEntrypointGeneratedTestEvidence.deployment_unit",
            max_length=4096,
        )
        _identity_tuple(
            self.selected_case_identities,
            "StandardEntrypointGeneratedTestEvidence.selected_case_identities",
            maximum=MAX_STANDARD_GENERATED_TEST_CASES,
        )
        if not self.cases or len(self.cases) > MAX_STANDARD_GENERATED_TEST_CASES:
            fail(
                "StandardEntrypointGeneratedTestEvidence.cases",
                "must contain a bounded non-empty set of cases",
            )
        if any(
            not isinstance(item, StandardGeneratedTestCaseEvidence)
            for item in self.cases
        ):
            fail(
                "StandardEntrypointGeneratedTestEvidence.cases",
                "must contain StandardGeneratedTestCaseEvidence values",
            )
        if tuple(item.case_identity for item in self.cases) != (
            self.selected_case_identities
        ):
            fail(
                "StandardEntrypointGeneratedTestEvidence.cases",
                "must execute every and only selected case in canonical order",
            )
        counts = (self.selected_count, self.executed_count, self.passed_count)
        if (
            any(type(item) is not int for item in counts)
            or counts != (len(self.cases),) * 3
        ):
            fail(
                "StandardEntrypointGeneratedTestEvidence",
                "selected, executed, and passed counts must equal case membership",
            )
        _outcome(self.outcome, "StandardEntrypointGeneratedTestEvidence.outcome")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "entrypoint_identity": self.entrypoint_identity.to_dict(),
            "deployment_unit": self.deployment_unit,
            "export_identity": self.export_identity.to_dict(),
            "command_identity": self.command_identity.to_dict(),
            "runner_identity": self.runner_identity.to_dict(),
            "process_observation_identity": self.process_observation_identity.to_dict(),
            "test_custody_identity": self.test_custody_identity.to_dict(),
            "selected_case_identities": [
                item.to_dict() for item in self.selected_case_identities
            ],
            "cases": [item.to_dict() for item in self.cases],
            "selected_count": self.selected_count,
            "executed_count": self.executed_count,
            "passed_count": self.passed_count,
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardEntrypointGeneratedTestEvidence"
    ) -> StandardEntrypointGeneratedTestEvidence:
        identity_names = (
            "entrypoint_identity",
            "export_identity",
            "command_identity",
            "runner_identity",
            "process_observation_identity",
            "test_custody_identity",
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    *identity_names,
                    "deployment_unit",
                    "selected_case_identities",
                    "cases",
                    "selected_count",
                    "executed_count",
                    "passed_count",
                    "outcome",
                }
            ),
        )
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identity_names
            },
            deployment_unit=string_value(
                data["deployment_unit"], f"{path}.deployment_unit", max_length=4096
            ),
            selected_case_identities=parse_tuple(
                data["selected_case_identities"],
                f"{path}.selected_case_identities",
                ContentIdentity.from_dict,
            ),
            cases=parse_tuple(
                data["cases"],
                f"{path}.cases",
                StandardGeneratedTestCaseEvidence.from_dict,
            ),
            selected_count=int_value(data["selected_count"], f"{path}.selected_count"),
            executed_count=int_value(data["executed_count"], f"{path}.executed_count"),
            passed_count=int_value(data["passed_count"], f"{path}.passed_count"),
            outcome=_parse_outcome(data["outcome"], f"{path}.outcome"),
        )


@dataclass(frozen=True, slots=True)
class StandardGeneratedTestExecutionEvidence:
    """Complete passing execution of the cases selected from one generated suite."""

    component_revision: ContentIdentity
    generated_test_suite_identity: ContentIdentity
    build_evidence_identity: ContentIdentity
    export_identities: tuple[ContentIdentity, ...]
    runner_identity: ContentIdentity
    test_custody_identity: ContentIdentity
    selected_case_identities: tuple[ContentIdentity, ...]
    cases: tuple[StandardGeneratedTestCaseEvidence, ...]
    selected_count: int
    executed_count: int
    passed_count: int
    outcome: StandardEvidenceOutcome = StandardEvidenceOutcome.PASSED
    entrypoint_evidence: tuple[StandardEntrypointGeneratedTestEvidence, ...] | None = (
        None
    )

    SCHEMA: ClassVar[str] = STANDARD_GENERATED_TEST_EXECUTION_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "generated_test_suite_identity",
            "build_evidence_identity",
            "runner_identity",
            "test_custody_identity",
        ):
            _identity(
                getattr(self, name),
                f"StandardGeneratedTestExecutionEvidence.{name}",
            )
        _identity_tuple(
            self.export_identities,
            "StandardGeneratedTestExecutionEvidence.export_identities",
            canonical=False,
        )
        _identity_tuple(
            self.selected_case_identities,
            "StandardGeneratedTestExecutionEvidence.selected_case_identities",
            maximum=MAX_STANDARD_GENERATED_TEST_OBSERVATIONS,
        )
        if not self.cases or len(self.cases) > MAX_STANDARD_GENERATED_TEST_OBSERVATIONS:
            fail(
                "StandardGeneratedTestExecutionEvidence.cases",
                "must contain a bounded non-empty set of observations",
            )
        for index, case in enumerate(self.cases):
            if not isinstance(case, StandardGeneratedTestCaseEvidence):
                fail(
                    f"StandardGeneratedTestExecutionEvidence.cases[{index}]",
                    "must be StandardGeneratedTestCaseEvidence",
                )
        case_ids = tuple(case.case_id for case in self.cases)
        unique(case_ids, "StandardGeneratedTestExecutionEvidence.cases", "case IDs")
        observed = tuple(case.case_identity for case in self.cases)
        if observed != self.selected_case_identities:
            fail(
                "StandardGeneratedTestExecutionEvidence.cases",
                "must execute every and only selected generated test case in "
                "canonical order",
            )
        counts = (
            int_value(
                self.selected_count,
                "StandardGeneratedTestExecutionEvidence.selected_count",
                minimum=1,
                maximum=MAX_STANDARD_GENERATED_TEST_OBSERVATIONS,
            ),
            int_value(
                self.executed_count,
                "StandardGeneratedTestExecutionEvidence.executed_count",
                minimum=1,
                maximum=MAX_STANDARD_GENERATED_TEST_OBSERVATIONS,
            ),
            int_value(
                self.passed_count,
                "StandardGeneratedTestExecutionEvidence.passed_count",
                minimum=1,
                maximum=MAX_STANDARD_GENERATED_TEST_OBSERVATIONS,
            ),
        )
        if counts != (len(self.cases),) * 3:
            fail(
                "StandardGeneratedTestExecutionEvidence",
                "selected, executed, and passed counts must equal exact case "
                "membership",
            )
        _outcome(self.outcome, "StandardGeneratedTestExecutionEvidence.outcome")
        if self.entrypoint_evidence is not None:
            units = self.entrypoint_evidence
            if len(units) < 2 or any(
                not isinstance(item, StandardEntrypointGeneratedTestEvidence)
                for item in units
            ):
                fail(
                    "StandardGeneratedTestExecutionEvidence.entrypoint_evidence",
                    "must contain evidence for at least two entrypoints",
                )
            deployment_units = tuple(item.deployment_unit for item in units)
            if deployment_units != tuple(sorted(set(deployment_units))):
                fail(
                    "StandardGeneratedTestExecutionEvidence.entrypoint_evidence",
                    "must use unique canonical deployment-unit order",
                )
            if {item.export_identity for item in units} != set(self.export_identities):
                fail(
                    "StandardGeneratedTestExecutionEvidence.entrypoint_evidence",
                    "must cover every and only exact built export",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "generated_test_suite_identity": (
                self.generated_test_suite_identity.to_dict()
            ),
            "build_evidence_identity": self.build_evidence_identity.to_dict(),
            "export_identities": [item.to_dict() for item in self.export_identities],
            "runner_identity": self.runner_identity.to_dict(),
            "test_custody_identity": self.test_custody_identity.to_dict(),
            "selected_case_identities": [
                item.to_dict() for item in self.selected_case_identities
            ],
            "cases": [item.to_dict() for item in self.cases],
            "selected_count": self.selected_count,
            "executed_count": self.executed_count,
            "passed_count": self.passed_count,
            "outcome": self.outcome.value,
        }
        if self.entrypoint_evidence is not None:
            result["entrypoint_evidence"] = [
                item.to_dict() for item in self.entrypoint_evidence
            ]
        return result

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardGeneratedTestExecutionEvidence"
    ) -> StandardGeneratedTestExecutionEvidence:
        names = frozenset(
            {
                "component_revision",
                "generated_test_suite_identity",
                "build_evidence_identity",
                "export_identities",
                "runner_identity",
                "test_custody_identity",
                "selected_case_identities",
                "cases",
                "selected_count",
                "executed_count",
                "passed_count",
                "outcome",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=names,
            optional=frozenset({"entrypoint_evidence"}),
        )
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            generated_test_suite_identity=ContentIdentity.from_dict(
                data["generated_test_suite_identity"],
                path=f"{path}.generated_test_suite_identity",
            ),
            build_evidence_identity=ContentIdentity.from_dict(
                data["build_evidence_identity"],
                path=f"{path}.build_evidence_identity",
            ),
            export_identities=parse_tuple(
                data["export_identities"],
                f"{path}.export_identities",
                ContentIdentity.from_dict,
            ),
            runner_identity=ContentIdentity.from_dict(
                data["runner_identity"], path=f"{path}.runner_identity"
            ),
            test_custody_identity=ContentIdentity.from_dict(
                data["test_custody_identity"], path=f"{path}.test_custody_identity"
            ),
            selected_case_identities=parse_tuple(
                data["selected_case_identities"],
                f"{path}.selected_case_identities",
                ContentIdentity.from_dict,
            ),
            cases=parse_tuple(
                data["cases"],
                f"{path}.cases",
                StandardGeneratedTestCaseEvidence.from_dict,
            ),
            selected_count=_parse_count(
                data["selected_count"], f"{path}.selected_count"
            ),
            executed_count=_parse_count(
                data["executed_count"], f"{path}.executed_count"
            ),
            passed_count=_parse_count(data["passed_count"], f"{path}.passed_count"),
            outcome=_parse_outcome(data["outcome"], f"{path}.outcome"),
            entrypoint_evidence=(
                parse_tuple(
                    data["entrypoint_evidence"],
                    f"{path}.entrypoint_evidence",
                    StandardEntrypointGeneratedTestEvidence.from_dict,
                )
                if "entrypoint_evidence" in data
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardEntrypointExecutionEvidence:
    """Successful runtime evidence attributable to one deployment unit."""

    entrypoint_identity: ContentIdentity
    deployment_unit: str
    export_identity: ContentIdentity
    execution_contract_identity: ContentIdentity
    runtime_identity: ContentIdentity
    observation_identity: ContentIdentity
    stdout_identity: ContentIdentity
    stderr_identity: ContentIdentity
    exit_code: int
    outcome: StandardEvidenceOutcome = StandardEvidenceOutcome.PASSED

    SCHEMA: ClassVar[str] = STANDARD_ENTRYPOINT_EXECUTION_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "entrypoint_identity",
            "export_identity",
            "execution_contract_identity",
            "runtime_identity",
            "observation_identity",
            "stdout_identity",
            "stderr_identity",
        ):
            _identity(
                getattr(self, name),
                f"StandardEntrypointExecutionEvidence.{name}",
            )
        string_value(
            self.deployment_unit,
            "StandardEntrypointExecutionEvidence.deployment_unit",
            max_length=4096,
        )
        if type(self.exit_code) is not int or self.exit_code != 0:
            fail(
                "StandardEntrypointExecutionEvidence.exit_code",
                "must be exactly zero",
            )
        _outcome(self.outcome, "StandardEntrypointExecutionEvidence.outcome")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "entrypoint_identity": self.entrypoint_identity.to_dict(),
            "deployment_unit": self.deployment_unit,
            "export_identity": self.export_identity.to_dict(),
            "execution_contract_identity": self.execution_contract_identity.to_dict(),
            "runtime_identity": self.runtime_identity.to_dict(),
            "observation_identity": self.observation_identity.to_dict(),
            "stdout_identity": self.stdout_identity.to_dict(),
            "stderr_identity": self.stderr_identity.to_dict(),
            "exit_code": self.exit_code,
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardEntrypointExecutionEvidence"
    ) -> StandardEntrypointExecutionEvidence:
        identity_names = (
            "entrypoint_identity",
            "export_identity",
            "execution_contract_identity",
            "runtime_identity",
            "observation_identity",
            "stdout_identity",
            "stderr_identity",
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {*identity_names, "deployment_unit", "exit_code", "outcome"}
            ),
        )
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identity_names
            },
            deployment_unit=string_value(
                data["deployment_unit"], f"{path}.deployment_unit", max_length=4096
            ),
            exit_code=int_value(data["exit_code"], f"{path}.exit_code"),
            outcome=_parse_outcome(data["outcome"], f"{path}.outcome"),
        )


@dataclass(frozen=True, slots=True)
class StandardExecutionEvidence:
    """Successful execution of the exact root export under one observed runtime."""

    component_revision: ContentIdentity
    build_evidence_identity: ContentIdentity
    export_identities: tuple[ContentIdentity, ...]
    root_export_identity: ContentIdentity
    execution_contract_identity: ContentIdentity
    runtime_identity: ContentIdentity
    artifact_custody_identity: ContentIdentity
    observation_identity: ContentIdentity
    stdout_identity: ContentIdentity
    stderr_identity: ContentIdentity
    exit_code: int
    outcome: StandardEvidenceOutcome = StandardEvidenceOutcome.PASSED
    entrypoint_evidence: tuple[StandardEntrypointExecutionEvidence, ...] | None = None

    provider_artifact_identities: tuple[ContentIdentity, ...] = ()
    execution_authority: StandardExecutionAuthority | None = None

    SCHEMA: ClassVar[str] = STANDARD_EXECUTION_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "build_evidence_identity",
            "root_export_identity",
            "execution_contract_identity",
            "runtime_identity",
            "artifact_custody_identity",
            "observation_identity",
            "stdout_identity",
            "stderr_identity",
        ):
            _identity(getattr(self, name), f"StandardExecutionEvidence.{name}")
        _identity_tuple(
            self.export_identities,
            "StandardExecutionEvidence.export_identities",
            canonical=False,
        )
        _identity_tuple(
            self.provider_artifact_identities,
            "StandardExecutionEvidence.provider_artifact_identities",
            nonempty=False,
        )
        if self.execution_authority is not None:
            authority = self.execution_authority
            if (
                not isinstance(authority, StandardExecutionAuthority)
                or authority.input_scope.component_revision != self.component_revision
                or set(authority.input_scope.export_identities)
                != set(self.export_identities)
                or authority.input_scope.provider_artifact_identities
                != self.provider_artifact_identities
            ):
                fail(
                    "StandardExecutionEvidence.execution_authority",
                    "must bind exact execution inputs",
                )
        if self.root_export_identity not in self.export_identities:
            fail(
                "StandardExecutionEvidence.root_export_identity",
                "must name one of the exact built exports",
            )
        if type(self.exit_code) is not int or self.exit_code != 0:
            fail("StandardExecutionEvidence.exit_code", "must be exactly zero")
        _outcome(self.outcome, "StandardExecutionEvidence.outcome")
        if self.entrypoint_evidence is not None:
            units = self.entrypoint_evidence
            if len(units) < 2 or any(
                not isinstance(item, StandardEntrypointExecutionEvidence)
                for item in units
            ):
                fail(
                    "StandardExecutionEvidence.entrypoint_evidence",
                    "must contain evidence for at least two entrypoints",
                )
            deployment_units = tuple(item.deployment_unit for item in units)
            if deployment_units != tuple(sorted(set(deployment_units))):
                fail(
                    "StandardExecutionEvidence.entrypoint_evidence",
                    "must use unique canonical deployment-unit order",
                )
            if {item.export_identity for item in units} != set(self.export_identities):
                fail(
                    "StandardExecutionEvidence.entrypoint_evidence",
                    "must cover every and only exact built export",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "build_evidence_identity": self.build_evidence_identity.to_dict(),
            "export_identities": [item.to_dict() for item in self.export_identities],
            "root_export_identity": self.root_export_identity.to_dict(),
            "execution_contract_identity": self.execution_contract_identity.to_dict(),
            "runtime_identity": self.runtime_identity.to_dict(),
            "artifact_custody_identity": self.artifact_custody_identity.to_dict(),
            "observation_identity": self.observation_identity.to_dict(),
            "stdout_identity": self.stdout_identity.to_dict(),
            "stderr_identity": self.stderr_identity.to_dict(),
            "exit_code": self.exit_code,
            "outcome": self.outcome.value,
        }
        if self.execution_authority is not None:
            result["execution_authority"] = self.execution_authority.to_dict()
        if self.provider_artifact_identities:
            result["provider_artifact_identities"] = [
                item.to_dict() for item in self.provider_artifact_identities
            ]
        if self.entrypoint_evidence is not None:
            result["entrypoint_evidence"] = [
                item.to_dict() for item in self.entrypoint_evidence
            ]
        return result

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardExecutionEvidence"
    ) -> StandardExecutionEvidence:
        names = frozenset(
            {
                "component_revision",
                "build_evidence_identity",
                "export_identities",
                "root_export_identity",
                "execution_contract_identity",
                "runtime_identity",
                "artifact_custody_identity",
                "observation_identity",
                "stdout_identity",
                "stderr_identity",
                "exit_code",
                "outcome",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=names,
            optional=frozenset(
                {
                    "entrypoint_evidence",
                    "provider_artifact_identities",
                    "execution_authority",
                }
            ),
        )
        return cls(
            execution_authority=(
                StandardExecutionAuthority.from_dict(
                    data["execution_authority"], path=f"{path}.execution_authority"
                )
                if "execution_authority" in data
                else None
            ),
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            build_evidence_identity=ContentIdentity.from_dict(
                data["build_evidence_identity"],
                path=f"{path}.build_evidence_identity",
            ),
            export_identities=parse_tuple(
                data["export_identities"],
                f"{path}.export_identities",
                ContentIdentity.from_dict,
            ),
            root_export_identity=ContentIdentity.from_dict(
                data["root_export_identity"], path=f"{path}.root_export_identity"
            ),
            execution_contract_identity=ContentIdentity.from_dict(
                data["execution_contract_identity"],
                path=f"{path}.execution_contract_identity",
            ),
            runtime_identity=ContentIdentity.from_dict(
                data["runtime_identity"], path=f"{path}.runtime_identity"
            ),
            artifact_custody_identity=ContentIdentity.from_dict(
                data["artifact_custody_identity"],
                path=f"{path}.artifact_custody_identity",
            ),
            observation_identity=ContentIdentity.from_dict(
                data["observation_identity"], path=f"{path}.observation_identity"
            ),
            stdout_identity=ContentIdentity.from_dict(
                data["stdout_identity"], path=f"{path}.stdout_identity"
            ),
            stderr_identity=ContentIdentity.from_dict(
                data["stderr_identity"], path=f"{path}.stderr_identity"
            ),
            exit_code=_parse_exit_code(data["exit_code"], f"{path}.exit_code"),
            outcome=_parse_outcome(data["outcome"], f"{path}.outcome"),
            provider_artifact_identities=parse_tuple(
                data.get("provider_artifact_identities", []),
                f"{path}.provider_artifact_identities",
                ContentIdentity.from_dict,
            ),
            entrypoint_evidence=(
                parse_tuple(
                    data["entrypoint_evidence"],
                    f"{path}.entrypoint_evidence",
                    StandardEntrypointExecutionEvidence.from_dict,
                )
                if "entrypoint_evidence" in data
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardComponentAcceptanceEvidence:
    """Exact generated, built, tested, and executed evidence accepted by policy."""

    component_revision: ContentIdentity
    source_generation_identity: ContentIdentity
    generated_test_suite_identity: ContentIdentity
    build: StandardBuildEvidence
    generated_tests: StandardGeneratedTestExecutionEvidence
    execution: StandardExecutionEvidence
    acceptance_policy_identity: ContentIdentity
    outcome: StandardEvidenceOutcome = StandardEvidenceOutcome.PASSED

    SCHEMA: ClassVar[str] = STANDARD_COMPONENT_ACCEPTANCE_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        for name in (
            "component_revision",
            "source_generation_identity",
            "generated_test_suite_identity",
            "acceptance_policy_identity",
        ):
            _identity(
                getattr(self, name), f"StandardComponentAcceptanceEvidence.{name}"
            )
        if not isinstance(self.build, StandardBuildEvidence):
            fail("StandardComponentAcceptanceEvidence.build", "must be build evidence")
        if not isinstance(self.generated_tests, StandardGeneratedTestExecutionEvidence):
            fail(
                "StandardComponentAcceptanceEvidence.generated_tests",
                "must be generated-test execution evidence",
            )
        if not isinstance(self.execution, StandardExecutionEvidence):
            fail(
                "StandardComponentAcceptanceEvidence.execution",
                "must be Component execution evidence",
            )
        if not (
            self.build.component_revision
            == self.generated_tests.component_revision
            == self.execution.component_revision
            == self.component_revision
        ):
            fail(
                "StandardComponentAcceptanceEvidence.component_revision",
                "must match every composed stage",
            )
        if (
            self.generated_tests.generated_test_suite_identity
            != self.generated_test_suite_identity
        ):
            fail(
                "StandardComponentAcceptanceEvidence.generated_test_suite_identity",
                "must match the exact suite executed",
            )
        if (
            self.generated_tests.build_evidence_identity != self.build.identity
            or self.execution.build_evidence_identity != self.build.identity
        ):
            fail(
                "StandardComponentAcceptanceEvidence.build",
                "tests and execution must bind the exact composed build evidence",
            )
        if (
            self.generated_tests.export_identities != self.build.export_identities
            or self.execution.export_identities != self.build.export_identities
            or self.execution.artifact_custody_identity
            != self.build.artifact_custody_identity
        ):
            fail(
                "StandardComponentAcceptanceEvidence",
                "tests and execution must bind the exact exports and artifact custody",
            )
        if len(self.build.exports) > 1 and (
            self.generated_tests.entrypoint_evidence is None
            or self.execution.entrypoint_evidence is None
        ):
            fail(
                "StandardComponentAcceptanceEvidence",
                "multiple exports require complete per-entrypoint test and "
                "execution evidence",
            )
        _outcome(self.outcome, "StandardComponentAcceptanceEvidence.outcome")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component_revision": self.component_revision.to_dict(),
            "source_generation_identity": self.source_generation_identity.to_dict(),
            "generated_test_suite_identity": (
                self.generated_test_suite_identity.to_dict()
            ),
            "build": self.build.to_dict(),
            "generated_tests": self.generated_tests.to_dict(),
            "execution": self.execution.to_dict(),
            "acceptance_policy_identity": self.acceptance_policy_identity.to_dict(),
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardComponentAcceptanceEvidence"
    ) -> StandardComponentAcceptanceEvidence:
        names = frozenset(
            {
                "component_revision",
                "source_generation_identity",
                "generated_test_suite_identity",
                "build",
                "generated_tests",
                "execution",
                "acceptance_policy_identity",
                "outcome",
            }
        )
        data = contract_fields(value, path=path, schema_uri=cls.SCHEMA, required=names)
        return cls(
            component_revision=ContentIdentity.from_dict(
                data["component_revision"], path=f"{path}.component_revision"
            ),
            source_generation_identity=ContentIdentity.from_dict(
                data["source_generation_identity"],
                path=f"{path}.source_generation_identity",
            ),
            generated_test_suite_identity=ContentIdentity.from_dict(
                data["generated_test_suite_identity"],
                path=f"{path}.generated_test_suite_identity",
            ),
            build=StandardBuildEvidence.from_dict(data["build"], path=f"{path}.build"),
            generated_tests=StandardGeneratedTestExecutionEvidence.from_dict(
                data["generated_tests"], path=f"{path}.generated_tests"
            ),
            execution=StandardExecutionEvidence.from_dict(
                data["execution"], path=f"{path}.execution"
            ),
            acceptance_policy_identity=ContentIdentity.from_dict(
                data["acceptance_policy_identity"],
                path=f"{path}.acceptance_policy_identity",
            ),
            outcome=_parse_outcome(data["outcome"], f"{path}.outcome"),
        )


def _parse_outcome(value: object, path: str) -> StandardEvidenceOutcome:
    try:
        outcome = StandardEvidenceOutcome(string_value(value, path))
    except ValueError:
        fail(path, "must be 'passed'")
    return _outcome(outcome, path)


def _parse_count(value: object, path: str) -> int:
    return int_value(
        value,
        path,
        minimum=1,
        maximum=MAX_STANDARD_GENERATED_TEST_OBSERVATIONS,
    )


def _parse_exit_code(value: object, path: str) -> int:
    if type(value) is not int or value != 0:
        fail(path, "must be exactly zero")
    return value


__all__ = [
    "MAX_STANDARD_EXPORTS",
    "MAX_STANDARD_GENERATED_TEST_CASES",
    "MAX_STANDARD_GENERATED_TEST_OBSERVATIONS",
    "STANDARD_BUILD_EVIDENCE_SCHEMA",
    "STANDARD_COMPONENT_ACCEPTANCE_EVIDENCE_SCHEMA",
    "STANDARD_EXECUTION_EVIDENCE_SCHEMA",
    "STANDARD_ENTRYPOINT_EXECUTION_EVIDENCE_SCHEMA",
    "STANDARD_ENTRYPOINT_GENERATED_TEST_EVIDENCE_SCHEMA",
    "STANDARD_GENERATED_TEST_CASE_EVIDENCE_SCHEMA",
    "STANDARD_GENERATED_TEST_EXECUTION_EVIDENCE_SCHEMA",
    "StandardBuildEvidence",
    "StandardComponentAcceptanceEvidence",
    "StandardEvidenceOutcome",
    "StandardExecutionEvidence",
    "StandardEntrypointExecutionEvidence",
    "StandardEntrypointGeneratedTestEvidence",
    "StandardGeneratedTestCaseEvidence",
    "StandardGeneratedTestExecutionEvidence",
]
