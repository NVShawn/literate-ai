"""Apply must write only what upstream solely authored, and refuse everything else.

The dangerous failure is a silent one: clobbering project authority while reporting
success. Each refusal below is therefore asserted twice -- the result must say it was
refused, and the file on disk must still hold the project's bytes.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.project_update_apply import apply_project_update
from literate_ai.adapters.project_updates import ProjectUpdateError
from literate_ai.contracts.identity import ContentIdentity, HashAlgorithm
from literate_ai.contracts.project_initialization import ProjectInitializationOrigin
from literate_ai.contracts.project_updates import (
    ProjectUpdateClassification,
    ProjectUpdateFile,
    ProjectUpdatePlan,
)

UPSTREAM = b"upstream bytes\n"
LOCAL = b"local bytes\n"
BASELINE = b"baseline bytes\n"


def identity(content: bytes) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest())


def origin() -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        repository_url="https://example.invalid/literate-ai.git",
        git_revision="0" * 40,
        distribution_name="literate-ai",
        distribution_version="0.2.0",
    )


class ApplyProjectUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, relative: str, content: bytes) -> Path:
        target = self.root.joinpath(*Path(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def plan(self, *files: ProjectUpdateFile) -> ProjectUpdatePlan:
        return ProjectUpdatePlan(
            project_identity=identity(b"project"),
            baseline_identity=identity(b"baseline"),
            previous_origin=origin(),
            upstream_origin=origin(),
            files=tuple(sorted(files, key=lambda item: item.path)),
        )

    def entry(
        self,
        path: str,
        classification: ProjectUpdateClassification,
        *,
        baseline: bytes | None = BASELINE,
        local: bytes | None = LOCAL,
        upstream: bytes | None = UPSTREAM,
    ) -> ProjectUpdateFile:
        return ProjectUpdateFile(
            path=path,
            classification=classification,
            baseline_identity=identity(baseline) if baseline is not None else None,
            local_identity=identity(local) if local is not None else None,
            upstream_identity=identity(upstream) if upstream is not None else None,
        )

    def apply(self, plan: ProjectUpdatePlan, content: dict[str, bytes], **kwargs):
        with mock.patch(
            "literate_ai.adapters.project_update_apply._upstream_template",
            return_value=content,
        ):
            return apply_project_update(plan, self.root, **kwargs)

    def test_conflict_is_refused_and_left_alone(self) -> None:
        target = self.write("c.md", LOCAL)
        plan = self.plan(self.entry("c.md", ProjectUpdateClassification.CONFLICT))
        result = self.apply(plan, {"c.md": UPSTREAM})
        self.assertEqual(result.applied, ())
        self.assertEqual(result.refused["conflict"], ("c.md",))
        self.assertEqual(target.read_bytes(), LOCAL)

    def test_local_only_is_refused_and_left_alone(self) -> None:
        target = self.write("l.md", LOCAL)
        plan = self.plan(self.entry("l.md", ProjectUpdateClassification.LOCAL_ONLY))
        result = self.apply(plan, {"l.md": UPSTREAM})
        self.assertEqual(result.applied, ())
        self.assertEqual(result.refused["local-only"], ("l.md",))
        self.assertEqual(target.read_bytes(), LOCAL)

    def test_a_local_change_after_planning_is_refused(self) -> None:
        target = self.write("a.md", b"changed after planning\n")
        plan = self.plan(
            self.entry(
                "a.md", ProjectUpdateClassification.UPSTREAM_ONLY, local=BASELINE
            )
        )
        with self.assertRaises(ProjectUpdateError) as raised:
            self.apply(plan, {"a.md": UPSTREAM})
        self.assertEqual(raised.exception.code, "project.update_local_changed")
        self.assertEqual(target.read_bytes(), b"changed after planning\n")


if __name__ == "__main__":
    unittest.main()
