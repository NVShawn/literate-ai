"""Agent discovery observations never mutate authored or retained project trees."""

import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.agent_entrypoints import audit_agent_entrypoints
from literate_ai.cli import main
from literate_ai.projects import load_project
from tests.smoke.test_project_reparenting import initialize_root


def configure_custom_skill(root, *, repair_delegates=True):
    from dataclasses import replace

    import yaml

    from literate_ai.projects import ProjectConfigurationStore

    declared = "docs/onboarding.md"
    target = root / declared
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\nname: literate-ai\ndescription: Project onboarding authority\n---\n\n"
        "Follow this project's [documentation](README.md) and "
        "[test matrix](user/test-matrix.md).\n"
    )
    store = ProjectConfigurationStore(root)
    snapshot = store.read()
    store.update(snapshot, replace(snapshot.definition, agent_skill=declared))
    (root / "SKILL.md").unlink()
    if repair_delegates:
        for name in ["AGENTS.md", "CLAUDE.md"]:
            (root / name).write_text(
                "# Custom project rules\n\nRetain our workflow. "
                f"Read and follow `{declared}`.\n"
            )
        cursor = root / ".cursor/rules/literate-ai.mdc"
        cursor.write_text(
            cursor.read_text().replace("../../SKILL.md", "../../" + declared)
        )
        metadata = root / "agents/openai.yaml"
        value = yaml.safe_load(metadata.read_text())
        value["interface"]["default_prompt"] += " Read and follow " + declared + "."
        metadata.write_text(yaml.safe_dump(value))
    return declared


class AgentEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "project"
        initialize_root(self.root)

    def snapshot(self):
        return {
            p.relative_to(self.root).as_posix(): (
                ("directory", None)
                if p.is_dir()
                else (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            )
            for p in self.root.rglob("*")
            if p.is_file() or p.is_dir()
        }

    def audit(self):
        return audit_agent_entrypoints(load_project(self.root))

    def test_initialized_project_is_ready_and_audit_is_read_only_idempotent(self):
        before = self.snapshot()
        first = self.audit()
        self.assertTrue(first["ready"])
        self.assertFalse(first["writes"])
        self.assertFalse(first["lifecycle_admitted"])
        self.assertEqual(first, self.audit())
        self.assertEqual(self.snapshot(), before)

    def test_public_cli_from_nested_directory_reports_missing_shim_without_writes(self):
        (self.root / "AGENTS.md").unlink()
        nested = self.root / "nested"
        nested.mkdir()
        before = self.snapshot()
        stdout, stderr = io.StringIO(), io.StringIO()
        status = main(
            ["project", "agent-entrypoints", str(nested)], stdout=stdout, stderr=stderr
        )
        self.assertEqual(status, 1, stderr.getvalue())
        report = json.loads(stdout.getvalue())["result"]
        self.assertFalse(report["ready"])
        entry = next(x for x in report["entries"] if x["path"] == "AGENTS.md")
        self.assertEqual(entry["state"], "missing")
        self.assertIn("SKILL.md", entry["required_action"])
        self.assertEqual(self.snapshot(), before)

    def test_missing_nested_entrypoint_directories_are_missing_not_created(self):
        import shutil

        shutil.rmtree(self.root / ".cursor")
        shutil.rmtree(self.root / "agents")
        report = self.audit()
        states = {e["path"]: e["state"] for e in report["entries"]}
        self.assertEqual(states[".cursor/rules/literate-ai.mdc"], "missing")
        self.assertEqual(states["agents/openai.yaml"], "missing")
        self.assertFalse((self.root / ".cursor").exists())
        self.assertFalse((self.root / "agents").exists())

    def test_customized_current_instructions_preserve_exact_bytes(self):
        content = (
            b"# Custom rules\r\n\r\nKeep our workflow. Read and follow `SKILL.md`.\r\n"
        )
        (self.root / "AGENTS.md").write_bytes(content)
        before = self.snapshot()
        self.assertTrue(self.audit()["ready"])
        self.assertEqual(self.snapshot(), before)

    def test_inert_or_wrong_targets_do_not_satisfy_delegation(self):
        for content in [
            "Read and follow `OLD_SKILL.md`.\n",
            "<!-- Read and follow SKILL.md -->\n",
            "<!-- Read and follow SKILL.md\n",
            "```md\nRead and follow SKILL.md\n```\n",
            "    Read and follow SKILL.md\n",
        ]:
            with self.subTest(content=content):
                (self.root / "AGENTS.md").write_text(content)
                report = self.audit()
                self.assertFalse(report["ready"])
                self.assertEqual(
                    next(e for e in report["entries"] if e["path"] == "AGENTS.md")[
                        "state"
                    ],
                    "stale",
                )
                self.assertEqual((self.root / "AGENTS.md").read_text(), content)

    def test_openai_alias_must_be_in_the_actual_prompt(self):
        path = self.root / "agents/openai.yaml"
        path.write_text(
            '# $literate-ai\ninterface:\n  default_prompt: "Use another skill"\n'
        )
        self.assertFalse(self.audit()["ready"])
        path.write_text('interface:\n  default_prompt: "Use $literate-ai-something"\n')
        self.assertFalse(self.audit()["ready"])

    def test_scalar_yaml_alias_remains_valid_without_writes(self):
        path = self.root / "agents/openai.yaml"
        path.write_text(
            'prompt: &prompt "Use $literate-ai"\ninterface: {default_prompt: *prompt}\n'
        )
        before = self.snapshot()
        self.assertTrue(self.audit()["ready"])
        self.assertEqual(self.snapshot(), before)

    def test_yaml_container_alias_expansion_is_refused_in_both_owned_documents(self):
        graph = "a0: &a0 {key: value}\n" + "".join(
            f"a{i}: &a{i} {{<<: [*a{i - 1}, *a{i - 1}]}}\n" for i in range(1, 13)
        )
        for relative in ("agents/openai.yaml", "SKILL.md"):
            with self.subTest(relative=relative):
                path = self.root / relative
                original = path.read_bytes()
                content = (
                    graph + 'interface: {default_prompt: "Use $literate-ai"}\n'
                    if relative.endswith("yaml")
                    else "---\nname: literate-ai\n" + graph + "---\n"
                )
                path.write_text(content)
                before = self.snapshot()
                report = self.audit()
                self.assertFalse(report["ready"])
                finding = (
                    next(e for e in report["entries"] if e["path"] == relative)
                    if relative.endswith("yaml")
                    else report["agent_skill"]
                )
                self.assertEqual(finding["state"], "unsafe-or-invalid")
                self.assertEqual(self.snapshot(), before)
                path.write_bytes(original)

    def test_yaml_depth_and_node_limits_are_read_only_findings(self):
        path = self.root / "agents/openai.yaml"
        for content in (
            "nested: " + "[" * 40 + "0" + "]" * 40 + "\n",
            "items: [" + ",".join("0" for _ in range(4100)) + "]\n",
            "cycle: &cycle [*cycle]\n",
        ):
            with self.subTest(size=len(content)):
                path.write_text(
                    content + 'interface: {default_prompt: "Use $literate-ai"}\n'
                )
                before = self.snapshot()
                report = self.audit()
                finding = next(
                    e for e in report["entries"] if e["path"] == "agents/openai.yaml"
                )
                self.assertEqual(finding["state"], "unsafe-or-invalid")
                self.assertEqual(self.snapshot(), before)

    @unittest.skipIf(os.name == "nt", "requires POSIX symbolic links")
    def test_indirect_and_broken_shim_or_ancestor_are_unsafe_without_following(self):
        import shutil

        agents = self.root / "AGENTS.md"
        agents.unlink()
        agents.symlink_to(self.root / "absent")
        report = self.audit()
        self.assertEqual(
            next(e for e in report["entries"] if e["path"] == "AGENTS.md")["state"],
            "unsafe-or-invalid",
        )
        agents.unlink()
        external = self.root.parent / "outside"
        external.mkdir()
        target = external / "rules/literate-ai.mdc"
        target.parent.mkdir()
        target.write_text("Read and follow ../../SKILL.md")
        shutil.rmtree(self.root / ".cursor")
        (self.root / ".cursor").symlink_to(external, target_is_directory=True)
        report = self.audit()
        self.assertEqual(
            next(
                e
                for e in report["entries"]
                if e["path"] == ".cursor/rules/literate-ai.mdc"
            )["state"],
            "unsafe-or-invalid",
        )
        self.assertEqual(target.read_text(), "Read and follow ../../SKILL.md")

    def test_invalid_encoding_and_size_are_bounded_findings(self):
        path = self.root / "AGENTS.md"
        for raw in [b"\xff", b"x" * (1024 * 1024 + 1)]:
            path.write_bytes(raw)
            self.assertFalse(self.audit()["ready"])
            self.assertEqual(path.read_bytes(), raw)

    def test_missing_or_invalid_onboarding_skill_is_not_ready(self):
        path = self.root / "SKILL.md"
        path.unlink()
        self.assertEqual(self.audit()["agent_skill"]["state"], "missing")
        path.write_text("No canonical onboarding frontmatter\n")
        self.assertEqual(self.audit()["agent_skill"]["state"], "invalid")

    def test_crlf_onboarding_frontmatter_remains_valid(self):
        path = self.root / "SKILL.md"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertTrue(self.audit()["ready"])

    def test_cli_refuses_missing_project_with_bounded_error(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(
            ["project", "agent-entrypoints", str(self.root.parent / "absent")],
            stdout=stdout,
            stderr=stderr,
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(
            json.loads(stderr.getvalue())["error"]["code"], "project.not_found"
        )

    def test_nearest_nested_project_is_selected_without_repairing_parent_or_child(self):
        child = self.root / "parents" / "child"
        initialize_root(child)
        (child / "AGENTS.md").unlink()
        nested = child / "nested"
        nested.mkdir()
        before = self.snapshot()
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(
            ["project", "agent-entrypoints", str(nested)], stdout=stdout, stderr=stderr
        )
        self.assertEqual(code, 1, stderr.getvalue())
        self.assertFalse(json.loads(stdout.getvalue())["result"]["ready"])
        self.assertTrue(self.audit()["ready"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((child / "AGENTS.md").exists())

    def test_independent_absorbed_agent_metadata_is_not_root_authority(self):
        from dataclasses import replace

        from literate_ai.contracts.repository_orchestration import (
            RepositoryOrchestration,
            RepositoryPin,
        )
        from literate_ai.projects import ProjectConfigurationStore

        store = ProjectConfigurationStore(self.root)
        snapshot = store.read()
        binding = RepositoryOrchestration(
            "sha256:" + "c" * 64,
            (
                RepositoryPin(
                    "metadata", "agents", "https://example.test/metadata.git", "a" * 40
                ),
            ),
            (),
        )
        store.update(
            snapshot, replace(snapshot.definition, repository_orchestration=binding)
        )
        (self.root / "agents/openai.yaml").write_bytes(b"\xff")
        before = self.snapshot()
        report = self.audit()
        self.assertTrue(report["ready"])
        self.assertEqual(
            next(e for e in report["entries"] if e["path"] == "agents/openai.yaml")[
                "state"
            ],
            "independent-child",
        )
        self.assertEqual(self.snapshot(), before)

    def test_public_audit_has_no_host_configuration_journal_or_telemetry_effects(self):
        from contextlib import ExitStack
        from unittest.mock import patch

        from literate_ai.cli import dispatch

        before = self.snapshot()
        with ExitStack() as stack:
            for name in [
                "maybe_host_self_update",
                "ensure_user_mcp_catalog",
                "journal_mutagenic_event",
                "_fan_out_operator_mcp_event",
                "_run_operator_mcp_discovery",
            ]:
                stack.enter_context(
                    patch.object(dispatch, name, side_effect=AssertionError(name))
                )
            stack.enter_context(
                patch.object(
                    dispatch.PerformanceRecorder,
                    "span",
                    side_effect=AssertionError("telemetry"),
                )
            )
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(
                main(
                    ["project", "agent-entrypoints", str(self.root)],
                    stdout=out,
                    stderr=err,
                ),
                0,
                err.getvalue(),
            )
        self.assertEqual(self.snapshot(), before)

    def test_debug_files_and_discovery_are_refused_before_writes(self):
        output = self.root / "debug.log"
        for options in [["--debug", str(output)], ["--discover-mcps"]]:
            with self.subTest(options=options):
                out, err = io.StringIO(), io.StringIO()
                code = main(
                    [*options, "project", "agent-entrypoints", str(self.root)],
                    stdout=out,
                    stderr=err,
                )
                self.assertNotEqual(code, 0)
                self.assertEqual(
                    json.loads(err.getvalue())["error"]["code"],
                    "project.agent_entrypoints_read_only_options",
                )
                self.assertFalse(output.exists())

    def test_report_matches_its_published_schema_and_cannot_claim_admission(self):
        import copy

        import jsonschema

        schema = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "schemas/v2/agent-entrypoint-audit.schema.json"
            ).read_text()
        )
        report = self.audit()
        jsonschema.Draft202012Validator(schema).validate(report)
        for name in ["writes", "lifecycle_admitted"]:
            altered = copy.deepcopy(report)
            altered[name] = True
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(schema).validate(altered)
        (self.root / "AGENTS.md").unlink()
        jsonschema.Draft202012Validator(schema).validate(self.audit())

    def test_deep_provider_yaml_returns_bounded_finding_without_repair(self):
        path = self.root / "agents/openai.yaml"
        content = "[" * 2000 + "]" * 2000
        path.write_text(content)
        report = self.audit()
        self.assertFalse(report["ready"])
        self.assertEqual(
            next(e for e in report["entries"] if e["path"] == "agents/openai.yaml")[
                "state"
            ],
            "unsafe-or-invalid",
        )
        self.assertEqual(path.read_text(), content)

    def test_custom_v2_skill_path_is_observed_and_preserved_without_default_file(self):
        from literate_ai.projects import validate_project_structure
        from tests.support.fixtures_test_schema_catalog import SchemaCatalog

        declared = configure_custom_skill(self.root)
        project = load_project(self.root)
        SchemaCatalog().validate(
            project.definition.SCHEMA, project.definition.to_dict()
        )
        self.assertEqual(validate_project_structure(project)["agent_skill"], declared)
        before = self.snapshot()
        report = self.audit()
        self.assertTrue(report["ready"])
        self.assertEqual(report["agent_skill"]["path"], declared)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "SKILL.md").exists())

    def test_custom_skill_reports_default_delegates_as_stale_before_authored_repair(
        self,
    ):
        declared = configure_custom_skill(self.root, repair_delegates=False)
        before = self.snapshot()
        report = self.audit()
        self.assertFalse(report["ready"])
        self.assertTrue(all(e["state"] == "stale" for e in report["entries"]))
        self.assertTrue(
            all(declared in e["required_action"] for e in report["entries"])
        )
        self.assertEqual(self.snapshot(), before)

    def test_custom_skill_paths_refuse_cross_platform_escapes_and_aliases(self):
        from dataclasses import replace

        definition = load_project(self.root).definition
        for path in [
            "../SKILL.md",
            "/outside/SKILL.md",
            "C:/outside/SKILL.md",
            "C:\\outside\\SKILL.md",
            "NUL.md",
            "docs/COM1/skill.md",
            "docs/skill.md:stream",
            "docs//skill.md",
        ]:
            with self.subTest(path=path), self.assertRaises((ValueError, TypeError)):
                replace(definition, agent_skill=path)

    def test_legacy_v1_keeps_its_frozen_root_skill_contract(self):
        import copy

        from literate_ai.contracts import ProjectDefinition
        from tests.support.fixtures_test_schema_catalog import SchemaCatalog

        current = load_project(self.root).definition.to_dict()
        legacy = {
            k: current[k]
            for k in [
                "project_id",
                "version",
                "profile",
                "agent_skill",
                "component_roots",
                "flavor_roots",
                "skill_roots",
                "workflow_roots",
                "routing_roots",
                "documentation_roots",
            ]
        }
        legacy["schema"] = "urn:literate-ai:schema:v1:project-definition"
        legacy["source_index"] = {
            "schema": "urn:literate-ai:schema:v1:project-source-index-requirement",
            "provider_id": "codegraph",
            "command": "codegraph",
            "minimum_version": "1.0.0",
            "index_path": ".codegraph/index.json",
        }
        catalog = SchemaCatalog()
        catalog.validate(legacy["schema"], legacy)
        self.assertEqual(ProjectDefinition.from_dict(legacy).agent_skill, "SKILL.md")
        changed = copy.deepcopy(legacy)
        changed["agent_skill"] = "docs/onboarding.md"
        with self.assertRaisesRegex(ValueError, "root SKILL.md"):
            ProjectDefinition.from_dict(changed)
        with self.assertRaises(AssertionError):
            catalog.validate(changed["schema"], changed)

    def test_custom_skill_redirected_parent_refuses_on_native_filesystem(self):
        declared = configure_custom_skill(self.root)
        outside = self.root.parent / "redirected-docs"
        (self.root / "docs").rename(outside)
        if os.name == "nt":
            from tests.support.fixtures_test_component_resolution_audit_store import (
                create_windows_junction,
            )

            create_windows_junction(self.root / "docs", outside)
        else:
            (self.root / "docs").symlink_to(outside, target_is_directory=True)
        original = (outside / "onboarding.md").read_bytes()
        report = self.audit()
        self.assertFalse(report["ready"])
        self.assertEqual(report["agent_skill"]["path"], declared)
        self.assertEqual(report["agent_skill"]["state"], "unsafe-or-invalid")
        self.assertIsNone(report["agent_skill"]["identity"])
        self.assertEqual((outside / "onboarding.md").read_bytes(), original)
