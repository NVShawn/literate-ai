from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.source_to_specification.promotion_journals import (
    LegacyJournalMigrationReceipt,
    LegacyJournalMigrationRequest,
    LegacySourcePromotionJournalMigrator,
)
from literate_ai.source_to_specification.promotion_materialization import (
    SourcePromotionError,
)
from tests.unit.test_schema_catalog import SchemaCatalog


class LegacySourcePromotionJournalMigratorTests(unittest.TestCase):
    def test_moves_exact_journal_to_content_addressed_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            provenance = root / "provenance"
            accepted.mkdir()
            provenance.mkdir()
            (accepted / ".literate").mkdir()
            legacy = accepted / ".literate" / "source-translation.json"
            content = (
                b'{"schema":"literate-ai/source-translation@1",'
                b'"canary":"RAW-JOURNAL"}\n'
            )
            legacy.write_bytes(content)

            result = LegacySourcePromotionJournalMigrator().migrate(
                LegacyJournalMigrationRequest(
                    accepted_root=accepted,
                    provenance_root=provenance,
                )
            )

            self.assertFalse(legacy.exists())
            self.assertEqual(result.destination.read_bytes(), content)
            self.assertEqual(
                result.destination.relative_to(provenance.resolve()).as_posix(),
                result.receipt.destination_relative_path,
            )
            self.assertTrue(result.receipt.legacy_retired)
            self.assertEqual(
                LegacyJournalMigrationReceipt.from_dict(result.receipt.to_dict()),
                result.receipt,
            )
            SchemaCatalog().validate(
                "urn:literate-ai:schema:v2:legacy-source-promotion-journal-migration",
                result.receipt.to_dict(),
            )
            self.assertTrue(result.receipt.identity.startswith("sha256:"))

    def test_can_copy_for_a_non_destructive_legacy_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            provenance = root / "provenance"
            accepted.mkdir()
            provenance.mkdir()
            (accepted / ".literate").mkdir()
            legacy = accepted / ".literate" / "source-translation.json"
            legacy.write_text('{"schema":"legacy"}', encoding="utf-8")

            result = LegacySourcePromotionJournalMigrator().migrate(
                LegacyJournalMigrationRequest(
                    accepted_root=accepted,
                    provenance_root=provenance,
                    retire_legacy=False,
                )
            )

            self.assertTrue(legacy.is_file())
            self.assertTrue(result.destination.is_file())
            self.assertFalse(result.receipt.legacy_retired)

    def test_rejects_traversal_and_authority_overlap_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            accepted.mkdir()
            (accepted / ".literate").mkdir()
            legacy = accepted / ".literate" / "source-translation.json"
            legacy.write_text('{"schema":"legacy"}', encoding="utf-8")

            with self.assertRaises(SourcePromotionError) as traversal:
                LegacyJournalMigrationRequest(
                    accepted_root=accepted,
                    provenance_root=root,
                    legacy_relative_path="../source-translation.json",
                )
            self.assertEqual(traversal.exception.code, "promotion.migration_traversal")

            overlapping_provenance = accepted / "provenance"
            overlapping_provenance.mkdir()
            with self.assertRaises(SourcePromotionError) as overlap:
                LegacySourcePromotionJournalMigrator().migrate(
                    LegacyJournalMigrationRequest(
                        accepted_root=accepted,
                        provenance_root=overlapping_provenance,
                    )
                )
            self.assertEqual(overlap.exception.code, "promotion.migration_overlap")
            self.assertTrue(legacy.is_file())
            self.assertEqual(list(overlapping_provenance.iterdir()), [])

    def test_rejects_source_and_destination_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            provenance = root / "provenance"
            outside = root / "outside"
            accepted.mkdir()
            provenance.mkdir()
            outside.mkdir()
            (accepted / ".literate").mkdir()
            external_journal = outside / "journal.json"
            external_journal.write_text('{"schema":"legacy"}', encoding="utf-8")
            legacy = accepted / ".literate" / "source-translation.json"
            try:
                legacy.symlink_to(external_journal)
            except OSError as error:
                self.skipTest(f"host cannot create symlinks: {error}")

            request = LegacyJournalMigrationRequest(
                accepted_root=accepted,
                provenance_root=provenance,
            )
            with self.assertRaises(SourcePromotionError) as source_link:
                LegacySourcePromotionJournalMigrator().migrate(request)
            self.assertEqual(source_link.exception.code, "promotion.migration_symlink")

            legacy.unlink()
            legacy.write_text('{"schema":"legacy"}', encoding="utf-8")
            (provenance / "source-promotion").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaises(SourcePromotionError) as destination_link:
                LegacySourcePromotionJournalMigrator().migrate(request)
            self.assertEqual(
                destination_link.exception.code, "promotion.migration_symlink"
            )
            self.assertTrue(legacy.is_file())
            self.assertEqual(json.loads(legacy.read_text())["schema"], "legacy")

    def test_retirement_never_deletes_a_path_substituted_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = root / "accepted"
            provenance = root / "provenance"
            accepted.mkdir()
            provenance.mkdir()
            (accepted / ".literate").mkdir()
            legacy = accepted / ".literate" / "source-translation.json"
            original = b'{"schema":"legacy","value":"original"}'
            substituted = b'{"schema":"legacy","value":"substituted"}'
            legacy.write_bytes(original)
            preserved = root / "preserved-original.json"
            real_replace = __import__("os").replace
            canonical_legacy = legacy.resolve()

            def substitute_before_retirement(source, destination):
                if Path(source) == canonical_legacy:
                    real_replace(canonical_legacy, preserved)
                    canonical_legacy.write_bytes(substituted)
                real_replace(source, destination)

            with mock.patch(
                "literate_ai.source_to_specification.promotion_journals.os.replace",
                side_effect=substitute_before_retirement,
            ):
                with self.assertRaises(SourcePromotionError) as raised:
                    LegacySourcePromotionJournalMigrator().migrate(
                        LegacyJournalMigrationRequest(
                            accepted_root=accepted,
                            provenance_root=provenance,
                        )
                    )

            self.assertEqual(
                raised.exception.code, "promotion.migration_source_changed"
            )
            self.assertEqual(preserved.read_bytes(), original)
            self.assertEqual(legacy.read_bytes(), substituted)
            self.assertFalse(any(provenance.rglob("source-translation.json")))


if __name__ == "__main__":
    unittest.main()
