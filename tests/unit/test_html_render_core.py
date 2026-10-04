"""Public render-record contracts, identities and adversarial wire inputs."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace

from jsonschema import Draft202012Validator

from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.html_observability import (
    HtmlArtifact,
    HtmlEmbedding,
    HtmlExternalAsset,
    HtmlProvenance,
    HtmlRendererBinding,
    HtmlRenderRefusal,
    HtmlRenderRequest,
    HtmlRenderResult,
    HtmlSourceBinding,
    HtmlView,
    render_inputs_identity,
)
from tests.support import fixtures_test_html_observability_schema as schema_tests


def identity(value: str) -> ContentIdentity:
    return canonical_identity(value)


def provenance(*, generated_at: str = "2026-09-11T12:00:00Z") -> HtmlProvenance:
    return HtmlProvenance.create(
        view=HtmlView("authority-graph", "1.0.0", "project", "example"),
        source_bindings=(
            HtmlSourceBinding(
                "component-locks",
                "urn:literate-ai:schema:v2:component-lock",
                identity("locks"),
            ),
            HtmlSourceBinding(
                "routing",
                "urn:literate-ai:schema:v2:model-route-decision",
                identity("routing"),
            ),
        ),
        renderer=HtmlRendererBinding(
            "render-html-observability",
            "1.0.0",
            identity("framework"),
            "1.1.0",
            identity("template"),
        ),
        external_assets=(
            HtmlExternalAsset(
                "graph-library",
                "https://cdn.example.test/graph@1.0.0.js",
                "sha384-" + "A" * 64,
                "anonymous",
                "script",
            ),
        ),
        generated_at=generated_at,
    )


class HtmlRenderCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Reuse the real schema resource registry without inheriting or importing
        # another TestCase into this module's discovery namespace.
        probe = schema_tests.HtmlObservabilitySchemaTests(
            "test_index_registration_is_exact"
        )
        probe.setUp()
        cls.registry = probe.registry

    def setUp(self) -> None:
        self.provenance = provenance()
        self.content = b"<!doctype html><title>Graph</title>"
        self.artifact = HtmlArtifact.from_bytes(
            artifact_path="generated/observability/graph.html",
            content=self.content,
            provenance=self.provenance,
            embedding=HtmlEmbedding(True, 0, 2, 1, 1),
        )
        self.request = HtmlRenderRequest(
            "authority-graph",
            self.provenance.view,
            "generated/observability/graph.html",
            "read-write",
            "pinned-cdn",
        )
        self.refusal = HtmlRenderRefusal("render.unknown_view", "No registered view")
        self.records = (
            self.provenance.view,
            *self.provenance.source_bindings,
            self.provenance.renderer,
            *self.provenance.external_assets,
            self.provenance,
            self.artifact.embedding,
            self.artifact,
            self.request,
            self.refusal,
            HtmlRenderResult("rendered", self.artifact, None),
            HtmlRenderResult("cached", self.artifact, None),
            HtmlRenderResult("refused", None, self.refusal),
        )

    def test_all_records_round_trip_through_json_and_the_accepted_schemas(self) -> None:
        for record in self.records:
            with self.subTest(
                record=type(record).__name__, status=getattr(record, "status", None)
            ):
                wire = json.loads(json.dumps(record.to_dict()))
                self.assertEqual(type(record).from_dict(wire), record)
                self.assertEqual(type(record).from_dict(wire).to_dict(), wire)
                if record.SCHEMA:
                    Draft202012Validator(
                        {"$ref": record.SCHEMA}, registry=self.registry
                    ).validate(wire)

    def test_every_record_is_closed_and_every_wire_field_is_required(self) -> None:
        for record in self.records:
            with self.subTest(record=type(record).__name__):
                raw = record.to_dict()
                with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
                    type(record).from_dict({**raw, "undeclared": True})
                for name in raw:
                    missing = {key: value for key, value in raw.items() if key != name}
                    with self.subTest(missing=name):
                        with self.assertRaises(ContractValidationError):
                            type(record).from_dict(missing)
                if record.SCHEMA:
                    with self.assertRaises(ContractValidationError):
                        type(record).from_dict({**raw, "schema": "urn:wrong"})

    def test_nested_unknown_fields_and_task_metadata_are_rejected(self) -> None:
        paths = (
            ("provenance", "view"),
            ("provenance", "renderer"),
            ("provenance", "renderer", "template_identity"),
            ("provenance", "source_bindings", 0),
            ("provenance", "external_assets", 0),
            ("embedding",),
        )
        for path in paths:
            with self.subTest(path=path):
                raw = self.artifact.to_dict()
                target = raw
                for key in path:
                    target = target[key]
                target["undeclared"] = "not authority"
                with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
                    HtmlArtifact.from_dict(raw)
        for field in ("task_id", "correlation_id", "model", "generated_at"):
            with self.subTest(request_field=field):
                with self.assertRaises(ContractValidationError):
                    HtmlRenderRequest.from_dict(
                        {**self.request.to_dict(), field: "external"}
                    )

    def test_identity_projection_is_exact_and_source_order_is_semantic(self) -> None:
        p = self.provenance
        expected = {
            "source_bindings": [item.to_dict() for item in p.source_bindings],
            "view": p.view.to_dict(),
            "renderer": p.renderer.to_dict(),
        }
        # Independent wire-byte calculation catches an added field or a different
        # identity envelope, not only two calls to the same implementation.
        raw = json.dumps(
            expected, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.assertEqual(
            p.render_inputs_identity.digest, hashlib.sha256(raw).hexdigest()
        )
        self.assertNotEqual(
            p.render_inputs_identity,
            render_inputs_identity(p.source_bindings[::-1], p.view, p.renderer),
        )
        self.assertEqual(
            HtmlProvenance.from_dict(
                json.loads(json.dumps(p.to_dict(), sort_keys=True))
            ),
            p,
        )

    def test_clock_changes_only_provenance_not_render_input_identity(self) -> None:
        later = provenance(generated_at="2026-09-12T12:00:00Z")
        self.assertEqual(
            self.provenance.render_inputs_identity, later.render_inputs_identity
        )
        self.assertNotEqual(
            self.provenance.provenance_identity, later.provenance_identity
        )
        raw = later.to_dict()
        self.assertNotIn("artifact_identity", raw)
        claimed = raw.pop("provenance_identity")
        self.assertEqual(ContentIdentity.from_dict(claimed), canonical_identity(raw))

    def test_each_source_view_and_renderer_change_invalidates_render_inputs(
        self,
    ) -> None:
        p = self.provenance
        cases = [
            (
                tuple(
                    replace(s, source_identity=identity("changed")) if i == index else s
                    for i, s in enumerate(p.source_bindings)
                ),
                p.view,
                p.renderer,
            )
            for index in range(len(p.source_bindings))
        ]
        cases.extend(
            (p.source_bindings, replace(p.view, **change), p.renderer)
            for change in (
                {"view_id": "other"},
                {"view_version": "1.0.1"},
                {"scope_kind": "component"},
                {"scope_identifier": "other"},
            )
        )
        cases.extend(
            (p.source_bindings, p.view, replace(p.renderer, **change))
            for change in (
                {"renderer_id": "other"},
                {"renderer_version": "1.0.1"},
                {"framework_distribution_identity": identity("other")},
                {"schema_catalog_release": "1.2.0"},
                {"template_identity": identity("other")},
            )
        )
        for sources, view, renderer in cases:
            with self.subTest(sources=sources, view=view, renderer=renderer):
                self.assertNotEqual(
                    p.render_inputs_identity,
                    render_inputs_identity(sources, view, renderer),
                )

    def test_tampered_provenance_refuses_even_when_its_outer_hash_is_recomputed(
        self,
    ) -> None:
        for rehash in (False, True):
            with self.subTest(rehash=rehash):
                raw = self.provenance.to_dict()
                raw["source_bindings"][0]["source_identity"] = identity(
                    "substitution"
                ).to_dict()
                if rehash:
                    del raw["provenance_identity"]
                    raw["provenance_identity"] = canonical_identity(raw).to_dict()
                with self.assertRaisesRegex(
                    ContractValidationError, "does not bind these inputs"
                ):
                    HtmlProvenance.from_dict(raw)
        for name in ("generated_at", "external_assets", "provenance_identity"):
            raw = self.provenance.to_dict()
            raw[name] = {
                "generated_at": "2026-09-12T12:00:00Z",
                "external_assets": [],
                "provenance_identity": identity("forged").to_dict(),
            }[name]
            with self.subTest(field=name):
                with self.assertRaisesRegex(
                    ContractValidationError, "does not bind this provenance"
                ):
                    HtmlProvenance.from_dict(raw)
        raw = self.provenance.to_dict()
        raw["artifact_identity"] = self.artifact.artifact_identity.to_dict()
        with self.assertRaisesRegex(ContractValidationError, "unknown fields"):
            HtmlProvenance.from_dict(raw)

    def test_completed_artifact_binds_raw_bytes_not_json_or_its_own_digest(
        self,
    ) -> None:
        self.assertEqual(self.artifact.byte_size, len(self.content))
        self.assertEqual(
            self.artifact.artifact_identity.digest,
            hashlib.sha256(self.content).hexdigest(),
        )
        self.assertTrue(self.artifact.matches_bytes(self.content))
        self.assertFalse(
            self.artifact.matches_bytes(self.content.replace(b"Graph", b"graph"))
        )
        self.assertFalse(self.artifact.matches_bytes(self.content + b" "))
        changed = HtmlArtifact.from_bytes(
            artifact_path=self.artifact.artifact_path,
            content=self.content + b" ",
            provenance=self.provenance,
            embedding=self.artifact.embedding,
        )
        self.assertNotEqual(changed.artifact_identity, self.artifact.artifact_identity)
        self.assertEqual(changed.provenance, self.artifact.provenance)
        for content in (b"", "not bytes", bytearray(self.content)):
            with self.subTest(content=type(content).__name__):
                with self.assertRaises(ContractValidationError):
                    HtmlArtifact.from_bytes(
                        artifact_path=self.artifact.artifact_path,
                        content=content,
                        provenance=self.provenance,
                        embedding=self.artifact.embedding,
                    )

    def test_success_and_refusal_are_mutually_exclusive(self) -> None:
        for status, artifact, refusal in (
            ("rendered", None, None),
            ("cached", None, None),
            ("rendered", self.artifact, self.refusal),
            ("refused", self.artifact, self.refusal),
            ("refused", None, None),
            ("unknown", self.artifact, None),
        ):
            with self.subTest(
                status=status,
                artifact=artifact is not None,
                refusal=refusal is not None,
            ):
                with self.assertRaises(ContractValidationError):
                    HtmlRenderResult(status, artifact, refusal)
        with self.assertRaises(ContractValidationError):
            HtmlRenderRefusal("render.went_wrong", "not a registered refusal")

    def test_primitive_types_and_collection_limits_fail_closed(self) -> None:
        cases = (
            (self.request, "surface_id", True),
            (self.request, "view", []),
            (self.request, "cache_mode", "anything"),
            (self.request, "external_asset_policy", None),
            (self.provenance.view, "view_version", "one"),
            (self.provenance.view, "scope_kind", "workspace"),
            (self.provenance.source_bindings[0], "source_schema", "unregistered-shape"),
            (self.provenance.source_bindings[0], "source_identity", False),
            (self.provenance.renderer, "schema_catalog_release", 1),
            (self.provenance.renderer, "framework_distribution_identity", {}),
            (self.artifact, "byte_size", True),
            (self.artifact, "media_type", "text/plain"),
            (self.refusal, "message", ""),
            (self.refusal, "message", "x" * 2049),
        )
        for record, field, value in cases:
            with self.subTest(record=type(record).__name__, field=field):
                wire = record.to_dict()
                wire[field] = value
                with self.assertRaises(ContractValidationError):
                    type(record).from_dict(wire)
        for count in (0, 257):
            sources = tuple(
                replace(self.provenance.source_bindings[0], source_label=f"source-{i}")
                for i in range(count)
            )
            with self.subTest(source_count=count):
                with self.assertRaises(ContractValidationError):
                    render_inputs_identity(
                        sources, self.provenance.view, self.provenance.renderer
                    )
        assets = tuple(
            replace(self.provenance.external_assets[0], asset_id=f"asset-{i}")
            for i in range(33)
        )
        with self.assertRaises(ContractValidationError):
            replace(self.provenance, external_assets=assets)

    def test_embedding_counts_and_booleans_cannot_lie_about_the_declaration(
        self,
    ) -> None:
        for changes in (
            {"single_file": 1},
            {"single_file": False},
            {"companion_asset_count": True},
            {"companion_asset_count": 1},
            {"inline_script_count": False},
            {"inline_script_count": -1},
            {"inline_style_count": 1.0},
            {"external_reference_count": True},
            {"external_reference_count": 33},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ContractValidationError):
                    replace(self.artifact.embedding, **changes)
        for count in (0, 2):
            with self.assertRaisesRegex(
                ContractValidationError, "does not match declared assets"
            ):
                replace(
                    self.artifact,
                    embedding=replace(
                        self.artifact.embedding, external_reference_count=count
                    ),
                )

    def test_portable_paths_are_checked_on_every_host(self) -> None:
        for path in (
            "../escape.html",
            "/absolute.html",
            "C:/escape.html",
            "C:escape.html",
            "a//graph.html",
            "a/./graph.html",
            "a\\graph.html",
            "NUL.html",
            "a/graph.html:stream",
            "a/graph.json",
            "a/graph.HTML",
            "a/\0graph.html",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ContractValidationError):
                    replace(self.request, output_path=path)
                with self.assertRaises(ContractValidationError):
                    replace(self.artifact, artifact_path=path)

    def test_calendar_and_sri_checks_go_beyond_regex_shape(self) -> None:
        for stamp in (
            "2026-02-30T12:00:00Z",
            "2026-09-11T24:00:00Z",
            "2026-09-11T12:00:00+00:00",
        ):
            with self.subTest(stamp=stamp):
                with self.assertRaises(ContractValidationError):
                    provenance(generated_at=stamp)
        asset = self.provenance.external_assets[0]
        for value in (
            "md5-abc",
            "sha384-" + "A" * 43,
            "sha256-" + "A" * 64,
            "sha384-" + "!" * 64,
        ):
            with self.subTest(integrity=value):
                with self.assertRaises(ContractValidationError):
                    replace(asset, integrity=value)
        for url in (
            "http://cdn.example.test/a.js",
            "https://",
            "https://user:secret@cdn.example.test/a.js",
            "https://cdn.example.test:99999/a.js",
            "https://cdn.example.test/\0a.js",
            "https://cdn.example.test\\other/a.js",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ContractValidationError):
                    replace(asset, url=url)

    def test_immutable_records_own_their_parsed_input_and_serialized_output(
        self,
    ) -> None:
        wire = self.provenance.to_dict()
        parsed = HtmlProvenance.from_dict(wire)
        saved = copy.deepcopy(wire)
        wire["view"]["view_id"] = "mutated"
        wire["source_bindings"].clear()
        self.assertEqual(parsed.to_dict(), saved)
        rendered = parsed.to_dict()
        rendered["external_assets"].clear()
        self.assertEqual(parsed.to_dict(), saved)
        with self.assertRaises(FrozenInstanceError):
            parsed.generated_at = "2026-09-12T12:00:00Z"
        with self.assertRaises(ContractValidationError):
            replace(parsed, source_bindings=list(parsed.source_bindings))
        with self.assertRaises(ContractValidationError):
            render_inputs_identity(
                (parsed.source_bindings[0],) * 2, parsed.view, parsed.renderer
            )
        with self.assertRaises(ContractValidationError):
            replace(parsed, external_assets=parsed.external_assets * 2)


if __name__ == "__main__":
    unittest.main()
