"""Published Draft 2020-12 wire schemas for HTML5 visual observability artifacts."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from tests.support.fixtures_test_schema_catalog import SchemaCatalog

ROOT = Path(__file__).resolve().parents[2]
V1_ROOT = ROOT / "schemas" / "v1"
V2_ROOT = ROOT / "schemas" / "v2"
SCHEMA_FILE = V2_ROOT / "html-observability.schema.json"
SCHEMA_ROOT = "urn:literate-ai:schema:v1:html-observability-contracts"
ARTIFACT = "urn:literate-ai:schema:v1:html-observability-artifact"
STALENESS = "urn:literate-ai:schema:v1:html-observability-staleness-report"
SURFACE = "urn:literate-ai:schema:v1:html-observability-surface"
REQUEST = "urn:literate-ai:schema:v1:html-observability-render-request"
RESULT = "urn:literate-ai:schema:v1:html-observability-render-result"
REFUSAL = "urn:literate-ai:schema:v1:html-observability-render-refusal"


def identity(nibble: str) -> dict[str, str]:
    return {
        "schema": "urn:literate-ai:schema:v1:content-identity",
        "algorithm": "sha256",
        "digest": nibble * 64,
    }


def artifact_fixture() -> dict[str, object]:
    return {
        "schema": ARTIFACT,
        "artifact_path": "generated/observability/graph.html",
        "media_type": "text/html",
        "artifact_identity": identity("a"),
        "byte_size": 48213,
        "provenance": {
            "schema": "urn:literate-ai:schema:v1:html-observability-provenance",
            "view": {
                "schema": "urn:literate-ai:schema:v1:html-observability-view",
                "view_id": "authority-graph",
                "view_version": "1.0.0",
                "scope_kind": "project",
                "scope_identifier": "literate-ai",
            },
            "source_bindings": [
                {
                    "schema": (
                        "urn:literate-ai:schema:v1:html-observability-source-binding"
                    ),
                    "source_label": "authority-graph",
                    "source_schema": (
                        "urn:literate-ai:schema:v2:component-authority-projection"
                    ),
                    "source_identity": identity("b"),
                }
            ],
            "renderer": {
                "schema": (
                    "urn:literate-ai:schema:v1:html-observability-renderer-binding"
                ),
                "renderer_id": "render-html-observability",
                "renderer_version": "1.0.0",
                "framework_distribution_identity": identity("c"),
                "schema_catalog_release": "1.0.0",
                "template_identity": identity("d"),
            },
            "external_assets": [
                {
                    "schema": (
                        "urn:literate-ai:schema:v1:html-observability-external-asset"
                    ),
                    "asset_id": "d3",
                    "url": "https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js",
                    "integrity": "sha384-" + "A" * 64,
                    "crossorigin": "anonymous",
                    "asset_kind": "script",
                }
            ],
            "generated_at": "2026-09-08T18:00:00Z",
            "render_inputs_identity": identity("e"),
            "provenance_identity": identity("f"),
        },
        "embedding": {
            "single_file": True,
            "companion_asset_count": 0,
            "inline_script_count": 2,
            "inline_style_count": 1,
            "external_reference_count": 1,
        },
    }


def surface_fixture() -> dict[str, object]:
    return {
        "schema": SURFACE,
        "surface_id": "authority-graph",
        "source_schema": "urn:literate-ai:schema:v2:component-authority-projection",
        "view_ids": ["authority-graph"],
    }


def request_fixture() -> dict[str, object]:
    return {
        "schema": REQUEST,
        "surface_id": "authority-graph",
        "view": artifact_fixture()["provenance"]["view"],
        "output_path": "generated/observability/graph.html",
        "cache_mode": "read-write",
        "external_asset_policy": "pinned-cdn",
    }


def staleness_fixture() -> dict[str, object]:
    return {
        "schema": STALENESS,
        "artifact_path": "generated/observability/graph.html",
        "status": "current",
        "expected_render_inputs_identity": identity("a"),
        "observed_render_inputs_identity": identity("a"),
        "stale_source_labels": [],
    }


class HtmlObservabilitySchemaTests(unittest.TestCase):
    """The Phase 0 artifact contract for GitHub #295 / #296."""

    def setUp(self) -> None:
        self.schemas = SchemaCatalog()
        resources: list[tuple[str, dict]] = []

        def collect(node: object) -> None:
            if isinstance(node, dict):
                identifier = node.get("$id")
                if isinstance(identifier, str):
                    resources.append((identifier, node))
                for child in node.values():
                    collect(child)
            elif isinstance(node, list):
                for child in node:
                    collect(child)

        for root in (V1_ROOT, V2_ROOT):
            for path in sorted(root.glob("*.schema.json")):
                collect(json.loads(path.read_text(encoding="utf-8")))
        self.registry = Registry().with_resources(
            (
                identifier,
                Resource.from_contents(node, default_specification=DRAFT202012),
            )
            for identifier, node in resources
        )

    def validator(self, resource_id: str) -> Draft202012Validator:
        return Draft202012Validator({"$ref": resource_id}, registry=self.registry)

    def assert_rejected(self, resource_id: str, document: object, reason: str) -> None:
        errors = sorted(self.validator(resource_id).iter_errors(document), key=str)
        self.assertTrue(errors, f"{reason} was accepted")

    def test_index_registration_is_exact(self) -> None:
        catalog = json.loads((V2_ROOT / "index.json").read_text(encoding="utf-8"))
        entry = next(
            item for item in catalog["schemas"] if item["file"] == SCHEMA_FILE.name
        )
        self.assertEqual(entry["root_id"], SCHEMA_ROOT)
        document = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        declared = {
            node["$id"]
            for node in document["$defs"].values()
            if isinstance(node, dict) and "$id" in node
        }
        self.assertEqual(declared, set(entry["public_ids"]))

    def test_conforming_artifact_validates_and_is_closed(self) -> None:
        document = artifact_fixture()
        self.assertEqual(list(self.validator(ARTIFACT).iter_errors(document)), [])
        self.schemas.validate(ARTIFACT, document)
        with self.assertRaisesRegex(AssertionError, "unknown field"):
            self.schemas.validate(ARTIFACT, {**document, "implicit": True})

    def test_provenance_binds_rendered_sources_to_content_identities(self) -> None:
        """Every artifact names the exact JSON it derives from; JSON stays canonical."""
        document = artifact_fixture()
        binding = document["provenance"]["source_bindings"][0]
        self.assertEqual(
            binding["source_identity"]["schema"],
            "urn:literate-ai:schema:v1:content-identity",
        )
        for field in ("source_label", "source_schema", "source_identity"):
            missing = copy.deepcopy(document)
            del missing["provenance"]["source_bindings"][0][field]
            self.assert_rejected(ARTIFACT, missing, f"source binding without {field}")

        unsourced = copy.deepcopy(document)
        unsourced["provenance"]["source_bindings"] = []
        self.assert_rejected(ARTIFACT, unsourced, "artifact with no rendered source")

    def test_renderer_binding_pins_the_framework_distribution(self) -> None:
        """Mixed-version panes must be detectable rather than silently inconsistent."""
        for field in (
            "framework_distribution_identity",
            "renderer_version",
            "schema_catalog_release",
            "template_identity",
        ):
            document = artifact_fixture()
            del document["provenance"]["renderer"][field]
            self.assert_rejected(ARTIFACT, document, f"renderer without {field}")

    def test_single_file_constraint_is_fail_closed(self) -> None:
        """No companion asset tree, so a pane loads one file with no bundler step."""
        for field, value in (
            ("companion_asset_count", 1),
            ("single_file", False),
        ):
            document = artifact_fixture()
            document["embedding"][field] = value
            self.assert_rejected(ARTIFACT, document, f"embedding with {field}={value}")

    def test_external_libraries_are_pinned_by_subresource_integrity(self) -> None:
        document = artifact_fixture()
        without_integrity = copy.deepcopy(document)
        del without_integrity["provenance"]["external_assets"][0]["integrity"]
        self.assert_rejected(ARTIFACT, without_integrity, "CDN asset without SRI")

        weak_integrity = copy.deepcopy(document)
        weak_integrity["provenance"]["external_assets"][0]["integrity"] = "md5-abc"
        self.assert_rejected(ARTIFACT, weak_integrity, "non-SHA subresource integrity")

        insecure = copy.deepcopy(document)
        insecure["provenance"]["external_assets"][0]["url"] = "http://cdn.test/d3.js"
        self.assert_rejected(ARTIFACT, insecure, "plain-http library reference")

    def test_artifact_paths_stay_relative_and_html(self) -> None:
        for path in (
            "../../etc/passwd.html",
            "/absolute/graph.html",
            "generated/graph.json",
            "generated\\observability\\graph.html",
        ):
            document = artifact_fixture()
            document["artifact_path"] = path
            self.assert_rejected(ARTIFACT, document, f"artifact path {path!r}")

    def test_generation_timestamp_is_utc(self) -> None:
        for stamp in (
            "2026-09-08T18:00:00-07:00",
            "2026-09-08 18:00:00Z",
            "2026-09-08",
        ):
            document = artifact_fixture()
            document["provenance"]["generated_at"] = stamp
            self.assert_rejected(ARTIFACT, document, f"timestamp {stamp!r}")

    def test_surface_registration_declares_its_json_schema_and_views(self) -> None:
        """Every HTML5-emitting surface registers here instead of rolling a renderer."""
        surface = surface_fixture()
        self.assertEqual(list(self.validator(SURFACE).iter_errors(surface)), [])
        self.schemas.validate(SURFACE, surface)

        viewless = copy.deepcopy(surface)
        viewless["view_ids"] = []
        self.assert_rejected(SURFACE, viewless, "surface supporting no view")

        duplicated = copy.deepcopy(surface)
        duplicated["view_ids"] = ["authority-graph", "authority-graph"]
        self.assert_rejected(SURFACE, duplicated, "surface repeating a view")

        untyped = copy.deepcopy(surface)
        untyped["source_schema"] = "not-a-schema-urn"
        self.assert_rejected(SURFACE, untyped, "surface without a schema URN")

    def test_render_request_names_a_surface_a_view_and_one_output(self) -> None:
        request = request_fixture()
        self.assertEqual(list(self.validator(REQUEST).iter_errors(request)), [])
        self.schemas.validate(REQUEST, request)
        with self.assertRaisesRegex(AssertionError, "unknown field"):
            self.schemas.validate(REQUEST, {**request, "model": "opus"})

        for field in ("surface_id", "view", "output_path", "cache_mode"):
            incomplete = copy.deepcopy(request)
            del incomplete[field]
            self.assert_rejected(REQUEST, incomplete, f"request without {field}")

        escaping = copy.deepcopy(request)
        escaping["output_path"] = "../outside/graph.html"
        self.assert_rejected(REQUEST, escaping, "request writing outside the project")

        unpinned = copy.deepcopy(request)
        unpinned["external_asset_policy"] = "any"
        self.assert_rejected(REQUEST, unpinned, "unbounded external asset policy")

    def test_render_result_carries_an_artifact_or_a_typed_refusal(self) -> None:
        """Rendering fails closed instead of emitting a partial or unpinned artifact."""
        rendered = {
            "schema": RESULT,
            "status": "rendered",
            "artifact": artifact_fixture(),
            "refusal": None,
        }
        self.assertEqual(list(self.validator(RESULT).iter_errors(rendered)), [])

        refused = {
            "schema": RESULT,
            "status": "refused",
            "artifact": None,
            "refusal": {
                "schema": REFUSAL,
                "code": "render.external_asset_unpinned",
                "message": "d3@7 was requested without a subresource integrity hash",
            },
        }
        self.assertEqual(list(self.validator(RESULT).iter_errors(refused)), [])

        empty = copy.deepcopy(rendered)
        empty["artifact"] = None
        self.assert_rejected(RESULT, empty, "successful render with no artifact")

        both = copy.deepcopy(rendered)
        both["refusal"] = refused["refusal"]
        self.assert_rejected(RESULT, both, "render both succeeding and refusing")

        silent = copy.deepcopy(refused)
        silent["refusal"] = None
        self.assert_rejected(RESULT, silent, "refusal without a typed cause")

        untyped = copy.deepcopy(refused)
        untyped["refusal"] = {**refused["refusal"], "code": "render.went_wrong"}
        self.assert_rejected(RESULT, untyped, "refusal outside the typed code set")

    def test_staleness_gate_reports_are_internally_consistent(self) -> None:
        report = staleness_fixture()
        self.assertEqual(list(self.validator(STALENESS).iter_errors(report)), [])

        current_with_stale = copy.deepcopy(report)
        current_with_stale["stale_source_labels"] = ["component-locks"]
        self.assert_rejected(
            STALENESS, current_with_stale, "current artifact naming stale sources"
        )

        stale = copy.deepcopy(report)
        stale["status"] = "stale"
        self.assert_rejected(STALENESS, stale, "stale artifact naming no stale source")
        stale["stale_source_labels"] = ["component-locks"]
        self.assertEqual(list(self.validator(STALENESS).iter_errors(stale)), [])

        missing = copy.deepcopy(report)
        missing["status"] = "missing"
        self.assert_rejected(STALENESS, missing, "missing artifact with an observation")
        missing["observed_render_inputs_identity"] = None
        self.assertEqual(list(self.validator(STALENESS).iter_errors(missing)), [])

        unknown = copy.deepcopy(report)
        unknown["status"] = "probably-fine"
        self.assert_rejected(STALENESS, unknown, "unknown staleness status")

    def test_provenance_cannot_contain_the_completed_file_digest(self) -> None:
        document = artifact_fixture()
        document["provenance"]["artifact_identity"] = document["artifact_identity"]
        self.assert_rejected(ARTIFACT, document, "self-referential file digest")

    def test_phase_two_surface_declarations_are_expressible(self) -> None:
        # These are contract-shape probes, not production surface registrations
        # or claims that any existing CLI output implements a selected schema.
        candidates = (
            ("verify-gate", STALENESS),
            ("component-locks", "urn:literate-ai:schema:v2:component-lock"),
            ("perf-timeline", "urn:literate-ai:schema:v1:workflow-stage-result"),
            ("workflow-routing", "urn:literate-ai:schema:v2:model-route-decision"),
            ("build-history", "urn:literate-ai:schema:v2:component-build-manifest"),
        )
        for surface_id, source_schema in candidates:
            with self.subTest(surface=surface_id):
                # Unlike an invented URN, each candidate source has a real
                # catalog resource; adapting actual producers is later work.
                self.registry.contents(source_schema)
                surface = surface_fixture()
                surface.update(
                    surface_id=surface_id,
                    source_schema=source_schema,
                    view_ids=[surface_id],
                )
                request = request_fixture()
                request["surface_id"] = surface_id
                request["view"]["view_id"] = surface_id
                artifact = artifact_fixture()
                artifact["provenance"]["view"] = request["view"]
                artifact["provenance"]["source_bindings"] = [
                    {
                        "schema": (
                            "urn:literate-ai:schema:v1:html-observability-source-binding"
                        ),
                        "source_label": surface_id,
                        "source_schema": source_schema,
                        "source_identity": identity("b"),
                    }
                ]
                for resource_id, document in (
                    (SURFACE, surface),
                    (REQUEST, request),
                    (ARTIFACT, artifact),
                ):
                    self.validator(resource_id).validate(document)

    def test_adversarial_checks_detect_weakened_contract_rules(self) -> None:
        # Mutate only in-memory resources. The acceptance tests themselves must
        # fail, not merely a second validator applied to hand-picked bad data.
        mutations = (
            (
                "artifact",
                ("properties", "embedding", "properties", "companion_asset_count"),
                "const",
                "test_single_file_constraint_is_fail_closed",
            ),
            (
                "artifact",
                ("properties", "embedding", "properties", "single_file"),
                "const",
                "test_single_file_constraint_is_fail_closed",
            ),
            (
                "externalAsset",
                (),
                "required",
                "test_external_libraries_are_pinned_by_subresource_integrity",
            ),
            (
                "provenance",
                ("properties", "source_bindings"),
                "minItems",
                "test_provenance_binds_rendered_sources_to_content_identities",
            ),
            (
                "provenance",
                (),
                "additionalProperties",
                "test_provenance_cannot_contain_the_completed_file_digest",
            ),
            (
                "renderResult",
                (),
                "allOf",
                "test_render_result_carries_an_artifact_or_a_typed_refusal",
            ),
            (
                "stalenessReport",
                (),
                "allOf",
                "test_staleness_gate_reports_are_internally_consistent",
            ),
        )
        original = SCHEMA_FILE.read_bytes()
        for definition, path, rule, test_name in mutations:
            with self.subTest(definition=definition, path=path, removed=rule):
                mutant = json.loads(original)
                target = mutant["$defs"][definition]
                for key in path:
                    target = target[key]
                del target[rule]
                # Replace both the root and public resources so absolute refs
                # cannot accidentally resolve back to an unmutated definition.
                resources = [mutant] + [
                    value for value in mutant["$defs"].values() if "$id" in value
                ]
                registry = self.registry.with_resources(
                    (
                        value["$id"],
                        Resource.from_contents(
                            value, default_specification=DRAFT202012
                        ),
                    )
                    for value in resources
                )
                subject = HtmlObservabilitySchemaTests(test_name)
                subject.schemas = self.schemas
                subject.registry = registry
                with self.assertRaises(AssertionError):
                    getattr(subject, test_name)()
        self.assertEqual(SCHEMA_FILE.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
