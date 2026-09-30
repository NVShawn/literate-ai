# Repository sample harness

This directory is test infrastructure, not Component authoring authority. A product
sample is designed to be understood and forked from its `component.md`; the files here
exist only so this repository can independently exercise known invocations and compare
the generated binaries with withheld expected results.

Each primary sample's versioned `sample.json` binds the exact current Component
authoring identity, self-rooted specification identity, and value-free execution
interface identity. `acceptance/execution.json` supplies fixed invocation vectors and a
closed result shape. `acceptance/oracle.json` separately supplies expected values and is
content-pinned by the harness manifest. Verifier-labeled files are forbidden from the
coding-agent input closure.

`invoice-service` and `money-calculation` are child Components of the composable service
sample, so they have execution/oracle fixtures but no top-level sample-selection
manifest. The self-hosting directory also carries repository-only conformance fixtures.

When adapting a sample into an application, copy and edit the Component specification.
Copy this harness only when adding or changing Literate AI's own conformance coverage.
