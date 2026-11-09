"""Exact root-export parity: real native packages plus typed replay fixtures.

Root acceptance identities are contract fixtures, not model qualification.
Cargo build, package custody, independent API calls and drift refusal are real.
"""

import base64
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.qualification import (
    FilesystemQualificationError,
    FilesystemQualificationParityVerifier,
    _expand,
    _observe,
)
from literate_ai.adapters.qualification_authority import qualification_verifier_record
from literate_ai.adapters.qualification_capture import QualificationEvidenceRecorder
from literate_ai.application.artifact_graph import create_artifact_build_graph
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.native_cargo_provider_graph import NativeCargoProviderGraph
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)
from literate_ai.source_to_specification.errors import SourceToSpecificationError
from literate_ai.source_to_specification.host_qualification import (
    LocalHostParityVerifier,
    LocalHostSpecRegenerator,
    LocalQualificationCase,
    LocalQualificationProfile,
)
from literate_ai.source_to_specification.inventory import inventory_source
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleRequest,
    QualificationWorkspaceAllocation,
)
from tests.smoke import test_native_cargo_standard as native_standard
from tests.support import fixtures_test_artifact_graph_contracts as graph_fixture
from tests.support.fixtures_test_component_node_generation_preparation import _fixture

_NATIVE_PROBE = r"""
from pathlib import Path
import json
import subprocess
import sys

mode, material, objects, cargo, beta = sys.argv[1:6]
value = json.loads(sys.argv[6])[0]
assert type(value) is int and 0 <= value <= 4294967295
material, out = Path(material), Path(objects)
out.mkdir(exist_ok=True, parents=True)
expected_cwd = material if mode == 'baseline' else material / 'source'
assert Path.cwd().resolve() == expected_cwd.resolve()
alpha = material / 'alpha' if mode == 'baseline' else material / 'source'
beta = material / 'beta' if mode == 'baseline' else material / beta
(out / 'src').mkdir(exist_ok=True)
(out / 'Cargo.toml').write_text(
    '[package]\nname="probe"\nversion="0.0.0"\nedition="2021"\n'
    '[workspace]\n[dependencies]\nalpha={path=' + json.dumps(str(alpha))
    + '}\nbeta={path=' + json.dumps(str(beta)) + '}\n'
)
(out / 'Cargo.lock').write_text(
    'version = 4\n\n[[package]]\nname="alpha"\nversion="1.0.0"\n'
    'dependencies=["beta"]\n\n[[package]]\nname="beta"\nversion="1.0.0"\n'
    '\n[[package]]\nname="probe"\nversion="0.0.0"\n'
    'dependencies=["alpha","beta"]\n'
)
(out / 'src/main.rs').write_text(
    'fn main(){let e=beta::Event{value:' + str(value)
    + 'u32};println!("{}",alpha::read(&e));}\n'
)
common = [
    '--quiet', '--locked', '--offline', '--manifest-path', str(out / 'Cargo.toml')
]
actual = subprocess.run(
    [cargo, 'run', *common, '--target-dir', str(out / 'target')],
    cwd=out, capture_output=True,
)
sys.stderr.buffer.write(actual.stderr)
assert actual.returncode == 0
metadata = subprocess.run(
    [cargo, 'metadata', *common, '--format-version', '1'],
    cwd=out, capture_output=True,
)
assert metadata.returncode == 0
packages = json.loads(metadata.stdout)['packages']
assert sorted(p['name'] for p in packages) == ['alpha', 'beta', 'probe']
assert len([p for p in packages if p['name'] == 'beta']) == 1
(out / 'native-metadata.json').write_bytes(metadata.stdout)
print(json.loads(actual.stdout))
"""


