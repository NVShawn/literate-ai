"""Check a derived project's declared authority without changing or building anything.

``verify``, ``rebuild``, and ``update`` are three verbs at three levels of
intrusiveness, and keeping them apart is the point:

- ``verify`` reads. It answers "is this project's declared state sound?" and writes
  nothing, builds nothing, and reaches no model. Safe on any tree at any time.
- ``rebuild`` builds. It is how a derived project runs its *own* gates through its own
  build harness, toolchain, and language choices. Verify deliberately does not invoke
  it, wrap it, or approximate it; a project's build is the project's business.
- ``update`` changes framework-owned files from upstream. It is the only one of the
  three that can alter authority the project did not write.

This repository has ``make validate``; a project initialized from it has nothing. Its
onboarding skill hands the operator a manual sequence and expects them to know the order
and which failures matter -- a checklist living in prose, which every project satisfies
differently or not at all. ``verify`` is that checklist as one command.

It composes the existing commands rather than reimplementing their checks, so a gate
cannot drift from the command that owns it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.adapters.project_lock_health import observe_project_locks
from literate_ai.adapters.project_validation import (
    ProjectValidationError,
    validate_project,
)
from literate_ai.adapters.project_validation import (
    validated_project_authority_identity as _authority_identity,
)
from literate_ai.contracts import SourceIntelligenceMode, SourceIntelligenceStage
from literate_ai.contracts.html_observability import (
    HtmlRenderRefusal,
    HtmlStalenessReport,
)
from literate_ai.project_source_index import (
    CodeGraphProjectSourceIntelligence,
    ProjectSourceIntelligenceError,
)
from literate_ai.projects import PROJECT_FILENAME, ProjectError, discover_project
from literate_ai.test_receipts import require_current_project_test_receipt


class ProjectVerificationError(ValueError):
    """Usage or project discovery failed before verification could report gates."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


VERIFY_SCHEMA = "literate-ai/project-verify@1"

GATES = ("authority", "locks", "source-intelligence", "html-observability", "receipt")

PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: str
    state: str
    detail: str
    artifacts: tuple[HtmlStalenessReport, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "gate": self.gate,
            "state": self.state,
            "detail": self.detail,
        }
        if self.artifacts is not None:
            value["artifacts"] = [artifact.to_dict() for artifact in self.artifacts]
        return value


def _authority(root: Path) -> GateResult:
    try:
        validate_project(
            root, require_authority_review=True, synchronize_source_intelligence=False
        )
    except ProjectValidationError as exc:
        return GateResult("authority", FAIL, f"{exc.code}: {exc.message}")
    return GateResult("authority", PASS, "project authority and documentation review")


def _locks(root: Path) -> GateResult:
    observed = observe_project_locks(root).gate
    return GateResult(observed.gate, observed.state, observed.detail)


def _source_intelligence(root: Path) -> GateResult:
    try:
        project = discover_project(root)
    except ProjectError as exc:
        return GateResult("source-intelligence", FAIL, f"{exc.code}: {exc.message}")
    if project is None:
        return GateResult("source-intelligence", FAIL, "project not found")
    policy = project.definition.source_intelligence
    if (
        policy.provider_id == "none"
        or policy.mode_for(SourceIntelligenceStage.PROJECT_MAINTENANCE)
        is SourceIntelligenceMode.OFF
    ):
        detail = (
            "project selects provider 'none'"
            if policy.provider_id == "none"
            else "project maintenance stage is off"
        )
        return GateResult("source-intelligence", SKIPPED, detail)
    if policy.provider_id != "codegraph-cli":
        return GateResult(
            "source-intelligence",
            FAIL,
            "configured source-intelligence provider is unsupported",
        )
    try:
        CodeGraphProjectSourceIntelligence(policy).check(project.root)
    except ProjectSourceIntelligenceError as exc:
        return GateResult("source-intelligence", FAIL, f"{exc.code}: {exc.message}")
    return GateResult("source-intelligence", PASS, "project index current")


