"""Compile pinned workflow and routing documents into one executable generation plan."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.contracts import ComponentDefinition, semantic_version
from literate_ai.contracts._validation import fields
from literate_ai.contracts.authoring_markdown import (
    AuthoringMarkdownError,
    parse_authoring_markdown,
)
from literate_ai.models import (
    DataEgress,
    Locality,
    ModelEndpoint,
    ModelGroup,
    ModelRouter,
    StageModelPolicy,
)
from literate_ai.workflows import StageDefinition, WorkflowDefinition

from .models import (
    FULL_GENERATION_LIFECYCLE_STEPS,
    GenerationExecutionPlan,
    IndependentAcceptancePolicy,
    ModelStage,
)

GENERATION_WORKFLOW_SCHEMA = "literate-ai/generation-workflow@1"
GENERATION_WORKFLOW_MARKDOWN_SCHEMA = "literate-ai/generation-workflow-markdown@1"
GENERATION_ROUTING_SCHEMA = "literate-ai/generation-routing@1"


class GenerationPlanningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _StageBinding:
    stage_id: str
    kind: str
    dependencies: tuple[str, ...]
    instructions: str
    response_schema_name: str
    content_kind: str
    required_capabilities: tuple[str, ...]
    produces_tree: bool
    maximum_output_tokens: int | None


@dataclass(frozen=True, slots=True)
class _RoutingBinding:
    routing_id: str
    version: str
    group_id: str
    required_locality: Locality | None
    fallback_allowed: bool
    data_egress: DataEgress


def compile_generation_execution_plan(
    *,
    component: ComponentDefinition,
    workflow_content: bytes,
    routing_content: bytes,
    endpoint: ModelEndpoint,
    generation_prompt: str,
    stage_endpoints: Mapping[str, ModelEndpoint] | None = None,
    independent_acceptance_policy: IndependentAcceptancePolicy | None = None,
    project_root: Path | None = None,
) -> GenerationExecutionPlan:
    """Verify both pins and compile their exact content into routes and stages."""

    _require_pinned(
        workflow_content,
        component.workflow_definition.identity.digest,
        "workflow",
    )
    _require_pinned(
        routing_content,
        component.routing_policy.identity.digest,
        "routing policy",
    )
    workflow_value = normalize_generation_workflow_document(
        workflow_content, project_root=project_root
    )
    routing_value = _json_object(routing_content, "routing policy")
    workflow, bindings = _workflow(workflow_value)
    routing = _routing(routing_value)

    endpoint_overrides = dict(stage_endpoints or {})
    model_stage_ids = {
        stage_id for stage_id, binding in bindings.items() if binding.kind == "model"
    }
    unknown_overrides = set(endpoint_overrides).difference(model_stage_ids)
    if unknown_overrides:
        raise GenerationPlanningError(
            "generation_plan.endpoint_stage_unknown",
            "stage endpoint overrides must target model stages in the pinned workflow",
        )
    model_stages: list[ModelStage] = []
    routes = []
    lifecycle_steps: list[str] = []
    for stage_id in workflow.order:
        binding = bindings[stage_id]
        if binding.kind == "lifecycle":
            lifecycle_steps.append(stage_id)
            continue
        stage_endpoint = endpoint_overrides.get(stage_id, endpoint)
        group = ModelGroup(
            routing.group_id,
            routing.version,
            (stage_endpoint.endpoint_id,),
            (stage_endpoint.ref,),
        )
        router = ModelRouter(endpoints=(stage_endpoint,), groups=(group,))
        policy = StageModelPolicy(
            policy_id=f"{routing.routing_id}:{stage_id}@{routing.version}",
            stage_type=stage_id,
            group_id=group.group_id,
            group_ref=group.ref,
            required_capabilities=binding.required_capabilities,
            required_locality=routing.required_locality,
            allowed_providers=(stage_endpoint.provider,),
            fallback_allowed=routing.fallback_allowed,
            data_egress=routing.data_egress,
            version=routing.version,
        )
        stage = ModelStage(
            stage_id=stage_id,
            policy=policy,
            dependencies=binding.dependencies,
            produces_tree=binding.produces_tree,
            instructions=binding.instructions.rstrip() + "\n\n" + generation_prompt,
            response_schema_name=binding.response_schema_name,
            response_schema={"type": "object"},
            content_kind=binding.content_kind,
            max_output_tokens=binding.maximum_output_tokens,
        )
        model_stages.append(stage)
        routes.append(router.select(policy))

    try:
        return GenerationExecutionPlan(
            component.workflow_definition,
            component.routing_policy,
            tuple(model_stages),
            tuple(routes),
            tuple(lifecycle_steps),
            independent_acceptance_policy or IndependentAcceptancePolicy(),
        )
    except (TypeError, ValueError) as exc:
        raise GenerationPlanningError(
            "generation_plan.invalid", "pinned generation plan is not executable"
        ) from exc


def _require_pinned(content: bytes, expected: str, label: str) -> None:
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise GenerationPlanningError(
            f"generation_plan.{label.replace(' ', '_')}_drift",
            f"pinned {label} identity changed",
        )


def _json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GenerationPlanningError(
            "generation_plan.invalid_json", f"{label} must be a UTF-8 JSON object"
        ) from exc
    if not isinstance(value, dict):
        raise GenerationPlanningError(
            "generation_plan.invalid_json", f"{label} must be a JSON object"
        )
    return value


def _workflow_document(
    content: bytes,
    *,
    project_root: Path | None = None,
    source: str | Path | None = None,
    _stack: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Project canonical Markdown into the unchanged workflow wire contract."""

    if not content.startswith(b"---\n"):
        return _json_object(content, "workflow")
    try:
        raw, body = parse_authoring_markdown(content, source="generation workflow")
        data = fields(
            raw,
            path="generation workflow",
            required=frozenset({"schema", "workflow_id", "version", "stages"}),
            optional=frozenset({"extends"}),
        )
    except (AuthoringMarkdownError, TypeError, ValueError) as exc:
        raise GenerationPlanningError(
            "generation_plan.invalid_workflow_markdown",
            "workflow Markdown is malformed",
        ) from exc
    if data["schema"] != GENERATION_WORKFLOW_MARKDOWN_SCHEMA:
        raise GenerationPlanningError(
            "generation_plan.workflow_schema",
            "unsupported generation workflow Markdown schema",
        )
    raw_stages = data["stages"]
    if not isinstance(raw_stages, list):
        raise GenerationPlanningError(
            "generation_plan.invalid_workflow_markdown",
            "workflow Markdown stages must be an array",
        )
    extends = data.get("extends")
    if extends is not None and (not isinstance(extends, str) or not extends.strip()):
        raise GenerationPlanningError(
            "generation_plan.workflow_extends_invalid",
            "workflow extends must be a catalog-relative path",
        )
    sections = _workflow_instruction_sections(body)
    stage_ids: set[str] = set()
    stages: list[dict[str, Any]] = []
    for index, raw_stage in enumerate(raw_stages):
        if not isinstance(raw_stage, dict):
            raise GenerationPlanningError(
                "generation_plan.invalid_workflow_markdown",
                "workflow Markdown stages must be mappings",
            )
        if "instructions" in raw_stage:
            raise GenerationPlanningError(
                "generation_plan.workflow_instruction_ambiguous",
                "workflow instructions belong only in Markdown Stage sections",
            )
        stage_id = raw_stage.get("stage_id")
        kind = raw_stage.get("kind")
        if not isinstance(stage_id, str) or not stage_id or stage_id in stage_ids:
            raise GenerationPlanningError(
                "generation_plan.invalid_workflow_markdown",
                f"workflow Markdown stage {index} has an invalid or duplicate ID",
            )
        stage_ids.add(stage_id)
        instruction = sections.get(stage_id, "")
        if kind == "model" and not instruction and not extends:
            raise GenerationPlanningError(
                "generation_plan.workflow_instruction_missing",
                f"model stage {stage_id!r} requires a Markdown Stage section",
            )
        if kind != "model" and instruction:
            raise GenerationPlanningError(
                "generation_plan.workflow_instruction_invalid",
                f"non-model stage {stage_id!r} cannot have model instructions",
            )
        stages.append({**raw_stage, "instructions": instruction})
    unknown = set(sections) - stage_ids
    if unknown:
        raise GenerationPlanningError(
            "generation_plan.workflow_instruction_unknown",
            f"Markdown instructions name unknown stage {sorted(unknown)[0]!r}",
        )
    result: dict[str, Any] = {
        "schema": GENERATION_WORKFLOW_SCHEMA,
        "workflow_id": data["workflow_id"],
        "version": data["version"],
        "stages": stages,
    }
    if not extends:
        return result
    parent_path = _resolve_extended_workflow(
        extends.strip(), project_root=project_root, source=source, stack=_stack
    )
    parent = _workflow_document(
        parent_path.read_bytes(),
        project_root=project_root,
        source=parent_path,
        _stack=(*_stack, parent_path),
    )
    return _merge_extended_workflows(parent, result)


