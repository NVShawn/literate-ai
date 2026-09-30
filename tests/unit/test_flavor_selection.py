"""Provider-neutral ordered Flavor selection tests."""

from __future__ import annotations

import unittest

from literate_ai.application.flavor_selection import (
    FlavorSelectionError,
    apply_flavor_selectors,
)
from literate_ai.contracts import ContractValidationError, FlavorSelectionCandidate


def _candidate(
    flavor_id: str,
    *,
    axis: str = "implementation.language-ecosystem",
    value: str | None = None,
    conflicts: tuple[str, ...] = (),
    coordinate_uri: str | None = None,
    slot_ids: tuple[str, ...] = (),
) -> FlavorSelectionCandidate:
    return FlavorSelectionCandidate(
        candidate_identity=f"candidate:{flavor_id}",
        flavor_id=flavor_id,
        axis=axis,
        value=flavor_id if value is None else value,
        conflicts=conflicts,
        coordinate_uri=coordinate_uri,
        slot_ids=slot_ids,
    )


class FlavorSelectionCandidateTests(unittest.TestCase):
    def test_slot_bindings_are_canonical(self) -> None:
        with self.assertRaises(ContractValidationError):
            _candidate("rust", slot_ids=("worker", "backend"))


class OrderedFlavorSelectionTests(unittest.TestCase):
    def test_explicit_selector_replaces_an_initial_preference(self) -> None:
        python = _candidate("python")
        rust = _candidate("rust")

        selected = apply_flavor_selectors(
            (python, rust),
            ("+rust",),
            initial=(python,),
            initial_is_preferences=True,
        )

        self.assertEqual(selected, (rust,))

    def test_one_candidate_can_bind_and_remove_multiple_slots_in_order(self) -> None:
        rust = _candidate("rust")
        slots = {
            "backend": "implementation.language-ecosystem",
            "worker": "implementation.language-ecosystem",
        }

        selected = apply_flavor_selectors(
            (rust,),
            ("+worker:rust", "+backend:rust", "-worker:rust"),
            slot_axes=slots,
        )

        self.assertEqual(selected[0].slot_ids, ("backend",))

    def test_ambiguous_alias_fails_with_provider_neutral_error(self) -> None:
        first = _candidate("python-a", value="python")
        second = _candidate("python-b", value="python")

        with self.assertRaises(FlavorSelectionError) as raised:
            apply_flavor_selectors((first, second), ("+python",))

        self.assertEqual(
            raised.exception.code,
            "flavor_selection.unknown_flavor",
        )

    def test_conflicts_require_explicit_subtraction(self) -> None:
        linux = _candidate(
            "linux",
            axis="platform.os",
            conflicts=("flavor://samples/cpu",),
        )
        cpu = _candidate(
            "cpu",
            axis="accelerator",
            coordinate_uri="flavor://samples/cpu",
        )

        with self.assertRaises(FlavorSelectionError) as raised:
            apply_flavor_selectors((linux, cpu), ("+linux", "+cpu"))

        self.assertEqual(
            raised.exception.code,
            "flavor_selection.mutually_exclusive_flavors",
        )

    def test_bare_and_qualified_cpp_selectors_select_the_same_flavor(self) -> None:
        cpp = _candidate(
            "lang-cpp",
            value="cpp",
            coordinate_uri="flavor://literate-ai/lang-cpp",
        )

        bare = apply_flavor_selectors((cpp,), ("+cpp",))
        qualified = apply_flavor_selectors((cpp,), ("+lang-cpp",))

        self.assertEqual(bare, qualified)
        self.assertEqual(bare, (cpp,))

    def test_qualified_aliases_and_canonical_coordinates_select_exactly(self) -> None:
        swift = _candidate(
            "implementation-swift",
            value="swift",
            coordinate_uri="flavor://samples/implementation-swift",
        )

        for selector in (
            "+lang-swift",
            "+implementation.language-ecosystem=swift",
            "+flavor://samples/implementation-swift",
        ):
            with self.subTest(selector=selector):
                self.assertEqual(
                    apply_flavor_selectors((swift,), (selector,)), (swift,)
                )

    def test_package_axis_has_a_collision_free_qualified_alias(self) -> None:
        wheel = _candidate(
            "package-pip",
            axis="packaging",
            value="pip",
            coordinate_uri="flavor://literate-ai/package-pip",
        )

        for selector in (
            "+package-pip",
            "+packaging=pip",
            "+flavor://literate-ai/package-pip",
        ):
            with self.subTest(selector=selector):
                self.assertEqual(
                    apply_flavor_selectors((wheel,), (selector,)), (wheel,)
                )

    def test_compatible_package_flavors_compose_on_the_multi_value_axis(self) -> None:
        wheel = _candidate("package-pip", axis="packaging", value="pip")
        conan = _candidate("package-conan", axis="packaging", value="conan")

        selected = apply_flavor_selectors(
            (wheel, conan),
            ("+package-pip", "+package-conan"),
            multi_value_axes=frozenset({"packaging"}),
        )

        self.assertEqual(selected, (conan, wheel))

    def test_an_override_can_disambiguate_two_singleton_axis_preferences(self) -> None:
        # Two defaults sharing a singleton-cardinality axis previously raised
        # mutually_exclusive_flavors before any override was even read, so no
        # -flavor override could ever rescue it (see issue #46).
        macos = _candidate("os-macos", axis="platform.os")
        linux = _candidate("os-linux", axis="platform.os")

        selected = apply_flavor_selectors(
            (macos, linux),
            ("-os-linux",),
            initial=(macos, linux),
            initial_is_preferences=True,
        )

        self.assertEqual(selected, (macos,))

    def test_an_unresolved_singleton_axis_preference_conflict_still_fails(self) -> None:
        macos = _candidate("os-macos", axis="platform.os")
        linux = _candidate("os-linux", axis="platform.os")

        with self.assertRaises(FlavorSelectionError) as raised:
            apply_flavor_selectors(
                (macos, linux),
                (),
                initial=(macos, linux),
                initial_is_preferences=True,
            )

        self.assertEqual(
            raised.exception.code, "flavor_selection.mutually_exclusive_flavors"
        )

    def test_ambiguous_alias_reports_canonical_candidates(self) -> None:
        first = _candidate(
            "python-a", value="python", coordinate_uri="flavor://a/python"
        )
        second = _candidate(
            "python-b", value="python", coordinate_uri="flavor://b/python"
        )

        with self.assertRaises(FlavorSelectionError) as raised:
            apply_flavor_selectors((second, first), ("+python",))

        self.assertIn("flavor://a/python, flavor://b/python", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
