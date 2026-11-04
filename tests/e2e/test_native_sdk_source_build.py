"""Real SDK composition: only the Git transport uses a local fixture substitute."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.native_sdk_custody import materialize_native_sdk
from literate_ai.adapters.native_sdk_recipes import select_native_sdk_recipes
from literate_ai.adapters.native_sdk_source_build import NativeSdkSourceBuildService
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
    source_payload_bytes = 144 * 1024 * 1024

    @classmethod
    def setUpClass(cls):
        # The recipe fixture (vendor Git repository plus locked component) is
        # read-only input for both tests; build it once. Each test still gets
        # its own quarantine, references and revocations, and runs its own
        # real build from a fresh checkout.
        recipe_fixture = test_native_sdk_recipes.NativeSdkRecipeTests(
            "test_selected_flavor_recipe_drives_real_native_build_and_relocation"
        )
        cls.addClassCleanup(recipe_fixture.doCleanups)
        recipe_fixture.source_payload_bytes = cls.source_payload_bytes
        recipe_fixture.setUp()
        cls.fixture = recipe_fixture.fixture
        cls.snapshot = recipe_fixture.snapshot()
        cls.origin_template = cls.fixture.root / "origin-template"
        shutil.copytree(cls.fixture.source, cls.origin_template, symlinks=True)
        (cls.selection,) = select_native_sdk_recipes(
            cls.snapshot,
            next(
                node.revision.identity
                for node in cls.snapshot.authority.lock.nodes
                if node.revision.repository_sources
            ),
        )

    def setUp(self):
        # A test may delete the producer origin; restore it from the template.
        if not self.fixture.source.exists():
            shutil.copytree(self.origin_template, self.fixture.source, symlinks=True)
        self.work = work = Path(tempfile.mkdtemp(prefix="test-", dir=self.fixture.root))
        self.quarantine = QuarantineStore(work / "quarantine", self.fixture.store)
        self.references = ReferenceIndex(work / "references", self.fixture.store)
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
        resource = cached_source / "sdk-resource.dat"
        self.assertEqual(resource.stat().st_size, self.source_payload_bytes)
        marker = b"retained SDK resource\n"
        with resource.open("rb") as stream:
            stream.seek(-len(marker), os.SEEK_END)
            self.assertEqual(stream.read(), marker)

        # The acquired checkout has already been deleted. Remove the origin too.
        self.fixture.remove_source()
        parent = self.work / "consumer"
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

    def test_real_sdk_build_respects_explicit_deadline_without_extending_defaults(self):
        # Advance only the true external wall clock read at build-validation
        # boundaries. Git capture, policy authorization, compilation, output
        # capture and repository cache admission all use production adapters.
        issued = datetime.now(UTC)
        for label, deadline, age, admitted in (
            ("default-expired", None, 11, False),
            ("bounded-current", issued + timedelta(minutes=30), 11, True),
            ("bounded-expired", issued + timedelta(minutes=30), 31, False),
        ):
            with self.subTest(label=label):
                root = self.work / label
                quarantine = QuarantineStore(root / "quarantine", self.fixture.store)
                references = ReferenceIndex(root / "references", self.fixture.store)
                service = self.service(
                    quarantine=quarantine,
                    references=references,
                    authorization_deadline=deadline,
                )
                with patch("literate_ai.adapters.native_sdk_build.datetime") as clock:
                    clock.now.return_value = issued + timedelta(minutes=age)
                    if admitted:
                        built = service.build()
                        self.assertEqual(
                            built.product.snapshot.source_lock_identity,
                            self.selection.source_lock.identity,
                        )
                        self.assertTrue(references.path.is_file())
                    else:
                        with self.refuses(AuthorizationError, "authorization_expired"):
                            service.build()
                        self.assertFalse(references.path.exists())
                        self.assertEqual(list(quarantine.ready_root.iterdir()), [])
                        self.assertEqual(list(quarantine.quarantine_root.iterdir()), [])

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
