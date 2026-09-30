"""Policy-driven Component and Flavor composition."""

from .components import (
    ComponentComposer,
    ComponentComposition,
    CompositionError,
    ProviderCandidateDecision,
    RequirementResolutionDecision,
)
from .flavors import FlavorResolution, FlavorResolver
from .policy import (
    ExplicitFlavorSelectionPolicy,
    FlavorSlotSelectionPolicy,
    PreferredSelectionPolicy,
    SelectionError,
    SelectionPolicy,
    UniqueSelectionPolicy,
)
from .versions import SemanticVersion, version_satisfies

__all__ = [
    "ComponentComposer",
    "ComponentComposition",
    "CompositionError",
    "ExplicitFlavorSelectionPolicy",
    "FlavorSlotSelectionPolicy",
    "FlavorResolution",
    "FlavorResolver",
    "PreferredSelectionPolicy",
    "ProviderCandidateDecision",
    "RequirementResolutionDecision",
    "SelectionError",
    "SelectionPolicy",
    "SemanticVersion",
    "UniqueSelectionPolicy",
    "version_satisfies",
]
