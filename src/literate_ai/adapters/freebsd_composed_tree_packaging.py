"""Versioned retained-origin native projection over a complete selected set.

Projection and archive inspection are mechanical. Native production additionally
requires the live retained owner, current native tools and independent selected-
set verification. A partial unit is never a standalone NativeSdkTree package.
"""

from __future__ import annotations

import platform
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

from literate_ai.adapters import freebsd_packaging as native
from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.freebsd_retained_unit_parameters import (
    UNIT_PACKAGE_EVIDENCE_FIELDS,
    FreeBsdRetainedUnitParameters,
)
from literate_ai.adapters.freebsd_tree_packaging import (
    FreeBsdTreePackageAdapter,
    _VerifiedComposedArchiveFiles,
)
from literate_ai.adapters.native_tar_archives import NativeTarToolBinding
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts.composed_tree_packages import ComposedTreePackageProjection
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.native_tree_shards import MAX_TREE_MEMBERS
from literate_ai.contracts.retained_unit_graphs import RetainedUnitGraph
from literate_ai.contracts.retained_unit_packages import (
    RetainedUnitPackagePlan,
    RetainedUnitPackageResult,
    RetainedUnitPackageSetPlan,
)
from literate_ai.contracts.tree_packages import TreePackageMember
from literate_ai.storage.cas import FileSystemCAS

_EVIDENCE_ROLES = UNIT_PACKAGE_EVIDENCE_FIELDS


def composed_freebsd_tool_identity(pkg_tool, tar_tool):
    if not isinstance(pkg_tool, native.FreeBsdPkgToolBinding) or not isinstance(
        tar_tool, NativeTarToolBinding
    ):
        raise TypeError(
            "Composed native packages require exact typed pkg/libarchive tools"
        )
    return canonical_identity(
        {
            "schema": "literate-ai/freebsd-composed-tree-tools@1",
            "pkg": pkg_tool.identity.uri,
            "tar": tar_tool.identity.uri,
        }
    )


def composed_freebsd_packager_identity(tool_identity):
    if not isinstance(tool_identity, ContentIdentity):
        raise TypeError("Composed native packages require an exact tool identity")
    return canonical_identity(
        {
            "schema": "literate-ai/freebsd-composed-tree-packager@1",
            "tool_identity": tool_identity.uri,
            "format": "native-pkg-pax",
            "compression": "xz-single-frame",
            "compression_threads": 1,
            "compression_level": 9,
            "checksum": "sha256-hex",
            "source_date_epoch": native._EPOCH,
        }
    )


def composed_freebsd_policy(
    max_closure_steps,
    max_closure_memory_bytes,
    *,
    max_provider_metadata_bytes=64 * 1024**2,
    max_provider_memory_bytes=256 * 1024**2,
    max_verified_record_cache_bytes=1024**3,
):
    """Local policy is bound before the set plan, avoiding projection-ID cycles."""
    return {
        "schema": "literate-ai/freebsd-composed-tree-policy@1",
        "max_closure_steps": max_closure_steps,
        "max_closure_memory_bytes": max_closure_memory_bytes,
        "max_provider_metadata_bytes": max_provider_metadata_bytes,
        "max_provider_memory_bytes": max_provider_memory_bytes,
        "max_verified_record_cache_bytes": max_verified_record_cache_bytes,
        "native_manifest_byte_limit": native._MAX_METADATA,
        "native_unit_member_limit": native._MAX_MEMBERS,
        "xz_decoder_memory_bytes": 128 * 1024**2,
    }


