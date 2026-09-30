"""Pin, pipeline, marker, and dispatch for dependency-aware test skip.

Consumes identities the authority graph already computes — Component
``input_closure_identity``, Flavor ``revision_identity``, skill ``identity``,
lifecycle-driver TCB identity, and documentation-authority inventory identity —
instead of a parallel tracker. The durable ``tested`` tag is the existing
project test receipt, keyed by pin hash (``subject_identity``), and is readable
from CycloneDX provenance properties. CACHE-007 admission events are the only
creation trigger that enqueues an artifact pin.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from literate_ai.application.project_authority import (
    ComponentAuthorityReviewEntry,
    FlavorAuthorityReviewEntry,
    ForwardSkillAuthorityReviewEntry,
    InverseSkillAuthorityReviewEntry,
    ProjectAuthorityInventory,
    review_project_authority,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.testing import ProjectTestReceipt

PIN_DISPATCH_SCHEMA = "literate-ai/pin-dispatch@1"
PIN_CLOSURE_SCHEMA = "literate-ai/pin-closure@1"
PIN_PLAN_SCHEMA = "literate-ai/pin-plan@1"
TESTED_AT_PIN_PROPERTY = "literate-ai:tested-at-pin"
COMPONENT_PIPELINE = ("generated-tests", "acceptance")
FRAMEWORK_TCB_GATES = frozenset(
    {
        "repository-layout-check",
        "python-check",
        "lint",
        "format-check",
        "driver-review",
        "skills-check",
        "installed-e2e",
        "wheel-check",
        "install-check",
        "installed-project-e2e",
        "samples",
        "test-receipt-current",
    }
)
FRAMEWORK_DOCS_GATES = frozenset({"openspec-check", "documentation-check"})


class PinKind(StrEnum):
    COMPONENT = "component"
    FLAVOR = "flavor"
    SKILL = "skill"
    FRAMEWORK_TCB = "framework-tcb"
    DOCUMENTATION_AUTHORITY = "documentation-authority"


class DispatchAction(StrEnum):
    SKIP = "skip"
    RUN = "run"
    RESUME = "resume"


class PinDispatchError(ValueError):
    """Fail-closed pin dispatch failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _identity(value: ContentIdentity, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        raise TypeError(f"{path} must be a ContentIdentity")
    return value


def _parse_identity(value: str, path: str) -> ContentIdentity:
    if not isinstance(value, str) or not value:
        raise PinDispatchError(
            "pin_dispatch.identity_invalid", f"{path} must be a content-identity URI"
        )
    try:
        return ContentIdentity.parse_uri(value)
    except (TypeError, ValueError) as exc:
        raise PinDispatchError(
            "pin_dispatch.identity_invalid", f"{path} must be a content-identity URI"
        ) from exc


@dataclass(frozen=True, slots=True)
class Pin:
    """One content-addressed node whose hash already folds its dependency closure."""

    identity: ContentIdentity
    kind: PinKind
    coordinate: str
    pipeline: tuple[str, ...]
    declared_inputs: tuple[str, ...]
    own_identity: ContentIdentity | None = None

    def __post_init__(self) -> None:
        _identity(self.identity, "Pin.identity")
        if not isinstance(self.kind, PinKind):
            raise TypeError("Pin.kind must be a PinKind")
        if not self.coordinate or not isinstance(self.coordinate, str):
            raise PinDispatchError(
                "pin_dispatch.coordinate_invalid", "pin coordinate must be non-empty"
            )
        if not isinstance(self.pipeline, tuple) or any(
            not isinstance(step, str) or not step for step in self.pipeline
        ):
            raise PinDispatchError(
                "pin_dispatch.pipeline_invalid",
                "pipeline steps must be a tuple of non-empty names",
            )
        if len(set(self.pipeline)) != len(self.pipeline):
            raise PinDispatchError(
                "pin_dispatch.pipeline_invalid", "pipeline steps must be unique"
            )
        if not isinstance(self.declared_inputs, tuple) or any(
            not isinstance(item, str) or not item for item in self.declared_inputs
        ):
            raise PinDispatchError(
                "pin_dispatch.declared_inputs_invalid",
                "declared inputs must be a tuple of non-empty labels",
            )
        if self.own_identity is not None:
            _identity(self.own_identity, "Pin.own_identity")

    @property
    def owns_pipeline(self) -> bool:
        return bool(self.pipeline)


@dataclass(frozen=True, slots=True)
class PinMarker:
    """Progress through one pin's pipeline, keyed only by that pin's hash."""

    pin_identity: ContentIdentity
    completed_steps: tuple[str, ...]

    def __post_init__(self) -> None:
        _identity(self.pin_identity, "PinMarker.pin_identity")
        if not isinstance(self.completed_steps, tuple) or any(
            not isinstance(step, str) or not step for step in self.completed_steps
        ):
            raise PinDispatchError(
                "pin_dispatch.marker_invalid",
                "marker steps must be a tuple of non-empty names",
            )
        if len(set(self.completed_steps)) != len(self.completed_steps):
            raise PinDispatchError(
                "pin_dispatch.marker_invalid", "marker steps must be unique"
            )


@dataclass(frozen=True, slots=True)
class AdmissionEvent:
    """CACHE-007 source-admission evidence that a pin was re-derived this run."""

    pin_identity: ContentIdentity
    admission_identity: ContentIdentity

    def __post_init__(self) -> None:
        _identity(self.pin_identity, "AdmissionEvent.pin_identity")
        _identity(self.admission_identity, "AdmissionEvent.admission_identity")


@dataclass(frozen=True, slots=True)
class ScheduledPin:
    pin: Pin
    action: DispatchAction
    completed_steps: tuple[str, ...] = ()
    receipt: ProjectTestReceipt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pin, Pin):
            raise TypeError("ScheduledPin.pin must be a Pin")
        if not isinstance(self.action, DispatchAction):
            raise TypeError("ScheduledPin.action must be a DispatchAction")
        if self.action is DispatchAction.SKIP and self.pin.owns_pipeline:
            if self.receipt is None:
                raise PinDispatchError(
                    "pin_dispatch.skip_without_receipt",
                    "a tested skip requires the project receipt keyed by pin hash",
                )


