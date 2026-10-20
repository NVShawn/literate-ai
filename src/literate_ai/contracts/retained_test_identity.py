"""Reviewed numeric secondary identity; this record grants no workspace access."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .identity import canonical_identity
from .retained_project_lifecycle import _fields


@dataclass(frozen=True, slots=True)
class RetainedSecondaryTestIdentity:
    name: str
    uid: int
    gid: int
    groups: tuple[int, ...]

    SCHEMA = "literate-ai/retained-secondary-test-identity@1"

    def __post_init__(self):
        if (
            not isinstance(self.name, str)
            or re.fullmatch(r"[a-z_][a-z0-9_-]{0,62}\$?", self.name) is None
        ):
            raise ValueError("retained.identity.account-name-invalid")
        if (
            type(self.groups) is not tuple
            or not 1 <= len(self.groups) <= 1024
            or any(
                type(value) is not int or not 1 <= value < 2**32 - 1
                for value in (self.uid, self.gid, *self.groups)
            )
            or self.groups != tuple(sorted(set(self.groups)))
            or self.gid not in self.groups
        ):
            raise ValueError(
                "retained.identity.exact-nonroot-numeric-identity-required"
            )

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "name": self.name,
            "uid": self.uid,
            "gid": self.gid,
            "groups": list(self.groups),
        }

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"schema", "name", "uid", "gid", "groups"})
        if value["schema"] != cls.SCHEMA or not isinstance(value["groups"], list):
            raise ValueError("retained.identity.record-invalid")
        return cls(value["name"], value["uid"], value["gid"], tuple(value["groups"]))

    @property
    def identity(self):
        return canonical_identity(self.to_dict())
