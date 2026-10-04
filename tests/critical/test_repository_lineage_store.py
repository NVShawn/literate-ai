from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.repository_lineage import (
    REPOSITORY_LINEAGE_FILE,
    REPOSITORY_PARENT_FILE,
    FilesystemRepositoryLineageStore,
    RepositoryLineageStoreError,
)
from literate_ai.contracts import (
    RepositoryLineage,
    RepositoryParentReference,
    RepositoryParentSelection,
)


def inherited() -> tuple[RepositoryParentSelection, RepositoryLineage]:
    from tests.support.fixtures_test_repository_lineage import fixture

    selection, _root, _child, lineage = fixture()
    return selection, lineage


class FilesystemRepositoryLineageStoreTests(unittest.TestCase):
    def test_compare_and_swap_rejects_stale_parent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FilesystemRepositoryLineageStore(root)
            selection = RepositoryParentSelection.root()
            lineage = RepositoryLineage(selection, (), ())
            store.replace(selection, lineage)
            stale = RepositoryParentSelection.inherit(
                (RepositoryParentReference("https://example.test/stale.git", "main"),)
            )

            with self.assertRaises(RepositoryLineageStoreError) as raised:
                store.replace(
                    selection,
                    lineage,
                    expected_selection_identity=stale.identity,
                    expected_lineage_identity=lineage.identity,
                )
        self.assertEqual(raised.exception.code, "repository_lineage.concurrent_change")

    def test_partial_write_failure_restores_both_previous_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = FilesystemRepositoryLineageStore(root)
            previous_selection = RepositoryParentSelection.root()
            previous_lineage = RepositoryLineage(previous_selection, (), ())
            store.replace(previous_selection, previous_lineage)
            parent_before = (root / REPOSITORY_PARENT_FILE).read_bytes()
            lineage_before = (root / REPOSITORY_LINEAGE_FILE).read_bytes()
            next_selection, next_lineage = inherited()
            original = store._atomic_write

            def fail_lineage(path: Path, content: bytes) -> None:
                if path.name == Path(REPOSITORY_LINEAGE_FILE).name:
                    raise OSError("injected")
                original(path, content)

            with mock.patch.object(store, "_atomic_write", side_effect=fail_lineage):
                with self.assertRaises(RepositoryLineageStoreError) as raised:
                    store.replace(
                        next_selection,
                        next_lineage,
                        expected_selection_identity=previous_selection.identity,
                        expected_lineage_identity=previous_lineage.identity,
                    )

            self.assertEqual(
                (root / REPOSITORY_PARENT_FILE).read_bytes(), parent_before
            )
            self.assertEqual(
                (root / REPOSITORY_LINEAGE_FILE).read_bytes(), lineage_before
            )
            self.assertEqual(store.load(), (previous_selection, previous_lineage))
        self.assertEqual(raised.exception.code, "repository_lineage.write_failed")


if __name__ == "__main__":
    unittest.main()