@dataclass(frozen=True, slots=True)
class TestedTag:
    """The existing committed receipt, re-keyed by pin hash — not a second ledger.

    Binds the receipt to both the content-pin identity AND the canonical
    identity of the ordered pipeline (plan) that was current when the receipt
    was recorded. A receipt attests only that exact (pin, plan) pair; it does
    not attest a plan that has since had a gate inserted, deleted, or
    reordered, even though the pin's content identity is unchanged.
    """

    pin_identity: ContentIdentity
    plan_identity: ContentIdentity
    receipt: ProjectTestReceipt

    def __post_init__(self) -> None:
        _identity(self.pin_identity, "TestedTag.pin_identity")
        _identity(self.plan_identity, "TestedTag.plan_identity")
        if not isinstance(self.receipt, ProjectTestReceipt):
            raise TypeError("TestedTag.receipt must be a ProjectTestReceipt")
        if self.receipt.subject_identity != self.pin_identity:
            raise PinDispatchError(
                "pin_dispatch.receipt_not_keyed_by_pin",
                "tested tag must be the project receipt keyed by this pin hash",
            )


class MarkerStore(Protocol):
    def load(self, pin_identity: ContentIdentity) -> PinMarker | None: ...

    def save(self, marker: PinMarker) -> None: ...


class TestedTagStore(Protocol):
    def lookup(
        self, pin_identity: ContentIdentity, plan_identity: ContentIdentity
    ) -> ProjectTestReceipt | None: ...

    def record(
        self,
        pin_identity: ContentIdentity,
        plan_identity: ContentIdentity,
        receipt: ProjectTestReceipt,
    ) -> None: ...


