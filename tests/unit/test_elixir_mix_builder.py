"""Native Mix cannot start without exact build, source and dependency authority."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from literate_ai.adapters.builders import (
    BuildError,
    GuardedMixBuilder,
    MixProviderLibrary,
)
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.builders.elixir import ElixirToolchain
from literate_ai.adapters.builders.hex import HexToolchain
from literate_ai.adapters.builders.mix import MixToolchain
from literate_ai.adapters.builders.mix_project import (
    _beam_paths,
    _files,
    _require_metadata,
)
from literate_ai.adapters.builders.python import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    canonical_tree_digest,
)
from literate_ai.adapters.dependencies.mix_lock import (
    MixLock,
    MixLockedDependency,
    MixLockedPackage,
)
from literate_ai.adapters.dependencies.types import DependencyObservationError
from literate_ai.contracts.mix_projects import MixDependencyIntent, MixProjectIntent
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
)

_NOW = datetime(2026, 10, 6, tzinfo=UTC)
_DIGEST = "sha256:" + "a" * 64


class GuardedMixBuilderTests(unittest.TestCase):
    def test_native_inventory_uses_relative_posix_string_order(self):
        names = ("a/file", "a.bin", "Z.beam", "app.app", "app.beam")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
            self.assertEqual(list(_files(root)), sorted(names))
            self.assertEqual(
                [path.name for path in _beam_paths(root)], ["Z.beam", "app.beam"]
            )

    def test_library_surface_requires_its_exact_request_binding(self):
        from tests.unit.test_elixir_mix_providers import _surface

        request, grant = self.authority()
        builder = GuardedMixBuilder(self.tool, AuthorizationRevocationSet())
        with mock.patch(
            "literate_ai.adapters.builders.mix_project.run_bounded_process"
        ) as process:
            with self.assertRaises(TypeError):
                builder.build(
                    request,
                    grant,
                    source_root=self.source,
                    artifact_store=self.root / "artifacts",
                    now=_NOW,
                    library_import_surface={},
                )
            with self.assertRaises(BuildError) as caught:
                builder.build(
                    request,
                    grant,
                    source_root=self.source,
                    artifact_store=self.root / "artifacts",
                    now=_NOW,
                    library_import_surface=_surface(),
                )
            self.assertEqual(caught.exception.code, "builder.mix_authority_mismatch")
            process.assert_not_called()
        self.assertNotEqual(
            GuardedMixBuilder.builder_id,
            GuardedMixBuilder.provider_bound_builder_id(
                [], library_import_surface=_surface().to_dict()
            ),
        )

    def test_untyped_provider_is_rejected_before_native_execution(self):
        request, grant = self.authority()
        with self.assertRaises(TypeError):
            GuardedMixBuilder(self.tool, AuthorizationRevocationSet()).build(
                request,
                grant,
                source_root=self.source,
                artifact_store=self.root / "artifacts",
                now=_NOW,
                provider_libraries=({},),
            )

    def test_provider_requires_exact_acknowledged_input_binding(self):
        from tests.unit.test_elixir_mix_providers import _surface

        ebin = self.root / "provider"
        ebin.mkdir()
        (ebin / "Elixir.ProviderApi.Math.beam").write_bytes(b"fixture")
        from literate_ai.contracts import canonical_identity

        provider = MixProviderLibrary(
            canonical_identity("provider-fixture"),
            _surface(),
            ebin,
            canonical_tree_digest(ebin),
        )
        request, grant = self.authority()
        with self.assertRaises(BuildError) as caught:
            GuardedMixBuilder(self.tool, AuthorizationRevocationSet()).build(
                request,
                grant,
                source_root=self.source,
                artifact_store=self.root / "artifacts",
                now=_NOW,
                provider_libraries=(provider,),
            )
        self.assertEqual(caught.exception.code, "builder.mix_authority_mismatch")
        bound = GuardedMixBuilder.provider_bound_builder_id([provider.to_dict()])
        self.assertNotEqual(bound, GuardedMixBuilder.builder_id)
        self.assertNotEqual(
            bound,
            GuardedMixBuilder.provider_bound_builder_id(
                [replace(provider, import_surface=_surface(("missing",))).to_dict()]
            ),
        )
        request, grant = self.authority(builder_id=bound)
        (ebin / "Elixir.ProviderApi.Math.beam").write_bytes(b"changed")
        with self.assertRaises(BuildError) as caught:
            GuardedMixBuilder(
                self.tool, AuthorizationRevocationSet(), clock=lambda: _NOW
            ).build(
                request,
                grant,
                source_root=self.source,
                artifact_store=self.root / "artifacts",
                now=_NOW,
                provider_libraries=(provider,),
            )
        self.assertEqual(caught.exception.code, "builder.mix_provider_changed")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "generated"
        package = self.source / "source"
        package.mkdir(parents=True)
        intent = MixProjectIntent(
            "fixture",
            "1.0.0",
            "Native Mix fixture",
            ("MIT",),
            (MixDependencyIntent("decimal", "== 2.3.0"),),
            links=(("Source", "https://example.invalid/fixture"),),
        )
        (package / "mix-project.json").write_text(json.dumps(intent.to_dict()))
        (package / "main.exs").write_text("IO.puts(:fixture)")
        (package / ".literate").mkdir()
        (package / ".literate/sbom.cdx.json").write_text(
            '{"components":[{"name":"decimal"}]}'
        )
        elixir = ElixirToolchain(
            (str(self.root / "elixir"),),
            "1.18.0",
            (1, 18, 0),
            "27",
            ((str(self.root / "elixir"), _DIGEST),),
        )
        mix = MixToolchain(
            elixir,
            elixir.version,
            tuple((str(self.root / name), _DIGEST) for name in ("mix", "a", "b", "c")),
        )
        ebin = self.root / "ebin"
        self.tool = HexToolchain(
            mix,
            str(ebin),
            "2.5.1",
            tuple((str(ebin / f"file-{n}.beam"), _DIGEST) for n in range(11)),
        )
        process = mock.patch(
            "literate_ai.adapters.builders.mix_project.run_bounded_process"
        )
        self.process = process.start()
        self.addCleanup(process.stop)
        self.expected_calls = 0
        self.addCleanup(
            lambda: self.assertEqual(self.process.call_count, self.expected_calls)
        )

    def authority(self, **changes):
        digest = canonical_tree_digest(self.source)
        policy = SecurityPolicy(policy_digest=_DIGEST)
        classification = policy.classify(
            effective_revision_digest=_DIGEST,
            attestations=(
                OriginAttestation(
                    source_digest=digest,
                    signer="fixture",
                    trust_root="fixture",
                    signature_identity="fixture",
                    verified=True,
                ),
            ),
            findings=(),
        )
        values = dict(
            effective_revision_digest=_DIGEST,
            source_bundle_digest=digest,
            builder_id=GuardedMixBuilder.builder_id,
            toolchain_digest=self.tool.identity,
            sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
            requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
            allowed_outputs=("elixir-mix-tree",),
        )
        values.update(changes)
        request = BuildRequest(**values)
        grant = policy.authorize_build(
            classification,
            request,
            actor="fixture",
            reason="Mix authority test",
            issued_at=_NOW,
            expires_at=_NOW + timedelta(minutes=10),
            yolo_acknowledged=True,
        )
        return request, grant

    def build(self, request, grant, *, verifier=None, now=_NOW, store=None):
        return GuardedMixBuilder(self.tool, verifier, clock=lambda: now).build(
            request,
            grant,
            source_root=self.source,
            artifact_store=store or self.root / "artifacts",
            now=now,
        )

    def test_live_revocation_verifier_is_required_by_default(self):
        with self.assertRaises(AuthorizationError):
            self.build(*self.authority())

    def test_expired_and_revoked_grants_cannot_start_mix(self):
        request, grant = self.authority()
        with self.assertRaises(AuthorizationError):
            self.build(
                request,
                grant,
                verifier=AuthorizationRevocationSet(),
                now=_NOW + timedelta(hours=1),
            )
        revoked = AuthorizationRevocationSet().revoke(
            grant.authorization_id, actor="fixture", reason="revoked"
        )
        with self.assertRaises(AuthorizationError):
            self.build(request, grant, verifier=revoked)

    def test_other_tool_builder_and_output_grants_cannot_start_mix(self):
        for values in (
            {"builder_id": "other-builder"},
            {"toolchain_digest": _DIGEST},
            {"allowed_outputs": ("other-output",)},
        ):
            with self.subTest(values=values), self.assertRaises(BuildError):
                self.build(
                    *self.authority(**values), verifier=AuthorizationRevocationSet()
                )

    def test_source_drift_after_authorization_cannot_start_mix(self):
        authority = self.authority()
        (self.source / "source/main.exs").write_text("changed")
        with self.assertRaises(BuildError):
            self.build(*authority, verifier=AuthorizationRevocationSet())

    def test_generated_native_project_and_lock_are_rejected(self):
        for name in ("mix.exs", "mix.lock", ".iex.exs"):
            path = self.source / "source" / name
            path.write_text("executable or model-provided authority")
            with self.subTest(name=name), self.assertRaises(BuildError):
                self.build(*self.authority(), verifier=AuthorizationRevocationSet())
            path.unlink()

    def test_source_bom_is_required_and_covers_declared_packages(self):
        path = self.source / "source/.literate/sbom.cdx.json"
        path.unlink()
        with self.assertRaises(BuildError):
            self.build(*self.authority(), verifier=AuthorizationRevocationSet())
        path.write_text('{"components":[]}')
        with self.assertRaises(DependencyObservationError):
            self.build(*self.authority(), verifier=AuthorizationRevocationSet())

    def test_object_projection_must_be_outside_generated_source(self):
        with self.assertRaises(BuildError):
            self.build(
                *self.authority(),
                verifier=AuthorizationRevocationSet(),
                store=self.source / "objects",
            )

    def test_reparse_payload_is_rejected_before_native_execution(self):
        linked = self.source / "source/main.exs"
        with (
            mock.patch(
                "literate_ai.adapters.builders.mix_project.path_is_link_or_reparse",
                side_effect=lambda path: path == linked,
            ),
            self.assertRaisesRegex(BuildError, "linked payloads"),
        ):
            self.build(*self.authority(), verifier=AuthorizationRevocationSet())

    def ready_staged_fixture(self):
        """Only exercise authorization boundaries; this payload is not native proof."""
        ebin = Path(self.tool.ebin)
        ebin.mkdir()
        payload = b"fixture-only plugin payload"
        bindings = []
        for path, _ in self.tool.file_bindings:
            Path(path).write_bytes(payload)
            bindings.append((path, "sha256:" + hashlib.sha256(payload).hexdigest()))
        self.tool = replace(self.tool, file_bindings=tuple(bindings))
        self.expected_calls = 1

    def test_revocation_after_fetch_prevents_compilation_and_publication(self):
        self.ready_staged_fixture()
        request, grant = self.authority()
        active = AuthorizationRevocationSet()
        revoked = active.revoke(
            grant.authorization_id, actor="fixture", reason="revoked after fetch"
        )
        state = [active]

        class Verifier:
            def require_build_valid(self, authorization, build_request, *, now):
                state[0].require_build_valid(authorization, build_request, now=now)

        def fetch(*_args, **_kwargs):
            state[0] = revoked
            return BoundedProcessResult(0, b"", b"")

        self.process.side_effect = fetch
        with (
            mock.patch.object(HexToolchain, "require_unchanged"),
            self.assertRaises(AuthorizationError),
        ):
            self.build(request, grant, verifier=Verifier())
        self.assertEqual(list((self.root / "artifacts").iterdir()), [])

    def test_fetch_mutating_projection_cannot_compile_or_publish(self):
        self.ready_staged_fixture()

        def mutate(*_args, **kwargs):
            (kwargs["cwd"] / "main.exs").write_text("changed in projection")
            return BoundedProcessResult(0, b"", b"")

        self.process.side_effect = mutate
        with (
            mock.patch.object(HexToolchain, "require_unchanged"),
            self.assertRaisesRegex(BuildError, "projection changed"),
        ):
            self.build(*self.authority(), verifier=AuthorizationRevocationSet())
        self.assertEqual(list((self.root / "artifacts").iterdir()), [])

    def test_failed_native_phase_never_publishes_an_artifact(self):
        self.ready_staged_fixture()
        self.process.return_value = BoundedProcessResult(1, b"", b"native failure")
        with (
            mock.patch.object(HexToolchain, "require_unchanged"),
            self.assertRaisesRegex(BuildError, "native failure"),
        ):
            self.build(*self.authority(), verifier=AuthorizationRevocationSet())
        self.assertEqual(list((self.root / "artifacts").iterdir()), [])


class MixNativeMetadataTests(unittest.TestCase):
    def setUp(self):
        dependency = MixLockedDependency("child", "~> 2.0", True)
        self.lock = MixLock(
            (MixLockedPackage("decimal", "2.3.0", "a" * 64, "b" * 64, (dependency,)),),
            (),
        )
        self.metadata = [
            {
                "name": "decimal",
                "app": "decimal",
                "version": "2.3.0",
                "build_tools": ["mix"],
                "requirements": {
                    "child": {
                        "app": "child",
                        "requirement": "~> 2.0",
                        "optional": True,
                        "repository": "hexpm",
                    }
                },
            }
        ]

    def test_native_metadata_matches_package_and_complete_optional_graph(self):
        _require_metadata(self.metadata, self.lock)

    def test_coordinate_version_manager_and_missing_graph_disagree(self):
        for change in (
            {"name": "other"},
            {"app": "alias"},
            {"version": "9.0.0"},
            {"build_tools": ["rebar3"]},
            {"requirements": {}},
        ):
            with self.subTest(change=change), self.assertRaises(BuildError):
                _require_metadata([{**self.metadata[0], **change}], self.lock)

    def test_private_alias_missing_constraint_and_nonboolean_option_disagree(self):
        value = self.metadata[0]["requirements"]["child"]
        for change in (
            {"app": "alias"},
            {"repository": "private"},
            {"requirement": "~> 9.0"},
            {"optional": 0},
            {"optional": False},
            {"git": "outside"},
        ):
            metadata = {
                **self.metadata[0],
                "requirements": {"child": {**value, **change}},
            }
            with self.subTest(change=change), self.assertRaises(BuildError):
                _require_metadata([metadata], self.lock)


if __name__ == "__main__":
    unittest.main()
