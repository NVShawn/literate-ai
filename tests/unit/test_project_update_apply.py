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

    def test_upstream_only_is_written(self) -> None:
        target = self.write("a.md", BASELINE)
        plan = self.plan(
            self.entry(
                "a.md", ProjectUpdateClassification.UPSTREAM_ONLY, local=BASELINE
            )
        )
        result = self.apply(plan, {"a.md": UPSTREAM})
        self.assertEqual(result.applied, ("a.md",))
        self.assertEqual(target.read_bytes(), UPSTREAM)

    def test_untouched_upstream_removal_is_applied(self) -> None:
        target = self.write("flavors/retired/flavor.md", BASELINE)
        plan = self.plan(
            self.entry(
                "flavors/retired/flavor.md",
                ProjectUpdateClassification.UPSTREAM_ONLY,
                local=BASELINE,
                upstream=None,
            )
        )

        result = self.apply(plan, {})

        self.assertEqual(result.applied, ("flavors/retired/flavor.md",))
        self.assertFalse(target.exists())

    def test_final_validation_failure_rolls_back_all_writes_and_directories(
        self,
    ) -> None:
        changed = self.write("existing.md", BASELINE)
        plan = self.plan(
            self.entry(
                "existing.md",
                ProjectUpdateClassification.UPSTREAM_ONLY,
                local=BASELINE,
            ),
            self.entry(
                "nested/new.md",
                ProjectUpdateClassification.UPSTREAM_ADDED,
                baseline=None,
                local=None,
            ),
        )

        def reject(root: Path) -> None:
            self.assertEqual((root / "existing.md").read_bytes(), UPSTREAM)
            self.assertEqual((root / "nested/new.md").read_bytes(), UPSTREAM)
            raise RuntimeError("complete prospective tree rejected")

        with self.assertRaisesRegex(RuntimeError, "prospective tree rejected"):
            self.apply(
                plan,
                {"existing.md": UPSTREAM, "nested/new.md": UPSTREAM},
                adopt_added=True,
                validator=reject,
            )

        self.assertEqual(changed.read_bytes(), BASELINE)
        self.assertFalse((self.root / "nested").exists())

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

    def test_preserved_dynamic_is_never_touched(self) -> None:
        target = self.write("d.json", LOCAL)
        plan = self.plan(
            self.entry(
                "d.json",
                ProjectUpdateClassification.PRESERVED_DYNAMIC,
                upstream=None,
            )
        )
        result = self.apply(plan, {})
        self.assertEqual(result.applied, ())
        self.assertEqual(target.read_bytes(), LOCAL)

    def test_upstream_added_auto_adopts_only_without_divergence(self) -> None:
        entry = self.entry(
            "new.md",
            ProjectUpdateClassification.UPSTREAM_ADDED,
            baseline=None,
            local=None,
        )
        automatic = self.apply(self.plan(entry), {"new.md": UPSTREAM})
        self.assertEqual(automatic.adopted, ("new.md",))
        self.assertEqual(
            automatic.adopted_because,
            "the project has not diverged from its baseline",
        )
        self.assertEqual((self.root / "new.md").read_bytes(), UPSTREAM)

        (self.root / "new.md").unlink()
        self.write("local.md", LOCAL)
        local = self.entry("local.md", ProjectUpdateClassification.LOCAL_ONLY)
        refused = self.apply(self.plan(local, entry), {"new.md": UPSTREAM})
        self.assertEqual(refused.adopted, ())
        self.assertEqual(refused.refused["upstream-added"], ("new.md",))
        self.assertFalse((self.root / "new.md").exists())

        adopted = self.apply(
            self.plan(local, entry), {"new.md": UPSTREAM}, adopt_added=True
        )
        self.assertEqual(adopted.adopted, ("new.md",))
        self.assertEqual(adopted.adopted_because, "requested")
        self.assertEqual(adopted.refused["local-only"], ("local.md",))
        self.assertEqual((self.root / "new.md").read_bytes(), UPSTREAM)

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

    def test_an_upstream_change_after_planning_is_refused(self) -> None:
        target = self.write("a.md", BASELINE)
        plan = self.plan(
            self.entry(
                "a.md", ProjectUpdateClassification.UPSTREAM_ONLY, local=BASELINE
            )
        )
        with self.assertRaises(ProjectUpdateError) as raised:
            self.apply(plan, {"a.md": b"different upstream now\n"})
        self.assertEqual(raised.exception.code, "project.update_upstream_changed")
        self.assertEqual(target.read_bytes(), BASELINE)

    def test_review_is_required_only_when_something_was_written(self) -> None:
        self.write("l.md", LOCAL)
        quiet = self.apply(
            self.plan(self.entry("l.md", ProjectUpdateClassification.LOCAL_ONLY)),
            {"l.md": UPSTREAM},
        )
        self.assertFalse(quiet.to_dict()["authority_review_required"])

        self.write("a.md", BASELINE)
        wrote = self.apply(
            self.plan(
                self.entry(
                    "a.md", ProjectUpdateClassification.UPSTREAM_ONLY, local=BASELINE
                )
            ),
            {"a.md": UPSTREAM},
        )
        self.assertTrue(wrote.to_dict()["authority_review_required"])


if __name__ == "__main__":
    unittest.main()