def normalize_generation_workflow_document(
    content: bytes,
    *,
    project_root: Path | None = None,
    source: str | Path | None = None,
) -> dict[str, Any]:
    """Return the strict machine projection for JSON compatibility or Markdown."""

    value = _workflow_document(content, project_root=project_root, source=source)
    _require_model_instructions(value)
    workflow, bindings = _workflow(value)
    _require_supported_flow(workflow, bindings)
    return value


def _workflow_roots(project_root: Path) -> tuple[Path, ...]:
    from literate_ai.projects import discover_project

    loaded = discover_project(project_root)
    if loaded is None:
        return (project_root / "workflows",)
    return tuple(loaded.roots("workflow"))


def _resolve_extended_workflow(
    extends: str,
    *,
    project_root: Path | None,
    source: str | Path | None,
    stack: tuple[Path, ...],
) -> Path:
    relative = Path(extends)
    if relative.is_absolute() or ".." in relative.parts:
        raise GenerationPlanningError(
            "generation_plan.workflow_extends_invalid",
            "workflow extends must be a catalog-relative path",
        )
    if project_root is None and source is not None:
        origin = Path(source)
        for candidate in (origin, *origin.parents):
            if (candidate / "literate.project.json").is_file():
                project_root = candidate
                break
    if project_root is None:
        raise GenerationPlanningError(
            "generation_plan.workflow_extends_unresolved",
            "workflow extends requires a project root",
        )
    for root in _workflow_roots(project_root):
        candidate = root.joinpath(*relative.parts)
        if candidate.is_file() and not candidate.is_symlink():
            resolved = candidate.resolve()
            if resolved in stack:
                raise GenerationPlanningError(
                    "generation_plan.workflow_extends_cycle",
                    "workflow extends contains a cycle",
                )
            return resolved
    raise GenerationPlanningError(
        "generation_plan.workflow_extends_unresolved",
        f"extended workflow {extends!r} was not found",
    )


