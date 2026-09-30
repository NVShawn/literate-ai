from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.diagnostics import debug_diagnostics
from literate_ai.spec_map import (
    DEBUG_SKILL_ID,
    SPEC_MAP_SCHEMA,
    instrument_generated_tree,
    load_debug_spec_map_skill,
    scan_spec_anchors,
    with_debug_skill_identities,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "spec_map_hello" / "source"


class SpecMapScannerTests(unittest.TestCase):
    def test_scanner_records_comment_line_numbers(self) -> None:
        entries = scan_spec_anchors(FIXTURE)
        kinds = {item.kind: item for item in entries}
        self.assertIn("entrypoint", kinds)
        self.assertEqual(
            kinds["entrypoint"].spec_path,
            "samples/hello-component/component.md",
        )
        self.assertEqual(kinds["entrypoint"].spec_line, 77)
        self.assertEqual(kinds["entrypoint"].generated_path, "app.py")
        self.assertIn("error", kinds)
        self.assertIn("behavior", kinds)

    def test_scanner_ignores_hidden_ancestors_outside_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / ".worktrees" / "checkout" / "source"
            source.mkdir(parents=True)
            (source / "app.py").write_text(
                "# litai:spec samples/example/component.md:7 entrypoint\n",
                encoding="utf-8",
            )

            entries = scan_spec_anchors(source.resolve())

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "entrypoint")

    def test_instrument_writes_sidecar_and_python_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "app.py").write_text(
                FIXTURE.joinpath("app.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            report = instrument_generated_tree(source)
            self.assertEqual(report["schema"], SPEC_MAP_SCHEMA)
            sidecar = Path(report["sidecar"])
            self.assertTrue(sidecar.is_file())
            document = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(document["schema"], SPEC_MAP_SCHEMA)
            self.assertGreaterEqual(len(document["entries"]), 3)
            self.assertTrue(Path(report["python_helper"]).is_file())

    def test_catalog_and_template_debug_skills_are_byte_identical(self) -> None:
        catalog = ROOT / "skills/specification-to-source/debug-spec-map/SKILL.md"
        template = (
            ROOT
            / "src/literate_ai/project_template/skills/specification-to-source"
            / "debug-spec-map/SKILL.md"
        )
        self.assertEqual(catalog.read_bytes(), template.read_bytes())

    def test_debug_skill_identity_is_appended_only_when_debug_is_on(self) -> None:
        stream = __import__("io").StringIO()
        with mock.patch.dict(os.environ, {"LITAI_DEBUG": ""}):
            self.assertIsNone(load_debug_spec_map_skill())
            self.assertEqual(with_debug_skill_identities(()), ())
        with debug_diagnostics("-", json_mode=True, stderr=stream):
            skill = load_debug_spec_map_skill()
            self.assertIsNotNone(skill)
            assert skill is not None
            self.assertEqual(skill.skill_id, DEBUG_SKILL_ID)
            identities = with_debug_skill_identities(())
            self.assertEqual(len(identities), 1)
            self.assertEqual(identities[0], skill.content_identity)


class SpecMapRuntimeTests(unittest.TestCase):
    def _run(
        self, request: object, *, debug: bool, json_mode: bool
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "app.py").write_text(
                FIXTURE.joinpath("app.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            instrument_generated_tree(source)
            environment = {
                **os.environ,
                "LITAI_DEBUG": "1" if debug else "",
                "LITAI_DEBUG_JSON": "1" if json_mode else "",
                "LITAI_SPEC_MAP": str(source / ".literate" / "spec-map.json"),
            }
            return subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(source / "app.py"),
                    json.dumps([request]),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                cwd=source,
            )

    def test_debug_prints_one_entrypoint_map_and_keeps_stdout_json(self) -> None:
        completed = self._run(
            {"name": "LitAI", "messages": ["hi there"]},
            debug=True,
            json_mode=True,
        )
        self.assertEqual(completed.returncode, 0)
        result = json.loads(completed.stdout)
        self.assertEqual(result["greeting"], "Hello, LitAI!")
        events = [
            json.loads(line)
            for line in completed.stderr.splitlines()
            if line.strip().startswith("{")
        ]
        maps = [item for item in events if item.get("event") == "spec-map"]
        self.assertEqual(len(maps), 1)
        self.assertEqual(maps[0]["kind"], "entrypoint")
        self.assertEqual(maps[0]["spec"]["line"], 77)

    def test_missing_name_prints_error_map_on_stderr(self) -> None:
        completed = self._run({"name": ""}, debug=True, json_mode=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(completed.stdout.strip())
        events = [
            json.loads(line)
            for line in completed.stderr.splitlines()
            if line.strip().startswith("{")
        ]
        errors = [
            item
            for item in events
            if item.get("event") == "spec-map" and item.get("kind") == "error"
        ]
        self.assertEqual(len(errors), 1)
        self.assertIn("ValueError", errors[0]["message"])

    def test_debug_off_prints_nothing_on_stderr(self) -> None:
        completed = self._run({"name": "LitAI"}, debug=False, json_mode=True)
        self.assertEqual(completed.returncode, 0)
        json.loads(completed.stdout)
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
