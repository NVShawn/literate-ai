from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.models import (
    CodingCliInverseTranslator,
    CodingCliIsolation,
    CodingCliTaskResult,
)
from literate_ai.cli._wire import result_from_wire, result_to_wire
from literate_ai.source_to_specification import (
    INVERSE_EVIDENCE_CUSTODY_SCHEMA,
    LANGUAGE_SKILL_IDS,
    LEGACY_SOURCE_INTELLIGENCE_SCHEMA,
    MODEL_TRANSLATION_MODE,
    MODEL_TRANSLATION_OUTPUT_SCHEMA,
    SOURCE_TRANSLATION_RUN_SCHEMA,
    EvidenceReference,
    IntelligenceQueryRecord,
    ModelEvidence,
    SourceIntelligence,
    SourceToSpecificationError,
    builtin_skill_set,
    canonical_digest,
    canonical_value,
    derive_model_checkout,
    inventory_source,
    load_builtin_skill_catalog,
    source_intelligence_from_dict,
    validate_model_translation_record,
    validate_qualification_inverse_evidence,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

ROOT = Path(__file__).resolve().parents[2]


class ScriptedTaskRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.selection = SimpleNamespace(name="codex")

    def run_json_task(self, prompt: str, *, model: str | None = None):
        self.calls.append((prompt, model))
        payload = json.loads(prompt.splitlines()[-1])
        language = payload["language"]
        root_coordinate = payload["root_coordinate"]
        evidence_ids = [
            item["reference"]["evidence_id"] for item in payload["evidence"]
        ]
        evidence_paths = [item["reference"]["path"] for item in payload["evidence"]]
        response = {
            "schema": MODEL_TRANSLATION_OUTPUT_SCHEMA,
            "language": language,
            "observations": [
                {
                    "skill_id": "architecture",
                    "facet": "entrypoints",
                    "claim_kind": "observed-current-behavior",
                    "requirement": (
                        f"The application exposes its {language} entrypoint."
                    ),
                    "capability": f"{language} application entrypoint",
                    "scenario": {
                        "name": f"Run the {language} application",
                        "when": "the executable is invoked with a supported input",
                        "then": "it emits the documented result",
                    },
                    "evidence_ids": evidence_ids,
                    "confidence_basis_points": 9000,
                    "scope": "base",
                    "component_coordinate": root_coordinate,
                },
                {
                    "skill_id": LANGUAGE_SKILL_IDS[language],
                    "facet": "language-binding",
                    "claim_kind": "observed-current-behavior",
                    "requirement": f"The implementation uses the {language} toolchain.",
                    "capability": f"{language} implementation",
                    "scenario": {
                        "name": f"Build with {language}",
                        "when": "the language Flavor is selected",
                        "then": "the native toolchain produces the application",
                    },
                    "evidence_ids": evidence_ids,
                    "confidence_basis_points": 9500,
                    "scope": f"flavor:{language}",
                    "component_coordinate": root_coordinate,
                },
            ],
            "component_graph": {
                "root_coordinate": root_coordinate,
                "nodes": [
                    {
                        "coordinate": root_coordinate,
                        "title": root_coordinate.rsplit("/", 1)[-1]
                        .replace("-", " ")
                        .title(),
                        "kind": "cli",
                        "profiles": ["portable", "source-derived"],
                        "provided_capabilities": ["behavior.application"],
                        "capability_contracts": [
                            {
                                "name": "behavior.application",
                                "contract": "Runs the documented application behavior.",
                                "evidence_ids": evidence_ids,
                            }
                        ],
                        "entrypoints": [
                            {
                                "name": "run-"
                                + hashlib.sha256(
                                    evidence_paths[0].encode()
                                ).hexdigest()[:8],
                                "kind": "cli",
                                "path": evidence_paths[0],
                                "evidence_ids": evidence_ids,
                            }
                        ],
                        "build_needs": ["implementation.language-ecosystem"],
                        "source_paths": evidence_paths,
                        "observation_indexes": [0, 1],
                        "evidence_ids": evidence_ids,
                    }
                ],
                "edges": [],
            },
        }
        response_text = json.dumps(response, sort_keys=True, separators=(",", ":"))
        executable_identity = canonical_digest("codex")
        isolation = CodingCliIsolation(
            "test", True, "temporary-workspace", False, ("test double",)
        )
        command = ("/opt/tools/codex", "exec", "inverse")
        return CodingCliTaskResult(
            response=response,
            response_text=response_text,
            stdout="",
            stderr="",
            coding_cli="codex",
            executable="/opt/tools/codex",
            executable_identity=executable_identity,
            model=model,
            command=command,
            prompt=prompt,
            request_identity=canonical_digest(prompt),
            response_identity=canonical_digest(response),
            command_identity=canonical_digest(command),
            selection_identity=canonical_digest(
                {
                    "coding_cli": "codex",
                    "executable": "/opt/tools/codex",
                    "executable_identity": executable_identity,
                    "isolation": isolation.to_dict(),
                }
            ),
            tool_binding_identity=canonical_digest(
                {
                    "schema": "literate-ai/coding-cli-tool-binding@1",
                    "coding_cli": "codex",
                    "executable_identity": executable_identity,
                    "isolation": isolation.to_dict(),
                }
            ),
            isolation=isolation,
            environment_keys=("PATH",),
        )


class FixtureCollector:
    def collect(self, *, source, inventory, languages):
        evidence = []
        for language in languages:
            selected = tuple(
                item
                for item in inventory.entries
                if ("javascript" if item.language == "typescript" else item.language)
                in {language, "c" if language == "cpp" else language}
                or item.language is None
            )
            for index, entry in enumerate(selected):
                root = Path(source) if Path(source).is_dir() else Path(source).parent
                content = (root / entry.path).read_text(encoding="utf-8")
                reference = EvidenceReference(
                    evidence_id=f"evidence:{language}:{index}",
                    source_snapshot_id=inventory.identity,
                    content_digest=(
                        "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
                    ),
                    path=entry.path,
                    symbol="main",
                )
                evidence.append(
                    ModelEvidence(
                        reference,
                        language,
                        "source-file",
                        content,
                        1,
                        max(1, len(content.splitlines())),
                    )
                )
        indexed_source_files = tuple(
            sorted(
                (
                    item.reference.path,
                    len(item.content.encode("utf-8")),
                    item.reference.content_digest,
                )
                for item in evidence
            )
        )
        indexed_manifest = [
            {"path": path, "size": size, "identity": identity}
            for path, size, identity in indexed_source_files
        ]
        return SourceIntelligence(
            authority_source_snapshot_id=inventory.identity,
            indexed_source_snapshot_id=canonical_digest("indexed-snapshot"),
            indexed_source_tree_id=canonical_digest("indexed-tree"),
            indexed_source_files=indexed_source_files,
            source_content_identity=canonical_digest(indexed_manifest),
            source_material_identity=canonical_digest("indexed-material"),
            intelligence_evidence_identity=canonical_digest("indexed-evidence"),
            provider_id="fixture-intelligence",
            provider_version="structured-symbol-query-v1",
            runtime_version="1.1.6",
            executable_identity=canonical_digest("fixture-intelligence"),
            artifact_identity=canonical_digest("database"),
            artifact_media_type="application/vnd.sqlite3",
            capabilities=("call-graph", "declarations", "references"),
            provider_properties=(
                "built-with-version=1.1.6",
                "extraction-version=24",
            ),
            document_count=len(evidence),
            symbol_count=len(evidence) * 2,
            relationship_count=len(evidence),
            unresolved_relationship_count=0,
            warning_count=0,
            languages=languages,
            evidence=tuple(evidence),
            relationships=(),
            unresolved_relationships=(),
            queries=tuple(
                IntelligenceQueryRecord(
                    canonical_digest(
                        {
                            "language": item.language,
                            "evidence_id": item.reference.evidence_id,
                        }
                    ),
                    item.language,
                    "main",
                    (item.reference.evidence_id,),
                )
                for item in evidence
            ),
        )


class ModelSourceToSpecificationTests(unittest.TestCase):
    def _source_tree(self, root: Path) -> None:
        (root / "app.py").write_text(
            "async def serve():\n    return 'ok'\n\nclass Worker:\n    pass\n",
            encoding="utf-8",
        )
        (root / "main.cpp").write_text(
            "struct Counter {};\nint total(int value) { return value + 1; }\n",
            encoding="utf-8",
        )
        (root / "shared.h").write_text(
            "int total(int value);\n",
            encoding="utf-8",
        )
        (root / "main.rs").write_text(
            "pub trait Render {}\npub fn total(value: i32) -> i32 { value + 1 }\n",
            encoding="utf-8",
        )
        (root / "app.js").write_text(
            "export const total = (value) => value + 1;\n"
            "exports.render = () => 'ok';\n",
            encoding="utf-8",
        )

    def test_four_language_calls_emit_journaled_base_and_flavor_drafts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._source_tree(root)
            inventory = inventory_source(root)
            catalog = dict(load_builtin_skill_catalog())
            skill_set = builtin_skill_set(
                catalog, languages=("cpp", "javascript", "python", "rust")
            )
            task_runner = ScriptedTaskRunner()
            result = derive_model_checkout(
                source=root,
                inventory=inventory,
                origin_attestation_id=canonical_digest("origin"),
                skill_set=skill_set,
                skill_catalog=catalog,
                intelligence_collector=FixtureCollector(),
                translator=CodingCliInverseTranslator(
                    model="test-model", task_runner=task_runner
                ),
            )
            translation_record = {
                "schema": SOURCE_TRANSLATION_RUN_SCHEMA,
                "mode": MODEL_TRANSLATION_MODE,
                "intelligence": result.intelligence.to_dict(include_content=True),
                "journals": [item.to_dict() for item in result.journals],
            }
            result_wire = result_to_wire(result.result)
            self.assertEqual(
                result_wire["schema"],
                "urn:literate-ai:schema:v3:source-to-specification-result",
            )
            self.assertEqual(result_from_wire(result_wire), result.result)
            verified_flavors = validate_model_translation_record(
                value=translation_record,
                result=result.result,
                skill_catalog=catalog,
                surface_inventory=result.surface_inventory.to_dict(),
                evidence_partition_manifest=(
                    result.evidence_partition_manifest.to_dict()
                ),
                evidence_batch_plan=result.evidence_batch_plan.to_dict(),
                source_inventory=canonical_value(inventory),
            )
            custody = {
                "schema": INVERSE_EVIDENCE_CUSTODY_SCHEMA,
                "translation_identity": canonical_digest(translation_record),
                "source_inventory": canonical_value(inventory),
                "behavioral_surface_inventory": result.surface_inventory.to_dict(),
                "evidence_partition_manifest": (
                    result.evidence_partition_manifest.to_dict()
                ),
                "evidence_batch_plan": result.evidence_batch_plan.to_dict(),
            }
            schemas = SchemaCatalog(ROOT / "schemas" / "v2")
            schemas.validate(
                INVERSE_EVIDENCE_CUSTODY_SCHEMA,
                custody,
            )
            self.assertEqual(
                validate_qualification_inverse_evidence(
                    translation=translation_record,
                    custody=custody,
                ),
                canonical_digest(custody),
            )
            with self.assertRaisesRegex(
                SourceToSpecificationError, "requires retained inverse evidence"
            ):
                validate_qualification_inverse_evidence(
                    translation=translation_record,
                    custody=None,
                )
            drifted_custody = json.loads(json.dumps(custody))
            drifted_custody["source_inventory"]["entries"][0]["size"] += 1
            with self.assertRaisesRegex(
                SourceToSpecificationError, "describes another inventory"
            ):
                validate_qualification_inverse_evidence(
                    translation=translation_record,
                    custody=drifted_custody,
                )
            over_budget_custody = json.loads(json.dumps(custody))
            over_budget_custody["evidence_batch_plan"]["maximum_batch_bytes"] = 1
            with self.assertRaises(SourceToSpecificationError) as over_budget:
                validate_qualification_inverse_evidence(
                    translation=translation_record,
                    custody=over_budget_custody,
                )
            self.assertIn("budget", over_budget.exception.code)
            current = result.intelligence
            legacy_intelligence = {
                "schema": LEGACY_SOURCE_INTELLIGENCE_SCHEMA,
                "authority_source_snapshot_id": (current.authority_source_snapshot_id),
                "indexed_source_snapshot_id": current.indexed_source_snapshot_id,
                "indexed_source_tree_id": current.indexed_source_tree_id,
                "indexed_source_files": [
                    {"path": path, "size": size, "identity": identity}
                    for path, size, identity in current.indexed_source_files
                ],
                "source_content_identity": current.source_content_identity,
                "source_material_identity": current.source_material_identity,
                "index_evidence_identity": current.intelligence_evidence_identity,
                "provider_id": current.provider_id,
                "provider_version": current.provider_version,
                "runtime_version": current.runtime_version,
                "executable_identity": current.executable_identity,
                "database_identity": current.artifact_identity,
                "built_with_version": "1.1.6",
                "extraction_version": 24,
                "document_count": current.document_count,
                "node_count": current.symbol_count,
                "edge_count": current.relationship_count,
                "languages": list(current.languages),
                "evidence": [
                    item.to_dict(include_content=True) for item in current.evidence
                ],
                "queries": [item.to_dict() for item in current.queries],
            }
            legacy_identity = canonical_digest(legacy_intelligence)
            migrated = source_intelligence_from_dict(legacy_intelligence)
            self.assertEqual(migrated.identity, legacy_identity)
            self.assertEqual(migrated.artifact_identity, current.artifact_identity)
            self.assertEqual(migrated.relationships, ())

        self.assertEqual(len(task_runner.calls), 4)
        self.assertNotEqual(
            result.surface_inventory.collector_identity,
            result.surface_inventory.translator_identity,
        )
        self.assertTrue(result.surface_inventory.surfaces)
        self.assertFalse(result.surface_inventory.blocking_surface_ids)
        self.assertTrue(
            any(
                item.detector_identity == result.surface_inventory.translator_identity
                and item.requirement.value == "advisory"
                for item in result.surface_inventory.surfaces
            ),
            result.surface_inventory.to_dict(),
        )
        self.assertTrue(
            all(
                item.detector_identity
                in {
                    result.surface_inventory.collector_identity,
                    result.surface_inventory.translator_identity,
                }
                for item in result.surface_inventory.surfaces
            )
        )
        self.assertEqual(
            {journal.language for journal in result.journals},
            {"cpp", "javascript", "python", "rust"},
        )
        self.assertEqual(
            {draft["flavor_id"] for draft in result.flavor_drafts},
            {"cpp", "javascript", "python", "rust"},
        )
        self.assertEqual(result.result.request.output_provider, "literate-markdown")
        self.assertEqual(
            result.result.draft.validation.provider_version,
            "literate-markdown@1",
        )
        self.assertTrue(result.result.draft.validation.valid)
        self.assertEqual(len(result.result.component_graph_draft.nodes), 1)
        self.assertEqual(len(result.result.draft.statements), 4)
        artifact_paths = tuple(item.path for item in result.result.draft.artifacts)
        self.assertEqual(
            artifact_paths[:2],
            ("specs/derived/spec.md", "specs/derived/components/spec.md"),
        )
        self.assertRegex(
            artifact_paths[2], r"^specs/derived/components/local-tmp[^/]*/spec\.md$"
        )
        rendered = "\n".join(item.content for item in result.result.draft.artifacts)
        for statement in result.result.draft.statements:
            self.assertEqual(rendered.count(statement.requirement), 2)
            # One occurrence is human-readable frontmatter and one is the normative
            # body; the complete scenario stays in that same narrow document.
            containing = tuple(
                item.content
                for item in result.result.draft.artifacts
                if statement.requirement in item.content
            )
            self.assertEqual(len(containing), 1)
            for scenario in statement.scenarios:
                self.assertIn(scenario.when, containing[0])
                self.assertIn(scenario.then, containing[0])
        self.assertIn("does not resolve semantic conflicts", rendered)
        self.assertIn("not evidence that the recovered intent is complete", rendered)
        self.assertTrue(
            all(item.model_call_id is not None for item in result.result.observations)
        )
        self.assertEqual(verified_flavors, result.flavor_drafts)
        tampered = json.loads(json.dumps(translation_record))
        tampered["journals"][0]["stdout"] = "edited after review"
        with self.assertRaisesRegex(SourceToSpecificationError, "complete transcript"):
            validate_model_translation_record(
                value=tampered,
                result=result.result,
                skill_catalog=catalog,
                surface_inventory=result.surface_inventory.to_dict(),
                evidence_batch_plan=result.evidence_batch_plan.to_dict(),
            )
        for prompt, model in task_runner.calls:
            self.assertEqual(model, "test-model")
            self.assertIn("provider provenance", prompt)
            self.assertIn("potentially heuristic evidence", prompt)
            self.assertIn("Unresolved-reference", prompt)
            self.assertIn("corroborate it with exact supplied source evidence", prompt)
            self.assertIn("untrusted evidence", prompt)
            self.assertIn("exhaustive contract-coverage", prompt)
            self.assertIn("audit over the supplied evidence", prompt)
            self.assertIn("ordering and tie-breaking", prompt)
            self.assertIn("exact output-shape contract", prompt)
            self.assertIn("non-echo guarantees", prompt)
            self.assertIn("absence of relationship evidence", prompt)
            self.assertIn("Deterministic validation", prompt)
            self.assertIn("error behavior implemented by the source", prompt)
            self.assertIn("failure atomicity", prompt)
            self.assertIn("diagnostic channel", prompt)
            self.assertIn("observable count field", prompt)
            self.assertIn("before or after normalization", prompt)
            self.assertIn("authorized pair from this exact allowlist", prompt)
            self.assertIn("exact skill-to-scope allowlist", prompt)
            self.assertIn("complete union of every", prompt)
            self.assertIn("required_behavioral_surfaces", prompt)
            self.assertIn("compatible_facets", prompt)
            self.assertIn("For a `normalization` surface", prompt)
            self.assertIn("every observable output and downstream use", prompt)
            self.assertIn("For an `io-protocol` surface", prompt)
            self.assertIn("derivation and cardinality of every field", prompt)
            self.assertIn(
                "one dedicated observation for every required surface", prompt
            )
            self.assertIn("^[a-z0-9][a-z0-9._-]{0,126}$", prompt)
            self.assertIn("`behavior.calculate-manifest`", prompt)
            self.assertIn("never `calculateManifest`", prompt)
            self.assertIn("Do not normalize or rewrite", prompt)

        python_prompt = next(
            prompt
            for prompt, _model in task_runner.calls
            if '"language":"python"' in prompt
        )
        self.assertIn(
            "Python `str` values compared or sorted with the default operators",
            python_prompt,
        )
        self.assertIn("Unicode code point", python_prompt)
        self.assertIn("default exception hook to standard error", python_prompt)
        self.assertIn("diagnostic text with product data", python_prompt)
        self.assertIn(
            "Recover missing-required-field behavior from unconditional mapping access",
            python_prompt,
        )
        self.assertIn("`mapping[key]` raises `KeyError`", python_prompt)
        javascript_prompt = next(
            prompt
            for prompt, _model in task_runner.calls
            if '"language":"javascript"' in prompt
        )
        self.assertIn("UTF-16 code units", javascript_prompt)
        rust_prompt = next(
            prompt
            for prompt, _model in task_runner.calls
            if '"language":"rust"' in prompt
        )
        self.assertIn("lexicographic by UTF-8 bytes", rust_prompt)

        tampered_inventory = result.surface_inventory.to_dict()
        tampered_inventory["translator_identity"] = canonical_digest("substitute")
        with self.assertRaisesRegex(
            SourceToSpecificationError, "differs from exact evidence and journals"
        ):
            validate_model_translation_record(
                value=translation_record,
                result=result.result,
                skill_catalog=catalog,
                surface_inventory=tampered_inventory,
                evidence_batch_plan=result.evidence_batch_plan.to_dict(),
            )
        tampered_partition = result.evidence_partition_manifest.to_dict()
        tampered_partition["collector_identity"] = canonical_digest("substitute")
        with self.assertRaisesRegex(
            SourceToSpecificationError, "differs from exact inventory and evidence"
        ):
            validate_model_translation_record(
                value=translation_record,
                result=result.result,
                skill_catalog=catalog,
                evidence_partition_manifest=tampered_partition,
                evidence_batch_plan=result.evidence_batch_plan.to_dict(),
                source_inventory=canonical_value(inventory),
            )
        tampered_batches = result.evidence_batch_plan.to_dict()
        tampered_batches["maximum_batch_bytes"] += 1
        with self.assertRaisesRegex(SourceToSpecificationError, "signed draft request"):
            validate_model_translation_record(
                value=translation_record,
                result=result.result,
                skill_catalog=catalog,
                evidence_batch_plan=tampered_batches,
            )

    def test_skill_identity_drift_fails_before_model_egress(self):
        runner = ScriptedTaskRunner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
            inventory = inventory_source(root)
            catalog = dict(load_builtin_skill_catalog())
            skill_set = builtin_skill_set(catalog, languages=("python",))
            catalog["architecture"] = replace(
                catalog["architecture"],
                content_digest=canonical_digest("changed-architecture-skill"),
            )
            with self.assertRaisesRegex(SourceToSpecificationError, "pinned reference"):
                derive_model_checkout(
                    source=root,
                    inventory=inventory,
                    origin_attestation_id=canonical_digest("origin"),
                    skill_set=skill_set,
                    skill_catalog=catalog,
                    intelligence_collector=FixtureCollector(),
                    translator=CodingCliInverseTranslator(task_runner=runner),
                )
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
