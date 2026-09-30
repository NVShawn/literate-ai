"""Typed, immutable HTML view records over canonical JSON authority.

These records do not render, fetch assets, grant execution, or publish a cache.
Shape validation is complemented by content-identity and cross-field checks;
filesystem containment and the actual HTML/reference attestation belong to the
emitter. No artifact may embed its own final byte identity in provenance.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime
from typing import Any, ClassVar, Self
from urllib.parse import urlsplit

from ._validation import (
    contract_fields,
    fail,
    fields,
    int_value,
    parse_tuple,
    string_value,
    unique,
)
from .identity import ContentIdentity, HashAlgorithm, canonical_identity
from .paths import canonical_relative_posix_path
from .versioning import semantic_version

_PREFIX = "urn:literate-ai:schema:v1:html-observability-"
_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
_URN = re.compile(r"urn:literate-ai:schema:v[0-9]+:[a-z0-9][a-z0-9-]{0,126}")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_HTTPS = re.compile(r"https://[^\s\"'<>]{1,2040}")
_REFUSALS = frozenset(
    {
        "render.unknown_view",
        "render.unsupported_surface",
        "render.surface_unavailable",
        "render.external_asset_unpinned",
        "render.external_asset_forbidden",
        "render.output_outside_project",
        "render.cache_write_forbidden",
    }
)


def _pattern(value: Any, pattern: re.Pattern[str], path: str) -> None:
    if pattern.fullmatch(string_value(value, path)) is None:
        fail(path, "does not match the required portable representation")


def _choice(value: Any, choices: tuple[str, ...] | frozenset[str], path: str) -> None:
    if string_value(value, path) not in choices:
        fail(path, "must be one of: " + ", ".join(sorted(choices)))


def _typed(value: Any, expected: type, path: str) -> None:
    if not isinstance(value, expected):
        fail(path, f"must be a typed {expected.__name__}")


def _tuple(value: Any, expected: type, path: str, minimum: int, maximum: int) -> None:
    if not isinstance(value, tuple) or not minimum <= len(value) <= maximum:
        fail(path, f"must be an immutable tuple of {minimum} to {maximum} items")
    for index, item in enumerate(value):
        _typed(item, expected, f"{path}[{index}]")


def _html_path(value: Any, path: str) -> None:
    try:
        relative = canonical_relative_posix_path(value, label=path)
    except (TypeError, ValueError):
        fail(path, "must be a canonical portable relative path")
    if relative.suffix != ".html":
        fail(path, "must name an .html artifact")


def _wire(value: Any) -> Any:
    if isinstance(value, (_Record, ContentIdentity)):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_wire(item) for item in value]
    return value


class _Record:
    """Local serialization glue; dataclass fields are the one Python field list."""

    __slots__ = ()
    SCHEMA: ClassVar[str | None] = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            item.name: _wire(getattr(self, item.name))
            for item in dataclass_fields(self)
        }
        return {"schema": self.SCHEMA, **result} if self.SCHEMA else result

    @classmethod
    def _read(cls, value: Any, path: str) -> dict[str, Any]:
        required = frozenset(item.name for item in dataclass_fields(cls))
        if cls.SCHEMA:
            data = contract_fields(
                value, path=path, schema_uri=cls.SCHEMA, required=required
            )
        else:
            data = fields(value, path=path, required=required)
        return {name: data[name] for name in required}

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "HtmlRecord") -> Self:
        return cls(**cls._read(value, path))


@dataclass(frozen=True, slots=True)
class HtmlView(_Record):
    view_id: str
    view_version: str
    scope_kind: str
    scope_identifier: str

    SCHEMA: ClassVar[str] = _PREFIX + "view"

    def __post_init__(self) -> None:
        _pattern(self.view_id, _ID, "HtmlView.view_id")
        semantic_version(self.view_version, "HtmlView.view_version")
        _choice(self.scope_kind, ("project", "component"), "HtmlView.scope_kind")
        _pattern(self.scope_identifier, _ID, "HtmlView.scope_identifier")


@dataclass(frozen=True, slots=True)
class HtmlSourceBinding(_Record):
    source_label: str
    source_schema: str
    source_identity: ContentIdentity

    SCHEMA: ClassVar[str] = _PREFIX + "source-binding"

    def __post_init__(self) -> None:
        _pattern(self.source_label, _ID, "HtmlSourceBinding.source_label")
        _pattern(self.source_schema, _URN, "HtmlSourceBinding.source_schema")
        _typed(
            self.source_identity, ContentIdentity, "HtmlSourceBinding.source_identity"
        )

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "HtmlSourceBinding") -> Self:
        data = cls._read(value, path)
        data["source_identity"] = ContentIdentity.from_dict(
            data["source_identity"], path=f"{path}.source_identity"
        )
        return cls(**data)


@dataclass(frozen=True, slots=True)
class HtmlSurface(_Record):
    surface_id: str
    source_schema: str
    view_ids: tuple[str, ...]

    SCHEMA: ClassVar[str] = _PREFIX + "surface"

    def __post_init__(self) -> None:
        _pattern(self.surface_id, _ID, "HtmlSurface.surface_id")
        _pattern(self.source_schema, _URN, "HtmlSurface.source_schema")
        _tuple(self.view_ids, str, "HtmlSurface.view_ids", 1, 64)
        for view_id in self.view_ids:
            _pattern(view_id, _ID, "HtmlSurface.view_ids")
        unique(self.view_ids, "HtmlSurface.view_ids")

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "HtmlSurface") -> Self:
        data = cls._read(value, path)
        data["view_ids"] = parse_tuple(
            data["view_ids"],
            f"{path}.view_ids",
            lambda value, *, path: string_value(value, path),
        )
        return cls(**data)


@dataclass(frozen=True, slots=True)
class HtmlExternalAsset(_Record):
    asset_id: str
    url: str
    integrity: str
    crossorigin: str
    asset_kind: str

    SCHEMA: ClassVar[str] = _PREFIX + "external-asset"

    def __post_init__(self) -> None:
        _pattern(self.asset_id, _ID, "HtmlExternalAsset.asset_id")
        _pattern(self.url, _HTTPS, "HtmlExternalAsset.url")
        try:
            parsed = urlsplit(self.url)
            if (
                not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or "\\" in self.url
                or any(ord(character) < 32 for character in self.url)
            ):
                raise ValueError
            _ = parsed.port  # Access validates malformed or out-of-range ports.
        except ValueError:
            fail("HtmlExternalAsset.url", "must be an HTTPS URL without credentials")
        algorithm, _, encoded = string_value(
            self.integrity, "HtmlExternalAsset.integrity"
        ).partition("-")
        try:
            expected = {"sha256": 32, "sha384": 48, "sha512": 64}[algorithm]
            digest = base64.b64decode(encoded, validate=True)
            if (
                len(digest) != expected
                or base64.b64encode(digest).decode("ascii") != encoded
            ):
                raise ValueError
        except (KeyError, ValueError, binascii.Error):
            fail(
                "HtmlExternalAsset.integrity",
                "must be one canonical SHA-256/384/512 SRI hash",
            )
        _choice(self.crossorigin, ("anonymous",), "HtmlExternalAsset.crossorigin")
        _choice(
            self.asset_kind, ("script", "stylesheet"), "HtmlExternalAsset.asset_kind"
        )


@dataclass(frozen=True, slots=True)
class HtmlRendererBinding(_Record):
    renderer_id: str
    renderer_version: str
    framework_distribution_identity: ContentIdentity
    schema_catalog_release: str
    template_identity: ContentIdentity

    SCHEMA: ClassVar[str] = _PREFIX + "renderer-binding"

    def __post_init__(self) -> None:
        _pattern(self.renderer_id, _ID, "HtmlRendererBinding.renderer_id")
        semantic_version(self.renderer_version, "HtmlRendererBinding.renderer_version")
        semantic_version(
            self.schema_catalog_release, "HtmlRendererBinding.schema_catalog_release"
        )
        _typed(
            self.framework_distribution_identity,
            ContentIdentity,
            "HtmlRendererBinding.framework_distribution_identity",
        )
        _typed(
            self.template_identity,
            ContentIdentity,
            "HtmlRendererBinding.template_identity",
        )

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "HtmlRendererBinding") -> Self:
        data = cls._read(value, path)
        for name in ("framework_distribution_identity", "template_identity"):
            data[name] = ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
        return cls(**data)


def render_inputs_identity(
    source_bindings: tuple[HtmlSourceBinding, ...],
    view: HtmlView,
    renderer: HtmlRendererBinding,
) -> ContentIdentity:
    """Hash ordered canonical inputs, excluding clocks and task/correlation IDs."""
    _tuple(source_bindings, HtmlSourceBinding, "render_inputs.source_bindings", 1, 256)
    _typed(view, HtmlView, "render_inputs.view")
    _typed(renderer, HtmlRendererBinding, "render_inputs.renderer")
    unique(
        tuple(item.source_label for item in source_bindings),
        "render_inputs.source_labels",
    )
    return canonical_identity(
        {
            "source_bindings": [item.to_dict() for item in source_bindings],
            "view": view.to_dict(),
            "renderer": renderer.to_dict(),
        }
    )


@dataclass(frozen=True, slots=True)
class HtmlProvenance(_Record):
    view: HtmlView
    source_bindings: tuple[HtmlSourceBinding, ...]
    renderer: HtmlRendererBinding
    external_assets: tuple[HtmlExternalAsset, ...]
    generated_at: str
    render_inputs_identity: ContentIdentity
    provenance_identity: ContentIdentity

    SCHEMA: ClassVar[str] = _PREFIX + "provenance"

    def __post_init__(self) -> None:
        expected = render_inputs_identity(
            self.source_bindings, self.view, self.renderer
        )
        _tuple(
            self.external_assets,
            HtmlExternalAsset,
            "HtmlProvenance.external_assets",
            0,
            32,
        )
        unique(
            tuple(item.asset_id for item in self.external_assets),
            "HtmlProvenance.asset_ids",
        )
        _pattern(self.generated_at, _UTC, "HtmlProvenance.generated_at")
        try:
            datetime.strptime(self.generated_at, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            fail("HtmlProvenance.generated_at", "must be a real UTC calendar instant")
        _typed(
            self.render_inputs_identity,
            ContentIdentity,
            "HtmlProvenance.render_inputs_identity",
        )
        _typed(
            self.provenance_identity,
            ContentIdentity,
            "HtmlProvenance.provenance_identity",
        )
        if self.render_inputs_identity != expected:
            fail("HtmlProvenance.render_inputs_identity", "does not bind these inputs")
        content = self.to_dict()
        del content["provenance_identity"]
        if self.provenance_identity != canonical_identity(content):
            fail("HtmlProvenance.provenance_identity", "does not bind this provenance")

    @classmethod
    def create(
        cls,
        *,
        view: HtmlView,
        source_bindings: tuple[HtmlSourceBinding, ...],
        renderer: HtmlRendererBinding,
        external_assets: tuple[HtmlExternalAsset, ...],
        generated_at: str,
    ) -> Self:
        inputs = render_inputs_identity(source_bindings, view, renderer)
        _tuple(
            external_assets, HtmlExternalAsset, "HtmlProvenance.external_assets", 0, 32
        )
        content = {
            "schema": cls.SCHEMA,
            "view": view.to_dict(),
            "source_bindings": [item.to_dict() for item in source_bindings],
            "renderer": renderer.to_dict(),
            "external_assets": [item.to_dict() for item in external_assets],
            "generated_at": generated_at,
            "render_inputs_identity": inputs.to_dict(),
        }
        return cls(
            view,
            source_bindings,
            renderer,
            external_assets,
            generated_at,
            inputs,
            canonical_identity(content),
        )

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "HtmlProvenance") -> Self:
        data = cls._read(value, path)
        data["view"] = HtmlView.from_dict(data["view"], path=f"{path}.view")
        data["renderer"] = HtmlRendererBinding.from_dict(
            data["renderer"], path=f"{path}.renderer"
        )
        data["source_bindings"] = parse_tuple(
            data["source_bindings"],
            f"{path}.source_bindings",
            HtmlSourceBinding.from_dict,
        )
        data["external_assets"] = parse_tuple(
            data["external_assets"],
            f"{path}.external_assets",
            HtmlExternalAsset.from_dict,
        )
        for name in ("render_inputs_identity", "provenance_identity"):
            data[name] = ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class HtmlEmbedding(_Record):
    single_file: bool
    companion_asset_count: int
    inline_script_count: int
    inline_style_count: int
    external_reference_count: int

    def __post_init__(self) -> None:
        if self.single_file is not True:
            fail("HtmlEmbedding.single_file", "must be true")
        int_value(
            self.companion_asset_count, "HtmlEmbedding.companion_asset_count", maximum=0
        )
        int_value(self.inline_script_count, "HtmlEmbedding.inline_script_count")
        int_value(self.inline_style_count, "HtmlEmbedding.inline_style_count")
        int_value(
            self.external_reference_count,
            "HtmlEmbedding.external_reference_count",
            maximum=32,
        )


@dataclass(frozen=True, slots=True)
class HtmlArtifact(_Record):
    artifact_path: str
    media_type: str
    artifact_identity: ContentIdentity
    byte_size: int
    provenance: HtmlProvenance
    embedding: HtmlEmbedding

    SCHEMA: ClassVar[str] = _PREFIX + "artifact"

    def __post_init__(self) -> None:
        _html_path(self.artifact_path, "HtmlArtifact.artifact_path")
        _choice(self.media_type, ("text/html",), "HtmlArtifact.media_type")
        _typed(
            self.artifact_identity, ContentIdentity, "HtmlArtifact.artifact_identity"
        )
        int_value(self.byte_size, "HtmlArtifact.byte_size", minimum=1)
        _typed(self.provenance, HtmlProvenance, "HtmlArtifact.provenance")
        _typed(self.embedding, HtmlEmbedding, "HtmlArtifact.embedding")
        if self.embedding.external_reference_count != len(
            self.provenance.external_assets
        ):
            fail(
                "HtmlArtifact.embedding.external_reference_count",
                "does not match declared assets",
            )

    @classmethod
    def from_bytes(
        cls,
        *,
        artifact_path: str,
        content: bytes,
        provenance: HtmlProvenance,
        embedding: HtmlEmbedding,
    ) -> Self:
        """Bind completed bytes; the emitter still owns actual HTML inspection."""
        if not isinstance(content, bytes):
            fail("HtmlArtifact.content", "must be immutable completed bytes")
        return cls(
            artifact_path,
            "text/html",
            ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()),
            len(content),
            provenance,
            embedding,
        )

    def matches_bytes(self, content: bytes) -> bool:
        if not isinstance(content, bytes):
            fail("HtmlArtifact.content", "must be immutable completed bytes")
        return (
            len(content) == self.byte_size
            and hashlib.sha256(content).hexdigest() == self.artifact_identity.digest
        )

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "HtmlArtifact") -> Self:
        data = cls._read(value, path)
        data["artifact_identity"] = ContentIdentity.from_dict(
            data["artifact_identity"], path=f"{path}.artifact_identity"
        )
        data["provenance"] = HtmlProvenance.from_dict(
            data["provenance"], path=f"{path}.provenance"
        )
        data["embedding"] = HtmlEmbedding.from_dict(
            data["embedding"], path=f"{path}.embedding"
        )
        return cls(**data)


@dataclass(frozen=True, slots=True)
class HtmlRenderRequest(_Record):
    surface_id: str
    view: HtmlView
    output_path: str
    cache_mode: str
    external_asset_policy: str

    SCHEMA: ClassVar[str] = _PREFIX + "render-request"

    def __post_init__(self) -> None:
        _pattern(self.surface_id, _ID, "HtmlRenderRequest.surface_id")
        _typed(self.view, HtmlView, "HtmlRenderRequest.view")
        _html_path(self.output_path, "HtmlRenderRequest.output_path")
        _choice(
            self.cache_mode,
            ("off", "read-only", "write-only", "read-write"),
            "HtmlRenderRequest.cache_mode",
        )
        _choice(
            self.external_asset_policy,
            ("inline-only", "pinned-cdn"),
            "HtmlRenderRequest.external_asset_policy",
        )

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "HtmlRenderRequest") -> Self:
        data = cls._read(value, path)
        data["view"] = HtmlView.from_dict(data["view"], path=f"{path}.view")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class HtmlRenderRefusal(_Record):
    code: str
    message: str

    SCHEMA: ClassVar[str] = _PREFIX + "render-refusal"

    def __post_init__(self) -> None:
        _choice(self.code, _REFUSALS, "HtmlRenderRefusal.code")
        string_value(self.message, "HtmlRenderRefusal.message", max_length=2048)


@dataclass(frozen=True, slots=True)
class HtmlRenderResult(_Record):
    status: str
    artifact: HtmlArtifact | None
    refusal: HtmlRenderRefusal | None

    SCHEMA: ClassVar[str] = _PREFIX + "render-result"

    def __post_init__(self) -> None:
        _choice(
            self.status, ("rendered", "cached", "refused"), "HtmlRenderResult.status"
        )
        if self.status == "refused":
            _typed(self.refusal, HtmlRenderRefusal, "HtmlRenderResult.refusal")
            if self.artifact is not None:
                fail("HtmlRenderResult.artifact", "must be null for a refused result")
        else:
            _typed(self.artifact, HtmlArtifact, "HtmlRenderResult.artifact")
            if self.refusal is not None:
                fail("HtmlRenderResult.refusal", "must be null for a successful result")

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "HtmlRenderResult") -> Self:
        data = cls._read(value, path)
        if data["artifact"] is not None:
            data["artifact"] = HtmlArtifact.from_dict(
                data["artifact"], path=f"{path}.artifact"
            )
        if data["refusal"] is not None:
            data["refusal"] = HtmlRenderRefusal.from_dict(
                data["refusal"], path=f"{path}.refusal"
            )
        return cls(**data)


@dataclass(frozen=True, slots=True)
class HtmlStalenessReport(_Record):
    """One read-only observation; unavailable provenance is never current evidence."""

    artifact_path: str
    status: str
    expected_render_inputs_identity: ContentIdentity
    observed_render_inputs_identity: ContentIdentity | None
    stale_source_labels: tuple[str, ...]

    SCHEMA: ClassVar[str] = _PREFIX + "staleness-report"

    def __post_init__(self) -> None:
        _html_path(self.artifact_path, "HtmlStalenessReport.artifact_path")
        _choice(
            self.status,
            ("current", "stale", "missing", "unreadable", "unpinned"),
            "HtmlStalenessReport.status",
        )
        _typed(
            self.expected_render_inputs_identity,
            ContentIdentity,
            "HtmlStalenessReport.expected_render_inputs_identity",
        )
        _tuple(
            self.stale_source_labels,
            str,
            "HtmlStalenessReport.stale_source_labels",
            1 if self.status == "stale" else 0,
            256 if self.status == "stale" else 0,
        )
        for label in self.stale_source_labels:
            _pattern(label, _ID, "HtmlStalenessReport.stale_source_labels")
        unique(self.stale_source_labels, "HtmlStalenessReport.stale_source_labels")
        if self.status in ("current", "stale"):
            _typed(
                self.observed_render_inputs_identity,
                ContentIdentity,
                "HtmlStalenessReport.observed_render_inputs_identity",
            )
            equal = (
                self.expected_render_inputs_identity
                == self.observed_render_inputs_identity
            )
            if equal != (self.status == "current"):
                fail(
                    "HtmlStalenessReport.status",
                    "must agree with equality of expected and observed inputs",
                )
        elif self.observed_render_inputs_identity is not None:
            fail(
                "HtmlStalenessReport.observed_render_inputs_identity",
                "must be null when provenance is unavailable",
            )

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "HtmlStalenessReport") -> Self:
        data = cls._read(value, path)
        data["expected_render_inputs_identity"] = ContentIdentity.from_dict(
            data["expected_render_inputs_identity"],
            path=f"{path}.expected_render_inputs_identity",
        )
        if data["observed_render_inputs_identity"] is not None:
            data["observed_render_inputs_identity"] = ContentIdentity.from_dict(
                data["observed_render_inputs_identity"],
                path=f"{path}.observed_render_inputs_identity",
            )
        data["stale_source_labels"] = parse_tuple(
            data["stale_source_labels"],
            f"{path}.stale_source_labels",
            lambda value, *, path: string_value(value, path),
        )
        return cls(**data)
