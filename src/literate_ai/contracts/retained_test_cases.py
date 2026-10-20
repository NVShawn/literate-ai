"""Shared bounded case names for original-authoritative retained tests."""

from ._validation import fail


def validate_retained_test_cases(values, maximum_cases=100000):
    if (
        type(maximum_cases) is not int
        or maximum_cases < 1
        or not isinstance(values, tuple)
        or len(values) > maximum_cases
        or any(
            not isinstance(v, str)
            or not v
            or len(v) > 1024
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in v)
            for v in values
        )
        or values != tuple(sorted(set(values)))
    ):
        fail("retained.tests.cases", "requires bounded, sorted, unique case names")
