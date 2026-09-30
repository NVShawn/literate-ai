from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

BACKEND_PARENT_SKILL_ID = "backend-application"

BACKEND_LANGUAGE_SKILLS: dict[str, str] = {
    "python": "python-service-application",
    "rust": "rust-service-application",
    "elixir": "elixir-service-application",
}

_ELIXIR_FLAVOR_ALIASES = frozenset(
    {
        "elixir",
        "lang-elixir",
        "flavor://literate-ai/lang-elixir",
    }
)
_ELIXIR_SKIP_REASON = "lang-elixir Flavor is not in the catalog"


@dataclass(frozen=True, slots=True)
class BackendEcosystemIndex:
    """Which back-end language chapters apply for one selected Flavor set."""

    injected: tuple[str, ...]
    omitted: tuple[str, ...]
    skips: tuple[tuple[str, str], ...]


def _catalog_has_elixir(catalog_flavor_ids: Collection[str]) -> bool:
    names = frozenset(catalog_flavor_ids)
    if names & _ELIXIR_FLAVOR_ALIASES:
        return True
    return any(item.rstrip("/").endswith("/lang-elixir") for item in names)


def index_backend_ecosystems(
    *,
    recipe_skill_ids: Collection[str],
    selected_language_targets: Collection[str],
    catalog_flavor_ids: Collection[str],
) -> BackendEcosystemIndex:
    """Index Python/Rust/Elixir back-end chapters off selected Flavors.

    Missing ``lang-elixir`` is a named skip, not a crash. Unselected language
    ecosystems are omitted. Selected Python or Rust Flavors inject the matching
    nested skill id. The parent skill itself is never injected or omitted here.
    """

    if BACKEND_PARENT_SKILL_ID not in frozenset(recipe_skill_ids):
        return BackendEcosystemIndex((), (), ())
    selected = frozenset(selected_language_targets)
    injected: list[str] = []
    omitted: list[str] = []
    skips: list[tuple[str, str]] = []
    elixir_present = _catalog_has_elixir(catalog_flavor_ids)
    for language, skill_id in BACKEND_LANGUAGE_SKILLS.items():
        if language == "elixir" and not elixir_present:
            skips.append((language, _ELIXIR_SKIP_REASON))
            continue
        if language in selected:
            injected.append(skill_id)
        else:
            omitted.append(skill_id)
    return BackendEcosystemIndex(tuple(injected), tuple(omitted), tuple(skips))
