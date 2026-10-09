"""Exact Standard plan/grant ownership for native Mix artifact production."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.builders.hex import HexToolchain
from literate_ai.adapters.builders.mix_project import (
    GuardedMixBuilder,
    MixBuildArtifact,
    MixProviderApplication,
    MixProviderLibrary,
    _provider_directory,
    _read,
    verify_mix_artifact,
)
from literate_ai.adapters.builders.mix_provider_sources import (
    MixProviderSourceLibrary,
    _compile_mix_provider_source,
)
from literate_ai.adapters.builders.python import canonical_tree_digest
from literate_ai.adapters.dependencies import CycloneDxLifecycleResolver
from literate_ai.adapters.dependencies.mix_lock import MixLock
from literate_ai.adapters.dependencies.mix_resolution import observe_mix_artifact
from literate_ai.adapters.dependencies.mix_source import (
    MixSourceAuthority,
    prepare_mix_source_authority,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    ContentIdentity,
    canonical_identity,
)
from literate_ai.security import (
    BuildAuthorizationVerifier,
    FailClosedBuildAuthorizationVerifier,
    SecurityProfile,
)

from .standard_cargo import StandardCargoLifecyclePorts
from .standard_local import (
    LocalStandardLifecycleError,
    _directory_export_bytes,
    _RecordedHostDependencyObserver,
    local_tree_identity,
)


@dataclass(frozen=True)
class StandardMixTarget:
    component_revision: ContentIdentity
    build_system_resolver_identity: ContentIdentity
    hex_toolchain: HexToolchain

    def __post_init__(self):
        if not isinstance(self.component_revision, ContentIdentity) or not isinstance(
            self.build_system_resolver_identity, ContentIdentity
        ):
            raise TypeError(
                "Mix target requires exact revision and resolver identities"
            )
        if not isinstance(self.hex_toolchain, HexToolchain):
            raise TypeError("Mix target requires an observed Hex toolchain")

    @property
    def build_system_toolchain_identity(self):
        return ContentIdentity.parse_uri(self.hex_toolchain.identity)

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def to_dict(self):
        return {
            "schema": "literate-ai/standard-mix-target@1",
            "component_revision": self.component_revision.uri,
            "build_system_resolver_identity": self.build_system_resolver_identity.uri,
            "hex_toolchain_identity": self.hex_toolchain.identity,
            "manifest": "source/mix-project.json",
        }


@dataclass(frozen=True)
class _MixObserver:
    source: MixSourceAuthority
    artifact: MixBuildArtifact
    target: StandardMixTarget
    history_verifier: Callable[[Mapping[str, object]], None]
    authority_guard: Callable[[], None]

    def __call__(self):
        self.authority_guard()
        observed = observe_mix_artifact(
            self.source,
            self.artifact,
            toolchain=self.target.hex_toolchain,
            history_verifier=self.history_verifier,
        )
        self.authority_guard()
        return observed


class StandardMixLifecyclePorts(StandardCargoLifecyclePorts):
    """Mix target execution bound to actual Standard intent, source and grant.

    The shared native producer never fabricates a second build grant. A live
    verifier and the exact finalized Standard authorization are checked at each
    phase. The standalone producer's tree digest stays distinct from Standard's
    source bundle and source tree identities.
    """

    def __init__(
        self,
        *,
        mix_targets: tuple[StandardMixTarget, ...],
        mix_authorization_verifier: BuildAuthorizationVerifier | None = None,
        **kwargs,
    ):
        kwargs.setdefault("cargo_targets", ())
        super().__init__(**kwargs)
        if not isinstance(mix_targets, tuple) or any(
            not isinstance(target, StandardMixTarget) for target in mix_targets
        ):
            raise TypeError("Mix targets must contain typed immutable selections")
        self.mix_targets = {
            target.component_revision.uri: target for target in mix_targets
        }
        if len(self.mix_targets) != len(mix_targets) or set(self.mix_targets) & (
            set(self.python_targets) | set(self.npm_targets) | set(self.cargo_targets)
        ):
            raise ValueError("Mix targets must own unique, disjoint Components")
        self.mix_authorization_verifier = (
            mix_authorization_verifier or FailClosedBuildAuthorizationVerifier()
        )
        self._mix_intents = {}
        self._mix_grants = {}
        self._mix_plan_authorities = {}
        self._mix_finalized_plans = {}
        self._mix_observers = {}
        self._mix_execution_trees = {}
        self._mix_provider_sources = {}
        self._mix_package_observers = {}
        for revision, target in self.mix_targets.items():
            contract = self.contracts.get(revision)
            if contract is None or (
                contract.locked_build_authority_identity != target.identity
                or contract.build_system_resolver_identity
                != target.build_system_resolver_identity
                or contract.build_system_toolchain_identity.uri
                != target.hex_toolchain.identity
                or contract.language_compiler_identity.uri
                != target.hex_toolchain.mix.elixir.identity
                or contract.tool_binding(ComponentCommandPhase.BUILD).toolchain_identity
                != contract.build_system_toolchain_identity
            ):
                raise ValueError("Mix target differs from locked command authority")
            binding = self.tool_bindings[contract.build_system_toolchain_identity.uri]
            if binding.command != target.hex_toolchain.command:
                raise ValueError("Mix target differs from its bound native command")
            for phase in (ComponentCommandPhase.TEST, ComponentCommandPhase.EXECUTE):
                runtime = self.tool_bindings[
                    contract.tool_binding(phase).toolchain_identity.uri
                ]
                if contract.is_library:
                    if contract.language_runtime_identity != runtime.toolchain_identity:
                        raise ValueError(
                            "Mix library runtime identity differs from "
                            "its verification driver"
                        )
                    from literate_ai.adapters.standard_project import (
                        _STANDARD_LIBRARY_IMPORT_DRIVER,
                        _STANDARD_LIBRARY_TEST_DRIVER,
                    )

                    driver = (
                        _STANDARD_LIBRARY_TEST_DRIVER
                        if phase is ComponentCommandPhase.TEST
                        else _STANDARD_LIBRARY_IMPORT_DRIVER
                    )
                    language = (
                        "elixir"
                        if phase is ComponentCommandPhase.TEST
                        else "elixir-mix"
                    )
                    argv = contract.command(phase).argv
                    if argv[:4] != ("{tool}", "-c", driver, language) or json.loads(
                        argv[4]
                    ) != list(target.hex_toolchain.mix.elixir.command):
                        raise ValueError(
                            "Mix library verification differs from its locked driver"
                        )
                elif (
                    runtime.command != target.hex_toolchain.mix.elixir.command
                    or runtime.toolchain_identity != contract.language_compiler_identity
                ):
                    raise ValueError(
                        "Mix runtime differs from selected Elixir installation"
                    )
            if (
                not contract.is_library
                and contract.language_runtime_identity
                != contract.language_compiler_identity
            ):
                raise ValueError("Mix runtime identity differs from selected compiler")

    def _has_dependency_resolution_target(self, revision):
        return (
            revision in self.mix_targets
            or super()._has_dependency_resolution_target(revision)
        )

    def _has_network_dependency_target(self, revision):
        return revision in self.mix_targets or super()._has_network_dependency_target(
            revision
        )

    def create(
        self,
        execution_plan,
        generation_plan,
        source_candidate,
        provider_artifacts,
        package_artifacts,
    ):
        target = self.mix_targets.get(generation_plan.component_revision.uri)
        if target is None:
            return super().create(
                execution_plan,
                generation_plan,
                source_candidate,
                provider_artifacts,
                package_artifacts,
            )
        if package_artifacts or self._sdk_input_identities(
            self._contract(generation_plan.component_revision)
        ):
            raise LocalStandardLifecycleError(
                "Mix package and SDK integration is not yet configured"
            )
        root = self.source_trees.resolve(source_candidate.tree_identity)
        source = prepare_mix_source_authority(self._source_text_files(root))
        custody = self.source_trees.evidence(source_candidate.tree_identity)
        resolver = CycloneDxLifecycleResolver(
            managed_graph=custody.managed_graph,
            observer=_RecordedHostDependencyObserver(self.dependency_observation),
            evidence_path=self.object_root / "unused-mix-admission.json",
            mix_source_authority=source,
        )
        resolver.validate_source(
            {
                "files": self._source_text_files(root),
                "effective_revision_digest": generation_plan.component_revision.uri,
                "source_bundle_digest": source_candidate.source_bundle_identity.uri,
            }
        )
        old = super().create(
            execution_plan,
            generation_plan,
            source_candidate,
            provider_artifacts,
            package_artifacts,
        )
        request = replace(
            old.build_request,
            builder_id=target.identity.uri,
            toolchain_digest=target.hex_toolchain.identity,
            requested_privileges=("execute-build-tools", "network-access"),
        )
        intent = replace(old, build_request=request)
        for mapping in (
            self._intent_artifacts,
            self._intent_package_artifacts,
            self._library_consumer_bindings,
        ):
            mapping[intent.identity.uri] = mapping.pop(old.identity.uri)
        self._mix_provider_sources[intent.identity.uri] = self._select_provider_sources(
            intent, provider_artifacts
        )
        self._mix_intents[intent.identity.uri] = intent
        self._record_evidence(intent.to_dict())
        self._record_evidence(request.to_dict())
        return intent

    def _select_provider_sources(self, intent, providers):
        """Admit only registered, published Elixir library closures and exact edges."""
        consumer = self._contract(intent.component_revision)
        bindings = self.library_consumer_bindings(intent)
        for item in providers:
            if not any(
                binding.artifact_identity == item.identity for binding in bindings
            ):
                raise LocalStandardLifecycleError(
                    "Mix library lacks its exact direct interface binding"
                )
        pending = list(providers)
        closure = {}
        while pending:
            item = pending.pop()
            if item.identity.uri in closure:
                continue
            if self._exports_by_identity.get(item.identity.uri) != item:
                raise LocalStandardLifecycleError(
                    "Mix provider is not a registered published export"
                )
            contract = self._contract(item.component_revision)
            if (
                not contract.is_library
                or contract.library_import_surface.language != "elixir"
                or item.target_identity != consumer.artifact_export.target_identity
                or item.toolchain_identity != consumer.language_compiler_identity
                or item.export_id != contract.artifact_export.export_id
            ):
                raise LocalStandardLifecycleError(
                    "Mix provider differs from its locked library authority"
                )
            root = self._artifact_paths[item.identity.uri]
            require_safe_directory(root)
            checkpoint = self._artifact_checkpoints.get("sha256:" + root.name)
            if checkpoint is None or local_tree_identity(root) != checkpoint:
                raise LocalStandardLifecycleError(
                    "Mix provider lacks intact publication custody"
                )
            export = root / item.export_id
            require_safe_directory(export)
            content = _directory_export_bytes(export)
            if (
                len(content) != item.blob.size
                or hashlib.sha256(content).hexdigest() != item.blob.digest
            ):
                raise LocalStandardLifecycleError(
                    "Mix provider differs from its independent export bytes"
                )
            surface = contract.library_import_surface
            if item.component_revision.uri in self.mix_targets:
                target = self.mix_targets[item.component_revision.uri]
                history = json.loads(
                    _read(export / ".literate/mix/evidence-manifest.json")
                )
                native = MixBuildArtifact(
                    canonical_tree_digest(export),
                    export,
                    history["source_bundle_digest"],
                    history["authorization_id"],
                    target.hex_toolchain.identity,
                    (),
                    canonical_identity(history).uri,
                )
                standard_history = history.get("standard_authority")
                producer_plan = self._mix_finalized_plans.get(
                    standard_history.get("build_plan_identity")
                    if isinstance(standard_history, Mapping)
                    else None
                )
                if (
                    producer_plan is None
                    or producer_plan.component_revision != item.component_revision
                ):
                    raise LocalStandardLifecycleError(
                        "Mix provider lacks its exact issued historical plan"
                    )
                verify_mix_artifact(
                    native,
                    toolchain=target.hex_toolchain,
                    history_verifier=lambda document, producer_plan=producer_plan: (
                        self._history_verifier(
                            producer_plan, document, require_live=False
                        )
                    ),
                )
                from literate_ai.contracts.mix_projects import MixProjectIntent

                project = MixProjectIntent.from_bytes(
                    _read(export / "source/mix-project.json")
                )
                lock = MixLock.from_bytes(
                    _read(export / ".literate/mix/mix.lock"), project=project
                )
                applications = tuple(
                    MixProviderApplication(
                        name,
                        version,
                        export / "runtime" / name / "ebin",
                        canonical_tree_digest(export / "runtime" / name / "ebin"),
                    )
                    for name, version in (
                        (project.app, project.version),
                        *((pkg.name, pkg.version) for pkg in lock.packages),
                    )
                )
                selected = MixProviderLibrary(
                    item.identity,
                    surface,
                    applications[0].ebin,
                    applications[0].tree_digest,
                    applications,
                )
            else:
                source = export / "source" / surface.package
                require_safe_directory(source)
                selected = MixProviderSourceLibrary(
                    item.identity, surface, source, canonical_tree_digest(source)
                )
            selected.require_unchanged()
            if local_tree_identity(root) != checkpoint:
                raise LocalStandardLifecycleError(
                    "Mix provider changed during source admission"
                )
            closure[item.identity.uri] = (item, selected)
            for identity in item.dependency_artifact_identities:
                dependency = self._exports_by_identity.get(identity.uri)
                if dependency is None:
                    raise LocalStandardLifecycleError(
                        "Mix provider dependency lacks publication custody"
                    )
                pending.append(dependency)
        ordered = []
        while closure:
            ready = sorted(
                (
                    key
                    for key, (item, _) in closure.items()
                    if all(
                        dep.uri not in closure
                        for dep in item.dependency_artifact_identities
                    )
                )
            )
            if not ready:
                raise LocalStandardLifecycleError(
                    "Mix library dependency closure is cyclic"
                )
            for key in ready:
                ordered.append(closure.pop(key)[1])
        return tuple(ordered)

    def _require_provider_custody(self, intent, providers):
        if (
            tuple(item.identity for item in providers)
            != intent.provider_artifact_identities
        ):
            raise LocalStandardLifecycleError(
                "Mix providers differ from their issued intent"
            )
        current = self._select_provider_sources(intent, providers)
        if current != self._mix_provider_sources[intent.identity.uri]:
            raise LocalStandardLifecycleError(
                "Mix provider source or interface authority changed"
            )
        return current

    def authorize(self, intent, index):
        authorization = super().authorize(intent, index)
        if intent.component_revision.uri in self.mix_targets:
            if self._mix_intents.get(intent.identity.uri) != intent:
                raise LocalStandardLifecycleError(
                    "Mix authorization requires its issued intent"
                )
            self._mix_grants[intent.identity.uri] = authorization
        return authorization

    def finalize(self, intent, authorization):
        is_mix = intent.component_revision.uri in self.mix_targets
        if is_mix and self._mix_grants.get(intent.identity.uri) != authorization:
            raise LocalStandardLifecycleError(
                "Mix finalization requires its exact issued grant"
            )
        plan = super().finalize(intent, authorization)
        if is_mix:
            self._mix_plan_authorities[plan.identity.uri] = (intent, authorization)
            self._mix_finalized_plans[plan.identity.uri] = plan
        return plan

    def _require_mix_authority(self, plan, *, require_source=True, require_live=True):
        if self._mix_finalized_plans.get(plan.identity.uri) != plan or (
            require_live
            and self._plans_by_revision.get(plan.component_revision.uri) != plan
        ):
            raise LocalStandardLifecycleError(
                "Mix build requires its exact finalized plan"
            )
        record = self._mix_plan_authorities.get(plan.identity.uri)
        if record is None:
            raise LocalStandardLifecycleError(
                "Mix build lacks current issued authorization"
            )
        intent, authorization = record
        target = self.mix_targets[plan.component_revision.uri]
        request = intent.build_request
        if (
            plan.request.source_tree_identity != intent.source_tree_identity
            or plan.provider_artifact_identities != intent.provider_artifact_identities
            or plan.request.authorization_identity
            != authorization.authorization_identity
            or request.builder_id != target.identity.uri
            or request.toolchain_digest != target.hex_toolchain.identity
            or request.requested_privileges != ("execute-build-tools", "network-access")
            or request.sandbox_profile != "local-explicit-host-process"
            or authorization.grant.privileges != request.requested_privileges
            or authorization.grant.profile is SecurityProfile.BLOCKED
        ):
            raise LocalStandardLifecycleError(
                "Mix plan/grant differs from selected authority"
            )
        if require_source:
            self.source_trees.resolve(intent.source_tree_identity)
        if require_live:
            authorization.grant.require_valid(request, now=self.clock())
            self.mix_authorization_verifier.require_build_valid(
                authorization.grant, request, now=self.clock()
            )
        return intent, authorization

    def _history_verifier(self, plan, document, *, require_live=True):
        intent, _ = self._require_mix_authority(
            plan, require_source=False, require_live=require_live
        )
        target = self.mix_targets[plan.component_revision.uri]
        history = document.get("standard_authority")
        request = document.get("request")
        if (
            not isinstance(history, Mapping)
            or not isinstance(request, Mapping)
            or (
                history.get("schema") != "literate-ai/standard-mix-build@1"
                or (
                    not require_live
                    and history.get("build_plan_identity") != plan.identity.uri
                )
                or history.get("target_identity") != target.identity.uri
                or history.get("source_tree_identity")
                != intent.source_tree_identity.uri
                or history.get("source_bundle_identity")
                != intent.source_bundle_identity.uri
                or request.get("builder_id") != target.identity.uri
                or request.get("sandbox_profile") != "local-explicit-host-process"
                or request.get("requested_privileges")
                != ["execute-build-tools", "network-access"]
                or request.get("allowed_outputs")
                != list(intent.build_request.allowed_outputs)
            )
            or history.get("provider_source_inputs", [])
            != [
                source.to_dict()
                for source in self._mix_provider_sources[intent.identity.uri]
            ]
            or document.get("library_import_surface")
            != (
                self._contract(plan.component_revision).library_import_surface.to_dict()
                if self._contract(plan.component_revision).is_library
                else None
            )
        ):
            raise LocalStandardLifecycleError(
                "Retained Mix history differs from current target/source"
            )
        expected = self._mix_provider_sources[intent.identity.uri]
        records = document.get("provider_libraries", [])
        if (
            not isinstance(records, list)
            or [
                (record.get("artifact_identity"), record.get("import_surface"))
                for record in records
                if isinstance(record, Mapping)
            ]
            != [
                (source.artifact_identity.uri, source.import_surface.to_dict())
                for source in expected
            ]
            or len(records) != len(expected)
        ):
            raise LocalStandardLifecycleError(
                "Mix compiled providers differ from current library bindings"
            )

    def _attach_observer(self, plan, root, native=None):
        target = self.mix_targets[plan.component_revision.uri]
        export = (
            root / self._contract(plan.component_revision).artifact_export.export_id
        )
        if native is None:
            # The caller has already verified the outer artifact against its
            # independently retained publication checkpoint before deriving pins.
            document = json.loads(
                _read(export / ".literate/mix/evidence-manifest.json")
            )
            native = MixBuildArtifact(
                canonical_tree_digest(export),
                export,
                document["source_bundle_digest"],
                document["authorization_id"],
                target.hex_toolchain.identity,
                (),
                canonical_identity(document).uri,
            )
        else:
            native = replace(native, artifact_path=export)
        source = prepare_mix_source_authority(
            self._source_text_files(
                self.source_trees.resolve(plan.request.source_tree_identity)
            )
        )
        observer = _MixObserver(
            source,
            native,
            target,
            lambda document: self._history_verifier(plan, document),
            lambda: self._require_mix_authority(plan, require_source=False),
        )
        observer()
        self._mix_observers[str(root.resolve())] = observer

    def _write_resolved_sbom(self, plan, artifact_root):
        if plan.component_revision.uri not in self.mix_targets:
            return super()._write_resolved_sbom(plan, artifact_root)
        observer = self._mix_observers.get(str(artifact_root.resolve()))
        if observer is None:
            raise LocalStandardLifecycleError(
                "Mix resolution lacks independently sealed evidence"
            )
        return self._resolve_and_write_sbom(
            plan,
            artifact_root,
            artifact_digest=observer.artifact.artifact_digest,
            mix_observer=observer,
        )

    def _build_locked(self, plan, provider_artifacts, contract):
        target = self.mix_targets.get(plan.component_revision.uri)
        if target is None:
            return super()._build_locked(plan, provider_artifacts, contract)
        intent, authorization = self._require_mix_authority(plan)
        if contract.is_multi_entrypoint or len(plan.manifest.export_declarations) != 1:
            raise LocalStandardLifecycleError(
                "Mix build requires its configured single tree export"
            )
        sources = self._require_provider_custody(intent, provider_artifacts)
        declaration = plan.manifest.export_declarations[0]
        if (
            declaration.export_id != contract.artifact_export.export_id
            or declaration.producer_identity
            != contract.artifact_export.producer_identity
            or declaration.toolchain_identity != contract.language_compiler_identity
        ):
            raise LocalStandardLifecycleError(
                "Mix export differs from locked command shape"
            )
        source_root = self.source_trees.resolve(plan.request.source_tree_identity)
        providers = self._provider_materials(
            provider_artifacts, consumer_revision=plan.component_revision
        )
        cache = canonical_identity(
            {
                "schema": "literate-ai/standard-mix-cache@1",
                "target": target.identity.uri,
                "contract": contract.identity.uri,
                "source": plan.request.source_tree_identity.uri,
                "source_bundle": intent.source_bundle_identity.uri,
                "providers": providers,
            }
        )
        artifact = self.object_root / cache.digest
        started = time.monotonic()
        if artifact.exists():
            reused = self._reuse_cached_artifact(
                plan,
                provider_artifacts,
                providers,
                cache,
                artifact,
                extra_validate=lambda: self._attach_observer(plan, artifact),
            )
            if reused is not None:
                self._mix_execution_trees[str(artifact.resolve())] = (
                    self._artifact_checkpoints[cache.uri]
                )
                self.build_cache_hits += 1
                return reused
        staging = Path(tempfile.mkdtemp(prefix="mix-standard-", dir=self.object_root))
        workspace = staging / "artifact"
        workspace.mkdir()
        try:
            compiled = {}
            compile_trees = {}

            def require_build_authority():
                self._require_mix_authority(plan)
                self._require_provider_custody(intent, provider_artifacts)
                for path, expected_tree in compile_trees.items():
                    if local_tree_identity(path) != expected_tree:
                        raise LocalStandardLifecycleError(
                            "Mix provider compilation evidence changed"
                        )

            for source in sources:
                if isinstance(source, MixProviderLibrary):
                    compiled[source.artifact_identity.uri] = source
                    continue
                export = self._exports_by_identity[source.artifact_identity.uri]
                dependencies = set()
                pending = list(export.dependency_artifact_identities)
                while pending:
                    dependency = pending.pop()
                    if dependency.uri in dependencies:
                        continue
                    dependencies.add(dependency.uri)
                    pending.extend(
                        self._exports_by_identity[
                            dependency.uri
                        ].dependency_artifact_identities
                    )
                compiled[source.artifact_identity.uri] = _compile_mix_provider_source(
                    source,
                    toolchain=target.hex_toolchain,
                    workspace=workspace
                    / ".literate/mix-providers"
                    / source.artifact_identity.digest,
                    require_authority=require_build_authority,
                    dependencies=tuple(compiled[key] for key in sorted(dependencies)),
                )
                compile_root = compiled[source.artifact_identity.uri].ebin.parent
                compile_trees[compile_root] = local_tree_identity(compile_root)
            native = GuardedMixBuilder(
                target.hex_toolchain, clock=self.clock
            )._build_authorized(
                intent.build_request,
                authorization.grant,
                source_root=source_root,
                artifact_store=staging / "native",
                source_tree_digest=canonical_tree_digest(source_root),
                require_authority=require_build_authority,
                provider_libraries=tuple(compiled.values()),
                library_import_surface=contract.library_import_surface,
                standard_authority={
                    "schema": "literate-ai/standard-mix-build@1",
                    "target_identity": target.identity.uri,
                    "build_plan_identity": plan.identity.uri,
                    "source_tree_identity": intent.source_tree_identity.uri,
                    "source_bundle_identity": intent.source_bundle_identity.uri,
                    "provider_source_inputs": [source.to_dict() for source in sources],
                },
            )
            shutil.copytree(
                native.artifact_path, workspace / contract.artifact_export.export_id
            )
            self._attach_observer(plan, workspace, native)
            history = verify_mix_artifact(
                replace(
                    native, artifact_path=workspace / contract.artifact_export.export_id
                ),
                toolchain=target.hex_toolchain,
                history_verifier=lambda document: self._history_verifier(
                    plan, document
                ),
            )
            process = self._record_evidence(history)
            require_build_authority()
            self._write_artifact_manifest(plan, workspace, providers, process)
            self._install_sealed_artifact(
                cache,
                artifact,
                workspace,
                conflict="Mix cache has conflicting sealed bytes",
            )
            self._attach_observer(plan, artifact, native)
            sealed = local_tree_identity(artifact)
            output = self._build_output(plan, artifact)
            if self._publish_artifact_checkpoint(cache, plan, artifact) != sealed:
                raise LocalStandardLifecycleError(
                    "Mix artifact changed before publication"
                )
            self._mix_execution_trees[str(artifact.resolve())] = sealed
            self.build_cache_misses += 1
            self.build_seconds += time.monotonic() - started
            return output
        finally:
            shutil.rmtree(staging)

    def create_project_package(
        self,
        component_lock,
        execution_plan,
        project_build_plan,
        artifact_graph,
        link_plan,
    ):
        if component_lock.root_revision.uri not in self.mix_targets:
            return super().create_project_package(
                component_lock,
                execution_plan,
                project_build_plan,
                artifact_graph,
                link_plan,
            )
        plan = self._plans_by_revision[component_lock.root_revision.uri]
        self._require_mix_authority(plan, require_source=False)
        export = self._exports_by_identity[link_plan.root_artifact_identity.uri]
        if export.component_revision != plan.component_revision:
            raise LocalStandardLifecycleError("Mix package selected a foreign root")
        original_root = self._artifact_paths[export.identity.uri].resolve()
        template = self._mix_observers.get(str(original_root))
        expected = self._mix_execution_trees.get(str(original_root))
        if template is None or local_tree_identity(original_root) != expected:
            raise LocalStandardLifecycleError("Mix package lacks sealed build custody")
        template()
        package_plan, result = super().create_project_package(
            component_lock,
            execution_plan,
            project_build_plan,
            artifact_graph,
            link_plan,
        )
        custody = self.project_package_custody(package_plan, result)
        copied_export = custody.artifact_paths[export.identity.uri]
        observer = replace(
            template, artifact=replace(template.artifact, artifact_path=copied_export)
        )
        observer()
        self._mix_package_observers[result.identity.uri] = (
            observer,
            local_tree_identity(copied_export.parent),
        )
        return package_plan, result

    def _packaged_argv(self, custody, phase, package_entrypoint=None, **kwargs):
        if custody.root_plan.component_revision.uri in self.mix_targets:
            if (
                self.project_package_custody(
                    custody.package_plan, custody.package_result
                )
                != custody
            ):
                raise LocalStandardLifecycleError(
                    "Mix package differs from issued custody"
                )
            record = self._mix_package_observers.get(
                custody.package_result.identity.uri
            )
            if record is None:
                raise LocalStandardLifecycleError(
                    "Mix package lacks its issued observer"
                )
            observer, expected = record
            root = observer.artifact.artifact_path.parent.resolve()
            if local_tree_identity(root) != expected:
                raise LocalStandardLifecycleError("Mix packaged artifact changed")
            observer()
            self._mix_observers[str(root)] = observer
            self._mix_execution_trees[str(root)] = expected
        return super()._packaged_argv(custody, phase, package_entrypoint, **kwargs)

    def _locked_argv(self, contract, phase, **kwargs):
        argv = super()._locked_argv(contract, phase, **kwargs)
        if (
            contract.component_revision.uri not in self.mix_targets
            or phase is ComponentCommandPhase.BUILD
        ):
            return argv
        root = kwargs["artifact_root"].resolve()
        expected = self._mix_execution_trees.get(str(root))
        observer = self._mix_observers.get(str(root))
        if (
            expected is None
            or local_tree_identity(root) != expected
            or observer is None
        ):
            raise LocalStandardLifecycleError(
                "Mix execution lacks independently sealed artifact custody"
            )
        evidence = observer()
        export = root / contract.artifact_export.export_id
        plan = self._plans_by_revision[contract.component_revision.uri]
        intent, _ = self._require_mix_authority(plan, require_source=False)
        document = json.loads(_read(export / ".literate/mix/evidence-manifest.json"))
        provider_roots = [
            _provider_directory(
                export,
                source.artifact_identity,
                index,
                layout=document.get("provider_layout", "digest-v1"),
            )
            for index, source in enumerate(
                self._mix_provider_sources[intent.identity.uri]
            )
        ]
        paths = [
            *(provider_root / "ebin" for provider_root in provider_roots),
            export / "runtime" / evidence.source.project.app / "ebin",
            *(
                export / "runtime" / package.name / "ebin"
                for package in evidence.lock.packages
            ),
        ]
        for source, provider_root in zip(
            self._mix_provider_sources[intent.identity.uri], provider_roots, strict=True
        ):
            if isinstance(source, MixProviderLibrary):
                paths.extend(
                    provider_root / "applications" / app.name / "ebin"
                    for app in source.applications[1:]
                )
        prefix = self.tool_bindings[
            contract.tool_binding(phase).toolchain_identity.uri
        ].command
        if argv[: len(prefix)] != prefix:
            raise LocalStandardLifecycleError(
                "Mix runtime differs from its bound native tool"
            )
        if contract.is_library:
            target = self.mix_targets[contract.component_revision.uri]
            target.hex_toolchain.require_unchanged()
            tool_index = len(prefix) + 3
            if json.loads(argv[tool_index]) != list(
                target.hex_toolchain.mix.elixir.command
            ):
                raise LocalStandardLifecycleError(
                    "Mix library verifier selected another native tool"
                )
            argv = list(argv)
            argv[tool_index] = json.dumps(
                [
                    *target.hex_toolchain.mix.elixir.command,
                    *(token for path in paths for token in ("-pa", str(path))),
                ]
            )
            return tuple(argv)
        return (
            *prefix,
            *(token for path in paths for token in ("-pa", str(path))),
            *argv[len(prefix) :],
        )
