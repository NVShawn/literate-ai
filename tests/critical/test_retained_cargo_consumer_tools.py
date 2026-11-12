"""Real consumer tools joined to explicit synthetic producer contract evidence.

The producer is the existing qualification contract fixture, not a claim of
model generation. Review reopening and complete archive verification are real;
consumer commands are discovered natively but this does not execute a workspace.
"""

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.builders.cargo import discover_cargo_toolchain
from literate_ai.adapters.builders.python import BuildError
from literate_ai.adapters.builders.rust import discover_rust_toolchain
from literate_ai.adapters.retained_cargo_current_v2 import (
    compose_retained_cargo_current_inputs_v2,
)
from literate_ai.adapters.retained_cargo_import_v2 import (
    verify_retained_cargo_archive_v2,
)
from literate_ai.adapters.retained_provider_native import RetainedProviderNative
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.projects import PinnedInputClosure
from tests.support import fixtures_test_retained_cargo_import as import_fixtures
from tests.support.fixtures_retained_cargo_scratch import scratch_review_fixture


@unittest.skipUnless(
    all(shutil.which(tool) for tool in ("cargo", "rustc", "make", "git")),
    "requires native consumer Cargo/Rust and retained Make/Git adoption",
)
class RetainedCargoConsumerToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="ctv2-"))
        self.base = Path(temporary).resolve()
        original_cargo = discover_cargo_toolchain()
        self.cargo_launcher = self.base / Path(original_cargo.command[0]).name
        shutil.copy2(original_cargo.command[0], self.cargo_launcher)
        self.cargo = discover_cargo_toolchain(pinned_command=str(self.cargo_launcher))
        self.rustc = discover_rust_toolchain()
        cargo_identity = ContentIdentity.parse_uri(self.cargo.identity)
        rustc_identity = ContentIdentity.parse_uri(self.rustc.identity)

        class ConsumerFixture(import_fixtures.RetainedCargoImportTests):
            def setUp(self):
                super().setUp()
                self.plan = replace(
                    self.plan,
                    cargo_identity=cargo_identity,
                    rustc_identity=rustc_identity,
                )

        self.reviewed, self.fixture = self.enterContext(
            scratch_review_fixture(
                self.base, with_provider=True, fixture=ConsumerFixture()
            )
        )
        inputs = self.fixture.inputs
        self.provider_file = self.base / "provider-observation"
        self.provider_file.write_bytes(b"current producer contract observation")
        custody = PinnedInputClosure(
            maximum_files=1, maximum_file_bytes=4096, maximum_total_bytes=4096
        )
        custody.pin(
            self.provider_file, boundary=self.base, label="provider-observation"
        )
        # Test-only composition of existing synthetic qualification evidence.
        # Production callers use read_retained_provider_native, never these stubs.
        provider = SimpleNamespace(
            require_unchanged=custody.require_unchanged,
            generation=SimpleNamespace(
                prepared=SimpleNamespace(
                    locked_authority_snapshot=SimpleNamespace(
                        authority=inputs.authority
                    )
                ),
                recipes=inputs.current_recipes,
            ),
            promotion=SimpleNamespace(
                evidence=inputs.promotion,
                generation_closure=inputs.current_generation_closure,
            ),
            qualification=SimpleNamespace(
                case_map=SimpleNamespace(
                    verifier_identity=inputs.current_verifier_identity
                ),
                profile=inputs.current_profile,
            ),
            lifecycle=SimpleNamespace(
                policy=SimpleNamespace(identity=inputs.current_policy_identity),
                driver=inputs.current_driver,
            ),
        )
        target = SimpleNamespace(
            component_revision=inputs.authority.lock.root_revision,
            library=True,
            build_system_toolchain_identity=canonical_identity("provider cargo"),
            language_compiler_identity=canonical_identity("provider rustc"),
        )
        toolchain = SimpleNamespace(
            require_unchanged=custody.require_unchanged,
            contracts=tuple(inputs.current_commands.values()),
            cargo_targets=(target,),
        )
        self.native = RetainedProviderNative(
            provider, toolchain, inputs.oracle, inputs.oracle.identity, custody
        )
        self.arguments = dict(
            reviewed_binding_identity=self.reviewed.binding.identity,
            configured_store_id="reviewed-store",
            consumer_cargo=self.cargo,
            consumer_rustc=self.rustc,
        )

    def compose(self, **changes):
        return compose_retained_cargo_current_inputs_v2(
            self.reviewed, self.native, **{**self.arguments, **changes}
        )

    def test_distinct_consumer_tools_reopen_exact_original_provider_archive(self):
        current = self.compose()
        self.assertEqual(
            current.current.current_cargo_identity.uri, self.cargo.identity
        )
        self.assertEqual(
            current.current.current_rustc_identity.uri, self.rustc.identity
        )
        self.assertEqual(
            current.current.current_commands, self.fixture.inputs.current_commands
        )
        self.assertNotIn(
            self.native.toolchain.cargo_targets[0].build_system_toolchain_identity,
            self.reviewed.policy.toolchains,
        )
        plan, capture = verify_retained_cargo_archive_v2(
            read_current=self.compose,
            read_archive=self.fixture.read_archive,
            max_plan_bytes=100000,
            max_archive_bytes=5_000_000,
            max_records=1000,
            max_package_bytes=4096,
        )
        self.assertEqual(plan, self.reviewed.plan)
        self.assertEqual(capture, self.fixture.products)

    def test_one_sided_or_wrong_role_tools_refuse_before_archive_read(self):
        for changes in (
            dict(consumer_cargo=None),
            dict(consumer_rustc=None),
            dict(consumer_cargo=self.rustc, consumer_rustc=self.cargo),
            dict(consumer_cargo=object()),
        ):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaisesRegex(TypeError, "consumer-tool-pair-required"):
                    self.compose(**changes)
        self.assertEqual(self.fixture.reads, [])

    def test_default_provider_tools_still_require_matching_consumer_policy(self):
        with self.assertRaisesRegex(ValueError, "gate-toolchains-mismatch"):
            self.compose(consumer_cargo=None, consumer_rustc=None)
        self.assertEqual(self.fixture.reads, [])

    def test_current_provider_drift_invalidates_joined_consumer(self):
        current = self.compose()
        self.provider_file.write_bytes(b"changed producer observation")
        with self.assertRaises(ValueError):
            current.require_unchanged()
        with self.assertRaises(ValueError):
            self.compose()
        self.assertEqual(
            self.provider_file.read_bytes(), b"changed producer observation"
        )
        self.assertEqual(self.fixture.reads, [])

    def test_actual_consumer_launcher_drift_invalidates_joined_consumer(self):
        current = self.compose()
        with self.cargo_launcher.open("ab") as stream:
            stream.write(b"changed owned consumer launcher")
        with self.assertRaises(BuildError):
            current.require_unchanged()
        with self.assertRaises(BuildError):
            self.compose()
        self.assertEqual(self.fixture.reads, [])
