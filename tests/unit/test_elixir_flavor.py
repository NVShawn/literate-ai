"""Elixir catalog installation and Standard command execution contracts."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from literate_ai.adapters.builders import BuildError, discover_elixir_toolchain
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.builders.elixir import ElixirToolchain
from literate_ai.adapters.flavor_add import FlavorAddError, add_flavor_to_project
from literate_ai.adapters.generation_preparation import (
    FilesystemLockedGenerationApplicationAdapter,
)
from literate_ai.adapters.host_install import load_host_install_sbom
from literate_ai.adapters.lifecycle.standard_runtime import (
    STANDARD_ELIXIR_RUNTIME_DRIVER,
    direct_service_process_argv,
)
from literate_ai.adapters.models import portable_source_entrypoint
from literate_ai.adapters.multi_entrypoint_build import build_many
from literate_ai.adapters.project_initialization import initialize_project
from literate_ai.adapters.standard_project import (
    _STANDARD_BUILD_DRIVER,
    project_locked_standard_toolchain_closure,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.bootstrap.host_install_requirements import HostInstallTarget
from literate_ai.cli.generation import load_flavor_catalog
from literate_ai.contracts import (
    ComponentCommandPhase,
    RepositoryParentSelection,
    parse_standard_command_profile,
)
from tests.conformance.support.sample_runner import _matrix_languages
from tests.unit.test_schema_catalog import SchemaCatalog
from tests.unit.test_standard_command_projection import (
    _locked_snapshot,
    _observation,
    _tool,
)

REPO = Path(__file__).resolve().parents[2]


def _encoded(value):
    return base64.urlsafe_b64encode(zlib.compress(json.dumps(value).encode())).decode()


class ElixirCatalogTests(unittest.TestCase):
    def test_native_observation_uses_beam_and_retains_script_and_module_hashes(self):
        paths = ("elixir", "beam.smp", "Kernel.beam", "JSON.beam", "json.beam", "sh")
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(ElixirToolchain, "require_unchanged"),
            mock.patch(
                "literate_ai.adapters.standard_project.PortableHostDependencyObserver"
            ) as observer,
        ):
            root = Path(temporary)
            bindings = tuple(
                (str(root / name), f"sha256:{index:064x}")
                for index, name in enumerate(paths)
            )
            for path, _ in bindings:
                Path(path).write_bytes(b"fixture")
            tool = ElixirToolchain(
                (str(root / "elixir"),), "1.18.0", (1, 18, 0), "27", bindings
            )
            observer.return_value.observe.return_value = _observation(())
            _, snapshot, execution = _locked_snapshot(
                root / "authority",
                language="elixir",
                platform="linux",
                build_system="bazel",
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="linux",
                toolchain_discoverer=lambda name, *_: (
                    tool if name == "elixir" else _tool(name)
                ),
            )
            commands = observer.call_args.kwargs["toolchain_commands"]
            self.assertIn((str(root / "beam.smp"),), commands)
            self.assertIn((str(root / "sh"),), commands)
            self.assertNotIn(tool.command, commands)
            self.assertIn(
                f"--action_env=LITAI_LANGUAGE_TOOL={tool.command[0]}",
                closure.bazel_targets[0].build_options,
            )
            files = [
                item
                for item in closure.dependency_observation.components
                if item["bom-ref"].startswith("urn:literate-ai:beam-toolchain-file:")
            ]
            self.assertEqual({item["name"] for item in files}, set(paths))
            self.assertTrue(
                all(len(item["hashes"][0]["content"]) == 64 for item in files)
            )

    def test_qualification_selection_and_host_prerequisites(self):
        self.assertEqual(_matrix_languages(("+lang-elixir",)), ("elixir",))
        self.assertIn("elixir", _matrix_languages(("lang.*",)))
        self.assertEqual(portable_source_entrypoint("elixir"), "source/main.exs")
        for target in (
            HostInstallTarget("macos", "arm64"),
            HostInstallTarget("macos", "x86_64"),
            HostInstallTarget("windows", "x86_64"),
            HostInstallTarget("linux-debian", "arm64"),
            HostInstallTarget("linux-debian", "x86_64"),
        ):
            with self.subTest(target=target):
                _, sbom = load_host_install_sbom(
                    REPO / "flavors", target, flavor_names=("lang-elixir",)
                )
                self.assertEqual(
                    sbom.base.capability("elixir").version_constraint, ">=1.18"
                )
                self.assertEqual(
                    sbom.base.capability("erlang").version_constraint, ">=27"
                )
                self.assertEqual(
                    {item.capability for item in sbom.packages} & {"elixir", "erlang"},
                    {"elixir", "erlang"},
                )

    def test_catalog_profile_schema_and_template_parity(self):
        (flavor,) = load_flavor_catalog((REPO / "flavors/lang-elixir",))
        self.assertEqual(flavor.coordinate_uri, "flavor://literate-ai/lang-elixir")
        self.assertEqual(flavor.value, "elixir")
        self.assertEqual(flavor.skills[0].skill_id, "elixir-portable-application")
        data = json.loads(
            (REPO / "flavors/lang-elixir/standard-command-profile.json").read_text()
        )
        self.assertEqual(
            parse_standard_command_profile(data).artifact_layout.value, "tree"
        )
        SchemaCatalog().validate(data["schema"], data)
        for relative in (
            "flavors/lang-elixir",
            "skills/specification-to-source/elixir-portable-application",
        ):
            source = REPO / relative
            template = REPO / "src/literate_ai/project_template" / relative
            files = {p.relative_to(source) for p in source.rglob("*") if p.is_file()}
            self.assertEqual(
                files,
                {p.relative_to(template) for p in template.rglob("*") if p.is_file()},
            )
            for path in files:
                self.assertEqual(
                    (source / path).read_bytes(), (template / path).read_bytes()
                )

    def test_canonical_mix_catalog_installs_and_locks_generation_skill(self):
        (flavor,) = load_flavor_catalog((REPO / "flavors/build-mix",))
        self.assertEqual(flavor.coordinate_uri, "flavor://literate-ai/build-mix")
        self.assertEqual(flavor.skills[0].skill_id, "mix-build-system")
        profile = json.loads(
            (REPO / "flavors/build-mix/standard-command-profile.json").read_text()
        )
        self.assertEqual(parse_standard_command_profile(profile).target, "mix")
        SchemaCatalog().validate(profile["schema"], profile)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "initialized"
            initialize_project(
                target,
                parent_selection=RepositoryParentSelection.root(),
                source_intelligence_provider="none",
                empty=True,
                flavor_selectors=("+elixir", "+linux", "+mix"),
            )
            for relative in (
                "flavors/build-mix",
                "skills/specification-to-source/mix-build-system",
            ):
                source = REPO / relative
                template = REPO / "src/literate_ai/project_template" / relative
                for path in source.rglob("*"):
                    if path.is_file():
                        name = path.relative_to(source)
                        self.assertEqual(
                            path.read_bytes(), (template / name).read_bytes()
                        )
                        self.assertEqual(
                            path.read_bytes(), (target / relative / name).read_bytes()
                        )
            component, snapshot, _ = _locked_snapshot(
                root / "locked",
                language="elixir",
                build_system="mix",
                generation_ready=True,
            )
            prepared = FilesystemLockedGenerationApplicationAdapter().prepare(
                GenerationPreparationRequest(
                    component_root=component,
                    target_name=snapshot.authority.lock.target_name,
                    flavor_selectors=snapshot.authority.requested_flavor_selectors,
                    flavor_roots=(root / "locked/flavors",),
                )
            )
            self.assertIn(
                "mix-build-system",
                {skill.skill_id for skill in prepared.recipe.resolved_skills},
            )

    def test_init_and_add_install_skill_and_preserve_language_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for language in ("elixir", "python"):
                target = root / language
                initialize_project(
                    target,
                    parent_selection=RepositoryParentSelection.root(),
                    source_intelligence_provider="none",
                    empty=True,
                    flavor_selectors=(f"+{language}", "+linux"),
                )
                if language == "python":
                    with self.assertRaises(FlavorAddError) as caught:
                        add_flavor_to_project(target, "elixir")
                    self.assertEqual(caught.exception.code, "flavor.add_conflict")
                    add_flavor_to_project(target, "elixir", set_default=False)
                self.assertTrue(
                    (target / "flavors/lang-elixir/toolchain.json").is_file()
                )
                self.assertTrue(
                    (
                        target
                        / "skills/specification-to-source"
                        / "elixir-portable-application/SKILL.md"
                    ).is_file()
                )

    def test_locked_profiles_bind_elixir_runtime_on_every_os_and_entrypoint(self):
        for platform in ("macos", "linux", "windows"):
            for build_system, multiple in (
                (None, False),
                (None, True),
                ("make", False),
                ("bazel", False),
            ):
                with (
                    self.subTest(
                        platform=platform, build=build_system, multiple=multiple
                    ),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    _, snapshot, execution = _locked_snapshot(
                        Path(temporary),
                        language="elixir",
                        platform=platform,
                        build_system=build_system,
                        duplicate_entrypoint=multiple,
                    )
                    closure = project_locked_standard_toolchain_closure(
                        snapshot,
                        execution,
                        host_platform=platform,
                        toolchain_discoverer=lambda name, *_: _tool(name),
                        dependency_observer=_observation,
                    )
                    closure.require_unchanged()
                    for contract in closure.contracts:
                        self.assertEqual(
                            contract.language_runtime_identity.uri,
                            _tool("elixir").identity,
                        )
                        self.assertIn(
                            STANDARD_ELIXIR_RUNTIME_DRIVER,
                            contract.command(ComponentCommandPhase.TEST).argv,
                        )


class ElixirToolchainTests(unittest.TestCase):
    def test_missing_toolchain_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(BuildError) as caught:
                discover_elixir_toolchain({"PATH": temporary})
            self.assertEqual(
                caught.exception.code, "builder.elixir_toolchain_unavailable"
            )

    def test_version_constraints_and_core_bytes_are_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vm = root / "erts-15.2/bin/beam.smp"
            vm.parent.mkdir(parents=True)
            vm.write_bytes(b"vm")
            files = [
                root / name for name in ("kernel.beam", "Elixir.JSON.beam", "json.beam")
            ]
            for path in files:
                path.write_bytes(b"module")
            record = dict(
                version="1.18.4",
                otp="27",
                erts="15.2",
                root=str(root),
                files=list(map(str, files)),
            )

            def result(*args, **kwargs):
                return BoundedProcessResult(0, json.dumps(record).encode(), b"")

            with mock.patch(
                "literate_ai.adapters.builders.elixir.run_bounded_process",
                side_effect=result,
            ):
                tool = discover_elixir_toolchain(
                    {"PATH": ""},
                    pinned_command=(sys.executable,),
                    required_version=(1, 18),
                )
                original = tool.identity
                files[0].write_bytes(b"changed")
                with self.assertRaises(BuildError) as caught:
                    tool.require_unchanged()
                self.assertEqual(
                    caught.exception.code, "builder.elixir_toolchain_changed"
                )
                changed = discover_elixir_toolchain(
                    {"PATH": ""}, pinned_command=(sys.executable,)
                )
                self.assertNotEqual(original, changed.identity)
                for updates, options in (
                    ({}, {"required_version": (1, 19)}),
                    ({"otp": "26"}, {}),
                    ({"version": "1.17.3"}, {}),
                    ({"version": "garbage"}, {}),
                ):
                    with self.subTest(updates=updates, options=options):
                        saved = dict(record)
                        record.update(updates)
                        with self.assertRaises(BuildError):
                            discover_elixir_toolchain(
                                {"PATH": ""},
                                pinned_command=(sys.executable,),
                                **options,
                            )
                        record.update(saved)


class ElixirBuildTests(unittest.TestCase):
    def test_build_parses_all_modules_without_evaluating_them_and_refuses_bad_source(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "candidate"
            (source / "source/tests").mkdir(parents=True)
            for relative in (
                "source/main.exs",
                "source/helper.ex",
                "source/tests/litai_test.exs",
            ):
                (source / relative).write_text(
                    'raise "must not execute during build"\n'
                )
            checker = root / "checker.py"
            checker.write_text(
                "import pathlib,sys\n"
                "files=sys.argv[sys.argv.index('--')+1:]\nassert len(files)==3\n"
                "assert 'Code.string_to_quoted!' in sys.argv[2]\n"
                "sys.exit(any('INVALID' in pathlib.Path(p).read_text() "
                "for p in files))\n"
            )
            compiler = [sys.executable, str(checker)]
            export = root / "export"

            def build():
                return subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        _STANDARD_BUILD_DRIVER,
                        "elixir-tree",
                        json.dumps(compiler),
                        _encoded([]),
                        str(source),
                        "source/main.exs",
                        str(root / "objects"),
                        str(export),
                    ],
                    capture_output=True,
                )

            built = build()
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(
                (export / "source/helper.ex").read_bytes(),
                (source / "source/helper.ex").read_bytes(),
            )
            shutil.rmtree(export)
            (source / "source/helper.ex").write_text("INVALID")
            self.assertNotEqual(build().returncode, 0)
            self.assertFalse(export.exists())
            (source / "source/helper.ex").write_text("valid")
            artifacts = root / "artifacts"
            artifacts.mkdir()
            build_many(
                "elixir-tree",
                compiler,
                _encoded([]),
                source,
                root / "objects",
                artifacts,
                artifacts / "primary",
                _encoded(
                    [
                        dict(source="source/main.exs", export_id="primary"),
                        dict(source="source/tests/litai_test.exs", export_id="test"),
                    ]
                ),
            )
            self.assertTrue((artifacts / "test/source/helper.ex").is_file())

    def test_service_process_owns_the_packaged_elixir_entrypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entry = root / "source/main.exs"
            entry.parent.mkdir()
            entry.write_text("nil\n")
            command = (
                "elixir",
                "-e",
                STANDARD_ELIXIR_RUNTIME_DRIVER,
                "--",
                str(root),
                str(root),
                "tree",
                "source/main.exs",
                "--litai-smoke",
            )
            self.assertEqual(
                direct_service_process_argv(command),
                ("elixir", str(entry), "--litai-serve"),
            )

    @unittest.skipUnless(
        shutil.which("elixir"), "native Elixir/OTP runtime is unavailable"
    )
    def test_native_runtime_runs_after_generation_workspace_is_removed(self):
        tool = discover_elixir_toolchain()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "candidate"
            entry = source / "source/main.exs"
            entry.parent.mkdir(parents=True)
            (entry.parent / "helper.ex").write_text(
                "defmodule Helper do\n def value, do: 42\nend\n"
            )
            entry.write_text(
                'Code.require_file("helper.ex", __DIR__)\n'
                '["--litai-smoke"] = System.argv()\n'
                "IO.puts(JSON.encode!(Helper.value()))\n"
            )
            export = root / "artifact"
            built = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _STANDARD_BUILD_DRIVER,
                    "elixir-tree",
                    json.dumps(tool.command),
                    _encoded([]),
                    str(source),
                    "source/main.exs",
                    str(root / "objects"),
                    str(export),
                ],
                capture_output=True,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            shutil.rmtree(source)
            completed = subprocess.run(
                [
                    *tool.command,
                    "-e",
                    STANDARD_ELIXIR_RUNTIME_DRIVER,
                    "--",
                    str(root),
                    str(export),
                    "tree",
                    "source/main.exs",
                    "--litai-smoke",
                ],
                cwd=root,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), 42)


if __name__ == "__main__":
    unittest.main()
