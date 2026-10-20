"""Reviewed read-only views and fresh fixtures; configuration grants no access."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .identity import ContentIdentity, canonical_identity
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths
from .retained_project import RetainedProjectManifest
from .retained_project_lifecycle import _fields, _name
from .retained_test_identity import RetainedSecondaryTestIdentity


def _view_name(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", value) is None
    ):
        raise ValueError("retained.secondary.view-name-invalid")
    canonical_relative_posix_path(value, label="retained secondary namespace")


@dataclass(frozen=True, slots=True)
class RetainedReadOnlyView:
    name: str
    origin: str
    prefix: str

    def __post_init__(self):
        _view_name(self.name)
        if self.origin not in {"inputs", "exports"}:
            raise ValueError("retained.secondary.view-origin-invalid")
        canonical_relative_posix_path(self.prefix, label="retained readonly view")

    def to_dict(self):
        return {"name": self.name, "origin": self.origin, "prefix": self.prefix}

    def bind_manifest(self, manifest: RetainedProjectManifest) -> ContentIdentity:
        """Require complete alias closure inside this selected directory view.

        The full manifest already rejects missing targets, cycles and intermediate
        symlinks. A view must additionally reject paths that traverse above its
        mount root, even when normalization would end inside the selected prefix.
        This pure binding neither mounts a filesystem nor grants permissions.
        """
        if not isinstance(manifest, RetainedProjectManifest):
            raise TypeError("retained.secondary.typed-manifest-required")
        if self.prefix not in manifest.directories:
            raise ValueError("retained.secondary.declared-directory-required")
        prefix = self.prefix + "/"
        depth = len(self.prefix.split("/"))
        for member in manifest.members:
            if not member.path.startswith(prefix) or member.symlink_target is None:
                continue
            cursor = member.path.split("/")[:-1]
            for part in member.symlink_target.split("/"):
                if part in ("", "."):
                    continue
                if part == "..":
                    if len(cursor) <= depth:
                        raise ValueError("retained.secondary.alias-escapes-view")
                    cursor.pop()
                else:
                    cursor.append(part)
        return canonical_identity(
            {"view": self.to_dict(), "manifest_identity": manifest.identity.uri}
        )

    @classmethod
    def from_dict(cls, value):
        value = _fields(value, {"name", "origin", "prefix"})
        return cls(value["name"], value["origin"], value["prefix"])


@dataclass(frozen=True, slots=True)
class RetainedSecondaryAccessPolicy:
    worker_identity: ContentIdentity
    account: RetainedSecondaryTestIdentity
    action_ids: tuple[str, ...]
    views: tuple[RetainedReadOnlyView, ...]
    fixtures: tuple[str, ...]

    SCHEMA = "literate-ai/retained-secondary-access-policy@1"

    def __post_init__(self):
        if not isinstance(self.worker_identity, ContentIdentity) or not isinstance(
            self.account, RetainedSecondaryTestIdentity
        ):
            raise TypeError("retained.secondary.typed-worker-and-account-required")
        for values in (self.action_ids, self.views, self.fixtures):
            if type(values) is not tuple or not 1 <= len(values) <= 32:
                raise ValueError("retained.secondary.bounded-nonempty-tuples-required")
        for action in self.action_ids:
            _name(action)
        if len(set(self.action_ids)) != len(self.action_ids):
            raise ValueError("retained.secondary.duplicate-action")
        names = set()
        for view in self.views:
            if not isinstance(view, RetainedReadOnlyView):
                raise TypeError("retained.secondary.typed-views-required")
            if view.name in names:
                raise ValueError("retained.secondary.namespace-collision")
            names.add(view.name)
        for fixture in self.fixtures:
            _view_name(fixture)
            if fixture in names:
                raise ValueError("retained.secondary.namespace-collision")
            names.add(fixture)
        for origin in ("inputs", "exports"):
            canonical_relative_posix_paths(
                (v.prefix for v in self.views if v.origin == origin),
                label="retained secondary disjoint view prefixes",
            )

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "worker_identity": self.worker_identity.uri,
            "account": self.account.to_dict(),
            "action_ids": list(self.action_ids),
            "views": [view.to_dict() for view in self.views],
            "fixtures": list(self.fixtures),
        }

    @classmethod
    def from_dict(cls, value):
        value = _fields(
            value,
            {"schema", "worker_identity", "account", "action_ids", "views", "fixtures"},
        )
        if value["schema"] != cls.SCHEMA or any(
            not isinstance(value[key], list)
            for key in ("action_ids", "views", "fixtures")
        ):
            raise ValueError("retained.secondary.policy-record-invalid")
        return cls(
            ContentIdentity.parse_uri(value["worker_identity"]),
            RetainedSecondaryTestIdentity.from_dict(value["account"]),
            tuple(value["action_ids"]),
            tuple(RetainedReadOnlyView.from_dict(v) for v in value["views"]),
            tuple(value["fixtures"]),
        )

    @property
    def identity(self):
        return canonical_identity(self.to_dict())
