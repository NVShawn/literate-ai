"""Shared Elixir/Mix test fixtures from test_elixir_mix_projection (PR #45)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from literate_ai.contracts import (
    StandardMixCommandProfile,
)
from tests.support.fixtures_test_standard_command_projection import (
    _locked_snapshot,
)

_REPOSITORY = Path(__file__).resolve().parents[2]


def _mix_snapshot(
    root,
    *,
    language="elixir",
    platform="macos",
    required=None,
    multiple=False,
    generation_ready=False,
    library=False,
):
    flavor = root / "fixture-mix-flavor"
    (flavor / "openspec").mkdir(parents=True)
    content = (_REPOSITORY / "flavors/build-cargo/flavor.md").read_text()
    content = (
        content.replace("cargo", "mix")
        .replace("Cargo", "Mix")
        .replace("rust", "elixir")
        .replace("Rust", "Elixir")
    )
    content = re.sub(
        r"authoring_inputs:\n.*?contributions:",
        "authoring_inputs: []\ncontributions:",
        content,
        count=1,
        flags=re.DOTALL,
    )
    content = content.replace(
        "conflicts: []",
        '  - contribution_id: "hex-constraint"\n'
        '    kind: "toolchain"\n'
        '    merge_operator: "exact-singleton"\n'
        '    slot: "hex"\n'
        "    content:\n"
        '      kind: "toolchain-constraint"\n'
        '      uri: "hex.json"\n'
        "conflicts: []",
    )
    (flavor / "flavor.md").write_text(content, encoding="utf-8", newline="\n")
    (flavor / "openspec/spec.md").write_text(
        "# Test-owned Mix profile\n\n"
        "### Requirement: Declarative intent\n\n"
        "The build SHALL use declarative Mix intent.\n\n"
        "#### Scenario: Produce native artifacts\n\n"
        "- **WHEN** the authorized Mix build runs\n"
        "- **THEN** it retains native artifacts\n",
        encoding="utf-8",
        newline="\n",
    )
    (flavor / "standard-command-profile.json").write_text(
        json.dumps(
            StandardMixCommandProfile("mix", "hex", "source/mix-project.json").to_dict()
        ),
        encoding="utf-8",
    )
    constraint = {
        "schema": "urn:literate-ai:schema:v2:toolchain-constraint",
        "toolchain": "hex",
        "minimum_version": [2, 5],
        "maximum_exclusive_version": [3],
    }
    if required is not None:
        constraint["required_version"] = required
    (flavor / "hex.json").write_text(json.dumps(constraint), encoding="utf-8")
    return _locked_snapshot(
        root / "project",
        language=language,
        platform=platform,
        build_system="mix",
        test_build_flavor_root=flavor,
        duplicate_entrypoint=multiple,
        generation_ready=generation_ready,
        no_entrypoint=library,
    )
