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
    def test_removes_only_manifest_bound_runtime_and_launcher_idempotently(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, layout = _installed_prefix(root)
            unrelated = prefix / "keep-me.txt"
            unrelated.write_text("operator owned\n", encoding="utf-8")

            plan = plan_host_uninstall(prefix)
            result = uninstall_host(plan)

            self.assertFalse(Path(layout.application_root).exists())
            self.assertFalse(Path(layout.launcher).exists())
            self.assertTrue(prefix.is_dir())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "operator owned\n")
            self.assertFalse(result["already_absent"])
            repeated = uninstall_host(plan_host_uninstall(prefix))
            self.assertTrue(repeated["already_absent"])

    def test_never_invokes_or_removes_host_dependency_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, _layout = _installed_prefix(root)
            native_package = root / "native-package-manager-owned-python"
            native_package.write_text("preserve\n", encoding="utf-8")

            uninstall_host(plan_host_uninstall(prefix))

            self.assertEqual(native_package.read_text(encoding="utf-8"), "preserve\n")

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

    def test_legacy_v1_manifest_still_uninstalls(self) -> None:
        from literate_ai.adapters.user_paths import HOST_INSTALL_MANIFEST_SCHEMA_V1

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, layout = _installed_prefix(root)
            Path(layout.manifest).write_text(
                json.dumps(
                    {
                        "schema": HOST_INSTALL_MANIFEST_SCHEMA_V1,
                        "prefix": str(prefix),
                        "environment": str(layout.environment),
                        "launcher": str(layout.launcher),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            uninstall_host(plan_host_uninstall(prefix))
            self.assertFalse(Path(layout.environment).exists())
            self.assertFalse(Path(layout.launcher).exists())

    def test_removes_self_update_staging_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, layout = _installed_prefix(root)
            staging = Path(layout.application_root) / "self-update"
            staging.mkdir()
            (staging / "cache.json").write_text("{}\n", encoding="utf-8")
            uninstall_host(plan_host_uninstall(prefix))
            self.assertFalse(staging.exists())
            self.assertFalse(Path(layout.application_root).exists())

    def test_pre_manifest_installation_requires_reinstall_before_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix, layout = _installed_prefix(root)
            Path(layout.manifest).unlink()

            with self.assertRaisesRegex(HostUninstallError, "reinstall"):
                plan_host_uninstall(prefix)


if __name__ == "__main__":
    unittest.main()
