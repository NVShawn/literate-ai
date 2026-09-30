from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import literate_ai.source_to_specification.model_workflow as model_workflow_module
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
    BehavioralInterfaceKind,
    BehavioralSurfaceInventoryItem,
    BehavioralSurfaceRequirement,
    CoverageState,
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
    parse_model_component_graph,
    parse_model_observations,
    source_intelligence_from_dict,
    validate_model_translation_record,
    validate_qualification_inverse_evidence,
)
from tests.unit.test_schema_catalog import SchemaCatalog

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


class LiteralDataObservationTaskRunner(ScriptedTaskRunner):
    """Adds the data-contracts observation a translator owes a literal table.

    The literal *values* are recovered deterministically from evidence content
    (see `.literal_data`), independent of this scripted response. This models the
    part of the fix that remains the translator's job: acknowledging the literal
    surface exists at all, rather than silently omitting it, so `spec derive`'s
    required-surface completeness gate accepts the draft.
    """

    def run_json_task(self, prompt: str, *, model: str | None = None):
        result = super().run_json_task(prompt, model=model)
        response = json.loads(json.dumps(result.response))
        root_coordinate = response["component_graph"]["root_coordinate"]
        evidence_ids = response["component_graph"]["nodes"][0]["evidence_ids"]
        response["observations"].append(
            {
                "skill_id": "api-surface",
                "facet": "data-contracts",
                "claim_kind": "observed-current-behavior",
                "requirement": (
                    "The lookup table's literal values are pinned as an asset; "
                    "callers look up a family key to resolve architecture and "
                    "model."
                ),
                "capability": "hardware lookup table",
                "scenario": {
                    "name": "Resolve hardware for a known family",
                    "when": "a supported family key is looked up",
                    "then": "the pinned asset's entry for that key is returned",
                },
                "evidence_ids": evidence_ids,
                "confidence_basis_points": 8800,
                "scope": "base",
                "component_coordinate": root_coordinate,
            }
        )
        response["component_graph"]["nodes"][0]["observation_indexes"] = [0, 1, 2]
        response_text = json.dumps(response, sort_keys=True, separators=(",", ":"))
        return replace(
            result,
            response=response,
            response_text=response_text,
            response_identity=canonical_digest(response),
        )


