"""Ownership and safety contracts for host CLI uninstallation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.host_uninstall import (
    HOST_INSTALL_MANIFEST_SCHEMA,
    HostUninstallError,
    plan_host_uninstall,
    uninstall_host,
)
from literate_ai.adapters.user_paths import HostInstallLayout
from tests.support.symlinks import skip_unless_symlinks_followable


def _installed_prefix(root: Path) -> tuple[Path, HostInstallLayout]:
    prefix = (root / "operator-prefix").resolve()
    layout = HostInstallLayout.for_prefix(prefix)
    environment = Path(layout.environment)
    launcher = Path(layout.launcher)
    manifest = Path(layout.manifest)
    environment.mkdir(parents=True)
    (environment / "runtime-marker").write_text("private runtime\n", encoding="utf-8")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "schema": HOST_INSTALL_MANIFEST_SCHEMA,
                "prefix": str(prefix.resolve()),
                "environment": str(environment),
                "launcher": str(launcher),
                "self_update": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return prefix, layout


class HostUninstallTests(unittest.TestCase):
    def test_manifest_mismatch_and_symlink_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, layout = _installed_prefix(root)
            manifest = Path(layout.manifest)
            original = manifest.read_text(encoding="utf-8")
            changed = json.loads(original)
            changed["prefix"] = str((root / "wrong-prefix").resolve())
            manifest.write_text(
                json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HostUninstallError, "does not match"):
                plan_host_uninstall(prefix)
            manifest.write_text(original, encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, layout = _installed_prefix(root)
            manifest = Path(layout.manifest)
            skip_unless_symlinks_followable(self, root)
            manifest.unlink()
            manifest.symlink_to(root / "missing")
            with self.assertRaisesRegex(HostUninstallError, "symbolic links"):
                plan_host_uninstall(prefix)

    def test_independently_owned_application_data_survives_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, layout = _installed_prefix(root)
            retained = Path(layout.application_root) / "tools" / "managed-tool"
            retained.parent.mkdir()
            retained.write_text("preserve\n", encoding="utf-8")

            result = uninstall_host(plan_host_uninstall(prefix))

            self.assertFalse(result["already_absent"])
            self.assertEqual(retained.read_text(encoding="utf-8"), "preserve\n")
            self.assertFalse(Path(layout.environment).exists())
            self.assertFalse(Path(layout.launcher).exists())
            repeated = uninstall_host(plan_host_uninstall(prefix))
            self.assertTrue(repeated["already_absent"])


if __name__ == "__main__":
    unittest.main()
