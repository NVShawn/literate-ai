"""Immutable origin and baseline evidence for initialized projects."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ._validation import contract_fields, fail, int_value, list_value, string_value
from .identity import ContentIdentity, contract_identity

INITIALIZATION_ORIGIN_SCHEMA = "urn:literate-ai:schema:v1:project-initialization-origin"
INITIALIZATION_BASELINE_FILE_SCHEMA = (
    "urn:literate-ai:schema:v1:project-initialization-baseline-file"
)
INITIALIZATION_BASELINE_SCHEMA = (
    "urn:literate-ai:schema:v1:project-initialization-baseline"
)
PROJECT_TEMPLATE_PROTOCOL = "literate-ai/project-template@1"

_GIT_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True, slots=True)
class ProjectInitializationOrigin:
    """Exact source repository and revision of the initializing distribution."""

    repository_url: str
    git_revision: str
    distribution_name: str
    distribution_version: str

    SCHEMA: ClassVar[str] = INITIALIZATION_ORIGIN_SCHEMA

    def __post_init__(self) -> None:
        for field, value in (
            ("repository_url", self.repository_url),
            ("distribution_name", self.distribution_name),
            ("distribution_version", self.distribution_version),
        ):
            text = string_value(value, f"ProjectInitializationOrigin.{field}")
            if any(ord(character) < 32 for character in text):
                fail(
                    f"ProjectInitializationOrigin.{field}",
                    "must not contain control characters",
                )
        parsed_repository = urlsplit(self.repository_url)
        if parsed_repository.password is not None or (
            parsed_repository.scheme in {"http", "https"}
            and (
                parsed_repository.username is not None
                or parsed_repository.query
                or parsed_repository.fragment
            )
        ):
            fail(
                "ProjectInitializationOrigin.repository_url",
                "must not contain credentials, query parameters, or fragments",
            )
        if not _GIT_REVISION.fullmatch(self.git_revision):
            fail(
                "ProjectInitializationOrigin.git_revision",
                "must be an exact 40- or 64-character lower-case Git object ID",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "repository_url": self.repository_url,
            "git_revision": self.git_revision,
            "distribution_name": self.distribution_name,
            "distribution_version": self.distribution_version,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProjectInitializationOrigin"
    ) -> ProjectInitializationOrigin:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "repository_url",
                    "git_revision",
                    "distribution_name",
                    "distribution_version",
                }
            ),
        )
        return cls(
            repository_url=string_value(
                data["repository_url"], f"{path}.repository_url"
            ),
            git_revision=string_value(data["git_revision"], f"{path}.git_revision"),
            distribution_name=string_value(
                data["distribution_name"], f"{path}.distribution_name"
            ),
            distribution_version=string_value(
                data["distribution_version"], f"{path}.distribution_version"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectInitializationBaselineFile:
    """One exact framework-owned file created by initialization."""

    path: str
    size: int
    identity: ContentIdentity

    SCHEMA: ClassVar[str] = INITIALIZATION_BASELINE_FILE_SCHEMA

    def __post_init__(self) -> None:
        raw = string_value(self.path, "ProjectInitializationBaselineFile.path")
        parsed = PurePosixPath(raw)
        if (
            parsed.is_absolute()
            or not parsed.parts
            or ".." in parsed.parts
            or parsed.as_posix() != raw
            or "\\" in raw
        ):
            fail(
                "ProjectInitializationBaselineFile.path",
                "must be a normalized project-relative POSIX path",
            )
        int_value(
            self.size,
            "ProjectInitializationBaselineFile.size",
            maximum=2**63 - 1,
        )
        if not isinstance(self.identity, ContentIdentity):
            fail(
                "ProjectInitializationBaselineFile.identity",
                "must be a ContentIdentity",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "path": self.path,
            "size": self.size,
            "identity": self.identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProjectInitializationBaselineFile"
    ) -> ProjectInitializationBaselineFile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"path", "size", "identity"}),
        )
        return cls(
            path=string_value(data["path"], f"{path}.path"),
            size=int_value(data["size"], f"{path}.size"),
            identity=ContentIdentity.from_dict(
                data["identity"], path=f"{path}.identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProjectInitializationBaseline:
    """Content-addressed initial framework-owned project state."""

    origin_identity: ContentIdentity
    template_protocol: str
    files: tuple[ProjectInitializationBaselineFile, ...]

    SCHEMA: ClassVar[str] = INITIALIZATION_BASELINE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.origin_identity, ContentIdentity):
            fail(
                "ProjectInitializationBaseline.origin_identity",
                "must be a ContentIdentity",
            )
        if self.template_protocol != PROJECT_TEMPLATE_PROTOCOL:
            fail(
                "ProjectInitializationBaseline.template_protocol",
                f"must be {PROJECT_TEMPLATE_PROTOCOL!r}",
            )
        if not isinstance(self.files, tuple) or not self.files:
            fail("ProjectInitializationBaseline.files", "must be a nonempty tuple")
        if any(
            not isinstance(item, ProjectInitializationBaselineFile)
            for item in self.files
        ):
            fail(
                "ProjectInitializationBaseline.files",
                "must contain baseline file records",
            )
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            fail(
                "ProjectInitializationBaseline.files",
                "must be uniquely sorted by path",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "origin_identity": self.origin_identity.to_dict(),
            "template_protocol": self.template_protocol,
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ProjectInitializationBaseline"
    ) -> ProjectInitializationBaseline:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"origin_identity", "template_protocol", "files"}),
        )
        files = tuple(
            ProjectInitializationBaselineFile.from_dict(
                item, path=f"{path}.files[{index}]"
            )
            for index, item in enumerate(list_value(data["files"], f"{path}.files"))
        )
        return cls(
            origin_identity=ContentIdentity.from_dict(
                data["origin_identity"], path=f"{path}.origin_identity"
            ),
            template_protocol=string_value(
                data["template_protocol"], f"{path}.template_protocol"
            ),
            files=files,
        )


__all__ = [
    "INITIALIZATION_BASELINE_FILE_SCHEMA",
    "INITIALIZATION_BASELINE_SCHEMA",
    "INITIALIZATION_ORIGIN_SCHEMA",
    "PROJECT_TEMPLATE_PROTOCOL",
    "ProjectInitializationBaseline",
    "ProjectInitializationBaselineFile",
    "ProjectInitializationOrigin",
]
