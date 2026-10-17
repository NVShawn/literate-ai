"""Exact public Hex pins used by Mix fixtures (no acquisition happens here).

Checksums were computed from the published repo.hex.pm tarballs: the outer
SHA-256 of ``<name>-<version>.tar`` and the inner checksum recorded in its
``CHECKSUM`` member. Mix acquisition is authorized only from such exact pins.
"""

from __future__ import annotations

from literate_ai.adapters.dependencies.mix_lock import MixLockedDependency
from literate_ai.adapters.dependencies.mix_resolution import (
    MIX_DEPENDENCY_PROPERTY,
    MIX_INNER_SHA256_PROPERTY,
    mix_dependency_property,
)

HEX_PINS = {
    "decimal": (
        "2.3.0",
        "3ad6255aa77b4a3c4f818171b12d237500e63525c2fd056699967a3e7ea20f62",
        "a4d66355cb29cb47c3cf30e71329e58361cfcb37c34235ef3bf1d7bf3773aeac",
        (),
    ),
    "jason": (
        "1.4.4",
        "b9226785a9aa77b6857ca22832cffa5d5011a667207eb2a0ad56adb5db443b8a",
        "c5eb0cab91f094599f94d55bc63409236a8ec69a21a67814529e8d5f6cc90b3b",
        (MixLockedDependency("decimal", "~> 1.0 or ~> 2.0", True),),
    ),
}


def exact_hex_component(name: str) -> dict[str, object]:
    """Source BOM component pinning one Hex package exactly."""
    version, inner, outer, dependencies = HEX_PINS[name]
    return {
        "type": "library",
        "bom-ref": "hex-" + name,
        "name": name,
        "version": version,
        "purl": f"pkg:hex/{name}@{version}",
        "isExternal": True,
        "hashes": [{"alg": "SHA-256", "content": outer}],
        "properties": [
            {"name": "literate-ai:dependency-kind", "value": "package"},
            {"name": "literate-ai:dependency-scope", "value": "runtime"},
            {"name": MIX_INNER_SHA256_PROPERTY, "value": inner},
            *(
                {
                    "name": MIX_DEPENDENCY_PROPERTY,
                    "value": mix_dependency_property(item),
                }
                for item in dependencies
            ),
        ],
    }
