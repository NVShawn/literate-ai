# Loan Risk Gate Public Interface 1.0

The portable `run` entrypoint accepts exactly one positional argument: a JSON object
with exactly integer fields `age` and `income`. The callable surface SHALL preserve
that one-object boundary in every language. In Python the public callable is
`main(request)`, not `main(age, income)`.

The provider SHALL evaluate the locked UNIQUE DMN decision table exactly. Closed
intervals include both endpoints, a hyphen input cell is don't-care, and an input
outside every rule fails closed rather than receiving an invented default category.

The provider SHALL read the assembled `source/data/score-bands.json` overlay and select
the `id` of the unique object in its `bands` array whose closed `[min, max]` interval
contains `income`. It SHALL NOT regenerate or reorder the pinned bands. An income
outside every band or in more than one band fails closed.

The result is one JSON object with exactly these fields and no others:

| Field | Type | Meaning |
| --- | --- | --- |
| `age` | integer | Echo of the supplied age |
| `income` | integer | Echo of the supplied income |
| `risk_category` | string | DMN `RiskCategory` literal without surrounding quotes: `high`, `medium`, or `low` |
| `score_band` | string | Matching band `id`: `starter`, `established`, or `prime` |

For example, age 22 and income 25000 produce `high` and `starter`; age 40 and income
80000 produce `low` and `prime`. These examples illustrate the general table and band
rules and SHALL NOT be hard-coded as special cases.
