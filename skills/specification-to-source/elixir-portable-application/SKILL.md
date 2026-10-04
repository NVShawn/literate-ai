---
name: "elixir-portable-application"
description: "Elixir portable JSON application. Use for Literate AI workflow tasks."
metadata:
  author: "Literate AI maintainers <literate-ai-maintainers@users.noreply.github.com>"
schema: "urn:literate-ai:schema:v1:specification-to-source-skill"
skill_id: "elixir-portable-application"
version: "1.0.0"
title: "Elixir portable JSON application"
stages:
  - "generate"
dependencies:
  - schema: "urn:literate-ai:schema:v1:skill-reference"
    skill_id: "portable-application-implementation"
    version: "1.8.3"
    identity:
      schema: "urn:literate-ai:schema:v1:content-identity"
      algorithm: "sha256"
      digest: "3d393c054d241eda78df8789144e75476d3131ae1b2e404fa33c6b02ef66d4af"
limitations:
  - "Do not invoke Mix, install Hex packages, use Mix.install, or fetch dependencies."
  - "Do not turn untrusted strings into atoms or evaluate request data as code."
  - "Do not emit ExUnit progress or compiler logs on the product JSON output stream."
trust: "repository-reviewed"
---
# Elixir portable JSON application

Implement the selected Elixir Flavor at `source/main.exs` with helper modules beneath
`source/`. Use Elixir 1.18+ and Erlang/OTP 27+ standard libraries. Decode the sole
`System.argv()` argument with `JSON.decode!`, require an arguments array, validate
its shapes and domain constraints, and call ordinary application functions with its
positional elements. Encode the complete specified result with `JSON.encode!` and
one newline. Preserve exact integers and represent fractional domain values as
specified decimal strings or scaled integers.

Resolve every `Code.require_file` helper and test path relative to `__DIR__`, so the
copied artifact works from any current directory after source custody is gone.
Dispatch `--litai-test` and `--litai-smoke` before JSON parsing. Put native behavior
cases in `source/tests/litai_test.exs`; assertions must call the real application
logic and fail nonzero. Report precisely the generated test result envelope required
by `portable-application-implementation`, without reading the manifest. When using
ExUnit, suppress its progress output and emit the envelope only after all cases pass.

Keep Mix/Hex/Phoenix and release assembly outside this dependency-free profile.
When Make or Bazel requires one export file, assemble a self-contained `.exs`
script containing its helper modules and native tests at `EXPORT_PATH` (Make) or
`run.exs` (Bazel). Run it with the selected Elixir tool; never ship a shell launcher
or leave helper paths pointing into the former generation workspace.
A selected build-system Flavor may supply a reviewed wrapper or faithful build;
it does not relax these dependency or source-custody requirements.
