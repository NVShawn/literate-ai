from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.cache import (
    FilesystemProjectSourceCachePublicationAdapter,
    FileSystemSourceCache,
)
from literate_ai.application import (
    SOURCE_CACHE_PUBLICATION_NOTE,
    SourceCachePublicationError,
    SourceCachePublicationService,
)
from literate_ai.cli import main
from literate_ai.cli.cache import CACHE_PUBLISH_SCHEMA, cache_publish_from_args
from literate_ai.contracts import (
    AcceptedSourceCacheEntry,
    ContentIdentity,
    SourceIntelligenceAttachment,
    canonical_identity,
)


def _entry(label: str) -> AcceptedSourceCacheEntry:
    entry = Mock(spec=AcceptedSourceCacheEntry)
    entry.identity = canonical_identity({"entry": label})
    entry.source_tree_identity = canonical_identity({"source": label})
    return entry


class _Source:
    def __init__(
        self,
        entries: tuple[AcceptedSourceCacheEntry, ...],
        attachments: dict[str, tuple[SourceIntelligenceAttachment, ...]],
    ) -> None:
        self.entries = entries
        self.attachments = attachments

    def published_entries(self) -> tuple[AcceptedSourceCacheEntry, ...]:
        return self.entries

    def intelligence_attachments(
        self, source_tree_identity: ContentIdentity
    ) -> tuple[SourceIntelligenceAttachment, ...]:
        return self.attachments.get(source_tree_identity.uri, ())


class _Destination:
    def __init__(
        self,
        existing: tuple[ContentIdentity, ...] = (),
        *,
        return_mismatch: bool = False,
    ) -> None:
        self.identities = list(existing)
        self.return_mismatch = return_mismatch
        self.calls: list[
            tuple[AcceptedSourceCacheEntry, tuple[SourceIntelligenceAttachment, ...]]
        ] = []

    def published_entry_identities(self) -> tuple[ContentIdentity, ...]:
        return tuple(self.identities)

    def publish(
        self,
        entry: AcceptedSourceCacheEntry,
        *,
        intelligence_attachments: tuple[SourceIntelligenceAttachment, ...] = (),
    ) -> ContentIdentity:
        self.calls.append((entry, intelligence_attachments))
        if self.return_mismatch:
            return canonical_identity({"another": "entry"})
        self.identities.append(entry.identity)
        return entry.identity


class SourceCachePublicationServiceTests(unittest.TestCase):
    def test_rejects_a_destination_identity_substitution(self) -> None:
        entry = _entry("fresh")
        with self.assertRaises(SourceCachePublicationError) as raised:
            SourceCachePublicationService().publish(
                _Source((entry,), {}),
                _Destination(return_mismatch=True),
                target_id="project-committed",
                destination_reference="derived/accepted-source-cache",
            )
        self.assertEqual(raised.exception.code, "source_cache.publish_mismatch")


class FilesystemProjectSourceCachePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_adapter_and_cli_publish_an_empty_runtime_cache(self) -> None:
        project = self.root / "project"
        _write_project(
            project,
            (("project-committed", "derived/accepted-source-cache"),),
        )
        FileSystemSourceCache(
            "standard-local",
            project / "generated" / "accepted-source-cache",
        )

        with patch.dict(os.environ, {"BUILD_DIR": "generated"}):
            result = FilesystemProjectSourceCachePublicationAdapter().publish(project)
            envelope = cache_publish_from_args(
                SimpleNamespace(project=str(project), target="project-committed")
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            status = main(
                (
                    "--json",
                    "cache",
                    "publish",
                    "--project",
                    str(project),
                    "--target",
                    "project-committed",
                ),
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(result.target_id, "project-committed")
        self.assertEqual(result.published_entry_identities, ())
        self.assertEqual(result.already_present_entry_identities, ())
        self.assertTrue(Path(result.destination).is_dir())
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["result"], envelope)
        self.assertEqual(
            envelope,
            {
                "schema": CACHE_PUBLISH_SCHEMA,
                "target": "project-committed",
                "destination": str(project / "derived" / "accepted-source-cache"),
                "published": [],
                "already_present": [],
                "note": SOURCE_CACHE_PUBLICATION_NOTE,
            },
        )


def _write_project(root: Path, targets: tuple[tuple[str, str], ...]) -> None:
    root.mkdir()
    manifest = {
        "schema": "urn:literate-ai:schema:v2:project-definition",
        "project_id": "source-cache-publication-test",
        "version": "1.0.0",
        "profile": "canonical",
        "agent_skill": "SKILL.md",
        "component_roots": ["components"],
        "flavor_roots": ["flavors"],
        "skill_roots": ["skills"],
        "workflow_roots": ["workflows"],
        "routing_roots": ["routing"],
        "documentation_roots": ["docs"],
        "source_intelligence": {
            "schema": ("urn:literate-ai:schema:v1:project-source-intelligence-policy"),
            "provider_id": "none",
            "command": None,
            "minimum_version": None,
            "artifact_path": None,
            "stages": {
                "project-maintenance": "off",
                "source-generation": "off",
                "cache-consumption": "off",
                "source-to-specification": "off",
                "repository-source-admission": "off",
                "structural-review": "off",
            },
            "artifact_publication": "metadata-only",
        },
        "source_cache": {
            "schema": "urn:literate-ai:schema:v2:source-cache-configuration",
            "mode": "read-only",
            "targets": [
                {
                    "schema": "urn:literate-ai:schema:v2:source-cache-target",
                    "target_id": target_id,
                    "format": "filesystem-v2",
                    "root_kind": "project-relative",
                    "root_reference": reference,
                }
                for target_id, reference in targets
            ],
            "write_target_id": None,
            "require_unique": True,
        },
    }
    (root / "literate.project.json").write_text(json.dumps(manifest), encoding="utf-8")
