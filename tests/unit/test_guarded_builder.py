"""The Python compiler cannot start without exact authorization."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    BuildError,
    CppToolchain,
    GuardedCppBuilder,
    GuardedPythonBuilder,
    canonical_tree_digest,
    discover_cpp_toolchain,
    discover_python_toolchain,
    executable_file_digest,
)
from literate_ai.adapters.builders import cpp as cpp_builder_module
from literate_ai.adapters.builders import python as python_builder_module
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.contracts import generated_source_tree_identity
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
    SecurityProfile,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 2, tzinfo=UTC)


class GuardedPythonBuilderTests(unittest.TestCase):
    @staticmethod
    def python_compile_calls(process_call):
        return [
            call
            for call in process_call.call_args_list
            if python_builder_module._PYTHON_COMPILE_SCRIPT in call.args[0]
        ]

    def request_and_authorization(
        self,
        source: Path,
        *,
        toolchain_digest: str | None = None,
        sandbox_profile: str = UNSANDBOXED_HOST_BUILD_PROFILE,
        requested_privileges: tuple[str, ...] = UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    ):
        source_digest = canonical_tree_digest(source)
        policy = SecurityPolicy(policy_digest=DIGEST_C)
        classification = policy.classify(
            effective_revision_digest=DIGEST_B,
            attestations=[
                OriginAttestation(
                    source_digest=source_digest,
                    signer="test",
                    trust_root="test",
                    signature_identity="test",
                    verified=True,
                )
            ],
            findings=[],
        )
        request = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=source_digest,
            builder_id=GuardedPythonBuilder.builder_id,
            toolchain_digest=(toolchain_digest or discover_python_toolchain().identity),
            sandbox_profile=sandbox_profile,
            requested_privileges=requested_privileges,
            allowed_outputs=("python-bytecode",),
        )
        authorization = policy.authorize_build(
            classification,
            request,
            actor="test",
            reason="conformance build",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        return request, authorization

    def test_tree_digest_uses_canonical_posix_path_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {"Z.txt": b"uppercase\n", "a.txt": b"lowercase\n"}
            for path, content in files.items():
                (root / path).write_bytes(content)

            self.assertEqual(
                canonical_tree_digest(root), generated_source_tree_identity(files)
            )

    def test_valid_authorization_builds_checked_hash_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text(
                "import json\n"
                "print(json.dumps({'value': 42}, sort_keys=True, "
                "separators=(',', ':')))\n",
                encoding="utf-8",
            )
            toolchain = discover_python_toolchain()
            request, authorization = self.request_and_authorization(
                source, toolchain_digest=toolchain.identity
            )
            artifact = GuardedPythonBuilder(
                AuthorizationRevocationSet(), toolchain=toolchain
            ).build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )
            self.assertEqual(artifact.compiled_files, ("hello.pyc",))
            bytecode = artifact.artifact_path / "hello.pyc"
            self.assertTrue(bytecode.is_file())
            self.assertEqual(artifact.toolchain_identity, toolchain.identity)
            completed = subprocess.run(
                [*toolchain.command, "-I", "-S", str(bytecode)],
                cwd=root,
                env=python_builder_module.controlled_python_environment(
                    dict(os.environ)
                ),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(completed.stdout.splitlines(), [b'{"value":42}'])

    def test_python_request_must_bind_builder_owned_exact_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                source, toolchain_digest=DIGEST_A
            )

            with patch.object(
                python_builder_module,
                "_run_bounded_process",
                wraps=python_builder_module._run_bounded_process,
            ) as process_call:
                with self.assertRaisesRegex(BuildError, "another Python toolchain"):
                    GuardedPythonBuilder(AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(self.python_compile_calls(process_call), [])

    def test_python_builder_rejects_falsely_constrained_host_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                source,
                sandbox_profile="constrained",
                requested_privileges=("compiler",),
            )

            with patch.object(
                python_builder_module,
                "_run_bounded_process",
                wraps=python_builder_module._run_bounded_process,
            ) as process_call:
                with self.assertRaisesRegex(BuildError, "unsandboxed-host") as error:
                    GuardedPythonBuilder(AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(
                error.exception.code, "builder.unsandboxed_host_profile_required"
            )
            self.assertEqual(self.python_compile_calls(process_call), [])

    def test_python_builder_requires_complete_ambient_privileges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                source,
                requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES[:-1],
            )

            with self.assertRaisesRegex(BuildError, "complete ambient") as error:
                GuardedPythonBuilder(AuthorizationRevocationSet()).build(
                    request,
                    authorization,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )
            self.assertEqual(
                error.exception.code, "builder.ambient_privileges_required"
            )

    def test_python_builder_requires_yolo_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source)
            authorization = replace(
                authorization, profile=SecurityProfile.CONSTRAINED, warning=None
            )

            with self.assertRaisesRegex(BuildError, "YOLO grant") as error:
                GuardedPythonBuilder(AuthorizationRevocationSet()).build(
                    request,
                    authorization,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )
            self.assertEqual(
                error.exception.code, "builder.yolo_authorization_required"
            )

    def test_python_executable_is_checked_before_and_after_compilation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            toolchain = discover_python_toolchain()
            request, authorization = self.request_and_authorization(
                source, toolchain_digest=toolchain.identity
            )
            digest_count = 0

            def drift_after_compile(path: Path) -> str:
                nonlocal digest_count
                digest_count += 1
                if digest_count == 7:
                    return DIGEST_A
                resolved = str(path.resolve(strict=True))
                if resolved == toolchain.runtime_executable:
                    return toolchain.runtime_digest
                return toolchain.launcher_digest

            with patch.object(
                python_builder_module,
                "executable_file_digest",
                side_effect=drift_after_compile,
            ):
                with self.assertRaisesRegex(BuildError, "bytes changed") as error:
                    GuardedPythonBuilder(
                        AuthorizationRevocationSet(), toolchain=toolchain
                    ).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(error.exception.code, "builder.python_toolchain_changed")
            self.assertEqual(digest_count, 7)

    def test_expired_authorization_prevents_compiler_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source)
            with patch.object(
                python_builder_module,
                "_run_bounded_process",
                wraps=python_builder_module._run_bounded_process,
            ) as process_call:
                with self.assertRaises(AuthorizationError):
                    GuardedPythonBuilder(AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=6),
                    )
                self.assertEqual(self.python_compile_calls(process_call), [])

    def test_shipped_default_fails_closed_without_live_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source)
            toolchain = discover_python_toolchain()
            with patch.object(
                python_builder_module,
                "_run_bounded_process",
                wraps=python_builder_module._run_bounded_process,
            ) as process_call:
                with self.assertRaisesRegex(
                    AuthorizationError,
                    "live_revocation_verifier_required",
                ):
                    GuardedPythonBuilder(toolchain=toolchain).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
                self.assertEqual(self.python_compile_calls(process_call), [])

    def test_source_drift_prevents_compiler_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = source / "hello.py"
            target.write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source)
            target.write_text("value = 43\n", encoding="utf-8")
            with patch.object(
                python_builder_module,
                "_run_bounded_process",
                wraps=python_builder_module._run_bounded_process,
            ) as process_call:
                with self.assertRaisesRegex(Exception, "changed after authorization"):
                    GuardedPythonBuilder(AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
                self.assertEqual(self.python_compile_calls(process_call), [])

    def test_source_drift_during_python_compilation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = source / "hello.py"
            target.write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source)
            real_process = python_builder_module._run_bounded_process

            def compile_then_mutate(*args, **kwargs):
                result = real_process(*args, **kwargs)
                if python_builder_module._PYTHON_COMPILE_SCRIPT in args[0]:
                    target.write_text("value = 43\n", encoding="utf-8")
                return result

            with patch.object(
                python_builder_module,
                "_run_bounded_process",
                side_effect=compile_then_mutate,
            ):
                with self.assertRaisesRegex(
                    BuildError, "changed during compilation"
                ) as error:
                    GuardedPythonBuilder(AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(
                error.exception.code, "builder.source_changed_during_build"
            )

    def test_revoked_authorization_prevents_compiler_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source)
            revocations = AuthorizationRevocationSet().revoke(
                authorization.authorization_id,
                actor="security",
                reason="fixture compromised",
            )

            with patch.object(
                python_builder_module,
                "_run_bounded_process",
                wraps=python_builder_module._run_bounded_process,
            ) as process_call:
                with self.assertRaisesRegex(AuthorizationError, "revoked"):
                    GuardedPythonBuilder(revocations).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(self.python_compile_calls(process_call), [])

    def test_cached_bytecode_is_verified_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source)
            builder = GuardedPythonBuilder(AuthorizationRevocationSet())
            artifact = builder.build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )
            (artifact.artifact_path / "hello.pyc").write_bytes(b"tampered")

            with self.assertRaisesRegex(BuildError, "Existing artifact differs"):
                builder.build(
                    request,
                    authorization,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )

    def test_staged_bytecode_rejects_undeclared_output_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "hello.py").write_text("value = 42\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source)
            real_match = python_builder_module._cached_artifact_matches

            def inject_undeclared_output(staging, *args, **kwargs):
                (staging / "undeclared.txt").write_text("rogue\n", encoding="utf-8")
                return real_match(staging, *args, **kwargs)

            with patch.object(
                python_builder_module,
                "_cached_artifact_matches",
                side_effect=inject_undeclared_output,
            ):
                with self.assertRaises(BuildError) as error:
                    GuardedPythonBuilder(AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(error.exception.code, "builder.artifact_invalid")
            self.assertEqual(list((root / "artifacts").iterdir()), [])


class GuardedCppBuilderTests(unittest.TestCase):
    @staticmethod
    def toolchain():
        try:
            return discover_cpp_toolchain()
        except BuildError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    @staticmethod
    def request_and_authorization(source: Path, toolchain):
        source_digest = canonical_tree_digest(source)
        policy = SecurityPolicy(policy_digest=DIGEST_C)
        classification = policy.classify(
            effective_revision_digest=DIGEST_B,
            attestations=(
                OriginAttestation(
                    source_digest=source_digest,
                    signer="test",
                    trust_root="test",
                    signature_identity="test",
                    verified=True,
                ),
            ),
            findings=(),
        )
        request = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=source_digest,
            builder_id=GuardedCppBuilder.builder_id,
            toolchain_digest=toolchain.identity,
            sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
            requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
            allowed_outputs=("native-executable",),
        )
        authorization = policy.authorize_build(
            classification,
            request,
            actor="test",
            reason="conformance build",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        return request, authorization

    def test_windows_gnu_link_disables_pe_timestamp(self) -> None:
        self.assertEqual(
            cpp_builder_module._gnu_linker_reproducibility_arguments("nt"),
            ("-Wl,--no-insert-timestamp",),
        )
        self.assertEqual(
            cpp_builder_module._gnu_linker_reproducibility_arguments("posix"), ()
        )

    def test_windows_default_cpp_toolchain_is_msvc_only(self) -> None:
        self.assertEqual(cpp_builder_module.DEFAULT_CPP_VERSION_TIMEOUT_SECONDS, 60.0)
        self.assertEqual(
            cpp_builder_module._default_cpp_compiler_names("nt"),
            ("cl", "clang-cl"),
        )
        self.assertEqual(
            cpp_builder_module._default_cpp_compiler_names("posix"),
            ("c++", "clang++", "g++"),
        )

    def test_msvc_help_is_reduced_to_its_exact_standard_sized_version(self) -> None:
        help_text = (
            "C/C++ COMPILER OPTIONS\r\n"
            + ("option details\r\n" * 2_000)
            + "Microsoft (R) C/C++ Optimizing Compiler Version "
            "19.44.35228 for x64\r\n"
        ).encode()

        self.assertEqual(
            cpp_builder_module._normalized_compiler_version(help_text, family="msvc"),
            "19.44.35228",
        )

    def test_msvc_version_requires_the_vendor_version_banner(self) -> None:
        with self.assertRaises(BuildError) as caught:
            cpp_builder_module._normalized_compiler_version(
                b"C/C++ COMPILER OPTIONS\r\n", family="msvc"
            )
        self.assertEqual(caught.exception.code, "builder.cpp_version_unparseable")

    def test_other_compilers_use_one_bounded_version_line(self) -> None:
        self.assertEqual(
            cpp_builder_module._normalized_compiler_version(
                b"clang version 20.1.0\nTarget: x86_64-pc-windows-msvc\n",
                family="msvc",
                clang_cl=True,
            ),
            "clang version 20.1.0",
        )
        with self.assertRaises(BuildError) as caught:
            cpp_builder_module._normalized_compiler_version(b"x" * 1025, family="gnu")
        self.assertEqual(caught.exception.code, "builder.cpp_version_unparseable")

    def test_msvc_environment_is_bound_into_toolchain_identity(self) -> None:
        configured = {
            "INCLUDE": "C:/SDK/include",
            "LIB": "C:/SDK/lib",
            "LIBPATH": "C:/SDK/libpath",
            "PATH": "C:/MSVC/bin;C:/Windows/System32",
        }
        environment = cpp_builder_module._windows_msvc_environment(
            Path(sys.executable), configured
        )
        self.assertEqual(
            environment,
            (
                ("INCLUDE", "C:/SDK/include"),
                ("LIB", "C:/SDK/lib"),
                ("LIBPATH", "C:/SDK/libpath"),
                ("PATH", "C:/MSVC/bin;C:/Windows/System32"),
            ),
        )
        executable = Path(sys.executable).resolve(strict=True)
        first = CppToolchain(
            (str(executable),),
            "msvc",
            "fixture",
            executable_file_digest(executable),
            environment,
        )
        second = CppToolchain(
            (str(executable),),
            "msvc",
            "fixture",
            executable_file_digest(executable),
            environment[:-1] + (("PATH", "C:/other"),),
        )
        self.assertNotEqual(first.identity, second.identity)

    def test_default_macos_sdk_selection_is_pinned_before_compiler_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sdk = root / "SDK with spaces"
            sdk.mkdir()
            metadata = sdk / "SDKSettings.json"
            metadata.write_text('{"Version":"1"}')
            developer = root / "Developer"
            developer.mkdir()
            calls = []

            def run(command, **kwargs):
                calls.append((tuple(command), dict(kwargs["environment"])))
                value = developer if "xcode-select" in command[0] else sdk
                return BoundedProcessResult(0, (str(value) + "\n").encode(), b"")

            with patch.object(
                cpp_builder_module, "_run_bounded_process", side_effect=run
            ):
                selected = cpp_builder_module._macos_sdk_environment(
                    {}, timeout_seconds=1
                )
            self.assertEqual(
                dict(selected), {"SDKROOT": str(sdk), "DEVELOPER_DIR": str(developer)}
            )
            self.assertEqual(calls[1][1]["DEVELOPER_DIR"], str(developer))
            identity = cpp_builder_module._sdk_selection_identity(selected)
            metadata.write_text('{"Version":"2"}')
            self.assertNotEqual(
                identity, cpp_builder_module._sdk_selection_identity(selected)
            )
            with patch.object(cpp_builder_module, "_run_bounded_process") as probe:
                self.assertEqual(
                    cpp_builder_module._macos_sdk_environment(
                        dict(selected), timeout_seconds=1
                    ),
                    selected,
                )
                probe.assert_not_called()

    def test_macos_sdk_discovery_and_bazel_options_fail_closed(self) -> None:
        for result in (
            BoundedProcessResult(1, b"/some/path\n", b""),
            BoundedProcessResult(0, b"/one\n/two\n", b""),
            BoundedProcessResult(0, b"relative\n", b""),
            BoundedProcessResult(0, b"\xff", b""),
        ):
            with (
                self.subTest(result=result),
                patch.object(
                    cpp_builder_module, "_run_bounded_process", return_value=result
                ),
            ):
                with self.assertRaises(BuildError):
                    cpp_builder_module._macos_sdk_environment({}, timeout_seconds=1)

        self.assertEqual(cpp_builder_module.bazel_sdk_build_options(()), ())
        sdk = "/SDK with space/a:b%"
        options = cpp_builder_module.bazel_sdk_build_options(
            (("SDKROOT", sdk), ("DEVELOPER_DIR", "/Dev"))
        )
        self.assertIn("--repo_env=SDKROOT=" + sdk, options)
        self.assertIn("--action_env=SDKROOT=" + sdk, options)
        self.assertIn("--host_action_env=DEVELOPER_DIR=/Dev", options)
        self.assertIn(
            "--repo_env=BAZEL_CXXOPTS=-std=c++17:-isystem:/SDK with space/a%:b%%",
            options,
        )
        self.assertIn(
            "--repo_env=BAZEL_CONLYOPTS=-isystem:/SDK with space/a%:b%%", options
        )

    def test_msvc_environment_is_loaded_from_vendor_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiler = (
                root
                / "VS"
                / "VC"
                / "Tools"
                / "MSVC"
                / "14.0"
                / "bin"
                / "Hostx64"
                / "x64"
                / "cl.exe"
            )
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b"fixture")
            setup = root / "VS" / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
            setup.parent.mkdir(parents=True)
            setup.write_bytes(b"fixture")
            command_interpreter = root / "Windows" / "System32" / "cmd.exe"
            command_interpreter.parent.mkdir(parents=True)
            command_interpreter.write_bytes(b"fixture")
            completed = BoundedProcessResult(
                0,
                (
                    b"INCLUDE=C:/VS/VC/Tools/MSVC/14.51/include;C:/SDK/include\r\n"
                    b"LIB=C:/VS/VC/Tools/MSVC/14.51/lib;C:/SDK/lib\r\n"
                    b"LIBPATH=C:/VS/VC/Tools/MSVC/14.51/libpath;C:/SDK/libpath\r\n"
                    b"Path=C:/VS/VC/Tools/MSVC/14.51/bin;C:/Windows/System32\r\n"
                ),
                b"",
            )
            observed: dict[str, str] = {}

            def initialize(command, **_kwargs):
                observed["wrapper"] = Path(command[4]).read_text(encoding="utf-8")
                observed["setup"] = command[5]
                observed["architecture"] = command[6]
                observed["toolset"] = command[7]
                return completed

            with patch.object(
                cpp_builder_module,
                "_run_bounded_process",
                side_effect=initialize,
            ):
                environment = cpp_builder_module._windows_msvc_environment(
                    compiler,
                    {"SystemRoot": str(root / "Windows"), "PATH": "C:/ambient"},
                )
        self.assertEqual(
            dict(environment),
            {
                "INCLUDE": "C:/VS/VC/Tools/MSVC/14.0/include;C:/SDK/include",
                "LIB": "C:/VS/VC/Tools/MSVC/14.0/lib;C:/SDK/lib",
                "LIBPATH": "C:/VS/VC/Tools/MSVC/14.0/libpath;C:/SDK/libpath",
                "PATH": "C:/VS/VC/Tools/MSVC/14.0/bin;C:/Windows/System32",
            },
        )
        self.assertEqual(observed["setup"], str(setup.resolve()))
        self.assertEqual(observed["architecture"], "x64")
        self.assertEqual(observed["toolset"], "-vcvars_ver=14.0")
        self.assertIn('@call "%~1" %~2 %~3 >nul 2>&1', observed["wrapper"])

    def test_msvc_environment_rejects_malformed_toolset_path(self) -> None:
        compiler = Path("C:/VS/VC/Tools/MSVC/current/bin/Hostx64/x64/cl.exe")
        with self.assertRaises(BuildError) as caught:
            cpp_builder_module._msvc_toolset_argument(compiler)
        self.assertEqual(
            caught.exception.code, "builder.cpp_msvc_environment_unavailable"
        )

    def test_msvc_environment_replaces_case_insensitive_ambient_values(self) -> None:
        executable = Path(sys.executable).resolve(strict=True)
        toolchain = CppToolchain(
            (str(executable),),
            "msvc",
            "fixture",
            executable_file_digest(executable),
            (("INCLUDE", "C:/SDK/include"), ("PATH", "C:/MSVC/bin")),
        )
        with patch.object(cpp_builder_module.os, "name", "nt"):
            environment = cpp_builder_module._compiler_process_environment(
                {"include": "ambient", "Path": "ambient", "OTHER": "retained"},
                toolchain,
            )
        self.assertEqual(
            environment,
            {
                "INCLUDE": "C:/SDK/include",
                "OTHER": "retained",
                "PATH": "C:/MSVC/bin",
            },
        )

    def test_msvc_compiles_utf8_source_to_utf8_execution_strings(self) -> None:
        executable = Path(sys.executable).resolve(strict=True)
        toolchain = CppToolchain(
            (str(executable),),
            "msvc",
            "fixture",
            executable_file_digest(executable),
        )
        command = GuardedCppBuilder(
            toolchain, AuthorizationRevocationSet()
        )._compile_command(
            ["source/main.cpp"],
            Path("artifact.exe"),
            Path("objects"),
            Path("source"),
        )

        self.assertIn("/utf-8", command)
        self.assertLess(command.index("/utf-8"), command.index("/link"))

    def test_valid_authorization_builds_native_executable(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int answer();\nint main() { return answer() == 42 ? 0 : 1; }\n",
                encoding="utf-8",
            )
            (source / "answer.cpp").write_text(
                "int answer() { return 42; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)
            artifact = GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )
            self.assertTrue(
                (artifact.artifact_path / artifact.executable_file).is_file()
            )
            self.assertEqual(artifact.compiler_identity, toolchain.identity)
            self.assertEqual(
                toolchain.executable_digest,
                executable_file_digest(Path(toolchain.command[0])),
            )
            completed = subprocess.run(
                [str(artifact.artifact_path / artifact.executable_file)],
                cwd=artifact.artifact_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_cpp_source_root_is_an_explicit_nested_header_include_root(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            module = source / "module"
            module.mkdir(parents=True)
            (source / "main.cpp").write_text(
                '#include "module/answer.hpp"\n'
                "int main() { return answer() == 42 ? 0 : 1; }\n",
                encoding="utf-8",
            )
            (module / "answer.hpp").write_text("int answer();\n", encoding="utf-8")
            (module / "answer.cpp").write_text(
                '#include "module/answer.hpp"\nint answer() { return 42; }\n',
                encoding="utf-8",
            )
            request, authorization = self.request_and_authorization(source, toolchain)

            artifact = GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )

            completed = subprocess.run(
                [str(artifact.artifact_path / artifact.executable_file)],
                cwd=artifact.artifact_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_cpp_test_translation_units_do_not_link_into_application(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            tests = source / "tests"
            embedded_tests = source / "source" / "tests"
            tests.mkdir(parents=True)
            embedded_tests.mkdir(parents=True)
            (source / "main.cpp").write_text(
                "int answer();\nint litai_test_marker();\n"
                "int main() { return answer() == 42 && litai_test_marker() == 7 "
                "? 0 : 1; }\n",
                encoding="utf-8",
            )
            (source / "answer.cpp").write_text(
                "int answer() { return 42; }\n", encoding="utf-8"
            )
            (source / "run_test.cpp").write_text(
                "int main() { return 99; }\n", encoding="utf-8"
            )
            (tests / "nested.cpp").write_text(
                "int main() { return 98; }\n", encoding="utf-8"
            )
            (source / "source" / "marker.hpp").write_text(
                "inline int embedded_marker() { return 7; }\n", encoding="utf-8"
            )
            (embedded_tests / "litai_test.cpp").write_text(
                '#include "marker.hpp"\n'
                "int litai_test_marker() { return embedded_marker(); }\n",
                encoding="utf-8",
            )
            request, authorization = self.request_and_authorization(source, toolchain)

            artifact = GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )

            completed = subprocess.run(
                [str(artifact.artifact_path / artifact.executable_file)],
                cwd=artifact.artifact_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_healthy_cpp_canary_types_generated_source_rejection(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int main() { this is not valid C++; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)

            with self.assertRaises(BuildError) as raised:
                GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                    request,
                    authorization,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )

            self.assertEqual(
                raised.exception.code, "builder.cpp_generated_source_rejected"
            )
            self.assertEqual(list((root / "artifacts").iterdir()), [])

    def test_failed_cpp_canary_keeps_compile_failure_nonretryable(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int main() { this is not valid C++; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)
            with (
                patch.object(
                    cpp_builder_module,
                    "_run_bounded_process",
                    return_value=BoundedProcessResult(1, b"", b"compiler unavailable"),
                ) as compiler,
                self.assertRaises(BuildError) as raised,
            ):
                GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                    request,
                    authorization,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )

            self.assertEqual(raised.exception.code, "builder.cpp_compile_failed")
            self.assertEqual(compiler.call_count, 2)

    def test_cpp_builder_rejects_falsely_constrained_host_request(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)
            constrained = replace(
                request,
                sandbox_profile="constrained",
                requested_privileges=("compiler",),
            )
            policy = SecurityPolicy(policy_digest=DIGEST_C)
            classification = policy.classify(
                effective_revision_digest=DIGEST_B,
                attestations=(
                    OriginAttestation(
                        source_digest=constrained.source_bundle_digest,
                        signer="test",
                        trust_root="test",
                        signature_identity="test",
                        verified=True,
                    ),
                ),
                findings=(),
            )
            constrained_authorization = policy.authorize_build(
                classification,
                constrained,
                actor="test",
                reason="false constrained build",
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                yolo_acknowledged=True,
            )

            with patch.object(
                cpp_builder_module, "_run_bounded_process"
            ) as compiler_call:
                with self.assertRaisesRegex(BuildError, "unsandboxed-host"):
                    GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                        constrained,
                        constrained_authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            compiler_call.assert_not_called()

    def test_cpp_executable_is_checked_before_and_after_compilation(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)
            with patch.object(
                cpp_builder_module,
                "executable_file_digest",
                side_effect=(toolchain.executable_digest, DIGEST_A),
            ) as digest_call:
                with self.assertRaisesRegex(BuildError, "bytes changed") as error:
                    GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(error.exception.code, "builder.cpp_toolchain_changed")
            self.assertEqual(digest_call.call_count, 2)

    def test_cpp_build_enforces_concurrent_output_caps(self) -> None:
        executable = Path(sys.executable).resolve(strict=True)
        script = (
            "import os\n"
            "chunk = b'x' * 4096\n"
            "while True:\n"
            "    os.write(1, chunk)\n"
            "    os.write(2, chunk)\n"
        )
        toolchain = CppToolchain(
            (str(executable), "-c", script),
            "gnu",
            "adversarial fixture compiler",
            executable_file_digest(executable),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)

            with self.assertRaises(BuildError) as error:
                GuardedCppBuilder(
                    toolchain,
                    AuthorizationRevocationSet(),
                    stdout_limit_bytes=1024,
                    stderr_limit_bytes=1024,
                ).build(
                    request,
                    authorization,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )
            self.assertEqual(error.exception.code, "builder.cpp_compile_output_limit")

    def test_source_drift_prevents_native_compiler_call(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = source / "main.cpp"
            target.write_text("int main() { return 0; }\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source, toolchain)
            target.write_text("int main() { return 1; }\n", encoding="utf-8")
            with patch.object(
                cpp_builder_module, "_run_bounded_process"
            ) as compiler_call:
                with self.assertRaisesRegex(BuildError, "changed after authorization"):
                    GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
                compiler_call.assert_not_called()

    def test_source_drift_during_native_compilation_is_rejected(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = source / "main.cpp"
            target.write_text("int main() { return 0; }\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(source, toolchain)
            real_run = cpp_builder_module._run_bounded_process

            def compile_then_mutate(*args, **kwargs):
                result = real_run(*args, **kwargs)
                target.write_text("int main() { return 1; }\n", encoding="utf-8")
                return result

            with patch.object(
                cpp_builder_module,
                "_run_bounded_process",
                side_effect=compile_then_mutate,
            ):
                with self.assertRaisesRegex(
                    BuildError, "changed during compilation"
                ) as error:
                    GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )
            self.assertEqual(
                error.exception.code, "builder.source_changed_during_build"
            )

    def test_cached_native_artifact_rejects_unexpected_files(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)
            builder = GuardedCppBuilder(toolchain, AuthorizationRevocationSet())
            artifact = builder.build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )
            (artifact.artifact_path / "unexpected.txt").write_text(
                "not authorized\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(BuildError, "Existing artifact differs"):
                builder.build(
                    request,
                    authorization,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )

    def test_staged_native_artifact_rejects_undeclared_output_before_publish(
        self,
    ) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)
            real_match = cpp_builder_module._cached_native_artifact_matches

            def inject_undeclared_output(staging, *args, **kwargs):
                (staging / "undeclared.txt").write_text("rogue\n", encoding="utf-8")
                return real_match(staging, *args, **kwargs)

            with patch.object(
                cpp_builder_module,
                "_cached_native_artifact_matches",
                side_effect=inject_undeclared_output,
            ):
                with self.assertRaises(BuildError) as error:
                    GuardedCppBuilder(toolchain, AuthorizationRevocationSet()).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(error.exception.code, "builder.artifact_invalid")
            self.assertEqual(list((root / "artifacts").iterdir()), [])

    def test_revoked_authorization_prevents_native_compiler_call(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "main.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            request, authorization = self.request_and_authorization(source, toolchain)
            revocations = AuthorizationRevocationSet().revoke(
                authorization.authorization_id,
                actor="security",
                reason="fixture compromised",
            )

            with patch.object(
                cpp_builder_module, "_run_bounded_process"
            ) as compiler_call:
                with self.assertRaisesRegex(AuthorizationError, "revoked"):
                    GuardedCppBuilder(toolchain, revocations).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            compiler_call.assert_not_called()


class BoundedCompilerProcessTests(unittest.TestCase):
    def test_short_lived_descendant_may_finish_closing_inherited_streams(self) -> None:
        parent = (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(1.25)'])\n"
        )
        started = time.monotonic()
        result = cpp_builder_module._run_bounded_process(
            [sys.executable, "-c", parent],
            cwd=None,
            environment=dict(os.environ),
            timeout_seconds=5,
            stdout_limit_bytes=1024,
            stderr_limit_bytes=1024,
            error_prefix="builder.test_compiler",
        )

        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(time.monotonic() - started, 1.0)

    def test_toolchain_discovery_enforces_version_output_cap(self) -> None:
        executable = str(Path(sys.executable).resolve(strict=True))
        script = "__import__('os').write(1,b'x'*4096)"
        command = (
            subprocess.list2cmdline([executable, "-c", script])
            if os.name == "nt"
            else shlex.join([executable, "-c", script])
        )
        environment = dict(os.environ)
        environment["CXX"] = command

        with self.assertRaises(BuildError) as error:
            discover_cpp_toolchain(
                environment,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
            )
        self.assertEqual(error.exception.code, "builder.cpp_version_output_limit")

    def test_concurrent_compiler_output_is_killed_at_the_first_cap(self) -> None:
        script = (
            "import os\n"
            "chunk = b'x' * 4096\n"
            "while True:\n"
            "    os.write(1, chunk)\n"
            "    os.write(2, chunk)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(BuildError) as error:
                cpp_builder_module._run_bounded_process(
                    [sys.executable, "-c", script],
                    cwd=Path(directory),
                    environment=dict(os.environ),
                    timeout_seconds=10,
                    stdout_limit_bytes=1024,
                    stderr_limit_bytes=1024,
                    error_prefix="builder.test_compiler",
                )
        self.assertEqual(error.exception.code, "builder.test_compiler_output_limit")

    def test_timeout_kills_compiler_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "descendant-survived"
            child = root / "child.py"
            child.write_text(
                "import pathlib, sys, time\n"
                "time.sleep(1)\n"
                "pathlib.Path(sys.argv[1]).write_text('alive')\n",
                encoding="utf-8",
            )
            parent = (
                "import subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
                "time.sleep(30)\n"
            )
            with self.assertRaises(BuildError) as error:
                cpp_builder_module._run_bounded_process(
                    [sys.executable, "-c", parent, str(child), str(sentinel)],
                    cwd=root,
                    environment=dict(os.environ),
                    timeout_seconds=0.2,
                    stdout_limit_bytes=1024,
                    stderr_limit_bytes=1024,
                    error_prefix="builder.test_compiler",
                )
            self.assertEqual(error.exception.code, "builder.test_compiler_timeout")
            time.sleep(1.1)
            self.assertFalse(
                sentinel.exists(), "compiler descendant escaped termination"
            )


if __name__ == "__main__":
    unittest.main()
