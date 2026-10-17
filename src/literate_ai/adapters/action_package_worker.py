"""Private package adapter composition for the one-shot command receiver."""

from collections.abc import Callable
from dataclasses import dataclass

from literate_ai.adapters.action_capabilities import package_profile_identity
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_package import (
    admit_package_action,
    execute_package_action,
)
from literate_ai.contracts import ContentIdentity


@dataclass(frozen=True)
class ConfiguredPackageWorker:
    packager_identity: ContentIdentity
    adapter_factory: Callable
    require_current: Callable

    def __post_init__(self):
        if (
            not isinstance(self.packager_identity, ContentIdentity)
            or not callable(self.adapter_factory)
            or not callable(self.require_current)
        ):
            raise TypeError(
                "PACKAGE requires private packager authority, factory and guard"
            )

    @property
    def identity(self):
        return package_profile_identity(self.packager_identity)

    def execute(
        self,
        request,
        deadline,
        records,
        *,
        expected_worker_identity,
        cas,
        admission_guard,
        blob_source=None,
        cancelled=lambda: False,
    ):
        if not callable(admission_guard) or not callable(cancelled):
            raise TypeError("PACKAGE requires live receiver admission and cancellation")
        records = dict(records)
        value = admit_package_action(
            request,
            deadline,
            records,
            expected_worker_identity=expected_worker_identity,
        )
        if value.plan.packager_identity != self.packager_identity:
            raise ActionWireError(
                "action_package.packager_mismatch",
                "configured packager differs from plan",
            )

        def current():
            deadline.remaining()
            if cancelled():
                raise ActionWireError(
                    "action_package.cancelled", "PACKAGE action was cancelled"
                )
            self.require_current()
            admission_guard()
            deadline.remaining()

        current()
        adapter = self.adapter_factory()
        current()
        result = execute_package_action(
            request,
            deadline,
            records,
            expected_worker_identity=expected_worker_identity,
            cas=cas,
            admission_guard=current,
            package_adapter=adapter,
            packager_identity=self.packager_identity,
            blob_source=blob_source,
            read_created_blob=getattr(adapter, "read_created_blob", None),
            cancelled=cancelled,
        )
        current()
        return result
