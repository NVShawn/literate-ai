"""Native compiler-cache routing stays bound to approved command drivers."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.compiler_cache import CompilerCacheSession
from literate_ai.adapters.lifecycle import standard_local as module
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.native_cpp_build import compile_cpp
from literate_ai.adapters.qualification_capture import (
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    verify_qualification_build,
)
from literate_ai.adapters.shared_cache_config import load_shared_cache
from literate_ai.adapters.standard_project import (
    _STANDARD_BUILD_DRIVER,
    _encoded_toolchain_environment,
    native_cpp_cache_contract,
)
from literate_ai.contracts import ComponentCommandPhase, ComponentLifecycleCommand
from tests.support.fixtures_test_shared_cache import _configuration
from tests.support.fixtures_test_standard_local_command_adapter import (
    _identity,
    _python_copy_lifecycle,
)


def _observation():
    return {
        "schema": "literate-ai/compiler-cache-observation@1",
        "configuration_identity": _identity("cache-configuration").uri,
        "tool_identity": _identity("cache-tool").uri,
        "available": True,
        "cache_hits": 1,
        "cache_misses": 0,
        "compile_requests": 1,
    }


class NativeCompilerCacheTests(unittest.TestCase):
    def test_msvc_wraps_object_compilation_and_keeps_linking_uncached(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            compiler = str(root / "cl.exe")
            wrapper = str(root / "sccache.exe")
            completed = subprocess.CompletedProcess([], 0, b"", b"")
            with mock.patch(
                "literate_ai.adapters.native_cpp_build.subprocess.run",
                return_value=completed,
            ) as invoked:
                result = compile_cpp(
                    [compiler],
                    [str(root / "one.cpp"), str(root / "two.cpp")],
                    root,
                    root / "objects",
                    root / "app.exe",
                    {"LITAI_COMPILER_CACHE_TOOL": wrapper},
                )
            self.assertEqual(result.returncode, 0)
            commands = [call.args[0] for call in invoked.call_args_list]
            self.assertEqual(len(commands), 3)
            for command in commands[:2]:
                self.assertEqual(command[:2], [wrapper, compiler])
                self.assertIn("/c", command)
                self.assertTrue(
                    any(
                        arg.startswith("/Fo") and arg.endswith(".obj")
                        for arg in command
                    )
                )
            self.assertEqual(commands[-1][0], compiler)
            self.assertNotIn(wrapper, commands[-1])
            self.assertNotIn("/c", commands[-1])

    def test_only_the_approved_native_driver_gets_the_bound_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            ports, plan, _, _ = _python_copy_lifecycle(root)
            contract = ports.contracts[plan.component_revision.uri]
            command = ComponentLifecycleCommand(
                ComponentCommandPhase.BUILD,
                (
                    "{tool}",
                    "-c",
                    _STANDARD_BUILD_DRIVER,
                    "cpp-executable",
                    json.dumps([sys.executable]),
                    _encoded_toolchain_environment(()),
                    "{source_root}",
                    "source/main.cpp",
                    "{object_root}",
                    "{export_path}",
                ),
            )
            selected = replace(contract, commands=(command, *contract.commands[1:]))
            self.assertTrue(native_cpp_cache_contract(selected))
            self.assertFalse(native_cpp_cache_contract(contract))
            (root / "shared-cache.json").write_text(
                json.dumps(
                    _configuration(endpoint=None, credential_reference=None).to_dict()
                )
            )
            cache = load_shared_cache(
                environment={
                    "LITAI_CONFIG_DIR": str(root),
                    "LITAI_CACHE_DIR": str(root / "cache"),
                }
            )
            ports.shared_cache = replace(
                cache, compiler_tool=LocalComponentToolBinding(sys.executable)
            )

            @contextmanager
            def session(_binding, *, environment, workspace):
                self.assertNotIn("LITAI_COMPILER_CACHE_TOOL", environment)
                yield CompilerCacheSession(dict(environment), _observation())

            with (
                mock.patch.dict(
                    "os.environ", {"LITAI_COMPILER_CACHE_TOOL": "unrelated"}
                ),
                mock.patch.object(
                    module, "compiler_cache_session", side_effect=session
                ),
                mock.patch.object(
                    ports,
                    "_run_with_environment",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ) as invoked,
            ):
                result = ports._run_locked(
                    selected,
                    ComponentCommandPhase.BUILD,
                    source_root=root / "source",
                    object_root=root / "objects",
                    artifact_root=root,
                    export_path=root / "app",
                    providers=(),
                )
            self.assertEqual(
                invoked.call_args.kwargs["environment"]["LITAI_COMPILER_CACHE_TOOL"],
                ports.shared_cache.compiler_tool.executable,
            )
            self.assertEqual(result.compiler_cache_observation, _observation())

    def test_advisory_cache_observation_survives_build_capture_reopening(self):
        recorder = QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000)
        with tempfile.TemporaryDirectory() as temporary:
            ports, plan, _, _ = _python_copy_lifecycle(Path(temporary))
            ports.retain_evidence_with(recorder)
            original = ports._run_locked

            def observed(*args, **kwargs):
                result = original(*args, **kwargs)
                result.compiler_cache_observation = _observation()
                return result

            with mock.patch.object(ports, "_run_locked", side_effect=observed):
                built = ports.build(plan, ())
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=1000
        )
        verify_qualification_build(reader, plan=plan, build=built.evidence)
