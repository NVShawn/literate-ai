"""Pure validation for disposable tests emitted with a generated source tree."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from literate_ai.contracts import canonical_json_bytes

GENERATED_TEST_SUITE_SCHEMA = "urn:literate-ai:schema:v1:generated-test-suite"
GENERATED_TEST_SUITE_PATH = "source/tests/manifest.json"
MAJOR_REBUILD_GENERATION_MODE = "major-rebuild"
MINIMUM_GENERATED_TEST_CASES = 3
MAXIMUM_GENERATED_TEST_CASES = 256
REQUIRED_GENERATED_TEST_CATEGORIES = frozenset({"example", "boundary", "invariant"})

_SUITE_FIELDS = frozenset({"schema", "recipe_identity", "generation_mode", "cases"})
_CASE_FIELDS = frozenset(
    {
        "case_id",
        "category",
        "specification_refs",
        "arguments",
        "expected_result",
    }
)


class GeneratedTestSuiteError(ValueError):
    """A generated implementation-test suite violated its generation contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class GeneratedTestCase:
    """One black-box implementation case emitted from current specifications."""

    case_id: str
    category: str
    specification_refs: tuple[str, ...]
    arguments: tuple[object, ...]
    expected_result: object


@dataclass(frozen=True, slots=True)
class ValidatedGeneratedTestSuite:
    """Small immutable receipt for a validated generated test-suite document."""

    content_identity: str
    recipe_identity: str
    generation_mode: str
    case_ids: tuple[str, ...]
    categories: frozenset[str]
    cases: tuple[GeneratedTestCase, ...]


class _DuplicateObjectKey(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateObjectKey(key)
        value[key] = item
    return value


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-JSON numeric constant {value!r}")


def _decode_document(content: str | bytes) -> tuple[dict[str, Any], bytes]:
    if isinstance(content, bytes):
        encoded = content
        try:
            text = content.decode("utf-8")
        except UnicodeError as exc:
            raise GeneratedTestSuiteError(
                "generated_tests.invalid_json",
                "generated test suite must be UTF-8 JSON",
            ) from exc
    elif isinstance(content, str):
        text = content
        encoded = content.encode("utf-8")
    else:
        raise GeneratedTestSuiteError(
            "generated_tests.invalid_json",
            "generated test suite must be supplied as UTF-8 text or bytes",
        )
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (
        json.JSONDecodeError,
        _DuplicateObjectKey,
        RecursionError,
        ValueError,
    ) as exc:
        raise GeneratedTestSuiteError(
            "generated_tests.invalid_json",
            "generated test suite must be strict JSON without duplicate object keys",
        ) from exc
    if not isinstance(value, dict):
        raise GeneratedTestSuiteError(
            "generated_tests.invalid_fields",
            "generated test suite must be a JSON object",
        )
    return value, encoded


def _require_exact_fields(
    value: dict[str, Any], expected: frozenset[str], *, path: str
) -> None:
    actual = frozenset(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(missing))
    if unknown:
        details.append("unknown " + ", ".join(unknown))
    raise GeneratedTestSuiteError(
        "generated_tests.invalid_fields",
        f"{path} must have exact fields ({'; '.join(details)})",
    )


def _argument_identity(value: Any, *, path: str) -> bytes:
    if not isinstance(value, list):
        raise GeneratedTestSuiteError(
            "generated_tests.invalid_arguments",
            f"{path} must be a JSON array",
        )
    try:
        return canonical_json_bytes(value)
    except ValueError as exc:
        raise GeneratedTestSuiteError(
            "generated_tests.invalid_arguments",
            f"{path} must use portable canonical JSON values",
        ) from exc


def _value_signature(value: Any) -> object:
    if value is None:
        return {"type": "null"}
    if type(value) is bool:
        return {"type": "boolean"}
    if type(value) is int:
        return {"type": "integer"}
    if type(value) is float:
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array"}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {
                key: _value_signature(item) for key, item in sorted(value.items())
            },
        }
    raise GeneratedTestSuiteError(
        "generated_tests.invalid_arguments",
        "invocation signatures require portable JSON values",
    )


