"""Inspect an operator-provisioned cgroup v2 leaf before production launch.

This adapter creates no cgroups, changes no limits and moves or kills no processes.
The operator owns the hierarchy outside worker authority. A snapshot is a preflight
observation, not enforcement qualification or permission to execute.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from literate_ai.contracts.identity import canonical_identity
from literate_ai.security.isolation.contracts import IsolationPolicyError


@dataclass(frozen=True, slots=True)
class CgroupV2Limits:
    cpu_quota_us: int
    cpu_period_us: int
    memory_bytes: int
    swap_bytes: int
    processes: int

    def __post_init__(self) -> None:
        for name in (
            "cpu_quota_us",
            "cpu_period_us",
            "memory_bytes",
            "swap_bytes",
            "processes",
        ):
            value = getattr(self, name)
            minimum = 0 if name == "swap_bytes" else 1
            if type(value) is not int or not minimum <= value < 2**63:
                raise ValueError(f"{name} must be a finite bounded integer")

    def _settings(self) -> dict[str, str]:
        return {
            "cpu.max": f"{self.cpu_quota_us} {self.cpu_period_us}",
            "cpu.max.burst": "0",
            "memory.max": str(self.memory_bytes),
            "memory.swap.max": str(self.swap_bytes),
            "memory.oom.group": "1",
            "pids.max": str(self.processes),
            "cgroup.type": "domain",
            "cgroup.subtree_control": "",
            "cgroup.procs": "",
        }


@dataclass(frozen=True, slots=True)
class CgroupV2Snapshot:
    device: int
    inode: int
    mount_id: int
    observations: tuple[tuple[str, str], ...]

    @property
    def identity(self) -> str:
        return canonical_identity(
            {
                "domain": "literate-ai/cgroup-v2-preflight/1",
                "device": self.device,
                "inode": self.inode,
                "mount_id": self.mount_id,
                "observations": dict(self.observations),
            }
        ).uri


def _refuse() -> IsolationPolicyError:
    return IsolationPolicyError("containment.cgroup-budget-unavailable")


def _read_fd(fd: int, limit: int) -> str:
    chunks = bytearray()
    while len(chunks) <= limit:
        chunk = os.read(fd, min(4096, limit + 1 - len(chunks)))
        if not chunk:
            return chunks.decode("ascii")
        chunks.extend(chunk)
    raise _refuse()


def _mount_id(fd: int) -> int:
    with open(f"/proc/self/fdinfo/{fd}", "rb") as stream:
        info = stream.read(4097)
    with open("/proc/self/mountinfo", "rb") as stream:
        mounts = stream.read(1024 * 1024 + 1)
    return _require_cgroup_mount(info, mounts)


def _require_cgroup_mount(info: bytes, mounts: bytes) -> int:
    if len(info) > 4096 or len(mounts) > 1024 * 1024:
        raise _refuse()
    ids = [
        line.split()[1:] for line in info.splitlines() if line.startswith(b"mnt_id:")
    ]
    if len(ids) != 1 or len(ids[0]) != 1 or not ids[0][0].isdigit():
        raise _refuse()
    mount_id = int(ids[0][0])
    matching = []
    for line in mounts.splitlines():
        before, separator, after = line.partition(b" - ")
        fields = before.split()
        if fields and fields[0] == str(mount_id).encode("ascii"):
            matching.append((fields, separator, after.split()))
    if len(matching) != 1:
        raise _refuse()
    fields, separator, tail = matching[0]
    if len(fields) < 6 or not separator or len(tail) < 3 or tail[0] != b"cgroup2":
        raise _refuse()
    return mount_id


def _open_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise _refuse()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_setting(directory: int, name: str) -> str:
    fd = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        dir_fd=directory,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _refuse()
        return _read_fd(fd, 4096)
    finally:
        os.close(fd)


def inspect_cgroup_v2_budget(path: Path, limits: CgroupV2Limits) -> CgroupV2Snapshot:
    """Require exact finite limits, an empty domain leaf and cleanup write access.

    The launcher must additionally require qualified fair-class scheduling, exclude
    worker access to this hierarchy, verify storage/wall-time budgets and own the tree.
    Host administrators and mount namespaces remain trusted. Reads are not atomic;
    the two passes detect observed drift but cannot prevent an administrator race.
    """
    if sys.platform != "linux" or not isinstance(limits, CgroupV2Limits):
        raise _refuse()
    try:
        fd = _open_directory(path)
        try:
            mount_id = _mount_id(fd)
            identity = os.fstat(fd)
            expected = limits._settings()
            observations = []
            for _ in range(2):
                with os.scandir(fd) as entries:
                    for count, entry in enumerate(entries):
                        if (
                            count >= 1024
                            or entry.is_dir(follow_symlinks=False)
                            or entry.is_symlink()
                        ):
                            raise _refuse()
                observed = {
                    name: _read_setting(fd, name)
                    for name in (*expected, "cgroup.controllers", "cgroup.events")
                }
                if any(
                    observed[name].strip() != value for name, value in expected.items()
                ):
                    raise _refuse()
                controllers = observed["cgroup.controllers"].split()
                if len(controllers) != len(set(controllers)) or not {
                    "cpu",
                    "memory",
                    "pids",
                }.issubset(controllers):
                    raise _refuse()
                events = {}
                for line in observed["cgroup.events"].splitlines():
                    fields = line.split()
                    if len(fields) != 2 or fields[0] in events:
                        raise _refuse()
                    events[fields[0]] = fields[1]
                if events.get("populated") != "0" or events.get("frozen") != "0":
                    raise _refuse()
                observations.append(tuple(sorted(observed.items())))
            if observations[0] != observations[1] or _mount_id(fd) != mount_id:
                raise _refuse()
            # Opening proves current permission without moving or killing a task.
            for name in ("cgroup.procs", "cgroup.kill"):
                writable = os.open(
                    name,
                    os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=fd,
                )
                try:
                    if not stat.S_ISREG(os.fstat(writable).st_mode):
                        raise _refuse()
                finally:
                    os.close(writable)
            return CgroupV2Snapshot(
                identity.st_dev, identity.st_ino, mount_id, observations[0]
            )
        finally:
            os.close(fd)
    except (OSError, UnicodeError, ValueError, OverflowError) as exc:
        raise _refuse() from exc
