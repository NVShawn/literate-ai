"""Opt-in Standard Mix authority across a transitive library/package graph."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

from literate_ai.adapters.builders import (
    discover_elixir_toolchain,
    discover_hex_toolchain,
    discover_mix_toolchain,
)
from literate_ai.adapters.dependencies import validate_cyclonedx_bom
from literate_ai.adapters.directory_artifacts import (
    DirectoryExportFile,
    encode_directory_export,
    read_directory_export,
)
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
)
from literate_ai.adapters.lifecycle.standard_mix import (
    StandardMixLifecyclePorts,
    StandardMixTarget,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    verify_qualification_build,
    verify_qualification_build_authorization,
)
from literate_ai.adapters.qualification_mix import verify_qualification_mix_build
from literate_ai.adapters.standard_project import (
    _STANDARD_BUILD_DRIVER,
    _STANDARD_LIBRARY_IMPORT_DRIVER,
    _STANDARD_LIBRARY_TEST_DRIVER,
    _encoded_library_import_surface,
)
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    realize_manifest,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentLifecycleCommand,
    ContentIdentity,
    CycloneDxLifecycle,
    CycloneDxManagedGraph,
    LibraryCapabilityImport,
    LibraryImportSurface,
    canonical_identity,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.artifacts import (
    artifact_driver_composition_identity,
)
from literate_ai.contracts.mix_projects import MixDependencyIntent, MixProjectIntent
from literate_ai.security import AuthorizationError, AuthorizationRevocationSet
from tests.unit.standard_source_evidence_fixture import register_strict_source
from tests.unit.test_component_node_generation_preparation import _fixture
from tests.unit.test_elixir_standard_mix import _system


def _chain(root, tool, verifier, recorder, *, source_leaf=False):
    snapshot, execution = _fixture(library_names=frozenset({"storage", "service"}))
    pending = list(execution.generation_plans)
    ordered = []
    while pending:
        revisions = {generation.component_revision for generation in pending}
        ready = [
            generation
            for generation in pending
            if all(
                edge.provider_revision not in revisions
                for edge in generation.direct_generation_edges
            )
        ]
        if not ready:
            raise ValueError("Fixture generation graph is cyclic")
        for generation in ready:
            ordered.append(generation)
            pending.remove(generation)
    generations = tuple(ordered)
    seed, seed_plan, _, _, _ = _system(root / "seed", tool=tool)
    base = seed._contract(seed_plan.component_revision)
    python = LocalComponentToolBinding(sys.executable)
    targets = tuple(
        StandardMixTarget(
            generation.component_revision,
            canonical_identity("chain-mix-resolver"),
            tool,
        )
        for generation in generations
    )
    contracts = []
    for index, (generation, target) in enumerate(
        zip(generations, targets, strict=True)
    ):
        contract = replace(
            base,
            component_revision=generation.component_revision,
            locked_build_authority_identity=target.identity,
            build_system_resolver_identity=target.build_system_resolver_identity,
        )
        if index < 2:
            edge = generations[index + 1].direct_generation_edges[0]
            package, module = (
                ("provider_api", "ProviderApi.Math")
                if index == 0
                else ("bridge_api", "BridgeApi.Math")
            )
            surface = LibraryImportSurface(
                "elixir",
                package,
                (
                    LibraryCapabilityImport(
                        edge.capability,
                        edge.public_interface_identity,
                        module,
                        ("add",),
                    ),
                ),
            )
            contract = replace(
                contract,
                library_import_surface=surface,
                language_runtime_identity=python.toolchain_identity,
                artifact_export=replace(
                    base.artifact_export,
                    export_id="library",
                    role="library",
                    media_type="application/vnd.literate-ai.elixir-library-tree",
                ),
                commands=(
                    base.command(ComponentCommandPhase.BUILD),
                    ComponentLifecycleCommand(
                        ComponentCommandPhase.TEST,
                        (
                            "{tool}",
                            "-c",
                            _STANDARD_LIBRARY_TEST_DRIVER,
                            "elixir",
                            json.dumps(tool.mix.elixir.command),
                            "{export_path}",
                            "-",
                            "-",
                            "{artifact_root}",
                        ),
                    ),
                    ComponentLifecycleCommand(
                        ComponentCommandPhase.EXECUTE,
                        (
                            "{tool}",
                            "-c",
                            _STANDARD_LIBRARY_IMPORT_DRIVER,
                            "elixir-mix",
                            json.dumps(tool.mix.elixir.command),
                            _encoded_library_import_surface(surface),
                            "{export_path}",
                            "-",
                            "{artifact_root}",
                        ),
                    ),
                ),
                tool_bindings=(
                    base.tool_binding(ComponentCommandPhase.BUILD),
                    ComponentCommandToolBinding(
                        ComponentCommandPhase.TEST, python.toolchain_identity
                    ),
                    ComponentCommandToolBinding(
                        ComponentCommandPhase.EXECUTE, python.toolchain_identity
                    ),
                ),
            )
        if source_leaf and index == 0:
            contract = replace(
                contract,
                locked_build_authority_identity=canonical_identity("source-leaf-build"),
                build_system_resolver_identity=canonical_identity(
                    "source-leaf-resolver"
                ),
                build_system_toolchain_identity=python.toolchain_identity,
                artifact_export=replace(
                    contract.artifact_export,
                    producer_identity=canonical_identity("source-leaf-producer"),
                ),
                commands=(
                    ComponentLifecycleCommand(
                        ComponentCommandPhase.BUILD,
                        (
                            "{tool}",
                            "-c",
                            _STANDARD_BUILD_DRIVER,
                            "elixir-tree",
                            json.dumps(tool.mix.elixir.command),
                            base64.urlsafe_b64encode(zlib.compress(b"[]")).decode(),
                            "{source_root}",
                            ".",
                            "{object_root}",
                            "{export_path}",
                        ),
                    ),
                    contract.command(ComponentCommandPhase.TEST),
                    replace(
                        contract.command(ComponentCommandPhase.EXECUTE),
                        argv=tuple(
                            "elixir" if argument == "elixir-mix" else argument
                            for argument in contract.command(
                                ComponentCommandPhase.EXECUTE
                            ).argv
                        ),
                    ),
                ),
                tool_bindings=tuple(
                    ComponentCommandToolBinding(phase, python.toolchain_identity)
                    for phase in ComponentCommandPhase
                ),
            )
        contracts.append(contract)
    registry = LocalSourceTreeRegistry()
    ports = StandardMixLifecyclePorts(
        source_trees=registry,
        object_root=root / "objects",
        contracts=tuple(contracts),
        tool_bindings=(*seed.tool_bindings.values(), python),
        mix_targets=targets[1:] if source_leaf else targets,
        mix_authorization_verifier=verifier,
    )
    ports.retain_evidence_with(recorder)
    sources, candidates = [], []
    hex_owner = 1 if source_leaf else 0
    for index, generation in enumerate(generations):
        source = root / str(index)
        (source / "source").mkdir(parents=True)
        app = ("provider_api", "bridge_api", "chain_consumer")[index]
        project = MixProjectIntent(
            app,
            "1.0.0",
            "Standard transitive graph qualification",
            ("MIT",),
            (MixDependencyIntent("decimal", "== 2.3.0"),) if index == hex_owner else (),
            links=(("Source", "https://example.invalid/chain"),),
        )
        if not (source_leaf and index == 0):
            (source / "source/mix-project.json").write_text(
                json.dumps(project.to_dict())
            )
        if index < 2:
            (source / "source" / app).mkdir()
            code = (
                "defmodule ProviderApi.Math do\n"
                " def add(a,b), do: Decimal.to_integer(Decimal.add(a,b))\nend\n"
                if index == 0
                else "defmodule BridgeApi.Math do\n"
                " def add(a,b), do: ProviderApi.Math.add(a,b)\nend\n"
            )
            if source_leaf:
                code = (
                    "defmodule ProviderApi.Math do\n def add(a,b), do: a+b\nend\n"
                    if index == 0
                    else "defmodule BridgeApi.Math do\n"
                    " def add(a,b), do: "
                    "Decimal.to_integer(Decimal.add(ProviderApi.Math.add(a,b),0))\nend\n"
                )
            (source / "source" / app / "math.ex").write_text(code)
        else:
            (source / "source/main.exs").write_text(
                "IO.puts(BridgeApi.Math.add(20,22))\n"
            )
        components = (
            (
                {
                    "type": "library",
                    "bom-ref": "hex-decimal",
                    "name": "decimal",
                    "purl": "pkg:hex/decimal",
                    "isExternal": True,
                    "versionRange": "vers:hex/>=2.3.0|<3.0.0",
                    "properties": [
                        {"name": "literate-ai:dependency-kind", "value": "package"},
                        {"name": "literate-ai:dependency-scope", "value": "runtime"},
                    ],
                },
            )
            if index == hex_owner
            else ()
        )
        candidates.append(
            register_strict_source(
                registry,
                source,
                snapshot=snapshot,
                generation_plan=generation,
                identity_namespace="standard-chain",
                additional_components=components,
                root_dependency_refs=("hex-decimal",) if index == hex_owner else (),
            )
        )
        sources.append(source)
    return snapshot, execution, generations, ports, tuple(sources), tuple(candidates)


@unittest.skipUnless(
    os.environ.get("LITERATE_AI_ELIXIR_MIX_QUALIFICATION") == "1",
    "set LITERATE_AI_ELIXIR_MIX_QUALIFICATION=1 for authorized native Mix proof",
)
class ElixirMixGraphQualificationTests(unittest.TestCase):
    def test_mixed_source_and_mix_graph_executes_after_producer_retirement(self):
        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin)
        tool = discover_hex_toolchain(
            discover_mix_toolchain(discover_elixir_toolchain()), ebin=Path(plugin)
        )
        with tempfile.TemporaryDirectory(prefix="mix-mixed-") as directory:
            root = Path(directory).resolve()
            recorder = QualificationEvidenceRecorder(
                max_bytes=64 * 1024 * 1024, max_records=2000
            )
            snapshot, execution, generations, ports, sources, candidates = _chain(
                root, tool, AuthorizationRevocationSet(), recorder, source_leaf=True
            )
            plans, builds, grants = [], [], []
            for generation, candidate in zip(generations, candidates, strict=True):
                providers = builds[-1].exports if builds else ()
                intent = ports.create(execution, generation, candidate, providers, ())
                indexed = ports.index(
                    candidate.component_revision, candidate.tree_identity
                )
                grant = ports.authorize(intent, indexed)
                plan = ports.finalize(intent, grant)
                builds.append(ports.build(plan, providers))
                plans.append(plan)
                grants.append(grant)
            manifests = tuple(
                realize_manifest(plan.manifest, built.exports)
                for plan, built in zip(plans, builds, strict=True)
            )
            drivers = tuple(
                sorted(
                    {manifest.build_system_driver_identity for manifest in manifests},
                    key=lambda identity: identity.uri,
                )
            )
            self.assertEqual(len(drivers), 2)
            self.assertNotEqual(
                plans[0].manifest.build_system_driver_identity,
                plans[1].manifest.build_system_driver_identity,
            )
            graph = create_artifact_build_graph(
                build_system_driver_identity=artifact_driver_composition_identity(
                    drivers
                ),
                driver_composition=drivers,
                manifests=manifests,
                link_roots=(builds[-1].exports[0].identity,),
            )
            self.assertEqual(len(graph.link_plans[0].ordered_artifact_identities), 3)
            package_plan, result = ports.create_project_package(
                snapshot.authority.lock, execution, None, graph, graph.link_plans[0]
            )
            custody = ports.project_package_custody(package_plan, result)
            for built in builds:
                for export in built.exports:
                    recorder.remember_bytes(ports.read_artifact_blob(export.blob))
            for source in sources:
                shutil.rmtree(source)
            for built in builds:
                shutil.rmtree(ports._artifact_paths[built.exports[0].identity.uri])
            completed = subprocess.run(
                ports._packaged_argv(custody, ComponentCommandPhase.EXECUTE),
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), b"42")
            captured = QualificationEvidenceReader(
                recorder.entries, max_bytes=64 * 1024 * 1024, max_records=2000
            )
            exports = {
                export.identity: export for built in builds for export in built.exports
            }
            commands = {
                contract.component_revision: contract
                for contract in ports.contracts.values()
            }
            with (
                mock.patch(
                    "subprocess.run", side_effect=AssertionError("host execution")
                ),
                mock.patch(
                    "tempfile.mkdtemp", side_effect=AssertionError("extraction")
                ),
            ):
                for plan, built, candidate, grant in zip(
                    plans, builds, candidates, grants, strict=True
                ):
                    verify_qualification_build_authorization(
                        captured,
                        candidate=candidate,
                        index_identity=grant.index_identity,
                        authorization_identity=grant.authorization_identity,
                        plan=plan,
                    )
                    verify_qualification_build(
                        captured,
                        plan=plan,
                        build=built.evidence,
                        provider_exports=exports,
                        current_commands=commands,
                    )
            for built in builds:
                evidence = built.evidence
                managed = CycloneDxManagedGraph.from_dict(
                    captured.read_json(evidence.source_sbom.managed_graph_identity)
                )
                source = captured.read_bytes(evidence.source_sbom.bom_identity)
                self.assertEqual(
                    validate_cyclonedx_bom(
                        captured.read_bytes(evidence.resolved_sbom.bom_identity),
                        lifecycle=CycloneDxLifecycle.RESOLVED,
                        managed_graph=managed,
                        source_content=source,
                        source_managed_graph=managed,
                    ),
                    evidence.resolved_sbom,
                )
            ports.mix_authorization_verifier = ports.mix_authorization_verifier.revoke(
                grants[-1].grant.authorization_id,
                actor="qualification",
                reason="revoked packaged consumer",
            )
            with self.assertRaises(AuthorizationError):
                ports._packaged_argv(custody, ComponentCommandPhase.EXECUTE)

    def test_standard_transitive_graph_packages_retained_hex_and_boms(self):
        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin)
        tool = discover_hex_toolchain(
            discover_mix_toolchain(discover_elixir_toolchain()), ebin=Path(plugin)
        )
        with tempfile.TemporaryDirectory(prefix="mix-graph-") as directory:
            root = Path(directory).resolve()
            verifier = AuthorizationRevocationSet()
            recorder = QualificationEvidenceRecorder(
                max_bytes=64 * 1024 * 1024, max_records=2000
            )
            snapshot, execution, generations, ports, sources, candidates = _chain(
                root, tool, verifier, recorder
            )
            plans, builds, grants = [], [], []
            for index, (generation, candidate) in enumerate(
                zip(generations, candidates, strict=True)
            ):
                providers = builds[-1].exports if builds else ()
                intent = ports.create(execution, generation, candidate, providers, ())
                indexed = ports.index(
                    candidate.component_revision, candidate.tree_identity
                )
                grant = ports.authorize(intent, indexed)
                plan = ports.finalize(intent, grant)
                built = ports.build(plan, providers)
                plans.append(plan)
                builds.append(built)
                grants.append(grant)
                if index < 2:
                    verifier = ports.mix_authorization_verifier.revoke(
                        grant.grant.authorization_id,
                        actor="qualification",
                        reason="retired producer grant does not authorize its consumer",
                    )
                    ports.mix_authorization_verifier = verifier
                    shutil.rmtree(sources[index])
            original_root_grant = grants[-1]
            expired_at = original_root_grant.grant.expires_at + timedelta(seconds=1)
            old_intent = ports._mix_plan_authorities[plans[-1].identity.uri][0]
            with self.assertRaises(AuthorizationError):
                original_root_grant.grant.require_valid(
                    old_intent.build_request, now=expired_at
                )
            ports.clock = lambda: expired_at
            renewed_intent = ports.create(
                execution, generations[-1], candidates[-1], builds[1].exports, ()
            )
            renewed_grant = ports.authorize(
                renewed_intent,
                ports.index(
                    candidates[-1].component_revision, candidates[-1].tree_identity
                ),
            )
            renewed_plan = ports.finalize(renewed_intent, renewed_grant)
            hits = ports.build_cache_hits
            renewed_build = ports.build(renewed_plan, builds[1].exports)
            self.assertEqual(ports.build_cache_hits, hits + 1)
            self.assertNotEqual(renewed_plan.identity, plans[-1].identity)
            plans[-1], builds[-1], grants[-1] = (
                renewed_plan,
                renewed_build,
                renewed_grant,
            )
            self.assertEqual(
                builds[1].exports[0].dependency_artifact_identities,
                (builds[0].exports[0].identity,),
            )
            consumer_intent = ports._mix_plan_authorities[plans[-1].identity.uri][0]
            selected = ports._mix_provider_sources[consumer_intent.identity.uri]
            self.assertEqual(len(selected), 2)
            graph = create_artifact_build_graph(
                build_system_driver_identity=plans[
                    -1
                ].manifest.build_system_driver_identity,
                manifests=tuple(
                    realize_manifest(plan.manifest, built.exports)
                    for plan, built in zip(plans, builds, strict=True)
                ),
                link_roots=(builds[-1].exports[0].identity,),
            )
            package_plan, result = ports.create_project_package(
                snapshot.authority.lock, execution, None, graph, graph.link_plans[0]
            )
            custody = ports.project_package_custody(package_plan, result)
            for built in builds:
                for export in built.exports:
                    recorder.remember_bytes(ports.read_artifact_blob(export.blob))
            captured = QualificationEvidenceReader(
                recorder.entries, max_bytes=64 * 1024 * 1024, max_records=2000
            )
            shutil.rmtree(sources[-1])
            for built in builds:
                shutil.rmtree(ports._artifact_paths[built.exports[0].identity.uri])
            argv = ports._packaged_argv(custody, ComponentCommandPhase.EXECUTE)
            completed = subprocess.run(argv, capture_output=True, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), b"42")
            exports = {
                export.identity: export for built in builds for export in built.exports
            }
            commands = {
                contract.component_revision: contract
                for contract in ports.contracts.values()
            }
            with (
                mock.patch(
                    "subprocess.run", side_effect=AssertionError("host execution")
                ),
                mock.patch(
                    "tempfile.mkdtemp", side_effect=AssertionError("extraction")
                ),
            ):
                for plan, built, candidate, grant in zip(
                    plans, builds, candidates, grants, strict=True
                ):
                    verify_qualification_build_authorization(
                        captured,
                        candidate=candidate,
                        index_identity=grant.index_identity,
                        authorization_identity=grant.authorization_identity,
                        plan=plan,
                    )
                    verify_qualification_build(
                        captured,
                        plan=plan,
                        build=built.evidence,
                        provider_exports=exports,
                        current_commands=commands,
                    )
                # Rehash an altered native export and file inventory. Locked
                # archive, stream and provider pins must still reject it.
                for index, mutation in (
                    (0, "archive"),
                    (2, "stream"),
                    (2, "provider"),
                    (2, "runtime"),
                    (2, "interface"),
                    (2, "phase"),
                ):
                    with self.subTest(mutation=mutation):
                        plan, built = plans[index], builds[index]
                        envelope = captured.read_json(
                            built.evidence.build_observation_identity
                        )
                        observation = captured.read_json(
                            ContentIdentity.parse_uri(
                                envelope["process_observation_identity"]
                            )
                        )
                        tree = captured.read_json(
                            ContentIdentity.parse_uri(
                                envelope["artifact_tree_identity"]
                            )
                        )
                        export = built.exports[0]
                        members = read_directory_export(
                            captured.read_bytes(
                                ContentIdentity.parse_uri(export.blob.identity)
                            ),
                            export.blob,
                            max_bytes=export.blob.size,
                            max_entries=8192,
                        )
                        payload = {member.path: member.content for member in members}
                        modes = {member.path: member.mode for member in members}
                        if mutation == "archive":
                            path = ".literate/mix/decimal-2.3.0.tar"
                            payload[path] += b"changed"
                        elif mutation == "stream":
                            payload[".literate/mix/process/compile.stdout"] += (
                                b"changed"
                            )
                        elif mutation == "provider":
                            path = next(
                                path
                                for path in payload
                                if path.startswith("providers/0/ebin/")
                                and path.endswith(".beam")
                            )
                            payload[path] += b"changed"
                        elif mutation == "runtime":
                            observation["runtime_modules"][0]["module"] = (
                                "Elixir.Foreign"
                            )
                        elif mutation == "interface":
                            observation["provider_libraries"][0]["import_surface"][
                                "capabilities"
                            ][0]["symbols"] = ["missing"]
                            observation["standard_authority"]["provider_source_inputs"][
                                0
                            ]["import_surface"]["capabilities"][0]["symbols"] = [
                                "missing"
                            ]
                        else:
                            observation["phases"][0]["returncode"] = 1
                        import hashlib

                        observation["files"] = [
                            {"path": path, "sha256": hashlib.sha256(data).hexdigest()}
                            for path, data in sorted(payload.items())
                            if path != ".literate/mix/evidence-manifest.json"
                        ]
                        payload[".literate/mix/evidence-manifest.json"] = json.dumps(
                            observation, sort_keys=True, separators=(",", ":")
                        ).encode()
                        data = encode_directory_export(
                            tuple(
                                DirectoryExportFile(path, content, modes[path])
                                for path, content in payload.items()
                            ),
                            max_bytes=64 * 1024 * 1024,
                            max_entries=8192,
                        )
                        changed = replace(
                            export,
                            blob=BlobRef(
                                hashlib.sha256(data).hexdigest(),
                                len(data),
                                media_type=export.media_type,
                            ),
                        )
                        recorder.remember_bytes(data)
                        altered = QualificationEvidenceReader(
                            recorder.entries,
                            max_bytes=64 * 1024 * 1024,
                            max_records=2000,
                        )
                        prefix = export.export_id + "/"
                        files = sorted(
                            [
                                item
                                for item in tree["files"]
                                if not item["path"].startswith(prefix)
                            ]
                            + [
                                {
                                    "path": prefix + path,
                                    "sha256": hashlib.sha256(content).hexdigest(),
                                }
                                for path, content in payload.items()
                            ],
                            key=lambda item: item["path"],
                        )
                        with self.assertRaises(QualificationCaptureError):
                            verify_qualification_mix_build(
                                altered,
                                plan=plan,
                                observation=observation,
                                files=files,
                                exports=(changed,),
                                provider_exports=exports,
                                current_commands=commands,
                            )
            for built in builds:
                evidence = built.evidence
                managed = CycloneDxManagedGraph.from_dict(
                    captured.read_json(evidence.source_sbom.managed_graph_identity)
                )
                source = captured.read_bytes(evidence.source_sbom.bom_identity)
                self.assertEqual(
                    validate_cyclonedx_bom(
                        source,
                        lifecycle=CycloneDxLifecycle.SOURCE,
                        managed_graph=managed,
                    ),
                    evidence.source_sbom,
                )
                self.assertEqual(
                    validate_cyclonedx_bom(
                        captured.read_bytes(evidence.resolved_sbom.bom_identity),
                        lifecycle=CycloneDxLifecycle.RESOLVED,
                        managed_graph=managed,
                        source_content=source,
                        source_managed_graph=managed,
                    ),
                    evidence.resolved_sbom,
                )
            ports.mix_authorization_verifier = ports.mix_authorization_verifier.revoke(
                grants[-1].grant.authorization_id,
                actor="qualification",
                reason="current packaged consumer grant is revoked",
            )
            with self.assertRaises(AuthorizationError):
                ports._packaged_argv(custody, ComponentCommandPhase.EXECUTE)
