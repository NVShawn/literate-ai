"""Independent authored provider identity with actual Standard/Cargo material.

Controlled source variants are not genuine-model generation or public retained
admission. The same oracle is loaded before either provider body is registered.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders.cargo import discover_cargo_toolchain
from literate_ai.adapters.builders.rust import discover_rust_toolchain
from literate_ai.adapters.component_acceptance import (
    ComponentAcceptanceError,
    load_versioned_library_acceptance,
)
from literate_ai.adapters.directory_artifacts import directory_export_bytes
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.adapters.retained_cargo_input_owners_v2 import (
    RetainedCargoInputRootV2,
    capture_retained_cargo_input_owners_v2,
)
from literate_ai.adapters.rust_library_harness_execution import (
    execute_rust_library_harness,
    verify_rust_harness_execution,
)
from literate_ai.application.artifact_graph import create_artifact_build_graph
from literate_ai.contracts import (
    LibraryCapabilityImport,
    LibraryImportSurface,
    canonical_identity,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.product_json import product_json_bytes
from literate_ai.contracts.retained_cargo_custody import (
    RetainedCargoCustodyPolicy,
    RetainedCargoInputBudget,
    RetainedCargoOwnerBudget,
)
from literate_ai.contracts.rust_library_harness import (
    RustHarnessInputSources,
    RustHarnessLockfile,
    RustHarnessQualifiedProvider,
    RustHarnessResolution,
    RustHarnessResolvedDependency,
    RustHarnessResolvedPackage,
    RustLibraryHarnessDependencies,
)
from tests.smoke import test_native_cargo_standard as native
from tests.support import fixtures_test_artifact_graph_contracts as graph_fixture
from tests.support.fixtures_test_component_node_generation_preparation import _fixture


def digest(raw):
    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(raw).hexdigest())


def interface_surface(lock, name, package, symbol):
    node = next(node for node in lock.nodes if node.revision.coordinate.name == name)
    public = node.interface_bindings[0]
    return LibraryImportSurface(
        "rust",
        package,
        (
            LibraryCapabilityImport(
                public.capability, public.interface_identity, package, (symbol,)
            ),
        ),
    )


def original_tools():
    observed = discover_rust_toolchain(os.environ)
    rust = Path(observed.compiler_executable)
    cargo = LocalComponentToolBinding.from_observed_toolchain(
        discover_cargo_toolchain(
            os.environ,
            pinned_command=(
                str(rust.with_name("cargo.exe" if os.name == "nt" else "cargo")),
            ),
        )
    )
    rustc = LocalComponentToolBinding.from_observed_toolchain(
        discover_rust_toolchain(
            {
                **os.environ,
                "RUSTC": subprocess.list2cmdline([str(rust)])
                if os.name == "nt"
                else shlex.quote(str(rust)),
            }
        )
    )
    return cargo, rustc


def authored_oracle(
    root,
    lock,
    cargo,
    rustc,
    *,
    diamond=False,
    component_names=("storage", "service", "application"),
    authority=None,
    oracle_filename="oracle.json",
):
    """Only locked public selectors, authored manifests and real tool/input facts."""
    authority = authority or root / "verification"
    authority.mkdir(exist_ok=True)
    beta_name, alpha_name, gamma_name = component_names
    home, workspace = authority / "cargo-home", authority / "workspace"
    home.mkdir()
    (workspace / "src").mkdir(parents=True)
    (workspace / "Cargo.toml").write_text(
        '[package]\nname="bookkeeping"\nversion="0.0.0"\nedition="2021"\n[workspace]\n'
    )
    (workspace / "src/lib.rs").write_text("pub fn bookkeeping() {}\n")
    env = {
        **os.environ,
        "CARGO_HOME": str(home),
        "CARGO_TARGET_DIR": str(root / "bookkeeping-target"),
        "CARGO_NET_OFFLINE": "true",
        "RUSTC": rustc.executable,
    }
    for args in [
        ("generate-lockfile", "--offline"),
        ("build", "--offline", "--locked"),
    ]:
        result = subprocess.run(
            [*cargo.command, *args],
            cwd=workspace,
            env=env,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            raise AssertionError(result.stderr.decode())
    budget = RetainedCargoInputBudget(1024, 2048, 1024**2, 16 * 1024**2)
    policy = RetainedCargoCustodyPolicy(
        tuple(
            RetainedCargoOwnerBudget(name, budget)
            for name in ["cargo-home", "workspace"]
        ),
        RetainedCargoInputBudget(2048, 4096, 1024**2, 32 * 1024**2),
    )
    owners = capture_retained_cargo_input_owners_v2(
        tuple(
            RetainedCargoInputRootV2(name, authority / name)
            for name in ["cargo-home", "workspace"]
        ),
        policy=policy,
        maximum_metadata_bytes=64 * 1024**2,
    )
    beta_revision = next(
        node.revision.identity
        for node in lock.nodes
        if node.revision.coordinate.name == beta_name
    )
    beta_surface = interface_surface(lock, beta_name, "beta", "Event")
    root_name, root_package = (
        (gamma_name, "gamma") if diamond else (alpha_name, "alpha")
    )
    alpha_surface = interface_surface(lock, root_name, root_package, "read")
    provider = RustHarnessQualifiedProvider(
        beta_revision, beta_surface.identity, "beta", "1.0.0", (), "beta"
    )
    beta = RustHarnessResolvedPackage(
        provider.scope, "source/Cargo.toml", "beta", "1.0.0", None, (), ()
    )
    beta_edge = RustHarnessResolvedDependency("beta", beta.identity, None, None)
    providers = [provider]
    provider_packages = [beta]
    if diamond:
        alpha_revision = next(
            node.revision.identity
            for node in lock.nodes
            if node.revision.coordinate.name == alpha_name
        )
        provider_alpha = RustHarnessQualifiedProvider(
            alpha_revision,
            interface_surface(lock, alpha_name, "alpha", "read").identity,
            "alpha",
            "1.0.0",
            (),
            None,
        )
        alpha = RustHarnessResolvedPackage(
            provider_alpha.scope,
            "source/Cargo.toml",
            "alpha",
            "1.0.0",
            None,
            (),
            (beta_edge,),
        )
        providers.append(provider_alpha)
        provider_packages.append(alpha)
        root_edges = (
            RustHarnessResolvedDependency("alpha", alpha.identity, None, None),
            beta_edge,
        )
    else:
        root_edges = (beta_edge,)
    library = RustHarnessResolvedPackage(
        "library", "source/Cargo.toml", root_package, "1.0.0", None, (), root_edges
    )
    harness_edges = tuple(
        sorted(
            (
                RustHarnessResolvedDependency(
                    root_package, library.identity, None, None
                ),
                beta_edge,
            ),
            key=lambda row: row.key,
        )
    )
    harness = RustHarnessResolvedPackage(
        "harness",
        "Cargo.toml",
        "litai-library-acceptance",
        "0.0.0",
        None,
        (),
        harness_edges,
    )
    resolution = RustHarnessResolution(
        harness.identity,
        library.identity,
        tuple(
            sorted(
                [harness, library, *provider_packages], key=lambda row: row.identity.uri
            )
        ),
    )
    records = [("alpha", "1.0.0", ["beta"]), ("beta", "1.0.0", [])]
    if diamond:
        records.append(("gamma", "1.0.0", ["alpha", "beta"]))
    records.append(
        ("litai-library-acceptance", "0.0.0", sorted([root_package, "beta"]))
    )
    lock_bytes = (
        "version = 4\n"
        + "".join(
            "\n[[package]]\nname="
            + json.dumps(name)
            + "\nversion="
            + json.dumps(version)
            + "\n"
            + (
                "dependencies=" + json.dumps(dependencies) + "\n"
                if dependencies
                else ""
            )
            for name, version, dependencies in records
        )
    ).encode()
    (authority / "Cargo.lock").write_bytes(lock_bytes)
    declaration = RustLibraryHarnessDependencies(
        (),
        RustHarnessLockfile("Cargo.lock", digest(lock_bytes), len(lock_bytes)),
        RustHarnessInputSources(
            (("cargo-home", "cargo-home"), ("workspace", "workspace")),
            policy,
            owners.identity,
            64 * 1024**2,
        ),
        resolution,
        cargo.toolchain_identity,
        rustc.toolchain_identity,
        180,
        1024**2,
        1024**2,
        tuple(sorted(providers, key=lambda row: row.component_revision.uri)),
    )
    cap = alpha_surface.capabilities[0].capability
    values = [0, 37, 4294967295]
    rows = []
    for value in values:
        fmt = (
            json.dumps(
                {"case_id": str(value), "capability": cap, "result": "__VALUE__"},
                separators=(",", ":"),
            )
            .replace("{", "{{")
            .replace("}", "}}")
            .replace('"__VALUE__"', "{}")
        )
        rows.append(
            "{let event=beta::Event{value:"
            + str(value)
            + "u32};format!("
            + json.dumps(fmt)
            + ","
            + root_package
            + "::read(&event))}"
        )
    envelope = (
        '{"schema":"literate-ai/library-acceptance-results@1","cases":[__ROWS__]}'.replace(
            "{", "{{"
        )
        .replace("}", "}}")
        .replace("__ROWS__", "{}")
    )
    source = (
        "fn main(){let rows=vec!["
        + ",".join(rows)
        + "];println!("
        + json.dumps(envelope)
        + ',rows.join(","));}\n'
    ).encode()
    (authority / "harness.rs").write_bytes(source)
    root_node = next(
        node for node in lock.nodes if node.revision.identity == lock.root_revision
    )
    document = {
        "schema": "literate-ai/library-acceptance-oracle@2",
        "component": root_name,
        "specification_set_identity": root_node.revision.specification_set_identity.uri,
        "public_interface_identities": sorted(
            item.identity.uri for item in root_node.revision.public_interfaces
        ),
        "import_surface_identity": alpha_surface.identity.uri,
        "language": "rust",
        "harness": "harness.rs",
        "harness_identity": digest(source).uri,
        "cases": [
            {
                "case_id": str(value),
                "capability": cap,
                "arguments": [value],
                "expected_result": value,
            }
            for value in values
        ],
        "rust_harness": declaration.to_dict(),
    }
    path = authority / oracle_filename
    path.write_text(json.dumps(document, sort_keys=True))
    return (
        load_versioned_library_acceptance(path, root_name),
        alpha_surface,
        declaration,
    )


@unittest.skipUnless(
    shutil.which("cargo") and shutil.which("rustc"), "actual Rust tools required"
)
class NativeCargoHarnessAliasTests(unittest.TestCase):
    def test_same_pre_generation_oracle_accepts_different_provider_material(self):
        self._two_materials(diamond=False)

    def test_diamond_keeps_one_nominal_provider_with_an_unchanged_oracle(self):
        self._two_materials(diamond=True)

    def _two_materials(self, *, diamond):
        with tempfile.TemporaryDirectory(prefix="native-qualified-alias-") as temporary:
            root = Path(temporary).resolve()
            snapshot, execution = _fixture(
                root_name="application" if diamond else "service",
                library_names=frozenset({"application", "service", "storage"}),
                diamond=diamond,
            )
            cargo, rustc = original_tools()
            oracle, surface, declaration = authored_oracle(
                root,
                snapshot.authority.lock,
                cargo,
                rustc,
                diamond=diamond,
            )
            oracle_identity = oracle.identity
            original_oracle = (root / "verification/oracle.json").read_bytes()
            observations = []
            for variant in range(2):
                current = root / str(variant)
                current.mkdir()
                original = native.register_strict_source

                def register(
                    registry, source, original=original, variant=variant, **kwargs
                ):
                    # Both body AND original manifest differ before source registration.
                    manifest = source / "source/Cargo.toml"
                    manifest.write_bytes(
                        manifest.read_bytes()
                        + (
                            "\n# independently generated variant " + str(variant) + "\n"
                        ).encode()
                    )
                    body = source / "source/src/lib.rs"
                    body.write_bytes(
                        (
                            "// independently generated body " + str(variant) + "\n"
                        ).encode()
                        + body.read_bytes()
                    )
                    return original(registry, source, **kwargs)

                with (
                    patch.object(
                        native, "register_strict_source", side_effect=register
                    ),
                    patch.object(
                        native.fixture,
                        "_native_tool_binding",
                        side_effect=lambda name: cargo if name == "cargo" else rustc,
                    ),
                ):
                    ports, actual_execution, candidates = (
                        native.NativeCargoStandardTests().system(
                            current, prepared=(snapshot, execution)
                        )
                    )
                self.assertEqual(actual_execution, execution)
                built = {}
                build_plans = {}
                ordered = sorted(
                    execution.generation_plans,
                    key=lambda plan: len(plan.direct_generation_edges),
                )
                for generation in ordered:
                    providers = tuple(
                        sorted(
                            (
                                built[edge.provider_revision].exports[0]
                                for edge in generation.direct_generation_edges
                            ),
                            key=lambda item: item.identity.uri,
                        )
                    )
                    candidate = candidates[generation.component_revision]
                    intent = ports.create(
                        execution, generation, candidate, providers, ()
                    )
                    index = ports.index(
                        candidate.component_revision, candidate.tree_identity
                    )
                    plan = ports.finalize(intent, ports.authorize(intent, index))
                    built[generation.component_revision] = ports.build(plan, providers)
                    build_plans[generation.component_revision] = plan
                export = built[execution.root_revision].exports[0]
                gf = graph_fixture.ArtifactGraphTests()
                manifests = tuple(
                    gf.manifest(
                        built[generation.component_revision].exports[0],
                        tuple(
                            sorted(
                                (
                                    built[edge.provider_revision].exports[0]
                                    for edge in generation.direct_generation_edges
                                ),
                                key=lambda item: item.identity.uri,
                            )
                        ),
                    )
                    for generation in ordered
                )
                graph = create_artifact_build_graph(
                    build_system_driver_identity=gf.driver,
                    manifests=manifests,
                    link_roots=(export.identity,),
                )
                link = next(
                    plan
                    for plan in graph.link_plans
                    if plan.root_artifact_identity == export.identity
                )
                plan, result = ports.create_project_package(
                    snapshot.authority.lock, execution, None, graph, link
                )
                custody = ports.project_package_custody(plan, result)
                context = ports._rust_harness_provider_context(
                    snapshot.authority.lock, custody, plan, result
                )
                context.selections(declaration)
                if variant == 0:
                    with self.assertRaisesRegex(
                        RuntimeError, "package custody changed"
                    ):
                        ports._rust_harness_provider_context(
                            snapshot.authority.lock, replace(custody), plan, result
                        )
                    with patch.dict(
                        ports._plans_by_revision, {execution.root_revision.uri: None}
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError, "BUILD plan is not currently registered"
                        ):
                            ports._rust_harness_provider_context(
                                snapshot.authority.lock, custody, plan, result
                            )
                    wrong_surface = replace(
                        declaration,
                        qualified_providers=tuple(
                            replace(
                                row,
                                import_surface_identity=canonical_identity(
                                    "wrong surface"
                                ),
                            )
                            for row in declaration.qualified_providers
                        ),
                    )
                    with self.assertRaisesRegex(ValueError, "authored_interface"):
                        context.selections(wrong_surface)
                if variant == 0:
                    self._assert_archived_current_authority(
                        snapshot.authority.lock,
                        ports,
                        build_plans[execution.root_revision],
                        built,
                        context,
                    )
                package = custody.artifact_paths[export.identity.uri]
                before = digest(directory_export_bytes(package))

                def require_current(
                    ports=ports,
                    custody=custody,
                    plan=plan,
                    result=result,
                    context=context,
                    package=package,
                    before=before,
                ):
                    self.assertEqual(
                        ports._rust_harness_provider_context(
                            snapshot.authority.lock, custody, plan, result
                        ),
                        context,
                    )
                    self.assertEqual(digest(directory_export_bytes(package)), before)

                recorder = QualificationEvidenceRecorder(
                    max_bytes=16 * 1024**2, max_records=96
                )
                stage = current / "verifier"
                stage.mkdir()
                arguments = dict(
                    stage=stage,
                    export=package,
                    library_manifest=package / "source/Cargo.toml",
                    library_alias=surface.package,
                    cargo=cargo,
                    rustc=rustc,
                    cases_document=product_json_bytes(
                        [
                            {
                                "case_id": case.case_id,
                                "capability": case.capability,
                                "arguments": case.arguments,
                            }
                            for case in oracle.cases
                        ]
                    ).decode(),
                    surface_document=canonical_json_bytes(surface.to_dict()).decode(),
                    environment=os.environ,
                    require_library_current=require_current,
                    record_bytes=recorder.remember_bytes,
                    evidence_root=current / "evidence",
                    allow_host_execution=True,
                )
                if variant == 0:
                    with patch(
                        "literate_ai.adapters.rust_library_harness_execution.run_bounded_process"
                    ) as process:
                        with (
                            self.assertRaises(ComponentAcceptanceError) as refusal,
                            execute_rust_library_harness(oracle, **arguments),
                        ):
                            self.fail("missing current provider context executed")
                        self.assertEqual(
                            refusal.exception.code,
                            "component_acceptance.rust_harness_qualified_context",
                        )
                        process.assert_not_called()
                    self.assertEqual(list(stage.iterdir()), [])
                    self.assertFalse((current / "evidence").exists())
                with execute_rust_library_harness(
                    oracle, **arguments, qualified_context=context
                ) as (native_result, observation):
                    self.assertEqual(native_result.returncode, 0)
                    identity = recorder.remember_json(observation)
                reader = QualificationEvidenceReader(
                    recorder.entries,
                    max_bytes=recorder.max_bytes,
                    max_records=recorder.max_records,
                )
                raw_metadata = json.loads(
                    reader.read_bytes(
                        ContentIdentity.parse_uri(
                            observation["commands"][0]["stdout_identity"]
                        )
                    )
                )
                beta_packages = [
                    row for row in raw_metadata["packages"] if row["name"] == "beta"
                ]
                self.assertEqual(len(beta_packages), 1)
                self.assertEqual(
                    sum(
                        any(
                            edge["pkg"] == beta_packages[0]["id"]
                            for edge in row["deps"]
                        )
                        for row in raw_metadata["resolve"]["nodes"]
                    ),
                    3 if diamond else 2,
                )
                # Transport copies retained bytes, not a live directory reference.
                import io
                import zipfile

                transport = io.BytesIO()
                with zipfile.ZipFile(
                    transport, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    for record_id, raw in recorder.entries:
                        archive.writestr(record_id.digest, raw)
                carried = transport.getvalue()
                del reader
                shutil.rmtree(current)
                self.assertFalse(current.exists())
                with zipfile.ZipFile(io.BytesIO(carried)) as archive:
                    entries = tuple(
                        sorted(
                            (
                                (
                                    ContentIdentity.parse_uri("sha256:" + name),
                                    archive.read(name),
                                )
                                for name in archive.namelist()
                            ),
                            key=lambda row: row[0].uri,
                        )
                    )
                replay = QualificationEvidenceReader(
                    entries,
                    max_bytes=recorder.max_bytes,
                    max_records=recorder.max_records,
                )
                with patch(
                    "literate_ai.adapters.rust_library_harness_execution.run_bounded_process"
                ) as process:
                    verify_rust_harness_execution(
                        replay,
                        identity,
                        oracle=oracle,
                        library_alias=surface.package,
                        surface=surface,
                        qualified_context=context,
                    )
                    process.assert_not_called()
                print(
                    json.dumps(
                        {
                            "control": "diamond" if diamond else "AlphaBeta",
                            "variant": variant,
                            "unchanged_oracle": oracle_identity.uri,
                            "beta_packages": 1,
                            "beta_incoming": 3 if diamond else 2,
                            "closed_harness_replay_without_old_source": True,
                            "actual_graph": context.graph.identity.uri,
                        }
                    )
                )

                observations.append(observation["qualified_providers"])
                self.assertEqual(oracle.identity, oracle_identity)
                self.assertEqual(
                    (root / "verification/oracle.json").read_bytes(), original_oracle
                )
            self.assertNotEqual(
                observations[0]["graph_identity"], observations[1]["graph_identity"]
            )
            for field in [
                "source_tree_identity",
                "original_manifest_identity",
                "artifact_identity",
                "projection_identity",
            ]:
                first = {
                    row["package"]: row[field] for row in observations[0]["providers"]
                }
                second = {
                    row["package"]: row[field] for row in observations[1]["providers"]
                }
                self.assertEqual(set(first), {"alpha", "beta"} if diamond else {"beta"})
                self.assertTrue(all(first[name] != second[name] for name in first))

    def _assert_archived_current_authority(self, lock, ports, plan, built, context):
        """Real qualified material; this value join grants no host execution."""
        from literate_ai.adapters.qualification_native_cargo import (
            reopen_native_cargo_inputs,
        )

        recorder = QualificationEvidenceRecorder(
            max_bytes=64 * 1024**2, max_records=512
        )
        exports = {
            export.identity: export
            for result in built.values()
            for export in result.exports
        }
        for export in exports.values():
            recorder.remember_bytes(ports.read_artifact_blob(export.blob))
        for target in ports.cargo_targets.values():
            recorder.remember_json(target.identity_document())
        reader = QualificationEvidenceReader(
            recorder.entries,
            max_bytes=recorder.max_bytes,
            max_records=recorder.max_records,
        )
        arguments = dict(
            plan=plan,
            component_lock=lock,
            current_commands={
                value.component_revision: value for value in ports.contracts.values()
            },
            provider_exports=exports,
            target=ports.cargo_targets[plan.component_revision.uri],
        )
        self.assertEqual(
            reopen_native_cargo_inputs(reader, **arguments),
            (context.graph, context.sources),
        )
        bad = [
            ("missing lock", dict(component_lock=None)),
            ("missing commands", dict(current_commands=None)),
            ("missing exports", dict(provider_exports=None)),
            ("foreign command membership", dict(current_commands={})),
            ("missing current provider", dict(provider_exports={})),
            (
                "foreign target",
                dict(
                    target=next(
                        value
                        for value in ports.cargo_targets.values()
                        if value.component_revision != plan.component_revision
                    )
                ),
            ),
        ]
        foreign = replace(
            next(iter(exports.values())),
            source_tree_identity=canonical_identity("foreign source"),
        )
        bad.append(
            (
                "false export identity association",
                dict(provider_exports={next(iter(exports)): foreign}),
            )
        )
        for name, changed in bad:
            with (
                self.subTest(current_authority=name),
                self.assertRaises((ValueError, KeyError)),
            ):
                reopen_native_cargo_inputs(reader, **{**arguments, **changed})

    def test_qualified_resolution_requires_explicit_declaration_and_exact_roundtrip(
        self,
    ):
        with tempfile.TemporaryDirectory(
            prefix="native-qualified-contract-"
        ) as temporary:
            root = Path(temporary).resolve()
            snapshot, _ = _fixture(
                root_name="service", library_names=frozenset({"service", "storage"})
            )
            cargo, rustc = original_tools()
            _oracle, _surface, declaration = authored_oracle(
                root, snapshot.authority.lock, cargo, rustc
            )
            self.assertEqual(
                RustLibraryHarnessDependencies.from_dict(declaration.to_dict()),
                declaration,
            )
            with self.assertRaisesRegex(ValueError, "no declared source owner"):
                replace(declaration, qualified_providers=())
            packages = {row.identity: row for row in declaration.resolution.packages}
            harness = packages[declaration.resolution.root]
            library_edge = next(
                edge
                for edge in harness.dependencies
                if edge.provider == declaration.resolution.library
            )
            no_alias_harness = replace(harness, dependencies=(library_edge,))
            no_alias_resolution = replace(
                declaration.resolution,
                packages=tuple(
                    sorted(
                        (
                            no_alias_harness
                            if row.identity == harness.identity
                            else row
                            for row in declaration.resolution.packages
                        ),
                        key=lambda row: row.identity.uri,
                    )
                ),
            )
            no_alias = replace(
                declaration,
                resolution=no_alias_resolution,
                qualified_providers=tuple(
                    replace(row, alias=None) for row in declaration.qualified_providers
                ),
            )
            no_alias.require_library_alias("alpha")
            from types import SimpleNamespace

            from literate_ai.adapters.rust_library_harness_execution import _manifest

            with self.assertRaises(ComponentAcceptanceError) as refused:
                _manifest(
                    SimpleNamespace(rust_harness=no_alias),
                    library_alias="alpha",
                    library_directory=Path("unused"),
                )
            self.assertEqual(
                refused.exception.code,
                "component_acceptance.rust_harness_qualified_context",
            )
            for field, value in [
                ("package", "wrong-package"),
                ("version", "2.0.0"),
                ("features", ("unreviewed",)),
            ]:
                with self.assertRaisesRegex(
                    ValueError, "exact qualified provider package"
                ):
                    replace(
                        declaration,
                        qualified_providers=(
                            replace(
                                declaration.qualified_providers[0], **{field: value}
                            ),
                        ),
                    )
            collision = replace(
                declaration,
                qualified_providers=(
                    replace(declaration.qualified_providers[0], alias="alpha"),
                ),
            )
            with self.assertRaisesRegex(ValueError, "collides"):
                collision.require_library_alias("alpha")
            for change in [
                {"qualified_providers": []},
                {
                    "qualified_providers": [
                        {
                            **declaration.qualified_providers[0].to_dict(),
                            "arbitrary_path": "source",
                        }
                    ]
                },
            ]:
                with self.assertRaises(ValueError):
                    RustLibraryHarnessDependencies.from_dict(
                        {**declaration.to_dict(), **change}
                    )
