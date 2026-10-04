"""SDK packages retain real native products independently of producer custody."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

from literate_ai.adapters.native_sdk_package import NativeSdkPackageResources
from literate_ai.contracts.identity import canonical_identity
from literate_ai.storage.cas import BlobIntegrityError
from tests.support import fixtures_test_native_sdk_consumer as test_native_sdk_consumer


class NativeSdkPackageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_sdk_consumer.NativeSdkConsumerTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.built = self.fixture.service.build()
        self.inputs = self.fixture.inputs()
        self.arguments = dict(
            component_lock_identity=self.inputs.snapshot.authority.lock.identity,
            root_revision=self.built.selection.component_revision,
            target_identity=self.built.selection.target_identity,
        )
        self.resources = NativeSdkPackageResources(self.inputs, **self.arguments)
        self.root = self.fixture.parent

    def test_relocated_package_runs_without_source_or_cas_and_detects_drift(self):
        self.resources.materialize(self.root)
        with self.assertRaises(FileExistsError):
            self.resources.materialize(self.root)
        index = json.loads((self.root / "native-sdks/inputs.json").read_text())
        self.assertEqual(len(index["consumers"]), 1)
        entry = index["consumers"][0]
        self.assertEqual(
            json.loads((self.root / entry["snapshot"]).read_text()),
            self.built.product.snapshot.to_dict(),
        )
        self.assertEqual(
            json.loads((self.root / entry["binding"]).read_text()),
            self.resources.bindings[0].to_dict(),
        )
        self.fixture.fixture.fixture.remove_source()
        shutil.rmtree(self.fixture.service.store.root)
        relocated = self.root.with_name("relocated-package")
        self.root.rename(relocated)
        self.resources.verify_materialized(relocated)
        result = subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys;sys.path.insert(0,sys.argv[1]);import vendor_math;"
                "print(vendor_math.scale(1.5,3))",
                str(relocated / entry["import_root"]),
            ),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4.5")
        self.resources.verify_materialized(relocated)
        path = relocated / self.resources.inputs[0].path
        original = path.read_bytes()
        path.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "bytes or mode"):
            self.resources.verify_materialized(relocated)
        path.write_bytes(original)
        if os.name != "nt":
            mode = path.stat().st_mode & 0o777
            path.chmod(mode ^ 0o111)
            with self.assertRaisesRegex(ValueError, "bytes or mode"):
                self.resources.verify_materialized(relocated)
            path.chmod(mode)
        extra = relocated / "native-sdks/undeclared"
        extra.write_text("extra")
        with self.assertRaisesRegex(ValueError, "unexpected file"):
            self.resources.verify_materialized(relocated)
        extra.unlink()
        if os.name != "nt":
            extra.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                self.resources.verify_materialized(relocated)
            extra.unlink()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "missing declared"):
            self.resources.verify_materialized(relocated)

    def test_foreign_authority_cas_corruption_and_collisions_are_refused(self):
        for key in ("component_lock_identity", "target_identity"):
            with self.assertRaisesRegex(ValueError, "differs"):
                NativeSdkPackageResources(
                    self.inputs,
                    **{**self.arguments, key: canonical_identity("foreign")},
                )
        foreign = self.fixture.service.store.put_bytes(b"not a package input")
        with self.assertRaisesRegex(ValueError, "not a declared"):
            self.resources.read_blob(foreign)
        self.assertFalse(self.resources.owns_blob(foreign))
        namespace = self.root / "native-sdks"
        namespace.write_text("existing consumer file")
        with self.assertRaises(FileExistsError):
            self.resources.materialize(self.root)
        self.assertEqual(namespace.read_text(), "existing consumer file")
        namespace.unlink()
        item = self.built.product.snapshot.files[0]
        path = self.fixture.service.store.path_for(item.blob)
        path.chmod(0o600)
        path.write_bytes(b"corrupt")
        with self.assertRaises(BlobIntegrityError):
            self.resources.materialize(self.root)
        self.assertFalse(namespace.exists())