def _merge_extended_workflows(
    parent: dict[str, Any], child: dict[str, Any]
) -> dict[str, Any]:
    merged = [dict(stage) for stage in parent["stages"]]
    index = {str(stage["stage_id"]): position for position, stage in enumerate(merged)}
    for stage in child["stages"]:
        stage_id = str(stage["stage_id"])
        overlay = dict(stage)
        if stage_id in index:
            current = merged[index[stage_id]]
            if not overlay.get("instructions"):
                overlay["instructions"] = current.get("instructions", "")
            merged[index[stage_id]] = overlay
            continue
        dependencies = overlay.get("dependencies") or []
        insert_at = 0
        if isinstance(dependencies, list):
            for dependency in dependencies:
                if dependency in index:
                    insert_at = max(insert_at, index[dependency] + 1)
        merged.insert(insert_at, overlay)
        index = {
            str(item["stage_id"]): position for position, item in enumerate(merged)
        }
    return {
        "schema": GENERATION_WORKFLOW_SCHEMA,
        "workflow_id": child["workflow_id"],
        "version": child["version"],
        "stages": merged,
    }


def _require_model_instructions(value: dict[str, Any]) -> None:
    for stage in value.get("stages", []):
        if not isinstance(stage, dict):
            continue
        if stage.get("kind") == "model" and not stage.get("instructions"):
            raise GenerationPlanningError(
                "generation_plan.workflow_instruction_missing",
                f"model stage {stage.get('stage_id')!r} requires a Markdown Stage "
                "section",
            )


