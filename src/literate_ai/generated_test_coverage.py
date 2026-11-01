"""Value-free coverage authority derived from an exact public callable contract."""

from __future__ import annotations

from dataclasses import dataclass

from literate_ai.contracts import ContentIdentity, canonical_identity

PUBLIC_CALLABLE_CONTRACT_PATH = "interfaces/callable.json"
COVERAGE_REQUEST_SCHEMA = "literate-ai/generated-test-coverage-request@1"
PUBLIC_CALLABLE_SIGNATURE_SCHEMA = "literate-ai/public-callable-signature@1"
COVERAGE_AUTHORITY_SCHEMA = "literate-ai/generated-test-coverage-authority@1"
ZERO_ARGUMENT_COVERAGE_MODE = "exhaustive-zero-argument"


@dataclass(frozen=True, slots=True)
class GeneratedTestCoverageAuthority:
    """An established closed zero-argument interface, never a generated assertion.

    The caller owns the reviewed document and its current content identity. Parsing
    the signature proves its declared shape; it cannot confer review or file custody.
    No oracle answers or acceptance argument values enter this record.
    """

    public_interface_identity: str

    def __post_init__(self):
        ContentIdentity.parse_uri(self.public_interface_identity)

    @classmethod
    def from_public_signature(cls, signature, *, public_interface_identity: str):
        if (
            not isinstance(signature, dict)
            or set(signature) != {"schema", "arity", "optional", "variadic"}
            or signature["schema"] != PUBLIC_CALLABLE_SIGNATURE_SCHEMA
            or type(signature["arity"]) is not int
            or signature["arity"] != 0
            or signature["optional"] is not False
            or signature["variadic"] is not False
        ):
            raise ValueError("generated_tests.closed_zero_argument_signature_required")
        return cls(public_interface_identity)

    def to_dict(self):
        return {
            "schema": COVERAGE_AUTHORITY_SCHEMA,
            "mode": ZERO_ARGUMENT_COVERAGE_MODE,
            "public_interface_identity": self.public_interface_identity,
        }

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "mode", "public_interface_identity"}
            or value["schema"] != COVERAGE_AUTHORITY_SCHEMA
            or value["mode"] != ZERO_ARGUMENT_COVERAGE_MODE
        ):
            raise ValueError("generated_tests.coverage_authority_invalid")
        return cls(value["public_interface_identity"])

    @property
    def identity(self):
        return canonical_identity(self.to_dict())


def require_generated_coverage_capacity(authorities, *, minimum_test_count: int):
    """Check the aggregate distinct-input ceiling without lowering project policy.

    Ordinary domains retain the existing 256-case maximum. A singleton contributes
    exactly one case. Additional native assertions cannot pad manifest membership.
    """
    values = tuple(authorities)
    if any(
        item is not None and not isinstance(item, GeneratedTestCoverageAuthority)
        for item in values
    ):
        raise TypeError("generated coverage capacity requires typed authority")
    if type(minimum_test_count) is not int or minimum_test_count < 1:
        raise ValueError("generated_tests.minimum_test_count_invalid")
    if any(item is not None for item in values):
        maximum = sum(1 if item is not None else 256 for item in values)
        if minimum_test_count > maximum:
            raise ValueError(
                "generated_tests.project_minimum_incompatible: project minimum "
                f"{minimum_test_count} exceeds distinct-input coverage {maximum}; "
                "review the project policy or public interface before generation"
            )