def native_fixture(root):
    ports, execution, candidates = native_standard.NativeCargoStandardTests().system(
        root
    )
    snapshot, again = _fixture(
        root_name="service", library_names=frozenset({"service", "storage"})
    )
    lock = snapshot.authority.lock
    assert execution.identity == again.identity
    built = {}
    ordered = sorted(
        execution.generation_plans, key=lambda g: len(g.direct_generation_edges)
    )
    for g in ordered:
        c = candidates[g.component_revision]
        providers = tuple(
            built[e.provider_revision].exports[0] for e in g.direct_generation_edges
        )
        intent = ports.create(execution, g, c, providers, ())
        index = ports.index(c.component_revision, c.tree_identity)
        plan = ports.finalize(intent, ports.authorize(intent, index))
        built[g.component_revision] = ports.build(plan, providers)
    root_revision = execution.root_revision
    export = built[root_revision].exports[0]
    gf = graph_fixture.ArtifactGraphTests()
    manifests = []
    for g in ordered:
        manifests.append(
            gf.manifest(
                built[g.component_revision].exports[0],
                tuple(
                    built[e.provider_revision].exports[0]
                    for e in g.direct_generation_edges
                ),
            )
        )
    graph = create_artifact_build_graph(
        build_system_driver_identity=gf.driver,
        manifests=tuple(manifests),
        link_roots=(export.identity,),
    )
    link = next(
        p for p in graph.link_plans if p.root_artifact_identity == export.identity
    )
    package_plan, package_result = ports.create_project_package(
        lock, execution, None, graph, link
    )
    integration = StandardRootIntegrationEvidence(
        lock.identity,
        execution.identity,
        canonical_identity("fixture-project-build"),
        graph,
        link,
        package_plan,
        package_result,
        canonical_identity("fixture-integration-test"),
        canonical_identity("fixture-packaged-execution"),
        canonical_identity("fixture-independent-acceptance"),
    )
    original = root / "baseline"
    original.mkdir()
    for n in ["alpha", "beta"]:
        d = original / n
        d.mkdir()
        (d / "src").mkdir()
        (d / "Cargo.toml").write_text(
            '[package]\nname="'
            + n
            + '"\nversion="1.0.0"\nedition="2021"\n'
            + ('[dependencies]\nbeta={path="../beta"}\n' if n == "alpha" else "")
        )
        (d / "src/lib.rs").write_text(
            "pub fn read(e:&beta::Event)->u32{e.value}\n"
            if n == "alpha"
            else "pub struct Event{pub value:u32}\n"
        )
    source_identity = ContentIdentity.parse_uri(inventory_source(original).identity)
    run = canonical_identity("native-context-run")
    generated = ports.source_trees.resolve(candidates[root_revision].tree_identity)
    request = QualificationLifecycleRequest(
        run,
        export.target_identity,
        lock.identity,
        canonical_identity("specification"),
        source_identity,
        canonical_identity("audit"),
        canonical_identity("promotion"),
        QualificationWorkspaceAllocation(canonical_identity("allocation"), True),
    )
    recorder = QualificationEvidenceRecorder(max_bytes=16 * 1024**2, max_records=1000)
    lifecycle = SimpleNamespace(
        generated_roots={
            run.uri: tuple(
                sorted(
                    (
                        (c.tree_identity, ports.source_trees.resolve(c.tree_identity))
                        for c in candidates.values()
                    ),
                    key=lambda x: x[0].uri,
                )
            )
        },
        root_generated_roots={run.uri: generated},
        run_roots={run.uri: root / "parity"},
        root_library_contexts={run.uri: (request, integration, ports)},
        prepared=SimpleNamespace(
            locked_authority_snapshot=SimpleNamespace(
                authority=SimpleNamespace(lock=lock)
            )
        ),
        require_current_authority=lambda: None,
        evidence_recorder=recorder,
    )
    (root / "parity/objects").mkdir(parents=True)
    native = NativeCargoProviderGraph.from_dict(
        json.loads(
            (
                ports.artifact_path(export) / ".literate/native-cargo/projection.json"
            ).read_text()
        )["graph"]
    )
    assert len(native.nodes) == 1 and native.nodes[0].package == "beta"
    beta_subpath = (
        ".literate/native-cargo/providers/"
        + native.nodes[0].projection_identity.digest
        + "/source"
    )
    helper = _NATIVE_PROBE

    script = (
        "import base64;exec(base64.b64decode("
        + repr(base64.b64encode(helper.encode()).decode())
        + "))"
    )
    cargo = shutil.which("cargo")
    command = (sys.executable, "-c", script)
    profile = LocalQualificationProfile(
        "native-root-context",
        ("unused",),
        (("unused",),),
        (
            *command,
            "baseline",
            str(original),
            str(root / "baseline-objects"),
            cargo,
            beta_subpath,
        ),
        (
            *command,
            "generated",
            "{library_artifact}",
            "{build_root}/probe",
            cargo,
            beta_subpath,
        ),
        (LocalQualificationCase("value", (37,), 37),),
        ("fixture.alpha",),
        timeout_seconds=120,
    )
    verifier = FilesystemQualificationParityVerifier(
        source_root=original,
        source_snapshot_identity=source_identity,
        profile=profile,
        lifecycle=lifecycle,
    )
    return SimpleNamespace(
        verifier=verifier,
        lifecycle=lifecycle,
        run=run,
        source_identity=source_identity,
        profile=profile,
        ports=ports,
        export=export,
        root=root,
        integration=integration,
    )


