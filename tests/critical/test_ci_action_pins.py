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
    @staticmethod
    def selector():
        spec = importlib.util.spec_from_file_location(
            "select_ci_profile", ROOT / "scripts/select_ci_profile.py"
        )
        selector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selector)
        return selector

    def test_change_scoped_profiles_are_narrow_and_fail_closed(self) -> None:
        selector = self.selector()

        documentation = selector.select_matrix(
            "pull_request",
            "main",
            (
                "README.md",
                "CONTRIBUTING.md",
                "PROJECT.md",
                "docs/user/html-repository-explorer.md",
                "docs/presentations/release/deck.pptx",
            ),
        )["include"]
        self.assertEqual([row["task"] for row in documentation], ["documentation"])

        policy_and_documentation = selector.select_matrix(
            "pull_request",
            "main",
            (
                "README.md",
                ".github/workflows/ci.yml",
                "scripts/select_ci_profile.py",
                "tests/critical/test_ci_action_pins.py",
            ),
        )["include"]
        self.assertEqual(
            [row["task"] for row in policy_and_documentation],
            ["documentation", "ci-policy"],
        )

        elixir = selector.select_matrix(
            "pull_request",
            "main",
            (
                "flavors/build-mix/flavor.md",
                "tests/conformance/test_elixir_mix_qualification.py",
            ),
        )["include"]
        self.assertEqual(
            len([row for row in elixir if row["task"] == "elixir-native"]), 6
        )
        self.assertIn("conformance", {row["task"] for row in elixir})

        for changed_paths in ((), ("src/literate_ai/cli/render.py",)):
            with self.subTest(changed_paths=changed_paths):
                selected = selector.select_matrix(
                    "pull_request", "main", changed_paths
                )["include"]
                self.assertTrue(selected)
                self.assertNotIn("documentation", {row["task"] for row in selected})
                self.assertNotIn("ci-policy", {row["task"] for row in selected})
                self.assertIn("conformance", {row["task"] for row in selected})

        full = selector.select_matrix("push", "", ("README.md",))["include"]
        self.assertTrue(full)
        self.assertNotIn("scoped", {key for row in full for key in row})

        self.assertFalse(
            selector.select_freebsd_acl(
                "pull_request", "main", ("README.md", "docs/user/guide.md")
            )
        )
        self.assertFalse(
            selector.select_freebsd_acl(
                "pull_request", "main", ("src/literate_ai/cli/render.py",)
            )
        )
        self.assertTrue(
            selector.select_freebsd_acl(
                "pull_request",
                "main",
                ("src/literate_ai/adapters/retained_posix_acl.py",),
            )
        )
        self.assertTrue(selector.select_freebsd_acl("pull_request", "main", None))
        self.assertTrue(
            selector.select_freebsd_acl("pull_request", "release/1.3.x", ("README.md",))
        )
        self.assertTrue(selector.select_freebsd_acl("push", "", ("README.md",)))

    def test_default_branch_pushes_scope_docs_but_preserve_qualification(self) -> None:
        selector = self.selector()
        for paths, tasks in (
            (("README.md", "docs/conference/README.md"), ["documentation"]),
            (
                ("docs/README.md", "scripts/select_ci_profile.py"),
                ["documentation", "ci-policy"],
            ),
        ):
            with self.subTest(paths=paths):
                selected = selector.select_matrix("push", "refs/heads/main", paths)
                self.assertEqual([row["task"] for row in selected["include"]], tasks)
                self.assertFalse(
                    selector.select_freebsd_acl("push", "refs/heads/main", paths)
                )
                self.assertFalse(
                    selector.select_full_qualification("push", "refs/heads/main", paths)
                )

        for event, ref, paths in (
            ("push", "refs/heads/main", None),
            ("push", "refs/heads/main", ()),
            ("push", "refs/heads/main", ("README.md", "src/literate_ai/cli.py")),
            ("push", "refs/heads/main", ("unclassified-input",)),
            ("push", "refs/heads/release/1.3.x", ("README.md",)),
            ("push", "refs/tags/v1.3.1", ("README.md",)),
            ("workflow_dispatch", "refs/heads/main", ("README.md",)),
        ):
            with self.subTest(event=event, ref=ref, paths=paths):
                rows = selector.select_matrix(event, ref, paths)["include"]
                self.assertTrue(rows)
                self.assertTrue(all("scoped" not in row for row in rows))
                self.assertEqual(
                    len([row for row in rows if row["task"] == "elixir-native"]), 6
                )
                self.assertTrue(selector.select_freebsd_acl(event, ref, paths))
                self.assertTrue(selector.select_full_qualification(event, ref, paths))

        self.assertTrue(
            selector.select_freebsd_acl(
                "push", "refs/heads/main", (".github/workflows/ci.yml",)
            )
        )

    def test_fixture_only_changes_receive_three_native_scoped_cells(self):
        selector = self.selector()
        paths = (
            "tests/critical/test_roadmap_dependencies.py",
            "tests/critical/test_retained_posix_acl.py",
            "docs/roadmap/active-work.md",
        )
        for event, ref in (("pull_request", "main"), ("push", "refs/heads/main")):
            with self.subTest(event=event):
                rows = selector.select_matrix(event, ref, paths)["include"]
                fixtures = [r for r in rows if r["task"] == "qualification-fixtures"]
                self.assertEqual(len(fixtures), 3)
                self.assertEqual(
                    {r["os"] for r in fixtures},
                    {
                        "windows-latest",
                        "macos-latest",
                        "ubuntu-latest",
                    },
                )
                self.assertTrue(all(r.get("scoped") for r in rows))
                self.assertFalse(selector.select_full_qualification(event, ref, paths))
                self.assertTrue(selector.select_freebsd_acl(event, ref, paths))
        for event, ref in (
            ("push", "refs/heads/release/1.3.x"),
            ("pull_request", "release/1.3.x"),
            ("workflow_dispatch", "refs/heads/main"),
        ):
            with self.subTest(event=event, ref=ref):
                rows = selector.select_matrix(event, ref, paths)["include"]
                self.assertTrue(all("scoped" not in r for r in rows))
                self.assertTrue(selector.select_full_qualification(event, ref, paths))

    def test_product_prs_retain_normal_cells_and_changed_fixture_cells(self):
        selector = self.selector()
        rows = selector.select_matrix(
            "pull_request",
            "main",
            (
                "src/literate_ai/application/roadmap_dependencies.py",
                "tests/critical/test_roadmap_dependencies.py",
            ),
        )["include"]
        self.assertIn("conformance", {r["task"] for r in rows})
        self.assertEqual(sum(r["task"] == "qualification-fixtures" for r in rows), 3)

    def test_push_diff_uses_both_endpoints_and_unknown_diff_fails_closed(self) -> None:
        selector = self.selector()
        with mock.patch.object(selector.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=b"README.md\0")
            self.assertEqual(
                selector._changed_paths("before", "after", push=True), ("README.md",)
            )
            self.assertEqual(run.call_args.args[0][-2:], ("before", "after"))
            selector._changed_paths("before", "after")
            self.assertEqual(run.call_args.args[0][-1], "before...after")
            run.return_value = mock.Mock(returncode=128, stdout=b"")
            self.assertIsNone(selector._changed_paths("before", "after", push=True))
            run.side_effect = selector.subprocess.TimeoutExpired("git", 30)
            self.assertIsNone(selector._changed_paths("before", "after", push=True))
            run.side_effect = OSError("git unavailable")
            self.assertIsNone(selector._changed_paths("before", "after", push=True))
            run.side_effect = None
            run.return_value = mock.Mock(returncode=0, stdout=b"\xff\0")
            self.assertIsNone(selector._changed_paths("before", "after", push=True))
            run.reset_mock()
            self.assertIsNone(selector._changed_paths("", "after", push=True))
            run.assert_not_called()

    def test_workflow_runs_only_the_checks_owned_by_each_scoped_profile(self) -> None:
        workflow = load_yaml_subset((ROOT / ".github/workflows/ci.yml").read_text())
        profile = workflow["jobs"]["profile"]["steps"][-1]
        self.assertEqual(
            profile["env"]["CI_BASE_REF"], "${{ github.base_ref || github.ref }}"
        )
        self.assertEqual(
            profile["env"]["CI_BASE_SHA"],
            "${{ github.event.pull_request.base.sha || github.event.before }}",
        )
        self.assertEqual(
            profile["env"]["CI_HEAD_SHA"],
            "${{ github.event.pull_request.head.sha || github.sha }}",
        )
        freebsd = workflow["jobs"]["freebsd-posix-acl"]
        self.assertEqual(freebsd["needs"], "profile")
        self.assertEqual(freebsd["if"], "needs.profile.outputs.freebsd_acl == 'true'")
        macos = workflow["jobs"]["macos-qualification"]
        self.assertEqual(macos["needs"], "profile")
        self.assertEqual(
            macos["if"], "needs.profile.outputs.full_qualification == 'true'"
        )
        steps = workflow["jobs"]["checks"]["steps"]
        documentation_steps = [
            step for step in steps if step.get("if") == "matrix.task == 'documentation'"
        ]
        policy_steps = [
            step for step in steps if step.get("if") == "matrix.task == 'ci-policy'"
        ]
        self.assertTrue(documentation_steps)
        self.assertTrue(policy_steps)
        documentation_text = json.dumps(documentation_steps)
        self.assertIn("documentation-check", documentation_text)
        self.assertIn("public-export-check", documentation_text)
        self.assertIn("test_roadmap_goal_gate", documentation_text)
        policy_text = json.dumps(policy_steps)
        self.assertIn("tests.critical.test_ci_action_pins", policy_text)
        self.assertNotIn("make validate", documentation_text + policy_text)
        self.assertNotIn("wheel-check", documentation_text + policy_text)

    def test_windows_native_receiver_runs_once_without_xdist_contention(self) -> None:
        workflow = load_yaml_subset((ROOT / ".github/workflows/ci.yml").read_text())
        steps = workflow["jobs"]["checks"]["steps"]
        shard = next(
            step for step in steps if step.get("name") == "Run this shard's tests"
        )
        receiver = next(
            step
            for step in steps
            if step.get("name")
            == "Qualify the native receiver without xdist contention"
        )
        upload = next(
            step
            for step in steps
            if step.get("name") == "Upload the profiling trace and hotspot report"
        )
        test_id = (
            "tests/smoke/test_remote_standard_toolchains.py::"
            "RemoteStandardToolchainTests::"
            "test_actual_admitted_receiver_supplies_discovery_and_live_profile_guard"
        )
        self.assertIn(f"--deselect={test_id}", shard["run"])
        self.assertEqual(
            receiver["if"], "matrix.task == 'windows-tests' && matrix.group == 3"
        )
        self.assertEqual(receiver["timeout-minutes"], 45)
        self.assertIn("-o addopts=''", receiver["run"])
        self.assertIn(test_id, receiver["run"])
        self.assertIn(
            "profile-${{ matrix.group }}-receiver.ndjson", upload["with"]["path"]
        )
        self.assertLess(steps.index(shard), steps.index(receiver))
        self.assertLess(steps.index(receiver), steps.index(upload))

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
        selector = self.selector()
        selected = selector.select_matrix("pull_request", "main")["include"]
        selected_elixir = [row for row in selected if row["task"] == "elixir-native"]
        self.assertEqual(len(selected_elixir), 1)
        self.assertEqual(selected_elixir[0]["os"], "ubuntu-24.04")
        self.assertEqual(selected_elixir[0]["elixir"], "1.18.0")

        for event, base in (("pull_request", "release/1.2"), ("push", "")):
            with self.subTest(event=event, base=base):
                selected = selector.select_matrix(event, base)["include"]
                self.assertEqual(
                    [row for row in selected if row["task"] == "elixir-native"],
                    elixir_rows,
                    "release qualification runs every native Elixir cell",
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
