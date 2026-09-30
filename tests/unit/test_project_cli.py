"""Canonical project onboarding, validation, and plan command tests."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.project_initialization import ProjectInitializationError
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.cli import main
from literate_ai.contracts import (
    ProjectDefinition,
    RepositoryLineage,
    RepositoryParentSelection,
)
from literate_ai.contracts.authoring_markdown import (
    AGENT_SKILL_EVALUATION_AUTHOR,
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.project_source_index import ProjectSourceIntelligenceError
from tests.unit.root_parent_adapter import root_parent_for_fixture_project

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_FIELDS = (
    "component_roots",
    "flavor_roots",
    "skill_roots",
    "workflow_roots",
    "routing_roots",
)
PROJECT_ROOT_FIELDS = (*CATALOG_FIELDS, "documentation_roots")
OPTIONAL_AGENT_SHIMS = (
    "AGENTS.md",
    "CLAUDE.md",
    "agents/openai.yaml",
    ".cursor/rules/literate-ai.mdc",
)
BAZEL_TEMPLATE_ASSETS = (
    "flavors/build-bazel/flavor.md",
    "flavors/build-bazel/openspec/spec.md",
    "skills/specification-to-source/bazel-build-system/SKILL.md",
)


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    errors = io.StringIO()
    with root_parent_for_fixture_project(arguments):
        status = main(arguments, stdout=output, stderr=errors)
    content = output.getvalue() if status == 0 else errors.getvalue()
    if not content:
        content = output.getvalue() or errors.getvalue()
    try:
        return status, json.loads(content)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"non-JSON CLI output ({status=}): {content!r}") from exc


def copy_generation_catalogs(target: Path) -> None:
    shutil.copytree(
        REPO_ROOT / "skills" / "specification-to-source",
        target / "skills" / "specification-to-source",
        dirs_exist_ok=True,
    )
    shutil.copytree(
        REPO_ROOT / "skills" / "agent" / "swift-toolchain-prerequisite",
        target / "skills" / "agent" / "swift-toolchain-prerequisite",
        dirs_exist_ok=True,
    )
    shutil.copytree(
        REPO_ROOT / "skills" / "agent" / "select-nvidia-accelerated-stack",
        target / "skills" / "agent" / "select-nvidia-accelerated-stack",
        dirs_exist_ok=True,
    )
    shutil.copy2(
        REPO_ROOT / "workflows" / "sample-host.md",
        target / "workflows" / "sample-host.md",
    )
    shutil.copy2(
        REPO_ROOT / "routing" / "sample-host.json",
        target / "routing" / "sample-host.json",
    )


def copy_inverse_catalog(target: Path, root: str = "skills") -> Path:
    destination = target / root / "source-to-specification"
    shutil.copytree(
        REPO_ROOT / "skills" / "source-to-specification",
        destination,
        dirs_exist_ok=True,
    )
    return destination


def copy_hello_component(target: Path) -> Path:
    component = target / "samples" / "hello-component"
    if not component.is_dir():
        shutil.copytree(REPO_ROOT / "samples" / "hello-component", component)
    for generated_lock in component.glob("component.*.json"):
        generated_lock.unlink()
    return component


def replace_skill_uri(manifest: Path, old: str, new: str) -> None:
    if manifest.suffix == ".md":
        value, body = parse_authoring_markdown(
            manifest.read_bytes(), source=manifest.as_posix()
        )
        references = value["authoring_inputs"]
        selected = next(item for item in references if item["uri"] == old)
        selected["uri"] = new
        manifest.write_bytes(render_authoring_markdown(value, body))
        return
    value = json.loads(manifest.read_text(encoding="utf-8"))
    references = value["authoring_inputs"]
    assert isinstance(references, list)
    selected = next(item for item in references if item["uri"] == old)
    selected["uri"] = new
    manifest.write_text(json.dumps(value), encoding="utf-8")


def _mutate_json(path: Path, mutate) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    path.write_text(json.dumps(value), encoding="utf-8")


def _mutate_skill_markdown(path: Path, mutate) -> None:
    value, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
    mutate(value)
    path.write_bytes(render_authoring_markdown(value, body))


def _add_duplicate_inverse_skill(target: Path, catalog: Path) -> None:
    duplicate = target / "other-skills" / "source-to-specification" / "architecture"
    shutil.copytree(catalog / "architecture", duplicate)
    manifest_path = target / "literate.project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skill_roots"].append("other-skills")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def refresh_authority_review(target: Path) -> None:
    manifest = json.loads((target / "literate.project.json").read_text())
    documents = sorted(
        path
        for root in manifest["documentation_roots"]
        for path in (target / root).rglob("*.md")
    )
    assert documents
    marker = re.compile(
        r"<!--\s*literate-ai:authority-reviewed sha256:[0-9a-f]{64}\s*-->"
    )
    selected = next(
        (
            path
            for path in documents
            if marker.search(path.read_text(encoding="utf-8"))
            or "<!-- literate-ai:authority-review-pending -->"
            in path.read_text(encoding="utf-8")
        ),
        documents[0],
    )
    content = selected.read_text(encoding="utf-8")
    if marker.search(content):
        content = marker.sub("<!-- literate-ai:authority-review-pending -->", content)
    elif "<!-- literate-ai:authority-review-pending -->" not in content:
        content += "\n<!-- literate-ai:authority-review-pending -->\n"
    selected.write_text(content, encoding="utf-8")
    status, envelope = invoke("project", "documentation-review", str(target))
    assert status == 0, envelope
    expected = envelope["result"]["expected_marker"]
    selected.write_text(
        selected.read_text(encoding="utf-8").replace(
            "<!-- literate-ai:authority-review-pending -->", expected
        ),
        encoding="utf-8",
    )


class ProjectCliTests(unittest.TestCase):
    def test_conversion_timeout_requires_conversion_mode(self) -> None:
        status, envelope = invoke(
            "init", "--baseline-timeout-seconds", "7200", "unused-project"
        )

        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"], "project.convert_timeout_requires_convert"
        )

    def test_conversion_diagnostic_limit_requires_conversion_mode(self) -> None:
        status, envelope = invoke(
            "init", "--baseline-diagnostic-chars", "16384", "unused-project"
        )

        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"],
            "project.convert_diagnostic_limit_requires_convert",
        )

    def test_harness_workspace_link_requires_conversion_mode(self) -> None:
        status, envelope = invoke(
            "init", "--harness-workspace-link", "sdk=/tmp/sdk", "unused-project"
        )

        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"], "harness.workspace_links_require_convert"
        )

    def test_convert_plan_validates_workspace_links_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            external = root / "sdk"
            project.mkdir()
            external.mkdir()
            (project / "Makefile").write_text("all:\n\t@echo ready\n", encoding="utf-8")

            status, envelope = invoke(
                "init",
                "--convert",
                "--plan",
                "--harness-workspace-link",
                f"sdk={external}",
                str(project),
            )

            self.assertEqual(status, 0, envelope)
            result = envelope["result"]
            self.assertFalse(result["writes"])
            self.assertEqual(result["workspace_links"]["destinations"], ["sdk"])
            self.assertFalse((project / "literate.project.json").exists())
            self.assertEqual(list(project.iterdir()), [project / "Makefile"])

    def test_conversion_diagnostic_environment_must_be_an_integer(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"LITAI_CONVERT_DIAGNOSTIC_CHARS": "not-an-integer"},
            clear=False,
        ):
            status, envelope = invoke("init", "--convert", "unused-project")

        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"], "project.convert_diagnostic_limit_invalid"
        )

    def test_conversion_gate_diagnostic_budget_reaches_cli_error_envelope(self) -> None:
        diagnostic = (
            "legacy build gate failed\n[stdout]\n[output head]\n"
            + ("x" * 600)
            + "\n[first error context]\nGLIBC_2.34 not found"
            + "\n[output tail]\nSystemExit: 1"
        )
        with mock.patch(
            "literate_ai.cli.project.FilesystemProjectInitializationAdapter.initialize",
            side_effect=ProjectInitializationError(
                "project.convert_legacy_gate_failed", diagnostic
            ),
        ):
            status, envelope = invoke(
                "init",
                "--convert",
                "--baseline-diagnostic-chars",
                "4096",
                ".",
            )

        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"], "project.convert_legacy_gate_failed"
        )
        self.assertGreater(len(envelope["error"]["message"]), 512)
        self.assertIn("GLIBC_2.34 not found", envelope["error"]["message"])
        self.assertIn("[output tail]\nSystemExit: 1", envelope["error"]["message"])

    def test_ordinary_cli_error_retains_default_message_bound(self) -> None:
        with mock.patch(
            "literate_ai.cli.project.FilesystemProjectInitializationAdapter.initialize",
            side_effect=ProjectInitializationError("project.fixture", "x" * 600),
        ):
            status, envelope = invoke("init", ".")

        self.assertEqual(status, 2, envelope)
        self.assertEqual(envelope["error"]["code"], "project.fixture")
        self.assertEqual(len(envelope["error"]["message"]), 512)

    def test_documentation_update_is_read_only_and_reports_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "documentation-update"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            guide = target / "docs" / "user" / "getting-started.md"
            guide.write_text(
                guide.read_text(encoding="utf-8") + "\nRun `litai obsolete-command`.\n",
                encoding="utf-8",
            )
            before = {
                path: path.read_bytes() for path in target.rglob("*") if path.is_file()
            }

            status, envelope = invoke("project", "documentation-update", str(target))

            self.assertEqual(status, 0, envelope)
            result = envelope["result"]
            self.assertEqual(result["schema"], "literate-ai/documentation-update@1")
            self.assertFalse(result["model_invoked"])
            self.assertFalse(result["applied"])
            kinds = {item["kind"] for item in result["findings"]}
            self.assertIn("command-drift", kinds)
            after = {
                path: path.read_bytes() for path in target.rglob("*") if path.is_file()
            }
            self.assertEqual(before, after)

    def test_documentation_update_requires_both_apply_authorizations(self) -> None:
        status, envelope = invoke("project", "documentation-update", ".", "--apply")
        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"],
            "project.documentation_update_authorization_required",
        )

        status, envelope = invoke(
            "project", "documentation-update", ".", "--allow-model-egress"
        )
        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"],
            "project.documentation_update_authorization_required",
        )

    def test_documentation_update_applies_through_isolated_task_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "documentation-apply"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            guide = target / "docs" / "user" / "getting-started.md"
            before = guide.read_bytes()
            response = {
                "schema": "literate-ai/documentation-update-proposal@1",
                "changes": [
                    {
                        "path": "docs/user/getting-started.md",
                        "base_identity": "sha256:" + hashlib.sha256(before).hexdigest(),
                        "reasons": ["Clarify the current workflow."],
                        "content": guide.read_text(encoding="utf-8")
                        + "\nCurrent documentation workflow.\n",
                    }
                ],
            }
            task = SimpleNamespace(
                response=response,
                request_identity="sha256:" + "1" * 64,
                response_identity="sha256:" + "2" * 64,
                selection_identity="sha256:" + "3" * 64,
                tool_binding_identity="sha256:" + "4" * 64,
            )
            runner = mock.Mock()
            runner.run_json_task.return_value = task
            with mock.patch(
                "literate_ai.adapters.models.CodingCliTaskRunner", return_value=runner
            ):
                status, envelope = invoke(
                    "project",
                    "documentation-update",
                    str(target),
                    "--apply",
                    "--allow-model-egress",
                )

            self.assertEqual(status, 0, envelope)
            self.assertTrue(envelope["result"]["applied"])
            self.assertEqual(envelope["result"]["marker"]["after"], "stale")
            self.assertIn("Current documentation workflow.", guide.read_text())
            runner.run_json_task.assert_called_once()

    def test_documentation_update_rejects_model_during_read_only_plan(self) -> None:
        status, envelope = invoke(
            "project", "documentation-update", ".", "--model", "example-model"
        )
        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"],
            "project.documentation_update_model_not_allowed",
        )

    def test_documentation_review_record_atomically_refreshes_exact_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "reviewed"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            guide = target / "docs" / "user" / "framework-flow.md"
            guide.write_text(
                guide.read_text(encoding="utf-8") + "\nReviewed autonomous change.\n",
                encoding="utf-8",
            )

            status, envelope = invoke(
                "project", "documentation-review", str(target), "--record"
            )

            self.assertEqual(status, 0, envelope)
            self.assertTrue(envelope["result"]["recorded"])
            self.assertEqual(envelope["result"]["state"], "current")
            self.assertIn(
                envelope["result"]["expected_marker"],
                (target / "docs/architecture/design-traceability.md").read_text(),
            )
            self.assertEqual(invoke("project", "validate", str(target))[0], 0)

    def test_documentation_review_record_replaces_placeholder_and_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "placeholder-review"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            marker_path = target / "docs/architecture/design-traceability.md"
            current = marker_path.read_text(encoding="utf-8")
            pending = re.sub(
                r"<!--\s*literate-ai:authority-reviewed sha256:[0-9a-f]{64}\s*-->",
                "<!-- literate-ai:authority-review-pending -->",
                current,
                count=1,
            )
            marker_path.write_text(pending, encoding="utf-8")
            status, envelope = invoke(
                "project", "documentation-review", str(target), "--record"
            )
            self.assertEqual(status, 0, envelope)
            self.assertTrue(envelope["result"]["recorded"])
            self.assertEqual(envelope["result"]["state"], "current")
            self.assertIn(
                envelope["result"]["expected_marker"],
                marker_path.read_text(encoding="utf-8"),
            )

            duplicate = marker_path.read_text(encoding="utf-8")
            marker_path.write_text(
                duplicate + "\n" + envelope["result"]["expected_marker"] + "\n",
                encoding="utf-8",
            )
            before = {
                path: path.read_bytes() for path in target.rglob("*") if path.is_file()
            }
            status, envelope = invoke(
                "project", "documentation-review", str(target), "--record"
            )
            self.assertEqual(status, 2, envelope)
            self.assertEqual(
                envelope["error"]["code"],
                "project.documentation_authority_review_duplicate",
            )
            after = {
                path: path.read_bytes() for path in target.rglob("*") if path.is_file()
            }
            self.assertEqual(before, after)

            marker_path.write_text("# No marker\n", encoding="utf-8")
            (target / "docs/user/getting-started.md").write_text(
                (target / "docs/user/getting-started.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            before = {
                path: path.read_bytes() for path in target.rglob("*") if path.is_file()
            }
            status, envelope = invoke(
                "project", "documentation-review", str(target), "--record"
            )
            self.assertEqual(status, 2, envelope)
            self.assertEqual(
                envelope["error"]["code"],
                "project.documentation_authority_review_document_required",
            )
            after = {
                path: path.read_bytes() for path in target.rglob("*") if path.is_file()
            }
            self.assertEqual(before, after)

    def test_derived_project_accepts_multi_output_catalog_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "multi-output"
            status, envelope = invoke("init", str(target), "--empty")
            self.assertEqual(status, 0, envelope)
            skill = target / "skills/specification-to-source/schema-bindings/SKILL.md"
            skill.parent.mkdir(parents=True)
            metadata = {
                "name": "schema-bindings",
                "description": "Generate both bindings from one reviewed schema.",
                "metadata": {
                    "author": AGENT_SKILL_EVALUATION_AUTHOR,
                },
                "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
                "skill_id": "schema-bindings",
                "version": "1.0.0",
                "title": "Shared schema bindings",
                "stages": ["generate"],
                "dependencies": [],
                "limitations": ["Both targets must pass independent acceptance."],
                "trust": "project-reviewed",
                "output_trees": ["source/backend", "source/frontend"],
            }
            body = (
                "# Shared schema bindings\n\n"
                "Generate Python backend and TypeScript frontend bindings from the "
                "same authored protocol schema under their declared output trees. "
                "Complete and validate both targets before admitting the result."
            )
            skill.write_bytes(render_authoring_markdown(metadata, body))
            status, envelope = invoke(
                "project", "documentation-review", str(target), "--record"
            )
            self.assertEqual(status, 0, envelope)
            before = skill.read_bytes()
            status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 0, envelope)
            self.assertEqual(skill.read_bytes(), before)

            for invalid in (["backend"], ["source/../escape"]):
                with self.subTest(output_trees=invalid):
                    metadata["output_trees"] = invalid
                    skill.write_bytes(render_authoring_markdown(metadata, body))
                    status, envelope = invoke("project", "validate", str(target))
                    self.assertEqual(status, 2, envelope)
                    self.assertEqual(envelope["error"]["code"], "project.skill_invalid")

    def test_roadmap_queue_edits_do_not_stale_authority_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "queue-scope"
            self.assertEqual(invoke("init", str(target))[0], 0)
            identity = invoke("project", "documentation-review", str(target))[1][
                "result"
            ]["authority_identity"]
            queue = target / "docs/roadmap/active-work.md"
            queue.write_text(
                queue.read_text(encoding="utf-8") + "\n- [ ] QUEUE-CHORES-001\n",
                encoding="utf-8",
            )
            status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 0, envelope)
            self.assertEqual(
                envelope["result"]["authority_review"]["authority_identity"], identity
            )
            self.assertEqual(envelope["result"]["authority_review"]["state"], "current")
            queue.write_text(
                queue.read_text(encoding="utf-8") + "\n[broken](missing.md)\n",
                encoding="utf-8",
            )
            status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 2, envelope)
            self.assertEqual(
                envelope["error"]["code"],
                "project.documentation_link_missing",
            )
            queue.write_text(
                queue.read_text(encoding="utf-8").replace(
                    "\n[broken](missing.md)\n", ""
                ),
                encoding="utf-8",
            )
            guide = target / "docs/user/framework-flow.md"
            guide.write_text(
                guide.read_text(encoding="utf-8") + "\nAuthority-reviewed prose.\n",
                encoding="utf-8",
            )
            status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 2, envelope)
            self.assertEqual(
                envelope["error"]["code"],
                "project.documentation_authority_review_stale",
            )

    def test_init_optional_type_preserves_default_ux(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            default_target = Path(directory) / "default-app"
            status, envelope = invoke("init", str(default_target))
            self.assertEqual(status, 0, envelope)
            self.assertEqual(envelope["result"]["project_type"], "application")
            manifest = json.loads(
                (default_target / "literate.project.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["project_type"], "application")
            self.assertTrue(
                (
                    default_target / "samples" / "hello-component" / "component.md"
                ).is_file()
            )

            library = Path(directory) / "lib-shape"
            status, envelope = invoke("init", str(library), "--type", "library")
            self.assertEqual(status, 0, envelope)
            self.assertEqual(envelope["result"]["project_type"], "library")
            self.assertFalse((library / "samples" / "hello-component").exists())
            self.assertTrue((library / "SKILL.md").is_file())

    def test_init_python_macos_does_not_install_unselected_flavors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "scoped-flavors"
            status, envelope = invoke(
                "init",
                str(target),
                "--empty",
                "--flavor",
                "python",
                "--flavor",
                "macos",
            )
            self.assertEqual(status, 0, envelope)
            self.assertTrue(
                (target / "flavors" / "lang-python" / "flavor.md").is_file()
            )
            self.assertTrue((target / "flavors" / "os-macos" / "flavor.md").is_file())
            self.assertFalse((target / "flavors" / "python").exists())
            self.assertFalse((target / "flavors" / "macos").exists())
            for unexpected in ("bazel", "javascript", "rust", "cpp"):
                self.assertFalse((target / "flavors" / unexpected).exists(), unexpected)

    def test_documentation_review_record_restores_git_backed_projects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "git-backed"
            subprocess.run(
                ["git", "init", "-b", "main", str(target)],
                check=True,
                capture_output=True,
            )
            self.assertEqual(invoke("init", str(target))[0], 0)
            guide = target / "docs/user/framework-flow.md"
            guide.write_text(
                guide.read_text(encoding="utf-8") + "\nGit-backed authority change.\n",
                encoding="utf-8",
            )
            status, envelope = invoke(
                "project", "documentation-review", str(target), "--record"
            )
            self.assertEqual(status, 0, envelope)
            self.assertEqual(envelope["result"]["state"], "current")
            self.assertEqual(invoke("project", "validate", str(target))[0], 0)

    def test_repository_is_a_self_describing_canonical_project(self) -> None:
        status, envelope = invoke("project", "validate", str(REPO_ROOT))

        self.assertEqual(status, 0, envelope)
        result = envelope["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["project_id"], "literate-ai")
        self.assertEqual(result["profile"], "canonical")
        self.assertEqual(
            result["source_intelligence"]["status"]["state"],
            "off",
        )
        self.assertEqual(
            result["default_flavor_selectors"],
            [
                "+flavor://literate-ai/build-make",
                "+flavor://literate-ai/doc-google-workspace",
            ],
        )
        components = result["components"]
        self.assertTrue(components)
        component_coordinates = [item["coordinate"] for item in components]
        self.assertEqual(len(component_coordinates), len(set(component_coordinates)))
        self.assertTrue(
            {
                "component://literate-ai/invoice-service",
                "component://literate-ai/money-calculation",
            }.issubset(component_coordinates)
        )
        for catalog_name, identity_field in (
            ("flavors", "coordinate"),
            ("specification_to_source_skills", "skill_id"),
            ("source_to_specification_skills", "skill_id"),
        ):
            with self.subTest(catalog=catalog_name):
                catalog = result[catalog_name]
                self.assertTrue(catalog)
                identities = [item[identity_field] for item in catalog]
                self.assertEqual(len(identities), len(set(identities)))
        self.assertTrue(result["documentation"])

    def test_root_and_installed_template_share_the_onboarding_contract(self) -> None:
        root = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
        installed = (
            files("literate_ai.project_template")
            .joinpath("SKILL.md")
            .read_text(encoding="utf-8")
        )
        preflight = (
            REPO_ROOT / "skills/agent/preflight-host-toolchains/SKILL.md"
        ).read_text(encoding="utf-8")

        def section_headings(content: str) -> tuple[str, ...]:
            return tuple(
                line for line in content.splitlines() if line.startswith("## ")
            )

        self.assertEqual(section_headings(root), section_headings(installed))
        for required in (
            "name: literate-ai",
            "Treat specifications as application authority",
            "litai project validate",
            "litai rebuild",
            "## Preserve authority boundaries",
            "## Read only the relevant detail",
        ):
            with self.subTest(required=required):
                self.assertIn(required, root)
                self.assertIn(required, installed)
        # Host-toolchain detail now lives in the skill the root points at; the
        # template keeps its own self-contained prose because it ships a subset
        # of agent skills that excludes that one.
        self.assertIn("skills/agent/preflight-host-toolchains/SKILL.md", root)
        self.assertIn(
            "repository-pinned Node contributor closure",
            preflight,
        )
        self.assertIn("This is a derived project", installed)
        self.assertIn("Do not assume", installed)

    def test_project_validation_accepts_crlf_agent_skill_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "crlf-skill"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            skill = target / "SKILL.md"
            skill.write_bytes(skill.read_bytes().replace(b"\n", b"\r\n"))
            refresh_authority_review(target)

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 0, envelope)
            self.assertEqual(envelope["result"]["onboarding_skill"]["path"], "SKILL.md")

    def test_project_validation_rejects_legacy_only_component_with_exit_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "legacy-only"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            component = target / "components" / "legacy"
            component.mkdir(parents=True)
            (component / "component.json").write_text("{}\n", encoding="utf-8")

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 2, envelope)
            self.assertEqual(
                envelope["error"]["code"],
                "project.component_migration_required",
            )
            self.assertIn(
                "litai component migrate COMPONENT", envelope["error"]["message"]
            )
            self.assertIn("0.3.0", envelope["error"]["message"])

    def test_packaged_bazel_template_assets_match_the_canonical_catalog(self) -> None:
        packaged = files("literate_ai.project_template")
        for relative in BAZEL_TEMPLATE_ASSETS:
            with self.subTest(relative=relative):
                self.assertEqual(
                    packaged.joinpath(*Path(relative).parts).read_bytes(),
                    (REPO_ROOT / relative).read_bytes(),
                )

    def test_documentation_review_marker_tracks_authority_and_document_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "reviewed"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 0)
            self.assertEqual(envelope["result"]["authority_review"]["state"], "current")

            guide = target / "docs" / "user" / "framework-flow.md"
            guide.write_text(
                guide.read_text(encoding="utf-8") + "\nCurrent review changed.\n",
                encoding="utf-8",
            )
            status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"],
                "project.documentation_authority_review_stale",
            )
            status, envelope = invoke("project", "documentation-review", str(target))
            self.assertEqual(status, 0)
            self.assertEqual(envelope["result"]["state"], "stale")
            self.assertRegex(
                envelope["result"]["expected_marker"],
                r"^<!-- literate-ai:authority-reviewed sha256:[0-9a-f]{64} -->$",
            )

    def test_authority_review_tracks_component_generation_input_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "review-inputs"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )[0],
                0,
            )
            copy_generation_catalogs(target)
            component = copy_hello_component(target)

            def authority_identity() -> str:
                status, envelope = invoke(
                    "project", "documentation-review", str(target)
                )
                self.assertEqual(status, 0, envelope)
                return str(envelope["result"]["authority_identity"])

            identities = [authority_identity()]

            specification = component / "component.md"
            specification.write_text(
                specification.read_text(encoding="utf-8")
                + "\nA generation-relevant specification revision.\n",
                encoding="utf-8",
            )
            identities.append(authority_identity())

            workflow = target / "workflows" / "sample-host.md"
            workflow.write_bytes(workflow.read_bytes() + b"\n")
            identities.append(authority_identity())

            self.assertEqual(len(set(identities)), len(identities))

    def test_init_creates_a_valid_starter_taxonomy_from_below(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "demo"
            status, envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
                "--project-id",
                "demo-app",
            )
            self.assertEqual(status, 0)
            result = envelope["result"]
            assert isinstance(result, dict)
            self.assertEqual(result["project_id"], "demo-app")
            self.assertFalse((target / "components" / "source").exists())
            manifest = json.loads((target / "literate.project.json").read_text())
            self.assertEqual(manifest["agent_skill"], "SKILL.md")
            self.assertEqual(manifest["test_receipt"], "verification/current.json")
            self.assertNotIn("test_receipt_policy", manifest)
            self.assertEqual(
                manifest["default_flavor_selectors"],
                [
                    "+flavor://literate-ai/lang-python",
                    "+flavor://literate-ai/os-macos",
                    "+flavor://literate-ai/build-bazel",
                    "+flavor://literate-ai/package-pip",
                ],
            )
            self.assertTrue(
                (target / "samples" / "hello-component" / "component.md").is_file()
            )
            project_ignore = (target / ".gitignore").read_text(encoding="utf-8")
            ignore_lines = project_ignore.splitlines()
            # Derivation caches stay ignored, but the project-relative source-cache
            # target is deliberately committable.
            self.assertIn("generated/*", ignore_lines)
            self.assertIn("!generated/committed-source-cache/", ignore_lines)
            self.assertIn(".litai-cache-locks/", project_ignore.splitlines())
            self.assertIn("/literate.test.json", project_ignore.splitlines())
            self.assertIn("/literate.workers.json", project_ignore.splitlines())
            self.assertIn(
                "/literate.worker-observations.json", project_ignore.splitlines()
            )
            self.assertTrue((target / "literate.workers.example.json").is_file())
            self.assertIn(".gitignore", result["created"])
            test_matrix = result["test_matrix"]
            self.assertEqual(test_matrix["state"], "optional-user-configured")
            self.assertEqual(Path(test_matrix["configuration"]).name, "test.json")
            self.assertEqual(
                Path(test_matrix["worker_configuration"]).name, "workers.json"
            )
            self.assertEqual(test_matrix["example"], "literate.test.example.json")
            self.assertEqual(
                test_matrix["worker_example"], "literate.workers.example.json"
            )
            self.assertEqual(test_matrix["documentation"], "docs/user/test-matrix.md")
            self.assertEqual(test_matrix["version_control"], "outside-project")
            self.assertTrue((target / "docs/user/test-matrix.md").is_file())
            self.assertTrue((target / "docs/roadmap/active-work.md").is_file())
            self.assertEqual(
                manifest["source_intelligence"],
                {
                    "schema": (
                        "urn:literate-ai:schema:v1:project-source-intelligence-policy"
                    ),
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
            )
            self.assertFalse((target / ".codegraph" / "codegraph.db").is_file())
            self.assertEqual(result["source_intelligence"]["status"]["state"], "off")
            self.assertTrue(all(manifest[field] for field in PROJECT_ROOT_FIELDS))
            self.assertTrue(
                all((target / relative).is_file() for relative in OPTIONAL_AGENT_SHIMS)
            )
            self.assertTrue(
                all((target / relative).is_file() for relative in BAZEL_TEMPLATE_ASSETS)
            )
            self.assertTrue(set(BAZEL_TEMPLATE_ASSETS) <= set(result["created"]))
            specification_guide = (
                target / "docs" / "user" / "specifications.md"
            ).read_text(encoding="utf-8")
            self.assertIn("Start every normal Component", specification_guide)
            self.assertIn("one `component.md`", specification_guide)
            self.assertIn("Do not create `component.json`", specification_guide)
            self.assertNotIn("Choose `openspec`", specification_guide)
            self.assertNotIn(
                "first declared artifact must be the root `spec.md`",
                specification_guide,
            )
            status, envelope = invoke("project", "validate", str(target / "components"))
            self.assertEqual(status, 0)
            validation = envelope["result"]
            assert isinstance(validation, dict)
            component_coordinates = [
                item["coordinate"] for item in validation["components"]
            ]
            self.assertEqual(
                len(component_coordinates), len(set(component_coordinates))
            )
            self.assertIn("component://example/hello-component", component_coordinates)
            flavor_coordinates = [item["coordinate"] for item in validation["flavors"]]
            self.assertEqual(len(flavor_coordinates), len(set(flavor_coordinates)))
            self.assertTrue(
                {
                    "flavor://literate-ai/build-bazel",
                    "flavor://literate-ai/os-macos",
                    "flavor://literate-ai/lang-python",
                }
                <= set(flavor_coordinates)
            )
            skill_ids = [
                item["skill_id"]
                for item in validation["specification_to_source_skills"]
            ]
            self.assertEqual(len(skill_ids), len(set(skill_ids)))
            self.assertTrue(
                {
                    "bazel-build-system",
                    "portable-application-implementation",
                    "portable-specification-planning",
                    "python-portable-application",
                }
                <= set(skill_ids)
            )
            self.assertTrue(validation["documentation"])
            self.assertTrue(validation["onboarding_skill"]["documentation_links"])
            status, lock_envelope = invoke("lock", str(target), "--check")
            self.assertEqual(status, 0, lock_envelope)
            self.assertTrue(lock_envelope["result"]["current"])
            starter_guide = (target / "docs" / "user" / "getting-started.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("litai rebuild samples/hello-component", starter_guide)
            self.assertIn("DOC-IDENTITY", starter_guide)
            self.assertIn("What is this project?", starter_guide)
            self.assertIn("Installation", starter_guide)
            self.assertIn("Development workflow", starter_guide)
            self.assertIn("health/readiness checks", starter_guide)
            self.assertIn("uninstall or cleanup", starter_guide)
            # DOC-IDENTITY-001: freshly initialized project carries the advisory
            status, validation = invoke("project", "validate", str(target))
            self.assertEqual(status, 0, validation)
            advisories = validation.get("result", validation).get("advisories", [])
            placeholder_paths = [
                a["path"]
                for a in advisories
                if a["code"] == "project.doc_identity_placeholder"
            ]
            self.assertIn("docs/user/getting-started.md", placeholder_paths)
            self.assertIn("docs/README.md", placeholder_paths)

    def test_minimal_project_needs_no_catalogs_or_provider_shims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "minimal"
            target.mkdir()
            manifest_path = target / "literate.project.json"
            manifest = json.loads(
                (REPO_ROOT / "literate.project.json").read_text(encoding="utf-8")
            )
            manifest["project_id"] = "minimal"
            manifest["version"] = "1.0.0"
            for field in CATALOG_FIELDS:
                manifest[field] = []
            manifest["source_intelligence"] = {
                "schema": (
                    "urn:literate-ai:schema:v1:project-source-intelligence-policy"
                ),
                "provider_id": "none",
                "command": None,
                "minimum_version": None,
                "artifact_path": None,
                "stages": {
                    stage: "off" for stage in manifest["source_intelligence"]["stages"]
                },
                "artifact_publication": "metadata-only",
            }
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            docs = target / "guide"
            docs.mkdir()
            (docs / "README.md").write_text("# Minimal guide\n", encoding="utf-8")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["documentation_roots"] = ["guide"]
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            (target / "SKILL.md").write_text(
                "---\nname: literate-ai\n---\n\n"
                "Read the [project guide](guide/README.md).\n",
                encoding="utf-8",
            )
            root = RepositoryParentSelection.root()
            FilesystemRepositoryLineageStore(target).replace(
                root, RepositoryLineage(root, (), ()), expected_absent=True
            )
            refresh_authority_review(target)
            self.assertEqual(
                {path.name for path in target.iterdir()},
                {"literate.project.json", "SKILL.md", "guide", ".literate"},
            )

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 0, envelope)
            result = envelope["result"]
            assert isinstance(result, dict)
            self.assertEqual(
                result["catalogs"],
                {
                    "component": [],
                    "flavor": [],
                    "skill": [],
                    "workflow": [],
                    "routing": [],
                    "documentation": ["guide"],
                },
            )
            self.assertEqual(result["components"], [])
            self.assertEqual(result["flavors"], [])
            self.assertEqual(result["specification_to_source_skills"], [])
            self.assertEqual(result["source_to_specification_skills"], [])
            self.assertEqual(
                [item["path"] for item in result["documentation"]],
                ["guide/README.md"],
            )

    def test_root_skill_remains_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing-skill"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            (target / "SKILL.md").unlink()

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 2)
            self.assertEqual(envelope["error"]["code"], "project.layout_incomplete")

    def test_declared_catalog_root_remains_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing-catalog"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            manifest_path = target / "literate.project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["component_roots"] = ["missing-components"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 2)
            self.assertEqual(envelope["error"]["code"], "project.layout_incomplete")

    def test_present_provider_shim_must_delegate_to_root_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "invalid-shim"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            (target / "AGENTS.md").write_text(
                "# Independent agent policy\n", encoding="utf-8"
            )

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 2)
            self.assertEqual(envelope["error"]["code"], "project.agent_shim_invalid")

    def test_init_refuses_to_merge_into_a_nonempty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing"
            target.mkdir()
            (target / "keep.txt").write_text("keep\n", encoding="utf-8")
            status, envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 2)
            self.assertEqual(envelope["error"]["code"], "project.init_target_not_empty")
            self.assertEqual((target / "keep.txt").read_text(), "keep\n")

    def test_init_accepts_a_fresh_git_repository_and_preserves_readme(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing-repository"
            target.mkdir()
            subprocess.run(
                ("git", "-C", str(target), "init", "--quiet"),
                check=True,
                capture_output=True,
            )
            readme = b"# Existing introduction\n"
            (target / "README.md").write_bytes(readme)

            status, envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )

            self.assertEqual(status, 0)
            self.assertTrue(envelope["ok"])
            self.assertEqual((target / "README.md").read_bytes(), readme)
            self.assertTrue((target / ".git").is_dir())
            self.assertTrue((target / "literate.project.json").is_file())

    def test_init_defaults_to_disabled_source_intelligence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "unindexed-project"

            status, envelope = invoke(
                "init",
                str(target),
                "--source-intelligence-provider",
                "none",
            )

            self.assertEqual(status, 0)
            self.assertTrue(envelope["ok"])
            self.assertTrue((target / "literate.project.json").is_file())
            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["source_intelligence"]["provider_id"], "none")
            self.assertFalse((target / ".codegraph" / "codegraph.db").is_file())

            status, index_envelope = invoke(
                "project", "source-intelligence", "check", str(target)
            )
            self.assertEqual(status, 2)
            self.assertEqual(
                index_envelope["error"]["code"],
                "project.source_intelligence_disabled",
            )

    def test_init_allows_opt_in_without_installing_external_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "opt-in-project"
            unavailable = ProjectSourceIntelligenceError(
                "project.source_intelligence_missing", "index missing"
            )
            with (
                mock.patch(
                    "literate_ai.adapters.project_validation."
                    "CodeGraphProjectSourceIntelligence.check",
                    side_effect=unavailable,
                ),
                mock.patch(
                    "literate_ai.adapters.project_validation."
                    "CodeGraphProjectSourceIntelligence.sync",
                    side_effect=unavailable,
                ),
            ):
                status, envelope = invoke(
                    "init",
                    str(target),
                    "--source-intelligence-provider",
                    "codegraph-cli",
                )

            self.assertEqual(status, 0, envelope)
            self.assertEqual(envelope["result"]["tool_bootstrap"]["state"], "disabled")
            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )
            policy = manifest["source_intelligence"]
            self.assertEqual(policy["provider_id"], "codegraph-cli")
            self.assertEqual(policy["command"], "codegraph")
            self.assertEqual(policy["minimum_version"], "1.1.1")
            self.assertFalse((target / ".codegraph" / "codegraph.db").exists())

    def test_explicit_source_intelligence_sync_uses_configured_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project with spaces"
            unavailable = ProjectSourceIntelligenceError(
                "project.source_intelligence_missing", "index missing"
            )
            with (
                mock.patch(
                    "literate_ai.adapters.project_validation."
                    "CodeGraphProjectSourceIntelligence.check",
                    side_effect=unavailable,
                ),
                mock.patch(
                    "literate_ai.adapters.project_validation."
                    "CodeGraphProjectSourceIntelligence.sync",
                    side_effect=unavailable,
                ),
            ):
                self.assertEqual(
                    invoke(
                        "init",
                        str(target),
                        "--source-intelligence-provider",
                        "codegraph-cli",
                    )[0],
                    0,
                )
            observation = {
                "schema": "literate-ai/project-source-intelligence-status@1",
                "state": "current",
                "provider_id": "codegraph-cli",
            }
            with mock.patch(
                "literate_ai.cli.project.CodeGraphProjectSourceIntelligence.sync",
                return_value=observation,
            ) as synchronize:
                status, envelope = invoke(
                    "project",
                    "source-intelligence",
                    "sync",
                    str(target),
                    "--binary",
                    "external-codegraph",
                )

            self.assertEqual(status, 0, envelope)
            self.assertEqual(envelope["result"]["observation"], observation)
            synchronize.assert_called_once_with(target.resolve())

    def test_validation_honors_preferred_and_required_stage_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "stage-policy"
            provider_error = ProjectSourceIntelligenceError(
                "project.source_intelligence_missing", "index missing"
            )
            with (
                mock.patch(
                    "literate_ai.adapters.project_validation."
                    "CodeGraphProjectSourceIntelligence.check",
                    side_effect=provider_error,
                ),
                mock.patch(
                    "literate_ai.adapters.project_validation."
                    "CodeGraphProjectSourceIntelligence.sync",
                    side_effect=provider_error,
                ),
            ):
                self.assertEqual(
                    invoke(
                        "init",
                        str(target),
                        "--source-intelligence-provider",
                        "codegraph-cli",
                    )[0],
                    0,
                )
            with mock.patch(
                "literate_ai.adapters.project_validation."
                "CodeGraphProjectSourceIntelligence.check",
                side_effect=provider_error,
            ):
                status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 0, envelope)
            self.assertEqual(
                envelope["result"]["source_intelligence"]["status"]["state"],
                "unavailable",
            )

            manifest_path = target / "literate.project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_intelligence"]["stages"]["project-maintenance"] = (
                "required"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            refresh_authority_review(target)
            with mock.patch(
                "literate_ai.adapters.project_validation."
                "CodeGraphProjectSourceIntelligence.check",
                side_effect=provider_error,
            ):
                status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"], "project.source_intelligence_missing"
            )

    def test_project_validation_checks_exact_skill_dependency_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "demo"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            destination = target / "skills" / "specification-to-source"
            shutil.copytree(
                REPO_ROOT / "skills" / "specification-to-source",
                destination,
                dirs_exist_ok=True,
            )
            manifest = destination / "portable-application-implementation" / "SKILL.md"
            _mutate_skill_markdown(
                manifest,
                lambda value: value["dependencies"][0]["identity"].update(
                    {"digest": "0" * 64}
                ),
            )
            status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"],
                "project.skill_dependency_identity_mismatch",
            )

    def test_component_cannot_admit_exact_skill_outside_declared_catalogs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "component-catalog-boundary"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            copy_generation_catalogs(target)
            component = copy_hello_component(target)

            admitted = (
                target
                / "skills"
                / "specification-to-source"
                / "portable-specification-planning"
                / "SKILL.md"
            )
            ambient = (
                target / "ambient" / "portable-specification-planning" / "SKILL.md"
            )
            ambient.parent.mkdir(parents=True)
            shutil.copy2(admitted, ambient)
            self.assertEqual(admitted.read_bytes(), ambient.read_bytes())
            self.assertTrue(ambient.resolve().is_relative_to(target.resolve()))
            replace_skill_uri(
                component / "component.md",
                "skills/specification-to-source/"
                "portable-specification-planning/SKILL.md",
                "ambient/portable-specification-planning/SKILL.md",
            )
            status, envelope = invoke("project", "documentation-review", str(target))

            self.assertEqual(status, 2)
            self.assertEqual(envelope["error"]["code"], "generate.skill_not_cataloged")

    def test_flavor_cannot_admit_exact_skill_outside_declared_catalogs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "flavor-catalog-boundary"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--empty",
            )
            self.assertEqual(status, 0)
            copy_generation_catalogs(target)
            flavor = target / "flavors" / "lang-python"
            shutil.copytree(
                REPO_ROOT / "flavors" / "lang-python", flavor, dirs_exist_ok=True
            )

            admitted = (
                target
                / "skills"
                / "specification-to-source"
                / "python-portable-application"
                / "SKILL.md"
            )
            ambient = target / "ambient" / "python-portable-application" / "SKILL.md"
            ambient.parent.mkdir(parents=True)
            shutil.copy2(admitted, ambient)
            self.assertEqual(admitted.read_bytes(), ambient.read_bytes())
            self.assertTrue(ambient.resolve().is_relative_to(target.resolve()))
            replace_skill_uri(
                flavor / "flavor.md",
                "../../skills/specification-to-source/"
                "python-portable-application/SKILL.md",
                "../../ambient/python-portable-application/SKILL.md",
            )

            status, envelope = invoke("project", "validate", str(flavor))

            self.assertEqual(status, 2)
            self.assertEqual(envelope["error"]["code"], "generate.skill_not_cataloged")

    def test_manifest_catalog_graph_is_the_only_skill_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "renamed-skill-catalog"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
                "--flavor",
                "cpp",
                "--flavor",
                "javascript",
                "--flavor",
                "rust",
                "--flavor",
                "google-workspace",
                "--empty",
            )
            self.assertEqual(status, 0)
            copy_generation_catalogs(target)
            component = copy_hello_component(target)
            approved = target / "approved-skills" / "specification-to-source"
            shutil.copytree(REPO_ROOT / "skills" / "specification-to-source", approved)
            manifest_path = target / "literate.project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            # The renamed generation catalog remains the only generation-skill
            # authority. Keep the provider-neutral agent taxonomy declared as its own
            # subroot so the root onboarding sentinel's routed references remain
            # closed under the shared AgentSkillCatalog contract.
            manifest["skill_roots"] = ["approved-skills", "skills/agent"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            root_skill = target / "SKILL.md"
            root_skill.write_text(
                root_skill.read_text(encoding="utf-8").replace(
                    "skills/specification-to-source/repository-layout/SKILL.md",
                    "approved-skills/specification-to-source/"
                    "repository-layout/SKILL.md",
                ),
                encoding="utf-8",
            )
            for skill_id in (
                "portable-specification-planning",
                "portable-application-implementation",
            ):
                replace_skill_uri(
                    component / "component.md",
                    f"skills/specification-to-source/{skill_id}/SKILL.md",
                    f"approved-skills/specification-to-source/{skill_id}/SKILL.md",
                )
            # Every Flavor that names a generation skill must follow the renamed
            # catalog, not just the build-system one.
            for flavor, skill in (
                ("build-bazel", "bazel-build-system"),
                ("lang-cpp", "cpp17-portable-json-application"),
                ("lang-javascript", "javascript-portable-json-application"),
                ("lang-python", "python-portable-application"),
                ("lang-rust", "rust-portable-json-application"),
            ):
                replace_skill_uri(
                    target / "flavors" / flavor / "flavor.md",
                    f"../../skills/specification-to-source/{skill}/SKILL.md",
                    f"../../approved-skills/specification-to-source/{skill}/SKILL.md",
                )
            replace_skill_uri(
                target / "components" / "document-pair" / "component.md",
                "skills/specification-to-source/document-pair/SKILL.md",
                "approved-skills/specification-to-source/document-pair/SKILL.md",
            )

            refresh_authority_review(target)

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 0)
            result = envelope["result"]
            assert isinstance(result, dict)
            catalogs = result["catalogs"]
            assert isinstance(catalogs, dict)
            self.assertEqual(catalogs["skill"], ["approved-skills", "skills/agent"])
            skills = result["specification_to_source_skills"]
            assert isinstance(skills, list)
            self.assertTrue(skills)
            skill_ids = [item["skill_id"] for item in skills]
            self.assertEqual(len(skill_ids), len(set(skill_ids)))
            self.assertTrue(
                all(
                    str(item["source"]).startswith("project catalog approved-skills/")
                    for item in skills
                )
            )

    def test_skill_catalog_subroot_cannot_be_a_symlink_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "catalog-symlink-boundary"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            external = root / "external-skills" / "specification-to-source"
            shutil.copytree(REPO_ROOT / "skills" / "specification-to-source", external)
            subroot = target / "skills" / "specification-to-source"
            if subroot.exists():
                shutil.rmtree(subroot)
            try:
                subroot.symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 2)
            self.assertEqual(envelope["error"]["code"], "project.skill_invalid")

    def test_case_skills_come_from_all_declared_roots_not_ambient_conventions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "inverse-roots"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            source_catalog = REPO_ROOT / "skills" / "source-to-specification"
            skill_ids = sorted(path.name for path in source_catalog.iterdir())
            for index, skill_id in enumerate(skill_ids):
                declared = (
                    target
                    / ("reviewed-a" if index % 2 == 0 else "reviewed-b")
                    / "source-to-specification"
                    / skill_id
                )
                shutil.copytree(source_catalog / skill_id, declared)
            ambient = target / "skills" / "source-to-specification"
            shutil.copytree(source_catalog, ambient, dirs_exist_ok=True)
            ambient_manifest = ambient / "architecture" / "SKILL.md"
            value, _body = parse_authoring_markdown(
                ambient_manifest.read_bytes(), source=str(ambient_manifest)
            )
            ambient_manifest.write_bytes(
                render_authoring_markdown(
                    value, "Ambient content must not be selected."
                )
            )
            manifest_path = target / "literate.project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["skill_roots"] = [
                "reviewed-a",
                "reviewed-b",
                "skills/agent",
                "skills/specification-to-source/repository-layout",
            ]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            case = target / "components" / "state-machine"
            shutil.copytree(
                REPO_ROOT
                / "tests"
                / "fixtures"
                / "source_to_specification"
                / "state-machine",
                case,
            )

            status, envelope = invoke("spec", "derive", str(case / "case.json"))

            self.assertEqual(status, 0)
            result = envelope["result"]["result"]
            self.assertEqual(
                [item["skill"]["skill_id"] for item in result["observations"]],
                ["api-surface", "behavior-state"],
            )

    def test_explicit_inverse_skill_root_overrides_discovered_project_catalog(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "explicit-inverse-root"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            case = target / "components" / "state-machine"
            shutil.copytree(
                REPO_ROOT
                / "tests"
                / "fixtures"
                / "source_to_specification"
                / "state-machine",
                case,
            )

            status, _envelope = invoke(
                "spec",
                "derive",
                str(case / "case.json"),
                "--skills-root",
                str(REPO_ROOT / "skills" / "source-to-specification"),
            )

            self.assertEqual(status, 0)

    def test_project_validation_reports_inverse_skills_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "inverse-report"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            copy_inverse_catalog(target)
            refresh_authority_review(target)

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 0)
            result = envelope["result"]
            inverse = result["source_to_specification_skills"]
            inverse_ids = [item["skill_id"] for item in inverse]
            forward_ids = [
                item["skill_id"] for item in result["specification_to_source_skills"]
            ]
            self.assertEqual(len(inverse_ids), len(set(inverse_ids)))
            self.assertEqual(len(forward_ids), len(set(forward_ids)))
            self.assertFalse(set(inverse_ids) & set(forward_ids))
            self.assertTrue(
                {"architecture", "api-surface", "language-python", "tests"}
                <= set(inverse_ids)
            )
            self.assertTrue(
                {
                    "bazel-build-system",
                    "portable-application-implementation",
                    "portable-specification-planning",
                    "python-portable-application",
                }
                <= set(forward_ids)
            )

    def test_project_validation_rejects_invalid_inverse_skill_catalogs(self) -> None:
        cases = (
            (
                "malformed",
                lambda target, catalog: _mutate_skill_markdown(
                    catalog / "architecture" / "SKILL.md",
                    lambda value: value.update({"unknown": True}),
                ),
                "project.source_to_specification_skill_invalid",
            ),
            (
                "missing-dependency",
                lambda target, catalog: _mutate_skill_markdown(
                    catalog / "architecture" / "SKILL.md",
                    lambda value: value.update({"dependencies": ["absent"]}),
                ),
                "project.source_to_specification_skill_dependency_missing",
            ),
            (
                "cycle",
                lambda target, catalog: _mutate_skill_markdown(
                    catalog / "architecture" / "SKILL.md",
                    lambda value: value.update({"after": ["operations"]}),
                ),
                "project.source_to_specification_skill_order_cycle",
            ),
            (
                "duplicate",
                _add_duplicate_inverse_skill,
                "project.source_to_specification_skill_duplicate",
            ),
        )
        for name, mutate, expected_code in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / name
                status, _envelope = invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--flavor",
                    "bazel",
                )
                self.assertEqual(status, 0)
                catalog = copy_inverse_catalog(target)
                mutate(target, catalog)

                status, envelope = invoke("project", "validate", str(target))

                self.assertEqual(status, 2)
                self.assertEqual(envelope["error"]["code"], expected_code)

    def test_declared_documentation_is_identified_and_ambient_docs_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "renamed-docs"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            (target / "docs").rename(target / "handbook")
            manifest_path = target / "literate.project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["documentation_roots"] = ["handbook"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            skill_path = target / "SKILL.md"
            skill_path.write_text(
                skill_path.read_text(encoding="utf-8").replace("docs/", "handbook/"),
                encoding="utf-8",
            )
            refresh_authority_review(target)
            ambient = target / "notes" / "README.md"
            ambient.parent.mkdir()
            ambient.write_text("# Ambient\n", encoding="utf-8")

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 0)
            documents = envelope["result"]["documentation"]
            self.assertTrue(documents)
            self.assertTrue(
                all(item["path"].startswith("handbook/") for item in documents)
            )
            self.assertTrue(
                all(item["identity"].startswith("sha256:") for item in documents)
            )
            self.assertNotIn("notes/README.md", {item["path"] for item in documents})

            with skill_path.open("a", encoding="utf-8") as stream:
                stream.write("\nRead [ambient notes](notes/README.md).\n")
            status, envelope = invoke("project", "validate", str(target))
            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"],
                "project.agent_skill_documentation_undeclared",
            )

    def test_declared_documentation_rejects_symlinks_and_non_utf8_markdown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "invalid-docs"
            status, _envelope = invoke(
                "init",
                str(target),
                "--flavor",
                "python",
                "--flavor",
                "macos",
                "--flavor",
                "bazel",
            )
            self.assertEqual(status, 0)
            document = target / "docs" / "linked.md"
            try:
                document.symlink_to(target / "docs" / "README.md")
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 2)
            self.assertEqual(envelope["error"]["code"], "project.documentation_invalid")
            document.unlink()
            document.write_bytes(b"\xff\xfe")

            status, envelope = invoke("project", "validate", str(target))

            self.assertEqual(status, 2)
            self.assertEqual(envelope["error"]["code"], "project.documentation_invalid")

    def test_plan_exposes_the_complete_resolved_flow_without_generation(self) -> None:
        # macOS exposes /var as a symlink to /private/var.  This test exercises the
        # no-symlink project boundary, so allocate beneath the resolved temp root.
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            target = Path(directory) / "project"
            self.assertEqual(
                invoke(
                    "init",
                    str(target),
                    "--flavor",
                    "python",
                    "--flavor",
                    "macos",
                    "--empty",
                )[0],
                0,
            )
            copy_generation_catalogs(target)
            shutil.copytree(
                REPO_ROOT / "flavors", target / "flavors", dirs_exist_ok=True
            )
            component = copy_hello_component(target)
            self.assertEqual(
                invoke(
                    "lock",
                    str(component),
                    "--target=host",
                )[0],
                0,
            )
            status, envelope = invoke(
                "plan",
                str(component),
                "--target=host",
            )
        self.assertEqual(status, 0)
        result = envelope["result"]
        assert isinstance(result, dict)
        generation_skill_ids = [
            item["skill_id"] for item in result["generation_skills"]
        ]
        self.assertEqual(len(generation_skill_ids), len(set(generation_skill_ids)))
        self.assertTrue(
            {
                "portable-specification-planning",
                "portable-application-implementation",
                "python-portable-application",
                "make-build-system",
            }
            <= set(generation_skill_ids)
        )
        self.assertLess(
            generation_skill_ids.index("portable-specification-planning"),
            generation_skill_ids.index("portable-application-implementation"),
        )
        self.assertLess(
            generation_skill_ids.index("portable-application-implementation"),
            generation_skill_ids.index("python-portable-application"),
        )
        self.assertEqual(
            [item["stage_id"] for item in result["workflow"]["model_stages"]],
            ["plan", "generate"],
        )
        self.assertEqual(
            result["routing"]["decision_timing"],
            "generation-after-coding-cli-selection",
        )
        self.assertNotIn("route_decisions", result["routing"])
        self.assertEqual(result["required_entrypoint"], "source/main.py")


class ProjectContractTests(unittest.TestCase):
    def test_project_source_intelligence_is_explicit_and_portable(self) -> None:
        value = json.loads((REPO_ROOT / "literate.project.json").read_text())
        definition = ProjectDefinition.from_dict(value)
        self.assertEqual(definition.source_intelligence.provider_id, "none")
        self.assertIsNone(definition.source_intelligence.minimum_version)
        self.assertIsNone(definition.source_intelligence.artifact_path)

        missing = dict(value)
        del missing["source_intelligence"]
        with self.assertRaisesRegex(
            ValueError, "missing .*fields.*source_intelligence"
        ):
            ProjectDefinition.from_dict(missing)

        invalid = json.loads(json.dumps(value))
        invalid["source_intelligence"]["provider_id"] = "other-index"
        invalid["source_intelligence"]["command"] = "tools/other"
        invalid["source_intelligence"]["minimum_version"] = "1.1.1"
        invalid["source_intelligence"]["artifact_path"] = (
            ".source-intelligence/index.db"
        )
        with self.assertRaisesRegex(ValueError, "portable command name"):
            ProjectDefinition.from_dict(invalid)

    def test_project_default_flavors_are_ordered_preferences_with_strict_syntax(
        self,
    ) -> None:
        value = json.loads((REPO_ROOT / "literate.project.json").read_text())
        value["default_flavor_selectors"] = ["+bazel", "-bazel", "+bazel"]

        definition = ProjectDefinition.from_dict(value)

        self.assertEqual(
            definition.default_flavor_selectors,
            ("+bazel", "-bazel", "+bazel"),
        )
        value["default_flavor_selectors"] = ["bazel"]
        with self.assertRaisesRegex(ValueError, r"ordered \+flavor/-flavor"):
            ProjectDefinition.from_dict(value)

    def test_project_component_flavors_are_ordered_path_scoped_overrides(self) -> None:
        value = json.loads((REPO_ROOT / "literate.project.json").read_text())
        value["component_flavor_selectors"] = {
            "components/web": ["-lang-python", "+lang-javascript"]
        }

        definition = ProjectDefinition.from_dict(value)

        self.assertEqual(
            definition.component_flavor_selectors,
            {"components/web": ("-lang-python", "+lang-javascript")},
        )
        value["component_flavor_selectors"] = {"../web": ["+lang-javascript"]}
        with self.assertRaisesRegex(ValueError, "normalized relative"):
            ProjectDefinition.from_dict(value)

    def test_project_definition_rejects_implicit_or_escaping_catalogs(self) -> None:
        value = json.loads((REPO_ROOT / "literate.project.json").read_text())
        definition = ProjectDefinition.from_dict(value)
        self.assertEqual(ProjectDefinition.from_dict(definition.to_dict()), definition)
        value["skill_roots"] = ["../ambient-skills"]
        with self.assertRaisesRegex(ValueError, "normalized relative"):
            ProjectDefinition.from_dict(value)

    def test_project_definition_allows_empty_optional_catalogs(self) -> None:
        value = json.loads((REPO_ROOT / "literate.project.json").read_text())
        for field in CATALOG_FIELDS:
            value[field] = []

        definition = ProjectDefinition.from_dict(value)

        self.assertTrue(all(not getattr(definition, field) for field in CATALOG_FIELDS))

    def test_project_definition_preserves_legacy_manifests_without_a_receipt(
        self,
    ) -> None:
        value = json.loads((REPO_ROOT / "literate.project.json").read_text())
        del value["test_receipt"]
        value.pop("test_receipt_policy", None)

        definition = ProjectDefinition.from_dict(value)

        self.assertIsNone(definition.test_receipt)
        self.assertNotIn("test_receipt", definition.to_dict())

    def test_project_definition_rejects_receipt_policy_without_a_path(self) -> None:
        value = json.loads((REPO_ROOT / "literate.project.json").read_text())
        del value["test_receipt"]

        with self.assertRaisesRegex(ValueError, "requires a configured test_receipt"):
            ProjectDefinition.from_dict(value)

    def test_project_definition_requires_a_normalized_receipt_path(self) -> None:
        value = json.loads((REPO_ROOT / "literate.project.json").read_text())
        value["test_receipt"] = "verification/../current.json"

        with self.assertRaisesRegex(ValueError, "normalized relative"):
            ProjectDefinition.from_dict(value)

        value["test_receipt"] = "docs/current.json"
        with self.assertRaisesRegex(ValueError, "declared catalog root"):
            ProjectDefinition.from_dict(value)

        value["test_receipt"] = "literate.project.json"
        with self.assertRaisesRegex(ValueError, "project manifest"):
            ProjectDefinition.from_dict(value)

    def test_project_definition_requires_root_skill(self) -> None:
        value = json.loads((REPO_ROOT / "literate.project.json").read_text())
        value["agent_skill"] = "docs/SKILL.md"

        with self.assertRaisesRegex(ValueError, "root SKILL.md"):
            ProjectDefinition.from_dict(value)

    def test_project_definition_requires_declared_documentation(self) -> None:
        value = json.loads((REPO_ROOT / "literate.project.json").read_text())
        value["documentation_roots"] = []

        with self.assertRaisesRegex(ValueError, "documentation root"):
            ProjectDefinition.from_dict(value)


if __name__ == "__main__":
    unittest.main()