class LibraryContextProfileTests(unittest.TestCase):
    def profile(self):
        return LocalQualificationProfile(
            "context",
            ("build",),
            (("test",),),
            ("baseline",),
            ("run", "{library_artifact}"),
            (LocalQualificationCase("value", [], 37),),
            ("fixture",),
        )

    def test_opt_in_is_generated_only_and_keeps_old_profile_protocol(self):
        profile = self.profile()
        self.assertTrue(profile.uses_library_artifact)
        self.assertEqual(
            LocalQualificationProfile.from_dict(profile.to_dict()).to_dict(),
            profile.to_dict(),
        )
        old = replace(profile, generated_command=("run", "{workspace}"))
        self.assertFalse(old.uses_library_artifact)
        self.assertEqual(
            qualification_verifier_record(old, canonical_identity("source"))[
                "provider"
            ],
            "filesystem-standard-independent-parity@1",
        )
        self.assertEqual(
            qualification_verifier_record(profile, canonical_identity("source"))[
                "provider"
            ],
            "filesystem-standard-independent-parity@2",
        )
        for field, value in [
            ("source_command", ("run", "{library_artifact}")),
            ("build_command", ("run", "{library_artifact}")),
            ("test_commands", (("run", "{library_artifact}"),)),
        ]:
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    SourceToSpecificationError, "unsupported placeholders"
                ),
            ):
                replace(profile, **{field: value})

    def test_missing_and_legacy_context_refuse_before_host_effects(self):
        profile = self.profile()
        with self.assertRaises(FilesystemQualificationError):
            _expand(
                profile.generated_command,
                workspace=Path("/unused"),
                build_root=Path("/unused"),
            )
        with patch(
            "literate_ai.source_to_specification.host_qualification._run"
        ) as run:
            with self.assertRaisesRegex(
                SourceToSpecificationError, "Standard lifecycle"
            ):
                LocalHostParityVerifier(
                    source_root=Path("/missing"),
                    source_snapshot_id="unused",
                    profile=profile,
                )
            with self.assertRaisesRegex(
                SourceToSpecificationError, "Standard lifecycle"
            ):
                LocalHostSpecRegenerator(
                    generation_command=("run", "{workspace}"),
                    generation_cwd=Path("/missing"),
                    profile=profile,
                )
            run.assert_not_called()


