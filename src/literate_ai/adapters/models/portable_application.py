"""Validation for language-neutral portable application specifications."""

from __future__ import annotations

import json
from collections.abc import Mapping

from literate_ai.contracts import (
    ContractValidationError,
    canonical_json_bytes,
    source_cache_model_selector,
)

PORTABLE_APPLICATION_SCHEMA = "literate-ai/portable-application@2"
SUPPORTED_IMPLEMENTATION_LANGUAGES = (
    "cpp",
    "elixir",
    "go",
    "javascript",
    "python",
    "rust",
    "swift",
)
_SOURCE_ENTRYPOINTS = {
    "cpp": "source/main.cpp",
    "elixir": "source/main.exs",
    "go": "source/main.go",
    "javascript": "source/main.js",
    "python": "source/main.py",
    "rust": "source/main.rs",
    "swift": "source/main.swift",
}
_CODING_CLIS = {"codex", "claude", "cursor-agent", "opencode"}


class PortableApplicationError(ValueError):
    """A portable application specification is invalid."""


_SUPPORTED_KINDS = frozenset(
    {
        "build-matrix",
        "batch-manifest-calculator",
        "content-cache",
        "critical-path-scheduler",
        "dependency-planner",
        "framework-compatibility-readiness-command",
        "greeting-summary",
        "integer-statistics",
        "invoice",
        "ledger-workbench",
        "model-router",
        "publication-manifest",
        "release-risk-dashboard",
        "security-policy",
        "text-jobs",
    }
)

_CONFIGURATION_FIELDS = {
    "batch-manifest-calculator": {"discount", "line_order", "money"},
    "build-matrix": {"accelerators", "artifact_suffixes", "toolchains"},
    "content-cache": {"digest", "encoding", "storage"},
    "critical-path-scheduler": {
        "algorithm",
        "duration_bounds",
        "maximum_tasks",
        "ordering",
    },
    "dependency-planner": {
        "algorithm",
        "dependency_field",
        "duration_bounds",
        "duration_field",
        "maximum_tasks",
        "ordering",
        "parallelism",
    },
    "framework-compatibility-readiness-command": {
        "expected_framework_version",
        "operation",
        "required_skill_ids",
    },
    "greeting-summary": {"metrics", "recipient_id"},
    "integer-statistics": {"metrics", "numeric_type"},
    "invoice": {"currency_storage", "discount_unit", "rounding"},
    "ledger-workbench": {
        "amount_storage",
        "category_ordering",
        "largest_debit_tie_break",
        "runtime_dependencies",
    },
    "model-router": {"data_egress", "required_locality", "selection"},
    "publication-manifest": {"digest", "encoding", "manifest_encoding"},
    "release-risk-dashboard": {
        "backend",
        "frontend",
        "process_protocol",
        "risk_arithmetic",
        "service_ordering",
    },
    "security-policy": {"block_at", "severity_order", "yolo_warning"},
    "text-jobs": {"digest", "digest_prefix_length", "encoding", "members"},
}


def _text_sequence(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item for item in value)
        and len(set(value)) == len(value)
    )


def _text_mapping(value: object) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(key, str) and key and isinstance(item, str)
            for key, item in value.items()
        )
    )


