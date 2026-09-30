"""Application-service tests for locked generation preparation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
)
from literate_ai.application.generation_preparation import (
    GenerationPreparationError,
    GenerationPreparationRequest,
    GenerationPreparationService,
)
from literate_ai.contracts.identity import canonical_identity
from tests.unit.test_component_lock_planning import _fixture
from tests.unit.test_locked_generation_authority import (
    _SELECTORS,
    _TARGET,
    _write_lock,
)


class GenerationPreparationServiceTests(unittest.TestCase):
    def test_filesystem_adapter_split_matches_atomic_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            _write_lock(component, flavors)
            reader = FilesystemLockedGenerationAuthorityReader()

            catalog = reader.load_catalog(component, flavor_roots=(flavors,))
            split = reader.resolve(
                catalog,
                target_name=_TARGET,
                flavor_selectors=_SELECTORS,
            )
            atomic = reader.read(
                component,
                target_name=_TARGET,
                flavor_selectors=_SELECTORS,
                flavor_roots=(flavors,),
            )

            self.assertEqual(split.authority, atomic.authority)
            self.assertEqual(
                split.input_closure_identity, atomic.input_closure_identity
            )
            self.assertEqual(
                split.catalog_audit_identity, atomic.catalog_audit_identity
            )

    def test_complete_workflow_is_ordered_and_guards_every_authority_boundary(
        self,
    ) -> None:
        events: list[str] = []
        request = GenerationPreparationRequest(
            Path("components/example"),
            "host",
            ("+linux", "+rust"),
            (Path("flavors"),),
            "example-recipe",
        )
        catalog = object()
        authority = object()
        recipe = object()
        plan = object()
        review = canonical_identity("review")
        service = GenerationPreparationService(
            catalog_loader=lambda value: (
                events.append("catalog") or self.assertIs(value, request) or catalog
            ),
            lock_resolver=lambda selected, value: (
                events.append("lock")
                or self.assertIs(selected, catalog)
                or self.assertIs(value, request)
                or authority
            ),
            recipe_creator=lambda selected, value: (
                events.append("recipe")
                or self.assertIs(selected, authority)
                or self.assertIs(value, request)
                or recipe
            ),
            execution_planner=lambda selected, value: (
                events.append("plan")
                or self.assertIs(selected, authority)
                or self.assertIs(value, recipe)
                or plan
            ),
            authority_reviewer=lambda selected, value: (
                events.append("review")
                or self.assertIs(selected, authority)
                or self.assertIs(value, recipe)
                or review
            ),
            authority_guard=lambda selected: (
                events.append("guard") or self.assertIs(selected, authority)
            ),
        )

        prepared = service.prepare(request)
        self.assertIs(prepared.catalog, catalog)
        self.assertIs(prepared.authority, authority)
        self.assertIs(prepared.recipe, recipe)
        self.assertIs(service.plan(authority, recipe), plan)
        self.assertEqual(service.review(authority, recipe), review)
        self.assertEqual(
            events,
            [
                "catalog",
                "lock",
                "guard",
                "recipe",
                "guard",
                "guard",
                "plan",
                "guard",
                "guard",
                "review",
                "guard",
            ],
        )

    def test_unconfigured_operations_fail_closed_with_stable_codes(self) -> None:
        service = GenerationPreparationService()
        request = GenerationPreparationRequest(Path("component"), "host")
        operations = (
            (
                lambda: service.prepare(request),
                "generation_preparation.prepare_unconfigured",
            ),
            (
                lambda: service.plan(object(), object()),
                "generation_preparation.planner_unconfigured",
            ),
            (
                lambda: service.review(object(), object()),
                "generation_preparation.reviewer_unconfigured",
            ),
        )
        for operation, code in operations:
            with (
                self.subTest(code=code),
                self.assertRaises(GenerationPreparationError) as caught,
            ):
                operation()
            self.assertEqual(caught.exception.code, code)

    def test_review_must_return_exact_typed_identity(self) -> None:
        service = GenerationPreparationService(
            authority_reviewer=lambda _authority, _recipe: "sha256:not-typed"
        )
        with self.assertRaises(GenerationPreparationError) as caught:
            service.review(object(), object())
        self.assertEqual(caught.exception.code, "generation_preparation.review_invalid")

    def test_request_rejects_ambiguous_empty_selections(self) -> None:
        with self.assertRaises(ValueError):
            GenerationPreparationRequest(Path("component"), "")
        with self.assertRaises(ValueError):
            GenerationPreparationRequest(Path("component"), "host", ("",))
        with self.assertRaises(ValueError):
            GenerationPreparationRequest(Path("component"), "host", recipe_id="")


if __name__ == "__main__":
    unittest.main()
