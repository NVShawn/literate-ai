"""Rebind observed FreeBSD loader contexts to a portable SDK dependency view."""

from __future__ import annotations

import json
from collections.abc import Mapping

from literate_ai.contracts import canonical_identity, canonical_json_bytes

_STATE = "literate-ai:freebsd-loader-state"
_FILTEE = "literate-ai:freebsd-filtee"


def project_freebsd_loader_context(
    component: Mapping[str, object],
    *,
    refs: Mapping[str, str],
    native_refs: frozenset[str],
    root_prefix: str,
) -> dict[str, object]:
    def fail():
        raise ValueError("SDK FreeBSD loader context is invalid or changed")

    properties = component.get("properties", [])
    states = [p["value"] for p in properties if p["name"] == _STATE]
    if len(states) != 1 or len(states[0].encode("utf-8")) > 16 * 1024 * 1024:
        fail()
    try:
        original = json.loads(states[0])
        if canonical_json_bytes(original).decode("utf-8") != states[0]:
            fail()
    except (ValueError, TypeError, RecursionError):
        fail()
    if not isinstance(original, dict) or set(original) != {
        "loader",
        "paths",
        "filtees",
    }:
        fail()
    identity = canonical_identity(original)
    if (
        component.get("type") != "data"
        or component.get("name") != "freebsd-loader-configuration"
        or component.get("bom-ref")
        != "urn:literate-ai:freebsd-loader-state:" + identity.digest
        or component.get("version") != identity.digest
        or component.get("hashes") != [{"alg": "SHA-256", "content": identity.digest}]
    ):
        fail()
    records = original["filtees"]
    if not isinstance(records, list) or len(records) > 100_000:
        fail()
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"image", "program", "kind", "name", "target", "loading"}
            or any(not isinstance(value, str) for value in record.values())
            or record["kind"] not in {"filter", "auxiliary"}
            or record["loading"] != "potential"
            or not record["name"]
            or any(
                record[key] not in native_refs for key in ("image", "program", "target")
            )
        ):
            fail()
    if [p["value"] for p in properties if p["name"] == _FILTEE] != [
        canonical_json_bytes(record).decode("utf-8") for record in records
    ]:
        fail()

    def portable(value, depth=0):
        if depth > 64:
            fail()
        if isinstance(value, str):
            return refs.get(value, value.replace(root_prefix, "sdk:///"))
        if isinstance(value, list):
            return [portable(item, depth + 1) for item in value]
        if isinstance(value, dict):
            return {key: portable(item, depth + 1) for key, item in value.items()}
        return value

    projected = portable(original)
    projected["schema"] = "literate-ai/native-sdk-freebsd-loader-context@1"
    projected["filtees"].sort(key=canonical_json_bytes)
    identity = canonical_identity(projected)
    result = dict(component)
    result.update(
        {
            "bom-ref": "urn:literate-ai:native-sdk-loader-context:" + identity.digest,
            "version": identity.digest,
            "hashes": [{"alg": "SHA-256", "content": identity.digest}],
            "properties": [
                *(p for p in properties if p["name"] not in {_STATE, _FILTEE}),
                {
                    "name": _STATE,
                    "value": canonical_json_bytes(projected).decode("utf-8"),
                },
                *(
                    {
                        "name": _FILTEE,
                        "value": canonical_json_bytes(record).decode("utf-8"),
                    }
                    for record in projected["filtees"]
                ),
            ],
        }
    )
    return result
