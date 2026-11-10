"""Real current graph/model planning with injected review/framework/provider ports.

No model, verifier or generated code executes. The complete graph and private
oracle admission use current filesystem readers; project review, installed-wheel
observation and provider selection are explicit fixture boundaries.
"""

import hashlib
import io
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters.authored_qualification_planning import (
    FilesystemAuthoredQualificationPlanner,
)
from literate_ai.adapters.component_acceptance import load_versioned_library_acceptance
from literate_ai.adapters.models import InheritedSessionSelection
from literate_ai.adapters.standard_lifecycle_binding import (
    InstalledFrameworkDistribution,
    InstalledFrameworkDistributionMember,
    ResolvedStandardProjectLifecycleDriver,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts import (
    StandardProjectLifecycleDriver,
    load_current_standard_lifecycle_policy,
)
from literate_ai.contracts.authored_qualification_plans import AuthoredQualificationPlan
from literate_ai.contracts.authored_qualification_profiles import (
    AuthoredQualificationOracleSelection,
)
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.projects import ProjectConfigurationStore
from tests.critical import test_authored_qualification_oracles as oracle_fixtures
from tests.support.fixtures_test_locked_generation_authority import (
    _SELECTORS,
    _TARGET,
    _write_lock,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


class AuthoredQualificationPlanningTests(unittest.TestCase):
    def setUp(self):
        f = oracle_fixtures.AuthoredQualificationOracleTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.fixture = f
        for component in (f.root / "components").iterdir():
            path = component / "component.md"
            document, body = parse_authoring_markdown(
                path.read_bytes(), source=str(path)
            )
            document["authoring_inputs"][0]["uri"] = (
                "skills/specification-to-source/portable-application-implementation/SKILL.md"
            )
            path.write_bytes(render_authoring_markdown(document, body))
        lock = _write_lock(f.component, f.flavors)
        selections = []
        for node in sorted(lock.nodes, key=lambda n: n.revision.coordinate.uri):
            coordinate = node.revision.coordinate
            path = f.oracle_root / (coordinate.name + ".json")
            document = json.loads(path.read_bytes())
            document["specification_set_identity"] = (
                node.revision.specification_set_identity.uri
            )
            path.write_text(json.dumps(document))
            oracle = load_versioned_library_acceptance(path, coordinate.name)
            selections.append(
                AuthoredQualificationOracleSelection(coordinate, oracle.identity)
            )
        f.write_profile(replace(f.profile, oracles=tuple(selections)))
        policy = load_current_standard_lifecycle_policy()
        distribution = InstalledFrameworkDistribution(
            "literate-ai",
            "1.4.0",
            (
                InstalledFrameworkDistributionMember(
                    "literate_ai/__init__.py",
                    1,
                    "sha256:" + "a" * 64,
                ),
            ),
        )
        driver = StandardProjectLifecycleDriver(distribution.identity, policy.identity)
        self.binding = ResolvedStandardProjectLifecycleDriver(
            driver,
            distribution,
            policy,
            lambda: distribution,
            lambda: policy,
        )
        store = ProjectConfigurationStore(f.root)
        current = store.read()
        store.update(current, replace(current.definition, lifecycle_driver=driver))
        self.review_identity = canonical_identity({"fixture-reviewed-authority": 1})
        self.selection = InheritedSessionSelection(
            canonical_identity({"fixture-provider": 1})
        )
        self.reviews = []

        def review(root, *, synchronize_source_intelligence):
            self.assertFalse(synchronize_source_intelligence)
            self.reviews.append(root)
            return self.review_identity

        self.planner = FilesystemAuthoredQualificationPlanner(
            reviewer=review,
            binding_resolver=lambda driver: self.binding,
            coding_selector=lambda: self.selection,
        )
        self.request = GenerationPreparationRequest(
            f.component, _TARGET, _SELECTORS, (f.flavors,)
        )

    def plan(self, **options):
        return self.planner.plan(self.fixture.root, self.request, **options)

    def files(self):
        return {
            p.relative_to(self.fixture.root).as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in self.fixture.root.rglob("*")
            if p.is_file()
        }

    def test_plan_reads_real_all_node_models_without_allocating_or_executing(self):
        before = self.files()
        current = self.plan(pipeline_model="fixture-model")
        plan = current.plan
        self.assertEqual(len(plan.execution_plan.generation_plans), 2)
        self.assertEqual(
            plan.execution_plan.component_lock_identity,
            plan.origin.component_lock_identity,
        )
        self.assertEqual(plan.minimum_clean_runs, 2)
        self.assertEqual(plan.output_root, "litai/qualification")
        self.assertEqual(AuthoredQualificationPlan.from_dict(plan.to_dict()), plan)
        SchemaCatalog().validate(plan.SCHEMA, plan.to_dict())
        self.assertNotIn(oracle_fixtures.PRIVATE, json.dumps(plan.to_dict()))
        self.assertEqual(set(self.reviews), {self.fixture.root})
        self.assertEqual(self.files(), before)
        self.assertFalse((self.fixture.root / "litai/qualification").exists())
        self.assertFalse(hasattr(type(current), "from_dict"))

    def test_review_or_provider_change_invalidates_plan_and_changes_identity(
        self,
    ):
        for field in ("review_identity", "selection"):
            with self.subTest(field=field):
                current = self.plan()
                old = current.plan.identity
                changed = canonical_identity({"changed": field})
                setattr(
                    self,
                    field,
                    InheritedSessionSelection(changed)
                    if field == "selection"
                    else changed,
                )
                with self.assertRaisesRegex(ValueError, "plan_changed"):
                    current.require_unchanged()
                self.assertNotEqual(self.plan().plan.identity, old)

    def test_private_oracle_drift_invalidates_the_live_plan(self):
        current = self.plan()
        path = self.fixture.oracle_root / "harness.py"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            current.require_unchanged()

    def test_redirected_output_directory_refuses_without_writing(self):
        path = self.fixture.root / "litai/qualification"
        path.symlink_to(self.fixture.root / "components", target_is_directory=True)
        before = self.files()
        with self.assertRaises(UnsafeFilesystemPathError):
            self.plan()
        self.assertEqual(self.files(), before)

    def test_closed_proposal_rejects_legacy_source_history_and_weakened_runs(self):
        plan = self.plan().plan
        for field in (
            "source_snapshot_identity",
            "promotion_tree_identity",
            "authorized",
        ):
            with self.assertRaises(ValueError):
                AuthoredQualificationPlan.from_dict(
                    {**plan.to_dict(), field: "invented"}
                )
        for runs in (0, 1, True, "2"):
            with self.assertRaises(ValueError):
                replace(plan, minimum_clean_runs=runs)
        for path in (
            "../elsewhere",
            "litai/components",
            "litai/qualification/../components",
            "litai/qualification/" + "a" * 4096,
        ):
            with self.assertRaises(ValueError):
                replace(plan, output_root=path)
        with self.assertRaises(ValueError):
            replace(plan, project_reviews=())

    def test_public_cli_plan_is_read_only_through_actual_parser_and_dispatch(self):
        from literate_ai.cli import authored_qualification, dispatch

        before = self.files()
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch.object(
                authored_qualification,
                "FilesystemAuthoredQualificationPlanner",
                return_value=self.planner,
            ),
            patch.object(
                dispatch,
                "maybe_host_self_update",
                side_effect=AssertionError("host update"),
            ),
            patch.object(
                dispatch,
                "ensure_user_mcp_catalog",
                side_effect=AssertionError("catalog write"),
            ),
            patch.object(
                dispatch,
                "journal_mutagenic_event",
                side_effect=AssertionError("event write"),
            ),
        ):
            status = dispatch.main(
                [
                    "--json",
                    "project",
                    "qualify",
                    "plan",
                    "components/greeting",
                    "--project",
                    str(self.fixture.root),
                    "--target",
                    _TARGET,
                    "--flavor",
                    "+macos",
                    "--flavor",
                    "+python",
                ],
                stdout=output,
                stderr=errors,
            )
        self.assertEqual(status, 0, errors.getvalue())
        envelope = json.loads(output.getvalue())
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["command"], "project.qualify")
        plan = AuthoredQualificationPlan.from_dict(envelope["result"]["plan"])
        self.assertEqual(plan.identity.uri, envelope["result"]["plan_identity"])
        self.assertEqual(len(plan.execution_plan.generation_plans), 2)
        self.assertEqual(before, self.files())

    def test_read_only_cli_refuses_implicit_external_writer_options(self):
        from literate_ai.cli import dispatch

        for options in (["--discover-mcps"], ["--debug", "trace.json"]):
            with self.subTest(options=options):
                output = io.StringIO()
                errors = io.StringIO()
                with patch.object(
                    dispatch,
                    "maybe_host_self_update",
                    side_effect=AssertionError("host update"),
                ):
                    status = dispatch.main(
                        [
                            "--json",
                            *options,
                            "project",
                            "qualify",
                            "plan",
                            "components/greeting",
                            "--project",
                            str(self.fixture.root),
                        ],
                        stdout=output,
                        stderr=errors,
                    )
                self.assertNotEqual(status, 0)
                self.assertEqual(
                    json.loads(errors.getvalue())["error"]["code"],
                    "qualification.authored.scoped_options",
                )
