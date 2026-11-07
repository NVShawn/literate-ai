"""Native Standard builder integration, not whole lifecycle acceptance."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.lifecycle.standard_cargo import (
    StandardCargoLifecyclePorts,
    StandardCargoTarget,
)
from literate_ai.adapters.lifecycle.standard_local import (
    LocalSourceTreeRegistry,
    local_generated_source_tree_identity,
)
from literate_ai.contracts import (
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from tests.smoke import test_standard_cargo_lifecycle as fixture
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.standard_source_evidence_fixture import register_strict_source


@unittest.skipUnless(
    shutil.which("cargo") and shutil.which("rustc"), "native Rust tools unavailable"
)
class NativeCargoStandardTests(unittest.TestCase):
    def system(self, root):
        snapshot, execution = _fixture(
            root_name="service", library_names=frozenset({"service", "storage"})
        )
        cargo, rustc = (
            fixture._native_tool_binding("cargo"),
            fixture._native_tool_binding("rustc"),
        )
        registry, contracts, targets, candidates = LocalSourceTreeRegistry(), [], [], {}
        by_revision = {
            node.revision.identity: node for node in snapshot.authority.lock.nodes
        }
        for generation in execution.generation_plans:
            revision = generation.component_revision
            alpha = revision == snapshot.authority.lock.root_revision
            name = "alpha" if alpha else "beta"
            target = StandardCargoTarget(
                revision,
                fixture._identity("resolver"),
                cargo.toolchain_identity,
                rustc.toolchain_identity,
                "source/Cargo.toml",
                name,
                rustc.command,
                library=True,
            )
            targets.append(target)
            public = by_revision[revision].interface_bindings[0]
            contracts.append(
                ComponentCommandContract(
                    component_revision=revision,
                    locked_build_authority_identity=target.identity,
                    build_system_resolver_identity=target.build_system_resolver_identity,
                    build_system_toolchain_identity=cargo.toolchain_identity,
                    language_compiler_identity=rustc.toolchain_identity,
                    language_runtime_identity=rustc.toolchain_identity,
                    commands=fixture._commands(),
                    tool_bindings=tuple(
                        ComponentCommandToolBinding(
                            phase,
                            cargo.toolchain_identity
                            if phase is ComponentCommandPhase.BUILD
                            else rustc.toolchain_identity,
                        )
                        for phase in ComponentCommandPhase
                    ),
                    artifact_export=ComponentArtifactExportShape(
                        "library",
                        "library",
                        fixture._identity("abi"),
                        fixture._identity("platform"),
                        "application/octet-stream",
                        fixture._identity("producer"),
                    ),
                    library_import_surface=LibraryImportSurface(
                        "rust",
                        name,
                        (
                            LibraryCapabilityImport(
                                public.capability,
                                public.interface_identity,
                                name,
                                ("read" if alpha else "Event",),
                            ),
                        ),
                    ),
                )
            )
            source = root / name
            (source / "source/src").mkdir(parents=True)
            (source / "source/Cargo.toml").write_text(
                '[package]\nname="' + name + '"\nversion="1.0.0"\nedition="2021"\n'
            )
            (source / "source/src/lib.rs").write_text(
                "pub fn read(event: &beta::Event) -> u32 { event.value }\n"
                if alpha
                else "pub struct Event { pub value: u32 }\n"
            )
            candidates[revision] = register_strict_source(
                registry,
                source,
                snapshot=snapshot,
                generation_plan=generation,
                identity_namespace="native-cargo-standard",
            )
        ports = StandardCargoLifecyclePorts(
            source_trees=registry,
            object_root=root / "objects",
            contracts=tuple(contracts),
            cargo_targets=tuple(targets),
            tool_bindings=(cargo, rustc),
        )
        return ports, execution, candidates

    def test_two_clean_real_builds_reuse_exact_provider_and_preserve_source_and_sbom(
        self,
    ):
        for attempt in range(2):
            with (
                self.subTest(attempt=attempt),
                tempfile.TemporaryDirectory(
                    prefix="native-cargo-standard-"
                ) as temporary,
            ):
                root = Path(temporary)
                ports, execution, candidates = self.system(root)
                built = {}
                plans = {}
                ordered = sorted(
                    execution.generation_plans,
                    key=lambda row: len(row.direct_generation_edges),
                )
                for generation in ordered:
                    candidate = candidates[generation.component_revision]
                    providers = tuple(
                        built[edge.provider_revision].exports[0]
                        for edge in generation.direct_generation_edges
                    )
                    intent = ports.create(
                        execution, generation, candidate, providers, ()
                    )
                    index = ports.index(
                        candidate.component_revision, candidate.tree_identity
                    )
                    plan = ports.finalize(intent, ports.authorize(intent, index))
                    built[generation.component_revision] = ports.build(plan, providers)
                    plans[generation.component_revision] = plan, providers
                    self.assertEqual(
                        local_generated_source_tree_identity(
                            ports.source_trees.resolve(candidate.tree_identity)
                        ),
                        candidate.tree_identity,
                    )
                final = ordered[-1].component_revision
                self.assertEqual(len(built), 2)
                alpha = ports.artifact_path(built[final].exports[0])
                self.assertTrue(
                    (alpha / ".literate/native-cargo/projection.json").is_file()
                )
                evidence = alpha.parent / ".literate/cargo/native-provider-graph.json"
                self.assertTrue(evidence.is_file())
                graph = json.loads(evidence.read_text())["graph"]
                self.assertEqual(len(graph["nodes"]), 1)
                sbom = json.loads(
                    (alpha.parent / ".literate/resolved-sbom.cdx.json").read_text()
                )
                self.assertFalse(
                    any(
                        row.get("purl", "").startswith("pkg:cargo/")
                        for row in sbom["components"]
                    )
                )
                native_record = json.loads(evidence.read_text())
                self.assertEqual(len(native_record["provider_locks"]), 1)
                before = ports.build_cache_hits
                ports.build(plans[final][0], plans[final][1])
                self.assertEqual(ports.build_cache_hits, before + 1)
                relocated = root / "relocated"
                shutil.copytree(alpha, relocated)
                provider = next(
                    (relocated / ".literate/native-cargo/providers").iterdir()
                )
                consumer = root / "independent"
                (consumer / "src").mkdir(parents=True)
                (consumer / "Cargo.toml").write_text(
                    '[package]\nname="consumer"\nversion="1.0.0"\nedition="2021"\n[dependencies]\n'
                    'alpha={path="../relocated/source"}\nbeta={path="../relocated/.literate/native-cargo/providers/'
                    + provider.name
                    + '/source"}\n'
                )
                (consumer / "src/main.rs").write_text(
                    "fn main(){let e=beta::Event{value:37};"
                    "assert_eq!(alpha::read(&e),37);}\n"
                )
                shutil.rmtree(alpha)
                result = subprocess.run(
                    [
                        shutil.which("cargo"),
                        "run",
                        "--offline",
                        "--quiet",
                        "--manifest-path",
                        str(consumer / "Cargo.toml"),
                        "--target-dir",
                        str(root / "independent-objects"),
                    ],
                    cwd=root,
                    env={**os.environ, "CARGO_INCREMENTAL": "0"},
                    capture_output=True,
                    timeout=60,
                )
                self.assertEqual(
                    result.returncode, 0, result.stderr.decode(errors="replace")
                )
