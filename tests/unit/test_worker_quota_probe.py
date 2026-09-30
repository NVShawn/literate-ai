"""Native quota failures must never become unlimited capacity."""

from __future__ import annotations

import errno
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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
    def test_disabled_classes_are_explicit_and_need_no_identity_query(self):
        queries = Queries()
        samples = probe(queries)
        self.assertEqual(len(samples), 3)
        self.assertTrue(
            all(
                s[k]["status"] == "not-applicable"
                for s in samples
                for k in ("available_bytes", "available_inodes")
            )
        )
        self.assertEqual({c[1] for c in queries.calls}, {quota._Q_GETINFO})

    def test_unsupported_filesystem_never_queries_unknown_quota_abi(self):
        queries = Queries(filesystem=0x58465342)
        self.assertEqual(probe(queries)[0]["available_bytes"]["status"], "unsupported")
        self.assertEqual(queries.calls, [])

    def test_user_and_both_possible_group_owners_keep_independent_limits(self):
        record = dict(
            hard_bytes=10,
            soft_bytes=8,
            used_bytes=1000,
            hard_inodes=15,
            soft_inodes=12,
            used_inodes=7,
            valid=15,
        )
        queries = Queries(
            info={0: 0, 1: 0},
            records={(0, 10): record, (1, 20): record, (1, 30): record},
        )
        samples = probe(queries, gid=30)
        measured = [s for s in samples if s["available_bytes"]["status"] == "measured"]
        self.assertEqual(len(measured), 3)
        self.assertEqual(len({s["domain"] for s in measured}), 3)
        for sample in measured:
            self.assertEqual(sample["available_bytes"]["value"], 8 * 1024 - 1000)
            self.assertEqual(sample["available_inodes"]["value"], 5)
        self.assertEqual(
            [s["domain"] for s in samples], sorted(s["domain"] for s in samples)
        )
        self.assertEqual(
            {c[1] for c in queries.calls}, {quota._Q_GETINFO, quota._Q_GETQUOTA}
        )

    def test_active_project_class_is_unknown_until_inheritance_is_qualified(self):
        queries = Queries(info={2: 0})
        sample = next(
            s
            for s in probe(queries)
            if s["domain"] == quota._domain("volume", 2, "applicability")
        )
        self.assertEqual(sample["available_bytes"]["status"], "unsupported")
        self.assertTrue(all(c[1] == quota._Q_GETINFO for c in queries.calls))

    def test_denied_unsupported_and_unavailable_are_not_disabled(self):
        for code, expected in (
            (errno.EPERM, "denied"),
            (errno.EACCES, "denied"),
            (errno.ENOSYS, "unsupported"),
            (errno.EOPNOTSUPP, "unsupported"),
            (errno.EIO, "unavailable"),
        ):
            with self.subTest(code=code):
                samples = probe(Queries(info={0: code, 1: code, 2: code}))
                self.assertEqual(
                    {s["available_bytes"]["status"] for s in samples}, {expected}
                )

    def test_missing_identity_record_is_not_known_unlimited(self):
        samples = probe(Queries(info={0: 0}))
        sample = next(
            s for s in samples if s["domain"] == quota._domain("volume", 0, 10)
        )
        self.assertEqual(sample["available_bytes"]["status"], "unavailable")

    def test_partial_validity_never_invents_zero_limits(self):
        for valid, byte_status, inode_status in (
            (0, "malformed", "malformed"),
            (3, "measured", "malformed"),
            (12, "malformed", "measured"),
        ):
            with self.subTest(valid=valid):
                samples = probe(
                    Queries(
                        info={0: 0},
                        records={
                            (0, 10): dict(valid=valid, hard_bytes=1, hard_inodes=2)
                        },
                    )
                )
                sample = next(
                    s for s in samples if s["domain"] == quota._domain("volume", 0, 10)
                )
                self.assertEqual(sample["available_bytes"]["status"], byte_status)
                self.assertEqual(sample["available_inodes"]["status"], inode_status)

    def test_soft_limit_is_conservative_exhaustion_clamps_and_bounds_remain(self):
        self.assertEqual(quota._headroom(0, 0, 99)["status"], "not-applicable")
        self.assertEqual(quota._headroom(10, 5, 3)["value"], 2)
        self.assertEqual(quota._headroom(10, 5, 7)["value"], 0)
        self.assertEqual(quota._headroom(2**63, 0, 0)["status"], "malformed")
        self.assertEqual(quota._headroom(2**63, 0, 1)["value"], 2**63 - 1)

    def test_filesystem_identity_uses_fourth_uid_gid_and_rejects_ambiguity(self):
        with patch(
            "builtins.open", return_value=io.BytesIO(b"Uid:\t1 2 3 4\nGid:\t5 6 7 8\n")
        ):
            self.assertEqual(quota._linux_identity(), (4, 8))
        for raw in (
            b"Uid: 1 2 3 4\n",
            b"Uid: 1 2 3 4\nUid: 1 2 3 4\nGid: 1 2 3 4\n",
            b"Uid: 1 2 3 -1\nGid: 1 2 3 4\n",
            b"x" * 65537,
        ):
            with (
                self.subTest(raw=raw[:40]),
                patch("builtins.open", return_value=io.BytesIO(raw)),
                self.assertRaises(ValueError),
            ):
                quota._linux_identity()

    def test_query_interface_refuses_mutation_before_libc(self):
        queries = object.__new__(quota._LinuxQueries)
        with self.assertRaisesRegex(ValueError, "read-only"):
            queries.quota(7, 0x800008, 0, 10, quota._LinuxQuota())

    def test_unknown_platform_never_calls_native_query(self):
        with (
            patch.object(quota.sys, "platform", "darwin"),
            patch.object(quota, "_linux_probe") as native,
        ):
            self.assertEqual(
                quota.probe(7, None, "volume")[0]["available_bytes"]["status"],
                "unsupported",
            )
            native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
