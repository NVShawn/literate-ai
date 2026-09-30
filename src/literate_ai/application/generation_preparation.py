"""Public application service for preparing one locked generation operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from literate_ai.contracts.identity import ContentIdentity

CatalogT = TypeVar("CatalogT")
AuthorityT = TypeVar("AuthorityT")
RecipeT = TypeVar("RecipeT")
PlanT = TypeVar("PlanT")


@dataclass(frozen=True, slots=True)
class GenerationPreparationRequest:
    """Provider-neutral selection inputs for one generation preparation."""

    component_root: Path
    target_name: str
    flavor_selectors: tuple[str, ...] = ()
    flavor_roots: tuple[Path, ...] = ()
    recipe_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.component_root, Path):
            raise TypeError("component_root must be a Path")
        if not self.target_name:
            raise ValueError("target_name must not be empty")
        if any(not item for item in self.flavor_selectors):
            raise ValueError("flavor selectors must not be empty")
        if any(not isinstance(item, Path) for item in self.flavor_roots):
            raise TypeError("flavor roots must be Paths")
        if self.recipe_id is not None and not self.recipe_id:
            raise ValueError("recipe_id must be non-empty when supplied")


@dataclass(frozen=True, slots=True)
class GenerationPreparation(
    Generic[CatalogT, AuthorityT, RecipeT],
):
    """The exact catalog, resolved authority, and recipe produced from a request."""

    request: GenerationPreparationRequest
    catalog: CatalogT
    authority: AuthorityT
    recipe: RecipeT


class GenerationPreparationError(RuntimeError):
    """A required application-service capability was not configured."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class GenerationPreparationService(
    Generic[CatalogT, AuthorityT, RecipeT, PlanT],
):
    """Coordinate the complete pre-generation authority workflow.

    Ports remain use-case-local until a second application service needs them, as
    required by ADR 0004.  Concrete filesystem, model, and presentation adapters are
    injected callables; this module depends only on neutral contracts.
    """

    def __init__(
        self,
        *,
        catalog_loader: Callable[[GenerationPreparationRequest], CatalogT]
        | None = None,
        lock_resolver: Callable[[CatalogT, GenerationPreparationRequest], AuthorityT]
        | None = None,
        recipe_creator: Callable[[AuthorityT, GenerationPreparationRequest], RecipeT]
        | None = None,
        execution_planner: Callable[[AuthorityT, RecipeT], PlanT] | None = None,
        authority_reviewer: Callable[[AuthorityT, RecipeT], ContentIdentity | None]
        | None = None,
        authority_guard: Callable[[AuthorityT], None] | None = None,
    ) -> None:
        self._catalog_loader = catalog_loader
        self._lock_resolver = lock_resolver
        self._recipe_creator = recipe_creator
        self._execution_planner = execution_planner
        self._authority_reviewer = authority_reviewer
        self._authority_guard = authority_guard

    def prepare(
        self, request: GenerationPreparationRequest
    ) -> GenerationPreparation[CatalogT, AuthorityT, RecipeT]:
        """Load, resolve, and create a recipe with drift checks at every boundary."""

        if (
            self._catalog_loader is None
            or self._lock_resolver is None
            or self._recipe_creator is None
        ):
            raise GenerationPreparationError(
                "generation_preparation.prepare_unconfigured",
                "catalog loading, lock resolution, and recipe creation are required",
            )
        catalog = self._catalog_loader(request)
        authority = self._lock_resolver(catalog, request)
        self._guard(authority)
        recipe = self._recipe_creator(authority, request)
        self._guard(authority)
        return GenerationPreparation(request, catalog, authority, recipe)

    def plan(self, authority: AuthorityT, recipe: RecipeT) -> PlanT:
        """Compile an execution plan against still-current resolved authority."""

        if self._execution_planner is None:
            raise GenerationPreparationError(
                "generation_preparation.planner_unconfigured",
                "execution planning is not configured",
            )
        self._guard(authority)
        result = self._execution_planner(authority, recipe)
        self._guard(authority)
        return result

    def review(self, authority: AuthorityT, recipe: RecipeT) -> ContentIdentity | None:
        """Obtain explicit authority review without treating absence as acceptance."""

        if self._authority_reviewer is None:
            raise GenerationPreparationError(
                "generation_preparation.reviewer_unconfigured",
                "authority review is not configured",
            )
        self._guard(authority)
        result = self._authority_reviewer(authority, recipe)
        if result is not None and not isinstance(result, ContentIdentity):
            raise GenerationPreparationError(
                "generation_preparation.review_invalid",
                "authority review must return an exact ContentIdentity or None",
            )
        self._guard(authority)
        return result

    def _guard(self, authority: AuthorityT) -> None:
        if self._authority_guard is not None:
            self._authority_guard(authority)


__all__ = [
    "GenerationPreparation",
    "GenerationPreparationError",
    "GenerationPreparationRequest",
    "GenerationPreparationService",
]
