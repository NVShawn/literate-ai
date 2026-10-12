"""Private file-backed FINALIZE authority observes live revocation and unsafe state."""

import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import tests.support.fixtures_test_action_finalize_authority as authority_fixture
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_grant import (
    MAX_FINALIZE_GRANT_BYTES,
    FileFinalizeExecutionAuthority,
)
from literate_ai.adapters.action_finalize_process import run_finalize_worker_process
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.contracts import canonical_json_bytes


class FinalizeGrantTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = authority_fixture.FinalizeAuthorityTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(root).resolve()
        self.private = self.root / "private"
        self.private.mkdir()
        self.path = self.private / "grant.json"
        self.publish(f.grant)
        self.authority = FileFinalizeExecutionAuthority(
            grant_path=self.path,
            runtime_identity=f.runtime,
            observe_runtime=f.observer,
            clock=lambda: f.now,
        )

    def publish(self, grant):
        staged = self.private / "next.json"
        staged.write_bytes(canonical_json_bytes(grant.to_dict()))
        staged.replace(self.path)

    def test_current_grant_and_atomic_revocation_are_observed(self):
        self.authority(self.fixture.value)
        self.publish(replace(self.fixture.grant, revoked=True))
        with self.assertRaises(ActionWireError) as error:
            self.authority(self.fixture.value)
        self.assertEqual(error.exception.code, "action_finalize.authority_invalid")

    def test_missing_corrupt_oversized_duplicate_and_nonobject_grants_refuse(self):
        for content in (
            b"",
            b"bad",
            b"[]",
            b"null",
            b"x" * (MAX_FINALIZE_GRANT_BYTES + 1),
            b'{"revoked":true,"revoked":false}',
        ):
            with self.subTest(content=content[:20]):
                self.path.write_bytes(content)
                with self.assertRaises(ActionWireError) as error:
                    self.authority(self.fixture.value)
                self.assertEqual(
                    error.exception.code, "action_finalize.grant_unavailable"
                )
        self.path.unlink()
        with self.assertRaises(ActionWireError):
            self.authority(self.fixture.value)
        self.assertFalse(self.path.exists())

    def test_file_replacement_during_read_refuses(self):
        original = os.fstat
        calls = 0
        grant_inode = self.path.stat().st_ino

        def replace_after_read(fd):
            nonlocal calls
            metadata = original(fd)
            if metadata.st_ino == grant_inode:
                calls += 1
                if calls == 2:
                    self.publish(replace(self.fixture.grant, revoked=True))
            return metadata

        with patch(
            "literate_ai.adapters.action_finalize_grant.os.fstat", replace_after_read
        ):
            with self.assertRaises(ActionWireError) as error:
                self.authority(self.fixture.value)
        self.assertEqual(error.exception.code, "action_finalize.grant_unavailable")

    def test_replaced_directory_and_directory_file_refuse(self):
        self.private.rename(self.root / "old")
        self.private.mkdir()
        self.publish(self.fixture.grant)
        with self.assertRaises(ActionWireError):
            self.authority(self.fixture.value)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(ActionWireError):
            self.authority.provider()

    def test_symlink_and_fifo_refuse_without_reading(self):
        self.path.unlink()
        target = self.private / "target"
        target.write_bytes(canonical_json_bytes(self.fixture.grant.to_dict()))
        try:
            self.path.symlink_to(target)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(ActionWireError):
            self.authority.provider()
        self.path.unlink()
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.path)
            with self.assertRaises(ActionWireError):
                self.authority.provider()

    def test_other_request_and_extra_fields_refuse(self):
        self.publish(
            replace(
                self.fixture.grant, classification_digest=record_identity(b"other").uri
            )
        )
        with self.assertRaises(ActionWireError):
            self.authority(self.fixture.value)
        document = self.fixture.grant.to_dict() | {"allow_all": True}
        self.path.write_text(json.dumps(document))
        with self.assertRaises(ActionWireError):
            self.authority(self.fixture.value)

    def test_file_revocation_stops_running_supervised_child(self):
        workspace = self.root / "job"
        workspace.mkdir()
        marker = workspace / "started"

        def current(value):
            if marker.exists():
                self.publish(replace(self.fixture.grant, revoked=True))
            self.authority(value)

        started = time.monotonic()
        raw = self.fixture.value.to_bytes()
        with self.assertRaises(ActionWireError) as error:
            run_finalize_worker_process(
                launcher=LocalComponentToolBinding(
                    sys.executable,
                    (
                        "-c",
                        "import sys,time; from pathlib import Path; "
                        "sys.stdin.buffer.read(); Path('started').touch(); "
                        "time.sleep(30)",
                    ),
                ),
                input_record=raw,
                input_identity=record_identity(raw),
                deadline=self.fixture.fixture.deadline,
                cwd=workspace,
                environment=dict(os.environ),
                require_execution_authority=current,
                cas_root=workspace,
                workspace_root=workspace,
            )
        self.assertEqual(error.exception.code, "action_finalize.authority_invalid")
        self.assertTrue(marker.exists())
        self.assertLess(time.monotonic() - started, 10)
