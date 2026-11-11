"""Native connector limits never increase an inherited file-size ceiling."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.remote_transport import BoundedRemoteProcessRunner
from literate_ai.contracts.execution_dispatch import MAX_REMOTE_EVIDENCE_BUNDLE_BYTES


@unittest.skipUnless(os.name == "posix", "native POSIX file-size limits")
class RemoteFileCeilingTests(unittest.TestCase):
    def test_limiter_preserves_stricter_inherited_soft_and_hard_bounds(self):
        import resource

        original = resource.getrlimit(resource.RLIMIT_FSIZE)
        for value in original:
            if value != resource.RLIM_INFINITY and value < 131072:
                self.skipTest("native inherited ceiling cannot hold the finite fixture")
        cases = [
            ((65536, 131072), MAX_REMOTE_EVIDENCE_BUNDLE_BYTES, (65536, 131072)),
            ((65536, 131072), 98304, (65536, 98304)),
            ((65536, 131072), 32768, (32768, 32768)),
            ((0, 131072), MAX_REMOTE_EVIDENCE_BUNDLE_BYTES, (0, 131072)),
        ]
        if original == (resource.RLIM_INFINITY, resource.RLIM_INFINITY):
            cases.append((original, 32768, (32768, 32768)))
        driver = """
import json, resource, sys
from pathlib import Path
from literate_ai.adapters.remote_transport import BoundedRemoteProcessRunner
resource.setrlimit(resource.RLIMIT_FSIZE, (int(sys.argv[1]), int(sys.argv[2])))
probe = ('import json,resource; '
         'print(json.dumps(resource.getrlimit(resource.RLIMIT_FSIZE)))')
result = BoundedRemoteProcessRunner().run(
    (sys.executable, '-B', '-c', probe), cwd=Path.cwd(),
    timeout_seconds=10, file_limit_bytes=int(sys.argv[3]))
print(json.dumps({'returncode': result.returncode,
                  'stdout': result.stdout.decode(), 'stderr': result.stderr.decode()}))
"""
        with tempfile.TemporaryDirectory() as raw:
            for inherited, requested, expected in cases:
                with self.subTest(inherited=inherited, requested=requested):
                    result = BoundedRemoteProcessRunner().run(
                        (
                            sys.executable,
                            "-B",
                            "-c",
                            driver,
                            *map(str, inherited),
                            str(requested),
                        ),
                        cwd=Path(raw),
                        timeout_seconds=20,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    observed = json.loads(result.stdout)
                    self.assertEqual(observed["returncode"], 0, observed["stderr"])
                    self.assertEqual(tuple(json.loads(observed["stdout"])), expected)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_FSIZE), original)

    def test_real_download_custody_survives_a_finite_inherited_ceiling(self):
        import resource

        original = resource.getrlimit(resource.RLIMIT_FSIZE)
        for value in original:
            if value != resource.RLIM_INFINITY and value < 131072:
                self.skipTest("native inherited ceiling cannot hold the finite fixture")
        driver = """
import resource, runpy, sys, unittest
ceiling = 512 * 1024**2
soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
soft = ceiling if soft == resource.RLIM_INFINITY else min(soft, ceiling)
hard = ceiling if hard == resource.RLIM_INFINITY else min(hard, ceiling)
resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
module = runpy.run_path(sys.argv[1])
case = module['SshTransportTests'](
    'test_command_route_transfers_refuse_corruption_and_preserve_destination')
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([case]))
raise SystemExit(0 if result.wasSuccessful() and result.testsRun == 1
                 and not result.skipped else 1)
"""
        result = BoundedRemoteProcessRunner().run(
            (
                sys.executable,
                "-B",
                "-c",
                driver,
                str(Path(__file__).with_name("test_ssh_transport.py").resolve()),
            ),
            cwd=Path(__file__).resolve().parents[2],
            timeout_seconds=40,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b"Ran 1 test", result.stderr)
        self.assertEqual(resource.getrlimit(resource.RLIMIT_FSIZE), original)
