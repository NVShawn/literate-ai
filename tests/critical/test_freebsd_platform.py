"""FreeBSD controller paths and release selection keep the native OS identity."""

from __future__ import annotations

import os
import unittest
from pathlib import PurePosixPath
from unittest.mock import patch

from literate_ai.adapters.application_settings import resolve_app_settings_path
from literate_ai.adapters.test_matrix_config import GlobalTestMatrix
from literate_ai.adapters.user_paths import HostInstallLayout, resolve_host_paths
from literate_ai.release_qualification import (
    QualificationRunner,
    ReleaseQualificationPolicy,
    plan_qualification,
    satisfies,
)


class FreeBSDPlatformTests(unittest.TestCase):
    def test_freebsd_uses_private_posix_roots_without_linux_proc_or_apparmor(self):
        paths = resolve_host_paths(
            platform_family="freebsd",
            environment={},
            home=PurePosixPath("/usr/home/tester"),
        )
        self.assertEqual(str(paths.config_root), "/usr/home/tester/.config/literate-ai")
        self.assertIsNone(paths.os_release_file)
        self.assertIsNone(paths.bubblewrap_proc_root)
        self.assertIsNone(paths.apparmor_user_namespace_restriction_file)
        layout = HostInstallLayout.for_prefix(
            PurePosixPath("/usr/local"), platform_family="freebsd"
        )
        self.assertEqual(str(layout.launcher), "/usr/local/bin/litai")
        settings = resolve_app_settings_path(
            "example",
            platform_family="freebsd",
            environment={},
            home=PurePosixPath("/usr/home/tester"),
        )
        self.assertEqual(
            settings.as_posix(), "/usr/home/tester/.config/example/settings.json"
        )

    @unittest.skipIf(os.name == "nt", "native POSIX host detection")
    def test_freebsd_host_autodetection(self):
        with patch("sys.platform", "freebsd16"):
            paths = resolve_host_paths(
                environment={}, home=PurePosixPath("/usr/home/tester")
            )
        self.assertEqual(str(paths.config_root), "/usr/home/tester/.config/literate-ai")

    def test_freebsd_matrix_and_release_require_native_worker(self):
        matrix = GlobalTestMatrix.from_dict(
            {
                "schema": "literate-ai/global-test-matrix@3",
                "default_samples": ["hello-component"],
                "workers": [
                    {
                        "worker_id": "bsd",
                        "platform_flavor": "freebsd",
                        "source_mode": "git",
                    }
                ],
            }
        )
        self.assertEqual(matrix.workers[0].platform, "freebsd")
        policy = ReleaseQualificationPolicy.from_dict(
            {"platforms": ["freebsd-x86_64"]}, gate_argv=("gmake", "check")
        )
        plan = plan_qualification(
            policy,
            host=("macos", "arm64"),
            workers=(
                QualificationRunner("linux", "linux", "x86_64"),
                QualificationRunner("bsd", "freebsd", "x86_64"),
            ),
        )
        self.assertEqual(plan.local, ())
        self.assertEqual(plan.workers, {"freebsd-x86_64": ("bsd",)})
        self.assertFalse(plan.ci_required)
        self.assertFalse(satisfies("freebsd-x86_64", "linux", "x86_64"))
        self.assertFalse(satisfies("freebsd-x86_64", "freebsd", None))
        self.assertFalse(satisfies("freebsd-x86_64", "freebsd", "arm64"))