@unittest.skipUnless(
    shutil.which("cargo") and shutil.which("rustc"), "native Rust tools unavailable"
)
class NativeLibraryContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-parity-context-")
        self.addCleanup(temporary.cleanup)
        self.fixture = native_fixture(Path(temporary.name))

    def verify(self):
        n = self.fixture
        return n.verifier.verify(
            run_identity=n.run,
            source_snapshot_identity=n.source_identity,
            generated_tree_identities=tuple(
                t for t, _ in n.lifecycle.generated_roots[n.run.uri]
            ),
            case_map=n.verifier.case_map,
        )

    def test_actual_native_calls_use_exact_sealed_projection_and_nominal_beta(self):
        n = self.fixture
        evidence = self.verify()
        self.assertTrue(all(c.passed for c in evidence.cases))
        records = dict(n.lifecycle.evidence_recorder.entries)
        case = evidence.cases[0]
        generated = json.loads(records[case.generated_observation_identity])
        baseline = json.loads(records[case.baseline_observation_identity])
        self.assertEqual(
            generated["schema"], "literate-ai/qualification-process-observation@2"
        )
        self.assertEqual(generated["artifact_export_identity"], n.export.identity.uri)
        self.assertIn("{library_artifact}", generated["command"])
        self.assertEqual(
            baseline["schema"], "literate-ai/qualification-process-observation@1"
        )
        self.assertEqual(
            inventory_source(n.root / "baseline").identity, n.source_identity.uri
        )

    def test_same_native_case_fails_with_old_source_only_context(self):
        n = self.fixture
        old_profile = replace(
            n.profile,
            generated_command=tuple(
                argument.replace("{library_artifact}", "{workspace}")
                for argument in n.profile.generated_command
            ),
        )
        n.verifier = FilesystemQualificationParityVerifier(
            source_root=n.root / "baseline",
            source_snapshot_identity=n.source_identity,
            profile=old_profile,
            lifecycle=n.lifecycle,
        )
        evidence = self.verify()
        self.assertFalse(evidence.cases[0].passed)

    def test_missing_wrong_run_lock_target_and_root_export_refuse_before_process(self):
        n = self.fixture
        original = n.lifecycle.root_library_contexts[n.run.uri]
        request, integration, ports = original
        changes = [
            None,
            (
                replace(request, run_identity=canonical_identity("other-run")),
                integration,
                ports,
            ),
            (
                replace(
                    request, component_lock_identity=canonical_identity("other-lock")
                ),
                integration,
                ports,
            ),
            (
                replace(
                    request, target_profile_identity=canonical_identity("other-target")
                ),
                integration,
                ports,
            ),
        ]
        for context in changes:
            with (
                self.subTest(context=context),
                patch("literate_ai.adapters.qualification._observe") as observe,
            ):
                n.lifecycle.root_library_contexts = (
                    {} if context is None else {n.run.uri: context}
                )
                with self.assertRaises(FilesystemQualificationError):
                    self.verify()
                observe.assert_not_called()
        n.lifecycle.root_library_contexts = {n.run.uri: original}
        lock = n.lifecycle.prepared.locked_authority_snapshot.authority.lock
        n.lifecycle.prepared = SimpleNamespace(
            locked_authority_snapshot=SimpleNamespace(
                authority=SimpleNamespace(
                    lock=SimpleNamespace(
                        identity=lock.identity,
                        root_revision=canonical_identity("foreign-root"),
                    )
                )
            )
        )
        with patch("literate_ai.adapters.qualification._observe") as observe:
            with self.assertRaises(FilesystemQualificationError):
                self.verify()
            observe.assert_not_called()

    def test_modified_package_and_stale_relocated_mapping_do_not_fall_back(self):
        n = self.fixture
        custody = n.ports.project_package_custody(
            n.integration.package_plan, n.integration.package_result
        )
        old = custody.root
        new = old.with_name(old.name + "-moved")
        old.rename(new)
        with patch("literate_ai.adapters.qualification._observe") as observe:
            with self.assertRaises(FilesystemQualificationError):
                self.verify()
            observe.assert_not_called()
        new.rename(old)
        material = custody.artifact_paths[n.export.identity.uri]
        (material / "source/src/lib.rs").write_text("pub fn wrong(){}")
        with patch("literate_ai.adapters.qualification._observe") as observe:
            with self.assertRaises(FilesystemQualificationError):
                self.verify()
            observe.assert_not_called()

    def test_actual_process_output_cannot_hide_after_execution_material_drift(self):
        n = self.fixture
        custody = n.ports.project_package_custody(
            n.integration.package_plan, n.integration.package_result
        )
        material = custody.artifact_paths[n.export.identity.uri]

        def observe(*args, **kwargs):
            result = _observe(*args, **kwargs)
            if kwargs.get("artifact_export_identity") is not None:
                (material / "changed-after-command").write_text("drift")
            return result

        with patch("literate_ai.adapters.qualification._observe", side_effect=observe):
            with self.assertRaises(FilesystemQualificationError):
                self.verify()

    def test_other_valid_export_and_package_need_exact_live_run_custody(self):
        from literate_ai.adapters.packaging import DirectoryPackageAdapter
        from literate_ai.application.artifact_graph import create_package_plan
        from literate_ai.contracts.executable_components import PackageKind

        n = self.fixture
        foreign = replace(
            n.export, authorization_identity=canonical_identity("different-execution")
        )
        original = n.integration.artifact_graph
        exports = {e.identity: e for m in original.manifests for e in m.exports}
        factory = graph_fixture.ArtifactGraphTests()
        graph = create_artifact_build_graph(
            build_system_driver_identity=original.build_system_driver_identity,
            manifests=tuple(
                factory.manifest(
                    foreign,
                    tuple(exports[x] for x in foreign.dependency_artifact_identities),
                )
                if m.component_revision == foreign.component_revision
                else m
                for m in original.manifests
            ),
            link_roots=(foreign.identity,),
        )
        link = next(
            p for p in graph.link_plans if p.root_artifact_identity == foreign.identity
        )
        plan = create_package_plan(
            graph,
            root_component_revision=foreign.component_revision,
            component_lock_identity=n.integration.component_lock_identity,
            target_identity=foreign.target_identity,
            root_artifact_identity=foreign.identity,
            package_kind=PackageKind.DIRECTORY,
            packager_identity=n.integration.package_plan.packager_identity,
            destinations={
                e.identity.uri: "artifacts/" + e.identity.digest
                for m in graph.manifests
                for e in m.exports
            },
            entrypoints=(),
            runtime_requirements=(),
        )
        result = DirectoryPackageAdapter().package(
            plan, read_blob=n.ports.read_artifact_blob
        )
        # Constructor validates graph/link/plan/result associations. Correct
        # bytes/role/revision/target still cannot mint this run's local custody.
        other = replace(
            n.integration,
            artifact_graph=graph,
            link_plan=link,
            package_plan=plan,
            package_result=result,
        )
        request, _, ports = n.lifecycle.root_library_contexts[n.run.uri]
        n.lifecycle.root_library_contexts[n.run.uri] = (request, other, ports)
        with patch.object(
            ports, "project_package_custody", wraps=ports.project_package_custody
        ) as custody:
            with patch("literate_ai.adapters.qualification._observe") as observe:
                with self.assertRaises(FilesystemQualificationError):
                    self.verify()
                observe.assert_not_called()
            custody.assert_called_once_with(plan, result)