class LayeredGraphTaskRunner(ScriptedTaskRunner):
    def run_json_task(self, prompt: str, *, model: str | None = None):
        result = super().run_json_task(prompt, model=model)
        response = json.loads(json.dumps(result.response))
        root_coordinate = response["component_graph"]["root_coordinate"]
        child_coordinate = f"{root_coordinate}/worker"
        evidence_id = response["component_graph"]["nodes"][0]["evidence_ids"][0]
        evidence_path = response["component_graph"]["nodes"][0]["source_paths"][0]
        response["observations"].append(
            {
                "skill_id": "behavior-state",
                "facet": "state",
                "claim_kind": "observed-current-behavior",
                "requirement": "The worker preserves submitted jobs until completion.",
                "capability": "durable job processing",
                "scenario": {
                    "name": "Complete a submitted job",
                    "when": "a valid job is submitted",
                    "then": "the worker records its completed result",
                },
                "evidence_ids": [evidence_id],
                "confidence_basis_points": 8800,
                "scope": "base",
                "component_coordinate": child_coordinate,
            }
        )
        response["component_graph"] = {
            "root_coordinate": root_coordinate,
            "nodes": [
                {
                    **response["component_graph"]["nodes"][0],
                    "observation_indexes": [0, 1],
                },
                {
                    "coordinate": child_coordinate,
                    "title": "Worker",
                    "kind": "service",
                    "profiles": ["portable", "worker"],
                    "provided_capabilities": ["jobs.durable-processing"],
                    "capability_contracts": [
                        {
                            "name": "jobs.durable-processing",
                            "contract": "Processes submitted jobs durably.",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "entrypoints": [
                        {
                            "name": "serve",
                            "kind": "service",
                            "path": evidence_path,
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "build_needs": ["implementation.language-ecosystem"],
                    "source_paths": [evidence_path],
                    "observation_indexes": [2],
                    "evidence_ids": [evidence_id],
                },
            ],
            "edges": [
                {
                    "source_coordinate": root_coordinate,
                    "target_coordinate": child_coordinate,
                    "requirement_id": "worker-runtime",
                    "capability": "jobs.durable-processing",
                    "version_range": ">=1.0.0 <2.0.0",
                    "dependency_kind": "runtime",
                    "optional": False,
                    "evidence_ids": [evidence_id],
                }
            ],
        }
        response_text = json.dumps(response, sort_keys=True, separators=(",", ":"))
        return replace(
            result,
            response=response,
            response_text=response_text,
            response_identity=canonical_digest(response),
        )


class InsufficientEvidenceThenObservedTaskRunner(ScriptedTaskRunner):
    """Emit an extra base observation that starts weak and later strengthens.

    The first call for a language classifies an extra, otherwise-observable base
    behavioral claim as `inferred-intent` (the retry-worthy shortfall issue #117
    describes). Every subsequent call for that same language reclassifies it as
    `observed-current-behavior`, simulating the exact "identical re-run recovers"
    shape from the issue.
    """

    def __init__(self) -> None:
        super().__init__()
        self.language_call_counts: dict[str, int] = {}

    def run_json_task(self, prompt: str, *, model: str | None = None):
        result = super().run_json_task(prompt, model=model)
        response = json.loads(json.dumps(result.response))
        language = response["language"]
        count = self.language_call_counts.get(language, 0) + 1
        self.language_call_counts[language] = count
        root_coordinate = response["component_graph"]["root_coordinate"]
        evidence_ids = response["observations"][0]["evidence_ids"]
        claim_kind = "inferred-intent" if count == 1 else "observed-current-behavior"
        response["observations"].append(
            {
                "skill_id": "architecture",
                "facet": "entrypoints",
                "claim_kind": claim_kind,
                "requirement": f"The {language} retried surface behaves as expected.",
                "capability": f"{language} retried behavioral surface",
                "scenario": {
                    "name": "Exercise the retried surface",
                    "when": "the retried behavior is exercised",
                    "then": "it behaves as documented",
                },
                "evidence_ids": evidence_ids,
                "confidence_basis_points": (
                    6000 if claim_kind == "inferred-intent" else 9000
                ),
                "scope": "base",
                "component_coordinate": root_coordinate,
            }
        )
        response["component_graph"]["nodes"][0]["observation_indexes"] = [0, 1, 2]
        response_text = json.dumps(response, sort_keys=True, separators=(",", ":"))
        return replace(
            result,
            response=response,
            response_text=response_text,
            response_identity=canonical_digest(response),
        )


class PersistentlyInsufficientEvidenceTaskRunner(ScriptedTaskRunner):
    """Classify an extra observable base claim as `inferred-intent` every call.

    Used to prove the retry is bounded: it never recovers, so the caller must
    observe the exhausted-retry count and a still-unsupported surface rather than
    retrying forever.
    """

    def __init__(self) -> None:
        super().__init__()
        self.language_call_counts: dict[str, int] = {}

    def run_json_task(self, prompt: str, *, model: str | None = None):
        result = super().run_json_task(prompt, model=model)
        response = json.loads(json.dumps(result.response))
        language = response["language"]
        self.language_call_counts[language] = (
            self.language_call_counts.get(language, 0) + 1
        )
        root_coordinate = response["component_graph"]["root_coordinate"]
        evidence_ids = response["observations"][0]["evidence_ids"]
        response["observations"].append(
            {
                "skill_id": "architecture",
                "facet": "entrypoints",
                "claim_kind": "inferred-intent",
                "requirement": f"The {language} weak surface behaves as expected.",
                "capability": f"{language} persistently weak behavioral surface",
                "scenario": {
                    "name": "Exercise the persistently weak surface",
                    "when": "the persistently weak behavior is exercised",
                    "then": "it behaves as documented",
                },
                "evidence_ids": evidence_ids,
                "confidence_basis_points": 6000,
                "scope": "base",
                "component_coordinate": root_coordinate,
            }
        )
        response["component_graph"]["nodes"][0]["observation_indexes"] = [0, 1, 2]
        response_text = json.dumps(response, sort_keys=True, separators=(",", ":"))
        return replace(
            result,
            response=response,
            response_text=response_text,
            response_identity=canonical_digest(response),
        )


class ReproducibleSuspectedDefectTaskRunner(ScriptedTaskRunner):
    """Classify an extra observable base claim as a genuine `suspected-defect`.

    This is a legitimate, reproducible epistemic finding -- not a model-call
    shortfall -- so it must never be retried away, unlike `inferred-intent`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.language_call_counts: dict[str, int] = {}

    def run_json_task(self, prompt: str, *, model: str | None = None):
        result = super().run_json_task(prompt, model=model)
        response = json.loads(json.dumps(result.response))
        language = response["language"]
        self.language_call_counts[language] = (
            self.language_call_counts.get(language, 0) + 1
        )
        root_coordinate = response["component_graph"]["root_coordinate"]
        evidence_ids = response["observations"][0]["evidence_ids"]
        response["observations"].append(
            {
                "skill_id": "architecture",
                "facet": "entrypoints",
                "claim_kind": "suspected-defect",
                "requirement": f"The {language} surface may defect under load.",
                "capability": f"{language} suspected-defect behavioral surface",
                "scenario": {
                    "name": "Exercise the suspected-defect surface",
                    "when": "the suspected-defect behavior is exercised",
                    "then": "it may not behave as documented",
                },
                "evidence_ids": evidence_ids,
                "confidence_basis_points": 6000,
                "scope": "base",
                "component_coordinate": root_coordinate,
            }
        )
        response["component_graph"]["nodes"][0]["observation_indexes"] = [0, 1, 2]
        response_text = json.dumps(response, sort_keys=True, separators=(",", ":"))
        return replace(
            result,
            response=response,
            response_text=response_text,
            response_identity=canonical_digest(response),
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

    def test_static_translation_is_explicitly_exempt_from_inverse_model_custody(self):
        translation = {
            "schema": SOURCE_TRANSLATION_RUN_SCHEMA,
            "mode": "deterministic-static",
            "intelligence": None,
            "journals": [],
        }
        self.assertIsNone(
            validate_qualification_inverse_evidence(
                translation=translation,
                custody=None,
            )
        )
        with self.assertRaisesRegex(
            SourceToSpecificationError, "no model or inverse custody evidence"
        ):
            validate_qualification_inverse_evidence(
                translation=translation,
                custody={"schema": INVERSE_EVIDENCE_CUSTODY_SCHEMA},
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

    def test_inverse_skill_model_override_is_lexical_and_restores_for_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._source_tree(root)
            (root / "app.js").unlink()
            (root / "main.rs").unlink()
            catalog = dict(load_builtin_skill_catalog())
            catalog["language-python"] = replace(
                catalog["language-python"],
                models={"codex": "python-skill-model"},
            )
            selected = builtin_skill_set(
                catalog,
                languages=("cpp", "python"),
            )
            runner = ScriptedTaskRunner()
            derive_model_checkout(
                source=root,
                inventory=inventory_source(root),
                origin_attestation_id=canonical_digest("origin"),
                skill_set=selected,
                skill_catalog=catalog,
                intelligence_collector=FixtureCollector(),
                translator=CodingCliInverseTranslator(
                    model="pipeline-model",
                    task_runner=runner,
                ),
            )

        calls = {
            json.loads(prompt.splitlines()[-1])["language"]: (prompt, model)
            for prompt, model in runner.calls
        }
        self.assertEqual(calls["python"][1], "python-skill-model")
        self.assertEqual(calls["cpp"][1], "pipeline-model")
        python_scope = json.loads(calls["python"][0].splitlines()[-1])["model_scope"]
        cpp_scope = json.loads(calls["cpp"][0].splitlines()[-1])["model_scope"]
        self.assertEqual(python_scope["model_selector"], "python-skill-model")
        self.assertEqual(cpp_scope["model_selector"], "pipeline-model")
        self.assertEqual(
            [step["decision"] for step in python_scope["resolution_trace"]],
            ["override", "override"],
        )
        self.assertEqual(
            [step["decision"] for step in cpp_scope["resolution_trace"]],
            ["override", "inherit"],
        )

    def test_inverse_selected_skills_with_conflicting_models_fail_before_egress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._source_tree(root)
            (root / "app.js").unlink()
            (root / "main.cpp").unlink()
            (root / "main.rs").unlink()
            (root / "shared.h").unlink()
            catalog = dict(load_builtin_skill_catalog())
            catalog["architecture"] = replace(
                catalog["architecture"],
                models={"codex": "architecture-model"},
            )
            catalog["language-python"] = replace(
                catalog["language-python"],
                models={"codex": "python-model"},
            )
            selected = builtin_skill_set(catalog, languages=("python",))
            runner = ScriptedTaskRunner()
            with self.assertRaises(SourceToSpecificationError) as raised:
                derive_model_checkout(
                    source=root,
                    inventory=inventory_source(root),
                    origin_attestation_id=canonical_digest("origin"),
                    skill_set=selected,
                    skill_catalog=catalog,
                    intelligence_collector=FixtureCollector(),
                    translator=CodingCliInverseTranslator(
                        model="pipeline-model",
                        task_runner=runner,
                    ),
                )

        self.assertEqual(raised.exception.code, "model_scope.ambiguous")
        self.assertEqual(runner.calls, [])

    def test_single_behavior_retains_simple_openspec_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("def main():\n    return 'ok'\n")
            inventory = inventory_source(root)
            catalog = dict(load_builtin_skill_catalog())
            result = derive_model_checkout(
                source=root,
                inventory=inventory,
                origin_attestation_id=canonical_digest("origin"),
                skill_set=builtin_skill_set(catalog, languages=("python",)),
                skill_catalog=catalog,
                intelligence_collector=FixtureCollector(),
                translator=CodingCliInverseTranslator(
                    model="test-model", task_runner=ScriptedTaskRunner()
                ),
            )

        self.assertEqual(result.result.request.output_provider, "openspec")
        paths = tuple(item.path for item in result.result.draft.artifacts)
        self.assertEqual(paths[0], "specs/derived/spec.md")
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[1].startswith("specs/derived/interface-"))
        self.assertTrue(result.result.draft.validation.valid)

    def test_literal_constant_table_is_pinned_as_asset_not_prose_summarized(self):
        # Regression for #116/#118: a closed literal dict such as
        # `_GCP_FAMILY_HARDWARE` must be extracted verbatim into a pinned asset
        # during `spec derive`, not left to a translator to paraphrase (and lose)
        # into prose Requirements.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "hardware_lookup.py").write_text(
                "_GCP_FAMILY_HARDWARE = {\n"
                "    'g2': ('x86_64', 'NVIDIA L4'),\n"
                "    'g4': ('x86_64', 'NVIDIA RTX PRO 6000 Blackwell'),\n"
                "    'a2': ('x86_64', 'NVIDIA A100'),\n"
                "}\n"
                "\n"
                "\n"
                "def derive_gpu_model(family):\n"
                "    return _GCP_FAMILY_HARDWARE.get(family, (None, 'Unknown'))\n",
                encoding="utf-8",
            )
            inventory = inventory_source(root)
            catalog = dict(load_builtin_skill_catalog())
            result = derive_model_checkout(
                source=root,
                inventory=inventory,
                origin_attestation_id=canonical_digest("origin"),
                skill_set=builtin_skill_set(catalog, languages=("python",)),
                skill_catalog=catalog,
                intelligence_collector=FixtureCollector(),
                translator=CodingCliInverseTranslator(
                    model="test-model", task_runner=LiteralDataObservationTaskRunner()
                ),
            )

        asset_artifacts = [
            item
            for item in result.result.draft.artifacts
            if item.path.startswith("assets/")
        ]
        self.assertEqual(len(asset_artifacts), 1)
        asset = asset_artifacts[0]
        self.assertEqual(
            json.loads(asset.content),
            {
                "g2": ["x86_64", "NVIDIA L4"],
                "g4": ["x86_64", "NVIDIA RTX PRO 6000 Blackwell"],
                "a2": ["x86_64", "NVIDIA A100"],
            },
        )
        # Every literal value survives verbatim; none were dropped by paraphrasing.
        self.assertIn("NVIDIA RTX PRO 6000 Blackwell", asset.content)
        self.assertTrue(result.result.draft.validation.valid)

        pinned_entries = [
            entry
            for entry in result.result.coverage.entries
            if entry.state is CoverageState.ASSET_PINNED
        ]
        self.assertTrue(pinned_entries, "expected an asset-pinned coverage entry")
        self.assertIn(asset.path, pinned_entries[0].reason)
        self.assertIn(asset.content_digest, pinned_entries[0].reason)
        # An asset-pinned surface is never silently reported as merely "covered".
        self.assertNotIn(
            pinned_entries[0].surface_id,
            {
                entry.surface_id
                for entry in result.result.coverage.entries
                if entry.state is CoverageState.COVERED
            },
        )

        literal_surfaces = [
            item
            for item in result.surface_inventory.surfaces
            if item.interface_kind is BehavioralInterfaceKind.LITERAL_DATA
        ]
        self.assertTrue(literal_surfaces, "expected a required literal-data surface")

    def test_insufficient_evidence_recovers_on_a_bounded_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("def main():\n    return 'ok'\n")
            inventory = inventory_source(root)
            catalog = dict(load_builtin_skill_catalog())
            task_runner = InsufficientEvidenceThenObservedTaskRunner()
            derivation = derive_model_checkout(
                source=root,
                inventory=inventory,
                origin_attestation_id=canonical_digest("origin"),
                skill_set=builtin_skill_set(catalog, languages=("python",)),
                skill_catalog=catalog,
                intelligence_collector=FixtureCollector(),
                translator=CodingCliInverseTranslator(
                    model="test-model", task_runner=task_runner
                ),
            )

        self.assertEqual(task_runner.language_call_counts["python"], 2)
        self.assertEqual(derivation.insufficient_evidence_retry_count, 1)
        unsupported_insufficient = [
            item
            for item in derivation.result.coverage.entries
            if item.state is CoverageState.UNSUPPORTED
            and item.reason == "observations were not sufficient for a draft statement"
        ]
        self.assertEqual(unsupported_insufficient, [])

    def test_insufficient_evidence_fails_closed_after_exhausting_bounded_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("def main():\n    return 'ok'\n")
            inventory = inventory_source(root)
            catalog = dict(load_builtin_skill_catalog())
            task_runner = PersistentlyInsufficientEvidenceTaskRunner()
            derivation = derive_model_checkout(
                source=root,
                inventory=inventory,
                origin_attestation_id=canonical_digest("origin"),
                skill_set=builtin_skill_set(catalog, languages=("python",)),
                skill_catalog=catalog,
                intelligence_collector=FixtureCollector(),
                translator=CodingCliInverseTranslator(
                    model="test-model", task_runner=task_runner
                ),
            )

        self.assertEqual(
            task_runner.language_call_counts["python"],
            1 + model_workflow_module._INSUFFICIENT_EVIDENCE_RETRY_ATTEMPTS,
        )
        self.assertEqual(
            derivation.insufficient_evidence_retry_count,
            model_workflow_module._INSUFFICIENT_EVIDENCE_RETRY_ATTEMPTS,
        )
        unsupported_insufficient = [
            item
            for item in derivation.result.coverage.entries
            if item.state is CoverageState.UNSUPPORTED
            and item.reason == "observations were not sufficient for a draft statement"
        ]
        self.assertEqual(len(unsupported_insufficient), 1)

    def test_a_genuine_suspected_defect_is_never_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("def main():\n    return 'ok'\n")
            inventory = inventory_source(root)
            catalog = dict(load_builtin_skill_catalog())
            task_runner = ReproducibleSuspectedDefectTaskRunner()
            derivation = derive_model_checkout(
                source=root,
                inventory=inventory,
                origin_attestation_id=canonical_digest("origin"),
                skill_set=builtin_skill_set(catalog, languages=("python",)),
                skill_catalog=catalog,
                intelligence_collector=FixtureCollector(),
                translator=CodingCliInverseTranslator(
                    model="test-model", task_runner=task_runner
                ),
            )

        self.assertEqual(task_runner.language_call_counts["python"], 1)
        self.assertEqual(derivation.insufficient_evidence_retry_count, 0)
        unsupported_insufficient = [
            item
            for item in derivation.result.coverage.entries
            if item.state is CoverageState.UNSUPPORTED
            and item.reason == "observations were not sufficient for a draft statement"
        ]
        self.assertEqual(len(unsupported_insufficient), 1)

    def test_one_language_uses_multiple_bounded_calls_without_evidence_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.py").write_text("def alpha():\n    return 'alpha'\n")
            (root / "b.py").write_text("def beta():\n    return 'beta'\n")
            inventory = inventory_source(root)
            catalog = dict(load_builtin_skill_catalog())
            runner = ScriptedTaskRunner()
            result = derive_model_checkout(
                source=root,
                inventory=inventory,
                origin_attestation_id=canonical_digest("origin"),
                skill_set=builtin_skill_set(catalog, languages=("python",)),
                skill_catalog=catalog,
                intelligence_collector=FixtureCollector(),
                translator=CodingCliInverseTranslator(
                    model="test-model", task_runner=runner
                ),
                maximum_model_evidence_bytes=40,
            )
            verified = validate_model_translation_record(
                value={
                    "schema": SOURCE_TRANSLATION_RUN_SCHEMA,
                    "mode": MODEL_TRANSLATION_MODE,
                    "intelligence": result.intelligence.to_dict(include_content=True),
                    "journals": [item.to_dict() for item in result.journals],
                },
                result=result.result,
                skill_catalog=catalog,
                evidence_batch_plan=result.evidence_batch_plan.to_dict(),
            )

        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(
            [
                (item.partition_ordinal, item.partition_count)
                for item in result.journals
            ],
            [(0, 2), (1, 2)],
        )
        self.assertEqual(
            {
                evidence_id
                for journal in result.journals
                for evidence_id in journal.evidence_ids
            },
            {item.reference.evidence_id for item in result.intelligence.evidence},
        )
        self.assertEqual(len(result.result.draft.statements), 2)
        self.assertEqual(verified, result.flavor_drafts)

    def test_recovered_component_graph_renders_narrow_dependency_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "class Worker:\n    pass\n\ndef main():\n    return Worker()\n"
            )
            inventory = inventory_source(root)
            catalog = dict(load_builtin_skill_catalog())
            first = derive_model_checkout(
                source=root,
                inventory=inventory,
                origin_attestation_id=canonical_digest("origin"),
                skill_set=builtin_skill_set(catalog, languages=("python",)),
                skill_catalog=catalog,
                intelligence_collector=FixtureCollector(),
                translator=CodingCliInverseTranslator(
                    model="test-model", task_runner=LayeredGraphTaskRunner()
                ),
            )
            second = derive_model_checkout(
                source=root,
                inventory=inventory,
                origin_attestation_id=canonical_digest("origin"),
                skill_set=builtin_skill_set(catalog, languages=("python",)),
                skill_catalog=catalog,
                intelligence_collector=FixtureCollector(),
                translator=CodingCliInverseTranslator(
                    model="test-model", task_runner=LayeredGraphTaskRunner()
                ),
            )

        self.assertEqual(first.result.draft, second.result.draft)
        self.assertEqual(first.result.request.output_provider, "literate-markdown")
        artifacts = {item.path: item.content for item in first.result.draft.artifacts}
        component_specs = {
            path: content
            for path, content in artifacts.items()
            if path.startswith("specs/derived/components/")
            and path != "specs/derived/components/spec.md"
            and path.endswith("/spec.md")
        }
        self.assertEqual(len(component_specs), 2)
        worker_path = next(path for path in component_specs if "-worker/" in path)
        root_path = next(path for path in component_specs if path != worker_path)
        self.assertNotIn("references:", component_specs[root_path])
        worker_prefix = worker_path.rsplit("/", 1)[0] + "/"
        worker = "\n".join(
            content
            for path, content in artifacts.items()
            if path.startswith(worker_prefix)
        )
        self.assertIn("The worker preserves submitted jobs until completion.", worker)
        self.assertIn("a valid job is submitted", worker)
        self.assertIn("the worker records its completed result", worker)
        self.assertTrue(first.result.draft.validation.valid)

        # Promotion rebases each recovered Component into a self-contained local
        # corpus. Graph edges remain typed Component contracts rather than importing
        # another Component's private specification hierarchy.
        from literate_ai.adapters.specifications import LiterateMarkdownProvider
        from literate_ai.source_to_specification.synthesis import (
            promoted_component_specification_projection,
        )

        graph = first.result.component_graph_draft
        assert graph is not None
        for node in graph.nodes:
            projection = promoted_component_specification_projection(
                provider=first.result.request.output_provider,
                graph=graph,
                artifacts=first.result.draft.artifacts,
                coordinate=node.coordinate,
            )
            self.assertEqual(projection[0][1].path, "specs/derived/spec.md")
            with tempfile.TemporaryDirectory() as promoted_temporary:
                promoted_root = Path(promoted_temporary)
                for _source_path, artifact in projection:
                    target = promoted_root / artifact.path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(artifact.content, encoding="utf-8")
                loaded = LiterateMarkdownProvider().load(
                    promoted_root,
                    [artifact.path for _source_path, artifact in projection],
                    id_prefix="promoted",
                )
                self.assertEqual(
                    loaded.specification_set.provider_kind, "literate-markdown"
                )

    def test_unselected_evidence_fails_closed(self):
        runner = ScriptedTaskRunner()
        original = runner.run_json_task

        def unknown_evidence(prompt, *, model=None):
            result = original(prompt, model=model)
            result.response["observations"][0]["evidence_ids"] = ["invented"]
            return result

        runner.run_json_task = unknown_evidence
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
            inventory = inventory_source(root)
            catalog = load_builtin_skill_catalog()
            skill_set = builtin_skill_set(catalog, languages=("python",))
            with self.assertRaisesRegex(SourceToSpecificationError, "evidence outside"):
                derive_model_checkout(
                    source=root,
                    inventory=inventory,
                    origin_attestation_id=canonical_digest("origin"),
                    skill_set=skill_set,
                    skill_catalog=catalog,
                    intelligence_collector=FixtureCollector(),
                    translator=CodingCliInverseTranslator(task_runner=runner),
                )

    def test_each_language_call_must_supply_base_and_flavor_evidence(self):
        runner = ScriptedTaskRunner()
        original = runner.run_json_task

        def base_only(prompt, *, model=None):
            result = original(prompt, model=model)
            result.response["observations"] = result.response["observations"][:1]
            return result

        runner.run_json_task = base_only
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text("def run(): return 1\n", encoding="utf-8")
            inventory = inventory_source(root)
            catalog = load_builtin_skill_catalog()
            skill_set = builtin_skill_set(catalog, languages=("python",))
            with self.assertRaisesRegex(SourceToSpecificationError, "language-Flavor"):
                derive_model_checkout(
                    source=root,
                    inventory=inventory,
                    origin_attestation_id=canonical_digest("origin"),
                    skill_set=skill_set,
                    skill_catalog=catalog,
                    intelligence_collector=FixtureCollector(),
                    translator=CodingCliInverseTranslator(task_runner=runner),
                )

    def test_three_level_component_graph_is_evidence_bound_and_composable(self):
        catalog = load_builtin_skill_catalog()
        skills = tuple(catalog[item] for item in ("architecture", "language-python"))
        evidence_paths = {
            "evidence:root": "main.py",
            "evidence:service": "invoice_service.py",
            "evidence:money": "money.py",
        }
        response = {
            "schema": MODEL_TRANSLATION_OUTPUT_SCHEMA,
            "language": "python",
            "observations": [
                {
                    "skill_id": "architecture",
                    "facet": "entrypoints",
                    "claim_kind": "observed-current-behavior",
                    "requirement": "The application exposes an invoice entrypoint.",
                    "capability": "invoice application",
                    "scenario": {
                        "name": "Run invoice",
                        "when": "the entrypoint receives an invoice",
                        "then": "it emits the invoice total",
                    },
                    "evidence_ids": ["evidence:root"],
                    "confidence_basis_points": 9500,
                    "scope": "base",
                    "component_coordinate": "local/invoice-application",
                },
                {
                    "skill_id": "architecture",
                    "facet": "entrypoints",
                    "claim_kind": "observed-current-behavior",
                    "requirement": "The invoice service aggregates line items.",
                    "capability": "invoice service",
                    "scenario": {
                        "name": "Aggregate lines",
                        "when": "line items are supplied",
                        "then": "the subtotal is emitted",
                    },
                    "evidence_ids": ["evidence:service"],
                    "confidence_basis_points": 9500,
                    "scope": "base",
                    "component_coordinate": "local/invoice-service",
                },
                {
                    "skill_id": "architecture",
                    "facet": "entrypoints",
                    "claim_kind": "observed-current-behavior",
                    "requirement": "Money arithmetic uses integer cents.",
                    "capability": "money calculation",
                    "scenario": {
                        "name": "Calculate discount",
                        "when": "a subtotal and basis points are supplied",
                        "then": "integer totals are emitted",
                    },
                    "evidence_ids": ["evidence:money"],
                    "confidence_basis_points": 9500,
                    "scope": "base",
                    "component_coordinate": "local/money-calculation",
                },
                {
                    "skill_id": "language-python",
                    "facet": "language-binding",
                    "claim_kind": "observed-current-behavior",
                    "requirement": "The implementation uses Python.",
                    "capability": "Python implementation",
                    "scenario": {
                        "name": "Build Python",
                        "when": "the Python Flavor is selected",
                        "then": "Python runs the application",
                    },
                    "evidence_ids": ["evidence:root"],
                    "confidence_basis_points": 9500,
                    "scope": "flavor:python",
                    "component_coordinate": "local/invoice-application",
                },
            ],
            "component_graph": {
                "root_coordinate": "local/invoice-application",
                "nodes": [
                    {
                        "coordinate": "local/invoice-application",
                        "title": "Invoice Application",
                        "kind": "cli",
                        "profiles": ["invoice", "portable"],
                        "provided_capabilities": ["sample.invoice-application"],
                        "capability_contracts": [
                            {
                                "name": "sample.invoice-application",
                                "contract": "Runs invoice processing.",
                                "evidence_ids": ["evidence:root"],
                            }
                        ],
                        "entrypoints": [
                            {
                                "name": "run",
                                "kind": "cli",
                                "path": "main.py",
                                "evidence_ids": ["evidence:root"],
                            }
                        ],
                        "build_needs": ["implementation.language-ecosystem"],
                        "source_paths": ["main.py"],
                        "observation_indexes": [0, 3],
                        "evidence_ids": ["evidence:root"],
                    },
                    {
                        "coordinate": "local/invoice-service",
                        "title": "Invoice Service",
                        "kind": "service",
                        "profiles": ["invoice", "portable"],
                        "provided_capabilities": ["literate-ai.invoice-service"],
                        "capability_contracts": [
                            {
                                "name": "literate-ai.invoice-service",
                                "contract": "Provides invoice operations.",
                                "evidence_ids": ["evidence:service"],
                            }
                        ],
                        "entrypoints": [
                            {
                                "name": "serve",
                                "kind": "service",
                                "path": "invoice_service.py",
                                "evidence_ids": ["evidence:service"],
                            }
                        ],
                        "build_needs": ["implementation.language-ecosystem"],
                        "source_paths": ["invoice_service.py"],
                        "observation_indexes": [1],
                        "evidence_ids": ["evidence:service"],
                    },
                    {
                        "coordinate": "local/money-calculation",
                        "title": "Money Calculation",
                        "kind": "library",
                        "profiles": ["library", "portable"],
                        "provided_capabilities": ["literate-ai.money-calculation"],
                        "capability_contracts": [
                            {
                                "name": "literate-ai.money-calculation",
                                "contract": "Calculates monetary values.",
                                "evidence_ids": ["evidence:money"],
                            }
                        ],
                        "entrypoints": [],
                        "build_needs": ["implementation.language-ecosystem"],
                        "source_paths": ["money.py"],
                        "observation_indexes": [2],
                        "evidence_ids": ["evidence:money"],
                    },
                ],
                "edges": [
                    {
                        "source_coordinate": "local/invoice-application",
                        "target_coordinate": "local/invoice-service",
                        "requirement_id": "invoice-service",
                        "capability": "literate-ai.invoice-service",
                        "version_range": ">=1,<2",
                        "dependency_kind": "runtime",
                        "optional": False,
                        "evidence_ids": ["evidence:root", "evidence:service"],
                    },
                    {
                        "source_coordinate": "local/invoice-service",
                        "target_coordinate": "local/money-calculation",
                        "requirement_id": "money-calculation",
                        "capability": "literate-ai.money-calculation",
                        "version_range": ">=1,<2",
                        "dependency_kind": "runtime",
                        "optional": False,
                        "evidence_ids": ["evidence:service", "evidence:money"],
                    },
                ],
            },
        }
        SchemaCatalog(ROOT / "schemas" / "v2").validate(
            MODEL_TRANSLATION_OUTPUT_SCHEMA, response
        )
        observations = parse_model_observations(
            language="python",
            response=response,
            skills=skills,
            evidence_ids=frozenset(evidence_paths),
        )
        graph = parse_model_component_graph(
            response=response,
            observations=observations,
            evidence_paths=evidence_paths,
            source_snapshot_id=canonical_digest("invoice-source"),
            root_coordinate="local/invoice-application",
        )
        # Optional native imports retain their exact reviewed names across model
        # decoding and cross-language graph merging, and bind the graph identity.
        native_response = json.loads(json.dumps(response))
        native_response["component_graph"]["nodes"][2]["library_imports"] = [
            {
                "language": "python",
                "package": "money_api",
                "capability": "literate-ai.money-calculation",
                "module": "money_api",
                "symbols": ["calculate"],
            }
        ]
        SchemaCatalog(ROOT / "schemas" / "v2").validate(
            MODEL_TRANSLATION_OUTPUT_SCHEMA, native_response
        )
        native_graph = parse_model_component_graph(
            response=native_response,
            observations=observations,
            evidence_paths=evidence_paths,
            source_snapshot_id=canonical_digest("invoice-source"),
            root_coordinate="local/invoice-application",
        )
        self.assertNotEqual(canonical_digest(graph), canonical_digest(native_graph))
        self.assertEqual(
            model_workflow_module._merge_component_graphs((graph, native_graph)),
            native_graph,
        )
        self.assertEqual(
            model_workflow_module._merge_component_graphs((native_graph, native_graph)),
            native_graph,
        )
        conflicting_node = replace(
            native_graph.nodes[2],
            library_imports=(
                replace(native_graph.nodes[2].library_imports[0], symbols=("other",)),
            ),
        )
        with self.assertRaises(SourceToSpecificationError) as conflict:
            model_workflow_module._merge_component_graphs(
                (
                    native_graph,
                    replace(
                        native_graph, nodes=(*native_graph.nodes[:2], conflicting_node)
                    ),
                )
            )
        self.assertEqual(
            conflict.exception.code,
            "model_translation.component_graph_semantics_conflict",
        )
        for invalid in (
            {"interface_identity": "sha256:" + "0" * 64},
            {"capability": "foreign"},
            {"module": "foreign.module"},
        ):
            invalid_response = json.loads(json.dumps(native_response))
            invalid_response["component_graph"]["nodes"][2]["library_imports"][
                0
            ].update(invalid)
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(SourceToSpecificationError),
            ):
                parse_model_component_graph(
                    response=invalid_response,
                    observations=observations,
                    evidence_paths=evidence_paths,
                    source_snapshot_id=canonical_digest("invoice-source"),
                    root_coordinate="local/invoice-application",
                )
        self.assertEqual(len(graph.nodes), 3)
        self.assertEqual(len(graph.edges), 2)
        self.assertEqual(
            tuple((node.kind, len(node.entrypoints)) for node in graph.nodes),
            (("cli", 1), ("service", 1), ("library", 0)),
        )
        self.assertEqual(
            tuple(
                contract.name
                for node in graph.nodes
                for contract in node.capability_contracts
            ),
            (
                "sample.invoice-application",
                "literate-ai.invoice-service",
                "literate-ai.money-calculation",
            ),
        )
        self.assertEqual(
            tuple(item.requirement_id for item in graph.edges),
            ("invoice-service", "money-calculation"),
        )
        invalid_capability = json.loads(json.dumps(response))
        invalid_capability["component_graph"]["nodes"][0]["provided_capabilities"] = [
            "calculateManifest"
        ]
        invalid_capability["component_graph"]["nodes"][0]["capability_contracts"][0][
            "name"
        ] = "calculateManifest"
        with self.assertRaisesRegex(
            SourceToSpecificationError, "never normalized"
        ) as invalid_capability_error:
            parse_model_component_graph(
                response=invalid_capability,
                observations=observations,
                evidence_paths=evidence_paths,
                source_snapshot_id=canonical_digest("invoice-source"),
                root_coordinate="local/invoice-application",
            )
        self.assertEqual(
            invalid_capability_error.exception.code,
            "model_translation.component_graph_identifier_invalid",
        )
        self.assertEqual(
            invalid_capability["component_graph"]["nodes"][0]["provided_capabilities"],
            ["calculateManifest"],
        )

        invalid_contract = json.loads(json.dumps(response))
        invalid_contract["component_graph"]["nodes"][0]["capability_contracts"][0][
            "name"
        ] = "Sample Invoice Application"
        with self.assertRaisesRegex(SourceToSpecificationError, "must match"):
            parse_model_component_graph(
                response=invalid_contract,
                observations=observations,
                evidence_paths=evidence_paths,
                source_snapshot_id=canonical_digest("invoice-source"),
                root_coordinate="local/invoice-application",
            )

        invalid_profile = json.loads(json.dumps(response))
        invalid_profile["component_graph"]["nodes"][0]["profiles"] = ["Source Derived"]
        with self.assertRaisesRegex(SourceToSpecificationError, "must match"):
            parse_model_component_graph(
                response=invalid_profile,
                observations=observations,
                evidence_paths=evidence_paths,
                source_snapshot_id=canonical_digest("invoice-source"),
                root_coordinate="local/invoice-application",
            )

        invalid_requirement = json.loads(json.dumps(response))
        invalid_requirement["component_graph"]["edges"][0]["requirement_id"] = (
            "invoiceService"
        )
        with self.assertRaisesRegex(SourceToSpecificationError, "must match"):
            parse_model_component_graph(
                response=invalid_requirement,
                observations=observations,
                evidence_paths=evidence_paths,
                source_snapshot_id=canonical_digest("invoice-source"),
                root_coordinate="local/invoice-application",
            )
        incomplete = json.loads(json.dumps(response))
        incomplete["observations"][3]["evidence_ids"] = ["evidence:service"]
        incomplete["component_graph"]["nodes"][0]["title"] = (
            "Language-specific display title"
        )
        incomplete_observations = parse_model_observations(
            language="python",
            response=incomplete,
            skills=skills,
            evidence_ids=frozenset(evidence_paths),
        )
        normalized_graph = parse_model_component_graph(
            response=incomplete,
            observations=incomplete_observations,
            evidence_paths=evidence_paths,
            source_snapshot_id=canonical_digest("invoice-source"),
            root_coordinate="local/invoice-application",
        )
        root_node = normalized_graph.nodes[0]
        self.assertEqual(root_node.title, "Invoice Application")

        omitted_index = json.loads(json.dumps(response))
        omitted_index["component_graph"]["nodes"][0]["observation_indexes"] = [0]
        omitted_index["component_graph"]["nodes"][0]["profiles"].append("invoice")
        canonical_graph = parse_model_component_graph(
            response=omitted_index,
            observations=observations,
            evidence_paths=evidence_paths,
            source_snapshot_id=canonical_digest("invoice-source"),
            root_coordinate="local/invoice-application",
        )
        self.assertEqual(
            canonical_graph.nodes[0].observation_ids,
            (observations[0].observation_id, observations[3].observation_id),
        )
        self.assertEqual(canonical_graph.nodes[0].profiles, ("invoice", "portable"))

        omitted_root = json.loads(json.dumps(response))
        del omitted_root["component_graph"]["root_coordinate"]
        root_bound_graph = parse_model_component_graph(
            response=omitted_root,
            observations=observations,
            evidence_paths=evidence_paths,
            source_snapshot_id=canonical_digest("invoice-source"),
            root_coordinate="local/invoice-application",
        )
        self.assertEqual(root_bound_graph.root_coordinate, "local/invoice-application")

        graph_only_entrypoint = json.loads(json.dumps(response))
        graph_only_entrypoint["observations"][0]["facet"] = "dependencies"
        graph_only_entrypoint["observations"][0]["requirement"] = (
            "The application depends on its service boundary."
        )
        entrypoint_surface_evidence = EvidenceReference(
            "evidence:entrypoint-surface",
            canonical_digest("invoice-source"),
            canonical_digest("entrypoint-surface"),
            "main.py",
            "run",
        )
        entrypoint_surface = BehavioralSurfaceInventoryItem.create(
            detector_identity=canonical_digest("surface-detector"),
            language="python",
            interface_kind=BehavioralInterfaceKind.ENTRYPOINT,
            path="main.py",
            symbol="run",
            evidence=(entrypoint_surface_evidence,),
            requirement=BehavioralSurfaceRequirement.REQUIRED,
        )
        normalized_observations = parse_model_observations(
            language="python",
            response=graph_only_entrypoint,
            skills=skills,
            evidence_ids=frozenset((*evidence_paths, "evidence:entrypoint-surface")),
            behavioral_surfaces=(entrypoint_surface,),
        )
        self.assertTrue(
            any(
                item.facet == "entrypoints"
                and item.requirement
                == "The Component exposes run as a cli entrypoint at main.py."
                for item in normalized_observations
            )
        )
        normalized_entrypoint = next(
            item
            for item in normalized_observations
            if item.requirement
            == "The Component exposes run as a cli entrypoint at main.py."
        )
        self.assertEqual(
            normalized_entrypoint.evidence_ids,
            ("evidence:entrypoint-surface",),
        )
        self.assertEqual(root_node.evidence_ids, ("evidence:root", "evidence:service"))
        self.assertEqual(root_node.source_paths, ("invoice_service.py", "main.py"))

        facet_build_need = json.loads(json.dumps(response))
        facet_build_need["component_graph"]["nodes"][0]["build_needs"] = [
            "language-python.runtime-semantics"
        ]
        normalized_build_graph = parse_model_component_graph(
            response=facet_build_need,
            observations=observations,
            evidence_paths=evidence_paths,
            source_snapshot_id=canonical_digest("invoice-source"),
            root_coordinate="local/invoice-application",
        )
        self.assertEqual(
            normalized_build_graph.nodes[0].build_needs,
            ("implementation.language-ecosystem",),
        )

        empty_profiles = json.loads(json.dumps(response))
        empty_profiles["component_graph"]["nodes"][0]["profiles"] = []
        normalized_profile_graph = parse_model_component_graph(
            response=empty_profiles,
            observations=observations,
            evidence_paths=evidence_paths,
            source_snapshot_id=canonical_digest("invoice-source"),
            root_coordinate="local/invoice-application",
        )
        self.assertEqual(
            normalized_profile_graph.nodes[0].profiles,
            ("cli", "python"),
        )

        omitted_capability = json.loads(json.dumps(response))
        omitted_capability["component_graph"]["nodes"][1]["provided_capabilities"] = [
            "sample.invoice-aggregation"
        ]
        with self.assertRaisesRegex(
            SourceToSpecificationError,
            "capability contracts must cover every provided capability",
        ):
            parse_model_component_graph(
                response=omitted_capability,
                observations=observations,
                evidence_paths=evidence_paths,
                source_snapshot_id=canonical_digest("invoice-source"),
                root_coordinate="local/invoice-application",
            )

        unknown_kind = json.loads(json.dumps(response))
        unknown_kind["component_graph"]["nodes"][0]["kind"] = "application"
        with self.assertRaisesRegex(
            SourceToSpecificationError, "exactly library, cli, or service"
        ):
            parse_model_component_graph(
                response=unknown_kind,
                observations=observations,
                evidence_paths=evidence_paths,
                source_snapshot_id=canonical_digest("invoice-source"),
                root_coordinate="local/invoice-application",
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
