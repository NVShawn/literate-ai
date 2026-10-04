"""Canonical project onboarding, validation, and plan command tests."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from literate_ai.cli import main
from tests.support.root_parent_adapter import root_parent_for_fixture_project

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


def copy_hello_component(target: Path) -> Path:
    component = target / "samples" / "hello-component"
    if not component.is_dir():
        shutil.copytree(REPO_ROOT / "samples" / "hello-component", component)
    for generated_lock in component.glob("component.*.json"):
        generated_lock.unlink()
    return component


class ProjectCliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