def generated_test_invocation_signature(arguments: Any) -> dict[str, object]:
    """Return a value-free callable signature for one JSON argument vector."""

    _argument_identity(arguments, path="invocation arguments")
    assert isinstance(arguments, list)
    return {
        "arity": len(arguments),
        "arguments": [_value_signature(item) for item in arguments],
    }


def _signature_identity(arguments: Any) -> bytes:
    return canonical_json_bytes(generated_test_invocation_signature(arguments))


def _result_matches_shape(value: Any, shape: Any) -> bool:
    if isinstance(shape, str):
        alternatives = shape.split("|")
        is_lower_hex = isinstance(value, str) and all(
            character in "0123456789abcdef" for character in value
        )
        matches = {
            "boolean": type(value) is bool,
            "integer": type(value) is int,
            "null": value is None,
            "number": type(value) in {int, float},
            "sha256-hex": is_lower_hex and len(value) == 64,
            "sha256-prefix-12": is_lower_hex and len(value) == 12,
            "string": type(value) is str,
        }
        return all(item in matches for item in alternatives) and any(
            matches[item] for item in alternatives
        )
    if isinstance(shape, list):
        return (
            len(shape) == 1
            and isinstance(value, list)
            and all(_result_matches_shape(item, shape[0]) for item in value)
        )
    if isinstance(shape, dict):
        return (
            isinstance(value, dict)
            and set(value) == set(shape)
            and all(
                _result_matches_shape(value[key], child) for key, child in shape.items()
            )
        )
    return False


