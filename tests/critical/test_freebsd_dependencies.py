"""FreeBSD loader parsing, native dispatch and exact dependency-custody boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.dependencies import observation as elf
from literate_ai.adapters.dependencies.freebsd import FreeBsdElfDependencyObserver
from literate_ai.adapters.dependencies.freebsd_loader import (
    FreeBsdLoaderState,
    parse_freebsd_hints,
)
from literate_ai.adapters.dependencies.types import DependencyObservationError
from literate_ai.adapters.freebsd_loader_paths import FreeBsdLoaderPaths
from literate_ai.adapters.user_paths import FreeBsdLoaderSystemPaths


class FreeBsdLoaderEnvironmentTests(unittest.TestCase):
    def test_explicit_library_paths_roundtrip_and_have_native_identity(self):
        value = FreeBsdLoaderPaths.from_environment(
            {"LD_LIBRARY_PATH": "/lib:/sdk/lib"}
        )
        self.assertEqual(value.library, ("/lib", "/sdk/lib"))
        self.assertEqual(value.to_environment(), {"LD_LIBRARY_PATH": "/lib:/sdk/lib"})
        self.assertNotEqual(value.identity, FreeBsdLoaderPaths().identity)

    def test_preloads_other_abi_controls_and_unsafe_paths_refuse(self):
        for environment in (
            {"LD_PRELOAD": "/tmp/inject.so"},
            {"LD_32_LIBRARY_PATH": "/lib32"},
            {"LD_ELF_HINTS_PATH": "/tmp/hints"},
            {"LD_LIBMAP": "a=b"},
            {"LD_LIBRARY_PATH_RPATH": "1"},
            {"LD_LIBRARY_PATH": "/lib:"},
            {"LD_LIBRARY_PATH": "/lib:relative"},
            {"LD_LIBRARY_PATH": "/lib/../other"},
        ):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                FreeBsdLoaderPaths.from_environment(environment)


@unittest.skipIf(os.name == "nt", "FreeBSD loader filesystem fixture uses POSIX paths")
class FreeBsdDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.library = self.root / "lib"
        self.library.mkdir()
        self.defaults = self.root / "defaults"
        self.defaults.mkdir()
        self.maps = self.root / "maps"
        self.maps.mkdir()
        self.hints = self.root / "hints"
        self.hints.write_bytes(b"native hint directory record")
        self.libmap = self.root / "libmap.conf"
        self.libmap.write_text("includedir " + str(self.maps) + "\n")
        self.reader = self.root / "readelf"
        self.ldconfig = self.root / "ldconfig"
        self.loader = self.root / "ld-elf.so.1"
        self.app = self.artifacts / "app"
        self.needed = {}
        self.rpaths = {}
        self.interpreters = {}
        self.headers = {}
        self.extra_dynamic = {}
        for path in (self.reader, self.ldconfig, self.loader, self.app):
            self.image(path)
        self.paths = FreeBsdLoaderSystemPaths(
            self.ldconfig, self.hints, self.libmap, (self.defaults,), self.loader
        )
        self.calls = []
        self.addCleanup(patch.stopall)
        patch("sys.platform", "freebsd16").start()
        patch(
            "literate_ai.adapters.dependencies.freebsd.resolve_freebsd_loader_system_paths",
            return_value=self.paths,
        ).start()
        patch.object(elf, "_resolved_bound_tool", return_value=self.reader).start()
        patch.object(elf, "_run_bounded_tool", side_effect=self.tool).start()

    def image(self, path, needed=(), rpath=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + str(path).encode())
        self.needed[str(path)] = needed
        self.rpaths[str(path)] = rpath
        return path

    def report(self):
        return f"{self.hints}:\n\tsearch directories: {self.library}\n"

    def tool(self, executable, arguments, **kwargs):
        self.calls.append((executable, tuple(arguments)))
        if executable == self.ldconfig:
            self.assertEqual(arguments, ("-r",))
            return self.report()
        self.assertEqual(executable, self.reader)
        mode, target = arguments
        if mode == "-h":
            return self.headers.get(
                target, "Class: ELF64\nOS/ABI: UNIX - FreeBSD\nMachine: X86-64\n"
            )
        if mode == "-d":
            text = "".join(
                f"(NEEDED) Shared library: [{n}]\n" for n in self.needed[target]
            )
            if self.rpaths[target]:
                kind, value = self.rpaths[target]
                text += f"({kind}) Library path: [{value}]\n"
            return text + self.extra_dynamic.get(target, "")
        if mode == "-l":
            value = self.interpreters.get(target)
            return f"[Requesting program interpreter: {value}]" if value else ""
        if mode == "-n":
            return ""
        self.fail("unmodeled inspector invocation")

    def observe(self, **changes):
        return elf.PortableHostDependencyObserver(
            **{"freebsd_loader_paths": FreeBsdLoaderPaths(), **changes}
        ).observe(
            {"artifact_path": str(self.artifacts)}, root_ref="urn:test:application"
        )

    def native_paths(self, observation):
        return {
            p["value"]
            for c in observation.components
            for p in c.get("properties", ())
            if p["name"] == "literate-ai:elf-path"
        }

    def test_hints_format_is_freebsd_and_rejects_linux_or_ambiguous_output(self):
        self.assertEqual(
            parse_freebsd_hints(self.report(), self.hints), (str(self.library),)
        )
        for text in (
            "12 libs found in cache `/etc/ld.so.cache'\n"
            "liba.so (libc6) => /lib/liba.so",
            self.report() + "unparsed warning\n",
            self.report().replace(str(self.library), "relative"),
        ):
            with (
                self.subTest(text=text),
                self.assertRaises((ValueError, DependencyObservationError)),
            ):
                parse_freebsd_hints(text, self.hints)

    def test_global_mapping_recursive_closure_and_loader_state_are_bound(self):
        dep = self.image(self.library / "libactual.so.1", ("libnested.so.1",))
        nested = self.image(self.defaults / "libnested.so.1")
        (self.maps / "native.conf").write_text("libalias.so.1 libactual.so.1\n")
        self.needed[str(self.app)] = ("libalias.so.1",)
        self.interpreters[str(self.app)] = str(self.loader)
        result = self.observe()
        self.assertTrue(
            {str(dep), str(nested), str(self.loader)} <= self.native_paths(result)
        )
        records = [
            p["value"]
            for c in result.components
            for p in c.get("properties", ())
            if p["name"] == "literate-ai:freebsd-loader-state"
        ]
        self.assertEqual(len(records), 1)
        state = json.loads(records[0])["loader"]
        self.assertEqual(state["maps"], [["libalias.so.1", "libactual.so.1"]])
        self.assertTrue(
            all(tool in (self.reader, self.ldconfig) for tool, _ in self.calls)
        )
        self.assertEqual(self.observe(), result)

    def test_rpath_runpath_and_explicit_environment_precedence(self):
        own = self.root / "own"
        env = self.root / "env"
        for directory in (own, env, self.library):
            self.image(directory / "libchoice.so.1")
        self.needed[str(self.app)] = ("libchoice.so.1",)
        for kind, expected in (("RPATH", own), ("RUNPATH", env)):
            with self.subTest(kind=kind):
                self.rpaths[str(self.app)] = (kind, str(own))
                self.app.write_bytes(b"\x7fELF" + kind.encode())
                result = self.observe(
                    freebsd_loader_paths=FreeBsdLoaderPaths((str(env),))
                )
                paths = self.native_paths(result)
                self.assertIn(str(expected / "libchoice.so.1"), paths)
                self.assertNotIn(str(self.library / "libchoice.so.1"), paths)

    def test_program_rpath_is_used_for_transitive_import(self):
        own = self.root / "own"
        a = self.image(own / "liba.so.1", ("libb.so.1",))
        b = self.image(own / "libb.so.1")
        self.needed[str(self.app)] = ("liba.so.1",)
        self.rpaths[str(self.app)] = ("RPATH", str(own))
        self.assertTrue({str(a), str(b)} <= self.native_paths(self.observe()))

    def test_observation_components_validate_against_cyclonedx_17(self):
        from literate_ai.adapters.dependencies.cyclonedx import _strict_validator

        result = self.observe()
        errors = _strict_validator().validate_str(
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "specVersion": "1.7",
                    "version": 1,
                    "components": list(result.components),
                }
            )
        )
        self.assertIsNone(errors, str(errors))

    def test_missing_candidate_cannot_appear_during_inspection(self):
        self.needed[str(self.app)] = ("libchoice.so.1",)
        self.image(self.defaults / "libchoice.so.1")
        original = self.tool

        def mutate(tool, args, **kwargs):
            output = original(tool, args, **kwargs)
            if args == ("-n", str(self.defaults / "libchoice.so.1")):
                self.image(self.library / "libchoice.so.1")
            return output

        with patch.object(elf, "_run_bounded_tool", side_effect=mutate):
            with self.assertRaisesRegex(
                DependencyObservationError, "candidates changed"
            ):
                self.observe()

    def test_origin_paths_resolve_without_running_the_image(self):
        own = self.artifacts / "private"
        dep = self.image(own / "liborigin.so.1")
        self.needed[str(self.app)] = ("liborigin.so.1",)
        self.rpaths[str(self.app)] = ("RUNPATH", "$ORIGIN/private")
        self.assertIn(str(dep), self.native_paths(self.observe()))

    def test_missing_library_other_abi_interpreter_and_unknown_tokens_refuse(self):
        for failure in ("missing", "interpreter", "token", "abi"):
            with self.subTest(failure=failure):
                self.needed[str(self.app)] = (
                    ("missing.so.1",) if failure == "missing" else ()
                )
                self.interpreters[str(self.app)] = (
                    "/other/ld.so" if failure == "interpreter" else None
                )
                self.rpaths[str(self.app)] = (
                    ("RPATH", "$PLATFORM/lib") if failure == "token" else None
                )
                self.headers.clear()
                if failure == "abi":
                    self.headers[str(self.app)] = (
                        "Class: ELF64\nOS/ABI: UNIX - GNU\nMachine: X86-64\n"
                    )
                self.app.write_bytes(b"\x7fELF" + failure.encode())
                with self.assertRaises(DependencyObservationError):
                    self.observe()

    def test_constrained_conflicting_maps_and_changed_includes_refuse(self):
        for content in (
            "[program]\nliba.so libb.so\n",
            "liba.so libb.so\nliba.so libc.so\n",
        ):
            with self.subTest(content=content):
                (self.maps / "native.conf").write_text(content)
                with self.assertRaises(DependencyObservationError):
                    FreeBsdLoaderState(self.paths)
        (self.maps / "native.conf").write_text("liba.so libb.so\n")
        state = FreeBsdLoaderState(self.paths)
        state.bind_hints(self.report())
        (self.maps / "added.conf").write_text("libx.so liby.so\n")
        with self.assertRaises(DependencyObservationError):
            state.require_unchanged()

    def test_loader_configuration_changed_during_inspection_is_rejected(self):
        original = self.tool

        def mutate(tool, args, **kwargs):
            output = original(tool, args, **kwargs)
            if tool == self.reader:
                self.hints.write_bytes(b"substituted native hints")
            return output

        with patch.object(elf, "_run_bounded_tool", side_effect=mutate):
            with self.assertRaisesRegex(
                DependencyObservationError, "configuration changed"
            ):
                self.observe()

    def test_elf_recognition_dispatches_freebsd_and_wrong_host_refuses(self):
        self.assertTrue(elf.is_host_native_executable(self.app))
        with patch("sys.platform", "linux"):
            with self.assertRaises(DependencyObservationError):
                FreeBsdElfDependencyObserver().observe(
                    {"artifact_path": str(self.artifacts)}, root_ref="urn:test:root"
                )


@unittest.skipUnless(sys.platform.startswith("freebsd"), "Actual FreeBSD native proof")
class NativeFreeBsdDependencyTests(unittest.TestCase):
    def test_compiled_program_has_real_inspected_loader_and_libc_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "hello.c"
            source.write_text(
                '#include <stdio.h>\nint main(void){puts("native");return 0;}\n'
            )
            compiler = shutil.which("cc")
            self.assertIsNotNone(
                compiler, "Native qualification requires the declared C compiler"
            )
            program = root / "hello"
            subprocess.run(
                (compiler, str(source), "-o", str(program)),
                check=True,
                capture_output=True,
                timeout=30,
            )
            result = elf.PortableHostDependencyObserver(
                artifact_files=(program,), freebsd_loader_paths=FreeBsdLoaderPaths()
            ).observe({"artifact_path": str(root)}, root_ref="urn:test:native-program")
            paths = {
                p["value"]
                for c in result.components
                for p in c.get("properties", ())
                if p["name"] == "literate-ai:elf-path"
            }
            self.assertIn(str(program), paths)
            self.assertTrue(any(Path(p).name.startswith("libc.so.") for p in paths))
            self.assertTrue(any(Path(p).name == "ld-elf.so.1" for p in paths))
            self.assertTrue(
                any(
                    c["name"] == "freebsd-loader-configuration"
                    for c in result.components
                )
            )


if __name__ == "__main__":
    unittest.main()
