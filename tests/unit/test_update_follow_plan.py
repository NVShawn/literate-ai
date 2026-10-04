from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.project_updates import FilesystemProjectUpdateAdapter
from literate_ai.adapters.repository_catalogs import (
    InheritedCatalogFile,
    InheritedCatalogItem,
    InheritedCatalogPlan,
)
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.project import update_project_from_args
from literate_ai.contracts import (
    RepositoryLineage,
    RepositoryParentReference,
    RepositoryParentSelection,
)
from tests.unit.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)
from tests.support.fixtures_test_repository_lineage import fixture, identity
from tests.support.fixtures_test_repository_updates import origin


class UpdateFollowPlanTests(unittest.TestCase):
    def test_follow_plan_reports_same_skill_changes_as_apply_without_writes(self):
        selection, ancestor, parent, previous = fixture()
        following = RepositoryParentSelection.inherit(
            (RepositoryParentReference(parent.repository_url, "next"),)
        )
        new_parent = replace(
            parent,
            requested_revision="next",
            resolved_revision="d" * 40,
            project_identity=identity("d"),
        )
        prospective = RepositoryLineage(
            following, (ancestor, new_parent), (new_parent.identity,)
        )

        def skill(node, name):
            content = (
                f"---\nname: {name}\n"
                "description: Fixture skill for update planning.\n---\n\n"
                f"# {name}\n\nPreserve reviewed application behavior.\n"
            ).encode()
            return InheritedCatalogItem(
                "skill",
                name,
                node,
                (InheritedCatalogFile(f"skills/{name}/SKILL.md", content, False),),
            )

        old_items = (skill(parent, "follow-a"), skill(parent, "follow-conflict"))
        new_items = tuple(
            replace(
                item,
                source=new_parent,
                files=tuple(
                    replace(f, content=f.content + b"\n<!-- upstream B -->\n")
                    for f in item.files
                ),
            )
            for item in old_items
        )
        added = skill(new_parent, "follow-added")
        old_catalog = InheritedCatalogPlan(previous, old_items)
        new_catalog = InheritedCatalogPlan(prospective, (*new_items, added))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _: previous,
                repository_catalog_planner=lambda _: old_catalog,
            ).initialize(
                root,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            conflict = root / old_items[1].files[0].destination
            conflict.write_bytes(conflict.read_bytes() + b"\n<!-- local change -->\n")
            before = {
                p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }
            args = Namespace(
                path=root,
                apply=False,
                adopt_added=True,
                record_work_items=False,
                unpin=False,
                follow_ref="next",
                allow_major=False,
                review_conflicts=False,
            )
            with (
                patch(
                    "literate_ai.cli.project._repository_fetch_provider",
                    return_value=SimpleNamespace(
                        deadline_evidence={"fixture": True},
                        update_blobs=lambda *_args: {},
                    ),
                ),
                patch(
                    "literate_ai.cli.project.resolve_repository_lineage",
                    side_effect=lambda s, _provider: (
                        prospective if s == following else previous
                    ),
                ),
                patch(
                    "literate_ai.cli.project.plan_inherited_catalogs",
                    side_effect=lambda lineage, _provider: (
                        new_catalog if lineage == prospective else old_catalog
                    ),
                ),
                patch(
                    "literate_ai.cli.project.FilesystemProjectUpdateAdapter",
                    side_effect=lambda **kw: FilesystemProjectUpdateAdapter(
                        origin_provider=origin, **kw
                    ),
                ),
                patch("literate_ai.cli.project.validate_project", return_value={}),
            ):
                plan = update_project_from_args(args)
                self.assertEqual(
                    before,
                    {
                        p.relative_to(root).as_posix(): p.read_bytes()
                        for p in root.rglob("*")
                        if p.is_file()
                    },
                )
                self.assertEqual(
                    FilesystemRepositoryLineageStore(root).load(), (selection, previous)
                )
                files = {f["path"]: f for f in plan["repository_lineage"]["files"]}
                self.assertEqual(
                    files[old_items[0].files[0].destination]["classification"],
                    "upstream-only",
                )
                self.assertEqual(
                    files[old_items[1].files[0].destination]["classification"],
                    "conflict",
                )
                self.assertEqual(
                    files[added.files[0].destination]["classification"],
                    "upstream-added",
                )
                args.apply = True
                applied = update_project_from_args(args)
            self.assertEqual(
                plan["repository_lineage"]["files"],
                applied["repository_lineage"]["files"],
            )
            self.assertEqual(
                plan["repository_lineage"]["identity"],
                applied["repository_lineage"]["identity"],
            )
            self.assertEqual(plan["parent_selectors"], applied["parent_selectors"])
            for before_file, after_file in zip(
                plan["framework"]["files"], applied["framework"]["files"], strict=True
            ):
                self.assertEqual(
                    {k: v for k, v in before_file.items() if k != "local_identity"},
                    {k: v for k, v in after_file.items() if k != "local_identity"},
                )
                if before_file["local_identity"] != after_file["local_identity"]:
                    self.assertEqual(before_file["classification"], "preserved-dynamic")
                    self.assertIn(
                        before_file["path"],
                        {
                            ".literate/imports.json",
                            ".literate/repository-lineage.json",
                            ".literate/repository-parent.json",
                        },
                    )
            self.assertEqual(
                conflict.read_bytes(), before[conflict.relative_to(root).as_posix()]
            )
            self.assertEqual(
                (root / new_items[0].files[0].destination).read_bytes(),
                new_items[0].files[0].content,
            )
            self.assertEqual(
                (root / added.files[0].destination).read_bytes(), added.files[0].content
            )

    def test_prospective_plan_refuses_stale_project_parent_and_remote_authority(self):
        from literate_ai.adapters.project_reparenting import (
            FilesystemRepositoryReparentAdapter,
        )
        from literate_ai.adapters.repository_updates import (
            FilesystemRepositoryUpdateAdapter,
            RepositoryUpdateError,
        )

        selection, ancestor, parent, previous = fixture()
        following = RepositoryParentSelection.inherit(
            (RepositoryParentReference(parent.repository_url, "next"),)
        )
        new_parent = replace(
            parent,
            requested_revision="next",
            resolved_revision="d" * 40,
            project_identity=identity("d"),
        )
        prospective = RepositoryLineage(
            following, (ancestor, new_parent), (new_parent.identity,)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
                repository_lineage_resolver=lambda _: previous,
                repository_catalog_planner=lambda _: InheritedCatalogPlan(previous, ()),
            ).initialize(
                root,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
                parent_selection=selection,
            )
            plan = FilesystemRepositoryReparentAdapter(
                resolver=lambda _: prospective
            ).plan(root, following)
            before = {
                p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }
            changed_parent = replace(new_parent, resolved_revision="e" * 40)
            changed_remote = RepositoryLineage(
                following, (ancestor, changed_parent), (changed_parent.identity,)
            )
            for name, supplied, resolved in (
                (
                    "foreign-project",
                    replace(plan, project_identity=identity("f")),
                    prospective,
                ),
                (
                    "stale-previous-authority",
                    replace(
                        plan, previous_selection=following, previous_lineage=prospective
                    ),
                    prospective,
                ),
                ("remote-drift", plan, changed_remote),
            ):
                with self.subTest(name=name):
                    adapter = FilesystemRepositoryUpdateAdapter(
                        lineage_resolver=lambda _, current=resolved: current,
                        catalog_planner=lambda lineage: InheritedCatalogPlan(
                            lineage, ()
                        ),
                    )
                    with self.assertRaises(RepositoryUpdateError) as raised:
                        adapter.plan(root, reparent_plan=supplied)
                    self.assertEqual(
                        raised.exception.code, "repository_update.changed_after_plan"
                    )
                    self.assertEqual(
                        before,
                        {
                            p.relative_to(root).as_posix(): p.read_bytes()
                            for p in root.rglob("*")
                            if p.is_file()
                        },
                    )

    def test_exact_git_revisions_plan_and_apply_the_same_skill_changes(self):
        self._prove_real_git_follow(empty=True)

    def test_real_git_follow_preserves_a_starter_component(self):
        self._prove_real_git_follow(empty=False)

    def _prove_real_git_follow(self, *, empty):
        import os

        from literate_ai.adapters import project_initialization as init_module
        from literate_ai.adapters.repository_lineage import repository_parent_reference
        from tests.support.fixtures_test_repository_initialization_e2e import (
            commit_project,
            git,
        )

        go_skill = "skills/specification-to-source/go-portable-application/SKILL.md"
        legacy_templates = {
            k: v for k, v in init_module._TEMPLATE_FILES.items() if k != go_skill
        }

        def initialize(target, selection):
            # Build the A-side with the pre-repair distribution's exact omission.
            with patch.dict(init_module._TEMPLATE_FILES, legacy_templates, clear=True):
                return FilesystemProjectInitializationAdapter(
                    standard_binding_provider=lambda: None,
                    initialization_origin_provider=origin,
                ).initialize(
                    target,
                    flavor_selectors=("+python", "+macos"),
                    source_intelligence_provider="none",
                    parent_selection=selection,
                    empty=empty,
                )

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            parent = temporary / "parent"
            child = temporary / "derived"
            with patch.dict(os.environ, {"OBJ_DIR": str(temporary / "objects")}):
                initialize(parent, RepositoryParentSelection.root())

                def write_skill(name, text):
                    p = parent / "skills" / name / "SKILL.md"
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(
                        f"---\nname: {name}\n"
                        "description: Local Git update fixture.\n---\n\n"
                        f"# {name}\n\n{text}\n",
                        encoding="utf-8",
                        newline="\n",
                    )
                    return p.relative_to(parent).as_posix()

                changed = write_skill("git-follow-change", "Revision A.")
                conflict = write_skill("git-follow-conflict", "Revision A.")
                commit_project(parent)
                revision_a = git(parent, "rev-parse", "HEAD")
                initialize(
                    child,
                    RepositoryParentSelection.inherit(
                        (repository_parent_reference(str(parent) + "#" + revision_a),)
                    ),
                )
                local = child / conflict
                local.write_text(
                    local.read_text() + "\nLocal ownership.\n",
                    encoding="utf-8",
                    newline="\n",
                )
                write_skill("git-follow-change", "Revision B.")
                write_skill("git-follow-conflict", "Upstream revision B.")
                added = write_skill("git-follow-added", "Added by B.")
                git(parent, "add", "skills")
                git(parent, "commit", "-m", "Advance parent skills to B")
                revision_b = git(parent, "rev-parse", "HEAD")
                before = {
                    p.relative_to(child).as_posix(): p.read_bytes()
                    for p in child.rglob("*")
                    if p.is_file()
                }
                args = Namespace(
                    path=child,
                    apply=False,
                    adopt_added=True,
                    record_work_items=False,
                    unpin=False,
                    follow_ref=revision_b,
                    allow_major=False,
                    review_conflicts=False,
                )
                with patch(
                    "literate_ai.cli.project.FilesystemProjectUpdateAdapter",
                    side_effect=lambda **kw: FilesystemProjectUpdateAdapter(
                        origin_provider=origin, **kw
                    ),
                ):
                    plan = update_project_from_args(args)
                    self.assertEqual(
                        before,
                        {
                            p.relative_to(child).as_posix(): p.read_bytes()
                            for p in child.rglob("*")
                            if p.is_file()
                        },
                    )
                    files = {f["path"]: f for f in plan["repository_lineage"]["files"]}
                    self.assertEqual(files[changed]["classification"], "upstream-only")
                    self.assertEqual(files[conflict]["classification"], "conflict")
                    self.assertEqual(files[added]["classification"], "upstream-added")
                    framework_files = {f["path"]: f for f in plan["framework"]["files"]}
                    self.assertEqual(
                        framework_files[go_skill]["classification"], "upstream-added"
                    )
                    args.apply = True
                    if empty:
                        original_template = init_module._template_text

                        def incomplete_template(resource):
                            content = original_template(resource)
                            return (
                                content.replace(
                                    "go-portable-application", "missing-go-authority"
                                )
                                if resource == "flavors/lang-go/flavor.md"
                                else content
                            )

                        with patch(
                            "literate_ai.adapters.project_updates._template_text",
                            side_effect=incomplete_template,
                        ):
                            with self.assertRaises(CliFailure) as rejected:
                                update_project_from_args(args)
                        self.assertIn("validation", str(rejected.exception))
                        self.assertEqual(
                            before,
                            {
                                p.relative_to(child).as_posix(): p.read_bytes()
                                for p in child.rglob("*")
                                if p.is_file()
                            },
                        )
                    applied = update_project_from_args(args)
                self.assertTrue((child / go_skill).is_file())
                self.assertEqual(
                    plan["repository_lineage"]["files"],
                    applied["repository_lineage"]["files"],
                )
                self.assertEqual(
                    plan["repository_lineage"]["identity"],
                    applied["repository_lineage"]["identity"],
                )
                self.assertEqual(plan["parent_selectors"], applied["parent_selectors"])
                self.assertEqual(
                    (child / changed).read_bytes(), (parent / changed).read_bytes()
                )
                self.assertEqual(
                    (child / added).read_bytes(), (parent / added).read_bytes()
                )
                self.assertEqual(local.read_bytes(), before[conflict])
                self.assertEqual(
                    FilesystemRepositoryLineageStore(child)
                    .load()[0]
                    .parents[0]
                    .requested_revision,
                    revision_b,
                )

    def test_explicit_go_initialization_includes_its_authoring_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "go-project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=origin,
            ).initialize(
                target,
                flavor_selectors=("+go", "+linux"),
                empty=True,
                source_intelligence_provider="none",
                parent_selection=RepositoryParentSelection.root(),
            )
            self.assertTrue((target / "flavors/lang-go/flavor.md").is_file())
            self.assertTrue(
                (
                    target
                    / "skills/specification-to-source/go-portable-application/SKILL.md"
                ).is_file()
            )
