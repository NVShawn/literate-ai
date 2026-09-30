"""Service teardown must close descendants even after the root has exited."""

import os
import subprocess
import sys
import unittest

from literate_ai.adapters._processes import (
    create_process_tree_ownership,
    terminate_process_tree,
)
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecyclePorts


class ServiceProcessCleanupTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "real POSIX process group")
    def test_shutdown_retires_descendants_after_graceful_or_early_root_exit(self):
        child = (
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(60)"
        )
        for early_exit in (False, True):
            with self.subTest(early_exit=early_exit):
                root = (
                    "import subprocess,sys,time; "
                    f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
                    + ("sys.exit(0)" if early_exit else "time.sleep(60)")
                )
                ownership = create_process_tree_ownership()
                process = subprocess.Popen(
                    [sys.executable, "-c", root],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **ownership.popen_options,
                )
                ownership.bind(process.pid)
                try:
                    self.assertEqual(process.stdout.readline(), b"ready\n")
                    if early_exit:
                        process.wait(timeout=5)
                    LocalStandardLifecyclePorts._stop_service_process(
                        process, 1, dict(os.environ), ownership
                    )
                    # The surviving child inherits both pipes. EOF proves it no
                    # longer holds them, even when the parent was already dead.
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual((stdout, stderr), (b"", b""))
                    self.assertIsNotNone(process.poll())
                finally:
                    terminate_process_tree(process, ownership=ownership)
                    process.communicate(timeout=5)
                    ownership.release()
