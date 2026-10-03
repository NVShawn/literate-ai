"""Bounded public refusal diagnostics for unsuccessful worker observations."""

import json
import re

from literate_ai.contracts import canonical_json_bytes

_SCHEMA = "literate-ai/action-observation-failure@1"
_METRICS = {"bytes", "limit", "components", "edges", "component_bytes", "edge_bytes"}


def encode_observation_failure(code, metrics=None):
    """Emit symbolic codes and numeric sizes, never exception text or stderr."""
    value = {"schema": _SCHEMA, "code": code, "metrics": metrics or {}}
    content = canonical_json_bytes(value)
    if observation_failure_message(content) is None:
        return canonical_json_bytes(
            {
                "schema": _SCHEMA,
                "code": "action_capability.custody_unavailable",
                "metrics": {},
            }
        )
    return content


def observation_failure_message(content):
    """Accept only a small closed diagnostic; invalid and legacy replies stay opaque."""
    if not isinstance(content, bytes) or len(content) > 2048:
        return None
    try:
        value = json.loads(content)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "code", "metrics"}
            or value["schema"] != _SCHEMA
            or not isinstance(value["code"], str)
            or re.fullmatch(r"action_[a-z_]+\.[a-z][a-z0-9_.-]{0,95}", value["code"])
            is None
            or not isinstance(value["metrics"], dict)
            or not set(value["metrics"]).issubset(_METRICS)
            or any(
                type(n) is not int or not 0 <= n < 2**63
                for n in value["metrics"].values()
            )
            or canonical_json_bytes(value) != content
        ):
            return None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    details = ", ".join(
        f"{key}={number}" for key, number in sorted(value["metrics"].items())
    )
    return (
        "worker observation refused ("
        + value["code"]
        + (", " + details if details else "")
        + ")"
    )
