from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.adapters.specifications import LiterateMarkdownProvider
from literate_ai.self_hosting import (
    FrozenPackageTree,
    SelfHostError,
    run_snapshot_replication,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "src" / "literate_ai"
SAMPLE_ROOT = REPO_ROOT / "samples" / "self-hosting"
COMPONENT_PATH = SAMPLE_ROOT / "component.md"
PROOF_HARNESS = (
    REPO_ROOT / "tests" / "conformance" / "support" / "self_hosting_proof.py"
)
HARNESS_ROOT = REPO_ROOT / "samples" / "_harness" / "self-hosting"
PROOF_MANIFEST = HARNESS_ROOT / "conformance" / "snapshot-replication.json"
SKILLS_MANIFEST = HARNESS_ROOT / "skills" / "self-hosting.json"


def platform_flavor() -> Path:
    import sys

    if sys.platform.startswith("linux"):
        host_os = "linux"
    elif sys.platform == "darwin":
        host_os = "macos"
    elif sys.platform in {"win32", "cygwin"}:
        host_os = "windows"
    else:
        raise AssertionError(f"unsupported test platform: {sys.platform}")
    return REPO_ROOT / "flavors" / f"os-{host_os}" / "flavor.md"


def sample_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(SAMPLE_ROOT.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(path.relative_to(SAMPLE_ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def self_host_temporary_directory() -> tempfile.TemporaryDirectory[str]:
    # Nested snapshot trees on Windows can exceed MAX_PATH in the default
    # per-runner temporary directory. The proof owns this disposable root, so
    # place it directly on the current drive there.
    directory = Path(tempfile.gettempdir()).anchor if os.name == "nt" else None
    return tempfile.TemporaryDirectory(prefix="litai-self-host-", dir=directory)


def synthetic_interpreter_layout(
    root: Path, *, platform: str = "linux"
) -> tuple[Path, dict[str, object]]:
    executable = root / ("python.exe" if platform == "win32" else "python")
    executable.write_bytes(b"synthetic interpreter identity")
    base = root / "runtime"
    stdlib = base / ("Lib" if platform == "win32" else "lib/python3.12")
    stdlib.mkdir(parents=True)
    dlls = base / "DLLs"
    if platform == "win32":
        dlls.mkdir()
    site_packages = stdlib / "site-packages"
    site_packages.mkdir()
    active_site_packages = root / "environment" / "site-packages"
    active_site_packages.mkdir(parents=True)
    json_origin = stdlib / "json" / "__init__.py"
    json_origin.parent.mkdir()
    json_origin.write_text("# synthetic json\n", encoding="utf-8")
    sysconfig_origin = stdlib / "sysconfig.py"
    sysconfig_origin.write_text("# synthetic sysconfig\n", encoding="utf-8")
    sys_path = [str(stdlib)]
    if platform == "win32":
        sys_path.append(str(dlls))
    return executable, {
        "schema": "literate-ai/python-interpreter-layout@1",
        "executable": str(executable),
        "base_prefix": str(base),
        "base_exec_prefix": str(base),
        "implementation": "cpython",
        "platform": platform,
        "flags": {"isolated": 1, "no_site": 1, "safe_path": True},
        "base_paths": {
            "stdlib": str(stdlib),
            "platstdlib": str(stdlib),
            "purelib": str(site_packages),
            "platlib": str(site_packages),
        },
        "active_package_paths": {
            "purelib": str(active_site_packages),
            "platlib": str(active_site_packages),
        },
        "sys_path": sys_path,
        "helper_origins": {
            "json": {"file": str(json_origin), "origin": str(json_origin)},
            "sysconfig": {
                "file": str(sysconfig_origin),
                "origin": str(sysconfig_origin),
            },
        },
    }


class SnapshotReplicationConformanceTests(unittest.TestCase):
    def test_candidate_environment_binds_the_running_python_toolchain(
        self,
    ) -> None:
        candidate = Path("candidate-source")
        dependencies = Path("dependency-projection")
        environment = runpy.run_path(str(PROOF_HARNESS))["_environment"]

        with mock.patch.dict(
            "os.environ",
            {
                "PYTHON": "/exact/toolchain/python",
                "PYTHONHOME": "/ambient/home",
                "PYTHONPATH": "/ambient/path",
            },
            clear=True,
        ):
            isolated = environment(candidate, dependencies)

        self.assertEqual(isolated["PYTHON"], str(Path(sys.executable).absolute()))
        self.assertNotIn("PYTHONHOME", isolated)
        self.assertEqual(
            isolated["PYTHONPATH"],
            os.pathsep.join((str(candidate), str(dependencies))),
        )
        self.assertEqual(isolated["PYTHONHASHSEED"], "0")

    def test_isolated_interpreter_probe_ignores_shadow_sysconfig(self) -> None:
        harness = runpy.run_path(str(PROOF_HARNESS))
        discover = harness["_python_standard_library_roots"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "shadow-imported"
            (root / "sysconfig.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('unsafe')\n"
                "raise RuntimeError('ambient sysconfig was imported')\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {
                    "PYTHONHOME": str(root / "ambient-home"),
                    "PYTHONPATH": str(root),
                },
            ):
                stdlib_roots, denied_roots = discover(
                    selected_executable=Path(sys.executable),
                    protected_roots=(root,),
                    working_directory=root,
                )

            self.assertTrue(stdlib_roots)
            self.assertTrue(all(not path.is_relative_to(root) for path in stdlib_roots))
            dependency_directory_names = {"site-packages", "dist-packages"}
            self.assertTrue(
                all(
                    dependency_directory_names.intersection(path.parts)
                    for path in denied_roots
                )
            )
            self.assertFalse(marker.exists())

    def test_windows_layout_admits_split_lib_and_dlls_but_denies_packages(
        self,
    ) -> None:
        harness = runpy.run_path(str(PROOF_HARNESS))
        validate = harness["_validate_interpreter_layout_probe"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable, document = synthetic_interpreter_layout(root, platform="win32")
            stdlib_roots, denied_roots = validate(
                json.dumps(document).encode("utf-8"),
                selected_executable=executable,
                protected_roots=(root / "replay-authority",),
            )

            base = root / "runtime"
            self.assertEqual(
                set(stdlib_roots), {(base / "Lib").resolve(), (base / "DLLs").resolve()}
            )
            self.assertIn((base / "Lib" / "site-packages").resolve(), denied_roots)
            self.assertNotIn((base / "Lib" / "site-packages").resolve(), stdlib_roots)

    def test_interpreter_layout_rejects_root_and_authority_overlap(self) -> None:
        harness = runpy.run_path(str(PROOF_HARNESS))
        validate = harness["_validate_interpreter_layout_probe"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable, document = synthetic_interpreter_layout(root)
            with self.subTest(case="filesystem-root"):
                rooted = dict(document)
                rooted["base_paths"] = dict(document["base_paths"])
                rooted["base_paths"]["stdlib"] = str(Path(root.anchor))
                with self.assertRaisesRegex(
                    harness["SelfHostSampleError"], "filesystem root|unsafe"
                ):
                    validate(
                        json.dumps(rooted).encode("utf-8"),
                        selected_executable=executable,
                        protected_roots=(root / "replay-authority",),
                    )

            with self.subTest(case="authority-overlap"):
                stdlib = root / "runtime" / "lib" / "python3.12"
                with self.assertRaisesRegex(
                    harness["SelfHostSampleError"], "overlaps replay authority"
                ):
                    validate(
                        json.dumps(document).encode("utf-8"),
                        selected_executable=executable,
                        protected_roots=(stdlib / "candidate",),
                    )

    def test_interpreter_layout_rejects_unbounded_or_malformed_output(self) -> None:
        harness = runpy.run_path(str(PROOF_HARNESS))
        validate = harness["_validate_interpreter_layout_probe"]
        maximum = harness["_MAX_INTERPRETER_LAYOUT_BYTES"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable, document = synthetic_interpreter_layout(root)
            cases = {
                "byte-budget": b"x" * (maximum + 1),
                "extra-field": json.dumps({**document, "extra": True}).encode(),
                "path-count": json.dumps(
                    {**document, "sys_path": [str(root / str(i)) for i in range(65)]}
                ).encode(),
            }
            for case, output in cases.items():
                with self.subTest(case=case):
                    with self.assertRaises(harness["SelfHostSampleError"]):
                        validate(
                            output,
                            selected_executable=executable,
                            protected_roots=(root / "replay-authority",),
                        )

    def test_interpreter_root_link_check_rejects_windows_reparse_points(self) -> None:
        is_link_like = runpy.run_path(str(PROOF_HARNESS))["_is_link_like"]
        linked = mock.Mock()
        linked.lstat.return_value = mock.Mock(
            st_mode=stat.S_IFDIR,
            st_file_attributes=0x0400,
        )
        regular = mock.Mock()
        regular.lstat.return_value = mock.Mock(
            st_mode=stat.S_IFDIR,
            st_file_attributes=0,
        )
        unavailable = mock.Mock()
        unavailable.lstat.side_effect = OSError("unavailable")

        self.assertTrue(is_link_like(linked))
        self.assertFalse(is_link_like(regular))
        self.assertTrue(is_link_like(unavailable))

    def test_candidate_probe_isolates_helpers_before_generated_paths(self) -> None:
        harness = runpy.run_path(str(PROOF_HARNESS))
        probe_candidate = harness["_probe_candidate"]
        events: list[str] = []

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            candidate_source = candidate / "source"
            candidate_source.mkdir(parents=True)
            (candidate_source / "json.py").write_text(
                "raise RuntimeError('generated helper shadow imported')\n",
                encoding="utf-8",
            )
            dependency_projection = root / "projected" / "site-packages"
            dependency_projection.mkdir(parents=True)
            stdlib = root / "trusted-stdlib"
            stdlib.mkdir()

            def discover(**_kwargs):
                events.append("layout")
                return (stdlib,), ()

            def project(_candidate_source, _projection_root):
                events.append("projection")
                return dependency_projection

            def run(command, *, environment, cwd):
                events.append("run")
                self.assertEqual(command[1:5], ["-I", "-S", "-P", "-B"])
                self.assertEqual(cwd, root / "probe")
                self.assertIn("PYTHONPATH", environment)
                source = command[6]
                self.assertLess(
                    source.index("import json"), source.index("sys.path[:0]")
                )
                self.assertLess(
                    source.index("sys.path[:0]"), source.index("import literate_ai")
                )
                return "{}"

            with (
                mock.patch.dict(
                    probe_candidate.__globals__,
                    {
                        "_python_standard_library_roots": discover,
                        "_candidate_dependency_projection": project,
                        "_run": run,
                    },
                ),
                mock.patch.dict("os.environ", {"PYTHON": sys.executable}, clear=True),
            ):
                self.assertEqual(
                    probe_candidate(
                        candidate,
                        original_root=root / "original",
                        sample_root=root / "sample",
                        scratch=root / "probe",
                    ),
                    {},
                )

            self.assertEqual(events, ["layout", "projection", "run"])

    def test_runtime_root_must_be_external_to_generation_authority(self) -> None:
        runtime_root = REPO_ROOT / ".self-host-runtime-must-stay-external"
        self.assertFalse(runtime_root.exists())

        with self.assertRaises(SelfHostError) as rejected:
            run_snapshot_replication(
                source_root=PACKAGE_ROOT,
                component_path=COMPONENT_PATH,
                skills_path=SKILLS_MANIFEST,
                proof_path=PROOF_MANIFEST,
                platform_flavor_path=platform_flavor(),
                runtime_root=runtime_root,
                host_build_acknowledged=True,
            )

        self.assertEqual(rejected.exception.code, "self-host.runtime-inside-authority")
        self.assertFalse(runtime_root.exists())

    def test_source_authority_is_rechecked_after_replication(self) -> None:
        original_capture = FrozenPackageTree.capture
        source_captures = 0

        def capture_with_drift(path: str | Path) -> FrozenPackageTree:
            nonlocal source_captures
            captured = original_capture(path)
            if Path(path).resolve() == PACKAGE_ROOT.resolve():
                source_captures += 1
                if source_captures > 1:
                    return FrozenPackageTree(
                        (*captured.files, ("zzzz-simulated-drift.py", "drift = True\n"))
                    )
            return captured

        with self_host_temporary_directory() as temporary:
            with mock.patch.object(
                FrozenPackageTree, "capture", side_effect=capture_with_drift
            ):
                with self.assertRaises(SelfHostError) as rejected:
                    run_snapshot_replication(
                        source_root=PACKAGE_ROOT,
                        component_path=COMPONENT_PATH,
                        skills_path=SKILLS_MANIFEST,
                        proof_path=PROOF_MANIFEST,
                        platform_flavor_path=platform_flavor(),
                        runtime_root=Path(temporary) / "runtime",
                        host_build_acknowledged=True,
                    )

        self.assertEqual(rejected.exception.code, "self-host.source-changed")
        self.assertEqual(source_captures, 2)

    def test_replication_proof_requires_explicit_host_build_acknowledgement(
        self,
    ) -> None:
        harness = runpy.run_path(str(PROOF_HARNESS))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                harness["SelfHostSampleError"], "explicit.*acknowledgement"
            ):
                harness["execute_replication_proof"](
                    repo_root=REPO_ROOT,
                    sample_root=SAMPLE_ROOT,
                    scratch=Path(temporary),
                )

    def test_isolated_candidate_timeout_terminates_descendant_process_tree(
        self,
    ) -> None:
        harness = runpy.run_path(str(PROOF_HARNESS))
        run_candidate = harness["_run"]
        globals_ = run_candidate.__globals__
        descendant = (
            "import time; print('descendant-ready', flush=True); time.sleep(60)"
        )
        parent = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
            "time.sleep(60)"
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(globals_, {"_CANDIDATE_PROCESS_TIMEOUT_SECONDS": 0.2}):
                with self.assertRaisesRegex(
                    harness["SelfHostSampleError"], "could not complete"
                ):
                    run_candidate(
                        [sys.executable, "-c", parent],
                        environment=dict(os.environ),
                        cwd=Path(temporary),
                    )
        self.assertLess(time.monotonic() - started, 5)

    def test_candidate_failure_diagnostic_retains_root_and_terminal_causes(
        self,
    ) -> None:
        harness = runpy.run_path(str(PROOF_HARNESS))
        diagnostic = harness["_candidate_failure_diagnostic"]
        maximum = harness["_CANDIDATE_FAILURE_DIAGNOSTIC_BYTES"]
        stderr = b"ROOT-CAUSE\n" + (b"x" * maximum) + b"\nTERMINAL-CAUSE"

        rendered = diagnostic(stderr)

        self.assertIn("ROOT-CAUSE", rendered)
        self.assertIn("stderr bytes omitted", rendered)
        self.assertTrue(rendered.endswith("TERMINAL-CAUSE"))

    def test_two_replays_are_stable_twice_from_clean_state(self) -> None:
        execute_proof = runpy.run_path(str(PROOF_HARNESS))["execute_replication_proof"]
        before = sample_digest()
        reports = []
        for _attempt in range(2):
            with self_host_temporary_directory() as temporary:
                with mock.patch(
                    "socket.create_connection",
                    side_effect=AssertionError(
                        "snapshot-replication proof must remain offline"
                    ),
                ):
                    reports.append(
                        execute_proof(
                            repo_root=REPO_ROOT,
                            sample_root=SAMPLE_ROOT,
                            scratch=Path(temporary),
                            host_build_acknowledged=True,
                        )
                    )
                    self.assertFalse((Path(temporary) / "cg").exists())
        self.assertEqual(reports[0], reports[1])
        report = reports[0]
        self.assertEqual(report["replication_count"], 2)
        self.assertEqual(
            report["classification"], "deterministic-offline-conformance-replay"
        )
        self.assertFalse(report["coding_cli_involved"])
        self.assertTrue(report["complete_file_set_stable"])
        self.assertFalse(report["original_import_fallback"])
        stable = report["stable_replication"]
        self.assertEqual(
            stable["classification"], "deterministic-offline-conformance-replay"
        )
        self.assertIn("security_classification", stable)
        self.assertEqual(
            stable["lifecycle_step_ids"],
            [
                "validate",
                "classify",
                "authorize-build",
                "build",
                "resolve-dependencies",
                "test-generated",
                "verify-independent",
                "prepare-tree",
                "commit-tree",
            ],
        )
        self.assertTrue(stable["validation"]["passed"])
        self.assertEqual(stable["authorization"]["profile"], "yolo")
        self.assertEqual(
            stable["build"]["compiled_file_count"], stable["python_file_count"]
        )
        self.assertTrue(stable["generated_tests"]["passed"])
        self.assertEqual(stable["generated_tests"]["test_count"], 3)
        self.assertTrue(stable["acceptance"]["passed"])
        self.assertEqual(
            stable["acceptance"]["execution_profile"],
            "non-executing-exact-tree",
        )
        self.assertEqual(
            stable["acceptance"]["suite_artifact_identity"],
            stable["generated_tests"]["test_suite_identity"],
        )
        for key in (
            "source_snapshot_id",
            "specification_id",
            "effective_revision_id",
            "accepted_tree_id",
        ):
            self.assertRegex(stable[key], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(sample_digest(), before)

    def test_readiness_recipe_excludes_replication_proof_inputs(self) -> None:
        component_bytes = COMPONENT_PATH.read_bytes()
        authoring = parse_component_markdown(
            COMPONENT_PATH,
            component_bytes.decode("utf-8"),
            project_root=REPO_ROOT,
        )
        loaded = LiterateMarkdownProvider().load(
            SAMPLE_ROOT,
            authoring.specification_roots,
            id_prefix=(f"{authoring.coordinate.namespace}.{authoring.coordinate.name}"),
        )
        self.assertEqual(
            {item.kind for item in authoring.authoring_inputs},
            {"specification-to-source-skill"},
        )
        self.assertEqual(authoring.specification_provider, "literate-markdown")
        self.assertEqual(authoring.specification_roots, ("component.md",))
        self.assertEqual(
            tuple(path for path, _content in loaded.contents), ("component.md",)
        )
        self.assertNotIn(PROOF_MANIFEST.read_bytes(), component_bytes)
        self.assertEqual(
            {item.uri for item in authoring.authoring_inputs},
            {
                "skills/specification-to-source/portable-application-implementation/SKILL.md",
                "skills/specification-to-source/portable-specification-planning/SKILL.md",
            },
        )

    def test_missing_source_unpinned_replay_skill_and_original_fallback_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            copied_package = root / "literate_ai"
            shutil.copytree(
                PACKAGE_ROOT,
                copied_package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            (copied_package / "self_hosting.py").unlink()
            with self.assertRaisesRegex(SelfHostError, "lacks required files"):
                FrozenPackageTree.capture(copied_package)

            shutil.rmtree(copied_package)
            shutil.copytree(
                PACKAGE_ROOT,
                copied_package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            with self.assertRaisesRegex(SelfHostError, "not imported from the package"):
                run_snapshot_replication(
                    source_root=copied_package,
                    component_path=COMPONENT_PATH,
                    skills_path=SKILLS_MANIFEST,
                    proof_path=PROOF_MANIFEST,
                    platform_flavor_path=platform_flavor(),
                    runtime_root=root / "fallback-runtime",
                )

            tampered_skills = root / "self-hosting.json"
            tampered_skills.write_bytes(SKILLS_MANIFEST.read_bytes() + b"\n")
            with self.assertRaisesRegex(SelfHostError, "does not match"):
                run_snapshot_replication(
                    source_root=PACKAGE_ROOT,
                    component_path=COMPONENT_PATH,
                    skills_path=tampered_skills,
                    proof_path=PROOF_MANIFEST,
                    platform_flavor_path=platform_flavor(),
                    runtime_root=root / "tampered-runtime",
                )


if __name__ == "__main__":
    unittest.main()
