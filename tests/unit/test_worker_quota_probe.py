"""Native quota failures must never become unlimited capacity."""

from __future__ import annotations

import errno
import unittest
from types import SimpleNamespace

from literate_ai import worker_quota_probe as quota


class Queries:
    def __init__(self, *, filesystem=quota._EXT_SUPER_MAGIC, info=None, records=None):
        self.kind = filesystem
        self.info = info or {}
        self.records = records or {}
        self.calls = []

    def filesystem(self, descriptor):
        return self.kind

    def quota(self, descriptor, operation, kind, identity, record):
        self.calls.append((descriptor, operation, kind, identity))
        if operation == quota._Q_GETINFO:
            return self.info.get(kind, errno.ESRCH)
        if operation != quota._Q_GETQUOTA:
            raise AssertionError("quota mutation attempted")
        values = self.records.get((kind, identity), errno.ESRCH)
        if isinstance(values, int):
            return values
        for name, value in values.items():
            setattr(record, name, value)
        return 0


def probe(queries, *, gid=20):
    return quota._linux_probe(
        7, SimpleNamespace(st_gid=gid), "volume", queries=queries, identity=(10, 20)
    )


class WorkerQuotaProbeTests(unittest.TestCase):
    def test_failed_or_partial_quota_query_never_becomes_unlimited(self):
        def user_sample(queries):
            return next(
                s
                for s in probe(queries)
                if s["domain"] == quota._domain("volume", 0, 10)
            )

        for code, expected in (
            (errno.EPERM, "denied"),
            (errno.ENOSYS, "unsupported"),
            (errno.EIO, "unavailable"),
        ):
            with self.subTest(failed=code):
                samples = probe(Queries(info={0: code, 1: code, 2: code}))
                self.assertEqual(
                    {s["available_bytes"]["status"] for s in samples}, {expected}
                )
        with self.subTest("missing identity record"):
            sample = user_sample(Queries(info={0: 0}))
            self.assertEqual(sample["available_bytes"]["status"], "unavailable")
        for valid, byte_status, inode_status in (
            (0, "malformed", "malformed"),
            (3, "measured", "malformed"),
            (12, "malformed", "measured"),
        ):
            with self.subTest(partial_valid=valid):
                sample = user_sample(
                    Queries(
                        info={0: 0},
                        records={
                            (0, 10): dict(valid=valid, hard_bytes=1, hard_inodes=2)
                        },
                    )
                )
                self.assertEqual(sample["available_bytes"]["status"], byte_status)
                self.assertEqual(sample["available_inodes"]["status"], inode_status)


if __name__ == "__main__":
    unittest.main()