class LibraryContextReplayTests(unittest.TestCase):
    def fixture(self):
        from tests.support.fixtures_test_qualification_capture import (
            QualificationCaptureTests,
        )

        fixture = QualificationCaptureTests("runTest")
        result, captures, entries = fixture.typed_product_fixture(
            library_artifact_context=True
        )
        return fixture, result, captures, entries

    def test_export_bound_contract_archive_reopens_after_transport_relocation(self):
        import hashlib

        from literate_ai.adapters.qualification_archive import (
            encode_qualification_archive,
            reopen_qualification_archive,
        )
        from literate_ai.adapters.qualification_capture import (
            reopen_qualification_products,
        )
        from literate_ai.contracts.blobs import BlobRef

        fixture, result, captures, entries = self.fixture()
        raw = encode_qualification_archive(
            entries, max_bytes=5_000_000, max_records=1000
        )
        reference = BlobRef(hashlib.sha256(raw).hexdigest(), len(raw))
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "original"
            moved = Path(directory) / "moved"
            old.mkdir()
            moved.mkdir()
            (old / "archive").write_bytes(raw)
            (old / "archive").rename(moved / "archive")
            old.rmdir()
            reader = reopen_qualification_archive(
                (moved / "archive").read_bytes(),
                reference,
                max_bytes=5_000_000,
                max_records=1000,
            )
            with patch("literate_ai.adapters.qualification.run_with_tree_kill") as run:
                for capture in captures:
                    actual = reopen_qualification_products(
                        reader,
                        qualification_identity=result.identity,
                        run_identity=capture.run.run_identity,
                        exports_identity=capture.exports.identity,
                        max_package_bytes=4096,
                        component_lock=fixture.current_lock,
                        oracle=fixture.current_oracle,
                        current_recipes=fixture.current_recipes,
                        current_commands=fixture.current_commands,
                        current_profile=fixture.current_profile,
                        current_driver=fixture.current_driver,
                    )
                    self.assertEqual(actual, capture)
                run.assert_not_called()

    def test_rehashed_wrong_missing_or_downgraded_export_binding_is_refused(self):
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceReader,
        )

        fixture, result, captures, entries = self.fixture()
        reader = QualificationEvidenceReader(
            entries, max_bytes=5_000_000, max_records=1000
        )
        parity = result.parity_evidence[1]
        case = parity.cases[0]
        for field, value in (
            ("artifact_export_identity", canonical_identity("foreign-export").uri),
            ("artifact_export_identity", None),
            ("schema", "literate-ai/qualification-process-observation@1"),
        ):
            recorder = QualificationEvidenceRecorder(
                max_bytes=5_000_000, max_records=1000
            )
            for _, payload in entries:
                recorder.remember_bytes(payload)
            document = reader.read_json(case.generated_observation_identity)
            if value is None:
                document.pop(field)
            else:
                document[field] = value
            changed_case = replace(
                case, generated_observation_identity=recorder.remember_json(document)
            )
            recorder.remember_json(changed_case.to_dict())
            changed_parity = replace(parity, cases=(changed_case, *parity.cases[1:]))
            recorder.remember_json(changed_parity.to_dict())
            changed_run = replace(
                result.runs[1], parity_evidence_identity=changed_parity.identity
            )
            recorder.remember_json(changed_run.to_dict())
            changed_result = replace(
                result,
                runs=(result.runs[0], changed_run),
                parity_evidence=(result.parity_evidence[0], changed_parity),
            )
            recorder.remember_json(changed_result.to_dict())
            with self.subTest(field=field, value=value):
                fixture.assert_product_records_refused(
                    changed_result, captures, recorder.entries, "parity-record-mismatch"
                )

    def test_archive_cannot_supply_its_own_current_lock_authority(self):
        from literate_ai.adapters.qualification_capture import (
            QualificationCaptureError,
            QualificationEvidenceReader,
            verify_qualification_parity,
        )

        fixture, result, _, entries = self.fixture()
        with self.assertRaisesRegex(
            QualificationCaptureError, "parity-library-authority-invalid"
        ):
            verify_qualification_parity(
                QualificationEvidenceReader(
                    entries, max_bytes=5_000_000, max_records=1000
                ),
                result=result,
                profile=fixture.current_profile,
            )


class LibraryRootSelectionTests(unittest.TestCase):
    def test_valid_nonlibrary_root_cannot_be_used_as_a_library_context(self):
        from literate_ai.adapters.qualification_authority import (
            qualification_root_library_export,
        )
        from tests.support.fixtures_test_qualification_capture import (
            fixture,
            root_records,
        )

        run, original, _, _ = fixture()
        export = replace(original.libraries[0].artifact_export, role="runtime")
        graph_fixture_object = graph_fixture.ArtifactGraphTests()
        graph = create_artifact_build_graph(
            build_system_driver_identity=graph_fixture_object.driver,
            manifests=(graph_fixture_object.manifest(export),),
            link_roots=(export.identity,),
        )
        _, documents = root_records(
            run,
            SimpleNamespace(
                graph=graph,
                link_plan=graph.link_plans[0],
                libraries=(SimpleNamespace(artifact_export=export),),
            ),
        )
        root = StandardRootIntegrationEvidence.from_dict(documents[1])
        with self.assertRaisesRegex(ValueError, "export_mismatch"):
            qualification_root_library_export(
                root,
                component_lock_identity=run.component_lock_identity,
                root_revision=export.component_revision,
                target_identity=run.target_profile_identity,
            )
