"""Read-only Unix quota adapters for the supervised stdlib storage receiver.

Linux query constants/layouts follow the kernel UAPI, not host command output.
Unsupported filesystems and ABI families remain explicit rather than unlimited.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import platform
import sys

_Q_GETINFO = 0x800005
_Q_GETQUOTA = 0x800007
_EXT_SUPER_MAGIC = 0xEF53


class _LinuxStatfs(ctypes.Structure):
    _fields_ = [
        ("kind", ctypes.c_long),
        ("block_size", ctypes.c_long),
        ("blocks", ctypes.c_ulong),
        ("free_blocks", ctypes.c_ulong),
        ("available_blocks", ctypes.c_ulong),
        ("files", ctypes.c_ulong),
        ("free_files", ctypes.c_ulong),
        ("fsid", ctypes.c_int * 2),
        ("name_length", ctypes.c_long),
        ("fragment_size", ctypes.c_long),
        ("flags", ctypes.c_long),
        ("spare", ctypes.c_long * 4),
    ]


class _LinuxQuotaInfo(ctypes.Structure):
    _fields_ = [
        ("block_grace", ctypes.c_uint64),
        ("inode_grace", ctypes.c_uint64),
        ("flags", ctypes.c_uint32),
        ("valid", ctypes.c_uint32),
    ]


class _LinuxQuota(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "hard_bytes",
            "soft_bytes",
            "used_bytes",
            "hard_inodes",
            "soft_inodes",
            "used_inodes",
            "byte_time",
            "inode_time",
        )
    ] + [("valid", ctypes.c_uint32)]


def _metric(status, value=None):
    return {"status": status, "value": value}


def _domain(volume, kind, identity):
    return (
        "quota-"
        + hashlib.sha256(f"{volume}:{kind}:{identity}".encode()).hexdigest()[:48]
    )


def _failure(domain, status):
    return {
        "domain": domain,
        "available_bytes": _metric(status),
        "available_inodes": _metric(status),
    }


def _error(code):
    if code in (errno.EACCES, errno.EPERM):
        return "denied"
    if code in (errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL):
        return "unsupported"
    return "unavailable"


def _headroom(hard, soft, used, *, unit=1):
    limits = [v * unit for v in (hard, soft) if v]
    if not limits:
        return _metric("not-applicable")
    # Do not promise a grace period remains valid through the job. The soft
    # limit is the conservative ceiling even when its grace timer has not expired.
    available = max(0, min(limits) - used)
    if available > 2**63 - 1:
        return _metric("malformed")
    return _metric("measured", available)


def _linux_identity():
    with open("/proc/self/status", "rb") as stream:
        raw = stream.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError("process identity record exceeds its bound")
    rows = {}
    for line in raw.splitlines():
        if line.startswith((b"Uid:", b"Gid:")):
            values = line.split()
            if len(values) != 5 or values[0] in rows:
                raise ValueError("process identity record is malformed")
            rows[values[0]] = int(values[4])
    if set(rows) != {b"Uid:", b"Gid:"} or any(
        not 0 <= v < 2**32 for v in rows.values()
    ):
        raise ValueError("process identity record is incomplete")
    return rows[b"Uid:"], rows[b"Gid:"]


class _LinuxQueries:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.fstatfs.argtypes = (ctypes.c_int, ctypes.POINTER(_LinuxStatfs))
        self.libc.fstatfs.restype = ctypes.c_int
        self.libc.syscall.restype = ctypes.c_long

    def filesystem(self, descriptor):
        record = _LinuxStatfs()
        if self.libc.fstatfs(descriptor, ctypes.byref(record)) != 0:
            raise OSError(ctypes.get_errno(), "filesystem query failed")
        return record.kind

    def quota(self, descriptor, operation, kind, identity, record):
        # quotactl_fd is syscall 443 on the explicitly admitted x86-64/aarch64
        # ABIs. It queries this same open filesystem, without device-path races.
        if operation not in (_Q_GETINFO, _Q_GETQUOTA):
            raise ValueError("only read-only quota operations are admitted")
        result = self.libc.syscall(
            ctypes.c_long(443),
            ctypes.c_int(descriptor),
            ctypes.c_uint((operation << 8) | kind),
            ctypes.c_uint(identity),
            ctypes.byref(record),
        )
        return 0 if result == 0 else ctypes.get_errno()


def _linux_probe(descriptor, node, volume, *, queries=None, identity=None):
    queries = _LinuxQueries() if queries is None else queries
    if queries.filesystem(descriptor) != _EXT_SUPER_MAGIC:
        return [_failure("unix-unobserved", "unsupported")]
    uid, gid = _linux_identity() if identity is None else identity
    samples = []
    for kind in (0, 1, 2):
        applicability = _LinuxQuotaInfo()
        code = queries.quota(descriptor, _Q_GETINFO, kind, 0, applicability)
        generic_domain = _domain(volume, kind, "applicability")
        if code:
            # ESRCH from GETINFO identifies a disabled quota class on a known
            # supported filesystem. ESRCH from a per-identity query is NOT this.
            samples.append(
                _failure(
                    generic_domain,
                    "not-applicable" if code == errno.ESRCH else _error(code),
                )
            )
            continue
        if kind == 2:
            # Project ownership needs its own verified directory-inheritance
            # query. Do not substitute project 0 or the caller's user identity.
            samples.append(_failure(generic_domain, "unsupported"))
            continue
        identities = (uid,) if kind == 0 else tuple(sorted({gid, node.st_gid}))
        for selected in identities:
            domain = _domain(volume, kind, selected)
            record = _LinuxQuota()
            code = queries.quota(descriptor, _Q_GETQUOTA, kind, selected, record)
            if code:
                samples.append(_failure(domain, _error(code)))
                continue
            samples.append(
                {
                    "domain": domain,
                    "available_bytes": _headroom(
                        record.hard_bytes,
                        record.soft_bytes,
                        record.used_bytes,
                        unit=1024,
                    )
                    if record.valid & 3 == 3
                    else _metric("malformed"),
                    "available_inodes": _headroom(
                        record.hard_inodes, record.soft_inodes, record.used_inodes
                    )
                    if record.valid & 12 == 12
                    else _metric("malformed"),
                }
            )
    return sorted(samples, key=lambda sample: sample["domain"])


def probe(descriptor, node, volume):
    if (
        sys.platform != "linux"
        or platform.machine().lower() not in ("x86_64", "aarch64")
        or ctypes.sizeof(ctypes.c_void_p) != 8
        or ctypes.sizeof(ctypes.c_long) != 8
        or ctypes.sizeof(_LinuxStatfs) != 120
        or ctypes.sizeof(_LinuxQuota) != 72
        or ctypes.sizeof(_LinuxQuotaInfo) != 24
    ):
        return [_failure("unix-unobserved", "unsupported")]
    try:
        return _linux_probe(descriptor, node, volume)
    except OSError as exc:
        return [_failure("unix-unobserved", _error(exc.errno))]
    except (ValueError, TypeError, OverflowError):
        return [_failure("unix-unobserved", "malformed")]
    except AttributeError:
        return [_failure("unix-unobserved", "unsupported")]