class PinInputOpener(Protocol):
    def open(self, pin: Pin, input_label: str) -> bytes: ...


class MemoryMarkerStore:
    def __init__(self, markers: Sequence[PinMarker] = ()) -> None:
        self._markers: dict[str, PinMarker] = {
            item.pin_identity.uri: item for item in markers
        }

    def load(self, pin_identity: ContentIdentity) -> PinMarker | None:
        return self._markers.get(pin_identity.uri)

    def save(self, marker: PinMarker) -> None:
        self._markers[marker.pin_identity.uri] = marker


class MemoryTestedTagStore:
    """In-memory tested-tag store, plan-identity bound.

    ``receipts`` is the legacy construction path used to seed a store from
    already-committed ``ProjectTestReceipt`` values (for example the durable
    Git receipt loaded from disk) that carry no recorded plan identity of
    their own. Those entries are explicitly unbound and therefore always fail
    closed on lookup — an old receipt is never silently interpreted as
    attesting whatever plan happens to be current now. Only entries written
    through :meth:`record` (which requires a plan identity) can be returned by
    :meth:`lookup`, and only when the caller's plan identity matches exactly.
    """

    def __init__(self, receipts: Sequence[ProjectTestReceipt] = ()) -> None:
        self._entries: dict[str, tuple[str | None, ProjectTestReceipt]] = {
            item.subject_identity.uri: (None, item) for item in receipts
        }

    def lookup(
        self, pin_identity: ContentIdentity, plan_identity: ContentIdentity
    ) -> ProjectTestReceipt | None:
        entry = self._entries.get(pin_identity.uri)
        if entry is None:
            return None
        bound_plan_uri, receipt = entry
        if bound_plan_uri is None or bound_plan_uri != plan_identity.uri:
            return None
        return receipt

    def record(
        self,
        pin_identity: ContentIdentity,
        plan_identity: ContentIdentity,
        receipt: ProjectTestReceipt,
    ) -> None:
        tag = TestedTag(pin_identity, plan_identity, receipt)
        self._entries[tag.pin_identity.uri] = (tag.plan_identity.uri, tag.receipt)


def pin_closure_identity(
    *,
    kind: PinKind,
    coordinate: str,
    own_identity: ContentIdentity,
    dependencies: Sequence[ContentIdentity] = (),
) -> ContentIdentity:
    """Fold a node's own identity with already-computed dependency identities."""

    _identity(own_identity, "own_identity")
    ordered = tuple(sorted(dependencies, key=lambda item: item.uri))
    if len(set(item.uri for item in ordered)) != len(ordered):
        raise PinDispatchError(
            "pin_dispatch.dependency_duplicate",
            "closure dependencies must be unique",
        )
    return canonical_identity(
        {
            "schema": PIN_CLOSURE_SCHEMA,
            "kind": kind.value,
            "coordinate": coordinate,
            "own": own_identity.uri,
            "dependencies": [item.uri for item in ordered],
        }
    )


def pin_from_component_entry(
    entry: ComponentAuthorityReviewEntry,
    *,
    pipeline: tuple[str, ...] = COMPONENT_PIPELINE,
    declared_inputs: tuple[str, ...] | None = None,
) -> Pin:
    """Use GRAPH-001's already-folded ``input_closure_identity`` as the pin hash."""

    if not isinstance(entry, ComponentAuthorityReviewEntry):
        raise TypeError("entry must be a ComponentAuthorityReviewEntry")
    return Pin(
        identity=_parse_identity(entry.input_closure, "input_closure_identity"),
        kind=PinKind.COMPONENT,
        coordinate=entry.coordinate,
        pipeline=pipeline,
        declared_inputs=declared_inputs or (entry.coordinate,),
        own_identity=_parse_identity(entry.revision, "revision"),
    )


