"""Exact linked Component owners for a packaged native SDK execution closure."""

from dataclasses import dataclass

from literate_ai.application.artifact_graph import realize_manifest
from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildPlan,
)
from literate_ai.contracts.component_locking import ComponentAuthoring, ComponentLock
from literate_ai.contracts.executable_components import (
    ArtifactBuildGraph,
    ComponentCommandContract,
    ExactLinkPlan,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.sbom import (
    ManagedComponentKind,
    project_component_lock_managed_graph,
)


@dataclass(frozen=True, slots=True)
class NativeSdkPackageExecutionScope:
    component_lock: ComponentLock
    artifact_graph: ArtifactBuildGraph
    link_plan: ExactLinkPlan
    plans: tuple[StandardComponentBuildPlan, ...]
    contracts: tuple[ComponentCommandContract, ...]
    execution_revision: ContentIdentity | None = None

    @property
    def root_revision(self):
        return self.execution_revision or self.component_lock.root_revision

    def __post_init__(self):
        if (
            not isinstance(self.component_lock, ComponentLock)
            or not isinstance(self.artifact_graph, ArtifactBuildGraph)
            or not isinstance(self.link_plan, ExactLinkPlan)
        ):
            raise TypeError(
                "SDK package scope requires typed lock and artifact authority"
            )
        if self.link_plan not in self.artifact_graph.link_plans:
            raise ValueError("SDK package link plan differs from its graph")
        exports = {
            item.identity: item
            for manifest in self.artifact_graph.manifests
            for item in manifest.exports
        }
        self._require(
            exports[self.link_plan.root_artifact_identity].component_revision
            == self.root_revision
        )
        linked = {
            exports[item].component_revision
            for item in self.link_plan.ordered_artifact_identities
        }
        managed = project_component_lock_managed_graph(
            self.component_lock, self.root_revision
        )
        self._require(
            linked
            <= {
                item.identity
                for item in managed.components
                if item.kind
                in (ManagedComponentKind.ROOT, ManagedComponentKind.COMPONENT)
            }
        )
        for values, kind in (
            (self.plans, StandardComponentBuildPlan),
            (self.contracts, ComponentCommandContract),
        ):
            self._require(
                isinstance(values, tuple) and all(isinstance(v, kind) for v in values)
            )
            revisions = tuple(v.component_revision for v in values)
            self._require(revisions == tuple(sorted(linked, key=lambda v: v.uri)))
        manifests = {
            item.component_revision: item for item in self.artifact_graph.manifests
        }
        for plan, contract in zip(self.plans, self.contracts, strict=True):
            manifest = manifests[plan.component_revision]
            self._require(realize_manifest(plan.manifest, manifest.exports) == manifest)
            self._require(contract.component_revision == plan.component_revision)
            shapes = {
                item.export_id: item for item in contract.artifact_export_shapes()
            }
            declarations = {
                item.export_id: item for item in plan.manifest.export_declarations
            }
            self._require(set(shapes) == set(declarations))
            self._require(
                all(
                    getattr(shape, key) == getattr(declarations[name], key)
                    for name, shape in shapes.items()
                    for key in (
                        "role",
                        "abi_identity",
                        "target_identity",
                        "media_type",
                        "producer_identity",
                    )
                )
            )

    @staticmethod
    def _require(value):
        if not value:
            raise ValueError(
                "SDK package execution scope differs from linked authority"
            )

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @property
    def input_identities(self):
        values = [
            item
            for plan in self.plans
            for item in plan.materialization.native_sdk_input_identities
        ]
        self._require(len(values) == len(set(values)))
        return tuple(sorted(values, key=lambda item: item.uri))

    def plan_for(self, revision):
        plan = next((p for p in self.plans if p.component_revision == revision), None)
        self._require(plan is not None)
        return plan

    def contract_for(self, revision):
        contract = next(
            (c for c in self.contracts if c.component_revision == revision), None
        )
        self._require(contract is not None)
        return contract

    def require_package(self, package):
        self._require(self.execution_revision is None)
        self._require(
            package.component_lock_identity == self.component_lock.identity
            and package.root_component_revision == self.root_revision
            and package.artifact_graph_identity == self.artifact_graph.identity
            and package.link_plan_identity == self.link_plan.identity
            and package.root_artifact_identity == self.link_plan.root_artifact_identity
            and package.target_identity
            == self.contract_for(self.root_revision).artifact_export.target_identity
        )

    def select_bindings(self, bindings, revision):
        self._require(revision == self.root_revision)
        owners = {plan.component_revision for plan in self.plans}
        selected = tuple(
            b for b in bindings if b.build.selection.component_revision in owners
        )
        self._require(
            tuple(sorted((b.identity for b in selected), key=lambda v: v.uri))
            == self.input_identities
        )
        namespaces = set()
        for binding in selected:
            owner = binding.build.selection.component_revision
            plan = self.plan_for(owner)
            self._require(
                binding.identity in plan.materialization.native_sdk_input_identities
            )
            sdk = binding.build.product.snapshot
            self._require(
                sdk.target_identity
                == self.contract_for(owner).artifact_export.target_identity
            )
            namespace = (sdk.import_surface.language, sdk.import_surface.package)
            if namespace in namespaces:
                raise ValueError("linked SDK import namespaces conflict")
            namespaces.add(namespace)
        return selected

    def to_dict(self):
        return {
            "schema": "literate-ai/native-sdk-package-execution-scope@1"
            if self.execution_revision is None
            else "literate-ai/native-sdk-linked-execution-scope@1",
            **(
                {}
                if self.execution_revision is None
                else {"execution_revision": self.execution_revision.to_dict()}
            ),
            "component_lock": self.component_lock.to_dict(),
            "authorings": [item.to_dict() for item in self.component_lock.authorings],
            "artifact_graph": self.artifact_graph.to_dict(),
            "link_plan": self.link_plan.to_dict(),
            "plans": [item.to_dict() for item in self.plans],
            "contracts": [item.to_dict() for item in self.contracts],
        }

    @classmethod
    def from_dict(cls, value):
        linked = (
            isinstance(value, dict)
            and value.get("schema") == "literate-ai/native-sdk-linked-execution-scope@1"
        )
        cls._require(
            isinstance(value, dict)
            and set(value)
            == {
                "schema",
                "component_lock",
                "authorings",
                "artifact_graph",
                "link_plan",
                "plans",
                "contracts",
            }
            | ({"execution_revision"} if linked else set())
        )
        cls._require(
            value["schema"]
            in (
                "literate-ai/native-sdk-package-execution-scope@1",
                "literate-ai/native-sdk-linked-execution-scope@1",
            )
        )
        return cls(
            ComponentLock.from_dict(
                value["component_lock"],
                authorings=tuple(
                    ComponentAuthoring.from_dict(v) for v in value["authorings"]
                ),
            ),
            ArtifactBuildGraph.from_dict(value["artifact_graph"]),
            ExactLinkPlan.from_dict(value["link_plan"]),
            tuple(StandardComponentBuildPlan.from_dict(v) for v in value["plans"]),
            tuple(ComponentCommandContract.from_dict(v) for v in value["contracts"]),
            ContentIdentity.from_dict(value["execution_revision"]) if linked else None,
        )
