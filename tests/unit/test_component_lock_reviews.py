"""Bounded large Component-lock review transaction tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from literate_ai.adapters.component_lock_reviews import (
    ComponentLockReviewError,
    ComponentLockReviewStore,
)
from literate_ai.adapters.component_locks import ComponentLockDifference
from literate_ai.cache_directories import BUILD_DIR_ENVIRONMENT, OBJ_DIR_ENVIRONMENT
from literate_ai.contracts.identity import canonical_json_bytes


def _differences(count: int = 513) -> tuple[ComponentLockDifference, ...]:
    return tuple(
        ComponentLockDifference(f"/nodes/{index}", "changed", index, index + 1)
        for index in range(count)
    )


def _binding(name: str = "candidate") -> dict[str, object]:
    return {
        "schema": "test/component-lock-review-binding@1",
        "component": "components/example",
        "candidate": name,
    }


@contextmanager
def _isolated_project() -> Iterator[Path]:
    """Bind cache roots to this temp project, not the xdist worker OBJ_DIR."""

    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        generated = project / "generated"
        objects = project / "_build"
        generated.mkdir()
        objects.mkdir()
        with mock.patch.dict(
            os.environ,
            {
                BUILD_DIR_ENVIRONMENT: str(generated),
                OBJ_DIR_ENVIRONMENT: str(objects),
            },
        ):
            yield project


class ComponentLockReviewStoreTests(unittest.TestCase):
    def test_pages_are_bounded_chained_restartable_and_explicitly_cleaned(self) -> None:
        with _isolated_project() as project:
            store = ComponentLockReviewStore(project)
            started = store.start(binding=_binding(), differences=_differences())
            first = started["next_page"]
            assert isinstance(first, dict)
            self.assertEqual(len(first["differences"]), 512)
            self.assertIsNone(first["previous_page_identity"])
            transaction = str(started["transaction_identity"])

            resumed = ComponentLockReviewStore(project).status(
                transaction,
                binding=_binding(),
                differences=_differences(),
            )
            self.assertEqual(resumed["next_page"], first)

            acknowledged = store.acknowledge(
                transaction,
                str(first["page_identity"]),
                binding=_binding(),
                differences=_differences(),
            )
            second = acknowledged["next_page"]
            assert isinstance(second, dict)
            self.assertEqual(len(second["differences"]), 1)
            self.assertEqual(
                second["previous_page_identity"],
                first["page_identity"],
            )
            completed = store.acknowledge(
                transaction,
                str(second["page_identity"]),
                binding=_binding(),
                differences=_differences(),
            )
            self.assertTrue(completed["ready"])
            self.assertIsNone(completed["next_page"])

            store.complete(transaction)
            with self.assertRaises(ComponentLockReviewError) as raised:
                store.status(
                    transaction,
                    binding=_binding(),
                    differences=_differences(),
                )
            self.assertEqual(raised.exception.code, "component_lock.review_missing")

    def test_acknowledgements_reject_skips_replays_and_cross_transaction_pages(
        self,
    ) -> None:
        with _isolated_project() as project:
            store = ComponentLockReviewStore(project)
            first_review = store.start(
                binding=_binding("first"),
                differences=_differences(),
            )
            second_review = store.start(
                binding=_binding("second"),
                differences=_differences(),
            )
            transaction = str(first_review["transaction_identity"])
            first_page = first_review["next_page"]
            second_page = second_review["next_page"]
            assert isinstance(first_page, dict) and isinstance(second_page, dict)

            for identity in ("sha256:" + "0" * 64, second_page["page_identity"]):
                with self.assertRaises(ComponentLockReviewError) as raised:
                    store.acknowledge(
                        transaction,
                        str(identity),
                        binding=_binding("first"),
                        differences=_differences(),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "component_lock.review_acknowledgement_mismatch",
                )

            store.acknowledge(
                transaction,
                str(first_page["page_identity"]),
                binding=_binding("first"),
                differences=_differences(),
            )
            with self.assertRaises(ComponentLockReviewError) as raised:
                store.acknowledge(
                    transaction,
                    str(first_page["page_identity"]),
                    binding=_binding("first"),
                    differences=_differences(),
                )
            self.assertEqual(
                raised.exception.code,
                "component_lock.review_acknowledgement_replayed",
            )

    def test_changed_authority_and_tampered_state_fail_closed(self) -> None:
        with _isolated_project() as project:
            store = ComponentLockReviewStore(project)
            started = store.start(binding=_binding(), differences=_differences())
            transaction = str(started["transaction_identity"])
            with self.assertRaises(ComponentLockReviewError) as changed:
                store.status(
                    transaction,
                    binding=_binding("changed"),
                    differences=_differences(),
                )
            self.assertEqual(
                changed.exception.code,
                "component_lock.review_transaction_mismatch",
            )
            with self.assertRaises(ComponentLockReviewError) as wrong_component:
                store.cleanup(transaction, component="components/other")
            self.assertEqual(
                wrong_component.exception.code,
                "component_lock.review_transaction_mismatch",
            )

            path = store.root / f"{transaction.removeprefix('sha256:')}.json"
            state = json.loads(path.read_bytes())
            state["page_identities"].reverse()
            path.write_bytes(canonical_json_bytes(state) + b"\n")
            with self.assertRaises(ComponentLockReviewError) as tampered:
                store.status(
                    transaction,
                    binding=_binding(),
                    differences=_differences(),
                )
            self.assertEqual(
                tampered.exception.code,
                "component_lock.review_state_tampered",
            )

    def test_small_review_and_invalid_identity_are_rejected(self) -> None:
        with _isolated_project() as project:
            store = ComponentLockReviewStore(project)
            with self.assertRaises(ComponentLockReviewError) as small:
                store.start(binding=_binding(), differences=_differences(512))
            self.assertEqual(
                small.exception.code,
                "component_lock.review_not_large",
            )
            with self.assertRaises(ComponentLockReviewError) as invalid:
                store.cleanup("../escape", component="components/example")
            self.assertEqual(
                invalid.exception.code,
                "component_lock.review_identity_invalid",
            )


if __name__ == "__main__":
    unittest.main()
