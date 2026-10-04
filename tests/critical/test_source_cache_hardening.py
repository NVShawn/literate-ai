"""Adversarial filesystem and resource-bound checks for accepted-source caches."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.cache import (
    SourceCacheError,
    SourceCacheResolver,
)
from literate_ai.contracts import (
    ProjectDefinition,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheRootKind,
    SourceCacheTarget,
)
from literate_ai.projects import LoadedProject
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_source_cache import (
    _source_intelligence_policy,
)


def _project(root: Path) -> LoadedProject:
    return LoadedProject(
        root,
        ProjectDefinition(
            project_id="cache-hardening",
            version="1.0.0",
            profile="canonical",
            agent_skill="SKILL.md",
            component_roots=("components",),
            flavor_roots=("flavors",),
            skill_roots=("skills",),
            workflow_roots=("workflows",),
            routing_roots=(),
            documentation_roots=("docs",),
            source_intelligence=_source_intelligence_policy(),
        ),
    )


class ReadOnlyAndPathSafetyTests(unittest.TestCase):
    def test_casefold_alias_cannot_overlap_project_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "docs").mkdir()
            configuration = SourceCacheConfiguration(
                SourceCacheMode.READ_WRITE,
                (
                    SourceCacheTarget(
                        "project",
                        SourceCacheRootKind.PROJECT_RELATIVE,
                        "DOCS/cache",
                    ),
                ),
                write_target_id="project",
            )

            with self.assertRaises(SourceCacheError) as captured:
                SourceCacheResolver.from_configuration(
                    configuration, project=_project(root)
                )

            self.assertEqual(captured.exception.code, "source-cache.authority-overlap")
            self.assertFalse((root / "docs" / "cache").exists())


class DescriptorAndMaterializationRaceTests(unittest.TestCase):
    def test_cas_returns_verified_bytes_from_the_opened_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cas = FileSystemCAS(Path(temporary) / "cas")
            reference = cas.put_bytes(b"original")
            target = cas.path_for(reference)
            displaced = target.with_name(target.name + ".displaced")
            real_open = os.open
            swapped = False
            replacement_blocked = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal replacement_blocked, swapped
                descriptor = real_open(path, flags, *args, **kwargs)
                if Path(path) == target and not swapped:
                    swapped = True
                    try:
                        target.replace(displaced)
                    except PermissionError:
                        # Windows opens without delete sharing, so the platform
                        # itself prevents replacement while this handle is live.
                        replacement_blocked = True
                    else:
                        target.write_bytes(b"tampered")
                return descriptor

            with patch("literate_ai.storage.cas.os.open", side_effect=racing_open):
                content = cas.get_bytes(reference)

            self.assertEqual(content, b"original")
            self.assertTrue(swapped)
            if replacement_blocked:
                self.assertEqual(target.read_bytes(), b"original")
                self.assertFalse(displaced.exists())
            else:
                self.assertEqual(target.read_bytes(), b"tampered")


if __name__ == "__main__":
    unittest.main()