def pin_from_flavor_entry(entry: FlavorAuthorityReviewEntry) -> Pin:
    """Flavor pins have no pipeline; they are validated only transitively."""

    if not isinstance(entry, FlavorAuthorityReviewEntry):
        raise TypeError("entry must be a FlavorAuthorityReviewEntry")
    return Pin(
        identity=_parse_identity(entry.revision, "revision_identity"),
        kind=PinKind.FLAVOR,
        coordinate=entry.coordinate,
        pipeline=(),
        declared_inputs=(entry.coordinate,),
    )


def pin_from_skill_entry(
    entry: ForwardSkillAuthorityReviewEntry | InverseSkillAuthorityReviewEntry,
) -> Pin:
    """Skill pins have no correctness pipeline; they are validated transitively."""

    if isinstance(entry, ForwardSkillAuthorityReviewEntry):
        identity = _parse_identity(entry.identity, "skill identity")
        coordinate = entry.skill_id
    elif isinstance(entry, InverseSkillAuthorityReviewEntry):
        identity = _parse_identity(entry.content_digest, "skill content_digest")
        coordinate = entry.skill_id
    else:
        raise TypeError("entry must be a skill authority review entry")
    return Pin(
        identity=identity,
        kind=PinKind.SKILL,
        coordinate=coordinate,
        pipeline=(),
        declared_inputs=(coordinate,),
    )


def pin_from_framework_tcb(
    identity: ContentIdentity,
    *,
    pipeline: tuple[str, ...] = (),
    declared_inputs: tuple[str, ...] = ("lifecycle-driver-tcb",),
) -> Pin:
    _identity(identity, "framework TCB identity")
    return Pin(
        identity=identity,
        kind=PinKind.FRAMEWORK_TCB,
        coordinate="framework-tcb",
        pipeline=pipeline,
        declared_inputs=declared_inputs,
        own_identity=identity,
    )


def pin_from_documentation_authority(
    identity: ContentIdentity,
    *,
    pipeline: tuple[str, ...] = (),
    declared_inputs: tuple[str, ...] = ("documentation-authority",),
) -> Pin:
    _identity(identity, "documentation-authority identity")
    return Pin(
        identity=identity,
        kind=PinKind.DOCUMENTATION_AUTHORITY,
        coordinate="documentation-authority",
        pipeline=pipeline,
        declared_inputs=declared_inputs,
        own_identity=identity,
    )


def pins_from_authority_inventory(
    inventory: ProjectAuthorityInventory,
    *,
    tcb_identity: ContentIdentity | None = None,
) -> tuple[Pin, ...]:
    """Project GRAPH-001 inventory entries into pins without re-walking files."""

    if not isinstance(inventory, ProjectAuthorityInventory):
        raise TypeError("inventory must be a ProjectAuthorityInventory")
    pins: list[Pin] = [pin_from_component_entry(item) for item in inventory.components]
    pins.extend(pin_from_flavor_entry(item) for item in inventory.flavors)
    pins.extend(
        pin_from_skill_entry(item) for item in inventory.specification_to_source_skills
    )
    pins.extend(
        pin_from_skill_entry(item) for item in inventory.source_to_specification_skills
    )
    docs = review_project_authority(inventory).authority_identity
    pins.append(pin_from_documentation_authority(docs))
    if tcb_identity is not None:
        pins.append(pin_from_framework_tcb(tcb_identity))
    return tuple(pins)


def bind_gates_to_pins(gates: Sequence[str], *, tcb: Pin, docs: Pin) -> dict[str, Pin]:
    """Assign release-check gates to the TCB or documentation-authority pin."""

    tcb_steps = tuple(gate for gate in gates if gate not in FRAMEWORK_DOCS_GATES)
    docs_steps = tuple(gate for gate in gates if gate in FRAMEWORK_DOCS_GATES)
    bound_tcb = replace(tcb, pipeline=tcb_steps)
    bound_docs = replace(docs, pipeline=docs_steps)
    return {
        gate: bound_docs if gate in FRAMEWORK_DOCS_GATES else bound_tcb
        for gate in gates
    }


