"""Opt-in acquired Hex, retained runtime and source/resolved BOM qualification.

These native fixtures include Standard ports; catalog selection and provider
generation require separate qualification.
The explicit opt-in authorizes network acquisition and generated host execution.
An explicitly staged, loadable Hex plugin is mandatory; no ambient installation
or tool download occurs during discovery.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.builders import (
    BuildError,
    GuardedMixBuilder,
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
    CycloneDxLifecycleResolver,
    HostDependencyObservation,
    build_cyclonedx_bom,
    validate_resolved_cyclonedx_bom,
)
from literate_ai.adapters.dependencies.mix_resolution import observe_mix_artifact
from literate_ai.adapters.dependencies.mix_source import prepare_mix_source_authority
from literate_ai.contracts import (
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


class _NoHostPackages:
    def observe(self, build, *, root_ref):
        return HostDependencyObservation((), ())


@unittest.skipUnless(
    os.environ.get("LITERATE_AI_ELIXIR_MIX_QUALIFICATION") == "1",
    "set LITERATE_AI_ELIXIR_MIX_QUALIFICATION=1 for authorized native Mix proof",
)
class ElixirMixQualificationTests(unittest.TestCase):
    def test_standard_published_source_library_compiles_under_consumer_grant(self):
        self._qualify_standard_source_provider("provider_api")

    def test_standard_provider_namespace_can_extend_installed_parent(self):
        self._qualify_standard_source_provider("kernel")

    def _qualify_standard_source_provider(self, package):
        from literate_ai.adapters.lifecycle.standard_local import (
            LocalStandardLifecycleError,
        )
        from literate_ai.contracts import ComponentCommandPhase
        from literate_ai.security import AuthorizationError
        from tests.unit.test_elixir_standard_mix import _system

        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin, "native Mix qualification requires LITAI_HEX_EBIN")
        tool = discover_hex_toolchain(
            discover_mix_toolchain(discover_elixir_toolchain()), ebin=Path(plugin)
        )
        with tempfile.TemporaryDirectory(prefix="mix-source-provider-") as directory:
            root = Path(directory).resolve()
            ports, plan, intent, authorization, source = _system(
                root,
                tool=tool,
                verifier=AuthorizationRevocationSet(),
                library=True,
                library_package=package,
            )
            providers = tuple(
                ports._exports_by_identity[item.uri]
                for item in plan.provider_artifact_identities
            )
            self.assertEqual(len(ports.library_consumer_bindings(intent)), 1)
            with self.assertRaises(LocalStandardLifecycleError):
                ports.build(plan, ())
            # The compiler must consume published bytes, not the provider's
            # original generation workspace or its historical build grant.
            shutil.rmtree(root / "provider-source")
            output = ports.build(plan, providers)
            self.assertEqual(ports.build(plan, providers), output)
            artifact = ports._artifact_paths[output.exports[0].identity.uri]
            self.assertTrue(
                list((artifact / ".literate/mix-providers").rglob("compile.stdout"))
            )
            contract = ports._contract(plan.component_revision)
            provider_root = ports._artifact_paths[providers[0].identity.uri]
            original_module = (
                ports._mix_provider_sources[intent.identity.uri][0].source_tree
                / "math.ex"
            )
            original = original_module.read_bytes()
            original_module.write_bytes(original + b"# drift\n")
            with self.assertRaises(LocalStandardLifecycleError):
                ports.build(plan, providers)
            original_module.write_bytes(original)
            shutil.rmtree(source)
            shutil.rmtree(provider_root)
            argv = ports._locked_argv(
                contract,
                ComponentCommandPhase.EXECUTE,
                source_root=source,
                object_root=root / "objects",
                artifact_root=artifact,
                export_path=artifact / "app",
                providers=(),
            )
            result = subprocess.run(argv, cwd=artifact, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), 42)
            beam = next((artifact / "app/providers").rglob("*.beam"))
            old = beam.read_bytes()
            beam.write_bytes(old + b"tamper")
            with self.assertRaises(LocalStandardLifecycleError):
                ports._locked_argv(
                    contract,
                    ComponentCommandPhase.EXECUTE,
                    source_root=source,
                    object_root=root / "objects",
                    artifact_root=artifact,
                    export_path=artifact / "app",
                    providers=(),
                )
            beam.write_bytes(old)
            ports.mix_authorization_verifier = AuthorizationRevocationSet().revoke(
                authorization.grant.authorization_id,
                actor="qualification",
                reason="consumer grant revoked",
            )
            with self.assertRaises(AuthorizationError):
                ports._locked_argv(
                    contract,
                    ComponentCommandPhase.EXECUTE,
                    source_root=source,
                    object_root=root / "objects",
                    artifact_root=artifact,
                    export_path=artifact / "app",
                    providers=(),
                )

    def test_compiled_provider_authority_interface_and_retired_runtime(self):
        from tests.unit.test_elixir_mix_providers import _surface

        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin, "native Mix qualification requires LITAI_HEX_EBIN")
        tool = discover_hex_toolchain(
            discover_mix_toolchain(discover_elixir_toolchain()), ebin=Path(plugin)
        )
        with tempfile.TemporaryDirectory(prefix="mix-provider-qual-") as directory:
            root = Path(directory).resolve()
            provider_source = root / "provider.ex"
            provider_source.write_bytes(
                b"defmodule ProviderApi.Math do\n  def add(a, b), do: a + b\nend\n"
            )
            ebin = root / "provider-ebin"
            ebin.mkdir()
            compiled = subprocess.run(
                (
                    *tool.mix.elixir.command,
                    "-e",
                    "[source, dest] = System.argv(); "
                    "{:ok, _, []} = "
                    "Kernel.ParallelCompiler.compile_to_path([source], dest)",
                    "--",
                    str(provider_source),
                    str(ebin),
                ),
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            provider = MixProviderLibrary(
                canonical_identity("native-qualified-provider"),
                _surface(),
                ebin,
                canonical_tree_digest(ebin),
            )
            source = root / "generated"
            (source / "source/.literate").mkdir(parents=True)
            revision = canonical_identity("native-provider-consumer")
            ref = component_bom_ref(revision)
            graph = CycloneDxManagedGraph(
                ref,
                (
                    CycloneDxManagedComponent(
                        ref,
                        ManagedComponentKind.ROOT,
                        revision,
                        "urn:literate-ai:component:fixture/provider-consumer",
                        "1.0.0",
                        (),
                    ),
                ),
                (),
                canonical_identity("native-provider-composition"),
            )
            bom, _ = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE, managed_graph=graph
            )
            project = MixProjectIntent(
                "litai_provider_fixture",
                "1.0.0",
                "Compiled provider fixture",
                ("MIT",),
                links=(("Source", "https://example.invalid/provider-fixture"),),
            )
            files = {
                "source/mix-project.json": json.dumps(project.to_dict()).encode(),
                "source/.literate/sbom.cdx.json": bom,
                "source/main.exs": b"IO.puts(ProviderApi.Math.add(20, 22))\n",
                "source/consumer.ex": b"defmodule MixProviderConsumer do\n"
                b"  def sum(a, b), do: ProviderApi.Math.add(a, b)\nend\n",
            }
            for name, content in files.items():
                (source / name).write_bytes(content)
            digest = canonical_tree_digest(source)
            now = datetime.now(UTC)
            policy = SecurityPolicy(
                policy_digest=canonical_identity("native-provider-policy").uri
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

            def authorized_build(selected, *, bind=True):
                request = BuildRequest(
                    effective_revision_digest=revision.uri,
                    source_bundle_digest=digest,
                    builder_id=GuardedMixBuilder.provider_bound_builder_id(
                        [selected.to_dict()] if bind else []
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
                    reason="Explicit native compiled-provider qualification opt-in",
                    issued_at=now,
                    expires_at=now + timedelta(minutes=30),
                    yolo_acknowledged=True,
                )
                return GuardedMixBuilder(tool, AuthorizationRevocationSet()).build(
                    request,
                    grant,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=now,
                    provider_libraries=(selected,),
                )

            with self.assertRaises(BuildError) as mismatch:
                authorized_build(provider, bind=False)
            self.assertEqual(mismatch.exception.code, "builder.mix_authority_mismatch")
            escaped_source = root / "escaped.ex"
            escaped_source.write_bytes(
                b"defmodule OutsideProviderNamespace do\n  def value, do: 1\nend\n"
            )
            escaped = subprocess.run(
                (
                    *tool.mix.elixir.command,
                    "-e",
                    "[source, dest] = System.argv(); "
                    "{:ok, _, []} = "
                    "Kernel.ParallelCompiler.compile_to_path([source], dest)",
                    "--",
                    str(escaped_source),
                    str(ebin),
                ),
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(escaped.returncode, 0, escaped.stderr)
            with self.assertRaises(BuildError) as escaped_module:
                authorized_build(
                    replace(provider, tree_digest=canonical_tree_digest(ebin))
                )
            self.assertEqual(
                escaped_module.exception.code, "builder.mix_provider_invalid"
            )
            (ebin / "Elixir.OutsideProviderNamespace.beam").unlink()
            escaped_source.unlink()
            with self.assertRaises(BuildError) as missing:
                authorized_build(
                    replace(provider, import_surface=_surface(("missing",)))
                )
            self.assertIn("missing provider export", str(missing.exception))
            self.assertFalse(list((root / "artifacts").iterdir()))
            artifact = authorized_build(provider)
            document = verify_mix_artifact(artifact, toolchain=tool)
            self.assertEqual(document["provider_libraries"], [provider.to_dict()])
            shutil.rmtree(source)
            shutil.rmtree(ebin)
            provider_source.unlink()
            result = subprocess.run(
                artifact.runtime_command,
                cwd=artifact.artifact_path,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), b"42")
            verify_mix_artifact(artifact, toolchain=tool)
            retained = next((artifact.artifact_path / "providers").rglob("*.beam"))
            retained.write_bytes(retained.read_bytes() + b"changed")
            with self.assertRaises(BuildError):
                verify_mix_artifact(artifact, toolchain=tool)

    def test_locked_profile_projects_native_standard_runtime(self):
        import sys
        from types import SimpleNamespace

        from literate_ai.adapters.lifecycle import LocalSourceTreeRegistry
        from literate_ai.adapters.lifecycle.standard_mix import (
            StandardMixLifecyclePorts,
        )
        from literate_ai.adapters.standard_project import (
            assemble_filesystem_standard_project_runtime,
            project_locked_standard_toolchain_closure,
        )
        from literate_ai.contracts import ComponentCommandPhase
        from tests.unit.standard_source_evidence_fixture import register_strict_source
        from tests.unit.test_elixir_mix_projection import _mix_snapshot

        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin, "native Mix qualification requires LITAI_HEX_EBIN")
        with tempfile.TemporaryDirectory(prefix="mix-profile-qual-") as directory:
            root = Path(directory).resolve()
            platform = {"darwin": "macos", "win32": "windows"}.get(
                sys.platform, "linux"
            )
            # Use reviewed generation contracts rather than the projection-only
            # fixture's inert placeholder skill.
            _, snapshot, execution = _mix_snapshot(
                root, platform=platform, generation_ready=True
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform=platform,
                environment={**os.environ, "LITAI_HEX_EBIN": plugin},
            )
            registry = LocalSourceTreeRegistry()
            runtime = assemble_filesystem_standard_project_runtime(
                generator=SimpleNamespace(),
                object_root=root / "objects",
                toolchain_closure=closure,
                source_trees=registry,
            )
            self.assertIsInstance(runtime.lifecycle_ports, StandardMixLifecyclePorts)
            source = root / "generated"
            (source / "source").mkdir(parents=True)
            project = MixProjectIntent(
                "litai_mix_fixture",
                "1.0.0",
                "Native locked Mix fixture",
                ("MIT",),
                links=(("Source", "https://example.invalid/fixture"),),
            )
            (source / "source/mix-project.json").write_text(
                json.dumps(project.to_dict()), encoding="utf-8"
            )
            (source / "source/math.ex").write_text(
                "defmodule MixQualificationMath do\n  def sum(a,b), do: a+b\nend\n",
                encoding="utf-8",
            )
            (source / "source/main.exs").write_text(
                "case System.argv() do\n"
                ' ["--litai-smoke"] -> '
                "IO.puts(JSON.encode!(MixQualificationMath.sum(20,22)))\n"
                " [input] -> [a,b] = JSON.decode!(input); "
                "IO.puts(JSON.encode!(MixQualificationMath.sum(a,b)))\n"
                "end\n",
                encoding="utf-8",
            )
            generation = execution.generation_plans[0]
            candidate = register_strict_source(
                registry,
                source,
                snapshot=snapshot,
                generation_plan=generation,
                identity_namespace="native-mix-profile",
            )
            ports = runtime.lifecycle_ports
            intent = ports.create(execution, generation, candidate, (), ())
            index = ports.index(candidate.component_revision, candidate.tree_identity)
            plan = ports.finalize(intent, ports.authorize(intent, index))
            output = ports.build(plan, ())
            artifact = ports._artifact_paths[output.exports[0].identity.uri]
            contract = closure.contracts[0]
            shutil.rmtree(source)
            argv = ports._locked_argv(
                contract,
                ComponentCommandPhase.EXECUTE,
                source_root=source,
                object_root=root / "objects",
                artifact_root=artifact,
                export_path=artifact / contract.artifact_export.export_id,
                providers=(),
            )
            result = subprocess.run(argv, cwd=artifact, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), 42)

    def test_standard_plan_cache_runtime_and_current_grant_custody(self):
        from literate_ai.adapters.lifecycle.standard_local import (
            LocalStandardLifecycleError,
        )
        from literate_ai.contracts import ComponentCommandPhase
        from literate_ai.security import AuthorizationError
        from tests.unit.test_elixir_standard_mix import _system

        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin, "native Mix qualification requires LITAI_HEX_EBIN")
        tool = discover_hex_toolchain(
            discover_mix_toolchain(discover_elixir_toolchain()), ebin=Path(plugin)
        )
        with tempfile.TemporaryDirectory(prefix="mix-std-qual-") as directory:
            root = Path(directory).resolve()
            ports, plan, _, authorization, source = _system(
                root,
                tool=tool,
                verifier=AuthorizationRevocationSet(),
                dependencies=True,
            )
            output = ports.build(plan, ())
            self.assertEqual(ports.build(plan, ()), output)
            self.assertEqual(ports.build_cache_hits, 1)
            artifact = ports._artifact_paths[output.exports[0].identity.uri]
            contract = ports._contract(plan.component_revision)
            args = dict(
                source_root=source,
                object_root=root / "objects",
                artifact_root=artifact,
                export_path=artifact / "app",
                providers=(),
            )
            observer = ports._mix_observers[str(artifact.resolve())]
            beam = next((artifact / "app/runtime/decimal/ebin").glob("*.beam"))
            original = beam.read_bytes()
            beam.write_bytes(original + b"changed")
            with self.assertRaises(BuildError):
                observer()
            with self.assertRaises(LocalStandardLifecycleError):
                ports.build(plan, ())
            beam.write_bytes(original)
            shutil.rmtree(source)
            for values, expected in (
                (["0.10", "0.20"], "0.30"),
                (["-9.25", "4.25"], "-5.00"),
                (["0", "0"], "0"),
            ):
                argv = ports._locked_argv(
                    contract, ComponentCommandPhase.EXECUTE, **args
                )
                result = subprocess.run(
                    (*argv[:-1], json.dumps(values)),
                    cwd=artifact,
                    capture_output=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)
            ports.mix_authorization_verifier = AuthorizationRevocationSet().revoke(
                authorization.grant.authorization_id,
                actor="qualification",
                reason="current grant revoked",
            )
            with self.assertRaises(AuthorizationError):
                ports._locked_argv(contract, ComponentCommandPhase.EXECUTE, **args)

    def test_acquired_graph_resolves_and_survives_workspace_retirement(self):
        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin, "native Mix qualification requires LITAI_HEX_EBIN")
        hex_tool = discover_hex_toolchain(
            discover_mix_toolchain(discover_elixir_toolchain()), ebin=Path(plugin)
        )
        with tempfile.TemporaryDirectory(prefix="mix-qual-") as directory:
            root = Path(directory).resolve()
            source = root / "generated"
            package = source / "source"
            (package / ".literate").mkdir(parents=True)
            revision = canonical_identity({"fixture": "native-mix-qualification"})
            ref = component_bom_ref(revision)
            graph = CycloneDxManagedGraph(
                ref,
                (
                    CycloneDxManagedComponent(
                        ref,
                        ManagedComponentKind.ROOT,
                        revision,
                        "urn:literate-ai:component:fixture/mix",
                        "1.0.0",
                        (),
                    ),
                ),
                (),
                canonical_identity({"composition": "native-mix-qualification"}),
            )
            components = tuple(
                {
                    "type": "library",
                    "bom-ref": "hex-" + name,
                    "name": name,
                    "purl": "pkg:hex/" + name,
                    "isExternal": True,
                    "versionRange": f"vers:hex/>={version}|<{int(version[0]) + 1}.0.0",
                    "properties": [
                        {"name": "literate-ai:dependency-kind", "value": "package"},
                        {"name": "literate-ai:dependency-scope", "value": "runtime"},
                    ],
                }
                for name, version in (("decimal", "2.3.0"), ("jason", "1.4.4"))
            )
            bom, _ = build_cyclonedx_bom(
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=graph,
                additional_components=components,
                additional_edges=(
                    (ref, "hex-decimal"),
                    (ref, "hex-jason"),
                    ("hex-jason", "hex-decimal"),
                ),
            )
            project = MixProjectIntent(
                "litai_mix_fixture",
                "1.0.0",
                "Native Mix qualification",
                ("MIT",),
                (
                    MixDependencyIntent("decimal", "== 2.3.0"),
                    MixDependencyIntent("jason", "== 1.4.4"),
                ),
                links=(("Source", "https://example.invalid/native-mix-fixture"),),
            )
            files = {
                "source/mix-project.json": json.dumps(project.to_dict()),
                "source/.literate/sbom.cdx.json": bom.decode(),
                "source/main.exs": "[a, b] = Jason.decode!(hd(System.argv()))\n"
                "IO.puts(Jason.encode!(Decimal.to_string(Decimal.add(a, b))))\n",
            }
            for name, content in files.items():
                (source / name).write_bytes(content.encode("utf-8"))
            authority = prepare_mix_source_authority(files)
            now = datetime.now(UTC)
            digest = canonical_tree_digest(source)
            policy = SecurityPolicy(
                policy_digest=canonical_identity({"fixture": "native-mix-policy"}).uri
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
                builder_id=GuardedMixBuilder.builder_id,
                toolchain_digest=hex_tool.identity,
                sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
                requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
                allowed_outputs=("elixir-mix-tree",),
            )
            grant = policy.authorize_build(
                classification,
                request,
                actor="qualification",
                reason="Explicit native Mix qualification opt-in",
                issued_at=now,
                expires_at=now + timedelta(minutes=30),
                yolo_acknowledged=True,
            )
            artifact = GuardedMixBuilder(hex_tool, AuthorizationRevocationSet()).build(
                request,
                grant,
                source_root=source,
                artifact_store=root / "artifacts",
                now=now,
            )
            calls = []

            def observe():
                calls.append(True)
                return observe_mix_artifact(authority, artifact, toolchain=hex_tool)

            resolver = CycloneDxLifecycleResolver(
                managed_graph=graph,
                observer=_NoHostPackages(),
                evidence_path=root / "resolved.json",
                mix_source_authority=authority,
                mix_dependency_observer=observe,
            )
            source_record = {
                "files": files,
                "effective_revision_digest": revision.uri,
                "source_bundle_digest": digest,
            }
            build_record = {
                "source_bundle_digest": digest,
                "artifact_digest": artifact.artifact_digest,
            }
            resolved = resolver.resolve(source_record, build_record)
            self.assertEqual(resolver.resolve(source_record, build_record), resolved)
            self.assertEqual(len(calls), 2)
            validate_resolved_cyclonedx_bom(
                resolver.evidence_path.read_bytes(),
                source_content=bom,
                source_managed_graph=graph,
            )
            relocated_root = root / "relocated"
            shutil.copytree(artifact.artifact_path, relocated_root)
            relocated = replace(artifact, artifact_path=relocated_root)
            observe_mix_artifact(authority, relocated, toolchain=hex_tool)
            beam = next((relocated_root / "runtime/decimal/ebin").glob("*.beam"))
            beam.write_bytes(beam.read_bytes() + b"changed")
            with self.assertRaises(BuildError):
                observe_mix_artifact(authority, relocated, toolchain=hex_tool)
            shutil.rmtree(source)
            self.assertFalse(list((root / "artifacts").glob("mix-build-*")))
            for values, expected in (
                (["0.10", "0.20"], "0.30"),
                (["-9.25", "4.25"], "-5.00"),
                (["0", "0"], "0"),
            ):
                result = subprocess.run(
                    (*artifact.runtime_command, json.dumps(values)),
                    cwd=artifact.artifact_path,
                    capture_output=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)


if __name__ == "__main__":
    unittest.main()
