from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.lifecycle.standard_local import (
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.project_lifecycle_driver import _retain_driver_diagnostic
from literate_ai.evidence_ledger import (
    EvidenceNode,
    EvidenceRun,
    load_run,
    open_run,
    record_retained_output,
    retained_directory,
)
from scripts.fanout_samples import _record_fanout_unavailable
from tests.conformance.support.sample_runner import (
    _ExecutionVariant,
    _sample_evidence_phase,
    main,
    run_all,
)


class Stage3EvidenceRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence_environment = patch.dict(
            os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
                # These tests fully mock generation (patch run_all) to exercise
                # evidence retention; the live-model preflight is out of scope and
                # would otherwise intercept the injected failure.
                "LITAI_SKIP_MODEL_PREFLIGHT": "1",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

    def test_owned_directory_pointer_is_registered_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            root = project / "runtime"
            root.mkdir()
            run = open_run(project, operation="runtime")
            assert run is not None
            with patch.dict(
                os.environ,
                {"LITAI_EVIDENCE_RUN": str(run.root)},
                clear=False,
            ):
                with self.assertRaises(RuntimeError):
                    with retained_directory(
                        root,
                        node_path="runtime/owned",
                        operation="runtime.owned",
                        role="runtime-root",
                    ):
                        raise RuntimeError("before execution")
            report = load_run(project, run.run_id)
            assert report is not None
            node = next(
                item
                for item in report.reduced()["nodes"]
                if item["path"] == "runtime/owned"
            )
            self.assertEqual(node["state"], "failed")
            self.assertEqual(node["outputs"][0]["role"], "runtime-root")
            self.assertEqual(
                node["outputs"][0]["path"],
                str(root.resolve()),
            )
            self.assertEqual(node["outputs"][0]["retention"], "retained")
            self.assertTrue(root.is_dir())

    def test_owned_directory_pointer_records_successful_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            root = project / "runtime"
            root.mkdir()
            run = open_run(project, operation="runtime")
            assert run is not None
            with patch.dict(
                os.environ,
                {"LITAI_EVIDENCE_RUN": str(run.root)},
                clear=False,
            ):
                with retained_directory(
                    root,
                    node_path="runtime/owned",
                    operation="runtime.owned",
                    role="runtime-root",
                ):
                    pass
            report = load_run(project, run.run_id)
            assert report is not None
            node = next(
                item
                for item in report.reduced()["nodes"]
                if item["path"] == "runtime/owned"
            )
            self.assertEqual(node["state"], "passed")
            self.assertEqual(node["outputs"][0]["retention"], "pruned")
            self.assertFalse(root.exists())

    def test_failure_handler_preserves_original_error_when_evidence_root_is_unusable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            unusable_root = project / "evidence-file"
            unusable_root.write_text("not a directory", encoding="utf-8")
            run = EvidenceRun("broken", unusable_root, project_root=project)
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=project / "objects",
                contracts=(),
                tool_bindings=(),
            )
            original = RuntimeError("original lifecycle failure")
            with patch(
                "literate_ai.adapters.lifecycle.standard_local.attach_run",
                return_value=run,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    try:
                        raise original
                    except RuntimeError:
                        ports._record_failure_diagnostic("component@1", "diagnostic")
                        raise
            self.assertIs(raised.exception, original)

    def test_explicit_sample_scratch_root_is_retained_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            scratch = project / "operator-disk"
            run = open_run(project, operation="samples")
            self.assertIsNotNone(run)
            assert run is not None
            failure = RuntimeError("sample failed")

            def fail_with_scratch(*args: object, **kwargs: object) -> None:
                del kwargs
                sample_scratch = Path(args[1])
                (sample_scratch / "diagnostic.txt").write_text(
                    "failure", encoding="utf-8"
                )
                raise failure

            with (
                patch.dict(
                    os.environ, {"LITAI_EVIDENCE_RUN": str(run.root)}, clear=False
                ),
                patch(
                    "tests.conformance.support.sample_runner.discover",
                    return_value=(Path("sample"),),
                ),
                patch(
                    "tests.conformance.support.sample_runner.discover_cpp_toolchain",
                    return_value=object(),
                ),
                patch(
                    "tests.conformance.support.sample_runner.run_sample",
                    side_effect=fail_with_scratch,
                ),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    run_all(
                        project / "samples",
                        scratch,
                        source_generator=object(),
                        allow_host_execution=True,
                        jobs=1,
                    )
            self.assertIs(raised.exception, failure)
            retained = scratch / "sample"
            self.assertTrue((retained / "diagnostic.txt").is_file())
            report = load_run(project, run.run_id)
            self.assertIsNotNone(report)
            assert report is not None
            pointer = report.reduced()["nodes"][0]["outputs"][0]
            self.assertEqual(pointer["path"], str(retained.resolve()))

    def test_default_main_runtime_root_is_retained_only_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            runtime = project / "runtime"
            runtime.mkdir()
            failure = RuntimeError("sample failed")
            with (
                patch(
                    "tests.conformance.support.sample_runner.tempfile.mkdtemp",
                    return_value=str(runtime),
                ),
                patch(
                    "tests.conformance.support.sample_runner.run_all",
                    side_effect=failure,
                ),
                patch("literate_ai.evidence_ledger.report_progress") as progress,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    main(["--allow-host-execution"])
            self.assertIs(raised.exception, failure)
            self.assertTrue(runtime.is_dir())
            progress.assert_called_once_with(
                f"Retained sample-runtime-root: {runtime.resolve()}"
            )

            with (
                patch(
                    "tests.conformance.support.sample_runner.tempfile.mkdtemp",
                    return_value=str(runtime),
                ),
                patch(
                    "tests.conformance.support.sample_runner.run_all",
                    return_value={"passed": True},
                ),
            ):
                self.assertEqual(main(["--allow-host-execution"]), 0)
            self.assertFalse(runtime.exists())

    def test_driver_diagnostic_is_bound_to_a_failed_evidence_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="lifecycle")
            self.assertIsNotNone(run)
            assert run is not None
            with patch.dict(
                os.environ,
                {
                    "LITAI_EVIDENCE_RUN": str(run.root),
                    "OPENAI_API_KEY": "supersecret",
                },
                clear=False,
            ):
                path = _retain_driver_diagnostic(
                    "driver failed while contacting https://user:supersecret@example.test"
                )
            self.assertIsNotNone(path)
            assert path is not None
            diagnostic = Path(path)
            self.assertTrue(diagnostic.is_file())
            self.assertNotIn("secret", diagnostic.read_text(encoding="utf-8"))
            report = load_run(project, run.run_id)
            self.assertIsNotNone(report)
            assert report is not None
            nodes = report.reduced()["nodes"]
            self.assertEqual(nodes[0]["state"], "failed")
            self.assertEqual(nodes[0]["outputs"][0]["role"], "driver-diagnostic")

    def test_lifecycle_failure_diagnostic_is_flushed_to_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="lifecycle")
            self.assertIsNotNone(run)
            assert run is not None
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=project / "objects",
                contracts=(),
                tool_bindings=(),
            )
            with patch.dict(
                os.environ, {"LITAI_EVIDENCE_RUN": str(run.root)}, clear=False
            ):
                ports._record_failure_diagnostic("component@1", "failed: private-value")
            report = load_run(project, run.run_id)
            self.assertIsNotNone(report)
            assert report is not None
            nodes = report.reduced()["nodes"]
            self.assertEqual(nodes[0]["state"], "failed")
            diagnostic = run.root / nodes[0]["outputs"][0]["path"]
            self.assertEqual(
                json.loads(diagnostic.read_text(encoding="utf-8"))["diagnostic"],
                "failed: private-value",
            )

    def test_retained_directory_is_persisted_on_a_failed_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            with patch.dict(os.environ, {"OBJ_DIR": "_objects"}, clear=False):
                run = open_run(project, operation="sample")
                self.assertIsNotNone(run)
                assert run is not None
                retained = project / "failed-sample"
                retained.mkdir()
                with patch.dict(
                    os.environ, {"LITAI_EVIDENCE_RUN": str(run.root)}, clear=False
                ):
                    result = record_retained_output(
                        retained,
                        node_path="samples/hello/host/run",
                        operation="sample.conformance",
                        role="scratch",
                        failed=True,
                    )
                self.assertEqual(result, str(retained.resolve()))
                report = load_run(project, run.run_id)
                self.assertIsNotNone(report)
                assert report is not None
                nodes = report.reduced()["nodes"]
                self.assertEqual(len(nodes), 1)
                node = nodes[0]
                self.assertEqual(node["state"], "failed")
                pointer = node["outputs"][0]
                self.assertEqual(pointer["media_type"], "inode/directory")
                self.assertEqual(pointer["retention"], "retained")
                self.assertEqual(pointer["path"], str(retained.resolve()))

    def test_failed_output_pointer_does_not_require_a_local_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="fanout")
            self.assertIsNotNone(run)
            assert run is not None
            with run.node("fanout/workers/linux", operation="fanout.worker") as node:
                assert isinstance(node, EvidenceNode)
                node.add_output(
                    role="worker-workspace",
                    path="/remote/build/litai-runs/example",
                    media_type="inode/directory",
                    retention="host-only",
                )
            report = json.loads((run.root / "index.json").read_text())
            pointer = report["nodes"][0]["outputs"][0]
            self.assertEqual(pointer["retention"], "host-only")
            self.assertEqual(pointer["path"], "/remote/build/litai-runs/example")

    def test_missing_worker_fleet_is_marked_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="fanout")
            self.assertIsNotNone(run)
            assert run is not None
            with patch.dict(
                os.environ, {"LITAI_EVIDENCE_RUN": str(run.root)}, clear=False
            ):
                _record_fanout_unavailable("no worker fleet is configured")
            report = load_run(project, run.run_id)
            self.assertIsNotNone(report)
            assert report is not None
            node = report.reduced()["nodes"][0]
            self.assertEqual(node["state"], "unavailable")
            self.assertEqual(
                node["pins"]["unavailable_reason"],
                "no worker fleet is configured",
            )

    def test_sample_phase_nodes_identify_variant_language_and_host_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            run = open_run(project, operation="samples")
            self.assertIsNotNone(run)
            assert run is not None
            variant = _ExecutionVariant("python", (), ("python",))
            for phase in ("standard", "host-e2e"):
                with _sample_evidence_phase(
                    evidence_run=run,
                    sample_id="hello-component",
                    variant=variant,
                    phase=phase,
                    scratch=project / "scratch",
                    flavor_selectors=("os.linux",),
                    pipeline_model="model@1",
                    source_generator=object(),
                ):
                    pass
            nodes = run.reduced()["nodes"]
            self.assertEqual(
                [node["path"] for node in nodes],
                [
                    "samples/hello-component/python/standard",
                    "samples/hello-component/python/host-e2e",
                ],
            )
            self.assertEqual(nodes[0]["pins"]["variant"], "python")
            self.assertEqual(nodes[0]["pins"]["languages"], ["python"])
            self.assertEqual(nodes[1]["pins"]["phase"], "host-e2e")
