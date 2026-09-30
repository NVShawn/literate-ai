"""Safe, manifest-bound removal of a Literate AI host installation."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.user_paths import (
    HOST_INSTALL_MANIFEST_SCHEMA,
    HOST_INSTALL_MANIFEST_SCHEMA_V1,
    HostInstallLayout,
)
from literate_ai.contracts import ContentIdentity, canonical_identity

_MAXIMUM_MANIFEST_BYTES = 64 * 1024


class HostUninstallError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HostUninstallPlan:
    prefix: Path
    application_root: Path
    environment: Path
    launcher: Path
    manifest: Path
    installed: bool

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/host-uninstall-plan@1",
                "prefix": str(self.prefix),
                "application_root": str(self.application_root),
                "environment": str(self.environment),
                "launcher": str(self.launcher),
                "manifest": str(self.manifest),
                "installed": self.installed,
            }
        )


def _contained(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _manifest_matches_prefix(
    document: object,
    *,
    prefix: Path,
    environment: Path,
    launcher: Path,
) -> bool:
    if not isinstance(document, dict):
        return False
    if document.get("prefix") != str(prefix):
        return False
    if document.get("environment") != str(environment):
        return False
    if document.get("launcher") != str(launcher):
        return False
    schema = document.get("schema")
    if schema == HOST_INSTALL_MANIFEST_SCHEMA_V1:
        return (
            document.keys() <= {"schema", "prefix", "environment", "launcher"}
            and len(document) == 4
        )
    if schema == HOST_INSTALL_MANIFEST_SCHEMA:
        return document.get("self_update") is True and set(document) == {
            "schema",
            "prefix",
            "environment",
            "launcher",
            "self_update",
        }
    return False


def plan_host_uninstall(prefix: Path) -> HostUninstallPlan:
    prefix = prefix.expanduser().resolve()
    if prefix == Path(prefix.anchor):
        raise HostUninstallError("refusing to uninstall from a filesystem root")
    layout = HostInstallLayout.for_prefix(prefix)
    application_root = Path(layout.application_root)
    environment = Path(layout.environment)
    launcher = Path(layout.launcher)
    manifest = Path(layout.manifest)
    plan = HostUninstallPlan(
        prefix, application_root, environment, launcher, manifest, False
    )
    if not manifest.exists() and not manifest.is_symlink():
        if any(path.exists() or path.is_symlink() for path in (environment, launcher)):
            raise HostUninstallError(
                "Literate AI installation artifacts exist without the 0.9.0 owned "
                "install manifest; reinstall into this prefix before uninstalling"
            )
        return plan
    if any(path.is_symlink() for path in (application_root, environment, manifest)):
        raise HostUninstallError("installation custody cannot contain symbolic links")
    if not manifest.is_file() or manifest.stat().st_size > _MAXIMUM_MANIFEST_BYTES:
        raise HostUninstallError("installation manifest is not a bounded regular file")
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostUninstallError("installation manifest is invalid") from exc
    if not _manifest_matches_prefix(
        document, prefix=prefix, environment=environment, launcher=launcher
    ):
        raise HostUninstallError(
            "installation manifest does not match the selected host prefix"
        )
    if (
        not application_root.is_dir()
        or not environment.is_dir()
        or launcher.is_symlink()
        or not launcher.is_file()
        or not _contained(environment, application_root)
        or not _contained(application_root, prefix)
        or not _contained(launcher, prefix)
    ):
        raise HostUninstallError("installation layout is incomplete or unsafe")
    return HostUninstallPlan(
        prefix, application_root, environment, launcher, manifest, True
    )


def uninstall_host(plan: HostUninstallPlan) -> dict[str, object]:
    if not isinstance(plan, HostUninstallPlan):
        raise TypeError("plan must be a HostUninstallPlan")
    if not plan.installed:
        return {
            "schema": "literate-ai/host-uninstall-result@1",
            "prefix": str(plan.prefix),
            "removed": [],
            "already_absent": True,
        }
    if plan_host_uninstall(plan.prefix) != plan:
        raise HostUninstallError("installation changed after uninstall planning")
    removed = [str(plan.launcher), str(plan.environment), str(plan.manifest)]
    plan.launcher.unlink()
    shutil.rmtree(plan.environment)
    plan.manifest.unlink()
    staging = plan.application_root / "self-update"
    if staging.is_dir() and not staging.is_symlink():
        shutil.rmtree(staging)
        removed.append(str(staging))
    # Config, state, caches, managed tools, and other independently owned data can
    # legitimately share the application namespace. They are not named by the install
    # manifest and must survive this exact removal operation.
    if not any(plan.application_root.iterdir()):
        plan.application_root.rmdir()
    # Conventional prefixes such as ``~/.local`` are operator-owned roots, even
    # when installation happened to find them empty.  Only prune empty child
    # directories that this layout may have created.
    for directory in (plan.application_root.parent, plan.launcher.parent):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {
        "schema": "literate-ai/host-uninstall-result@1",
        "prefix": str(plan.prefix),
        "removed": removed,
        "already_absent": False,
    }


__all__ = [
    "HOST_INSTALL_MANIFEST_SCHEMA",
    "HostUninstallError",
    "HostUninstallPlan",
    "plan_host_uninstall",
    "uninstall_host",
]