@dataclass(frozen=True, slots=True)
class FreeBsdComposedPackageSetProjection:
    set_plan: RetainedUnitPackageSetPlan
    graph: RetainedUnitGraph
    parameters: tuple[FreeBsdRetainedUnitParameters, ...]
    pkg_tool: native.FreeBsdPkgToolBinding
    tar_tool: NativeTarToolBinding
    max_closure_steps: int
    max_closure_memory_bytes: int
    max_provider_metadata_bytes: int = 64 * 1024**2
    max_provider_memory_bytes: int = 256 * 1024**2
    max_verified_record_cache_bytes: int = 1024**3
    composition: ComposedTreePackageProjection = field(init=False, repr=False)
    _definitions: object = field(init=False, repr=False, compare=False)
    _parameters: object = field(init=False, repr=False, compare=False)
    _runtime_edges: object = field(init=False, repr=False, compare=False)

    SCHEMA: ClassVar[str] = "literate-ai/freebsd-composed-tree-pkg-projection@1"

    def __post_init__(self):
        if not isinstance(self.set_plan, RetainedUnitPackageSetPlan) or not isinstance(
            self.graph, RetainedUnitGraph
        ):
            raise TypeError(
                "Composed native projection requires typed set plan and graph"
            )
        tool_identity = composed_freebsd_tool_identity(self.pkg_tool, self.tar_tool)
        if (
            self.set_plan.tool_identity != tool_identity
            or self.set_plan.packager_identity
            != composed_freebsd_packager_identity(tool_identity)
        ):
            raise PackagingError(
                "Composed native projection binds another provider/tool"
            )
        if self.set_plan.provider_policy_identity != canonical_identity(
            self.provider_policy
        ):
            raise PackagingError(
                "Composed native set plan binds another provider resource policy"
            )
        if (
            self.set_plan.graph_index.identity != self.graph.identity.uri
            or self.set_plan.graph_index.size
            != len(canonical_json_bytes(self.graph.index.to_dict()))
            or self.set_plan.tree_index != self.graph.index.tree_index
        ):
            raise PackagingError("Composed native projection binds another graph/tree")
        definitions = self.graph.partition.definitions
        for value, maximum in (
            (self.max_provider_metadata_bytes, 512 * 1024**2),
            (self.max_provider_memory_bytes, 16 * 1024**3),
            (self.max_verified_record_cache_bytes, 16 * 1024**3),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise PackagingError(
                    "Composed native provider metadata policy is invalid"
                )
        provider_bytes = sum(value.provider_parameters.size for value in definitions)
        provider_memory = 16 * provider_bytes + 512 * 64 * len(definitions)
        if (
            provider_bytes > self.max_provider_metadata_bytes
            or provider_memory > self.max_provider_memory_bytes
        ):
            raise PackagingError(
                "Composed native provider metadata exceeds decode/memory policy"
            )
        first = definitions[0]
        if (
            self.set_plan.retained_plan_identity,
            self.set_plan.receipt_identity,
            self.set_plan.target_identity,
        ) != (
            first.retained_plan_identity,
            first.receipt_identity,
            first.target_identity,
        ):
            raise PackagingError(
                "Composed native projection has contradictory retained context"
            )
        if (
            type(self.parameters) is not tuple
            or len(self.parameters) != len(definitions)
            or any(
                not isinstance(value, FreeBsdRetainedUnitParameters)
                for value in self.parameters
            )
        ):
            raise TypeError(
                "Composed native projection requires one exact parameter owner per unit"
            )
        if self.set_plan.max_archive_bytes > 16 * 1024**3:
            raise PackagingError(
                "Composed native per-unit archive exceeds provider ceiling"
            )
        if any(
            path.split("/", 1)[0].casefold() == ".literate-ai"
            for path in (
                *self.graph.tree.directories,
                *(value.path for value in self.graph.tree.files),
                *(value.path for value in self.graph.tree.aliases),
            )
        ):
            raise PackagingError(
                "Composed native payload overlaps reserved evidence namespace"
            )
        names, prefixes, evidence_directories = set(), set(), set()
        evidence_count = evidence_bytes = 0
        for definition, params in zip(definitions, self.parameters, strict=True):
            content = canonical_json_bytes(params.to_dict())
            if (
                definition.provider_parameters.identity != params.identity.uri
                or definition.provider_parameters.size != len(content)
            ):
                raise PackagingError(
                    "Composed native parameters differ from exact unit definition"
                )
            if (
                native._ABI.fullmatch(definition.abi) is None
                or native._VERSION.fullmatch(definition.version) is None
                or definition.abi != self.pkg_tool.abi
                or params.architecture != self.pkg_tool.architecture
            ):
                raise PackagingError(
                    "Composed native unit contradicts selected target/tool ABI"
                )
            if params.package.casefold() in names:
                raise PackagingError(
                    "Composed native package coordinates are ambiguous"
                )
            names.add(params.package.casefold())
            prefixes.add(params.prefix)
            roles = {}
            for item in params.evidence:
                evidence_count += 1
                evidence_bytes += item.blob.size
                parent = item.path.rpartition("/")[0]
                while parent:
                    if parent not in evidence_directories:
                        provider_memory += 256 + 4 * len(parent.encode("utf-8"))
                        if provider_memory > self.max_provider_memory_bytes:
                            raise PackagingError(
                                "Native evidence directory memory budget exceeded"
                            )
                        evidence_directories.add(parent)
                    parent = parent.rpartition("/")[0]
                if (
                    self.graph.tree.index.member_count
                    + 1
                    + evidence_count
                    + len(evidence_directories)
                    > min(self.graph.tree.index.limits.max_entries, MAX_TREE_MEMBERS)
                    or self.graph.tree.index.payload_bytes + evidence_bytes
                    > self.graph.tree.index.limits.max_total_bytes
                    or item.blob.size > self.graph.tree.index.limits.max_file_bytes
                ):
                    raise PackagingError(
                        "Composed native set including evidence exceeds "
                        "aggregate custody policy"
                    )
                if (
                    not item.path.startswith(
                        ".literate-ai/units/" + definition.unit_name + "/"
                    )
                    or item.target_identity != definition.target_identity
                ):
                    raise PackagingError(
                        "Composed native evidence has foreign unit/target ownership"
                    )
                roles.setdefault(item.role, []).append(item)
                if item.role == "component-specification":
                    if item.source_identity != definition.component_authoring_identity:
                        raise PackagingError(
                            "Composed native specification has foreign "
                            "authoring provenance"
                        )
                elif item.role not in _EVIDENCE_ROLES:
                    raise PackagingError("Composed native evidence role is unsupported")
                else:
                    expected = getattr(definition, _EVIDENCE_ROLES[item.role])
                    if (
                        item.source_identity != expected
                        or item.blob.identity != expected.uri
                    ):
                        raise PackagingError(
                            "Composed native evidence differs from bound provenance"
                        )
            if set(roles) != {*_EVIDENCE_ROLES, "component-specification"} or any(
                len(roles[role]) != 1 for role in _EVIDENCE_ROLES
            ):
                raise PackagingError(
                    "Composed native unit requires complete exact "
                    "provenance and license"
                )
        if len(prefixes) != 1:
            raise PackagingError(
                "Composed native units require one exact logical/install prefix"
            )
        object.__setattr__(
            self,
            "composition",
            ComposedTreePackageProjection(
                self.graph.partition,
                self.max_closure_steps,
                self.max_closure_memory_bytes,
            ),
        )
        object.__setattr__(
            self,
            "_definitions",
            MappingProxyType({value.identity: value for value in definitions}),
        )
        object.__setattr__(
            self,
            "_parameters",
            MappingProxyType(
                {
                    value.identity: params
                    for value, params in zip(definitions, self.parameters, strict=True)
                }
            ),
        )
        edges = {value.identity: [] for value in definitions}
        for edge in self.graph.partition.dependencies:
            if edge.kind in {"runtime", "tree-link"}:
                edges[edge.source_unit_identity].append(edge.target_unit_identity)
        object.__setattr__(
            self,
            "_runtime_edges",
            MappingProxyType(
                {
                    key: tuple(sorted(set(value), key=lambda identity: identity.uri))
                    for key, value in edges.items()
                }
            ),
        )

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @property
    def provider_policy(self):
        return composed_freebsd_policy(
            self.max_closure_steps,
            self.max_closure_memory_bytes,
            max_provider_metadata_bytes=self.max_provider_metadata_bytes,
            max_provider_memory_bytes=self.max_provider_memory_bytes,
            max_verified_record_cache_bytes=self.max_verified_record_cache_bytes,
        )

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "authority_mode": "retained-project",
            "release_authorized": False,
            "set_plan": self.set_plan.to_dict(),
            # The immutable graph was rehashed against this exact reference in
            # __post_init__; avoid hashing its complete index once per unit.
            "graph_identity": self.set_plan.graph_index.identity,
            "pkg_tool_identity": self.pkg_tool.identity.uri,
            "tar_tool_identity": self.tar_tool.identity.uri,
            "provider_policy": self.provider_policy,
        }

    def unit_plan(self, identity):
        if (
            not isinstance(identity, ContentIdentity)
            or identity not in self._definitions
        ):
            raise PackagingError("Composed native projection requires a selected unit")
        return RetainedUnitPackagePlan(
            self._definitions[identity],
            self.set_plan.identity,
            self.set_plan.graph_index,
            self.set_plan.graph_index,
            self.set_plan.packager_identity,
            self.set_plan.tool_identity,
        )

    def unit_members(self, identity):
        self.unit_plan(identity)
        params = self._parameters[identity]
        members = {
            value.path: value for value in self.composition.members_for(identity)
        }
        for item in params.evidence:
            members[item.path] = TreePackageMember(item.path, "file", 0o644, item.blob)
            parent = item.path.rpartition("/")[0]
            while parent:
                members.setdefault(
                    parent, TreePackageMember(parent, "directory", 0o755)
                )
                parent = parent.rpartition("/")[0]
        members[""] = TreePackageMember("", "directory", 0o755)
        limits = self.graph.index.per_unit_limits
        sizes = tuple(
            value.blob.size
            if value.blob is not None
            else len(value.target.encode("utf-8"))
            if value.kind == "symlink"
            else 0
            for value in members.values()
        )
        if (
            len(members) > min(limits.max_entries, native._MAX_MEMBERS)
            or any(size > limits.max_file_bytes for size in sizes)
            or sum(sizes) > limits.max_total_bytes
        ):
            raise PackagingError(
                "Composed native unit including provenance exceeds its custody budget"
            )
        return {
            params.prefix + ("/" + path if path else ""): members[path]
            for path in sorted(members)
        }

    def unit_manifest(self, identity):
        plan = self.unit_plan(identity)
        definition = plan.definition
        params = self._parameters[identity]
        dependencies = {}
        for target in self._runtime_edges[identity]:
            selected = self._parameters[target]
            dependencies[selected.package] = {
                "origin": selected.origin,
                "version": self._definitions[target].version,
            }
        return {
            "name": params.package,
            "version": definition.version,
            "origin": params.origin,
            "abi": definition.abi,
            "arch": params.architecture,
            "prefix": params.prefix,
            "comment": "Retained Component package unit",
            "desc": "Retained payload with exact provenance and composed link closure.",
            "maintainer": "literate-ai-maintainers@users.noreply.github.com",
            "www": "https://github.com/jordanhubbard/literate-ai",
            "deps": dependencies,
            "annotations": {
                "literate-ai:authority-mode": "retained-project",
                "literate-ai:unit-plan": plan.identity.uri,
                "literate-ai:unit-definition": definition.identity.uri,
                "literate-ai:set-plan": self.set_plan.identity.uri,
                "literate-ai:graph": self.set_plan.graph_index.identity,
                "literate-ai:tool": self.set_plan.tool_identity.uri,
                **{
                    "literate-ai:" + field.replace("_", "-"): getattr(
                        definition, field
                    ).uri
                    for field in definition.IDENTITY_FIELDS
                },
            },
        }

    def unit_expansion_limit(self, identity):
        return self.unit_geometry(identity)[1]

    def unit_geometry(self, identity):
        """Compute canonical geometry and its expansion reservation together."""
        members = self.unit_members(identity)
        expanded = (
            sum(
                value.blob.size
                if value.blob is not None
                else len(value.target.encode("utf-8"))
                if value.kind == "symlink"
                else 0
                for value in members.values()
            )
            + 2 * native._MAX_METADATA
            + len(members) * 16384
            + 20480
        )
        return members, expanded


