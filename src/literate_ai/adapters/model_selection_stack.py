"""Explicit per-agent coding-model stack (ADR 0017).

Each thread and each ``contextvars``-copied agent context has its own stack.
After ``begin_session`` the stack is never empty: frame 0 is the session model.
Nested Component, Flavor, skill, and specification-language changes push when
the model changes and pop when leaving that scope. Popping the only remaining
frame is refused; the warning names the precise caller so the mismatch can be
debugged.
"""

from __future__ import annotations

import inspect
import json
import re
import warnings
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from literate_ai.contracts.generation_cache import CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR
from literate_ai.contracts.model_scopes import ModelScopeBinding, ModelScopeDecision
from literate_ai.diagnostics import emit_log_event

_STACK: ContextVar[tuple[ModelStackFrame, ...] | None] = ContextVar(
    "literate_ai_model_selection_stack",
    default=None,
)
_DIRECTIVE = re.compile(
    r"<!--\s*literate-ai:model-stack\s+(?P<body>.*?)\s*-->",
    re.IGNORECASE | re.DOTALL,
)
_ATTRIBUTE = re.compile(
    r"""(?P<key>[A-Za-z_][\w-]*)\s*=\s*(?:"""
    r""""(?P<dq>[^"]*)\"|'(?P<sq>[^']*)'|(?P<bare>[^\s]+))"""
)
_STACK_OPS = frozenset({"push", "pop", "depth", "current"})
_SKIP_LOCATION_NAMES = frozenset(
    {
        "model_selection_stack.py",
        "contextlib.py",
        "contextvars.py",
    }
)
ModelStackReason = Literal[
    "session-user-default",
    "session-cli-override",
    "session",
    "pipeline",
    "component",
    "flavor-role",
    "skill-invocation",
    "section",
    "spec-language",
]
ModelStackOpName = Literal["push", "pop", "depth", "current"]


class ModelStackError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ModelStackPopRefusedWarning(UserWarning):
    """Emitted when a caller tries to pop the session's only model frame."""


@dataclass(frozen=True, slots=True)
class ModelStackFrame:
    coding_cli: str
    model: str
    reason: str
    location: str = ""

    @property
    def explicit_model(self) -> str | None:
        if self.model in {"", CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR}:
            return None
        return self.model


