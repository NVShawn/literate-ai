"""Source-provider custody and live consumer authority around native compilation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.builders.elixir import ElixirToolchain
from literate_ai.adapters.builders.hex import HexToolchain
from literate_ai.adapters.builders.mix_project import MixProviderLibrary
from literate_ai.adapters.builders.mix_provider_sources import (
    MixProviderSourceLibrary,
    _compile_mix_provider_source,
)
from literate_ai.adapters.builders.python import BuildError, canonical_tree_digest
from literate_ai.contracts import canonical_identity
from literate_ai.security import AuthorizationError, AuthorizationRevocationSet
from tests.unit.test_elixir_mix_providers import _surface
from tests.unit.test_elixir_standard_mix import _system


class MixProviderSourceCustodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        source = self.root / "provider"
        source.mkdir()
        (source / "math.ex").write_bytes(
            b"defmodule ProviderApi.Math do\n def add(a,b), do: a+b\nend\n"
        )
        self.source = MixProviderSourceLibrary(
            canonical_identity("source-provider-fixture"),
            _surface(),
            source,
            canonical_tree_digest(source),
        )
        for cls in (HexToolchain, ElixirToolchain):
            patch = mock.patch.object(cls, "require_unchanged")
            patch.start()
            self.addCleanup(patch.stop)
        self.ports, self.plan, _, self.authorization, _ = _system(
            self.root / "consumer", verifier=AuthorizationRevocationSet()
        )

    def compile(self, dependencies=()):
        return _compile_mix_provider_source(
            self.source,
            toolchain=self.ports.mix_targets[
                self.plan.component_revision.uri
            ].hex_toolchain,
            workspace=self.root / "compiled",
            require_authority=lambda: self.ports._require_mix_authority(self.plan),
            dependencies=dependencies,
        )

    def test_dependency_probe_cannot_replace_its_input_manifest(self):
        ebin = self.root / "dependency" / "ebin"
        ebin.mkdir(parents=True)
        (ebin / "Elixir.ProviderApi.Math.beam").write_bytes(b"unit fixture bytes")
        dependency = MixProviderLibrary(
            canonical_identity("compiled-dependency-fixture"),
            _surface(),
            ebin,
            canonical_tree_digest(ebin),
        )

        def replace_inventory(*args, **kwargs):
            (self.root / "compiled/dependency-input.json").write_bytes(b"{}")
            return BoundedProcessResult(0, b"[]", b"")

        with mock.patch(
            "literate_ai.adapters.builders.mix_provider_sources.run_bounded_process",
            side_effect=replace_inventory,
        ) as process:
            with self.assertRaises(BuildError) as caught:
                self.compile((dependency,))
        self.assertEqual(caught.exception.code, "builder.mix_provider_changed")
        self.assertEqual(process.call_count, 1)
        dependency.require_unchanged()

    def test_source_drift_is_rejected_before_native_execution(self):
        (self.source.source_tree / "math.ex").write_bytes(b"changed")
        with mock.patch(
            "literate_ai.adapters.builders.mix_provider_sources.run_bounded_process"
        ) as process:
            with self.assertRaises(BuildError):
                self.compile()
            process.assert_not_called()

    def test_source_drift_during_compilation_refuses_output(self):
        def mutate(*args, **kwargs):
            (self.source.source_tree / "math.ex").write_bytes(b"changed")
            return BoundedProcessResult(0, b"[]", b"")

        with mock.patch(
            "literate_ai.adapters.builders.mix_provider_sources.run_bounded_process",
            side_effect=mutate,
        ):
            with self.assertRaises(BuildError) as caught:
                self.compile()
        self.assertEqual(caught.exception.code, "builder.mix_provider_changed")

    def test_revoked_consumer_grant_refuses_compilation_output(self):
        def revoke(*args, **kwargs):
            self.ports.mix_authorization_verifier = AuthorizationRevocationSet().revoke(
                self.authorization.grant.authorization_id,
                actor="test",
                reason="revoked during provider compilation",
            )
            return BoundedProcessResult(0, b"[]", b"")

        with mock.patch(
            "literate_ai.adapters.builders.mix_provider_sources.run_bounded_process",
            side_effect=revoke,
        ):
            with self.assertRaises(AuthorizationError):
                self.compile()

    def test_compiler_cannot_replace_its_input_manifest(self):
        def mutate(*args, **kwargs):
            (self.root / "compiled/input.json").write_bytes(b"{}")
            return BoundedProcessResult(0, b"[]", b"")

        with mock.patch(
            "literate_ai.adapters.builders.mix_provider_sources.run_bounded_process",
            side_effect=mutate,
        ):
            with self.assertRaises(BuildError) as caught:
                self.compile()
        self.assertEqual(caught.exception.code, "builder.mix_provider_changed")
