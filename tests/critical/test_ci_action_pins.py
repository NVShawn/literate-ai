"""Release CI must not change when a third-party action tag moves."""

from __future__ import annotations

import importlib.util
import json
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from literate_ai.compatibility.ova import load_yaml_subset

ROOT = Path(__file__).resolve().parents[2]
USES = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*([^\n#]+)")
PIN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}")


class CiActionPinTests(unittest.TestCase):
    def test_all_external_actions_use_full_commit_ids(self) -> None:
        references = []
        for path in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
            for match in USES.finditer(path.read_text()):
                reference = match[1].strip().strip("\"'")
                if reference.startswith("./"):
                    continue
                references.append(reference)
                with self.subTest(workflow=path.name, reference=reference):
                    self.assertRegex(reference, "^" + PIN.pattern + "$")
        self.assertTrue(references, "pin check must inspect actual action references")

    def test_policy_rejects_tags_short_hashes_and_expressions(self) -> None:
        for reference in (
            "actions/checkout@v6",
            "actions/checkout@abcdef0",
            "${{ matrix.action }}",
        ):
            self.assertIsNone(PIN.fullmatch(reference))

    def test_elixir_matrix_requires_explicit_hex_and_unskipped_native_mix(self):
        workflow = load_yaml_subset((ROOT / ".github/workflows/ci.yml").read_text())
        all_steps = workflow["jobs"]["checks"]["steps"]
        steps = [
            step
            for step in all_steps
            if str(step.get("if", "")).startswith("matrix.task == 'elixir-native'")
        ]
        for step in all_steps:
            text = json.dumps(step)
            if any(
                marker in text
                for marker in ("elixir", "setup-beam", "LITAI_HEX", "Hex")
            ):
                with self.subTest(step=step.get("name") or step.get("uses")):
                    self.assertIn(step, steps, "Elixir steps stay in Elixir cells")
        stage = next(
            step for step in steps if "stage_elixir_hex.py" in step.get("run", "")
        )
        native = next(step for step in steps if "--junitxml=" in step.get("run", ""))
        self.assertLess(steps.index(stage), steps.index(native))
        self.assertIn("${{ matrix.hex_args }}", stage["run"])
        self.assertEqual(
            native["env"]["LITAI_ELIXIR_EXPECT_OTP"], "${{ matrix.otp_major }}"
        )
        rows = json.loads((ROOT / ".github/ci-matrix.json").read_text())
        elixir_rows = [row for row in rows if row["task"] == "elixir-native"]
        self.assertEqual(len(elixir_rows), 6)
        self.assertEqual(
            {(row["os"].split("-")[0], row["elixir"]) for row in elixir_rows},
            {
                (os_name, version)
                for os_name in ("ubuntu", "macos", "windows")
                for version in ("1.18.0", "1.20.4")
            },
        )
        for row in elixir_rows:
            self.assertEqual(row["otp_major"], row["otp"].split(".")[0])
            self.assertEqual(
                row["hex_args"],
                "--build-from-source" if row["otp_major"] == "29" else "",
            )
        spec = importlib.util.spec_from_file_location(
            "select_ci_profile", ROOT / "scripts/select_ci_profile.py"
        )
        selector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selector)
        for event, base in (
            ("pull_request", "main"),
            ("pull_request", "release/1.2"),
            ("push", ""),
        ):
            with self.subTest(event=event, base=base):
                selected = selector.select_matrix(event, base)["include"]
                self.assertEqual(
                    [row for row in selected if row["task"] == "elixir-native"],
                    elixir_rows,
                    "every native Elixir cell runs on every pull request",
                )
        self.assertEqual(native["env"]["LITERATE_AI_ELIXIR_MIX_QUALIFICATION"], "1")
        self.assertEqual(native["env"]["LITERATE_AI_ELIXIR_BUILD_SYSTEMS"], "1")
        self.assertEqual(
            native["env"]["LITAI_HEX_EBIN"],
            "${{ github.workspace }}/_build/elixir/hex/hex-2.5.1/ebin",
        )
        self.assertIn("test_elixir_mix_qualification.py", native["run"])
        self.assertIn("test_elixir_mix_graph_qualification.py", native["run"])
        self.assertIn("test_elixir_mix_shared_qualification.py", native["run"])
        self.assertIn("-o addopts=''", native["run"])
        gate = next(step for step in steps if step.get("shell") == "python")
        probe = compile(gate["run"], "ci.yml:elixir-native-gate", "exec")
        for counts, accepted in (
            ((18, 0, 0, 0), True),
            ((18, 0, 0, 1), False),
            ((17, 0, 0, 0), False),
            ((18, 1, 0, 0), False),
            ((18, 0, 1, 0), False),
        ):
            root = ET.Element("testsuites")
            suite = ET.SubElement(
                root,
                "testsuite",
                dict(
                    zip(
                        ("tests", "failures", "errors", "skipped"),
                        map(str, counts),
                        strict=True,
                    )
                ),
            )
            for index in range(counts[0]):
                ET.SubElement(suite, "testcase", {"name": f"fixture-{index}"})
            with (
                self.subTest(counts=counts),
                mock.patch.object(ET, "parse", return_value=ET.ElementTree(root)),
                mock.patch("builtins.print"),
            ):
                if accepted:
                    exec(probe, {})
                else:
                    with self.assertRaises(SystemExit):
                        exec(probe, {})
