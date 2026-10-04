"""Shared test fixtures extracted from test_native_sdk_source_build."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.native_sdk_custody import materialize_native_sdk
from literate_ai.adapters.native_sdk_recipes import select_native_sdk_recipes
from literate_ai.adapters.native_sdk_source_build import NativeSdkSourceBuildService
from literate_ai.adapters.source.repository_cache import (
    RepositorySourceCachePolicyError,
)
from literate_ai.application.repository_sources import RepositorySourceResolutionError
from literate_ai.contracts import SourceIntelligenceMode
from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import PinnedInputClosureError
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    SecurityPolicy,
)
from literate_ai.sources import QuarantineStore
from literate_ai.storage import BlobRef, ReferenceIndex
from tests.support import fixtures_test_native_sdk_recipes as test_native_sdk_recipes
from tests.support.fixtures_test_repository_sources import source_intelligence_policy


class NativeSdkSourceBuildTests(unittest.TestCase):
    def setUp(self):
        self.recipe_fixture = test_native_sdk_recipes.NativeSdkRecipeTests(
            "test_selected_flavor_recipe_drives_real_native_build_and_relocation"
        )
        self.addCleanup(self.recipe_fixture.doCleanups)
        self.recipe_fixture.entrypoint_kind = getattr(self, "entrypoint_kind", None)
        self.recipe_fixture.setUp()
        self.fixture = self.recipe_fixture.fixture
        configure = getattr(self, "configure_recipe_fixture", None)
        if configure is not None:
            configure(self.recipe_fixture)
        self.snapshot = self.recipe_fixture.snapshot()
        (self.selection,) = select_native_sdk_recipes(
            self.snapshot,
            next(
                node.revision.identity
                for node in self.snapshot.authority.lock.nodes
                if node.revision.repository_sources
            ),
        )
        self.quarantine = QuarantineStore(
            self.fixture.root / "quarantine", self.fixture.store
        )
        self.references = ReferenceIndex(
            self.fixture.root / "references", self.fixture.store
        )
        self.revocations = AuthorizationRevocationSet()

    def service(self, **overrides):
        return NativeSdkSourceBuildService(
            **{
                "snapshot": self.snapshot,
                "selection": self.selection,
                "tools": self.fixture.tools,
                "store": self.fixture.store,
                "quarantine": self.quarantine,
                "source_intelligence_policy": source_intelligence_policy(
                    "none", SourceIntelligenceMode.OFF
                ),
                "security_policy": SecurityPolicy(canonical_identity("policy").uri),
                "revocations": lambda: self.revocations,
                "actor": "native-fixture-operator",
                "reason": "Build the isolated native SDK qualification fixture",
                "environment": dict(os.environ),
                "host_build_acknowledged": True,
                "references": self.references,
                **overrides,
            }
        )

    def assert_not_admitted(self):
        self.assertFalse(self.references.path.exists())
        self.assertEqual(list(self.quarantine.ready_root.iterdir()), [])
        self.assertEqual(list(self.quarantine.quarantine_root.iterdir()), [])

    @contextmanager
    def refuses(self, cause_type, message):
        with self.assertRaises(RepositorySourceResolutionError) as raised:
            yield
        cause = raised.exception.__cause__
        self.assertIsInstance(cause, cause_type)
        self.assertRegex(str(cause), message)

    def test_real_policy_build_cache_and_relocated_native_execution(self):
        service = self.service()
        built = service.build()
        resolution = built.resolution
        product = built.product
        self.assertEqual(product.verification, resolution.build_verification)
        self.assertEqual(
            product.snapshot.source_lock_identity, self.selection.source_lock.identity
        )
        self.assertEqual(
            self.fixture.manifest(resolution.index_binding.identity),
            resolution.index_binding.to_dict(),
        )
        inventory = self.fixture.manifest(resolution.index_binding.index)
        self.assertFalse(inventory["semantic_graph"])
        scan = self.fixture.store.get_manifest(BlobRef.from_dict(inventory["scan"]))
        self.assertTrue(scan["module_digests"])
        classification = self.fixture.manifest(resolution.build_approval.classification)
        self.assertEqual(
            classification["source_digests"], [resolution.lock.source_snapshot.uri]
        )
        grant = self.fixture.manifest(resolution.build_approval.authorization)
        self.assertEqual(grant["actor"], "native-fixture-operator")
        cache = self.fixture.manifest(resolution.cache_record)
        self.assertEqual(cache["admission"], resolution.admission.to_dict())
        reference = self.references.resolve(
            "repository-source", resolution.admission.cache_key.digest
        )
        self.assertEqual(reference.target.identity, resolution.cache_record.uri)
        cached_source = (
            self.quarantine.quarantine_root
            / resolution.lock.source_snapshot.digest
            / "tree"
        )
        self.assertEqual(
            (cached_source / "vendor_math.c").read_bytes(),
            (self.fixture.source / "vendor_math.c").read_bytes(),
        )

        # The acquired checkout has already been deleted. Remove the origin too.
        self.fixture.remove_source()
        parent = self.fixture.root / "consumer"
        parent.mkdir()
        sdk = materialize_native_sdk(
            product.snapshot,
            expected_identity=product.snapshot.identity,
            store=self.fixture.store,
            parent=parent,
        )
        executed = subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys;sys.path.insert(0,sys.argv[1]);import vendor_math;"
                "print(vendor_math.scale(1.5,3))",
                str(sdk / product.snapshot.import_root),
            ),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(executed.returncode, 0, executed.stderr)
        self.assertEqual(executed.stdout.strip(), "4.5")
        with self.assertRaisesRegex(ValueError, "single-use"):
            service.build()
        forged = dataclasses.replace(
            product.verification, build_plan=canonical_identity("different-plan")
        )
        with self.assertRaisesRegex(ValueError, "verified"):
            service.builder.product(forged)

    def test_missing_host_acknowledgement_refuses_before_any_command(self):
        service = self.service(host_build_acknowledged=False)
        with patch("literate_ai.adapters.native_sdk_build.run_bounded_process") as run:
            with self.refuses(AuthorizationError, "security.privilege_not_permitted"):
                service.build()
            run.assert_not_called()
        self.assert_not_admitted()

    def test_source_changed_after_configure_stops_compile_and_admission(self):
        service = self.service()
        commands = []

        def run_and_change_source(command, **kwargs):
            result = run_bounded_process(command, **kwargs)
            commands.append(command)
            self.assertEqual(result.returncode, 0, result.stderr)
            source = kwargs["cwd"] / "vendor_math.c"
            source.write_bytes(
                source.read_bytes() + b"\n/* changed after inspection */\n"
            )
            return result

        with patch(
            "literate_ai.adapters.native_sdk_build.run_bounded_process",
            side_effect=run_and_change_source,
        ):
            with self.refuses(
                PinnedInputClosureError, "changed.*sdk-source:vendor_math.c"
            ):
                service.build()
        self.assertEqual(len(commands), 1)
        self.assert_not_admitted()

    def test_live_revocation_after_configure_stops_compile_and_admission(self):
        service = self.service()
        commands = []

        def run_and_revoke(command, **kwargs):
            result = run_bounded_process(command, **kwargs)
            commands.append(command)
            self.assertEqual(result.returncode, 0, result.stderr)
            for grant in service._grants.values():
                self.revocations = self.revocations.revoke(
                    grant.authorization_id, actor="operator", reason="Stop this build"
                )
            return result

        with patch(
            "literate_ai.adapters.native_sdk_build.run_bounded_process",
            side_effect=run_and_revoke,
        ):
            with self.refuses(AuthorizationError, "security.authorization_revoked"):
                service.build()
        self.assertEqual(len(commands), 1)
        self.assertIn("-S", commands[0])
        self.assert_not_admitted()

    def test_invalid_live_revocation_state_refuses_before_any_command(self):
        service = self.service(revocations=lambda: None)
        with patch("literate_ai.adapters.native_sdk_build.run_bounded_process") as run:
            with self.refuses(AuthorizationError, "live_revocation_verifier_invalid"):
                service.build()
            run.assert_not_called()
        self.assert_not_admitted()

    def test_required_unavailable_intelligence_refuses_service_composition(self):
        with self.assertRaises(RepositorySourceCachePolicyError):
            self.service(
                source_intelligence_policy=source_intelligence_policy(
                    "codegraph", SourceIntelligenceMode.REQUIRED
                )
            )
        self.assert_not_admitted()

    def test_changed_recipe_plan_refuses_even_with_real_source_inspection(self):
        service = self.service()
        original = service.authorize

        def changed_plan(lock, index, plan):
            command = dataclasses.replace(plan.commands[0], argv=("cmake", "--version"))
            return original(lock, index, dataclasses.replace(plan, commands=(command,)))

        with patch.object(service, "authorize", side_effect=changed_plan):
            with patch(
                "literate_ai.adapters.native_sdk_build.run_bounded_process"
            ) as run:
                with self.refuses(ValueError, "exact authored recipe"):
                    service.build()
                run.assert_not_called()
        self.assert_not_admitted()