def _html_observability(root: Path) -> GateResult:
    from literate_ai.adapters.html_staleness import verify_html_artifact

    gate = "html-observability"
    try:
        project = discover_project(root)
    except ProjectError as exc:
        return GateResult(gate, FAIL, f"{exc.code}: {exc.message}", ())
    if project is None:
        return GateResult(gate, FAIL, "project not found", ())
    if not project.definition.html_render_requests:
        return GateResult(gate, SKIPPED, "no declared HTML artifacts to verify", ())
    reports = []
    findings = []
    for request in project.definition.html_render_requests:
        observed = verify_html_artifact(project.root, request)
        if isinstance(observed, HtmlRenderRefusal):
            findings.append(
                f"{request.output_path}: {observed.code}: {observed.message}"
            )
        else:
            reports.append(observed)
            if observed.status != "current":
                changed = ", ".join(observed.stale_source_labels)
                findings.append(
                    f"{request.output_path}: {observed.status}"
                    + (f" ({changed})" if changed else "")
                )
    try:
        current = discover_project(project.root)
    except ProjectError as exc:
        return GateResult(gate, FAIL, f"{exc.code}: {exc.message}", ())
    if current is None or current.definition != project.definition:
        return GateResult(
            gate, FAIL, "project declarations changed during verification", ()
        )
    return GateResult(
        gate,
        FAIL if findings else PASS,
        "; ".join(findings)
        if findings
        else f"{len(reports)} declared HTML artifact(s) current",
        tuple(reports),
    )


def _receipt(root: Path) -> GateResult:
    try:
        project = discover_project(root)
        if project is None:
            return GateResult("receipt", FAIL, "project not found")
        if project.definition.test_receipt_policy is None:
            return GateResult("receipt", SKIPPED, "project declares no receipt policy")
        require_current_project_test_receipt(
            project, project_revision_identity=_authority_identity(root)
        )
    except (ProjectError, ProjectValidationError) as exc:
        return GateResult("receipt", FAIL, f"{exc.code}: {exc.message}")
    return GateResult("receipt", PASS, "committed test receipt is current")


_GATES = {
    "authority": _authority,
    "locks": _locks,
    "source-intelligence": _source_intelligence,
    "html-observability": _html_observability,
    "receipt": _receipt,
}


def observe_project_verification(
    root: Path, *, gates: tuple[str, ...] | list[str] | None = None
) -> tuple[dict[str, Any], int]:
    """Run the selected gates in declared order and report each one separately."""

    selected = gates or list(GATES)
    unknown = [gate for gate in selected if gate not in _GATES]
    if unknown:
        raise ProjectVerificationError(
            "cli.usage",
            f"unknown gate {unknown[0]!r}; choose from {', '.join(GATES)}",
        )
    root = Path(root)
    try:
        if discover_project(root) is None:
            raise ProjectVerificationError(
                "project.not_found", f"no {PROJECT_FILENAME} found from {root}"
            )
    except ProjectError as exc:
        raise ProjectVerificationError(exc.code, exc.message) from exc

    ordered = [gate for gate in GATES if gate in selected]
    from literate_ai.diagnostics import debug_stage

    results = []
    for gate in ordered:
        with debug_stage("verify", gate=gate):
            results.append(_GATES[gate](root))
    failed = [item for item in results if item.state == FAIL]
    return {
        "schema": VERIFY_SCHEMA,
        "project": str(root),
        "gates": [item.to_dict() for item in results],
        "counts": {
            state: sum(1 for item in results if item.state == state)
            for state in (PASS, FAIL, SKIPPED)
        },
        "ok": not failed,
    }, (1 if failed else 0)


__all__ = [
    "GATES",
    "VERIFY_SCHEMA",
    "GateResult",
    "observe_project_verification",
    "ProjectVerificationError",
]
