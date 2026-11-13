"""Explicit authored producer records and genuine retained receiving contracts.

Generation, publication/current-plan/native projection and independent comparison
admission are injected. Archive hashes/stages, Cargo manifest attribution,
independent review reopening and consumer tool observations are real. The native
variant additionally uses actual Cargo workspace/gate/protocol execution; it does
not convert injected producer records into genuine authored qualification.
"""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
)
from literate_ai.adapters.authored_qualification_projection import (
    CurrentAuthoredQualificationProjection,
)
from literate_ai.adapters.authored_qualification_publication import (
    PublishedAuthoredQualification,
)
from literate_ai.adapters.builders.cargo import discover_cargo_toolchain
from literate_ai.adapters.builders.rust import discover_rust_toolchain
from literate_ai.adapters.qualification_archive import encode_qualification_archive
from literate_ai.adapters.qualification_capture import QualificationEvidenceReader
from literate_ai.adapters.retained_authored_cargo_import_v2 import (
    RetainedAuthoredCargoCurrentInputsV2,
)
from literate_ai.adapters.retained_authored_provider import RetainedAuthoredProvider
from literate_ai.contracts.authored_qualification_reference import (
    AuthoredQualificationReference,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from tests.critical import test_authored_qualification_reopening as authored
from tests.support import fixtures_test_retained_cargo_import as source
from tests.support.fixtures_retained_cargo_native_v2 import NativeReceivingFixture
from tests.support.fixtures_retained_cargo_scratch import scratch_review_fixture


@contextmanager
def authored_seed_provider(seed, *, environment=None):
    """Attach an explicit authored record fixture to already prepared package bytes.

    This helper is a test fixture port, not open_retained_authored_provider and
    never a production owner factory. All real receiving checks remain enabled.
    """
    cargo = discover_cargo_toolchain(environment)
    rustc = discover_rust_toolchain(environment)
    fixture = authored.AuthoredQualificationReopeningTests("runTest")
    original = authored.legacy.QualificationCaptureTests.typed_product_fixture

    def cargo_products(owner, **kwargs):
        return original(owner, artifact_payloads=seed.artifact_payloads, **kwargs)

    try:
        with patch.object(
            authored.legacy.QualificationCaptureTests,
            "typed_product_fixture",
            cargo_products,
        ):
            fixture.setUp()
        reopened = fixture.reopen()
        run = fixture.result.runs[0]
        exports = dict(reopened.membership.exports)[run.request.run_identity]
        assert exports == seed.products.exports
        archive = encode_qualification_archive(
            fixture.recorder.entries, max_bytes=5_000_000, max_records=3000
        )
        reference = AuthoredQualificationReference(
            fixture.plan.origin,
            fixture.plan.identity,
            fixture.result.identity,
            fixture.products.identity,
            canonical_identity("fixture acknowledged publication intent"),
            canonical_identity("fixture publication stage"),
            source.blob(archive, "application/zip"),
            "1" * 32,
        )
        seed.plan = replace(
            seed.plan,
            cargo_identity=ContentIdentity.parse_uri(cargo.identity),
            rustc_identity=ContentIdentity.parse_uri(rustc.identity),
        )
        seed.binding = replace(
            seed.binding,
            qualification_archive=reference.archive,
            qualification_identity=reference.qualification_identity,
            run_identity=run.request.run_identity,
            verifier_identity=fixture.plan.oracle_set_identity,
            policy_identity=fixture.plan.lifecycle_policy_identity,
            exports=exports,
        )
        state = SimpleNamespace(active=True, reads=[])

        def current_guard(*_args):
            if not state.active:
                raise ValueError("fixture original authored owner expired")

        projection = CurrentAuthoredQualificationProjection(
            fixture.current,
            fixture.fixture.current_recipes,
            fixture.fixture.current_commands,
            current_guard,
        )
        provider = RetainedAuthoredProvider(
            fixture.current,
            PublishedAuthoredQualification(reference, "fixture", "fixture"),
            projection,
            QualificationEvidenceReader(
                fixture.recorder.entries, max_bytes=5_000_000, max_records=3000
            ),
            current_guard,
        )
        comparisons = {
            r.request.run_identity: r.comparison for r in fixture.result.runs
        }

        def compare(_current, *, run_identity, **_kwargs):
            current_guard()
            return comparisons[run_identity]

        def current(reviewed):
            return RetainedAuthoredCargoCurrentInputsV2(
                reviewed,
                provider,
                reviewed.binding.identity,
                reviewed.binding.common.source_store_id,
                cargo,
                rustc,
                reviewed.plan.common.gates,
            )

        def read_archive(store, blob, maximum):
            assert store == seed.binding.source_store_id
            assert blob == reference.archive and len(archive) <= maximum
            state.reads.append(blob)
            return archive

        with (
            patch.object(
                CurrentAuthoredQualificationPlan, "require_unchanged", current_guard
            ),
            patch(
                "literate_ai.adapters.authored_qualification_runs.compare_authored_qualification",
                side_effect=compare,
            ),
        ):
            yield SimpleNamespace(
                current=current,
                read_archive=read_archive,
                state=state,
                run=run,
                exports=exports,
                reference=reference,
                source_archive=seed.archive,
            )
    finally:
        fixture.doCleanups()


@contextmanager
def authored_cargo_fixture(base):
    seed = source.RetainedCargoImportTests("runTest")
    seed.setUp()
    try:
        with authored_seed_provider(seed) as provider:
            # Keep the prepared seed, rather than rebuilding source-derived
            # inputs over the authored reference before independent review.
            seed.setUp = lambda: None
            with scratch_review_fixture(base, fixture=seed) as reviewed:
                workspace = reviewed.project.root / reviewed.plan.common.workspace_root
                (workspace / "Cargo.toml").write_bytes(b"workspace")
                (workspace / "Cargo.lock").write_bytes(b"lock")
                yield SimpleNamespace(
                    reviewed=reviewed,
                    current=provider.current(reviewed),
                    read_archive=provider.read_archive,
                    state=provider.state,
                    run=provider.run,
                    exports=provider.exports,
                    source_archive=provider.source_archive,
                    calls=dict(
                        read_current=lambda: provider.current(reviewed),
                        read_archive=provider.read_archive,
                        max_plan_bytes=100000,
                        max_archive_bytes=5_000_000,
                        max_records=3000,
                        max_package_bytes=4096,
                    ),
                )
    finally:
        seed.doCleanups()


class AuthoredNativeReceivingFixture(NativeReceivingFixture):
    """Actual native workspace, with the same visibly injected producer boundary."""

    def setUp(self):
        super().setUp()
        self.authored = self.enterContext(
            authored_seed_provider(self, environment=self.environment)
        )
        self.calls = {
            **self.calls,
            "read_archive": self.authored.read_archive,
            "max_records": 3000,
        }