def _workflow_instruction_sections(body: str) -> dict[str, str]:
    marker = "## Stage: "
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        if line.startswith(marker):
            stage_id = line[len(marker) :].strip()
            if not stage_id or stage_id in sections:
                raise GenerationPlanningError(
                    "generation_plan.workflow_instruction_ambiguous",
                    "workflow Stage headings must be non-empty and unique",
                )
            current = stage_id
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    return {
        stage_id: "\n".join(lines).strip()
        for stage_id, lines in sections.items()
        if "\n".join(lines).strip()
    }


def _exact_fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise GenerationPlanningError(
            "generation_plan.invalid_fields", f"{label} fields do not match its schema"
        )


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise GenerationPlanningError(
            "generation_plan.invalid_value", f"{label} must be a string"
        )
    return value


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise GenerationPlanningError(
            "generation_plan.invalid_value", f"{label} must be a string array"
        )
    result = tuple(value)
    if len(set(result)) != len(result):
        raise GenerationPlanningError(
            "generation_plan.invalid_value", f"{label} must not contain duplicates"
        )
    return result


def _workflow(
    value: dict[str, Any],
) -> tuple[WorkflowDefinition, dict[str, _StageBinding]]:
    _exact_fields(value, {"schema", "workflow_id", "version", "stages"}, "workflow")
    if value["schema"] != GENERATION_WORKFLOW_SCHEMA:
        raise GenerationPlanningError(
            "generation_plan.workflow_schema", "unsupported generation workflow schema"
        )
    workflow_id = _text(value["workflow_id"], "workflow.workflow_id")
    version = semantic_version(value["version"], "workflow.version")
    raw_stages = value["stages"]
    if not isinstance(raw_stages, list) or not raw_stages:
        raise GenerationPlanningError(
            "generation_plan.invalid_workflow", "workflow requires stages"
        )
    definitions: list[StageDefinition] = []
    bindings: dict[str, _StageBinding] = {}
    stage_fields = {
        "stage_id",
        "kind",
        "dependencies",
        "instructions",
        "response_schema_name",
        "content_kind",
        "required_capabilities",
        "produces_tree",
        "maximum_output_tokens",
    }
    for index, raw in enumerate(raw_stages):
        if not isinstance(raw, dict):
            raise GenerationPlanningError(
                "generation_plan.invalid_stage", "workflow stages must be objects"
            )
        _exact_fields(raw, stage_fields, f"workflow.stages[{index}]")
        stage_id = _text(raw["stage_id"], f"workflow.stages[{index}].stage_id")
        kind = _text(raw["kind"], f"workflow.stages[{index}].kind")
        if kind not in {"model", "lifecycle"}:
            raise GenerationPlanningError(
                "generation_plan.invalid_stage", "stage kind must be model or lifecycle"
            )
        dependencies = _text_tuple(
            raw["dependencies"], f"workflow.stages[{index}].dependencies"
        )
        instructions = _text(
            raw["instructions"],
            f"workflow.stages[{index}].instructions",
            empty=kind == "lifecycle",
        )
        response_schema_name = _text(
            raw["response_schema_name"],
            f"workflow.stages[{index}].response_schema_name",
            empty=kind == "lifecycle",
        )
        content_kind = _text(
            raw["content_kind"], f"workflow.stages[{index}].content_kind"
        )
        if content_kind not in {"metadata", "source"}:
            raise GenerationPlanningError(
                "generation_plan.invalid_stage", "unsupported stage content kind"
            )
        capabilities = _text_tuple(
            raw["required_capabilities"],
            f"workflow.stages[{index}].required_capabilities",
        )
        produces_tree = raw["produces_tree"]
        maximum = raw["maximum_output_tokens"]
        if not isinstance(produces_tree, bool) or (
            maximum is not None
            and (
                not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1
            )
        ):
            raise GenerationPlanningError(
                "generation_plan.invalid_stage", "invalid stage output configuration"
            )
        if kind == "lifecycle" and (
            stage_id not in FULL_GENERATION_LIFECYCLE_STEPS
            or instructions
            or response_schema_name
            or content_kind != "metadata"
            or capabilities
            or produces_tree
            or maximum is not None
        ):
            raise GenerationPlanningError(
                "generation_plan.invalid_lifecycle_stage",
                "lifecycle stages cannot contain model configuration",
            )
        definitions.append(StageDefinition(stage_id, kind, dependencies))
        bindings[stage_id] = _StageBinding(
            stage_id,
            kind,
            dependencies,
            instructions,
            response_schema_name,
            content_kind,
            capabilities,
            produces_tree,
            maximum,
        )
    try:
        workflow = WorkflowDefinition(workflow_id, version, tuple(definitions))
    except ValueError as exc:
        raise GenerationPlanningError(
            "generation_plan.invalid_workflow", "generation workflow DAG is invalid"
        ) from exc
    return workflow, bindings


