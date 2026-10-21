"""Observed local execution context; this data never grants admission itself."""

from dataclasses import dataclass

from .execution_dispatch import ExecutionWorker, ExecutionWorkerKind
from .identity import ContentIdentity, canonical_identity
from .retained_project_lifecycle import _fields
from .worker_capabilities import WorkerHardwareObservation


@dataclass(frozen=True, slots=True)
class RetainedLocalWorkerEvidence:
    plan_identity: ContentIdentity
    worker: ExecutionWorker
    observations: tuple[WorkerHardwareObservation, ...]

    def __post_init__(self):
        if (
            not isinstance(self.plan_identity, ContentIdentity)
            or not isinstance(self.worker, ExecutionWorker)
            or self.worker.kind is not ExecutionWorkerKind.LOCAL
            or type(self.observations) is not tuple
            or len(self.observations) != 2
            or any(
                not isinstance(v, WorkerHardwareObservation)
                or v.worker_id != self.worker.worker_id
                for v in self.observations
            )
        ):
            raise ValueError("retained.worker.typed-local-observations-required")

    def require_plan(self, plan):
        if (
            self.plan_identity != plan.identity
            or self.worker.identity != plan.profile.worker_identity
            or self.worker.target_profile != plan.profile.target
        ):
            raise ValueError("retained.worker.plan-mismatch")

    def to_dict(self):
        return {
            "schema": "literate-ai/retained-local-worker-evidence@1",
            "plan_identity": self.plan_identity.uri,
            "worker": self.worker.to_dict(),
            "observations": [v.to_dict() for v in self.observations],
            "grants_admission": False,
        }

    @classmethod
    def from_dict(cls, value):
        value = _fields(
            value,
            {"schema", "plan_identity", "worker", "observations", "grants_admission"},
        )
        if (
            value["schema"] != "literate-ai/retained-local-worker-evidence@1"
            or value["grants_admission"] is not False
            or not isinstance(value["observations"], list)
            or len(value["observations"]) != 2
        ):
            raise ValueError("retained.worker.observation-record-invalid")
        return cls(
            ContentIdentity.parse_uri(value["plan_identity"]),
            ExecutionWorker.from_dict(value["worker"]),
            tuple(
                WorkerHardwareObservation.from_dict(v) for v in value["observations"]
            ),
        )

    @property
    def identity(self):
        return canonical_identity(self.to_dict())
