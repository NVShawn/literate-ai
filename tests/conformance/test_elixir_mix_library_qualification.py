"""Opt-in production of a native Mix library with its own Hex closure."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.builders import (
    BuildError,
    GuardedMixBuilder,
    MixProviderApplication,
    MixProviderLibrary,
    discover_elixir_toolchain,
    discover_hex_toolchain,
    discover_mix_toolchain,
    verify_mix_artifact,
)
from literate_ai.adapters.builders.python import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    canonical_tree_digest,
)
from literate_ai.adapters.dependencies import (
    build_cyclonedx_bom,
    validate_cyclonedx_bom,
)
from literate_ai.contracts import (
    ContentIdentity,
    CycloneDxLifecycle,
    CycloneDxManagedComponent,
    CycloneDxManagedGraph,
    ManagedComponentKind,
    canonical_identity,
    component_bom_ref,
)
from literate_ai.contracts.mix_projects import MixDependencyIntent, MixProjectIntent
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
)
from tests.unit.test_elixir_mix_providers import _surface


def _source(root, *, library, name=None):
    name = name or ("provider_api" if library else "library_consumer")
    revision = canonical_identity({"native-mix-library": name})
    ref = component_bom_ref(revision)
    graph = CycloneDxManagedGraph(
        ref,
        (
            CycloneDxManagedComponent(
                ref,
                ManagedComponentKind.ROOT,
                revision,
                "urn:literate-ai:component:fixture/" + name,
                "1.0.0",
                (),
            ),
        ),
        (),
        canonical_identity({"composition": name}),
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
        if library
        else ()
    )
    bom, _ = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=graph,
        additional_components=components,
        additional_edges=((ref, "hex-decimal"),) if library else (),
    )
    project = MixProjectIntent(
        name,
        "1.0.0",
        "Native library closure qualification",
        ("MIT",),
        (MixDependencyIntent("decimal", "== 2.3.0"),) if library else (),
        links=(("Source", "https://example.invalid/native-library"),),
    )
    files = {
        "source/mix-project.json": json.dumps(project.to_dict()).encode(),
        "source/.literate/sbom.cdx.json": bom,
    }
    if library:
        files["source/provider_api/math.ex"] = (
            b"defmodule ProviderApi.Math do\n"
            b" def add(a,b), do: Decimal.to_integer(Decimal.add(a,b))\nend\n"
        )
    else:
        files["source/consumer.ex"] = (
            b"defmodule LibraryConsumer do\n"
            b" @answer ProviderApi.Math.add(20,22)\n def answer, do: @answer\nend\n"
        )
        files["source/main.exs"] = b"IO.puts(LibraryConsumer.answer())\n"
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return revision


def _build(
    tool, source, destination, revision, *, surface=None, providers=(), bind=True
):
    now = datetime.now(UTC)
    digest = canonical_tree_digest(source)
    policy = SecurityPolicy(
        policy_digest=canonical_identity("native-library-policy").uri
    )
    classification = policy.classify(
        effective_revision_digest=revision.uri,
        attestations=(
            OriginAttestation(
                source_digest=digest,
                signer="qualification",
                trust_root="qualification",
                signature_identity="qualification",
                verified=True,
            ),
        ),
        findings=(),
    )
    request = BuildRequest(
        effective_revision_digest=revision.uri,
        source_bundle_digest=digest,
        builder_id=GuardedMixBuilder.provider_bound_builder_id(
            [item.to_dict() for item in providers] if bind else [],
            library_import_surface=surface.to_dict() if surface and bind else None,
        ),
        toolchain_digest=tool.identity,
        sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
        requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
        allowed_outputs=("elixir-mix-tree",),
    )
    grant = policy.authorize_build(
        classification,
        request,
        actor="qualification",
        reason="Explicit native Mix library qualification opt-in",
        issued_at=now,
        expires_at=now + timedelta(minutes=30),
        yolo_acknowledged=True,
    )
    return GuardedMixBuilder(tool, AuthorizationRevocationSet()).build(
        request,
        grant,
        source_root=source,
        artifact_store=destination,
        now=now,
        provider_libraries=providers,
        library_import_surface=surface,
    )


@unittest.skipUnless(
    os.environ.get("LITERATE_AI_ELIXIR_MIX_QUALIFICATION") == "1",
    "set LITERATE_AI_ELIXIR_MIX_QUALIFICATION=1 for authorized native Mix proof",
)
class ElixirMixLibraryQualificationTests(unittest.TestCase):
    def test_standard_mix_library_uses_current_consumer_grant_and_retained_hex(self):
        from literate_ai.adapters.lifecycle.standard_local import (
            LocalStandardLifecycleError,
        )
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceReader,
            QualificationEvidenceRecorder,
        )
        from literate_ai.application.artifact_graph import (
            create_artifact_build_graph,
            realize_manifest,
        )
        from literate_ai.contracts import ComponentCommandPhase
        from literate_ai.security import AuthorizationError
        from tests.unit.standard_source_evidence_fixture import register_strict_source
        from tests.unit.test_component_node_generation_preparation import _fixture
        from tests.unit.test_elixir_standard_mix import _system

        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin)
        tool = discover_hex_toolchain(
            discover_mix_toolchain(discover_elixir_toolchain()), ebin=Path(plugin)
        )
        with tempfile.TemporaryDirectory(prefix="mix-std-library-") as directory:
            root = Path(directory).resolve()
            verifier = AuthorizationRevocationSet()
            recorder = QualificationEvidenceRecorder(
                max_bytes=32 * 1024 * 1024, max_records=1000
            )
            ports, plan, intent, grant, source = _system(
                root,
                tool=tool,
                verifier=verifier,
                library=True,
                mix_library=True,
                evidence_recorder=recorder,
                package_root=True,
            )
            providers = tuple(
                ports._exports_by_identity[item.uri]
                for item in plan.provider_artifact_identities
            )
            provider_contract = ports._contract(providers[0].component_revision)
            provider_root = ports._artifact_paths[providers[0].identity.uri]
            command = ports._locked_argv(
                provider_contract,
                ComponentCommandPhase.EXECUTE,
                source_root=root / "provider-source",
                object_root=root / "objects",
                artifact_root=provider_root,
                export_path=provider_root / provider_contract.artifact_export.export_id,
                providers=(),
            )
            imported = subprocess.run(command, capture_output=True, timeout=60)
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(
                json.loads(imported.stdout)["capabilities"], ["storage-api"]
            )
            producer_plan = ports._plans_by_revision[
                providers[0].component_revision.uri
            ]
            producer_grant = ports._mix_plan_authorities[producer_plan.identity.uri][1]
            snapshot, execution = _fixture(
                library_names=frozenset({"storage"}), root_name="service"
            )
            producer_generation = execution.generation_plans[0]
            self.assertEqual(
                producer_generation.component_revision, producer_plan.component_revision
            )
            # Publish a different source tree under a later issued producer plan.
            # The consumer still selects the original, independently sealed export.
            rebuilt_source = root / "provider-new"
            shutil.copytree(root / "provider-source", rebuilt_source)
            module = rebuilt_source / "source/provider_api/math.ex"
            module.write_bytes(
                b"# later equivalent source publication\n" + module.read_bytes()
            )
            old_custody = ports.source_trees.evidence(
                producer_plan.request.source_tree_identity
            )
            candidate = register_strict_source(
                ports.source_trees,
                rebuilt_source,
                snapshot=snapshot,
                generation_plan=producer_generation,
                identity_namespace="later-mix-library",
                additional_components=tuple(
                    item
                    for item in json.loads(old_custody.source_bom_content)["components"]
                    if item.get("purl") == "pkg:hex/decimal"
                ),
                root_dependency_refs=("hex-decimal",),
            )
            later_intent = ports.create(
                execution, producer_generation, candidate, (), ()
            )
            later_index = ports.index(
                candidate.component_revision, candidate.tree_identity
            )
            later_plan = ports.finalize(
                later_intent, ports.authorize(later_intent, later_index)
            )
            later_export = ports.build(later_plan, ()).exports[0]
            self.assertNotEqual(later_export.identity, providers[0].identity)
            shutil.rmtree(root / "provider-source")
            ports.mix_authorization_verifier = verifier.revoke(
                producer_grant.grant.authorization_id,
                actor="qualification",
                reason="historical producer grant must not authorize consumption",
            )
            built = ports.build(plan, providers)
            self.assertEqual(ports.build(plan, providers), built)
            artifact = ports._artifact_paths[built.exports[0].identity.uri]
            graph = create_artifact_build_graph(
                build_system_driver_identity=plan.manifest.build_system_driver_identity,
                manifests=(
                    realize_manifest(producer_plan.manifest, providers),
                    realize_manifest(plan.manifest, built.exports),
                ),
                link_roots=(built.exports[0].identity,),
            )
            package_plan, package_result = ports.create_project_package(
                snapshot.authority.lock, execution, None, graph, graph.link_plans[0]
            )
            packaged = ports.project_package_custody(package_plan, package_result)
            for export in (*providers, *built.exports):
                recorder.remember_bytes(ports.read_artifact_blob(export.blob))
            captured = QualificationEvidenceReader(
                recorder.entries, max_bytes=32 * 1024 * 1024, max_records=1000
            )
            producer_evidence = next(
                value
                for value in ports._build_evidence.values()
                if providers[0].identity in value.export_identities
            )
            shutil.rmtree(source)
            shutil.rmtree(provider_root)
            consumer_contract = ports._contract(plan.component_revision)
            kwargs = dict(
                source_root=source,
                object_root=root / "objects",
                artifact_root=artifact,
                export_path=artifact / consumer_contract.artifact_export.export_id,
                providers=(),
            )
            runtime = ports._locked_argv(
                consumer_contract, ComponentCommandPhase.EXECUTE, **kwargs
            )
            executed = subprocess.run(runtime, capture_output=True, timeout=60)
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(executed.stdout.strip(), b"42")
            shutil.rmtree(artifact)
            packaged_runtime = ports._packaged_argv(
                packaged, ComponentCommandPhase.EXECUTE
            )
            executed = subprocess.run(packaged_runtime, capture_output=True, timeout=60)
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(executed.stdout.strip(), b"42")
            for value in (producer_evidence, built.evidence):
                managed_graph = CycloneDxManagedGraph.from_dict(
                    captured.read_json(value.source_sbom.managed_graph_identity)
                )
                self.assertEqual(
                    validate_cyclonedx_bom(
                        captured.read_bytes(value.source_sbom.bom_identity),
                        lifecycle=CycloneDxLifecycle.SOURCE,
                        managed_graph=managed_graph,
                    ),
                    value.source_sbom,
                )
                self.assertEqual(
                    validate_cyclonedx_bom(
                        captured.read_bytes(value.resolved_sbom.bom_identity),
                        lifecycle=CycloneDxLifecycle.RESOLVED,
                        managed_graph=managed_graph,
                        source_content=captured.read_bytes(
                            value.source_sbom.bom_identity
                        ),
                        source_managed_graph=managed_graph,
                    ),
                    value.resolved_sbom,
                )
            with zipfile.ZipFile(
                io.BytesIO(
                    captured.read_bytes(
                        ContentIdentity.parse_uri(providers[0].blob.identity)
                    )
                )
            ) as archive:
                self.assertIn(".literate/mix/decimal-2.3.0.tar", archive.namelist())
                self.assertIn("source/.literate/sbom.cdx.json", archive.namelist())
            retained_beam = next(
                packaged.artifact_paths[built.exports[0].identity.uri].rglob("*.beam")
            )
            original = retained_beam.read_bytes()
            retained_beam.write_bytes(original + b"tampered")
            with self.assertRaises(LocalStandardLifecycleError):
                ports._packaged_argv(packaged, ComponentCommandPhase.EXECUTE)
            retained_beam.write_bytes(original)
            ports.mix_authorization_verifier = ports.mix_authorization_verifier.revoke(
                grant.grant.authorization_id,
                actor="qualification",
                reason="current consumer grant revoked",
            )
            with self.assertRaises(AuthorizationError):
                ports._packaged_argv(packaged, ComponentCommandPhase.EXECUTE)

    def test_library_own_hex_closure_compiles_and_survives_source_retirement(self):
        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin, "native library qualification requires LITAI_HEX_EBIN")
        tool = discover_hex_toolchain(
            discover_mix_toolchain(discover_elixir_toolchain()),
            ebin=Path(plugin),
        )
        with tempfile.TemporaryDirectory(prefix="mix-library-") as directory:
            root = Path(directory).resolve()
            source = root / "library-source"
            revision = _source(source, library=True)
            with self.assertRaises(BuildError) as unbound:
                _build(
                    tool,
                    source,
                    root / "library-artifacts",
                    revision,
                    surface=_surface(),
                    bind=False,
                )
            self.assertEqual(unbound.exception.code, "builder.mix_authority_mismatch")
            artifact = _build(
                tool, source, root / "library-artifacts", revision, surface=_surface()
            )
            history = verify_mix_artifact(artifact, toolchain=tool)
            self.assertEqual(history["library_import_surface"], _surface().to_dict())
            self.assertEqual(
                [item["package"] for item in history["acquired_archives"]], ["decimal"]
            )
            self.assertFalse((artifact.artifact_path / "source/main.exs").exists())
            self.assertTrue(
                (artifact.artifact_path / ".literate/mix/package.tar").is_file()
            )
            applications = tuple(
                MixProviderApplication(
                    name,
                    version,
                    artifact.artifact_path / "runtime" / name / "ebin",
                    canonical_tree_digest(
                        artifact.artifact_path / "runtime" / name / "ebin"
                    ),
                )
                for name, version in (("provider_api", "1.0.0"), ("decimal", "2.3.0"))
            )
            provider = MixProviderLibrary(
                canonical_identity({"artifact": artifact.artifact_digest}),
                _surface(),
                applications[0].ebin,
                applications[0].tree_digest,
                applications,
            )
            shutil.rmtree(source)
            provider.require_unchanged()
            consumer_source = root / "consumer-source"
            consumer_revision = _source(consumer_source, library=False)
            consumer = _build(
                tool,
                consumer_source,
                root / "consumer-artifacts",
                consumer_revision,
                providers=(provider,),
            )
            verify_mix_artifact(consumer, toolchain=tool)
            shutil.rmtree(artifact.artifact_path)
            shutil.rmtree(consumer_source)
            completed = subprocess.run(
                consumer.runtime_command,
                cwd=consumer.artifact_path,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), b"42")
            verify_mix_artifact(consumer, toolchain=tool)
            dependency = next(
                (consumer.artifact_path / "providers").glob(
                    "*/applications/decimal/ebin/*.beam"
                )
            )
            dependency.write_bytes(dependency.read_bytes() + b"tampered")
            with self.assertRaises(BuildError):
                verify_mix_artifact(consumer, toolchain=tool)

    def test_transitive_shared_hex_closure_survives_producer_retirement(self):
        from literate_ai.application.library_artifacts import (
            project_library_import_surface,
        )
        from literate_ai.contracts.library_imports import AuthoredLibraryImport

        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin)
        tool = discover_hex_toolchain(
            discover_mix_toolchain(discover_elixir_toolchain()), ebin=Path(plugin)
        )
        with tempfile.TemporaryDirectory(prefix="mix-chain-") as directory:
            root = Path(directory).resolve()
            leaf_source = root / "leaf"
            leaf_revision = _source(leaf_source, library=True)
            leaf = _build(
                tool, leaf_source, root / "a", leaf_revision, surface=_surface()
            )
            leaf_apps = tuple(
                MixProviderApplication(
                    name,
                    version,
                    leaf.artifact_path / "runtime" / name / "ebin",
                    canonical_tree_digest(
                        leaf.artifact_path / "runtime" / name / "ebin"
                    ),
                )
                for name, version in (("provider_api", "1.0.0"), ("decimal", "2.3.0"))
            )
            original = MixProviderLibrary(
                canonical_identity({"artifact": leaf.artifact_digest}),
                _surface(),
                leaf_apps[0].ebin,
                leaf_apps[0].tree_digest,
                leaf_apps,
            )
            bridge_surface = project_library_import_surface(
                "bridge-api",
                "elixir",
                (("math.answer", canonical_identity("bridge-interface")),),
                declarations=(
                    AuthoredLibraryImport(
                        "elixir",
                        "bridge_api",
                        "math.answer",
                        "BridgeApi.Math",
                        ("answer",),
                    ),
                ),
            )
            bridge_source = root / "bridge"
            bridge_revision = _source(bridge_source, library=False, name="bridge_api")
            (bridge_source / "source/main.exs").unlink()
            (bridge_source / "source/consumer.ex").unlink()
            (bridge_source / "source/bridge_api").mkdir()
            (bridge_source / "source/bridge_api/math.ex").write_bytes(
                b"defmodule BridgeApi.Math do\n"
                b" @answer ProviderApi.Math.add(20,22)\n def answer, do: @answer\nend\n"
            )
            bridge = _build(
                tool,
                bridge_source,
                root / "b",
                bridge_revision,
                surface=bridge_surface,
                providers=(original,),
            )
            bridge_history = verify_mix_artifact(bridge, toolchain=tool)
            self.assertEqual(len(bridge_history["provider_libraries"]), 1)
            own_ebin = bridge.artifact_path / "runtime/bridge_api/ebin"
            own = MixProviderApplication(
                "bridge_api", "1.0.0", own_ebin, canonical_tree_digest(own_ebin)
            )
            inherited = tuple(
                MixProviderApplication(
                    app.name,
                    app.version,
                    bridge.artifact_path
                    / (
                        "providers/0/ebin"
                        if index == 0
                        else f"providers/0/applications/{app.name}/ebin"
                    ),
                    app.tree_digest,
                )
                for index, app in enumerate(leaf_apps)
            )
            indirect = MixProviderLibrary(
                canonical_identity({"artifact": bridge.artifact_digest}),
                bridge_surface,
                own.ebin,
                own.tree_digest,
                (own, *inherited),
            )
            # Direct and transitive imports select exactly the same sealed bytes.
            # Both producer identities remain distinct in the consumer request.
            source = root / "consumer"
            revision = _source(source, library=False)
            (source / "source/consumer.ex").write_bytes(
                b"defmodule LibraryConsumer do\n"
                b" @answer BridgeApi.Math.answer() + ProviderApi.Math.add(0,0)\n"
                b" def answer, do: @answer\nend\n"
            )
            consumer = _build(
                tool, source, root / "c", revision, providers=(original, indirect)
            )
            history = verify_mix_artifact(consumer, toolchain=tool)
            self.assertEqual(len(history["provider_libraries"]), 2)
            for path in (
                leaf_source,
                bridge_source,
                source,
                leaf.artifact_path,
                bridge.artifact_path,
            ):
                shutil.rmtree(path)
            completed = subprocess.run(
                consumer.runtime_command,
                cwd=consumer.artifact_path,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), b"42")
            verify_mix_artifact(consumer, toolchain=tool)
            dependency = next(
                (consumer.artifact_path / "providers/1/applications/decimal/ebin").glob(
                    "*.beam"
                )
            )
            dependency.write_bytes(dependency.read_bytes() + b"tampered")
            with self.assertRaises(BuildError):
                verify_mix_artifact(consumer, toolchain=tool)
