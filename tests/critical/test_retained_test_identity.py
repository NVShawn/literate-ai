"""Numeric identity and current account observations must agree in both directions."""

import os
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.retained_test_identity import (
    require_current_secondary_test_identity as require_current,
)
from literate_ai.contracts.retained_test_identity import RetainedSecondaryTestIdentity


class RetainedSecondaryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.reviewed = RetainedSecondaryTestIdentity("litaitest", 2001, 2001, (2001,))

    def test_round_trip_binds_every_numeric_group(self):
        self.assertEqual(
            RetainedSecondaryTestIdentity.from_dict(self.reviewed.to_dict()),
            self.reviewed,
        )
        self.assertNotEqual(
            replace(self.reviewed, groups=(2001, 2002)).identity,
            self.reviewed.identity,
        )

    def test_rejects_root_groups_aliases_booleans_and_unbounded_ids(self):
        for changes in (
            {"uid": 0},
            {"gid": 0},
            {"groups": (0, 2001)},
            {"uid": True},
            {"uid": 2**32 - 1},
            {"groups": (2002,)},
            {"groups": (2002, 2001)},
            {"groups": (2001, 2001)},
            {"groups": [2001]},
            {"groups": ()},
            {"name": "root/../other"},
            {"name": "-agent"},
            {"name": "a\n"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.reviewed, **changes)

    def test_unknown_wire_fields_do_not_imply_authority(self):
        for changes in ({"disposable": True}, {"grants_access": True}):
            with self.assertRaises(ValueError):
                RetainedSecondaryTestIdentity.from_dict(
                    self.reviewed.to_dict() | changes
                )

    def test_unsupported_platform_refuses_without_posix_lookup(self):
        with patch("os.name", "nt"), self.assertRaisesRegex(ValueError, "unsupported"):
            require_current(self.reviewed)

    def test_rejects_untyped_configuration(self):
        with self.assertRaises(TypeError):
            require_current(self.reviewed.to_dict())

    @unittest.skipUnless(os.name == "posix", "requires POSIX account database")
    def test_actual_current_account_observation(self):
        import pwd

        account = pwd.getpwuid(os.getuid())
        groups = tuple(sorted(set(os.getgrouplist(account.pw_name, account.pw_gid))))
        if 0 in (account.pw_uid, account.pw_gid, *groups):
            self.skipTest("current account belongs to a privileged numeric group")
        reviewed = RetainedSecondaryTestIdentity(
            account.pw_name, account.pw_uid, account.pw_gid, groups
        )
        self.assertEqual(require_current(reviewed), reviewed)

    @unittest.skipUnless(os.name == "posix", "requires POSIX account database")
    def test_identity_and_group_drift_refuse(self):
        account = SimpleNamespace(pw_name="litaitest", pw_uid=2001, pw_gid=2001)
        for named, numbered, groups, error in (
            (replace_account(account, pw_uid=2002), account, [2001], "mapping-changed"),
            (
                account,
                replace_account(account, pw_name="other"),
                [2001],
                "mapping-changed",
            ),
            (account, account, [2001, 2002], "membership-changed"),
        ):
            with (
                self.subTest(error=error),
                patch("pwd.getpwnam", return_value=named),
                patch("pwd.getpwuid", return_value=numbered),
                patch("os.getgrouplist", return_value=groups),
                self.assertRaisesRegex(ValueError, error),
            ):
                require_current(self.reviewed)

    @unittest.skipUnless(os.name == "posix", "requires POSIX account database")
    def test_account_changes_during_observation_refuse(self):
        account = SimpleNamespace(pw_name="litaitest", pw_uid=2001, pw_gid=2001)
        with (
            patch(
                "pwd.getpwnam",
                side_effect=[account, replace_account(account, pw_uid=2002)],
            ),
            patch("pwd.getpwuid", return_value=account),
            patch("os.getgrouplist", return_value=[2001]),
            self.assertRaisesRegex(ValueError, "observation-changed"),
        ):
            require_current(self.reviewed)

    @unittest.skipUnless(os.name == "posix", "requires POSIX account database")
    def test_disappeared_account_refuses(self):
        with (
            patch("pwd.getpwnam", side_effect=KeyError),
            self.assertRaisesRegex(ValueError, "unavailable"),
        ):
            require_current(self.reviewed)


def replace_account(account, **changes):
    return SimpleNamespace(**(vars(account) | changes))
