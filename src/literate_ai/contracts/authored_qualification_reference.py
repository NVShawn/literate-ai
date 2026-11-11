"""A published authored result location, never importer or execution permission."""

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields, fail
from .blobs import BlobRef
from .identity import ContentIdentity, contract_identity
from .qualification_origins import AuthoredQualificationOrigin


@dataclass(frozen=True, slots=True)
class AuthoredQualificationReference:
    origin: AuthoredQualificationOrigin
    plan_identity: ContentIdentity
    qualification_identity: ContentIdentity
    products_identity: ContentIdentity
    intent_identity: ContentIdentity
    stage_identity: ContentIdentity
    archive: BlobRef
    attempt: str

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:authored-qualification-reference"
    _IDENTITIES: ClassVar[tuple[str, ...]] = (
        "plan_identity",
        "qualification_identity",
        "products_identity",
        "intent_identity",
        "stage_identity",
    )

    def __post_init__(self):
        if not isinstance(self.origin, AuthoredQualificationOrigin):
            fail("origin", "must be an authored qualification origin")
        for name in self._IDENTITIES:
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(name, "must be a ContentIdentity")
        if (
            not isinstance(self.archive, BlobRef)
            or self.archive.media_type != "application/zip"
            or self.archive.size <= 0
        ):
            fail("archive", "must name the complete immutable qualification archive")
        if (
            not isinstance(self.attempt, str)
            or len(self.attempt) != 32
            or any(c not in "0123456789abcdef" for c in self.attempt)
        ):
            fail("attempt", "must be an exact publication attempt key")

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "origin": self.origin.to_dict(),
            **{n: getattr(self, n).to_dict() for n in self._IDENTITIES},
            "archive": self.archive.to_dict(),
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"origin", *cls._IDENTITIES, "archive", "attempt"}),
        )
        return cls(
            AuthoredQualificationOrigin.from_dict(data["origin"]),
            **{n: ContentIdentity.from_dict(data[n]) for n in cls._IDENTITIES},
            archive=BlobRef.from_dict(data["archive"]),
            attempt=data["attempt"],
        )
