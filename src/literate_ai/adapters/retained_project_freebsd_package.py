"""Native pkg construction from live retained admission, without installation."""

from dataclasses import replace

from literate_ai.adapters.freebsd_packaging import (
    FreeBsdPackageAdapter,
    FreeBsdPkgToolBinding,
    project_freebsd_package,
)
from literate_ai.adapters.retained_project_package import _RetainedProjectPackage
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts import BlobRef, canonical_identity, canonical_json_bytes


class RetainedProjectFreeBsdPackage(_RetainedProjectPackage):
    """Keep native format authority separate from retained lifecycle admission.

    This backend constructs an archive of an accepted retained payload. It does
    not infer per-Component runtime dependencies or grant install/release access.
    """

    def __init__(
        self,
        admission,
        *,
        provider_lock_identity,
        provider_revision,
        tool,
        package,
        version,
        origin,
        target_abi,
        prefix="/usr/local",
        installed_paths=None,
    ):
        if not isinstance(tool, FreeBsdPkgToolBinding):
            raise TypeError("retained.package.typed-native-tool-required")
        self.tool = tool
        super().__init__(
            admission,
            provider_lock_identity=provider_lock_identity,
            provider_revision=provider_revision,
            provider="pkg",
            packager_identity=canonical_identity(
                {"schema": "literate-ai/retained-pkg-projection-inputs@1"}
            ),
        )
        tool.require_unchanged()
        source_plan = self.plan
        projection = project_freebsd_package(
            source_plan,
            package=package,
            version=version,
            origin=origin,
            target_abi=target_abi,
            worker_abi=tool.abi,
            worker_architecture=tool.architecture,
            resolved_sbom=admission.run.resolved_bom.content,
            tool_identity=tool.identity,
            prefix=prefix,
            installed_paths=installed_paths,
        )
        self.adapter = FreeBsdPackageAdapter(projection, tool)
        self._plan = replace(
            source_plan, packager_identity=self.adapter.packager_identity
        )
        self.require_current()

    @property
    def projection(self):
        return self.adapter.projection

    def require_current(self):
        super().require_current()
        self.adapter._require_plan(self.plan)
        if self.tool.identity != self.projection.tool_identity:
            raise PackagingError("retained native pkg tool differs from projection")
        self.tool.require_unchanged()

    def _package(self, stage, workspace):
        self.admission.store.put_manifest(self.projection.to_dict())
        self.admission.store.put_manifest(self.tool.to_dict())
        return self.adapter.package(
            self.plan, materialized_root=stage, object_root=workspace
        )

    def _verify_archive(self, result, content):
        self.adapter.verify_bytes(self.plan, result, content)

    def verify(self, result):
        self.require_current()
        for record in (self.projection.to_dict(), self.tool.to_dict()):
            self.admission.store.verify(
                BlobRef(
                    canonical_identity(record).digest,
                    len(canonical_json_bytes(record)),
                    media_type="application/json",
                )
            )
        return super().verify(result)
