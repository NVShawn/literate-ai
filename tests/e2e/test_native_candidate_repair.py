"""Real Rust builds and verifier rejection through the existing bounded loop.

Only the external model and editable-framework installation observation are
fixtures. Source, locks, native commands, oracle custody and admission are real.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.support.fixtures_rust_library_harness import prepare_native_verifier_class


@unittest.skipUnless(
    os.name == "posix" and shutil.which("make"),
    "the public Standard source journey needs a POSIX external coding CLI",
)
class NativeCandidateRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse genuine native tool/seed provisioning, not another test's results.
        prepare_native_verifier_class(cls)

    def _run(self, modes, *, mutation=None):
        from literate_ai.adapters import native_candidate_verification as verification
        from literate_ai.adapters import rust_library_harness_execution as execution
        from literate_ai.adapters.standard_lifecycle_binding import (
            InstalledFrameworkDistribution,
            InstalledFrameworkDistributionMember,
            resolve_standard_project_lifecycle_driver,
        )
        from literate_ai.cli import main
        from literate_ai.contracts import (
            ProjectTestReceiptPolicy,
            StandardProjectLifecycleDriver,
            load_current_standard_lifecycle_policy,
        )
        from tests.support.fixtures_public_rust_harness import author_public_project

        root = Path(tempfile.mkdtemp(prefix="native candidate repair ")).resolve()
        success = [False]
        self.addCleanup(
            lambda: (
                shutil.rmtree(root)
                if success[0]
                else print(f"Retained native repair fixture: {root}", file=sys.stderr)
            )
        )
        distribution = InstalledFrameworkDistribution(
            "literate-ai",
            "1.1.0",
            (
                InstalledFrameworkDistributionMember(
                    "literate_ai/__init__.py", 1, "sha256:" + "a" * 64
                ),
            ),
        )
        policy = load_current_standard_lifecycle_policy()
        driver = StandardProjectLifecycleDriver(distribution.identity, policy.identity)
        receipt = ProjectTestReceiptPolicy(
            policy.policy_id,
            policy.policy_version,
            driver.identity,
            policy.required_evidence_kinds,
            policy.minimum_test_count,
        )
        environment = {
            k: v
            for k, v in self.environment.items()
            if not k.startswith(("LITAI_", "GIT_"))
        }
        environment.update(
            CARGO=shlex.quote(self.cargo.command[0]),
            RUSTC=shlex.quote(self.rustc.command[0]),
            BUILD_DIR=str(root / "build"),
            OBJ_DIR=str(root / "obj"),
            CODING_CLI="claude",
        )
        calls = []
        mutations = []
        selected_export = []
        selected_native = []
        original_verify = verification._NativeCandidateVerifier.verify
        original_process = execution.run_bounded_process
        original_results = execution._require_results

        def mutate_export():
            library = selected_export[0] / "source/lib.rs"
            before = library.read_bytes()
            after = before.replace(b"42", b"41")
            self.assertNotEqual(before, after)
            library.write_bytes(after)
            mutations.append("export")

        def verify(owner, plan, output, evidence):
            record = owner.ports._planned_exports[owner.lock.root_revision.uri]
            selected_export[:] = [owner.ports.artifact_path(record)]
            if mutation == "export-before":
                mutate_export()
            return original_verify(owner, plan, output, evidence)

        def process(argv, **kwargs):
            result = original_process(argv, **kwargs)
            if Path(argv[0]).name == "litai-library-acceptance":
                selected_native[:] = [Path(argv[0])]
            if mutation == "export-during" and "metadata" in argv and not mutations:
                mutate_export()
            return result

        def results(raw, oracle):
            if mutation == "native-rejection" and not mutations:
                executable = selected_native[0]
                # Change the actual executable after its real execution and
                # before the genuine semantic validator rejects its result.
                with executable.open("ab") as stream:
                    stream.write(b"physical verifier custody mutation")
                mutations.append("native")
            return original_results(raw, oracle)

        def invoke(arguments):
            output, errors = io.StringIO(), io.StringIO()
            with (
                ExitStack() as stack,
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "literate_ai.cli.rebuild.resolve_standard_project_lifecycle_driver",
                    lambda value: resolve_standard_project_lifecycle_driver(
                        value, distribution_observer=lambda: distribution
                    ),
                ),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                if mutation is not None:
                    stack.enter_context(
                        patch.object(
                            verification._NativeCandidateVerifier, "verify", verify
                        )
                    )
                    stack.enter_context(
                        patch.object(execution, "run_bounded_process", process)
                    )
                    stack.enter_context(
                        patch.object(execution, "_require_results", results)
                    )
                status = main(["--json", *arguments])
            calls.append((arguments, status))
            (root / f"call-{len(calls)}.stdout").write_text(output.getvalue())
            (root / f"call-{len(calls)}.stderr").write_text(errors.getvalue())
            return status, json.loads(output.getvalue() or errors.getvalue())

        project, component, fixture, tools = author_public_project(
            root,
            self.seed,
            self.cargo,
            self.rustc,
            driver=driver,
            receipt=receipt,
            invoke=invoke,
        )
        oracle_before = fixture.oracle_path.read_bytes()
        agent = tools / "claude"
        body = agent.read_text().split("\n", 1)[1]
        native_command_prefix = (
            "pub fn answer(input: &str) -> String { "
            'if input.len() > 50 { return String::from("not-json"); } '
        )
        # This external provider deliberately makes its own tests agree with an
        # incorrect answer. It does not import the framework or read the oracle.
        script = (
            f"#!{sys.executable}\nimport sys,json,hashlib\nfrom pathlib import Path\n"
            f"body={body!r}\nmodes={modes!r}\n"
            f"log=Path({str(root / 'model-requests.jsonl')!r})\n"
            'if sys.argv[1:] != ["--version"]:\n'
            ' request=(Path.cwd()/".literate-ai-generation-request.md").read_text()\n'
            " count=len(log.read_text().splitlines()) if log.exists() else 0\n"
            ' with log.open("a") as stream:stream.write(json.dumps('
            '{"request":request,"workspace":str(Path.cwd())})+"\\n")\n'
            " mode=modes[min(count,len(modes)-1)]\n"
            ' if mode=="wrong":body=body.replace("42","41")\n'
            ' if mode=="test":body=body.replace(\'"answer":42\',\'"answer":41\',1)\n'
            ' if mode=="native-command":body=body.replace('
            "'pub fn answer(input: &str) -> String {',"
            f"{native_command_prefix!r},1)\n"
            # A replacement emits a new source tree even when it repeats the
            # same wrong behavior; competing custody for identical trees must
            # remain refused by the existing source registry.
            ' tag="// fresh request "+hashlib.sha256(request.encode()).hexdigest()'
            '+"\\n"\n'
            ' body=body.replace("LIBRARY = ","LIBRARY = "+repr(tag)+" + ",1)\n'
            'exec(compile(body,__file__,"exec"),{"__name__":"__main__","__file__":__file__})\n'
        )
        agent.write_text(script)
        compile(script, str(agent), "exec")
        environment["PATH"] = str(tools) + os.pathsep + environment.get("PATH", "")
        status, result = invoke(
            [
                "rebuild",
                component.relative_to(project).as_posix(),
                "--project",
                str(project),
                "--runtime-root",
                str(root / "runtime"),
                "--allow-host-execution",
                "--update-receipt",
            ]
        )
        requests = [
            json.loads(line)
            for line in (root / "model-requests.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len({r["workspace"] for r in requests}), len(requests))
        self.assertEqual(len({r["request"] for r in requests}), len(requests))
        for index, row in enumerate(requests):
            self.assertEqual(
                "## Bounded candidate-repair evidence" in row["request"], index > 0
            )
            self.assertNotIn("serde fixture", row["request"])
            self.assertNotIn(fixture.oracle.harness_content.decode(), row["request"])
        self.assertEqual(fixture.oracle_path.read_bytes(), oracle_before)
        passed = status == 0
        self.assertEqual((project / "verification/current.json").exists(), passed)
        self.assertLessEqual(len(requests), 3)
        self.assertEqual(passed, modes[-1] == "correct" and mutation is None, result)
        if mutation is not None:
            self.assertEqual(len(mutations), 1)
            self.assertEqual(len(requests), 1)
            self.assertNotIn(
                "component_acceptance.rust_harness_result_mismatch", json.dumps(result)
            )
        if passed:
            self.assertEqual(
                result["result"]["repair"],
                {"attempts": len(requests) - 1, "status": "repaired"},
            )
        # Retain actual rejected source, not merely a summary of its existence.
        bodies = [
            p.read_text()
            for p in root.rglob("lib.rs")
            if "qualified_fixture" not in str(p.parent) and p.stat().st_size < 4096
        ]
        if "wrong" in modes:
            self.assertTrue(any('"answer":41' in text for text in bodies))
        if "native-command" in modes:
            self.assertTrue(any("not-json" in text for text in bodies))
        native_failures = []
        terminal_failures = []
        for p in root.rglob("*"):
            if (
                not any("checkpoint" in part for part in p.parts)
                or not p.is_file()
                or len(p.name) != 64
            ):
                continue
            try:
                record = json.loads(p.read_bytes())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if (
                isinstance(record, dict)
                and record.get("schema")
                == "urn:literate-ai:schema:v2:standard-lifecycle-checkpoint"
            ):
                ev = record["evidence"]
                if (
                    ev.get("failure_code")
                    == "component_acceptance.rust_harness_result_mismatch"
                ):
                    self.assertEqual(ev["stage"], "accept")
                    self.assertEqual(ev["outcome"], "failed")
                    native_failures.append(record)
                if (
                    ev.get("failure_code")
                    == "component_acceptance.rust_harness_native_command_failed"
                ):
                    self.assertEqual(ev["stage"], "accept")
                    self.assertEqual(ev["outcome"], "failed")
                    terminal_failures.append(record)
        self.assertEqual(
            len(native_failures),
            0
            if mutation
            else 3
            if modes == ["wrong"]
            else 0
            if modes == ["native-command"]
            else 1,
        )
        self.assertEqual(
            len(terminal_failures), 1 if modes == ["native-command"] else 0
        )
        success[0] = True
        return status, result, requests

    def test_native_result_rejection_gets_fresh_same_authority_repair(self):
        status, result, requests = self._run(["wrong", "correct"])
        self.assertEqual(status, 0, result)
        self.assertEqual(len(requests), 2)

    def test_persistent_native_mismatch_exhausts_the_shared_budget(self):
        status, result, requests = self._run(["wrong"])
        self.assertNotEqual(status, 0, result)
        self.assertEqual(len(requests), 3)

    def test_generated_test_and_native_rejection_share_three_total_attempts(self):
        status, result, requests = self._run(["test", "wrong", "correct"])
        self.assertEqual(status, 0, result)
        self.assertEqual(len(requests), 3)

    def test_native_command_failure_is_terminal_not_result_repair(self):
        status, result, requests = self._run(["native-command"])
        self.assertNotEqual(status, 0, result)
        self.assertEqual(len(requests), 1)

    def test_actual_export_mutation_before_native_capture_is_terminal(self):
        self._run(["correct"], mutation="export-before")

    def test_actual_export_mutation_during_native_command_is_terminal(self):
        self._run(["correct"], mutation="export-during")

    def test_actual_native_executable_mutation_overrides_result_rejection(self):
        self._run(["wrong"], mutation="native-rejection")


if __name__ == "__main__":
    unittest.main()
