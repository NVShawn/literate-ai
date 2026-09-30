"""Acquire all locked native SDKs under the caller's current host policy."""

import platform
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_recipes import select_native_sdk_recipes
from literate_ai.adapters.native_sdk_source_build import NativeSdkSourceBuildService
from literate_ai.adapters.source.repository_cache import (
    RepositorySourceCachePolicyError,
)
from literate_ai.application.generation_preparation import GenerationPreparationError
from literate_ai.application.repository_sources import RepositorySourceResolutionError
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    SecurityPolicy,
)
from literate_ai.sources import QuarantineStore
from literate_ai.storage import FileSystemCAS, ReferenceIndex


class NativeSdkProjectAcquisition:
    """Single-use acquisition; retain real producer services through consumer use."""

    def __init__(
        self,
        *,
        cache_root: Path,
        source_intelligence_policy,
        security_policy: SecurityPolicy,
        revocations: Callable[[], AuthorizationRevocationSet],
        environment: Mapping[str, str],
        actor: str,
        reason: str,
        host_build_acknowledged: bool = False,
    ):
        self.cache_root = cache_root
        self.source_intelligence_policy = source_intelligence_policy
        self.security_policy = security_policy
        self.revocations = revocations
        self.environment = dict(environment)
        self.actor = actor
        self.reason = reason
        self.host_build_acknowledged = host_build_acknowledged
        self._started = False

    def __call__(self, snapshot) -> NativeSdkConsumerInputs | None:
        if self._started:
            raise ValueError("SDK project acquisition is single-use")
        self._started = True
        try:
            return self._acquire(snapshot)
        except (
            AuthorizationError,
            RepositorySourceCachePolicyError,
            RepositorySourceResolutionError,
            ValueError,
            OSError,
        ) as exc:
            raise GenerationPreparationError(
                getattr(exc, "code", "native_sdk.acquisition_failed"),
                "locked SDK acquisition did not complete: " + str(exc),
            ) from exc

    def _acquire(self, snapshot) -> NativeSdkConsumerInputs | None:
        snapshot.require_unchanged()
        selections = tuple(
            selection
            for node in snapshot.authority.lock.nodes
            if node.revision.repository_sources
            for selection in select_native_sdk_recipes(snapshot, node.revision.identity)
        )
        if not selections:
            return None
        if self.host_build_acknowledged is not True:
            raise GenerationPreparationError(
                "native_sdk.host_build_not_acknowledged",
                "SDK dependency builds require host execution acknowledgement",
            )
        if not callable(self.revocations) or not isinstance(
            self.revocations(), AuthorizationRevocationSet
        ):
            raise GenerationPreparationError(
                "native_sdk.revocation_state_invalid",
                "SDK builds require current revocation state",
            )
        host_os = {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(
            sys.platform
        )
        machine = platform.machine().lower()
        architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
        if any(
            (item.recipe.layout.operating_system, item.recipe.layout.architecture)
            != (host_os, architecture)
            for item in selections
        ):
            raise GenerationPreparationError(
                "native_sdk.host_target_mismatch",
                "SDK recipes require an execution worker with the selected target",
            )
        tools = {}
        for name in sorted({name for item in selections for name in item.recipe.tools}):
            executable = shutil.which(name, path=self.environment.get("PATH", ""))
            if executable is None:
                raise GenerationPreparationError(
                    "native_sdk.tool_unavailable",
                    f"selected SDK build tool {name!r} is unavailable",
                )
            tools[name] = LocalComponentToolBinding(
                str(Path(executable).resolve(strict=True))
            )
        snapshot.require_unchanged()
        store = FileSystemCAS(self.cache_root / "cas")
        quarantine = QuarantineStore(self.cache_root / "quarantine", store)
        references = ReferenceIndex(self.cache_root / "references", store)
        services = tuple(
            NativeSdkSourceBuildService(
                snapshot=snapshot,
                selection=selection,
                tools={name: tools[name] for name in selection.recipe.tools},
                store=store,
                quarantine=quarantine,
                references=references,
                source_intelligence_policy=self.source_intelligence_policy,
                security_policy=self.security_policy,
                revocations=self.revocations,
                actor=self.actor,
                reason=self.reason,
                environment=self.environment,
                host_build_acknowledged=True,
            )
            for selection in selections
        )
        for service in services:
            service.build()
        inputs = NativeSdkConsumerInputs(snapshot=snapshot, services=services)
        inputs.require_unchanged()
        return inputs
