"""Native Mix cannot start without exact build, source and dependency authority."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
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
_CONTENTS = {
    "mix.exs": b"defmodule Decimal.MixProject do\nend\n",
    "lib/decimal.ex": b"defmodule Decimal do\nend\n",
}


def _hex_package(contents=None, *, links=()):
    """A synthetic Hex v3 archive; checksums are computed, never trusted."""
    inner_stream = io.BytesIO()
    with tarfile.open(fileobj=inner_stream, mode="w:gz") as tar:
        for name, content in (contents or _CONTENTS).items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    files = {
        "VERSION": b"3",
        "metadata.config": b'{<<"name">>,<<"decimal">>}.\n',
        "contents.tar.gz": inner_stream.getvalue(),
    }
    inner = hashlib.sha256(b"".join(files.values())).hexdigest()
    files["CHECKSUM"] = inner.upper().encode()
    outer_stream = io.BytesIO()
    with tarfile.open(fileobj=outer_stream, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    content = outer_stream.getvalue()
    return content, inner, hashlib.sha256(content).hexdigest()


def _exact_bom(inner, outer, *, version="2.3.0"):
    return json.dumps(
        {
            "components": [
                {
                    "bom-ref": "hex-decimal",
                    "name": "decimal",
                    "purl": "pkg:hex/decimal@" + version,
                    "version": version,
                    "hashes": [{"alg": "SHA-256", "content": outer}],
                    "properties": [
                        {"name": "literate-ai:mix-inner-sha256", "value": inner}
                    ],
                }
            ]
        }
    )


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
        from tests.support.fixtures_test_elixir_mix_providers import _surface

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
        from tests.support.fixtures_test_elixir_mix_providers import _surface

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
        self.intent = intent = MixProjectIntent(
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
        self.archive, inner, outer = _hex_package()
        (package / ".literate/sbom.cdx.json").write_text(_exact_bom(inner, outer))
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

    def test_version_range_bom_cannot_start_any_native_process(self):
        self.ready_staged_fixture()
        self.expected_calls = 0
        path = self.source / "source/.literate/sbom.cdx.json"
        path.write_text(
            json.dumps(
                {
                    "components": [
                        {
                            "bom-ref": "hex-decimal",
                            "name": "decimal",
                            "purl": "pkg:hex/decimal",
                            "versionRange": "vers:hex/>=2.3.0|<3.0.0",
                        }
                    ]
                }
            )
        )
        with (
            mock.patch.object(HexToolchain, "require_unchanged"),
            self.assertRaises(DependencyObservationError) as caught,
        ):
            self.build(*self.authority(), verifier=AuthorizationRevocationSet())
        self.assertEqual(caught.exception.code, "dependencies.mix-pins-required")
        self.assertFalse((self.root / "artifacts").exists())

    def fetch(self, *, contents=None, extra=None, lock=None, link=None):
        """Fake ``deps.get``: record the lock Mix saw, then cache and extract."""
        seen = []

        def run(argv, **kwargs):
            if argv[-1] != "deps.get":
                return BoundedProcessResult(1, b"", b"stop after admission")
            seen.append((kwargs["cwd"] / "mix.lock").read_bytes())
            environment = kwargs["environment"]
            cache = Path(environment["HEX_HOME"]) / "packages/hexpm"
            cache.mkdir(parents=True)
            (cache / "decimal-2.3.0.tar").write_bytes(self.archive)
            deps = Path(environment["MIX_DEPS_PATH"]) / "decimal"
            for name, content in {
                **(contents or _CONTENTS),
                ".hex": b"hex bookkeeping",
                **(extra or {}),
            }.items():
                target = deps / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            if link is not None:
                os.symlink(self.root / "outside", deps / link)
            if lock is not None:
                (kwargs["cwd"] / "mix.lock").write_bytes(lock)
            return BoundedProcessResult(0, b"", b"")

        self.process.side_effect = run
        return seen

    def test_deps_get_runs_against_the_rendered_admitted_lock(self):
        from literate_ai.adapters.dependencies.mix_lock import render_mix_lock

        self.ready_staged_fixture()
        self.expected_calls = 2
        seen = self.fetch()
        with (
            mock.patch.object(HexToolchain, "require_unchanged"),
            self.assertRaisesRegex(BuildError, "stop after admission"),
        ):
            self.build(*self.authority(), verifier=AuthorizationRevocationSet())
        lock = MixLock.from_bytes(seen[0], project=self.intent)
        package = lock.packages[0]
        self.assertEqual(
            seen[0],
            render_mix_lock(
                (
                    MixLockedPackage(
                        "decimal",
                        "2.3.0",
                        package.inner_sha256,
                        hashlib.sha256(self.archive).hexdigest(),
                        (),
                    ),
                )
            ),
        )
        # The first native phase after acquisition is the inert archive probe.
        self.assertEqual(
            Path(self.process.call_args_list[1].args[0][-1]).name,
            "dependency-probe.json",
        )

    def test_lock_drift_during_fetch_is_refused_before_any_other_phase(self):
        self.ready_staged_fixture()
        self.fetch(
            lock=b'%{"decimal": {:hex, :decimal, "2.3.1", "'
            + b"a" * 64
            + b'", [:mix], [], "hexpm", "'
            + b"b" * 64
            + b'"}}'
        )
        with (
            mock.patch.object(HexToolchain, "require_unchanged"),
            self.assertRaises(DependencyObservationError) as caught,
        ):
            self.build(*self.authority(), verifier=AuthorizationRevocationSet())
        self.assertEqual(caught.exception.code, "dependencies.mix-lock-not-admitted")

    def test_extracted_tree_is_replaced_by_verified_archive_contents(self):
        self.ready_staged_fixture()
        self.expected_calls = 2
        observed = {}
        self.fetch()
        fetch = self.process.side_effect

        def run(argv, **kwargs):
            if argv[-1] != "deps.get":
                deps = Path(kwargs["cwd"]) / "deps/decimal"
                observed.update(_files(deps))
            return fetch(argv, **kwargs)

        self.process.side_effect = run
        with (
            mock.patch.object(HexToolchain, "require_unchanged"),
            mock.patch(
                "literate_ai.adapters.builders.mix_project.shutil.rmtree",
                wraps=__import__("shutil").rmtree,
            ) as removed,
            self.assertRaisesRegex(BuildError, "stop after admission"),
        ):
            self.build(*self.authority(), verifier=AuthorizationRevocationSet())
        self.assertEqual(observed, {**_CONTENTS, ".hex": b"hex bookkeeping"})
        self.assertTrue(
            any(call.args[0].name == "decimal" for call in removed.call_args_list)
        )

    def test_changed_extra_or_linked_extraction_is_refused_before_compile(self):
        for change in (
            {"contents": {**_CONTENTS, "lib/decimal.ex": b"tampered"}},
            {"extra": {"lib/injected.ex": b"defmodule Injected do end"}},
            {"link": "lib/linked.ex"},
        ):
            with self.subTest(change=sorted(change)):
                self.process.reset_mock()
                if not Path(self.tool.ebin).exists():
                    self.ready_staged_fixture()
                self.expected_calls = 1
                self.fetch(**change)
                with (
                    mock.patch.object(HexToolchain, "require_unchanged"),
                    self.assertRaises(BuildError) as caught,
                ):
                    self.build(*self.authority(), verifier=AuthorizationRevocationSet())
                self.assertIn(
                    caught.exception.code,
                    {
                        "builder.mix_dependency_tree_changed",
                        "builder.mix_payload_invalid",
                    },
                )
                self.assertEqual(self.process.call_count, 1)

    def test_source_projection_preserves_links_for_refusal(self):
        self.ready_staged_fixture()
        self.expected_calls = 0
        link = self.source / "source/linked.exs"
        real_copytree = __import__("shutil").copytree

        def racing_copy(source, destination, *args, **kwargs):
            if Path(destination).name != "projection":
                return real_copytree(source, destination, *args, **kwargs)
            os.symlink(self.source / "source/main.exs", link)
            try:
                return real_copytree(source, destination, *args, **kwargs)
            finally:
                link.unlink()

        with (
            mock.patch.object(HexToolchain, "require_unchanged"),
            mock.patch(
                "literate_ai.adapters.builders.mix_project.shutil.copytree",
                side_effect=racing_copy,
            ),
            self.assertRaisesRegex(BuildError, "linked payloads"),
        ):
            self.build(*self.authority(), verifier=AuthorizationRevocationSet())


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
