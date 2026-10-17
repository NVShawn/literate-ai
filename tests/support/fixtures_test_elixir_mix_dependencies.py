"""Shared Elixir/Mix test fixtures from test_elixir_mix_dependencies (PR #45)."""

from __future__ import annotations

from literate_ai.contracts.mix_projects import MixDependencyIntent, MixProjectIntent


def _project():
    return MixProjectIntent(
        "fixture",
        "1.0.0",
        "Mix dependency fixture",
        ("MIT",),
        (MixDependencyIntent("decimal", "== 2.3.0"),),
        links=(("Source", "https://example.invalid/fixture"),),
    )


def _entry(name="decimal", deps="[]", manager="mix", repo="hexpm"):
    return (
        f'"{name}": {{:hex, :{name}, "2.3.0", "' + "a" * 64 + '", '
        f'[:{manager}], {deps}, "{repo}", "' + "b" * 64 + '"}'
    )


def _lock(*entries):
    return ("%{\n" + ",\n".join(entries) + ",\n}\n").encode()
