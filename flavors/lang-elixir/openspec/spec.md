# Elixir implementation Flavor

### Requirement: Portable Elixir implementation

When `implementation.language-ecosystem=elixir` is selected, the application SHALL
provide `source/main.exs` and run on Elixir 1.18 or later with Erlang/OTP 27 or later.
It SHALL use only the Elixir and Erlang standard libraries, including the built-in
`JSON` module. It SHALL accept one complete UTF-8 JSON arguments array from
`System.argv()` and emit one deterministic JSON result followed by a newline.
It SHALL keep helpers and native behavior tests beneath `source/`, load them by
paths relative to `__DIR__`, and require no Mix project, Hex packages or network fetch. When a selected
build-system profile requires a single-file export, it SHALL assemble helpers and
native tests into one self-contained `.exs` script, preserving the same modes.

#### Scenario: Elixir application executes from the artifact

- **WHEN** the selected Elixir runtime invokes the copied application tree after the generation workspace is unavailable
- **THEN** the application implements the Component acceptance contract without reading the former workspace

### Requirement: Elixir build and test modes

The language-native build SHALL parse every `.ex` and `.exs` source file without
evaluating application code and publish the complete source tree only after parsing
succeeds. The application SHALL implement `--litai-test` and `--litai-smoke` before
product argument parsing, delegating native behavior tests to
`source/tests/litai_test.exs` without reading the generated test manifest.

#### Scenario: Invalid Elixir source refuses publication

- **WHEN** any generated Elixir source file contains a syntax error
- **THEN** the build fails and does not publish a runnable artifact
