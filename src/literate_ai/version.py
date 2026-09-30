"""Single checked-in authority for distribution and protocol compatibility."""

from __future__ import annotations

DISTRIBUTION_VERSION = "1.1.0"
EXPECTED_RELEASE_TAG = f"v{DISTRIBUTION_VERSION}"

SCHEMA_CATALOG_RELEASES = {
    "v1": "0.1.1",
    "v2": DISTRIBUTION_VERSION,
}

PROJECT_PROTOCOL_SCHEMA = "urn:literate-ai:schema:v2:project-definition"
LIFECYCLE_DRIVER_SCHEMA = "urn:literate-ai:schema:v2:project-lifecycle-driver"
TEST_SUITE_POLICY_SCHEMA = "urn:literate-ai:schema:v1:project-test-receipt-policy"
SUPPORTED_COMPONENT_CONTRACT_SCHEMAS = frozenset(
    {
        "urn:literate-ai:schema:v1:component-definition",
        "urn:literate-ai:schema:v2:component-definition",
    }
)

__all__ = [
    "DISTRIBUTION_VERSION",
    "EXPECTED_RELEASE_TAG",
    "LIFECYCLE_DRIVER_SCHEMA",
    "PROJECT_PROTOCOL_SCHEMA",
    "SCHEMA_CATALOG_RELEASES",
    "SUPPORTED_COMPONENT_CONTRACT_SCHEMAS",
    "TEST_SUITE_POLICY_SCHEMA",
]
