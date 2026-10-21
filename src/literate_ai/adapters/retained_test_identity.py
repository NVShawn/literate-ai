"""Observe existing POSIX account mappings without creating or changing accounts.

An exact account observation is a prerequisite, never disposable-allocation or
permission authority. Re-observe around each future grant/command/cleanup; a
previous successful observation cannot authorize a later access by itself.
"""

from __future__ import annotations

import os

from literate_ai.contracts.retained_test_identity import RetainedSecondaryTestIdentity


def require_current_secondary_test_identity(
    reviewed: RetainedSecondaryTestIdentity,
) -> RetainedSecondaryTestIdentity:
    if not isinstance(reviewed, RetainedSecondaryTestIdentity):
        raise TypeError("retained.identity.typed-review-required")
    if os.name != "posix" or not hasattr(os, "getgrouplist"):
        raise ValueError("retained.identity.observation-unsupported")
    # Import here so merely importing this module remains portable on Windows.
    import pwd

    try:
        named = pwd.getpwnam(reviewed.name)
        numbered = pwd.getpwuid(reviewed.uid)
        if (named.pw_name, named.pw_uid, named.pw_gid) != (
            reviewed.name,
            reviewed.uid,
            reviewed.gid,
        ) or (numbered.pw_name, numbered.pw_uid, numbered.pw_gid) != (
            reviewed.name,
            reviewed.uid,
            reviewed.gid,
        ):
            raise ValueError("retained.identity.account-mapping-changed")
        groups = tuple(sorted(set(os.getgrouplist(reviewed.name, reviewed.gid))))
        if groups != reviewed.groups:
            raise ValueError("retained.identity.group-membership-changed")
        # NSS can change while querying supplementary groups. Require both
        # directions again instead of treating one username lookup as custody.
        if (
            pwd.getpwnam(reviewed.name) != named
            or pwd.getpwuid(reviewed.uid) != numbered
            or tuple(sorted(set(os.getgrouplist(reviewed.name, reviewed.gid))))
            != groups
        ):
            raise ValueError("retained.identity.observation-changed")
    except (KeyError, OSError, OverflowError) as exc:
        raise ValueError("retained.identity.observation-unavailable") from exc
    return reviewed