def validate_generated_test_suite(
    content: str | bytes,
    *,
    recipe_identity: str,
    specification_references: Iterable[str],
    acceptance_arguments: Iterable[Any] = (),
    result_shape: object | None = None,
) -> ValidatedGeneratedTestSuite:
    """Validate one generated suite without reading or mutating external state.

    ``specification_references`` is the exact set of current, non-acceptance recipe
    documents. ``acceptance_arguments`` contains argument vectors visible only to
    the framework and is used solely to require coverage of every value-free
    callable signature. Generated and acceptance cases may independently converge
    on the same useful input; their authority and expected-result provenance remain
    separate. When supplied, ``result_shape`` requires complete generated
    expectations before any candidate can be built or cached.
    """

    value, encoded = _decode_document(content)
    _require_exact_fields(value, _SUITE_FIELDS, path="generated test suite")
    if value["schema"] != GENERATED_TEST_SUITE_SCHEMA:
        raise GeneratedTestSuiteError(
            "generated_tests.schema_mismatch",
            f"generated test suite schema must be {GENERATED_TEST_SUITE_SCHEMA!r}",
        )
    if value["recipe_identity"] != recipe_identity:
        raise GeneratedTestSuiteError(
            "generated_tests.recipe_identity_mismatch",
            "generated test suite recipe_identity does not match the current recipe",
        )
    if value["generation_mode"] != MAJOR_REBUILD_GENERATION_MODE:
        raise GeneratedTestSuiteError(
            "generated_tests.generation_mode_mismatch",
            "generated test suite generation_mode must be 'major-rebuild'",
        )
    cases = value["cases"]
    if not isinstance(cases, list) or not (
        MINIMUM_GENERATED_TEST_CASES <= len(cases) <= MAXIMUM_GENERATED_TEST_CASES
    ):
        raise GeneratedTestSuiteError(
            "generated_tests.case_count",
            "generated test suite must contain between 3 and 256 cases",
        )

    allowed_references = frozenset(specification_references)
    acceptance_vectors = tuple(acceptance_arguments)
    required_signatures = {_signature_identity(item) for item in acceptance_vectors}
    case_ids: list[str] = []
    categories: set[str] = set()
    argument_identities: set[bytes] = set()
    generated_signatures: set[bytes] = set()
    validated_cases: list[GeneratedTestCase] = []
    for index, case in enumerate(cases):
        path = f"generated test suite cases[{index}]"
        if not isinstance(case, dict):
            raise GeneratedTestSuiteError(
                "generated_tests.invalid_fields", f"{path} must be an object"
            )
        _require_exact_fields(case, _CASE_FIELDS, path=path)
        case_id = case["case_id"]
        if not isinstance(case_id, str) or not case_id.strip():
            raise GeneratedTestSuiteError(
                "generated_tests.invalid_case_id",
                f"{path}.case_id must be a non-empty string",
            )
        if case_id in case_ids:
            raise GeneratedTestSuiteError(
                "generated_tests.duplicate_case_id",
                f"generated test suite repeats case_id {case_id!r}",
            )
        case_ids.append(case_id)

        category = case["category"]
        if (
            not isinstance(category, str)
            or category not in REQUIRED_GENERATED_TEST_CATEGORIES
        ):
            raise GeneratedTestSuiteError(
                "generated_tests.invalid_category",
                f"{path}.category must be example, boundary, or invariant",
            )
        categories.add(category)

        references = case["specification_refs"]
        if (
            not isinstance(references, list)
            or not references
            or any(not isinstance(item, str) for item in references)
            or len(set(references)) != len(references)
        ):
            raise GeneratedTestSuiteError(
                "generated_tests.invalid_specification_refs",
                f"{path}.specification_refs must be a non-empty unique string list",
            )
        unavailable = tuple(
            reference for reference in references if reference not in allowed_references
        )
        if unavailable:
            raise GeneratedTestSuiteError(
                "generated_tests.invalid_specification_refs",
                f"{path}.specification_refs cites non-current or acceptance content: "
                + ", ".join(unavailable)
                + "; each entry must copy one of these exactly: "
                + ", ".join(sorted(allowed_references)),
            )

        argument_identity = _argument_identity(
            case["arguments"], path=f"{path}.arguments"
        )
        if argument_identity in argument_identities:
            raise GeneratedTestSuiteError(
                "generated_tests.duplicate_arguments",
                "generated test cases must use unique argument vectors",
            )
        argument_identities.add(argument_identity)
        generated_signatures.add(_signature_identity(case["arguments"]))
        if result_shape is not None and not _result_matches_shape(
            case["expected_result"], result_shape
        ):
            raise GeneratedTestSuiteError(
                "generated_tests.expected_result_shape_mismatch",
                f"{path}.expected_result does not match the execution result shape",
            )
        validated_cases.append(
            GeneratedTestCase(
                case_id=case_id,
                category=category,
                specification_refs=tuple(references),
                arguments=tuple(case["arguments"]),
                expected_result=case["expected_result"],
            )
        )

    missing_categories = REQUIRED_GENERATED_TEST_CATEGORIES - categories
    if missing_categories:
        raise GeneratedTestSuiteError(
            "generated_tests.missing_category",
            "generated test suite is missing required categories: "
            + ", ".join(sorted(missing_categories)),
        )
    missing_signatures = required_signatures - generated_signatures
    if missing_signatures:
        first_missing = min(missing_signatures).decode("utf-8")
        raise GeneratedTestSuiteError(
            "generated_tests.acceptance_signature_missing",
            "generated test cases must cover every value-free acceptance invocation "
            f"signature; missing value-free signature {first_missing[:2048]}",
        )
    return ValidatedGeneratedTestSuite(
        content_identity=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        recipe_identity=recipe_identity,
        generation_mode=MAJOR_REBUILD_GENERATION_MODE,
        case_ids=tuple(case_ids),
        categories=frozenset(categories),
        cases=tuple(validated_cases),
    )


__all__ = [
    "GENERATED_TEST_SUITE_PATH",
    "GENERATED_TEST_SUITE_SCHEMA",
    "MAJOR_REBUILD_GENERATION_MODE",
    "MAXIMUM_GENERATED_TEST_CASES",
    "MINIMUM_GENERATED_TEST_CASES",
    "REQUIRED_GENERATED_TEST_CATEGORIES",
    "GeneratedTestSuiteError",
    "GeneratedTestCase",
    "ValidatedGeneratedTestSuite",
    "generated_test_invocation_signature",
    "validate_generated_test_suite",
]
