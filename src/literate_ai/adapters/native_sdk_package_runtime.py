"""Prepare imports from locally admitted package bytes and fresh host observations."""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.native_sdk_consumer import MaterializedNativeSdkInput
from literate_ai.adapters.native_sdk_custody import materialize_native_sdk
from literate_ai.adapters.native_sdk_dependencies import project_native_sdk_dependencies
from literate_ai.adapters.native_sdk_package import NativeSdkPackageResources
from literate_ai.adapters.native_sdk_package_scope import NativeSdkPackageExecutionScope
from literate_ai.adapters.native_sdk_runtime import observe_native_sdk_runtime
from literate_ai.security import AuthorizationError, AuthorizationRevocationSet
from literate_ai.storage import FileSystemCAS


class PackagedNativeSdkInputs:
    """Live local package custody; transported manifests alone cannot construct it."""

    def __init__(self, resources, root, require_current, store, execution_scope=None):
        if not isinstance(resources, NativeSdkPackageResources):
            raise TypeError("packaged SDK execution requires admitted resource custody")
        if not callable(require_current):
            raise TypeError("packaged SDK execution requires current package custody")
        self.resources = resources
        self.root = root
        self.require_current = require_current
        self.store = store
        if execution_scope is not None and not isinstance(
            execution_scope, NativeSdkPackageExecutionScope
        ):
            raise TypeError("packaged SDK scope must carry typed linked authority")
        self.execution_scope = execution_scope
        self.require_unchanged()

    def require_unchanged(self):
        self.require_current()
        self.resources.verify_materialized(self.root)

    def for_consumer(self, revision, *, target_identity):
        self.require_unchanged()
        selected = self._bindings(revision)
        if not selected or any(
            binding.build.selection.target_identity != target_identity
            for binding in selected
        ):
            raise ValueError("packaged SDK inputs differ from consumer or target")
        return selected

    def _bindings(self, revision):
        if self.execution_scope is not None:
            return self.execution_scope.select_bindings(
                self.resources.bindings, revision
            )
        return tuple(
            binding
            for binding in self.resources.bindings
            if binding.build.selection.component_revision == revision
        )

    def observe(self, binding, root):
        sdk = binding.build.product.snapshot
        layout = binding.build.selection.recipe.layout
        observation = observe_native_sdk_runtime(
            sdk,
            expected_identity=sdk.identity,
            target_identity=sdk.target_identity,
            operating_system=layout.operating_system,
            architecture=layout.architecture,
            root=root,
            store=self.store,
        )
        dependencies = project_native_sdk_dependencies(
            binding, root=root, runtime=self.store.get_manifest(observation)
        )
        return MaterializedNativeSdkInput(binding, root, observation, dependencies)

    @contextmanager
    def materialize(self, revision, *, target_identity, parent):
        values = []
        try:
            for binding in self.for_consumer(revision, target_identity=target_identity):
                sdk = binding.build.product.snapshot
                root = self.root / "native-sdks" / binding.identity.digest / "sdk"
                for item in sdk.files:
                    if self.store.put_file(root / item.path) != item.blob:
                        raise ValueError(
                            "packaged SDK bytes changed during preparation"
                        )
                materialized = materialize_native_sdk(
                    sdk, expected_identity=sdk.identity, store=self.store, parent=parent
                )
                try:
                    values.append(self.observe(binding, materialized))
                except BaseException:
                    shutil.rmtree(materialized)
                    raise
            self.require_unchanged()
            yield tuple(values)
            for value in values:
                self.reobserve_runtime(value)
        finally:
            for value in reversed(values):
                shutil.rmtree(value.root)

    def reobserve_runtime(self, value):
        self.require_unchanged()
        if value.binding not in self.resources.bindings:
            raise ValueError("SDK input is outside this package")
        if self.observe(value.binding, value.root).dependencies != value.dependencies:
            raise ValueError("packaged SDK runtime dependencies changed")

    def require_execution_authorized(self, revision, request, authorization, *, now):
        self.require_unchanged()
        if request.effective_revision_digest != revision.uri:
            raise ValueError("packaged SDK grant differs from consumer")
        sources = dict(self.resources.revocation_sources)
        evidence = []
        for binding in self._bindings(revision):
            current = sources[binding.identity]()
            if not isinstance(current, AuthorizationRevocationSet):
                raise AuthorizationError("security.live_revocation_verifier_invalid")
            current.require_build_valid(authorization, request, now=now)
            evidence.append(
                {
                    "input_identity": binding.identity.to_dict(),
                    "state": current.to_dict(),
                }
            )
        if not evidence:
            raise ValueError("packaged SDK grant has no selected inputs")
        return tuple(evidence)


@contextmanager
def prepare_packaged_native_sdk_execution(
    resources, root, revision, *, target, parent, require_current, execution_scope=None
):
    from literate_ai.adapters.native_sdk_execution import prepare_native_sdk_execution

    require_safe_directory(parent)
    with tempfile.TemporaryDirectory(prefix=".packaged-sdk-", dir=parent) as staging:
        owner = PackagedNativeSdkInputs(
            resources,
            root,
            require_current,
            FileSystemCAS(Path(staging) / "cas"),
            execution_scope,
        )
        with prepare_native_sdk_execution(
            owner, revision, target=target, parent=Path(staging)
        ) as inputs:
            yield inputs
