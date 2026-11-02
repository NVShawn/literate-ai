"""Per-unit plans from live retained admission and exact locked output intent.

Planning reads current authority and custody but writes no CAS/host files and
executes no tools. Parsed receipts cannot instantiate this owner. Native archive
construction and live SDK/runtime/LINK scope remain separate mandatory gates.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters import freebsd_packaging as native
from literate_ai.adapters.freebsd_composed_tree_packaging import (
    FreeBsdComposedPackageSetProjection,
    composed_freebsd_packager_identity,
    composed_freebsd_policy,
    composed_freebsd_tool_identity,
)
from literate_ai.adapters.freebsd_retained_unit_parameters import (
    UNIT_PACKAGE_EVIDENCE_FIELDS,
    FreeBsdRetainedUnitParameters,
)
from literate_ai.adapters.native_tree_shards import prepare_native_tree
from literate_ai.adapters.retained_project_metadata import (
    read_retained_project_document,
)
from literate_ai.adapters.retained_project_receipt import (
    AdmittedRetainedProject,
    select_retained_package_payload,
)
from literate_ai.adapters.retained_unit_graphs import prepare_retained_unit_graph
from literate_ai.adapters.retained_unit_specifications import (
    UNIT_REQUEST_SUFFIX,
    RetainedUnitSpecificationContext,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageInput,
)
from literate_ai.contracts.flavors import FlavorAxis
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.native_sdk_trees import NativeSdkAlias
from literate_ai.contracts.native_sdks import NativeSdkFile
from literate_ai.contracts.native_tree_shards import MAX_TREE_MEMBERS
from literate_ai.contracts.retained_component_ownership import (
    RetainedComponentOwner,
    require_registered_retained_owners,
    retained_owner,
)
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectManifest,
)
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectPlan
from literate_ai.contracts.retained_project_receipt import RetainedProjectReceipt
from literate_ai.contracts.retained_unit_packages import (
    RetainedUnitDependency,
    RetainedUnitOwnership,
    RetainedUnitPackageDefinition,
    RetainedUnitPackageSetPlan,
)
from literate_ai.contracts.retained_unit_partition import RetainedUnitPartition
from literate_ai.contracts.retained_unit_requests import RetainedUnitPackageRequest
from literate_ai.storage.cas import FileSystemCAS


def _reference(content, media_type="application/json"):
    return BlobRef(
        hashlib.sha256(content).hexdigest(), len(content), media_type=media_type
    )


@dataclass(frozen=True, slots=True)
class RetainedUnitPackagePlanningInputs:
    """Typed assertions for pure planning, never a completed verifier or producer."""

    source: Path
    store: FileSystemCAS
    receipt: RetainedProjectReceipt
    plan: RetainedProjectPlan
    source_manifest: RetainedProjectManifest
    payload_manifest: RetainedProjectManifest
    output_manifest: RetainedProjectManifest
    component_locks: tuple[ComponentLock, ...]

    def __post_init__(self):
        if (
            not isinstance(self.source, Path)
            or not isinstance(self.store, FileSystemCAS)
            or not isinstance(self.receipt, RetainedProjectReceipt)
            or not isinstance(self.plan, RetainedProjectPlan)
            or not isinstance(self.source_manifest, RetainedProjectManifest)
            or not isinstance(self.payload_manifest, RetainedProjectManifest)
            or not isinstance(self.output_manifest, RetainedProjectManifest)
            or type(self.component_locks) is not tuple
            or any(not isinstance(lock, ComponentLock) for lock in self.component_locks)
        ):
            raise TypeError("retained.unit.typed-planning-inputs-required")
        if (
            self.receipt.root("plan").identity != self.plan.identity.uri
            or self.receipt.root("input-manifest").identity
            != self.source_manifest.identity.uri
            or self.receipt.root("payload-manifest").identity
            != self.payload_manifest.identity.uri
            or self.plan.manifest_identity != self.source_manifest.identity
            or tuple(lock.identity for lock in self.component_locks)
            != self.plan.component_locks
            or self.receipt.project_id != self.plan.project_id
            or self.output_manifest.limits != self.source_manifest.limits
            or self.receipt.root("output-manifest").identity
            != self.output_manifest.identity.uri
            or self.payload_manifest
            != select_retained_package_payload(self.output_manifest, self.plan.profile)
        ):
            raise ValueError("retained.unit.planning-assertion-context-mismatch")


def plan_retained_unit_packages(inputs, **policy):
    """Compute exact payload-bound plans without retaining or granting authority."""
    return _RetainedUnitPackagePlanner(inputs, **policy).projection


class _RetainedUnitPackagePlanner:
    """Pure computation over typed assertions; no native production methods."""

    def __init__(
        self,
        inputs,
        *,
        pkg_tool,
        tar_tool,
        limits,
        per_unit_limits,
        max_units,
        max_dependencies,
        max_metadata_bytes,
        max_staging_bytes,
        max_transport_bytes,
        max_archive_bytes,
        max_total_archive_bytes,
        max_descriptor_bytes=8 * 1024**2,
        max_descriptor_total_bytes=512 * 1024**2,
        max_index_bytes=2 * 1024**2,
        max_metadata_memory_bytes=1024**3,
        max_closure_steps=16_000_000,
        max_closure_memory_bytes=1024**3,
        max_provider_metadata_bytes=64 * 1024**2,
        max_provider_memory_bytes=256 * 1024**2,
        max_verified_record_cache_bytes=1024**3,
        specification_context=None,
    ):
        if not isinstance(inputs, RetainedUnitPackagePlanningInputs):
            raise TypeError("retained.unit.typed-planning-inputs-required")
        if not isinstance(limits, RetainedProjectLimits) or not isinstance(
            per_unit_limits, RetainedProjectLimits
        ):
            raise TypeError("retained.unit.explicit-package-limits-required")
        self.inputs, self.pkg_tool, self.tar_tool = inputs, pkg_tool, tar_tool
        if specification_context is not None and not isinstance(
            specification_context, RetainedUnitSpecificationContext
        ):
            raise TypeError("retained.unit.current-reference-context-required")
        self.specification_context = specification_context
        composed_freebsd_tool_identity(pkg_tool, tar_tool)
        self.limits, self.per_unit_limits = limits, per_unit_limits
        self.max_units, self.max_dependencies, self.max_metadata_bytes = (
            max_units,
            max_dependencies,
            max_metadata_bytes,
        )
        for value, maximum in (
            (max_units, 1_000_000),
            (max_dependencies, 4_000_000),
            (max_metadata_bytes, 512 * 1024**2),
            (max_provider_metadata_bytes, 512 * 1024**2),
            (max_provider_memory_bytes, 16 * 1024**3),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("retained.unit.package-policy-out-of-bounds")
        self.tree_policy = dict(
            max_descriptor_bytes=max_descriptor_bytes,
            max_descriptor_total_bytes=max_descriptor_total_bytes,
            max_index_bytes=max_index_bytes,
            max_metadata_memory_bytes=max_metadata_memory_bytes,
        )
        self.provider_policy = dict(
            max_provider_metadata_bytes=max_provider_metadata_bytes,
            max_provider_memory_bytes=max_provider_memory_bytes,
            max_verified_record_cache_bytes=max_verified_record_cache_bytes,
        )
        self.closure_policy = dict(
            max_closure_steps=max_closure_steps,
            max_closure_memory_bytes=max_closure_memory_bytes,
        )
        self.archive_policy = dict(
            max_staging_bytes=max_staging_bytes,
            max_transport_bytes=max_transport_bytes,
            max_archive_bytes=max_archive_bytes,
            max_total_archive_bytes=max_total_archive_bytes,
        )
        self._metadata = {}
        self._metadata_bytes = 0
        self.projection = self._plan()

    def _document(self, value, identity):
        if identity in self._known_metadata:
            return self._known_metadata[identity]
        if callable(value):
            value = value()
        content = canonical_json_bytes(value)
        reference = _reference(content)
        if reference.identity != identity.uri:
            raise ValueError("retained.unit.metadata-provenance-mismatch")
        total = self._metadata_bytes + len(content)
        if (
            total > self.provider_policy["max_provider_metadata_bytes"]
            or self._request_memory + 16 * total
            > self.provider_policy["max_provider_memory_bytes"]
        ):
            raise ValueError("retained.unit.metadata-byte-policy-exceeded")
        self._metadata[reference] = content
        self._metadata_bytes = total
        self._known_metadata[identity] = reference
        return reference

    def _tree(self, manifest):
        files, aliases = [], []
        count, encoded = len(manifest.directories), 0
        for directory in manifest.directories:
            encoded += len(canonical_json_bytes(directory)) + 1
        baseline = 16 * (
            self.tree_policy["max_descriptor_bytes"]
            + self.tree_policy["max_index_bytes"]
        )
        for member in manifest.members:
            if member.symlink_target is not None:
                value, group = (
                    NativeSdkAlias(member.path, member.symlink_target),
                    aliases,
                )
            elif member.hardlink_target is not None:
                value, group = (
                    NativeSdkAlias(member.path, member.hardlink_target, "hardlink"),
                    aliases,
                )
            else:
                value, group = (
                    NativeSdkFile(
                        member.path,
                        BlobRef(member.sha256, member.size),
                        member.mode == 0o755,
                    ),
                    files,
                )
            count += 1
            encoded += len(canonical_json_bytes(value.to_dict())) + 1
            if (
                count > min(self.limits.max_entries, MAX_TREE_MEMBERS)
                or baseline + 16 * encoded + 2048 * count
                > self.tree_policy["max_metadata_memory_bytes"]
            ):
                raise ValueError("retained.unit.tree-metadata-policy-exceeded")
            group.append(value)
        return prepare_native_tree(
            manifest.directories,
            tuple(files),
            tuple(aliases),
            limits=self.limits,
            case_sensitive=manifest.tree_policy.case_sensitive
            if manifest.tree_policy is not None
            else False,
            **self.tree_policy,
        )

    def _requests(self, members):
        wanted = {}
        memory = 128 * len(members)
        context = self.specification_context
        if context is not None:
            context.require_current(
                self.inputs.source,
                self.inputs.source_manifest,
                self.inputs.component_locks,
            )
            memory += context.memory_bytes
        self._lock_ids = {}
        for lock in self.inputs.component_locks:
            node = next(
                value
                for value in lock.nodes
                if value.revision.identity == lock.root_revision
            )
            memory += 8192 * len(lock.nodes)
            if memory > self.provider_policy["max_provider_memory_bytes"]:
                raise ValueError("retained.unit.lock-index-memory-policy-exceeded")
            self._lock_ids[id(lock)] = lock.identity
            for spec in node.revision.specifications:
                memory += 4096 + 4 * len(spec.uri.encode("utf-8"))
                if memory > self.provider_policy["max_provider_memory_bytes"]:
                    raise ValueError(
                        "retained.unit.request-index-memory-policy-exceeded"
                    )
                if not spec.uri.endswith(UNIT_REQUEST_SUFFIX):
                    continue
                source_path = (
                    spec.uri if context is None else context.path_for(lock, spec)
                )
                wanted.setdefault((source_path, spec.identity.digest), []).append(
                    (lock, node.revision, spec)
                )
        result, total = [], 0
        self._spec_members = {}
        for member in members.values():
            key = (member.path, member.sha256)
            if not member.path.endswith(UNIT_REQUEST_SUFFIX) or key not in wanted:
                continue
            if (
                member.symlink_target is not None
                or member.mode != 0o644
                or member.size > 8 * 1024**2
            ):
                raise ValueError("retained.unit.locked-regular-request-required")
            for lock, revision, spec in wanted[key]:
                self._spec_members[(lock.identity, spec.uri, spec.identity)] = member
                total += member.size
                if (
                    len(result) >= self.max_units
                    or total > self.provider_policy["max_provider_metadata_bytes"]
                    or memory + 16 * total
                    > self.provider_policy["max_provider_memory_bytes"]
                ):
                    raise ValueError("retained.unit.request-decode-policy-exceeded")
                document = read_retained_project_document(
                    self.inputs.source,
                    member,
                    maximum_bytes=8 * 1024**2,
                    error_prefix="retained.unit.request",
                )
                request = RetainedUnitPackageRequest.from_dict(document)
                selected = [
                    value.flavor_revision
                    for slot in node_selection(lock).slots
                    if slot.slot.axis is FlavorAxis.PACKAGING
                    for value in slot.selected
                    if value.value == "pkg"
                ]
                if len(selected) != 1:
                    raise ValueError("retained.unit.exact-locked-pkg-provider-required")
                if native._VERSION.fullmatch(request.version) is None:
                    raise ValueError("retained.unit.native-package-version-invalid")
                result.append((request, lock, revision, spec, selected[0]))
        if not result:
            raise ValueError(
                "retained.unit.locked-package-unit-specifications-required"
            )
        if len({value[0].unit_name for value in result}) != len(result):
            raise ValueError("retained.unit.ambiguous-request-owner")
        self._request_memory = memory + 16 * total
        return tuple(sorted(result, key=lambda value: value[0].unit_name))

    def _plan(self):
        inputs, receipt = self.inputs, self.inputs.receipt
        source = inputs.source_manifest
        if (
            128 * len(source.members)
            + (
                self.specification_context.memory_bytes
                if self.specification_context
                else 0
            )
            > self.provider_policy["max_provider_memory_bytes"]
        ):
            raise ValueError("retained.unit.source-index-memory-policy-exceeded")
        members = {value.path: value for value in source.members}
        requests = self._requests(members)
        targets = {lock.target_profile_identity for _, lock, _, _, _ in requests}
        if len(targets) != 1 or any(
            lock.target_profile_identity not in targets
            for lock in inputs.component_locks
        ):
            raise ValueError("retained.unit.mixed-target-locks")
        tree = self._tree(inputs.payload_manifest)
        tree_ref = _reference(canonical_json_bytes(tree.index.to_dict()))
        self._known_metadata = {
            ContentIdentity.parse_uri(value.identity): value
            for value in receipt.records
        }
        self._known_metadata.update(
            {
                ContentIdentity.parse_uri(value.identity): value
                for _, value in receipt.roots
            }
        )
        receipt_ref = self._document(receipt.to_dict, receipt.identity)
        profile_ref = self._document(
            inputs.plan.profile.to_dict, inputs.plan.profile.identity
        )
        definitions, parameters, declarations = {}, {}, {}
        for request, lock, revision, request_spec, provider_revision in requests:
            request_identity = request_spec.identity
            lock_identity = self._lock_ids[id(lock)]
            license_member = members.get(request.license_path)
            if (
                license_member is None
                or license_member.symlink_target is not None
                or license_member.mode != 0o644
                or not license_member.size
            ):
                raise ValueError("retained.unit.authoritative-source-license-required")
            license_ref = BlobRef(license_member.sha256, license_member.size)
            self.inputs.store.verify(license_ref)
            evidence_refs = {
                "component-authoring": self._document(
                    revision.definition.to_dict, revision.authoring_identity
                ),
                "component-lock": self._document(lock.to_dict, lock_identity),
                "retained-project-profile": profile_ref,
                "retained-source-manifest": receipt.root("input-manifest"),
                "cyclonedx-source-sbom": receipt.root("source-bom"),
                "cyclonedx-resolved-sbom": receipt.root("resolved-bom"),
                "retained-receipt": receipt_ref,
                "license": license_ref,
            }
            prefix = ".literate-ai/units/" + request.unit_name + "/"
            evidence = [
                PackageInput(
                    prefix + role + ".blob",
                    role,
                    PackageFileKind.RESOURCE,
                    ContentIdentity.parse_uri(ref.identity),
                    lock.target_profile_identity,
                    ref,
                )
                for role, ref in evidence_refs.items()
            ]
            # The full selected lock retains every specification reference.
            # Include this unit's exact locked declaration as its raw evidence;
            # do not duplicate all sibling unit declarations into every archive.
            for spec_identity in (request_identity,):
                member = self._spec_members.get(
                    (lock_identity, request_spec.uri, spec_identity)
                )
                if member is None:
                    raise ValueError("retained.unit.locked-specification-bytes-missing")
                evidence.append(
                    PackageInput(
                        prefix + "specs/" + member.sha256 + ".blob",
                        "component-specification",
                        PackageFileKind.RESOURCE,
                        revision.authoring_identity,
                        lock.target_profile_identity,
                        BlobRef(member.sha256, member.size),
                    )
                )
            # Equal-content specifications are one retained evidence file.
            evidence = tuple(
                sorted(
                    {value.path: value for value in evidence}.values(),
                    key=lambda value: value.path,
                )
            )
            params = FreeBsdRetainedUnitParameters(
                request.package,
                request.origin,
                request.prefix,
                self.pkg_tool.architecture,
                evidence,
            )
            content = canonical_json_bytes(params.to_dict())
            params_ref = _reference(content)
            if params_ref not in self._metadata:
                total = self._metadata_bytes + len(content)
                if (
                    total > self.provider_policy["max_provider_metadata_bytes"]
                    or self._request_memory + 16 * total
                    > self.provider_policy["max_provider_memory_bytes"]
                ):
                    raise ValueError("retained.unit.provider-metadata-policy-exceeded")
                self._metadata_bytes = total
                self._metadata[params_ref] = content
            refs = {
                UNIT_PACKAGE_EVIDENCE_FIELDS[role]: ContentIdentity.parse_uri(
                    ref.identity
                )
                for role, ref in evidence_refs.items()
            }
            definition = RetainedUnitPackageDefinition(
                request.unit_name,
                revision.coordinate,
                request_identity=request_identity,
                retained_plan_identity=inputs.plan.identity,
                payload_identity=tree.identity,
                target_identity=lock.target_profile_identity,
                provider_revision=provider_revision,
                provider_parameters=params_ref,
                abi=self.pkg_tool.abi,
                version=request.version,
                **refs,
            )
            definitions[request.unit_name], parameters[request.unit_name] = (
                definition,
                params,
            )
            declarations[request.unit_name] = (request, revision)
        ownership = _output_partition(tree, definitions, declarations)
        edges = []
        for request, *_ in requests:
            for edge in request.dependencies:
                if edge.unit_name not in definitions:
                    raise ValueError("retained.unit.unselected-dependency")
                target = definitions[edge.unit_name]
                edges.append(
                    RetainedUnitDependency(
                        definitions[request.unit_name].identity,
                        target.identity,
                        edge.kind,
                        target.target_identity,
                        edge.required_abi,
                        edge.required_version,
                    )
                )
                if len(edges) > self.max_dependencies:
                    raise ValueError("retained.unit.dependency-count-policy-exceeded")
        selected = tuple(
            sorted(definitions.values(), key=lambda value: value.identity.uri)
        )
        partition = RetainedUnitPartition(
            tree,
            selected,
            ownership,
            tuple(
                sorted(
                    edges,
                    key=lambda value: (
                        value.source_unit_identity.uri,
                        value.target_unit_identity.uri,
                        value.kind,
                    ),
                )
            ),
            self.per_unit_limits,
            self.max_units,
            self.max_dependencies,
            self.max_metadata_bytes,
        )
        graph = prepare_retained_unit_graph(partition, tree_ref, **self.tree_policy)
        graph_ref = _reference(canonical_json_bytes(graph.index.to_dict()))
        tools = composed_freebsd_tool_identity(self.pkg_tool, self.tar_tool)
        policy = composed_freebsd_policy(**self.closure_policy, **self.provider_policy)
        first = selected[0]
        plan = RetainedUnitPackageSetPlan(
            first.retained_plan_identity,
            first.receipt_identity,
            first.target_identity,
            graph_ref,
            tree_ref,
            canonical_identity(policy),
            composed_freebsd_packager_identity(tools),
            tools,
            1,
            **self.archive_policy,
        )
        return FreeBsdComposedPackageSetProjection(
            plan,
            graph,
            tuple(parameters[value.unit_name] for value in selected),
            self.pkg_tool,
            self.tar_tool,
            **self.closure_policy,
            **self.provider_policy,
        )


class RetainedProjectUnitPackages(_RetainedUnitPackagePlanner):
    """Original-authority owner; mechanical planning inputs cannot instantiate it."""

    def __init__(self, admission, **policy):
        if not isinstance(admission, AdmittedRetainedProject):
            raise TypeError("retained.unit.live-admission-required")
        admission.require_current()
        self.admission = admission
        super().__init__(_live_planning_inputs(admission), **policy)
        admission.require_current()

    def require_current(self):
        """Recompute original-authority bindings without executing native tools."""
        self.admission.require_current()
        self.inputs = _live_planning_inputs(self.admission)
        self._metadata = {}
        self._metadata_bytes = 0
        current = self._plan()
        if current.identity != self.projection.identity:
            raise ValueError("retained.unit.package-plan-changed")
        self.admission.require_current()

    def construct(self, *, object_root, link_handoff=None):
        """Produce and independently reopen every unit under current live scope."""
        from literate_ai.adapters.freebsd_retained_unit_archives import (
            construct_retained_unit_archives,
        )

        return construct_retained_unit_archives(
            self, object_root=object_root, link_handoff=link_handoff
        )

    def retain_planning_evidence(self):
        """Capture historical joins without changing the aggregate receipt."""
        from literate_ai.adapters.retained_unit_planning_evidence import (
            retain_retained_unit_planning_evidence,
        )

        return retain_retained_unit_planning_evidence(
            self.admission,
            maximum_bytes=min(
                self.tree_policy["max_descriptor_bytes"],
                self.provider_policy["max_provider_metadata_bytes"],
            ),
            maximum_memory_bytes=self.provider_policy["max_provider_memory_bytes"],
        )

    def construct_set(self, *, object_root, link_handoff=None):
        """Retain bounded descriptors only after complete native verification."""
        from literate_ai.adapters.retained_unit_results import (
            retain_retained_unit_results,
        )

        results = self.construct(object_root=object_root, link_handoff=link_handoff)
        reference = retain_retained_unit_results(
            self.projection, results, self.admission.store, **self.tree_policy
        )
        self.require_current()
        self.admission.require_native_package_scope()
        self.pkg_tool.require_unchanged()
        self.tar_tool.require_current()
        return reference

    def verify_set(self, reference, *, object_root):
        """Reopen assertions under current live authority and native tools."""
        import platform

        from literate_ai.adapters.freebsd_composed_tree_packaging import (
            verify_freebsd_composed_package_set,
        )
        from literate_ai.adapters.retained_unit_link import link_retained_unit_packages
        from literate_ai.adapters.retained_unit_results import (
            read_retained_unit_results,
        )
        from literate_ai.application.packaging import PackagingError

        if platform.system().casefold() != "freebsd":
            raise PackagingError("Native unit package verification requires FreeBSD")
        self.require_current()
        self.admission.require_native_package_scope()
        self.pkg_tool.require_unchanged()
        self.tar_tool.require_current()
        handoff = link_retained_unit_packages(self)
        index, results = read_retained_unit_results(
            self.projection, reference, self.admission.store, **self.tree_policy
        )
        verify_freebsd_composed_package_set(
            self.projection,
            results,
            store=self.admission.store,
            object_root=object_root,
        )
        handoff.require_current(self)
        self.pkg_tool.require_unchanged()
        self.tar_tool.require_current()
        return index


def _live_planning_inputs(admission):
    return RetainedUnitPackagePlanningInputs(
        admission.source,
        admission.store,
        admission.receipt,
        admission.run.source_bom.plan,
        admission.run.source_bom.manifest,
        admission.payload_manifest,
        admission.run.output_manifest,
        admission.run.source_bom.component_locks,
    )


def node_selection(lock):
    return next(
        value.target_flavor_selection
        for value in lock.nodes
        if value.revision.identity == lock.root_revision
    )


def _output_partition(tree, definitions, declarations):
    # Reuse canonical Component references and depth-based ownership lookup, but
    # construct these records ONLY from explicitly authored OUTPUT selectors.
    nodes = {
        *tree.directories,
        *(value.path for value in tree.files),
        *(value.path for value in tree.aliases),
    }
    by_prefix, units = {}, {}
    whole = []
    authorings = tuple(
        {
            value[1].authoring_identity: value[1].definition
            for value in declarations.values()
        }.values()
    )
    for name, (request, revision) in declarations.items():
        if request.whole_payload:
            whole.append(name)
        for prefix in request.output_prefixes:
            if prefix not in nodes or prefix in by_prefix:
                raise ValueError("retained.unit.missing-or-overlapping-output-prefix")
            by_prefix[prefix] = RetainedComponentOwner(
                prefix, revision.coordinate, revision.authoring_identity
            )
            units[prefix] = name
    if len(whole) > 1:
        raise ValueError("retained.unit.overlapping-whole-payload-owners")
    require_registered_retained_owners(tuple(by_prefix.values()), authorings)
    nonempty = {path.rpartition("/")[0] for path in nodes if "/" in path}
    payload = {value.path: "file" for value in tree.files}
    payload.update((value.path, value.kind) for value in tree.aliases)
    payload.update(
        (path, "empty-directory") for path in tree.directories if path not in nonempty
    )
    result, used = [], set()
    for path in sorted(payload):
        owner = retained_owner(path, by_prefix)
        if owner is not None:
            name = units[owner.prefix]
            used.add(owner.prefix)
        elif whole:
            name = whole[0]
        else:
            raise ValueError("retained.unit.unowned-output-payload")
        result.append(
            RetainedUnitOwnership(path, payload[path], definitions[name].identity)
        )
    if used != set(by_prefix):
        raise ValueError("retained.unit.unused-output-prefix")
    return tuple(result)