def _require_supported_flow(
    workflow: WorkflowDefinition, bindings: dict[str, _StageBinding]
) -> None:
    ordered = tuple(bindings[stage_id] for stage_id in workflow.order)
    first_lifecycle = next(
        (index for index, item in enumerate(ordered) if item.kind == "lifecycle"),
        len(ordered),
    )
    if any(item.kind == "model" for item in ordered[first_lifecycle:]):
        raise GenerationPlanningError(
            "generation_plan.unsupported_flow",
            "generation model stages must precede the guarded lifecycle",
        )
    model_stages = ordered[:first_lifecycle]
    lifecycle_stages = ordered[first_lifecycle:]
    tree_stages = [item for item in model_stages if item.produces_tree]
    if len(tree_stages) != 1 or tree_stages[0] is not model_stages[-1]:
        raise GenerationPlanningError(
            "generation_plan.unsupported_flow",
            "the final model stage must be the only source-tree producer",
        )
    if tuple(item.stage_id for item in lifecycle_stages) != (
        FULL_GENERATION_LIFECYCLE_STEPS
    ):
        raise GenerationPlanningError(
            "generation_plan.unsupported_flow",
            "generation requires the complete guarded lifecycle",
        )

    def transitively_depends_on(stage_id: str, required_id: str) -> bool:
        pending = list(bindings[stage_id].dependencies)
        visited: set[str] = set()
        while pending:
            dependency = pending.pop()
            if dependency == required_id:
                return True
            if dependency in visited:
                continue
            visited.add(dependency)
            pending.extend(bindings[dependency].dependencies)
        return False

    required_predecessor = tree_stages[0].stage_id
    for stage in lifecycle_stages:
        if not transitively_depends_on(stage.stage_id, required_predecessor):
            raise GenerationPlanningError(
                "generation_plan.unsupported_flow",
                "guarded lifecycle stages must preserve the required safety order",
            )
        required_predecessor = stage.stage_id


def _routing(value: dict[str, Any]) -> _RoutingBinding:
    _exact_fields(
        value,
        {
            "schema",
            "routing_id",
            "version",
            "group_id",
            "required_locality",
            "fallback_allowed",
            "data_egress",
        },
        "routing policy",
    )
    if value["schema"] != GENERATION_ROUTING_SCHEMA:
        raise GenerationPlanningError(
            "generation_plan.routing_schema", "unsupported generation routing schema"
        )
    locality_value = value["required_locality"]
    try:
        locality = None if locality_value is None else Locality(locality_value)
        egress = DataEgress(value["data_egress"])
    except (TypeError, ValueError) as exc:
        raise GenerationPlanningError(
            "generation_plan.invalid_routing", "routing constraints are invalid"
        ) from exc
    fallback = value["fallback_allowed"]
    if not isinstance(fallback, bool):
        raise GenerationPlanningError(
            "generation_plan.invalid_routing", "fallback_allowed must be a boolean"
        )
    return _RoutingBinding(
        _text(value["routing_id"], "routing.routing_id"),
        semantic_version(value["version"], "routing.version"),
        _text(value["group_id"], "routing.group_id"),
        locality,
        fallback,
        egress,
    )


__all__ = [
    "GENERATION_ROUTING_SCHEMA",
    "GENERATION_WORKFLOW_SCHEMA",
    "GENERATION_WORKFLOW_MARKDOWN_SCHEMA",
    "GenerationPlanningError",
    "compile_generation_execution_plan",
    "normalize_generation_workflow_document",
]
