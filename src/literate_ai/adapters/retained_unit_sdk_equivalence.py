"""Compare independently verified SDK epochs without reusing either grant.

Only execution authorization/result provenance and cache records embedding that
admission receive explicit stable counterparts. Source indexing, resolver, pinned
snapshot, recipe, placement and complete native graphs must remain exact.
"""

import json

from literate_ai.contracts import canonical_identity

_EPOCH_PROPERTIES = {
    "literate-ai:native-sdk-input",
    "literate-ai:native-sdk-source-admission",
    "literate-ai:native-sdk-source-resolution",
}


def retained_unit_sdk_context(
    run, inputs, roots, references, *, maximum_memory_bytes, maximum_steps
):
    """Pure comparison documents; both full epoch roots remain separately bound."""
    memory = 0
    steps = maximum_steps
    if (
        type(maximum_memory_bytes) is not int
        or maximum_memory_bytes <= 0
        or type(steps) is not int
        or steps <= 0
    ):
        raise ValueError("retained.unit.sdk-comparison-memory-exceeded")

    def reserve(amount):
        nonlocal memory, steps
        steps -= 1
        if amount < 0 or steps < 0 or memory + amount > maximum_memory_bytes:
            raise ValueError("retained.unit.sdk-comparison-budget-exceeded")
        memory += amount

    placements = {}
    for _platform, _root, rows in roots:
        for prefix, identity in rows:
            reserve(0)
            known = placements.get(identity)
            if known is None:
                reserve(1024)
                placements[identity] = known = set()
            if prefix not in known:
                reserve(1024 + 8 * len(prefix.encode("utf-8")))
                known.add(prefix)
    result = []
    keys = set()

    def document(identity):
        reference = references.get(identity.uri)
        if reference is None:
            raise ValueError("retained.unit.sdk-comparison-record-missing")
        reserve(32 * reference.size + 1024)
        return json.loads(inputs.store.get_bytes(reference))

    for dependency in run.resolved_bom.sdk_dependencies:
        reserve(
            32 * len(dependency._components_json) + 512 * len(dependency.edges) + 4096
        )
        resolution = dependency.repository_resolution
        cache = document(resolution.cache_record_identity)
        admission = dict(cache["admission"])
        if canonical_identity(admission) != resolution.admission_identity:
            raise ValueError("retained.unit.sdk-comparison-admission-differs")
        for field in ("authorization", "build_result"):
            admission.pop(field)
        stable_admission = canonical_identity(
            {
                "schema": "literate-ai/retained-unit-stable-sdk-admission@1",
                "source": admission,
            }
        )
        cache = {
            **cache,
            "admission": admission,
            "admission_identity": stable_admission.to_dict(),
        }
        stable_cache = canonical_identity(
            {
                "schema": "literate-ai/retained-unit-stable-sdk-cache@1",
                "record": cache,
            }
        )
        stable_resolution = {
            **resolution.to_dict(),
            "admission_identity": stable_admission.to_dict(),
            "cache_record_identity": stable_cache.to_dict(),
        }
        resolution_identity = canonical_identity(stable_resolution)
        components = dependency.observation.components
        original_ref = "urn:literate-ai:native-sdk:" + dependency.input_identity.digest
        packages = [value for value in components if value["bom-ref"] == original_ref]
        if len(packages) != 1:
            raise ValueError("retained.unit.sdk-comparison-exact-package-required")
        package = packages[0]
        properties = package["properties"]
        expected = {
            "literate-ai:native-sdk-input": dependency.input_identity.uri,
            "literate-ai:native-sdk-source-admission": (
                resolution.admission_identity.uri
            ),
            "literate-ai:native-sdk-source-resolution": resolution.identity.uri,
        }
        for name, identity in expected.items():
            if [value["value"] for value in properties if value["name"] == name] != [
                identity
            ]:
                raise ValueError("retained.unit.sdk-comparison-epoch-property-differs")
        prefixes = sorted(placements.get(dependency.input_identity.uri, ()))
        if not prefixes:
            raise ValueError("retained.unit.sdk-comparison-verified-placement-required")
        package_body = {
            **{key: value for key, value in package.items() if key != "bom-ref"},
            "properties": [
                value for value in properties if value["name"] not in _EPOCH_PROPERTIES
            ],
        }
        key = canonical_identity(
            {
                "schema": "literate-ai/retained-unit-stable-sdk-input@1",
                "package": package_body,
                "placements": prefixes,
                "source_resolution": stable_resolution,
            }
        )
        if key in keys:
            raise ValueError("retained.unit.sdk-comparison-key-collision")
        keys.add(key)
        replacement = "urn:literate-ai:retained-unit-stable-sdk:" + key.digest
        if any(value["bom-ref"] == replacement for value in components):
            raise ValueError("retained.unit.sdk-comparison-reference-collision")
        values = {
            "literate-ai:native-sdk-input": key.uri,
            "literate-ai:native-sdk-source-admission": stable_admission.uri,
            "literate-ai:native-sdk-source-resolution": resolution_identity.uri,
        }
        projected_package = {
            **package,
            "bom-ref": replacement,
            "properties": [
                {"name": value["name"], "value": values[value["name"]]}
                if value["name"] in values
                else value
                for value in properties
            ],
        }
        projected = [
            projected_package if value is package else value for value in components
        ]
        edges = sorted(
            [
                [
                    replacement if parent == original_ref else parent,
                    replacement if child == original_ref else child,
                ]
                for parent, child in dependency.edges
            ]
        )
        result.append(
            {
                "key": key.uri,
                "source_admission": admission,
                "source_cache": cache,
                "source_resolution": stable_resolution,
                "components": sorted(projected, key=lambda value: value["bom-ref"]),
                "edges": edges,
            }
        )
    return sorted(result, key=lambda value: value["key"])
