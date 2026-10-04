"""WebMCP, front-end, back-end, and sample-stack catalog contracts."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.component_lock_planning import (
    ComponentLockPlanningError,
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.adapters.flavor_markdown import parse_flavor_markdown
from literate_ai.adapters.models.coding_cli import (
    CodingCliError,
    RecipeDocument,
    RecipeFlavor,
    RecipeSkill,
    _resolved_recipe_skills,
)
from literate_ai.adapters.project_initialization import (
    KNOWN_FLAVOR_SELECTORS,
    FilesystemProjectInitializationAdapter,
    canonical_flavor_coordinate,
)
from literate_ai.adapters.project_mcp import assert_generation_skills_are_mcp_free
from literate_ai.contracts import (
    BACKEND_PARENT_SKILL_ID,
    SAMPLE_HARNESS_ENTRYPOINT_KINDS,
    WEB_APPLICATION_ENTRYPOINT_KIND,
    CandidateStatus,
    ContentIdentity,
    ContentReference,
    FlavorAxis,
    HashAlgorithm,
    ProjectInitializationOrigin,
    RepositoryParentSelection,
    index_backend_ecosystems,
)
from literate_ai.contracts.project_mcp import generation_skill_requires_operator_mcp
from tests.conformance.support.sample_runner import _matrix_languages
from tests.support.fixtures_test_component_lock_planning import _fixture

REPO = Path(__file__).resolve().parents[2]
CATALOG = REPO / "skills" / "specification-to-source"
TEMPLATE = (
    REPO
    / "src"
    / "literate_ai"
    / "project_template"
    / "skills"
    / "specification-to-source"
)
_STACK_SKILLS = (
    "mcp-application/SKILL.md",
    "mcp-application/webmcp/SKILL.md",
    "frontend-application/SKILL.md",
    "frontend-application/react-application/SKILL.md",
    "frontend-application/react-dashboard-application/SKILL.md",
    "backend-application/SKILL.md",
    "backend-application/python-service-application/SKILL.md",
    "backend-application/rust-service-application/SKILL.md",
    "backend-application/scheduler-lease-worker/SKILL.md",
    "backend-application/durable-split-service/SKILL.md",
    "backend-application/rest-application/SKILL.md",
    "backend-application/grpc-application/SKILL.md",
)


def _skill(relative: str) -> RecipeSkill:
    path = CATALOG.joinpath(*relative.split("/"))
    content = path.read_bytes()
    reference = ContentReference(
        "specification-to-source-skill",
        path.relative_to(REPO).as_posix(),
        ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()),
    )
    return RecipeSkill.from_reference(reference, content, source=path.as_posix())


def _language_flavor(value: str) -> RecipeFlavor:
    return RecipeFlavor(
        flavor_id=f"lang-{value}",
        axis=FlavorAxis.IMPLEMENTATION_LANGUAGE_ECOSYSTEM,
        value=value,
        documents=(
            RecipeDocument.create(f"flavors/{value}/openspec/spec.md", "# spec\n"),
        ),
        coordinate_uri=f"flavor://literate-ai/lang-{value}",
    )


class AppStackSkillCatalogTests(unittest.TestCase):
    def test_catalog_and_template_copies_are_byte_identical(self) -> None:
        for relative in _STACK_SKILLS:
            catalog = CATALOG.joinpath(*relative.split("/"))
            template = TEMPLATE.joinpath(*relative.split("/"))
            with self.subTest(skill=relative):
                self.assertEqual(catalog.read_bytes(), template.read_bytes())
        react_flavor = REPO / "flavors" / "ui-react" / "flavor.md"
        template_flavor = (
            REPO
            / "src"
            / "literate_ai"
            / "project_template"
            / "flavors"
            / "ui-react"
            / "flavor.md"
        )
        self.assertEqual(react_flavor.read_bytes(), template_flavor.read_bytes())
        javascript = REPO / "flavors" / "lang-javascript" / "flavor.md"
        template_javascript = (
            REPO
            / "src"
            / "literate_ai"
            / "project_template"
            / "flavors"
            / "lang-javascript"
            / "flavor.md"
        )
        self.assertEqual(javascript.read_bytes(), template_javascript.read_bytes())
        npm = REPO / "flavors" / "package-npm" / "flavor.md"
        template_npm = (
            REPO
            / "src"
            / "literate_ai"
            / "project_template"
            / "flavors"
            / "package-npm"
            / "flavor.md"
        )
        self.assertEqual(npm.read_bytes(), template_npm.read_bytes())

    def test_webmcp_requires_the_mcp_parent_identity(self) -> None:
        parent = _skill("mcp-application/SKILL.md")
        child = _skill("mcp-application/webmcp/SKILL.md")
        self.assertEqual(
            {item.skill_id for item in child.dependencies},
            {"mcp-application", "frontend-application"},
        )
        self.assertEqual(
            next(
                item.identity.digest
                for item in child.dependencies
                if item.skill_id == "mcp-application"
            ),
            parent.content_identity.digest,
        )
        resolved = _resolved_recipe_skills(
            (
                _skill("mcp-application/SKILL.md"),
                _skill("frontend-application/SKILL.md"),
                child,
            ),
            (),
        )
        self.assertEqual(
            [item.skill_id for item in resolved],
            ["mcp-application", "frontend-application", "webmcp"],
        )
        with self.assertRaises(CodingCliError) as raised:
            _resolved_recipe_skills((child,), ())
        self.assertEqual(raised.exception.code, "coding_cli.skill_dependency_missing")

    def test_scheduler_lease_worker_pins_backend_parent_and_is_a_delta(self) -> None:
        parent = _skill("backend-application/SKILL.md")
        child = _skill("backend-application/scheduler-lease-worker/SKILL.md")
        self.assertEqual(
            {item.skill_id for item in child.dependencies},
            {"backend-application"},
        )
        self.assertEqual(
            next(
                item.identity.digest
                for item in child.dependencies
                if item.skill_id == "backend-application"
            ),
            parent.content_identity.digest,
        )
        resolved = _resolved_recipe_skills((parent, child), ())
        self.assertEqual(
            [item.skill_id for item in resolved],
            ["backend-application", "scheduler-lease-worker"],
        )
        text = CATALOG.joinpath(
            "backend-application", "scheduler-lease-worker", "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("delta of `backend-application`", text)
        self.assertNotIn("Unselected language ecosystems are omitted", text)
        self.assertIn("injected clock", text)
        self.assertIn("injected random source", text)
        self.assertIn("single-writer lease", text)

    def test_durable_split_service_pins_backend_parent_and_is_a_delta(self) -> None:
        parent = _skill("backend-application/SKILL.md")
        child = _skill("backend-application/durable-split-service/SKILL.md")
        self.assertEqual(
            {item.skill_id for item in child.dependencies},
            {"backend-application"},
        )
        self.assertEqual(
            next(
                item.identity.digest
                for item in child.dependencies
                if item.skill_id == "backend-application"
            ),
            parent.content_identity.digest,
        )
        resolved = _resolved_recipe_skills((parent, child), ())
        self.assertEqual(
            [item.skill_id for item in resolved],
            ["backend-application", "durable-split-service"],
        )
        text = CATALOG.joinpath(
            "backend-application", "durable-split-service", "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("delta of `backend-application`", text)
        self.assertNotIn("Unselected language ecosystems are omitted", text)
        # References the other three boundaries by their skills, does not reinvent.
        self.assertIn("frontend-application", text)
        self.assertIn("scheduler-lease-worker", text)
        self.assertIn("single writer", text.lower())
        self.assertIn("read-only connection", text.lower())

    def test_rest_application_pins_backend_parent_and_is_an_ipc_adapter(self) -> None:
        parent = _skill("backend-application/SKILL.md")
        child = _skill("backend-application/rest-application/SKILL.md")
        self.assertEqual(
            {item.skill_id for item in child.dependencies},
            {"backend-application"},
        )
        self.assertEqual(
            next(
                item.identity.digest
                for item in child.dependencies
                if item.skill_id == "backend-application"
            ),
            parent.content_identity.digest,
        )
        resolved = _resolved_recipe_skills((parent, child), ())
        self.assertEqual(
            [item.skill_id for item in resolved],
            ["backend-application", "rest-application"],
        )
        text = CATALOG.joinpath(
            "backend-application", "rest-application", "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("delta of `backend-application`", text)
        self.assertNotIn("Unselected language ecosystems are omitted", text)
        # References the IPC-surface core so REST cannot drift from the contract.
        self.assertIn("ipc-surface-conformance-acceptance@1", text)
        self.assertIn('`protocol` set to `"rest"`', text)
        self.assertIn("/openapi.json", text)
        self.assertIn("OpenAPI 3.x", text)
        self.assertIn("CompatibilityPromise", text)
        # REST is an adapter of the unified surface, not a standalone protocol.
        self.assertIn("adapter of the unified IPC surface", text)

    def test_initialized_grpc_skill_resolves_its_backend_parent(self) -> None:
        origin = ProjectInitializationOrigin(
            repository_url="ssh://git.example.test/fixture/literate-ai.git",
            git_revision="a" * 40,
            distribution_name="literate-ai",
            distribution_version="1.1.0",
        )
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "project"
            FilesystemProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: origin,
            ).initialize(
                target,
                bootstrap_tools=False,
                source_intelligence_provider="none",
                flavor_selectors=("+bazel", "+python", "+macos"),
                parent_selection=RepositoryParentSelection.root(),
            )
            skills = []
            for relative in (
                "backend-application/SKILL.md",
                "backend-application/grpc-application/SKILL.md",
            ):
                path = target / "skills/specification-to-source" / relative
                content = path.read_bytes()
                self.assertEqual(content, (CATALOG / relative).read_bytes())
                reference = ContentReference(
                    "specification-to-source-skill",
                    str(path.relative_to(target)),
                    ContentIdentity(
                        HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()
                    ),
                )
                skills.append(
                    RecipeSkill.from_reference(reference, content, source=str(path))
                )
            resolved = _resolved_recipe_skills(tuple(skills), ())
            self.assertEqual(
                [item.skill_id for item in resolved],
                ["backend-application", "grpc-application"],
            )
            self.assertEqual(skills[-1].version, "1.0.2")

    def test_grpc_application_pins_backend_parent_and_is_an_ipc_adapter(self) -> None:
        parent = _skill("backend-application/SKILL.md")
        child = _skill("backend-application/grpc-application/SKILL.md")
        self.assertEqual(
            {item.skill_id for item in child.dependencies},
            {"backend-application"},
        )
        self.assertEqual(
            next(
                item.identity.digest
                for item in child.dependencies
                if item.skill_id == "backend-application"
            ),
            parent.content_identity.digest,
        )
        resolved = _resolved_recipe_skills((parent, child), ())
        self.assertEqual(
            [item.skill_id for item in resolved],
            ["backend-application", "grpc-application"],
        )
        text = CATALOG.joinpath(
            "backend-application", "grpc-application", "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("delta of `backend-application`", text)
        self.assertNotIn("Unselected language ecosystems are omitted", text)
        # References the IPC-surface core so gRPC cannot drift from the contract.
        self.assertIn("ipc-surface-conformance-acceptance@2", text)
        self.assertIn('`protocol` set to `"grpc"`', text)
        self.assertIn("server reflection", text)
        self.assertIn("FileDescriptorSet", text)
        self.assertIn("CompatibilityPromise", text)
        # gRPC is an adapter of the unified surface, not a standalone protocol.
        self.assertIn("adapter of the unified IPC surface", text)

    def test_generation_skills_do_not_require_operator_mcp(self) -> None:
        assert_generation_skills_are_mcp_free((REPO / "skills",))
        for relative in _STACK_SKILLS:
            text = CATALOG.joinpath(*relative.split("/")).read_text(encoding="utf-8")
            with self.subTest(skill=relative):
                self.assertFalse(generation_skill_requires_operator_mcp(text))
        self.assertTrue(
            generation_skill_requires_operator_mcp(
                "Write tools into ~/.config/literate-ai during generation.\n"
            )
        )

    def test_nested_skills_are_deltas(self) -> None:
        webmcp = CATALOG.joinpath("mcp-application", "webmcp", "SKILL.md").read_text(
            encoding="utf-8"
        )
        python_service = CATALOG.joinpath(
            "backend-application", "python-service-application", "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Hygiene this parent owns", webmcp)
        self.assertNotIn("One protocol, four distinct leaves", webmcp)
        self.assertIn("Do not copy MCP hygiene", webmcp)
        self.assertNotIn("Unselected language ecosystems are omitted", python_service)
        self.assertIn("delta of `backend-application`", python_service)

    def test_frontend_skills_require_browser_observable_verification(self) -> None:
        frontend = CATALOG.joinpath("frontend-application", "SKILL.md").read_text(
            encoding="utf-8"
        )
        dashboard = CATALOG.joinpath(
            "frontend-application", "react-dashboard-application", "SKILL.md"
        ).read_text(encoding="utf-8")
        browser_path = REPO.joinpath(
            "skills", "agent", "verify-frontend-browser", "SKILL.md"
        )
        template_browser = REPO.joinpath(
            "src",
            "literate_ai",
            "project_template",
            "skills",
            "agent",
            "verify-frontend-browser",
            "SKILL.md",
        )

        self.assertIn("stable accessible names", frontend)
        self.assertIn("never render `NaN`, `Infinity`", frontend)
        self.assertIn("Browser-observable dashboard state", dashboard)
        self.assertIn(
            "desktop and one narrow mobile viewport",
            browser_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(browser_path.read_bytes(), template_browser.read_bytes())


class BackendEcosystemIndexTests(unittest.TestCase):
    def test_selected_python_injects_python_and_omits_rust(self) -> None:
        index = index_backend_ecosystems(
            recipe_skill_ids=(BACKEND_PARENT_SKILL_ID,),
            selected_language_targets=("python",),
            catalog_flavor_ids=("lang-python", "lang-rust"),
        )
        self.assertEqual(index.injected, ("python-service-application",))
        self.assertEqual(index.omitted, ("rust-service-application",))
        self.assertEqual(
            index.skips, (("elixir", "lang-elixir Flavor is not in the catalog"),)
        )

    def test_selected_rust_injects_rust(self) -> None:
        index = index_backend_ecosystems(
            recipe_skill_ids=(BACKEND_PARENT_SKILL_ID,),
            selected_language_targets=("rust",),
            catalog_flavor_ids=("lang-rust",),
        )
        self.assertEqual(index.injected, ("rust-service-application",))
        self.assertIn("python-service-application", index.omitted)

    def test_missing_elixir_is_a_named_skip(self) -> None:
        index = index_backend_ecosystems(
            recipe_skill_ids=(BACKEND_PARENT_SKILL_ID,),
            selected_language_targets=("python",),
            catalog_flavor_ids=("lang-python",),
        )
        self.assertEqual(
            index.skips, (("elixir", "lang-elixir Flavor is not in the catalog"),)
        )

    def test_recipe_drops_unselected_backend_ecosystem(self) -> None:
        skills = (
            _skill("mcp-application/SKILL.md"),
            _skill("backend-application/SKILL.md"),
            _skill("backend-application/python-service-application/SKILL.md"),
            _skill("backend-application/rust-service-application/SKILL.md"),
        )
        resolved = _resolved_recipe_skills(skills, (_language_flavor("python"),))
        self.assertEqual(
            [item.skill_id for item in resolved],
            [
                "mcp-application",
                "backend-application",
                "python-service-application",
            ],
        )


class FrontendFlavorAxisTests(unittest.TestCase):
    def test_javascript_provides_web_frontend_and_python_does_not(self) -> None:
        javascript = parse_flavor_markdown(
            (REPO / "flavors" / "lang-javascript" / "flavor.md").read_bytes(),
            source="flavors/lang-javascript/flavor.md",
        )
        python = parse_flavor_markdown(
            (REPO / "flavors" / "lang-python" / "flavor.md").read_bytes(),
            source="flavors/lang-python/flavor.md",
        )
        self.assertIn("application.web-frontend", javascript.applicable_capabilities)
        self.assertNotIn("application.web-frontend", python.applicable_capabilities)

    def test_ui_react_alias_and_javascript_constraint(self) -> None:
        self.assertEqual(
            canonical_flavor_coordinate("lang-javascript-react"),
            "flavor://literate-ai/ui-react",
        )
        self.assertEqual(
            KNOWN_FLAVOR_SELECTORS.get("lang-javascript-react"),
            "implementation.ui-framework",
        )
        self.assertEqual(
            KNOWN_FLAVOR_SELECTORS.get("ui-react"),
            "implementation.ui-framework",
        )
        authoring = parse_flavor_markdown(
            (REPO / "flavors" / "ui-react" / "flavor.md").read_bytes(),
            source="flavors/ui-react/flavor.md",
        )
        self.assertEqual(authoring.primary_axis, FlavorAxis.IMPLEMENTATION_UI_FRAMEWORK)
        constraint = authoring.secondary_constraints[0]
        self.assertEqual(constraint["axis"], "implementation.language-ecosystem")
        self.assertEqual(constraint["value"], "javascript")
        self.assertFalse(constraint["optional"])
        self.assertEqual(
            authoring.authoring_inputs[0].uri,
            "../../skills/specification-to-source/frontend-application/"
            "react-application/SKILL.md",
        )

    def test_frontend_parent_without_javascript_fails_closed(self) -> None:
        with self.assertRaises(ComponentLockPlanningError) as raised:
            FilesystemComponentLockPlanner().plan(
                REPO / "components" / "frontend-application",
                target_name="macos-host",
                flavor_selectors=("+python", "+macos", "+make"),
                flavor_roots=(REPO / "flavors",),
            )
        self.assertIn(
            raised.exception.code,
            {
                "component_lock.flavor_cardinality",
                "component_lock.flavor_selector_noop",
            },
        )

    def test_ui_react_without_javascript_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            shutil.copytree(REPO / "flavors" / "ui-react", flavors / "ui-react")
            skill = (
                REPO
                / "skills"
                / "specification-to-source"
                / "frontend-application"
                / "react-application"
            )
            destination = (
                Path(temporary)
                / "skills"
                / "specification-to-source"
                / "frontend-application"
                / "react-application"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill, destination)
            package_skill = REPO / "skills" / "agent" / "package-artifacts"
            package_destination = (
                Path(temporary) / "skills" / "agent" / "package-artifacts"
            )
            package_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(package_skill, package_destination)
            manifest = component / "component.md"
            text = manifest.read_text(encoding="utf-8")
            text = text.replace(
                "    capability_contract: sample.portable-app\n  - slot_id: os",
                "    capability_contract: sample.portable-app\n"
                "  - slot_id: ui\n"
                "    axis: implementation.ui-framework\n"
                "    cardinality: zero-or-one\n"
                "    capability_contract: application.web-frontend\n"
                "  - slot_id: os",
            )
            manifest.write_text(text, encoding="utf-8", newline="\n")
            with self.assertRaises(ComponentLockPlanningError) as raised:
                FilesystemComponentLockPlanner().plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python", "+ui-react"),
                    flavor_roots=(flavors,),
                )
            self.assertIn(
                raised.exception.code,
                {
                    "component_lock.flavor_corequisite_missing",
                    "component_lock.flavor_target_unsatisfied",
                },
            )


class SampleAppStackTests(unittest.TestCase):
    def test_unpinned_languages_default_to_python(self) -> None:
        self.assertEqual(_matrix_languages(()), ("python",))
        self.assertEqual(_matrix_languages(("+make", "+macos")), ("python",))

    def test_explicit_javascript_pin_stays_javascript(self) -> None:
        self.assertEqual(_matrix_languages(("+lang-javascript",)), ("javascript",))
        self.assertEqual(
            _matrix_languages(("+lang-javascript", "+ui-react")),
            ("javascript",),
        )
        self.assertEqual(_matrix_languages(("+lang-javascript-react",)), ("python",))
        self.assertEqual(
            _matrix_languages(("+lang-javascript", "+lang-javascript-react")),
            ("javascript",),
        )

    def test_frontend_and_react_skills_name_the_npm_package_flavor(self) -> None:
        frontend = CATALOG.joinpath("frontend-application", "SKILL.md").read_text(
            encoding="utf-8"
        )
        react = CATALOG.joinpath(
            "frontend-application", "react-application", "SKILL.md"
        ).read_text(encoding="utf-8")
        javascript = (
            REPO / "flavors" / "lang-javascript" / "openspec" / "spec.md"
        ).read_text(encoding="utf-8")
        self.assertIn("package-npm", frontend)
        self.assertIn("package-npm", react)
        self.assertIn("package-npm", javascript)
        self.assertIn("lockfile-pinned", frontend)
        self.assertNotIn("SHALL use no npm package", javascript)

    def test_package_npm_requires_javascript_and_contributes_ecosystem_skill(
        self,
    ) -> None:
        self.assertEqual(
            KNOWN_FLAVOR_SELECTORS.get("package-npm"),
            "packaging",
        )
        self.assertEqual(
            canonical_flavor_coordinate("npm"),
            "flavor://literate-ai/package-npm",
        )
        authoring = parse_flavor_markdown(
            (REPO / "flavors" / "package-npm" / "flavor.md").read_bytes(),
            source="flavors/package-npm/flavor.md",
        )
        self.assertEqual(authoring.primary_axis, FlavorAxis.PACKAGING)
        constraint = authoring.secondary_constraints[0]
        self.assertEqual(constraint["axis"], "implementation.language-ecosystem")
        self.assertEqual(constraint["value"], "javascript")
        self.assertFalse(constraint["optional"])
        self.assertEqual(
            authoring.authoring_inputs[1].uri,
            "../../skills/specification-to-source/javascript-ecosystem/SKILL.md",
        )
        spec = (REPO / "flavors" / "package-npm" / "openspec" / "spec.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Detect before install", spec)
        self.assertIn("package-lock.json", spec)

    def test_package_npm_without_javascript_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            shutil.copytree(REPO / "flavors" / "package-npm", flavors / "package-npm")
            skill = REPO / "skills" / "specification-to-source" / "javascript-ecosystem"
            destination = (
                Path(temporary)
                / "skills"
                / "specification-to-source"
                / "javascript-ecosystem"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill, destination)
            package_skill = REPO / "skills" / "agent" / "package-artifacts"
            package_destination = (
                Path(temporary) / "skills" / "agent" / "package-artifacts"
            )
            package_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(package_skill, package_destination)
            manifest = component / "component.md"
            text = manifest.read_text(encoding="utf-8")
            text = text.replace(
                "    capability_contract: sample.portable-app\n  - slot_id: os",
                "    capability_contract: sample.portable-app\n"
                "  - slot_id: packages\n"
                "    axis: packaging\n"
                "    cardinality: zero-or-one\n"
                "    capability_contract: sample.portable-app\n"
                "  - slot_id: os",
            )
            manifest.write_text(text, encoding="utf-8", newline="\n")
            with self.assertRaises(ComponentLockPlanningError) as raised:
                FilesystemComponentLockPlanner().plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python", "+package-npm"),
                    flavor_roots=(flavors,),
                )
            self.assertIn(
                raised.exception.code,
                {
                    "component_lock.flavor_corequisite_missing",
                    "component_lock.flavor_target_unsatisfied",
                },
            )

    def test_package_npm_with_javascript_locks(self) -> None:
        plan = FilesystemComponentLockPlanner().plan(
            REPO / "samples" / "frontend-base",
            target_name="macos-host",
            flavor_selectors=(
                "+lang-javascript",
                "+package-npm",
                "+macos",
            ),
            flavor_roots=(REPO / "flavors",),
        )
        selected = {
            item.value
            for node in plan.nodes
            for item in node.flavor_candidates
            if item.status is CandidateStatus.SELECTED
        }
        self.assertIn("npm", selected)
        self.assertIn("javascript", selected)

    def test_react_dashboard_locks_under_ui_react(self) -> None:
        plan = FilesystemComponentLockPlanner().plan(
            REPO / "samples" / "react-dashboard-example",
            target_name="macos-host",
            flavor_selectors=(
                "+lang-javascript",
                "+ui-react",
                "+macos",
            ),
            flavor_roots=(REPO / "flavors",),
        )
        selected = {
            item.value
            for node in plan.nodes
            for item in node.flavor_candidates
            if item.status is CandidateStatus.SELECTED
        }
        self.assertIn("react", selected)
        self.assertIn("javascript", selected)

    def test_react_dashboard_without_javascript_fails_closed(self) -> None:
        with self.assertRaises(ComponentLockPlanningError) as raised:
            FilesystemComponentLockPlanner().plan(
                REPO / "samples" / "react-dashboard-example",
                target_name="macos-host",
                flavor_selectors=("+python", "+ui-react", "+macos"),
                flavor_roots=(REPO / "flavors",),
            )
        self.assertEqual(
            raised.exception.code, "component_lock.flavor_target_unsatisfied"
        )

    def test_peer_samples_use_shared_entrypoint_kinds(self) -> None:
        backend = parse_component_markdown(
            REPO / "samples" / "backend-base" / "component.md",
            (REPO / "samples" / "backend-base" / "component.md").read_text(
                encoding="utf-8"
            ),
            project_root=REPO,
        )
        frontend = parse_component_markdown(
            REPO / "samples" / "frontend-base" / "component.md",
            (REPO / "samples" / "frontend-base" / "component.md").read_text(
                encoding="utf-8"
            ),
            project_root=REPO,
        )
        webmcp = parse_component_markdown(
            REPO / "samples" / "webmcp-page" / "component.md",
            (REPO / "samples" / "webmcp-page" / "component.md").read_text(
                encoding="utf-8"
            ),
            project_root=REPO,
        )
        self.assertEqual(backend.entrypoints[0].kind, "persistent-service")
        self.assertEqual(frontend.entrypoints[0].kind, WEB_APPLICATION_ENTRYPOINT_KIND)
        self.assertEqual(webmcp.entrypoints[0].kind, WEB_APPLICATION_ENTRYPOINT_KIND)
        self.assertTrue(
            {backend.entrypoints[0].kind, frontend.entrypoints[0].kind}
            <= SAMPLE_HARNESS_ENTRYPOINT_KINDS
        )
        language = next(
            slot for slot in backend.flavor_slots if slot.slot_id == "language"
        )
        self.assertEqual(language.capability_contract, "sample.portable-app")
        frontend_language = next(
            slot for slot in frontend.flavor_slots if slot.slot_id == "language"
        )
        self.assertEqual(
            frontend_language.capability_contract, "application.web-frontend"
        )
        webmcp_language = next(
            slot for slot in webmcp.flavor_slots if slot.slot_id == "language"
        )
        self.assertEqual(
            webmcp_language.capability_contract, "application.web-frontend"
        )
        frontend_skills = tuple(item.uri for item in frontend.authoring_inputs)
        self.assertTrue(
            any(
                uri.endswith("frontend-application/SKILL.md") for uri in frontend_skills
            )
        )
        self.assertFalse(
            any(uri.endswith("react-application/SKILL.md") for uri in frontend_skills)
        )
        backend_parent = parse_component_markdown(
            REPO / "components" / "backend-application" / "component.md",
            (REPO / "components" / "backend-application" / "component.md").read_text(
                encoding="utf-8"
            ),
            project_root=REPO,
        )
        parent_skills = {item.uri for item in backend_parent.authoring_inputs}
        self.assertIn(
            "skills/specification-to-source/backend-application/"
            "python-service-application/SKILL.md",
            parent_skills,
        )
        self.assertIn(
            "skills/specification-to-source/backend-application/"
            "rust-service-application/SKILL.md",
            parent_skills,
        )


if __name__ == "__main__":
    unittest.main()
