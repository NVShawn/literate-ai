from calculator import divide


def test_division_by_zero_contract_is_unsettled() -> None:
    assert divide(1, 0) == 0.0
    # A second retained test specification expects ZeroDivisionError.