@dataclass(frozen=True, slots=True)
class ModelStackOperation:
    """One authorable push, pop, or query in the specification language."""

    op: ModelStackOpName
    model: str | None = None
    coding_cli: str | None = None
    section: str | None = None
    source: str = ""
    line: int | None = None

    def location(self) -> str:
        parts: list[str] = []
        if self.source:
            parts.append(
                self.source if self.line is None else f"{self.source}:{self.line}"
            )
        if self.section:
            parts.append(f"section={self.section}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class ModelStackSnapshot:
    frames: tuple[ModelStackFrame, ...]

    @property
    def depth(self) -> int:
        return len(self.frames)

    @property
    def current(self) -> ModelStackFrame:
        if not self.frames:
            raise ModelStackError(
                "model_stack.empty",
                "model stack has no session; begin_session first",
            )
        return self.frames[-1]


def begin_session(
    *,
    coding_cli: str,
    model: str,
    reason: str = "session",
    location: str = "session",
    user_coding_cli: str | None = None,
    user_model: str | None = None,
    cli_override: bool = False,
) -> ModelStackSnapshot:
    """Replace this context's stack with frame 0. Depth is always 1 afterwards."""

    if not coding_cli.strip():
        raise ModelStackError(
            "model_stack.invalid_session",
            "model stack session requires a coding CLI",
        )
    selector = model.strip() or CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR
    frame_reason = "session-cli-override" if cli_override else reason
    frame = ModelStackFrame(coding_cli.strip(), selector, frame_reason, location)
    _STACK.set((frame,))
    emit_log_event(
        "model.session.start",
        coding_cli=frame.coding_cli,
        model=frame.explicit_model or "",
        model_stack_depth=1,
        location=location,
        reason=frame.reason,
    )
    if user_coding_cli or user_model:
        emit_log_event(
            "model.session.user_default",
            coding_cli=user_coding_cli or "",
            model=user_model or "",
        )
    if cli_override:
        emit_log_event(
            "model.session.cli_override",
            coding_cli=frame.coding_cli,
            model=frame.explicit_model or "",
            user_coding_cli=user_coding_cli or "",
            user_model=user_model or "",
        )
    return snapshot()


def reset_session() -> None:
    """Clear this context's stack. Tests use this; generation must begin again."""

    _STACK.set(None)


def snapshot() -> ModelStackSnapshot:
    return ModelStackSnapshot(_require_frames())


def depth() -> int:
    """Specification-language query: current stack depth (never 0 in a session)."""

    return snapshot().depth


def current() -> ModelStackFrame:
    """Specification-language query: the model the converter will use."""

    return snapshot().current


def stack_log_fields() -> dict[str, object]:
    frames = _STACK.get()
    if not frames:
        return {}
    top = frames[-1]
    fields: dict[str, object] = {
        "coding_cli": top.coding_cli,
        "model": top.explicit_model or "",
        "model_stack_depth": len(frames),
        "model_scope": top.reason,
    }
    if top.location:
        fields["model_stack_location"] = top.location
    return fields


def explicit_stack_model() -> str | None:
    frames = _STACK.get()
    if not frames:
        return None
    return frames[-1].explicit_model


def push(
    *,
    coding_cli: str | None = None,
    model: str,
    reason: str = "spec-language",
    location: str = "",
) -> ModelStackSnapshot:
    frames = _require_frames()
    cli = (coding_cli or frames[-1].coding_cli).strip()
    selector = model.strip()
    if not cli or not selector:
        raise ModelStackError(
            "model_stack.invalid_push",
            "model stack push requires a coding CLI and a model",
        )
    frame = ModelStackFrame(cli, selector, reason, location)
    _STACK.set((*frames, frame))
    emit_log_event(
        "model.stack.push",
        coding_cli=cli,
        model=frame.explicit_model or selector,
        model_stack_depth=len(frames) + 1,
        reason=reason,
        location=location,
    )
    return snapshot()


def pop(*, location: str | None = None) -> ModelStackSnapshot:
    """Pop unless this is the session frame; depth 1 is never removed."""

    frames = _require_frames()
    caller = caller_location()
    spec_location = location or ""
    if len(frames) <= 1:
        top = frames[0]
        message = (
            "model stack pop refused: would empty the stack (depth=1). "
            f"The session model stays {top.coding_cli}/{top.model}. "
            f"Caller: {caller}."
        )
        if spec_location:
            message += f" Spec location: {spec_location}."
        emit_log_event(
            "model.stack.pop_refused",
            coding_cli=top.coding_cli,
            model=top.explicit_model or top.model,
            model_stack_depth=1,
            caller=caller,
            location=spec_location,
            caller_file=_caller_file(),
            caller_line=_caller_line(),
            caller_function=_caller_function(),
        )
        warnings.warn(message, ModelStackPopRefusedWarning, stacklevel=2)
        return snapshot()
    remaining = frames[:-1]
    _STACK.set(remaining)
    emit_log_event(
        "model.stack.pop",
        coding_cli=remaining[-1].coding_cli,
        model=remaining[-1].explicit_model or remaining[-1].model,
        model_stack_depth=len(remaining),
        location=spec_location,
        popped_reason=frames[-1].reason,
        popped_location=frames[-1].location,
    )
    return snapshot()


@contextmanager
def scope(
    *,
    coding_cli: str | None = None,
    model: str,
    reason: str = "spec-language",
    location: str = "",
) -> Iterator[ModelStackSnapshot]:
    entered = push(
        coding_cli=coding_cli,
        model=model,
        reason=reason,
        location=location,
    )
    try:
        yield entered
    finally:
        pop(location=location)


def apply_model_scope_binding(binding: ModelScopeBinding) -> ModelStackSnapshot:
    """Push OVERRIDE frames from a lexical binding when the model actually changes."""

    for step in binding.resolution_trace:
        if step.decision is not ModelScopeDecision.OVERRIDE:
            continue
        top = current()
        if (
            top.coding_cli == binding.provider_id
            and top.model == step.selected_selector
        ):
            continue
        push(
            coding_cli=binding.provider_id,
            model=step.selected_selector,
            reason=step.scope_kind.value,
            location=step.owner_identity.uri,
        )
    return snapshot()


def apply_spec_stack_operations(
    operations: Sequence[ModelStackOperation],
    *,
    default_cli: str | None = None,
) -> ModelStackSnapshot:
    """Execute authorable specification-language stack operations."""

    for operation in operations:
        if operation.op == "push":
            if not operation.model:
                raise ModelStackError(
                    "model_stack.invalid_push",
                    "specification-language model-stack push requires a model",
                )
            push(
                coding_cli=operation.coding_cli or default_cli,
                model=operation.model,
                reason="section" if operation.section else "spec-language",
                location=operation.location(),
            )
        elif operation.op == "pop":
            pop(location=operation.location())
        elif operation.op in {"depth", "current"}:
            continue
        else:
            raise ModelStackError(
                "model_stack.invalid_operation",
                f"unsupported model-stack operation {operation.op!r}",
            )
    return snapshot()


def query_spec_stack_operation(operation: ModelStackOperation) -> int | str:
    """Evaluate a specification-language depth or current-model query."""

    if operation.op == "depth":
        return depth()
    if operation.op == "current":
        frame = current()
        return frame.explicit_model or frame.model
    raise ModelStackError(
        "model_stack.invalid_operation",
        f"model-stack query does not support operation {operation.op!r}",
    )


def parse_model_stack_operations(
    text: str,
    *,
    source: str = "",
) -> tuple[ModelStackOperation, ...]:
    """Parse HTML-comment directives from a Component or Flavor specification."""

    operations: list[ModelStackOperation] = []
    for match in _DIRECTIVE.finditer(text):
        line = text[: match.start()].count("\n") + 1
        attributes = _parse_attributes(match.group("body"))
        operations.append(_operation_from_mapping(attributes, source=source, line=line))
    return tuple(operations)


def parse_model_stack_document(
    value: Mapping[str, object],
    *,
    source: str = "",
) -> tuple[ModelStackOperation, ...]:
    """Parse optional ``stack`` operations from coding-model-selection@2 JSON."""

    schema = value.get("schema")
    if schema != "literate-ai/coding-model-selection@2":
        return ()
    raw = value.get("stack", [])
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise ModelStackError(
            "model_stack.invalid_document",
            "coding-model-selection stack must be an array",
        )
    return tuple(
        _operation_from_mapping(item, source=source, line=index + 1)
        if isinstance(item, Mapping)
        else _reject_stack_item(item)
        for index, item in enumerate(raw)
    )


def parse_model_stack_json(
    content: str,
    *,
    source: str = "",
) -> tuple[ModelStackOperation, ...]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ModelStackError(
            "model_stack.invalid_document",
            "coding-model-selection stack document must be UTF-8 JSON",
        ) from exc
    if not isinstance(value, dict):
        raise ModelStackError(
            "model_stack.invalid_document",
            "coding-model-selection stack document must be one JSON object",
        )
    return parse_model_stack_document(value, source=source)


def caller_location() -> str:
    """Return the first non-stack caller plus a short chain for debugging."""

    frames = _caller_frames()
    if not frames:
        return "<unknown>"
    return " <- ".join(frames[:3])


def _skip_caller_frame(filename: str) -> bool:
    return Path(filename).name in _SKIP_LOCATION_NAMES


def _caller_frames() -> list[str]:
    recorded: list[str] = []
    for info in inspect.stack()[1:]:
        filename = info.filename
        if _skip_caller_frame(filename):
            continue
        recorded.append(f"{filename}:{info.lineno} in {info.function}")
        if len(recorded) >= 4:
            break
    return recorded


def _caller_file() -> str:
    frames = _caller_frames()
    if not frames:
        return ""
    return frames[0].rsplit(":", 1)[0].rsplit(" in ", 1)[0]


def _caller_line() -> int | None:
    for info in inspect.stack()[1:]:
        if _skip_caller_frame(info.filename):
            continue
        return info.lineno
    return None


def _caller_function() -> str:
    for info in inspect.stack()[1:]:
        if _skip_caller_frame(info.filename):
            continue
        return info.function
    return ""


def _require_frames() -> tuple[ModelStackFrame, ...]:
    frames = _STACK.get()
    if not frames:
        raise ModelStackError(
            "model_stack.empty",
            "model stack has no session; begin_session first",
        )
    return frames


def _parse_attributes(body: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for match in _ATTRIBUTE.finditer(body):
        value = match.group("dq") or match.group("sq") or match.group("bare") or ""
        attributes[match.group("key")] = value
    return attributes


def _operation_from_mapping(
    value: Mapping[str, object],
    *,
    source: str,
    line: int | None,
) -> ModelStackOperation:
    op = str(value.get("op", "")).strip()
    if op not in _STACK_OPS:
        raise ModelStackError(
            "model_stack.invalid_operation",
            "model-stack directive op must be push, pop, depth, or current",
        )
    model = _optional_text(value.get("model"))
    if op == "push" and not model:
        raise ModelStackError(
            "model_stack.invalid_push",
            "specification-language model-stack push requires a model",
        )
    return ModelStackOperation(
        op,  # type: ignore[arg-type]
        model=model,
        coding_cli=_optional_text(value.get("coding_cli")),
        section=_optional_text(value.get("section")),
        source=source,
        line=line,
    )


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _reject_stack_item(_item: object) -> ModelStackOperation:
    raise ModelStackError(
        "model_stack.invalid_document",
        "coding-model-selection stack items must be objects",
    )


def begin_generation_session(
    *,
    coding_cli: str,
    recipe_documents: Iterable[object] | None = None,
    model_scope: ModelScopeBinding | None = None,
    pipeline_model: str | None = None,
    location: str = "coding_session",
) -> ModelStackSnapshot:
    """Start a generation session and apply lexical plus authored stack ops."""

    session_model = pipeline_model or CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR
    if model_scope is not None and model_scope.resolution_trace:
        session_model = model_scope.resolution_trace[0].selected_selector
    begin_session(
        coding_cli=coding_cli,
        model=session_model,
        reason="session",
        location=location,
    )
    if model_scope is not None:
        apply_model_scope_binding(model_scope)
    if recipe_documents is not None:
        for document in recipe_documents:
            path = getattr(document, "path", "") or location
            content = getattr(document, "content", None)
            if isinstance(content, str):
                apply_spec_stack_operations(
                    parse_model_stack_operations(content, source=str(path)),
                    default_cli=coding_cli,
                )
    return snapshot()