@dataclass(frozen=True, slots=True)
class _UnitArchiveView:
    selected: FreeBsdComposedPackageSetProjection
    unit_identity: ContentIdentity

    @property
    def members(self):
        return self.selected.unit_members(self.unit_identity)

    @property
    def manifest(self):
        return self.selected.unit_manifest(self.unit_identity)

    @property
    def prefix(self):
        return self.selected._parameters[self.unit_identity].prefix

    @property
    def abi(self):
        return self.selected.unit_plan(self.unit_identity).definition.abi

    @property
    def graph_identity(self):
        return self.selected.graph.identity

    @property
    def set_plan_identity(self):
        return self.selected.set_plan.identity


class _UnitArchiveVerifier(FreeBsdTreePackageAdapter):
    """Use the existing native entry/checksum/ELF checker on an exact unit view."""

    def __init__(self, view):
        if not isinstance(view, _UnitArchiveView):
            raise TypeError(
                "Composed native archive verifier requires an exact unit view"
            )
        self.projection = view

    def _verify_tar(
        self, plan, expected, expanded, *, composed=None, collect_records=False
    ):
        records = super()._verify_tar(
            plan, expected, expanded, composed=composed, collect_records=True
        )
        if any(
            re.fullmatch(r"[0-9a-f]{64}", record["sum"]) is None
            for record in records.values()
        ):
            raise PackagingError(
                "Composed native checksum encoding differs from provider policy"
            )
        return records if collect_records else None