def suite_fallback_pin(suite: str, steps: Sequence[str]) -> Pin:
    """Isolated-test pin when no project authority graph is available."""

    identity = canonical_identity(
        {"schema": "literate-ai/pin-dispatch-suite@1", "suite": suite}
    )
    return Pin(
        identity=identity,
        kind=PinKind.FRAMEWORK_TCB,
        coordinate=suite,
        pipeline=tuple(steps),
        declared_inputs=(suite,),
        own_identity=identity,
    )


def rekey_receipt_to_pin(receipt: ProjectTestReceipt, pin: Pin) -> ProjectTestReceipt:
    """Re-key the existing receipt by pin hash; do not invent a second ledger."""

    if not isinstance(receipt, ProjectTestReceipt):
        raise TypeError("receipt must be a ProjectTestReceipt")
    if not isinstance(pin, Pin):
        raise TypeError("pin must be a Pin")
    return replace(receipt, subject_identity=pin.identity)


def pin_plan_identity(pin: Pin) -> ContentIdentity:
    """Canonical identity of a pin's ordered gate/test plan.

    Two pipelines are the same plan only if they name the same steps in the
    same order — inserting, deleting, or reordering a step yields a distinct
    plan identity even when the pin's own content identity is unchanged.
    """

    if not isinstance(pin, Pin):
        raise TypeError("pin must be a Pin")
    return canonical_identity(
        {"schema": PIN_PLAN_SCHEMA, "pipeline": list(pin.pipeline)}
    )


def tested_tag_for_pin(pin: Pin, receipt: ProjectTestReceipt) -> TestedTag:
    return TestedTag(pin.identity, pin_plan_identity(pin), receipt)


def sbom_properties_for_tested_pin(pin: Pin) -> tuple[dict[str, str], ...]:
    """Provenance field packaged Components already carry in CycloneDX properties."""

    if not isinstance(pin, Pin):
        raise TypeError("pin must be a Pin")
    return ({"name": TESTED_AT_PIN_PROPERTY, "value": pin.identity.uri},)


def tested_pin_from_sbom_properties(
    properties: Sequence[Mapping[str, object]],
) -> ContentIdentity | None:
    matches = [
        item["value"]
        for item in properties
        if isinstance(item, Mapping) and item.get("name") == TESTED_AT_PIN_PROPERTY
    ]
    if not matches:
        return None
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise PinDispatchError(
            "pin_dispatch.provenance_ambiguous",
            "SBOM provenance must name exactly one tested-at-pin identity",
        )
    return _parse_identity(matches[0], "tested-at-pin")


