"""Real Cargo targets precede the Standard library case-result protocol."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.standard_project import _STANDARD_LIBRARY_TEST_DRIVER


class RustLibraryTestDriverTests(unittest.TestCase):
    def setUp(self):
        cargo = shutil.which("cargo")
        if cargo is None:
            self.skipTest("Cargo is unavailable")
        self.cargo = cargo
        temporary = tempfile.TemporaryDirectory(prefix="rust library targets ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.export = self.root / "library"
        self.package = self.export / "source"
        (self.package / "src/bin").mkdir(parents=True)
        (self.package / "tests").mkdir()
        (self.package / "Cargo.toml").write_text(
            '[package]\nname="fixture-library"\nversion="0.0.0"\nedition="2021"\n'
            '[[bin]]\nname="litai_artifact"\npath="src/bin/litai_artifact.rs"\n',
            encoding="utf-8",
        )
        (self.package / "src/lib.rs").write_text(
            "pub fn value() -> u8 { 7 }\n"
            "#[cfg(test)] mod tests { use super::value; "
            "#[test] fn unit_value() { assert_eq!(value(), 7); } }\n",
            encoding="utf-8",
        )
        (self.package / "src/bin/litai_artifact.rs").write_text(
            'fn main() { println!(r#"{{"schema":"literate-ai/generated-test-results@1",'
            '"cases":[{{"case_id":"example","outcome":"passed"}}]}}"#); }\n',
            encoding="utf-8",
        )
        self.integration = self.package / "tests/litai_test.rs"
        self.integration.write_text(
            "use fixture_library::value;\n"
            "#[test] fn integration_value() { assert_eq!(value(), 7); }\n",
            encoding="utf-8",
        )
        self.environment = {**os.environ, "CARGO_NET_OFFLINE": "true"}
        subprocess.run(
            [
                self.cargo,
                "generate-lockfile",
                "--offline",
                "--manifest-path",
                str(self.package / "Cargo.toml"),
            ],
            check=True,
            capture_output=True,
            env=self.environment,
            timeout=60,
        )

    def run_driver(self):
        before = self.snapshot()
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _STANDARD_LIBRARY_TEST_DRIVER,
                "rust",
                json.dumps([self.cargo]),
                str(self.export),
                "source/Cargo.toml",
                "litai_artifact",
                str(self.export.parent),
            ],
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=120,
        )
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.package / "target").exists())
        return completed

    def snapshot(self):
        return {
            p.relative_to(self.export).as_posix(): p.read_bytes()
            for p in self.export.rglob("*")
            if p.is_file()
        }

    def test_native_integration_and_nested_unit_tests_pass_before_json_protocol(self):
        result = self.run_driver()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "schema": "literate-ai/generated-test-results@1",
                "cases": [{"case_id": "example", "outcome": "passed"}],
            },
        )
        self.assertIn("integration_value", result.stderr)
        self.assertIn("unit_value", result.stderr)

    def test_failing_native_case_refuses_and_retains_case_diagnostic(self):
        self.integration.write_text(
            "use fixture_library::value;\n"
            "#[test] fn wrong_expected_value() { assert_eq!(value(), 99); }\n",
            encoding="utf-8",
        )
        result = self.run_driver()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("wrong_expected_value", result.stderr)
        self.assertIn("FAILED", result.stderr)


class RustVerifierDependencyExecutionTests(unittest.TestCase):
    """Native adapter controls; public installed Standard qualification is separate."""

    @classmethod
    def setUpClass(cls):
        from tests.support.fixtures_rust_library_harness import (
            prepare_native_verifier_class,
        )

        prepare_native_verifier_class(cls)

    def test_real_extra_crates_and_selected_features_reopen_and_refuse_drift(self):
        import re
        from dataclasses import replace

        from literate_ai.adapters.component_acceptance import ComponentAcceptanceError
        from literate_ai.adapters.lifecycle.standard_local import local_tree_identity
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceReader,
            QualificationEvidenceRecorder,
        )
        from literate_ai.adapters.rust_library_harness_execution import (
            execute_rust_library_harness,
            verify_rust_harness_execution,
        )
        from literate_ai.contracts import ContentIdentity
        from literate_ai.contracts.identity import canonical_json_bytes
        from literate_ai.contracts.product_json import product_json_bytes
        from tests.support.fixtures_rust_library_harness import (
            CASE_INPUT,
            NativeRustVerifierFixture,
        )

        for scenario in (
            "pass",
            "source-drift",
            "native-failure",
            "closing-drift",
            "manifest-drift",
        ):
            with (
                self.subTest(scenario=scenario),
                tempfile.TemporaryDirectory(
                    prefix="native Rust verifier "
                ) as temporary,
            ):
                root = Path(temporary).resolve()
                secret = "native-harness-private-token-3915"
                cargo = (
                    replace(self.cargo, environment=(("FIXTURE_NATIVE_TOKEN", secret),))
                    if scenario == "native-failure"
                    else self.cargo
                )
                fixture = NativeRustVerifierFixture(root, self.seed, cargo, self.rustc)
                if scenario == "native-failure":
                    (fixture.export / "src/lib.rs").write_text(
                        'compile_error!(env!("FIXTURE_NATIVE_TOKEN"));\n',
                        encoding="utf-8",
                    )
                expected_library = local_tree_identity(fixture.export)

                def require_library_current(
                    fixture=fixture, expected_library=expected_library
                ):
                    self.assertEqual(
                        local_tree_identity(fixture.export), expected_library
                    )

                recorder = QualificationEvidenceRecorder(
                    max_bytes=32 * 1024**2, max_records=64
                )
                stage = root / "stage"
                stage.mkdir()
                operation = execute_rust_library_harness(
                    fixture.oracle,
                    stage=stage,
                    export=fixture.export,
                    library_manifest=fixture.export / "Cargo.toml",
                    library_alias=fixture.surface.package,
                    cargo=cargo,
                    rustc=self.rustc,
                    cases_document=product_json_bytes(
                        [
                            {
                                "case_id": case.case_id,
                                "capability": case.capability,
                                "arguments": case.arguments,
                            }
                            for case in fixture.oracle.cases
                        ]
                    ).decode(),
                    surface_document=canonical_json_bytes(
                        fixture.surface.to_dict()
                    ).decode(),
                    environment=self.environment,
                    require_library_current=require_library_current,
                    record_bytes=recorder.remember_bytes,
                    evidence_root=root / "evidence",
                    allow_host_execution=True,
                )
                if scenario == "source-drift":
                    (
                        fixture.authority / "vendor/serde_json-1.0.151/src/lib.rs"
                    ).write_text(
                        "// actual unreviewed source mutation\n",
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError), operation:
                        self.fail("changed provisioned source ran a verifier")
                    self.assertFalse((root / "evidence").exists())
                    continue
                if scenario == "native-failure":
                    with (
                        self.assertRaises(ComponentAcceptanceError) as refused,
                        operation,
                    ):
                        self.fail("failed native compilation admitted acceptance")
                    self.assertEqual(
                        refused.exception.code,
                        "component_acceptance.rust_harness_native_command_failed",
                    )
                    self.assertNotIn(secret, str(refused.exception))
                    identity = ContentIdentity.parse_uri(
                        re.search(
                            r"sha256:[0-9a-f]{64}", str(refused.exception)
                        ).group()
                    )
                    reader = QualificationEvidenceReader(
                        recorder.entries, max_bytes=32 * 1024**2, max_records=64
                    )
                    failure = reader.read_json(identity)
                    self.assertIs(failure["qualified"], False)
                    self.assertEqual(failure["commands"][-1]["phase"], "build")
                    stderr = reader.read_bytes(
                        ContentIdentity.parse_uri(
                            failure["commands"][-1]["stderr_identity"]
                        )
                    )
                    self.assertIn(secret.encode(), stderr)
                    self.assertTrue(any((root / "evidence/blobs/sha256").rglob("*")))
                    continue
                if scenario == "closing-drift":
                    with (
                        self.assertRaises(ValueError),
                        operation as (result, _observation),
                    ):
                        self.assertEqual(result.returncode, 0)
                        (fixture.authority / "workspace/new-unreviewed.txt").write_text(
                            "real post-execution membership change",
                            encoding="utf-8",
                        )
                    continue
                if scenario == "manifest-drift":
                    with (
                        self.assertRaises(ValueError),
                        operation as (result, _observation),
                    ):
                        self.assertEqual(result.returncode, 0)
                        with (stage / "Cargo.toml").open(
                            "a", encoding="utf-8"
                        ) as stream:
                            stream.write("\n# unreviewed generated manifest change\n")
                    continue
                with operation as (result, observation):
                    self.assertEqual(
                        json.loads(result.stdout)["cases"][0]["result"],
                        {"answer": 42, "echo": CASE_INPUT},
                    )
                identity = recorder.remember_json(observation)
                reader = QualificationEvidenceReader(
                    recorder.entries, max_bytes=32 * 1024**2, max_records=64
                )
                verify_rust_harness_execution(
                    reader,
                    identity,
                    oracle=fixture.oracle,
                    library_alias=fixture.surface.package,
                    surface=fixture.surface,
                )
                (fixture.authority / "Cargo.lock").write_text(
                    "# actual retained verifier lock drift\n",
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    verify_rust_harness_execution(
                        reader,
                        identity,
                        oracle=fixture.oracle,
                        library_alias=fixture.surface.package,
                        surface=fixture.surface,
                    )

    def test_own_path_and_fixed_git_dependency_graph_reopens_and_refuses_drift(self):
        from literate_ai.adapters.lifecycle.standard_local import local_tree_identity
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceReader,
            QualificationEvidenceRecorder,
        )
        from literate_ai.adapters.rust_library_harness_execution import (
            execute_rust_library_harness,
            verify_rust_harness_execution,
        )
        from literate_ai.contracts.identity import canonical_json_bytes
        from literate_ai.contracts.product_json import product_json_bytes
        from tests.support.fixtures_rust_library_harness import (
            NativeRustVerifierFixture,
        )
        from tests.support.fixtures_rust_own_graph import author_own_dependency_graph

        root = Path(tempfile.mkdtemp(prefix="native Rust own graph ")).resolve()
        succeeded = [False]

        def retain_failure_or_remove_success():
            if succeeded[0]:
                shutil.rmtree(root)
            else:
                print(
                    f"Retained failed own Cargo graph fixture: {root}", file=sys.stderr
                )

        self.addCleanup(retain_failure_or_remove_success)
        fixture = NativeRustVerifierFixture(root, self.seed, self.cargo, self.rustc)
        path_root, git_checkout = author_own_dependency_graph(fixture, self.environment)
        expected_library = local_tree_identity(fixture.export)

        def require_library_current():
            self.assertEqual(local_tree_identity(fixture.export), expected_library)

        stage = root / "stage"
        stage.mkdir()
        recorder = QualificationEvidenceRecorder(max_bytes=48 * 1024**2, max_records=96)
        with execute_rust_library_harness(
            fixture.oracle,
            stage=stage,
            export=fixture.export,
            library_manifest=fixture.export / "Cargo.toml",
            library_alias=fixture.surface.package,
            cargo=self.cargo,
            rustc=self.rustc,
            cases_document=product_json_bytes(
                [
                    {
                        "case_id": row.case_id,
                        "capability": row.capability,
                        "arguments": row.arguments,
                    }
                    for row in fixture.oracle.cases
                ]
            ).decode(),
            surface_document=canonical_json_bytes(fixture.surface.to_dict()).decode(),
            environment=self.environment,
            require_library_current=require_library_current,
            record_bytes=recorder.remember_bytes,
            evidence_root=root / "evidence",
            allow_host_execution=True,
        ) as (result, observation):
            self.assertEqual(
                json.loads(result.stdout)["cases"][0]["result"]["answer"], 42
            )
        self.assertEqual(len(observation["resolution"]["packages"]), 15)
        identity = recorder.remember_json(observation)
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=48 * 1024**2, max_records=96
        )

        def reopen():
            verify_rust_harness_execution(
                reader,
                identity,
                oracle=fixture.oracle,
                library_alias=fixture.surface.package,
                surface=fixture.surface,
            )

        reopen()
        for kind, source in (
            ("owned-path", path_root / "src/lib.rs"),
            ("fixed-git-checkout", git_checkout / "src/lib.rs"),
        ):
            original = source.read_bytes()
            with self.subTest(changed_current_source=kind):
                source.write_bytes(original + b"\n// Actual dependency byte drift.\n")
                with self.assertRaises(ValueError):
                    reopen()
            # This does not restore a live execution owner. Each historical
            # reopen captures current verifier inputs afresh and grants no run.
            source.write_bytes(original)
        succeeded[0] = True

    def test_real_verifier_configuration_checksum_features_and_output_refuse(self):
        from literate_ai.adapters.component_acceptance import ComponentAcceptanceError
        from literate_ai.adapters.lifecycle.standard_local import local_tree_identity
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceReader,
            QualificationEvidenceRecorder,
        )
        from literate_ai.adapters.rust_library_harness_execution import (
            execute_rust_library_harness,
        )
        from literate_ai.contracts import ContentIdentity
        from literate_ai.contracts.identity import canonical_json_bytes
        from literate_ai.contracts.product_json import product_json_bytes
        from tests.support.fixtures_rust_library_harness import (
            NativeRustVerifierFixture,
        )

        expected_codes = {
            "configuration": "configuration_override",
            "checksum": "native_command_failed",
            "features": "resolution_drift",
            "output": "result_protocol",
        }
        for scenario, reason in expected_codes.items():
            with self.subTest(scenario=scenario):
                root = Path(tempfile.mkdtemp(prefix="native Rust refusal ")).resolve()
                verified = [False]

                def retain_failure_or_remove_success(root=root, verified=verified):
                    if verified[0]:
                        shutil.rmtree(root)
                    else:
                        print(
                            f"Retained failed Rust refusal fixture: {root}",
                            file=sys.stderr,
                        )

                self.addCleanup(retain_failure_or_remove_success)
                fixture = NativeRustVerifierFixture(
                    root, self.seed, self.cargo, self.rustc
                )
                if scenario == "configuration":
                    configuration = fixture.authority / "workspace/.cargo/config.toml"
                    with configuration.open("a", encoding="utf-8") as stream:
                        stream.write('\n[build]\nrustc="unreviewed-compiler"\n')
                elif scenario == "checksum":
                    source = fixture.authority / "vendor/serde_json-1.0.151/src/lib.rs"
                    with source.open("a", encoding="utf-8") as stream:
                        stream.write(
                            "\n// Real extracted source differs from crate checksum.\n"
                        )
                elif scenario == "features":
                    manifest = fixture.export / "Cargo.toml"
                    manifest.write_text(
                        manifest.read_text("utf-8").replace(
                            'default=["checked"]',
                            'default=["checked","unreviewed"]',
                        )
                        + "\nunreviewed=[]\n",
                        encoding="utf-8",
                    )
                else:
                    fixture.harness = b'fn main() { println!("{{}}"); }\n'
                    (fixture.authority / "harness.rs").write_bytes(fixture.harness)
                # This is newly authored verifier intent, captured AFTER the
                # mutation. A stale owner cannot short-circuit these controls:
                # actual config policy, native checksums, graph or output must
                # decide refusal under otherwise genuine current custody.
                fixture.author_oracle()
                expected_library = local_tree_identity(fixture.export)

                def require_library_current(
                    fixture=fixture, expected_library=expected_library
                ):
                    self.assertEqual(
                        local_tree_identity(fixture.export), expected_library
                    )

                stage = root / "stage"
                stage.mkdir()
                recorder = QualificationEvidenceRecorder(
                    max_bytes=32 * 1024**2, max_records=64
                )
                with (
                    self.assertRaises(ComponentAcceptanceError) as refused,
                    execute_rust_library_harness(
                        fixture.oracle,
                        stage=stage,
                        export=fixture.export,
                        library_manifest=fixture.export / "Cargo.toml",
                        library_alias=fixture.surface.package,
                        cargo=self.cargo,
                        rustc=self.rustc,
                        cases_document=product_json_bytes(
                            [
                                {
                                    "case_id": case.case_id,
                                    "capability": case.capability,
                                    "arguments": case.arguments,
                                }
                                for case in fixture.oracle.cases
                            ]
                        ).decode(),
                        surface_document=canonical_json_bytes(
                            fixture.surface.to_dict()
                        ).decode(),
                        environment=self.environment,
                        require_library_current=require_library_current,
                        record_bytes=recorder.remember_bytes,
                        evidence_root=root / "evidence",
                        allow_host_execution=True,
                    ),
                ):
                    self.fail("actual invalid verifier inputs admitted acceptance")
                self.assertEqual(
                    refused.exception.code,
                    "component_acceptance.rust_harness_" + reason,
                )
                if scenario == "configuration":
                    self.assertFalse((stage / "Cargo.toml").exists())
                if scenario == "checksum":
                    import re

                    failure_identity = ContentIdentity.parse_uri(
                        re.search(
                            r"sha256:[0-9a-f]{64}", str(refused.exception)
                        ).group()
                    )
                    reader = QualificationEvidenceReader(
                        recorder.entries, max_bytes=32 * 1024**2, max_records=64
                    )
                    failure = reader.read_json(failure_identity)
                    self.assertIs(failure["qualified"], False)
                    diagnostics = b"\n".join(
                        reader.read_bytes(
                            ContentIdentity.parse_uri(row["stderr_identity"])
                        )
                        for row in failure["commands"]
                    ).lower()
                    self.assertIn(b"checksum", diagnostics)
                    self.assertIn(b"serde_json", diagnostics)
                verified[0] = True

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("make"),
        "the public Standard source journey needs a POSIX external coding CLI",
    )
    def test_public_standard_library_rebuild_uses_reviewed_extra_crates(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch

        from literate_ai.adapters.builders._process import run_bounded_process
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
        from tests.support.fixtures_rust_harness_boundaries import (
            author_native_boundary_graph,
            bind_exact_native_input_boundaries,
            public_boundary_refusals,
        )

        root = Path(tempfile.mkdtemp(prefix="public Rust verifier ")).resolve()
        succeeded = [False]

        def retain_failure_or_remove_success():
            if succeeded[0]:
                shutil.rmtree(root)
            else:
                print(f"Retained failed public Rust fixture: {root}", file=sys.stderr)

        self.addCleanup(retain_failure_or_remove_success)
        evidence = root / "cli-evidence"
        evidence.mkdir()
        calls = []
        # Editable source cannot supply a measured installed distribution. This
        # observation-only seam is explicit; all project/lock/native authority is
        # real. Installed-wheel qualification is a separate required control.
        distribution = InstalledFrameworkDistribution(
            "literate-ai",
            "1.1.0",
            (
                InstalledFrameworkDistributionMember(
                    "literate_ai/__init__.py",
                    1,
                    "sha256:" + "a" * 64,
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
        environment = dict(self.environment)
        for name in tuple(environment):
            if name.startswith("LITAI_") or name.startswith("GIT_"):
                environment.pop(name)
        environment.update(
            {
                "CARGO": shlex.quote(self.cargo.command[0]),
                "RUSTC": shlex.quote(self.rustc.command[0]),
                "BUILD_DIR": str(root / "build"),
                "OBJ_DIR": str(root / "obj"),
                "CODING_CLI": "claude",
            }
        )

        # A synthetic operator home proves that the fixture does not pick up
        # ambient Cargo configuration. No real operator files are changed.
        operator_home = root / "synthetic operator"
        operator_cargo = operator_home / ".cargo"
        operator_cargo.mkdir(parents=True)
        marker = root / "hostile-wrapper-invoked"
        wrapper_source = root / "rejecting-wrapper.rs"
        wrapper_source.write_text(
            'fn main() { std::fs::write(std::env::var_os("FIXTURE_WRAPPER_MARKER")'
            '.unwrap(), b"invoked").unwrap(); eprintln!("fixture-hostile-wrapper");'
            " std::process::exit(91); }\n",
            encoding="utf-8",
        )
        wrapper = root / (
            "rejecting wrapper é😀.exe" if os.name == "nt" else "rejecting wrapper é😀"
        )
        configuration = operator_cargo / "config.toml"
        configuration_bytes = (
            "[build]\nrustc-wrapper="
            + json.dumps(str(wrapper), ensure_ascii=False)
            + "\n"
        ).encode()
        configuration.write_bytes(configuration_bytes)
        environment.update(
            HOME=str(operator_home),
            USERPROFILE=str(operator_home),
            FIXTURE_WRAPPER_MARKER=str(marker),
        )

        def run_native(arguments, values, label):
            result = run_bounded_process(
                tuple(arguments),
                cwd=root,
                environment=values,
                timeout_seconds=60,
                stdout_limit_bytes=1024**2,
                stderr_limit_bytes=1024**2,
                terminate_descendants=True,
                trace=False,
                error_prefix="fixture.cargo_isolation_" + label,
            )
            (evidence / (label + ".stdout")).write_bytes(result.stdout)
            (evidence / (label + ".stderr")).write_bytes(result.stderr)
            return result

        compiled = run_native(
            (*self.rustc.command, str(wrapper_source), "-o", str(wrapper)),
            environment,
            "compile-wrapper",
        )
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        control = root / "control"
        (control / "src").mkdir(parents=True)
        (control / "Cargo.toml").write_text(
            '[package]\nname="cargo-isolation-control"\nversion="0.0.0"\n'
            'edition="2021"\n[workspace]\n',
            encoding="utf-8",
        )
        (control / "src/lib.rs").write_text("pub fn value() {}\n", encoding="utf-8")
        negative = run_native(
            (
                *self.cargo.command,
                "check",
                "--offline",
                "--manifest-path",
                str(control / "Cargo.toml"),
            ),
            {**environment, "CARGO_HOME": str(operator_cargo)},
            "hostile-home-control",
        )
        self.assertNotEqual(negative.returncode, 0)
        self.assertIn(b"fixture-hostile-wrapper", negative.stderr)
        self.assertEqual(marker.read_bytes(), b"invoked")
        marker.unlink()

        def invoke(arguments):
            output, errors = io.StringIO(), io.StringIO()
            status = None
            calls.append(tuple(arguments))
            record = evidence / f"call-{len(calls)}"
            try:
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch(
                        "literate_ai.cli.rebuild.resolve_standard_project_lifecycle_driver",
                        lambda value: resolve_standard_project_lifecycle_driver(
                            value,
                            distribution_observer=lambda: distribution,
                        ),
                    ),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    status = main(["--json", *arguments])
            finally:
                record.with_suffix(".stdout").write_text(output.getvalue())
                record.with_suffix(".stderr").write_text(errors.getvalue())
                record.with_suffix(".json").write_text(
                    json.dumps({"arguments": arguments, "exit": status})
                )
            raw = output.getvalue() or errors.getvalue()
            self.assertTrue(raw, (status, arguments, errors.getvalue()))
            return status, json.loads(raw)

        project, component, fixture, tools = author_public_project(
            root,
            self.seed,
            self.cargo,
            self.rustc,
            driver=driver,
            receipt=receipt,
            invoke=invoke,
        )
        boundary = author_native_boundary_graph(fixture, environment)
        self.assertEqual(boundary["direct_dependencies"], 128)
        self.assertEqual(boundary["resolved_selected_features"], 4096)
        bind_exact_native_input_boundaries(fixture)
        # Record real authored authority after the actual local-registry and
        # finite-policy inputs exist; never patch a review marker by hand.
        status, result = invoke(
            ["project", "documentation-review", str(project), "--record"]
        )
        self.assertEqual(status, 0, result)
        environment["PATH"] = str(tools) + os.pathsep + environment.get("PATH", "")
        oracle_bytes = fixture.oracle_path.read_bytes()
        status, result = invoke(
            [
                "rebuild",
                component.relative_to(project).as_posix(),
                "--project",
                str(project),
                "--allow-host-execution",
                "--update-receipt",
            ]
        )
        self.assertEqual(status, 0, result)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["result"]["passed"], result)
        self.assertTrue(result["result"]["receipt_committed"], result)
        self.assertTrue((project / "verification/current.json").is_file())
        self.assertEqual(fixture.oracle_path.read_bytes(), oracle_bytes)
        receipt_bytes = (project / "verification/current.json").read_bytes()
        for scenario, document, code, message in public_boundary_refusals(
            fixture, boundary
        ):
            with self.subTest(public_refusal=scenario):
                fixture.oracle_path.write_text(
                    json.dumps(document, sort_keys=True), encoding="utf-8"
                )
                status, refused = invoke(
                    [
                        "rebuild",
                        component.relative_to(project).as_posix(),
                        "--project",
                        str(project),
                        "--allow-host-execution",
                        "--update-receipt",
                    ]
                )
                self.assertEqual(status, 2, refused)
                self.assertIs(refused["ok"], False)
                self.assertEqual(refused["error"]["code"], code, refused)
                if message is not None:
                    self.assertIn(message, refused["error"]["message"])
                self.assertEqual(
                    (project / "verification/current.json").read_bytes(), receipt_bytes
                )
        # The actual public oracle remains the source of authority. Restore its
        # bytes only after all failed invocations have retired; this grants no
        # owner or execution from the successful historical receipt.
        fixture.oracle_path.write_bytes(oracle_bytes)
        missing = fixture.authority / "vendor/fixture-boundary-001-1.0.0/src/lib.rs"
        missing.unlink()
        status, refused = invoke(
            [
                "rebuild",
                component.relative_to(project).as_posix(),
                "--project",
                str(project),
                "--allow-host-execution",
                "--update-receipt",
            ]
        )
        self.assertEqual(status, 2, refused)
        self.assertIs(refused["ok"], False)
        self.assertEqual(refused["error"]["code"], "cli.invalid_input", refused)
        self.assertIn(
            "rust.harness.provisioned-inputs-changed",
            refused["error"]["message"],
        )
        self.assertFalse(missing.exists(), "missing inputs must not be fetched")
        self.assertEqual(
            (project / "verification/current.json").read_bytes(), receipt_bytes
        )
        self.assertFalse(
            marker.exists(), "public rebuild selected the hostile Cargo wrapper"
        )
        self.assertEqual(configuration.read_bytes(), configuration_bytes)
        succeeded[0] = True


if __name__ == "__main__":
    unittest.main()
