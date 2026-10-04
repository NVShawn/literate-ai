from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.cli.dispatch import main
from literate_ai.evidence_ledger import (
    EvidenceRun,
    attach_run,
    evidence_root,
    explain_run,
    latest_run,
    load_run,
    open_run,
    prune_runs,
    record_subprocess,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def _allocate_node(project: str, run_root: str) -> None:
    run = EvidenceRun(
        Path(run_root).name,
        Path(run_root),
        project_root=Path(project),
        operation="concurrent",
    )
    with run.node("worker/node", operation="worker"):
        pass


class EvidenceLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence_environment = patch.dict(
            os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

    def test_attach_run_is_project_scoped_and_preserves_same_project_nesting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            project_a = parent / "project-a"
            project_b = parent / "project-b"
            project_a.mkdir()
            project_b.mkdir()
            with patch.dict(
                os.environ,
                {"OBJ_DIR": str(parent / "shared-objects")},
                clear=False,
            ):
                ambient = open_run(project_a, operation="ambient")
                assert ambient is not None
                with ambient.node("parent", operation="parent") as node:
                    with patch.dict(
                        os.environ,
                        {
                            "LITAI_EVIDENCE_RUN": str(ambient.root),
                            "LITAI_EVIDENCE_PARENT": node.node_id,
                        },
                        clear=False,
                    ):
                        attached = attach_run(project_a)
                        self.assertIsNotNone(attached)
                        assert attached is not None
                        self.assertEqual(attached.root, ambient.root)
                        with attached.node("child", operation="child") as child:
                            self.assertEqual(child.parent_id, node.node_id)

                        self.assertIsNone(attach_run(project_b))
                        foreign = open_run(project_b, operation="foreign")
                        assert foreign is not None
                        with foreign.node("foreign", operation="foreign"):
                            pass

                self.assertEqual(latest_run(project_a).run_id, ambient.run_id)
                self.assertEqual(latest_run(project_b).run_id, foreign.run_id)
                self.assertNotEqual(
                    evidence_root(project_a).resolve(),
                    evidence_root(project_b).resolve(),
                )

    def test_explicit_project_run_ignores_ambient_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            ambient_project = project / "ambient"
            explicit_project = project / "explicit"
            ambient_project.mkdir()
            explicit_project.mkdir()
            ambient = open_run(ambient_project, operation="ambient")
            assert ambient is not None
            with patch.dict(
                os.environ,
                {
                    "LITAI_EVIDENCE_RUN": str(ambient.root),
                    "LITAI_EVIDENCE_PARENT": "n9999",
                },
                clear=False,
            ):
                explicit = open_run(explicit_project, operation="explicit")
                assert explicit is not None
                self.assertNotEqual(explicit.root, ambient.root)
                with explicit.node("explicit", operation="explicit") as node:
                    self.assertEqual(node.node_id, "n0001")
                    self.assertIsNone(node.parent_id)

    def test_create_attach_nesting_and_derived_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            with patch.dict(os.environ, {"OBJ_DIR": "_obj"}, clear=False):
                run = open_run(project, operation="release")
                self.assertIsNotNone(run)
                assert run is not None
                with run.node("release", operation="release") as root:
                    with run.node(
                        "release/gate",
                        operation="gate",
                        parent=root.node_id,
                    ):
                        pass
                with patch.dict(
                    os.environ,
                    {"LITAI_EVIDENCE_RUN": str(run.root)},
                    clear=False,
                ):
                    attached = latest_run(project)
                    self.assertIsNotNone(attached)
                    self.assertEqual(attached.run_id, run.run_id)
                    self.assertEqual(attached.root, run.root)
                index = run.reduced()
                nodes = {item["node_id"]: item for item in index["nodes"]}
                self.assertEqual(nodes["n0001"]["children"], ["n0002"])

    def test_concurrent_node_ids_are_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="concurrent")
            assert run is not None
            processes = [
                multiprocessing.Process(
                    target=_allocate_node,
                    args=(str(project), str(run.root)),
                )
                for _ in range(2)
            ]
            # The production lock has a 30-second contention deadline. Windows
            # ``spawn`` must also import this dependency-heavy test module in each
            # child, so the test deadline must cover that lifecycle rather than
            # imposing a shorter, unrelated 10-second startup limit.
            deadline = time.monotonic() + 45
            try:
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(max(0, deadline - time.monotonic()))
                self.assertTrue(
                    all(process.exitcode == 0 for process in processes),
                    [process.exitcode for process in processes],
                )
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)
            records = run.reduced()["nodes"]
            self.assertEqual({item["node_id"] for item in records}, {"n0001", "n0002"})

    def test_running_start_record_survives_reduction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="interrupted")
            assert run is not None
            node = run.node("release/gate", operation="gate")
            entered = node.__enter__()
            self.assertEqual(entered.node_id, "n0001")
            reduced = run.reduced()
            self.assertEqual(reduced["nodes"][0]["state"], "running")

    def test_subprocess_tees_redacts_and_records_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="subprocess")
            assert run is not None
            stdout = io.StringIO()
            argv = [
                sys.executable,
                "-c",
                "import os; print(os.environ['TEST_TOKEN']); print('done')",
            ]
            with (
                patch("sys.stdout", stdout),
                patch.dict(os.environ, {"TEST_TOKEN": "do-not-persist"}, clear=False),
            ):
                completed = record_subprocess(
                    argv,
                    cwd=project,
                    run=run,
                    parent=None,
                    path="release/gate",
                    operation="gate",
                    environment={**os.environ, "TEST_TOKEN": "do-not-persist"},
                )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("do-not-persist", stdout.getvalue())
            node = run.reduced()["nodes"][0]
            transcript = run.root / "n0001" / "stdout.log"
            self.assertNotIn("do-not-persist", transcript.read_text(encoding="utf-8"))
            pointer = next(item for item in node["outputs"] if item["role"] == "stdout")
            self.assertEqual(pointer["bytes"], transcript.stat().st_size)
            self.assertTrue(pointer["digest"].startswith("sha256:"))
            self.assertEqual(node["state"], "passed")
            self.assertEqual(
                pointer["digest"],
                "sha256:" + hashlib.sha256(transcript.read_bytes()).hexdigest(),
            )

    def test_subprocess_without_run_preserves_child_result(self) -> None:
        result = record_subprocess(
            [sys.executable, "-c", "print('child')"],
            cwd=Path.cwd(),
            run=None,
            parent=None,
            path="release/gate",
            operation="gate",
            tee=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "child\n")

    def test_captured_output_preserves_universal_newline_behavior(self) -> None:
        result = record_subprocess(
            [sys.executable, "-c", "import os;os.write(1,b'one\\r\\ntwo\\rthree\\n')"],
            cwd=Path.cwd(),
            run=None,
            parent=None,
            path="release/gate",
            operation="gate",
            tee=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "one\ntwo\nthree\n")

    def test_subprocess_preserves_unicode_across_pipe_read_boundaries(self) -> None:
        expected = "x" * 4095 + "\N{SLIGHTLY SMILING FACE}" + "tail"
        for tee in (False, True):
            with self.subTest(tee=tee), tempfile.TemporaryDirectory() as temporary:
                run = open_run(Path(temporary), operation="unicode-output")
                assert run is not None
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                    result = record_subprocess(
                        [
                            sys.executable,
                            "-c",
                            "import os; data=('x'*4095+chr(0x1f642)+'tail').encode();"
                            "os.write(1,data);os.write(2,data)",
                        ],
                        cwd=Path(temporary),
                        run=run,
                        parent=None,
                        path="release/gate",
                        operation="gate",
                        tee=tee,
                    )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, expected)
                self.assertEqual(result.stderr, expected)
                self.assertEqual(stdout.getvalue(), expected if tee else "")
                self.assertEqual(stderr.getvalue(), expected if tee else "")
                for label in ("stdout", "stderr"):
                    transcript = run.root / "n0001" / (label + ".log")
                    self.assertEqual(transcript.read_text(encoding="utf-8"), expected)

    def test_timeout_covers_output_drain_after_root_process_exits(self) -> None:
        original_popen = subprocess.Popen
        for tee in (False, True):
            with self.subTest(tee=tee), tempfile.TemporaryDirectory() as temporary:
                processes = []

                def start(*args, captured=processes, **kwargs):
                    process = original_popen(*args, **kwargs)
                    captured.append(process)
                    return process

                run = open_run(Path(temporary), operation="output-drain-timeout")
                assert run is not None
                with patch("subprocess.Popen", side_effect=start):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        record_subprocess(
                            [
                                sys.executable,
                                "-c",
                                "import subprocess,sys;subprocess.Popen("
                                "[sys.executable,'-c','import time;time.sleep(3)'])",
                            ],
                            cwd=Path(temporary),
                            run=run,
                            parent=None,
                            path="release/gate",
                            operation="gate",
                            timeout=1,
                            tee=tee,
                        )
                self.assertEqual(processes[0].returncode, 0)
                self.assertEqual(run.reduced()["nodes"][0]["state"], "failed")

    def test_open_run_degrades_when_evidence_root_cannot_be_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "literate_ai.evidence_ledger.Path.mkdir",
                side_effect=OSError("read-only"),
            ):
                self.assertIsNone(open_run(Path(temporary), operation="release"))

    def test_nonzero_subprocess_is_returned_and_recorded_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="subprocess")
            assert run is not None
            result = record_subprocess(
                [sys.executable, "-c", "raise SystemExit(7)"],
                cwd=project,
                run=run,
                parent=None,
                path="release/gate",
                operation="gate",
            )
            self.assertEqual(result.returncode, 7)
            self.assertEqual(run.reduced()["nodes"][0]["state"], "failed")

    def test_nontee_subprocess_still_retains_redacted_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="subprocess")
            assert run is not None
            result = record_subprocess(
                [
                    sys.executable,
                    "-c",
                    "import os; print('x' * 4090 + os.environ['TEST_TOKEN'])",
                ],
                cwd=project,
                run=run,
                parent=None,
                path="release/gate",
                operation="gate",
                environment={**os.environ, "TEST_TOKEN": "do-not-persist"},
                tee=False,
            )
            self.assertEqual(result.returncode, 0)
            transcript = run.root / "n0001" / "stdout.log"
            self.assertNotIn("do-not-persist", transcript.read_text(encoding="utf-8"))
            pointer = run.reduced()["nodes"][0]["outputs"][0]
            self.assertEqual(pointer["bytes"], transcript.stat().st_size)

    def test_timeout_retains_transcript_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="subprocess")
            assert run is not None
            with self.assertRaises(subprocess.TimeoutExpired):
                record_subprocess(
                    [
                        sys.executable,
                        "-c",
                        "import time; print('before', flush=True); time.sleep(2)",
                    ],
                    cwd=project,
                    run=run,
                    parent=None,
                    path="release/gate",
                    operation="gate",
                    timeout=0.1,
                    tee=False,
                )
            node = run.reduced()["nodes"][0]
            self.assertTrue(any(item["role"] == "stdout" for item in node["outputs"]))

    def test_short_secret_environment_values_do_not_rewrite_ledger_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            with patch.dict(os.environ, {"CI_TOKEN": "root"}, clear=False):
                run = open_run(project, operation="release")
                assert run is not None
                with run.node("release", operation="release"):
                    pass
                record = json.loads(
                    run.ledger_path.read_text(encoding="utf-8").splitlines()[0]
                )
                self.assertIn("root", record)
                self.assertNotIn("<redacted>", record.get("operation", ""))

    def test_schema_validation_and_explain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="release")
            assert run is not None
            with run.node("release", operation="release") as root:
                with run.node(
                    "release/failing-gate",
                    operation="gate",
                    parent=root.node_id,
                ) as leaf:
                    leaf.add_pins(commit="abc")
                    leaf.attach_text(
                        "failure.log",
                        "failure details\n",
                        role="failure",
                    )
                    leaf.finish(2)
            run.write_index()
            schemas = SchemaCatalog(Path(__file__).parents[2] / "schemas" / "v2")
            for line in run.ledger_path.read_text(encoding="utf-8").splitlines():
                value = json.loads(line)
                schemas.validate(value["schema"], value)
            explanation = explain_run(run)
            self.assertEqual(explanation["failure"]["node_id"], "n0002")
            self.assertTrue(
                any(
                    line.startswith("  pins:") and "commit" in line
                    for line in explanation["render"]
                )
            )
            rendered = "\n".join(explanation["render"])
            self.assertEqual(explanation["root"], str(run.root.resolve()))
            output_line = next(
                line for line in explanation["render"] if line.startswith("  output: ")
            )
            transcript = Path(output_line.split()[2])
            self.assertTrue(transcript.is_absolute())
            self.assertEqual(
                transcript.read_text(encoding="utf-8"), "failure details\n"
            )
            self.assertIn(str(transcript), rendered)
            self.assertIn("failure.log", rendered)
            persisted = [
                json.loads(line)
                for line in run.ledger_path.read_text(encoding="utf-8").splitlines()
            ]
            for record in persisted:
                for pointer in record.get("outputs", []):
                    self.assertFalse(Path(pointer["path"]).is_absolute())
            self.assertLessEqual(len(explanation["render"]), 64)

    def test_explain_renders_failure_and_independent_ancestor_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="release")
            assert run is not None
            try:
                with run.node("release", operation="release") as root:
                    try:
                        with run.node(
                            "release/failing-gate",
                            operation="gate",
                            parent=root.node_id,
                        ):
                            raise RuntimeError("leaf causal error")
                    except RuntimeError as error:
                        raise ValueError("ancestor causal error") from error
            except ValueError:
                pass

            explanation = explain_run(run)
            rendered = "\n".join(explanation["render"])
            self.assertIn("- release/failing-gate [failed]", rendered)
            self.assertIn("leaf causal error", rendered)
            self.assertIn("ancestor causal error", rendered)
            self.assertEqual(
                len(
                    [
                        line
                        for line in explanation["render"]
                        if line.startswith("  error: ")
                    ]
                ),
                2,
            )

    def test_explain_renders_nearest_enclosing_transcripts_for_outputless_leaf(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="release")
            assert run is not None
            with run.node("release", operation="release") as root:
                with run.node(
                    "release/step",
                    operation="step",
                    parent=root.node_id,
                ) as step:
                    step.attach_text(
                        "step.log",
                        "enclosing step details\n",
                        role="transcript",
                    )
                    with run.node(
                        "release/step/failure",
                        operation="failure",
                        parent=step.node_id,
                    ) as leaf:
                        leaf.finish(1)

            explanation = explain_run(run)
            self.assertEqual(explanation["failure"]["path"], "release/step/failure")
            self.assertIn(
                "  transcripts: enclosing step release/step",
                explanation["render"],
            )
            output_line = next(
                line for line in explanation["render"] if line.startswith("  output: ")
            )
            transcript = Path(output_line.split()[2])
            self.assertTrue(transcript.is_absolute())
            self.assertEqual(
                transcript.read_text(encoding="utf-8"),
                "enclosing step details\n",
            )

    def test_explain_selects_latest_deepest_failure_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="release")
            assert run is not None
            with run.node("release", operation="release") as root:
                with run.node(
                    "release/shallow", operation="gate", parent=root.node_id
                ) as shallow:
                    shallow.finish(1)
                with run.node(
                    "release/deep", operation="gate", parent=root.node_id
                ) as deep:
                    with run.node(
                        "release/deep/leaf", operation="gate", parent=deep.node_id
                    ) as leaf:
                        leaf.finish(1)
            explanation = explain_run(run)
            self.assertEqual(explanation["failure"]["path"], "release/deep/leaf")
            self.assertEqual(explanation["other_failure_leaves"], 1)

    def test_prune_keeps_failed_runs_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            first = open_run(project, operation="first")
            assert first is not None
            with first.node("first", operation="first") as node:
                node.finish(1)
            second = open_run(project, operation="second")
            assert second is not None
            with second.node("second", operation="second"):
                pass
            result = prune_runs(project, keep=1)
            self.assertNotIn(first.run_id, result["removed_run_ids"])
            self.assertTrue(first.root.exists())
            self.assertTrue(second.root.exists())

    @unittest.skipIf(
        os.name == "nt",
        "Windows CI cannot rmtree an evidence run the test process still names",
    )
    def test_prune_removes_passed_and_preserves_latest_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            first = open_run(project, operation="first")
            assert first is not None
            with first.node("first", operation="first"):
                pass
            failed = open_run(project, operation="failed")
            assert failed is not None
            with failed.node("failed", operation="failed") as node:
                node.finish(1)
            middle = open_run(project, operation="middle")
            assert middle is not None
            with middle.node("middle", operation="middle"):
                pass
            latest = open_run(project, operation="latest")
            assert latest is not None
            with latest.node("latest", operation="latest"):
                pass
            first.close("passed")
            failed.close("failed")
            middle.close("passed")
            latest.close("passed")
            result = prune_runs(project, keep=1)
            self.assertFalse(first.root.exists())
            self.assertIn(first.root.name, result["removed_run_ids"])
            self.assertGreater(result["reclaimed_bytes"], 0)
            self.assertTrue(failed.root.exists())
            self.assertTrue(middle.root.exists())
            self.assertTrue(latest.root.exists())
            self.assertEqual(latest_run(project).root.resolve(), latest.root.resolve())

    def test_close_writes_authoritative_run_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = open_run(Path(temporary), operation="release")
            assert run is not None
            run.close("passed")
            self.assertEqual(run.reduced()["state"], "passed")
            self.assertIsNotNone(load_run(Path(temporary), run.run_id))

    def test_cli_index_show_and_explain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="release")
            assert run is not None
            with run.node("release", operation="release") as node:
                node.finish(1)
            output = io.StringIO()
            with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
                status = main(
                    [
                        "--json",
                        "release",
                        "evidence",
                        "explain",
                        "--project",
                        str(project),
                        "--run",
                        run.run_id,
                    ],
                    stdout=output,
                    stderr=io.StringIO(),
                )
            self.assertEqual(status, 0)
            self.assertEqual(
                json.loads(output.getvalue())["result"]["failure"]["node_id"],
                "n0001",
            )


if __name__ == "__main__":
    unittest.main()