def inspect_freebsd_composed_package_set(projection, results, *, store, object_root):
    """Independently inspect complete archive bytes; grant no native/live authority.

    This portable inspection deliberately cannot establish native tool reopening,
    completed production, runtime, LINK, PACKAGE, installation or release admission.
    The native front door additionally reopens every unit with the current pkg.
    """
    return _inspect_composed_archives(
        projection, results, store, object_root, native_reopen=False
    )


def verify_freebsd_composed_package_set(projection, results, *, store, object_root):
    """Require actual native reopening as well as complete independent byte checks."""
    if not isinstance(projection, FreeBsdComposedPackageSetProjection):
        raise TypeError("Composed native verification requires an exact projection")
    if platform.system().casefold() != "freebsd":
        raise PackagingError("Native composed pkg verification requires FreeBSD")
    projection.pkg_tool.require_unchanged()
    projection.tar_tool.require_current()
    value = _inspect_composed_archives(
        projection, results, store, object_root, native_reopen=True
    )
    projection.pkg_tool.require_unchanged()
    projection.tar_tool.require_current()
    return value


def _inspect_composed_archives(
    projection, results, store, object_root, *, native_reopen
):
    if not isinstance(
        projection, FreeBsdComposedPackageSetProjection
    ) or not isinstance(store, FileSystemCAS):
        raise TypeError("Composed archive inspection requires typed projection/store")
    if (
        type(results) is not tuple
        or len(results) != projection.graph.index.unit_count
        or any(not isinstance(value, RetainedUnitPackageResult) for value in results)
    ):
        raise PackagingError(
            "Composed archive inspection requires exactly one result per selected unit"
        )
    identities = tuple(value.definition_identity.uri for value in results)
    if identities != tuple(sorted(set(identities))) or set(identities) != {
        identity.uri for identity in projection._definitions
    }:
        raise PackagingError(
            "Composed archive result membership is missing, duplicate or foreign"
        )
    total = 0
    for result in results:
        plan = projection.unit_plan(result.definition_identity)
        if (
            result.unit_plan_identity,
            result.package_set_plan_identity,
            result.graph_identity,
            result.packager_identity,
            result.tool_identity,
        ) != (
            plan.identity,
            projection.set_plan.identity,
            projection.graph.identity,
            projection.set_plan.packager_identity,
            projection.set_plan.tool_identity,
        ):
            raise PackagingError(
                "Composed archive result binds another plan/graph/provider/tool"
            )
        total += result.archive.size
        if (
            result.archive.size > projection.set_plan.max_archive_bytes
            or total > projection.set_plan.max_total_archive_bytes
        ):
            raise PackagingError(
                "Composed archive results exceed declared archive budgets"
            )
        if (
            result.archive.size
            + projection.unit_expansion_limit(result.definition_identity)
            > projection.set_plan.max_staging_bytes
        ):
            raise PackagingError(
                "Composed archive inspection exceeds declared staging budget"
            )
    parent = Path(object_root).absolute()
    directory_node(parent)
    selected = {value.definition_identity: value for value in results}
    physical = _VerifiedComposedArchiveFiles(
        projection.graph.identity,
        projection.set_plan.identity,
        projection.max_verified_record_cache_bytes,
    )
    with tempfile.TemporaryDirectory(prefix="composed-pkg-inspect-", dir=parent) as raw:
        work = Path(raw)
        repos = work / "repos"
        repos.mkdir()
        for identity in projection.graph.partition.verification_order():
            result = selected[identity]
            compressed_path = work / "unit.pkg"
            store.copy_to(result.archive, compressed_path)
            try:
                with (
                    compressed_path.open("rb") as source,
                    tempfile.TemporaryFile(dir=work) as expanded,
                ):
                    native._expand_single_xz_stream(
                        source,
                        expanded,
                        result.archive.size,
                        projection.unit_expansion_limit(identity),
                    )
                    expanded.seek(0)
                    view = _UnitArchiveView(projection, identity)
                    expected = view.members
                    records = _UnitArchiveVerifier(view)._verify_tar(
                        projection.unit_plan(identity),
                        expected,
                        expanded,
                        composed=physical,
                        collect_records=True,
                    )
                    if native_reopen:
                        _reopen_native_unit(
                            projection, identity, compressed_path, work, repos
                        )
                    physical.register_verified(identity, expected, records)
            finally:
                compressed_path.unlink()
    return results


def _reopen_native_unit(projection, identity, archive_path, work, repos):
    projection.pkg_tool.require_unchanged()
    params = projection._parameters[identity]
    definition = projection.unit_plan(identity).definition
    result = run_bounded_process(
        (
            *projection.pkg_tool.command,
            "-R",
            str(repos),
            "query",
            "-F",
            str(archive_path),
            "%n\t%v\t%o\t%p\t%q\t%Q",
        ),
        cwd=work,
        environment=projection.pkg_tool.environment,
        timeout_seconds=30,
        stdout_limit_bytes=4096,
        stderr_limit_bytes=65536,
        error_prefix="freebsd.composed-pkg.reopen",
        terminate_descendants=True,
    )
    expected = "\t".join(
        (
            params.package,
            definition.version,
            params.origin,
            params.prefix,
            definition.abi,
            params.architecture,
        )
    )
    if (
        result.returncode
        or result.stdout.decode("utf-8", errors="strict").rstrip("\n") != expected
    ):
        raise PackagingError(
            "Native pkg reopening contradicts selected unit coordinates"
        )
    projection.pkg_tool.require_unchanged()