@dataclass(slots=True)
class PinDispatchService:
    """Shared skip/resume/run service consulted by every checkpointed workflow."""

    markers: MarkerStore = field(default_factory=MemoryMarkerStore)
    receipts: TestedTagStore = field(default_factory=MemoryTestedTagStore)

    def evaluate(
        self,
        pins: Sequence[Pin],
        *,
        open_inputs: PinInputOpener | None = None,
    ) -> tuple[ScheduledPin, ...]:
        """Walk already-hashed pins. Skip does not open declared inputs."""

        return tuple(self._decide(pin, open_inputs=open_inputs) for pin in pins)

    def schedule(
        self,
        pins: Sequence[Pin],
        admissions: Sequence[AdmissionEvent],
        *,
        open_inputs: PinInputOpener | None = None,
    ) -> tuple[ScheduledPin, ...]:
        """Enqueue only pins named by this run's CACHE-007 admission events."""

        catalog = {pin.identity.uri: pin for pin in pins}
        if len(catalog) != len(pins):
            raise PinDispatchError(
                "pin_dispatch.pin_duplicate", "pins must have unique identities"
            )
        seen: set[str] = set()
        scheduled: list[ScheduledPin] = []
        for event in admissions:
            if not isinstance(event, AdmissionEvent):
                raise TypeError("admissions must contain AdmissionEvent values")
            uri = event.pin_identity.uri
            if uri in seen:
                continue
            seen.add(uri)
            pin = catalog.get(uri)
            if pin is None:
                raise PinDispatchError(
                    "pin_dispatch.admission_unknown",
                    "admission names a pin that is not in the current catalog",
                )
            scheduled.append(self._decide(pin, open_inputs=open_inputs))
        return tuple(scheduled)

    def record_step(self, pin: Pin, step: str) -> PinMarker:
        if not isinstance(pin, Pin):
            raise TypeError("pin must be a Pin")
        if pin.pipeline and step not in pin.pipeline:
            raise PinDispatchError(
                "pin_dispatch.step_not_in_pipeline",
                f"step {step!r} is not in pin {pin.coordinate!r} pipeline",
            )
        current = self.markers.load(pin.identity)
        completed = list(current.completed_steps) if current is not None else []
        if step not in completed:
            completed.append(step)
        marker = PinMarker(pin.identity, tuple(completed))
        self.markers.save(marker)
        return marker

    def record_tested(self, pin: Pin, receipt: ProjectTestReceipt) -> TestedTag:
        tag = tested_tag_for_pin(pin, receipt)
        self.receipts.record(tag.pin_identity, tag.plan_identity, tag.receipt)
        return tag

    def _decide(self, pin: Pin, *, open_inputs: PinInputOpener | None) -> ScheduledPin:
        if not pin.owns_pipeline:
            return ScheduledPin(pin, DispatchAction.SKIP, ())
        receipt = self.receipts.lookup(pin.identity, pin_plan_identity(pin))
        if receipt is not None:
            if receipt.subject_identity != pin.identity:
                raise PinDispatchError(
                    "pin_dispatch.receipt_not_keyed_by_pin",
                    "tested tag must be the project receipt keyed by this pin hash",
                )
            return ScheduledPin(pin, DispatchAction.SKIP, pin.pipeline, receipt)
        marker = self.markers.load(pin.identity)
        completed = () if marker is None else marker.completed_steps
        remaining = tuple(step for step in pin.pipeline if step not in completed)
        if remaining:
            action = DispatchAction.RESUME if completed else DispatchAction.RUN
            if open_inputs is not None:
                for label in pin.declared_inputs:
                    open_inputs.open(pin, label)
            return ScheduledPin(pin, action, completed, None)
        return ScheduledPin(pin, DispatchAction.RESUME, completed, None)


__all__ = [
    "COMPONENT_PIPELINE",
    "FRAMEWORK_DOCS_GATES",
    "FRAMEWORK_TCB_GATES",
    "PIN_CLOSURE_SCHEMA",
    "PIN_DISPATCH_SCHEMA",
    "PIN_PLAN_SCHEMA",
    "TESTED_AT_PIN_PROPERTY",
    "AdmissionEvent",
    "DispatchAction",
    "MarkerStore",
    "MemoryMarkerStore",
    "MemoryTestedTagStore",
    "Pin",
    "PinDispatchError",
    "PinDispatchService",
    "PinInputOpener",
    "PinKind",
    "PinMarker",
    "ScheduledPin",
    "TestedTag",
    "TestedTagStore",
    "bind_gates_to_pins",
    "pin_closure_identity",
    "pin_from_component_entry",
    "pin_from_documentation_authority",
    "pin_from_flavor_entry",
    "pin_from_framework_tcb",
    "pin_from_skill_entry",
    "pin_plan_identity",
    "pins_from_authority_inventory",
    "rekey_receipt_to_pin",
    "sbom_properties_for_tested_pin",
    "suite_fallback_pin",
    "tested_pin_from_sbom_properties",
    "tested_tag_for_pin",
]
