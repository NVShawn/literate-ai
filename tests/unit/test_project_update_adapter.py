"""Safe three-way planning for projects created by ``litai init``."""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from literate_ai.adapters import (
    project_initialization,
    project_updates,
    project_validation,
)
from literate_ai.adapters.project_initialization import (
    INITIALIZATION_BASELINE_FILE,
    LEGACY_LIFT_SHIFT_ADR,
    render_starter_document,
)
from literate_ai.adapters.project_update_apply import apply_project_update
from literate_ai.adapters.project_updates import (
    FilesystemProjectUpdateAdapter,
    ProjectUpdateError,
)
from literate_ai.adapters.project_validation import ProjectValidationError
from literate_ai.contracts import (
    ProjectInitializationBaseline,
    ProjectInitializationBaselineFile,
    ProjectInitializationOrigin,
    ProjectUpdateClassification,
    ProjectUpdatePlan,
    canonical_json_bytes,
)
from literate_ai.projects import discover_project
from literate_ai.repository_urls import repository_urls_equivalent
from tests.unit.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)
from tests.unit.test_schema_catalog import SchemaCatalog


def _origin(revision: str, version: str) -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        "ssh://git.example.test/operator/literate-ai.git",
        revision * 40,
        "literate-ai",
        version,
    )


class FilesystemProjectUpdateAdapterTests(unittest.TestCase):
    def _local_component_using_retired_route(self, target: Path) -> None:
        root = Path(__file__).resolve().parents[2]
        content = (
            root
            / "src/literate_ai/project_template/samples/hello-component/component.md"
        ).read_text(encoding="utf-8")
        content = (
            content.replace("sample: true", "sample: false")
            .replace(
                "workflows/production/staging/dev/workflow.md",
                "workflows/dev/workflow.md",
            )
            .replace("routing/production/staging/dev/routing.json", "routing/dev.json")
        )
        manifest = target / "components/local-service/component.md"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(content, encoding="utf-8")

    def _validate_local_component(self, target: Path) -> None:
        project = discover_project(target)
        assert project is not None
        project_validation._validate_component_authoring(
            target / "components/local-service/component.md", project
        )

    def _mark_initialization_as_converted(self, target: Path) -> None:
        readme = target / "docs/README.md"
        readme.write_text(
            render_starter_document("docs/README.md", converted=True),
            encoding="utf-8",
        )
        adr = target / LEGACY_LIFT_SHIFT_ADR
        adr.parent.mkdir(parents=True, exist_ok=True)
        adr.write_text("# Legacy project lift-and-shift\n", encoding="utf-8")
        baseline_path = target / INITIALIZATION_BASELINE_FILE
        baseline = ProjectInitializationBaseline.from_dict(
            json.loads(baseline_path.read_text(encoding="utf-8"))
        )
        replacements = {
            "docs/README.md": readme.read_bytes(),
            LEGACY_LIFT_SHIFT_ADR: adr.read_bytes(),
        }
        retained = {
            item.path: item for item in baseline.files if item.path not in replacements
        }
        for relative, content in replacements.items():
            retained[relative] = ProjectInitializationBaselineFile(
                relative, len(content), project_updates._identity(content)
            )
        converted = ProjectInitializationBaseline(
            baseline.origin_identity,
            baseline.template_protocol,
            tuple(retained[path] for path in sorted(retained)),
        )
        baseline_path.write_bytes(canonical_json_bytes(converted.to_dict()) + b"\n")

    def _add_legacy_shim_authority(
        self, target: Path, *, include_dependents: bool = True
    ) -> None:
        """Reproduce the framework-owned files a Phase 1 `--convert` run leaves.

        `legacy_shim_authority()` (harness_inventory.py) writes these paths during
        `litai init --convert`; they land in the initialization baseline as ordinary
        catalog files even though no static `_TEMPLATE_FILES`/`_STARTER_TEMPLATE_FILES`
        entry ever reproduces them for a later `litai update` comparison.
        """

        from literate_ai.adapters.harness_inventory import legacy_shim_authority

        inventory = {"commands": {"build": {"command": "make build"}}}
        baseline = {"phases": [{"phase": "build"}]}
        generated = legacy_shim_authority(inventory, baseline)
        keep = {
            "components/legacy-project-wrapper/component.md",
            "flavors/legacy-project-shim/flavor.md",
            "flavors/legacy-project-shim/openspec/spec.md",
        }
        if include_dependents:
            keep |= {
                "workflows/legacy-adoption/workflow.md",
                "routing/legacy-adoption.json",
                "skills/specification-to-source/legacy-project-shim/SKILL.md",
            }
        files = {path: generated[path] for path in keep}
        files[".literate/harness-inventory.json"] = "{}\n"
        if include_dependents:
            files["components/legacy-project-wrapper/implementation/README.md"] = (
                "retained implementation\n"
            )
        baseline_path = target / INITIALIZATION_BASELINE_FILE
        baseline = ProjectInitializationBaseline.from_dict(
            json.loads(baseline_path.read_text(encoding="utf-8"))
        )
        retained = {item.path: item for item in baseline.files}
        for relative, text in files.items():
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            content = path.read_bytes()
            retained[relative] = ProjectInitializationBaselineFile(
                relative, len(content), project_updates._identity(content)
            )
        updated = ProjectInitializationBaseline(
            baseline.origin_identity,
            baseline.template_protocol,
            tuple(retained[path] for path in sorted(retained)),
        )
        baseline_path.write_bytes(canonical_json_bytes(updated.to_dict()) + b"\n")

    def test_plan_classifies_three_way_state_without_mutating_project(self) -> None:
        previous = _origin("a", "0.2.0")
        upstream = _origin("b", "0.3.0")
        original_template_text = project_updates._template_text

        def changed_template(resource: str) -> str:
            content = original_template_text(resource)
            if resource in {"AGENTS.md", "CLAUDE.md"}:
                return content + f"\nupstream-{resource}\n"
            return content

        template_files = {
            **project_updates._TEMPLATE_FILES,
            "UPSTREAM.md": "AGENTS.md",
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: previous,
            ).initialize(
                target,
                flavor_selectors=("+bazel", "+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )
            skill = target / "SKILL.md"
            skill.write_text(skill.read_text(encoding="utf-8") + "\nlocal\n")
            claude = target / "CLAUDE.md"
            claude.write_text(claude.read_text(encoding="utf-8") + "\nlocal\n")
            before = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }

            with (
                mock.patch.object(project_updates, "_TEMPLATE_FILES", template_files),
                mock.patch.object(
                    project_updates, "_template_text", side_effect=changed_template
                ),
            ):
                plan = FilesystemProjectUpdateAdapter(
                    origin_provider=lambda: upstream
                ).plan(target)
                reassigned = ProjectInitializationOrigin(
                    "ssh://git.example.test/other/literate-ai.git",
                    "c" * 40,
                    "literate-ai",
                    "0.3.0",
                )
                with self.assertRaisesRegex(
                    ProjectUpdateError, "differs from the initializing origin"
                ):
                    FilesystemProjectUpdateAdapter(
                        origin_provider=lambda: reassigned
                    ).plan(target)

            after = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }

        by_path = {item.path: item.classification for item in plan.files}
        self.assertEqual(
            by_path["AGENTS.md"], ProjectUpdateClassification.UPSTREAM_ONLY
        )
        self.assertEqual(by_path["SKILL.md"], ProjectUpdateClassification.LOCAL_ONLY)
        self.assertEqual(by_path["CLAUDE.md"], ProjectUpdateClassification.CONFLICT)
        self.assertEqual(
            by_path["UPSTREAM.md"], ProjectUpdateClassification.UPSTREAM_ADDED
        )
        self.assertEqual(
            by_path["literate.project.json"],
            ProjectUpdateClassification.PRESERVED_DYNAMIC,
        )
        self.assertEqual(before, after)
        self.assertEqual(plan.previous_origin, previous)
        self.assertEqual(plan.upstream_origin, upstream)
        self.assertFalse(plan.to_dict()["apply_supported"])
        SchemaCatalog().validate(plan.SCHEMA, plan.to_dict())
        self.assertEqual(ProjectUpdatePlan.from_dict(plan.to_dict()), plan)
        tampered = deepcopy(plan.to_dict())
        tampered["counts"]["conflict"] += 1
        with self.assertRaisesRegex(ValueError, "counts does not match"):
            ProjectUpdatePlan.from_dict(tampered)

    def test_github_ssh_and_https_initialization_origins_are_equivalent(self) -> None:
        previous = ProjectInitializationOrigin(
            "https://github.com/jordanhubbard/literate-ai",
            "a" * 40,
            "literate-ai",
            "0.9.0",
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: previous,
            ).initialize(
                target,
                flavor_selectors=("+make", "+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )
            for repository_url in (
                "git@github.com:jordanhubbard/literate-ai.git",
                "ssh://git@github.com/jordanhubbard/literate-ai.git",
                "ssh://git@github.com:22/jordanhubbard/literate-ai/",
                "https://github.com:443/jordanhubbard/literate-ai.git",
            ):
                with self.subTest(repository_url=repository_url):
                    upstream = ProjectInitializationOrigin(
                        repository_url,
                        "b" * 40,
                        "literate-ai",
                        "0.10.0",
                    )
                    plan = FilesystemProjectUpdateAdapter(
                        origin_provider=lambda upstream=upstream: upstream
                    ).plan(target)
                    self.assertEqual(plan.previous_origin, previous)
                    self.assertEqual(plan.upstream_origin, upstream)

    def test_repository_origin_equivalence_remains_conservative(self) -> None:
        official = "https://github.com/jordanhubbard/literate-ai.git"
        for different in (
            "https://gitlab.com/jordanhubbard/literate-ai.git",
            "https://github.com/other/literate-ai.git",
            "https://github.com/example-org/other.git",
            "http://github.com/jordanhubbard/literate-ai.git",
            "ssh://root@github.com/jordanhubbard/literate-ai.git",
            "ssh://git@github.com:2222/jordanhubbard/literate-ai.git",
            "https://github.com/example-org/literate%2Dai.git",
            "https://github.com/example-org/literate ai.git",
            "file:///tmp/literate-ai.git",
        ):
            with self.subTest(different=different):
                self.assertFalse(repository_urls_equivalent(official, different))
        self.assertTrue(
            repository_urls_equivalent(
                "file:///tmp/literate-ai.git", "file:///tmp/literate-ai.git"
            )
        )

    def test_project_initialized_before_repository_move_plans_from_successor(
        self,
    ) -> None:
        previous = ProjectInitializationOrigin(
            "https://github.com/NVIDIA-dev/literate-ai",
            "a" * 40,
            "literate-ai",
            "1.0.1",
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: previous,
            ).initialize(
                target,
                flavor_selectors=("+make", "+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )
            successor = ProjectInitializationOrigin(
                "https://github.com/jordanhubbard/literate-ai",
                "b" * 40,
                "literate-ai",
                "1.1.0",
            )
            plan = FilesystemProjectUpdateAdapter(
                origin_provider=lambda: successor
            ).plan(target)
            self.assertEqual(plan.previous_origin, previous)
            self.assertEqual(plan.upstream_origin, successor)
            for unrelated in (
                "https://github.com/other/literate-ai",
                "https://github.com/jordanhubbard/other",
            ):
                with self.subTest(unrelated=unrelated):
                    with self.assertRaises(ProjectUpdateError) as raised:
                        FilesystemProjectUpdateAdapter(
                            origin_provider=lambda unrelated=unrelated: (
                                ProjectInitializationOrigin(
                                    unrelated, "b" * 40, "literate-ai", "1.1.0"
                                )
                            )
                        ).plan(target)
                    self.assertEqual(
                        raised.exception.code, "project.update_origin_changed"
                    )

    def test_untouched_retired_catalog_template_is_a_safe_removal(self) -> None:
        previous = _origin("a", "0.2.0")
        upstream = _origin("b", "0.9.0")
        retired = "flavors/build-bazel/flavor.md"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: previous,
            ).initialize(
                target,
                flavor_selectors=("+bazel", "+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )
            without_retired = {
                path: resource
                for path, resource in project_updates._TEMPLATE_FILES.items()
                if path != retired
            }
            with mock.patch.object(project_updates, "_TEMPLATE_FILES", without_retired):
                plan = FilesystemProjectUpdateAdapter(
                    origin_provider=lambda: upstream
                ).plan(target)

        by_path = {item.path: item.classification for item in plan.files}
        self.assertEqual(by_path[retired], ProjectUpdateClassification.UPSTREAM_ONLY)
        self.assertEqual(
            by_path["literate.project.json"],
            ProjectUpdateClassification.PRESERVED_DYNAMIC,
        )

    def test_project_owned_component_keeps_local_route_during_update(self) -> None:
        previous = _origin("a", "0.9.0")
        upstream = _origin("b", "0.10.0")
        historical = {
            **project_updates._TEMPLATE_FILES,
            "routing/dev.json": "routing/production/staging/dev/routing.json",
            "workflows/dev/workflow.md": (
                "workflows/production/staging/dev/workflow.md"
            ),
        }
        original_template_text = project_updates._template_text

        def changed_template(resource: str) -> str:
            content = original_template_text(resource)
            if resource == "AGENTS.md":
                return content + "\n0.10 update\n"
            return content

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            with mock.patch(
                "literate_ai.adapters.project_initialization._TEMPLATE_FILES",
                historical,
            ):
                FilesystemProjectInitializationAdapter(
                    standard_binding_provider=lambda: None,
                    initialization_origin_provider=lambda: previous,
                ).initialize(
                    target,
                    flavor_selectors=("+make", "+python", "+macos"),
                    source_intelligence_provider="none",
                    empty=True,
                )
            self._local_component_using_retired_route(target)
            route_before = (target / "routing/dev.json").read_bytes()
            workflow_before = (target / "workflows/dev/workflow.md").read_bytes()
            agents_before = (target / "AGENTS.md").read_bytes()

            with mock.patch.object(
                project_updates, "_template_text", side_effect=changed_template
            ):
                plan = FilesystemProjectUpdateAdapter(
                    origin_provider=lambda: upstream
                ).plan(target)
                by_path = {item.path: item.classification for item in plan.files}
                self.assertNotIn("routing/dev.json", by_path)
                self.assertNotIn("workflows/dev/workflow.md", by_path)
                self.assertEqual(
                    by_path["AGENTS.md"], ProjectUpdateClassification.UPSTREAM_ONLY
                )
                applied = apply_project_update(
                    plan,
                    target,
                    validator=self._validate_local_component,
                )

            self.assertEqual(applied.applied, ("AGENTS.md",))
            self.assertNotEqual((target / "AGENTS.md").read_bytes(), agents_before)
            self.assertEqual((target / "routing/dev.json").read_bytes(), route_before)
            self.assertEqual(
                (target / "workflows/dev/workflow.md").read_bytes(), workflow_before
            )

    def test_missing_project_owned_route_still_fails_and_rolls_back(self) -> None:
        previous = _origin("a", "0.9.0")
        upstream = _origin("b", "0.10.0")
        historical = {
            **project_updates._TEMPLATE_FILES,
            "routing/dev.json": "routing/production/staging/dev/routing.json",
            "workflows/dev/workflow.md": (
                "workflows/production/staging/dev/workflow.md"
            ),
        }
        original_template_text = project_updates._template_text

        def changed_template(resource: str) -> str:
            content = original_template_text(resource)
            return content + "\n0.10 update\n" if resource == "AGENTS.md" else content

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            with mock.patch(
                "literate_ai.adapters.project_initialization._TEMPLATE_FILES",
                historical,
            ):
                FilesystemProjectInitializationAdapter(
                    standard_binding_provider=lambda: None,
                    initialization_origin_provider=lambda: previous,
                ).initialize(
                    target,
                    flavor_selectors=("+make", "+python", "+macos"),
                    source_intelligence_provider="none",
                    empty=True,
                )
            self._local_component_using_retired_route(target)
            agents_before = (target / "AGENTS.md").read_bytes()
            with mock.patch.object(
                project_updates, "_template_text", side_effect=changed_template
            ):
                plan = FilesystemProjectUpdateAdapter(
                    origin_provider=lambda: upstream
                ).plan(target)
                (target / "routing/dev.json").unlink()
                with self.assertRaises(ProjectValidationError) as raised:
                    apply_project_update(
                        plan,
                        target,
                        validator=self._validate_local_component,
                    )

            self.assertEqual(raised.exception.code, "inputs.closure_unavailable")
            self.assertEqual((target / "AGENTS.md").read_bytes(), agents_before)

    def test_converted_readme_keeps_legacy_adr_reachable_across_update(self) -> None:
        previous = _origin("a", "0.9.0")
        upstream = _origin("b", "0.10.0")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: previous,
            ).initialize(
                target,
                flavor_selectors=("+make", "+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )
            self._mark_initialization_as_converted(target)
            changed_docs = {
                **project_initialization._STARTER_DOCS,
                "docs/README.md": (
                    project_initialization._STARTER_DOCS["docs/README.md"]
                    + "\n## Updated framework guidance\n"
                ),
            }

            def validate_docs(root: Path) -> None:
                project = discover_project(root)
                assert project is not None
                project_validation._documentation_catalog(project)

            with mock.patch.object(
                project_initialization, "_STARTER_DOCS", changed_docs
            ):
                plan = FilesystemProjectUpdateAdapter(
                    origin_provider=lambda: upstream
                ).plan(target)
                by_path = {item.path: item.classification for item in plan.files}
                self.assertEqual(
                    by_path["docs/README.md"],
                    ProjectUpdateClassification.UPSTREAM_ONLY,
                )
                applied = apply_project_update(plan, target, validator=validate_docs)
                self.assertIn("docs/README.md", applied.applied)
                updated = (target / "docs/README.md").read_text(encoding="utf-8")
                self.assertIn("## Updated framework guidance", updated)
                self.assertIn(
                    "decisions/0001-legacy-project-lift-and-shift.md", updated
                )

                current = FilesystemProjectUpdateAdapter(
                    origin_provider=lambda: upstream
                ).plan(target)
                current_by_path = {
                    item.path: item.classification for item in current.files
                }
                self.assertEqual(
                    current_by_path["docs/README.md"],
                    ProjectUpdateClassification.ALREADY_CURRENT,
                )

                (target / "docs/README.md").write_text(
                    updated + "\nLocal project guidance.\n", encoding="utf-8"
                )
                local = FilesystemProjectUpdateAdapter(
                    origin_provider=lambda: upstream
                ).plan(target)
                local_by_path = {item.path: item.classification for item in local.files}
                self.assertEqual(
                    local_by_path["docs/README.md"],
                    ProjectUpdateClassification.CONFLICT,
                )

    def test_qualified_conversion_shim_authority_survives_update(self) -> None:
        """Regression for #340: retained legacy-adoption authority must not be

        planned for removal while the retained implementation, harness evidence,
        routing, workflow, and specification-to-source skill it still generated
        for still exist.
        """

        previous = _origin("a", "0.9.0")
        upstream = _origin("b", "0.11.0")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: previous,
            ).initialize(
                target,
                flavor_selectors=("+make", "+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )
            self._add_legacy_shim_authority(target)

            plan = FilesystemProjectUpdateAdapter(
                origin_provider=lambda: upstream
            ).plan(target)
            by_path = {item.path: item.classification for item in plan.files}

            for shim_path in (
                "components/legacy-project-wrapper/component.md",
                "flavors/legacy-project-shim/flavor.md",
                "flavors/legacy-project-shim/openspec/spec.md",
            ):
                self.assertNotIn(
                    shim_path,
                    by_path,
                    f"{shim_path} must not be planned for mechanical removal while "
                    "retained legacy-adoption state still depends on it",
                )

            applied = apply_project_update(plan, target, adopt_added=True)
            self.assertNotIn(
                "components/legacy-project-wrapper/component.md", applied.applied
            )
            self.assertTrue(
                (target / "components/legacy-project-wrapper/component.md").is_file()
            )
            self.assertTrue(
                (target / "flavors/legacy-project-shim/flavor.md").is_file()
            )
            self.assertTrue(
                (target / "flavors/legacy-project-shim/openspec/spec.md").is_file()
            )

    def test_unqualified_conversion_shim_authority_remains_removable(self) -> None:
        """A conversion shim with no surviving dependents is an ordinary retired

        catalog file: it should still classify as a safe `upstream-only` removal.
        """

        previous = _origin("a", "0.9.0")
        upstream = _origin("b", "0.11.0")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: previous,
            ).initialize(
                target,
                flavor_selectors=("+make", "+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )
            self._add_legacy_shim_authority(target, include_dependents=False)

            plan = FilesystemProjectUpdateAdapter(
                origin_provider=lambda: upstream
            ).plan(target)
            by_path = {item.path: item.classification for item in plan.files}
            self.assertEqual(
                by_path["components/legacy-project-wrapper/component.md"],
                ProjectUpdateClassification.UPSTREAM_ONLY,
            )
            self.assertEqual(
                by_path["flavors/legacy-project-shim/flavor.md"],
                ProjectUpdateClassification.UPSTREAM_ONLY,
            )
            self.assertEqual(
                by_path["flavors/legacy-project-shim/openspec/spec.md"],
                ProjectUpdateClassification.UPSTREAM_ONLY,
            )

    def test_dangling_roadmap_owner_protects_queue_from_blind_replacement(
        self,
    ) -> None:
        """A conversion-added detailed roadmap still owned by the queue blocks apply.

        Regression test for https://github.com/NVIDIA-dev/literate-ai/issues/339:
        ``litai update --apply --adopt-added`` from a converted 0.9.0 project used
        to classify ``docs/roadmap/active-work.md`` as mechanically safe
        ``upstream-only`` even though a preserved dynamic roadmap document still
        pointed at a queue heading the upstream queue no longer carries. Applying
        the safe subset then failed validation
        (``project.roadmap_lifecycle_owner_dangling``) and rolled back.
        """

        previous = _origin("a", "0.9.0")
        upstream = _origin("b", "0.11.0")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: previous,
            ).initialize(
                target,
                flavor_selectors=("+make", "+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )

            active_work = target / "docs/roadmap/active-work.md"
            section = (
                "\n### [ ] ADOPT-002 — Adopt legacy conversion follow-through\n\n"
                "- **Owner:** project core\n"
                "- **Direction:** Finish the legacy conversion follow-through.\n"
                "- **Conclusion:** pending.\n"
                "- **Depends on:** none\n"
                "- **Implementation:**\n"
                "  - [ ] Track it here.\n"
                "- **Evidence:**\n"
                "  - [ ] Track it here.\n"
            )
            active_work_content = active_work.read_text(encoding="utf-8") + section
            active_work.write_text(active_work_content, encoding="utf-8")
            active_work_bytes = active_work_content.encode("utf-8")

            from literate_ai.documentation import markdown_anchors

            fragment = next(
                anchor
                for anchor in markdown_anchors(active_work_content)
                if anchor.startswith("adopt-002")
            )

            program = target / "docs/roadmap/native-rewrite-program.md"
            program_content = (
                "# Native rewrite program\n\n"
                "- **Status:** active\n"
                f"- **Owning queue item:** [ADOPT-002](active-work.md#{fragment})\n"
                "- **Completion / archival evidence:** pending while ADOPT-002 "
                "remains open\n\n"
                "## Detail\n\nBody text.\n"
            )
            program.write_text(program_content, encoding="utf-8")
            program_bytes = program_content.encode("utf-8")

            baseline_path = target / INITIALIZATION_BASELINE_FILE
            baseline = ProjectInitializationBaseline.from_dict(
                json.loads(baseline_path.read_text(encoding="utf-8"))
            )
            retained = {
                item.path: item
                for item in baseline.files
                if item.path
                not in (
                    "docs/roadmap/active-work.md",
                    "docs/roadmap/native-rewrite-program.md",
                )
            }
            retained["docs/roadmap/active-work.md"] = ProjectInitializationBaselineFile(
                "docs/roadmap/active-work.md",
                len(active_work_bytes),
                project_updates._identity(active_work_bytes),
            )
            retained["docs/roadmap/native-rewrite-program.md"] = (
                ProjectInitializationBaselineFile(
                    "docs/roadmap/native-rewrite-program.md",
                    len(program_bytes),
                    project_updates._identity(program_bytes),
                )
            )
            converted = ProjectInitializationBaseline(
                baseline.origin_identity,
                baseline.template_protocol,
                tuple(retained[path] for path in sorted(retained)),
            )
            baseline_path.write_bytes(canonical_json_bytes(converted.to_dict()) + b"\n")

            plan = FilesystemProjectUpdateAdapter(
                origin_provider=lambda: upstream
            ).plan(target)

            by_path = {item.path: item.classification for item in plan.files}
            self.assertEqual(
                by_path["docs/roadmap/native-rewrite-program.md"],
                ProjectUpdateClassification.PRESERVED_DYNAMIC,
            )
            self.assertEqual(
                by_path["docs/roadmap/active-work.md"],
                ProjectUpdateClassification.CONFLICT,
            )

            applied = apply_project_update(plan, target, adopt_added=True)
            self.assertNotIn("docs/roadmap/active-work.md", applied.applied)
            self.assertIn(
                "docs/roadmap/active-work.md",
                applied.refused.get(ProjectUpdateClassification.CONFLICT.value, ()),
            )


if __name__ == "__main__":
    unittest.main()