def _validate_configuration(kind: str, configuration: dict[str, object]) -> None:
    if set(configuration) != _CONFIGURATION_FIELDS[kind]:
        raise PortableApplicationError(
            f"{kind} configuration fields do not match the schema"
        )
    valid = True
    if kind == "batch-manifest-calculator":
        valid = configuration == {
            "discount": "basis-points-floor",
            "line_order": "sku-codepoint-ascending",
            "money": "integer-cents",
        }
    elif kind == "build-matrix":
        suffixes = configuration["artifact_suffixes"]
        valid = (
            _text_mapping(suffixes)
            and set(suffixes) == {"linux", "macos", "windows"}
            and _text_sequence(configuration["accelerators"])
            and _text_mapping(configuration["toolchains"])
        )
    elif kind == "content-cache":
        valid = configuration == {
            "digest": "sha256",
            "encoding": "utf-8",
            "storage": "host-temporary-directory",
        }
    elif kind == "critical-path-scheduler":
        valid = configuration == {
            "algorithm": "critical-path-method",
            "duration_bounds": {
                "maximum": 1_000_000,
                "maximum_total": 2_147_483_647,
                "minimum": 1,
            },
            "maximum_tasks": 512,
            "ordering": "ascii-task-id-ascending",
        }
    elif kind == "dependency-planner":
        valid = configuration == {
            "algorithm": "critical-path-method",
            "dependency_field": "depends_on",
            "duration_bounds": {
                "maximum": 1_000_000,
                "maximum_total": 2_147_483_647,
                "minimum": 1,
            },
            "duration_field": "duration_minutes",
            "maximum_tasks": 512,
            "ordering": "ascii-task-id-ascending",
            "parallelism": "unlimited",
        }
    elif kind == "framework-compatibility-readiness-command":
        valid = (
            isinstance(configuration["expected_framework_version"], str)
            and bool(configuration["expected_framework_version"])
            and configuration["operation"] == "framework-compatibility-readiness"
            and _text_sequence(configuration["required_skill_ids"])
        )
    elif kind == "greeting-summary":
        valid = configuration == {
            "metrics": ["message_count", "word_count"],
            "recipient_id": "unicode-alphanumeric-kebab",
        }
    elif kind == "integer-statistics":
        valid = configuration == {
            "metrics": [
                "count",
                "total",
                "minimum",
                "maximum",
                "mean",
                "median",
            ],
            "numeric_type": "integer",
        }
    elif kind == "invoice":
        valid = configuration == {
            "currency_storage": "integer-cents",
            "discount_unit": "basis-points",
            "rounding": "half-up",
        }
    elif kind == "ledger-workbench":
        valid = configuration == {
            "amount_storage": "integer-cents",
            "category_ordering": "ascii-ascending",
            "largest_debit_tie_break": "ascii-transaction-id-ascending",
            "runtime_dependencies": "node-built-ins-only",
        }
    elif kind == "model-router":
        valid = (
            isinstance(configuration["required_locality"], str)
            and bool(configuration["required_locality"])
            and isinstance(configuration["data_egress"], str)
            and bool(configuration["data_egress"])
            and configuration["selection"] == "first-compatible"
        )
    elif kind == "publication-manifest":
        valid = configuration == {
            "digest": "sha256",
            "encoding": "utf-8",
            "manifest_encoding": "canonical-json",
        }
    elif kind == "release-risk-dashboard":
        valid = configuration == {
            "backend": "rust-standard-library",
            "frontend": "node-built-ins-only",
            "process_protocol": "host-observed-two-stage-canonical-json",
            "risk_arithmetic": "bounded-integers",
            "service_ordering": "risk-descending-then-ascii-name",
        }
    elif kind == "security-policy":
        severity = configuration["severity_order"]
        valid = (
            _text_sequence(severity)
            and "informational" in severity
            and configuration["block_at"] in severity
            and isinstance(configuration["yolo_warning"], str)
            and bool(configuration["yolo_warning"])
        )
    elif kind == "text-jobs":
        prefix = configuration["digest_prefix_length"]
        valid = (
            configuration["members"] == ["api", "worker"]
            and configuration["digest"] == "sha256"
            and configuration["encoding"] == "utf-8"
            and isinstance(prefix, int)
            and not isinstance(prefix, bool)
            and 1 <= prefix <= 64
        )
    if not valid:
        raise PortableApplicationError(f"{kind} configuration is unsupported")


def validate_portable_application(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Return the canonical specification after strict structural validation."""

    required = {
        "schema",
        "application_id",
        "kind",
        "entrypoint",
        "configuration",
    }
    optional = {"models"}
    if (
        not isinstance(value, Mapping)
        or not required <= set(value)
        or set(value) - required - optional
    ):
        raise PortableApplicationError(
            "portable application fields do not match schema"
        )
    application = dict(value)
    if application.get("schema") != PORTABLE_APPLICATION_SCHEMA:
        raise PortableApplicationError("portable application schema is unsupported")
    if (
        not isinstance(application.get("application_id"), str)
        or not application["application_id"]
        or application.get("kind") not in _SUPPORTED_KINDS
        or application.get("entrypoint") != "run"
        or not isinstance(application.get("configuration"), Mapping)
    ):
        raise PortableApplicationError("portable application contract is invalid")
    models = application.get("models", {})
    if not isinstance(models, Mapping) or any(
        key not in _CODING_CLIS for key in models
    ):
        raise PortableApplicationError("portable application coding models are invalid")
    try:
        for key, model in models.items():
            source_cache_model_selector(
                model,
                path=f"portable_application.models[{key!r}]",
            )
    except ContractValidationError as exc:
        raise PortableApplicationError(
            "portable application coding models are invalid"
        ) from exc
    try:
        normalized = json.loads(canonical_json_bytes(application))
    except (TypeError, ValueError) as exc:
        raise PortableApplicationError(
            "portable application is not canonical JSON"
        ) from exc
    kind = normalized["kind"]
    configuration = normalized["configuration"]
    assert isinstance(kind, str) and isinstance(configuration, dict)
    _validate_configuration(kind, configuration)
    return normalized


def portable_source_entrypoint(language: str) -> str:
    """Resolve the concrete entrypoint contributed by one language Flavor."""

    try:
        return _SOURCE_ENTRYPOINTS[language]
    except KeyError as exc:
        raise PortableApplicationError(
            f"unsupported implementation language: {language!r}"
        ) from exc


__all__ = [
    "PORTABLE_APPLICATION_SCHEMA",
    "SUPPORTED_IMPLEMENTATION_LANGUAGES",
    "PortableApplicationError",
    "portable_source_entrypoint",
    "validate_portable_application",
]
