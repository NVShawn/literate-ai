"""Vertical-slice tests: convert inspects a real legacy tree and emits a working
harness wrapper, and detected flavors flow into ``litai init`` selector resolution."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.adapters.harness_inventory import (
    HARNESS_INVENTORY_SCHEMA,
    HARNESS_WRAPPER_FILENAME,
    _run_harness_command,
    render_harness_wrapper,
)
from literate_ai.adapters.monorepo_adoption import (
    SELECTION_SCHEMA,
    MonorepoAdoptionError,
)
from literate_ai.adapters.monorepo_components import (
    check_installed_monorepo_components,
)
from literate_ai.adapters.project_initialization import (
    ProjectInitializationError,
    detect_repo_flavors,
    host_platform_selector,
)
from literate_ai.adapters.project_validation import (
    ProjectValidationError,
    validate_project,
)
from literate_ai.adapters.retained_scope_refresh import (
    apply_retained_scope_refresh,
    plan_retained_scope_refresh,
)
from literate_ai.contracts import ProjectInitializationOrigin
from tests.support.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)


def _origin() -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        repository_url="ssh://git.example.test/operator/literate-ai.git",
        git_revision="c" * 40,
        distribution_name="literate-ai",
        distribution_version="0.2.0",
    )


def _adapter() -> FilesystemProjectInitializationAdapter:
    return FilesystemProjectInitializationAdapter(
        initialization_origin_provider=_origin,
        standard_binding_provider=lambda: None,
    )


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_repo_man_fixture(target: Path) -> None:
    """Synthetic NVIDIA repo.sh monorepo: root driver plus kit/rendering roots."""

    _write_executable(
        target / "repo.sh",
        "#!/bin/sh\n"
        "set -eu\n"
        'case "${1:-}" in\n'
        "  build)\n"
        "    ./kit/repo.sh build\n"
        "    ./rendering/repo.sh build\n"
        "    echo built > build.sentinel\n"
        "    ;;\n"
        "  *)\n"
        '    echo "unknown command: ${1:-}" >&2\n'
        "    exit 2\n"
        "    ;;\n"
        "esac\n",
    )
    nested = '#!/bin/sh\nset -eu\necho "$(basename "$(pwd)") ${1:-}"\nexit 0\n'
    _write_executable(target / "kit" / "repo.sh", nested)
    _write_executable(target / "rendering" / "repo.sh", nested)
    (target / "kit" / "repo.toml").write_text('name = "kit"\n', encoding="utf-8")
    (target / "rendering" / "repo.toml").write_text(
        'name = "rendering"\n', encoding="utf-8"
    )
    (target / "kit" / "main.cpp").write_text(
        "int main() { return 0; }\n", encoding="utf-8"
    )
    (target / "rendering" / "tools.py").write_text("print('hi')\n", encoding="utf-8")
    workflow = target / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    runs-on: ${{ matrix.os }}\n"
        "    strategy:\n"
        "      matrix:\n"
        "        os: [ubuntu-latest, windows-latest]\n",
        encoding="utf-8",
    )
    (target / ".gitlab-ci.yml").write_text(
        "test:\n  script: echo ok\n", encoding="utf-8"
    )


def _signed_selectors(target: Path) -> tuple[str, ...]:
    selectors = detect_repo_flavors(target)
    return (
        *(
            f"+{selector}" if not selector.startswith("+") else selector
            for selector in selectors
        ),
        host_platform_selector(),
    )


class ConvertHarnessSliceTests(unittest.TestCase):
    """Full slice: legacy Makefile+Python repo -> convert -> executable wrapper."""

    def test_convert_produces_inventory_and_wrapper_that_executes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "legacy-project"
            target.mkdir()
            # A real, runnable legacy harness: make build/test write sentinels.
            (target / "Makefile").write_text(
                "all:\n"
                "\t@echo built > build.sentinel\n"
                "test:\n"
                "\t@echo tested > test.sentinel\n",
                encoding="utf-8",
            )
            (target / "app.py").write_text("print('hi')\n", encoding="utf-8")

            result = _adapter().initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
                baseline_timeout_seconds=37,
                baseline_diagnostic_chars=4096,
            )

            # The conversion result names the evidence and the wrapper.
            summary = result["harness_inventory"]
            self.assertIsNotNone(summary)
            self.assertEqual(summary["path"], ".literate/harness-inventory.json")
            self.assertIn("build", summary["commands"])
            self.assertEqual(result["harness_wrapper"], HARNESS_WRAPPER_FILENAME)
            self.assertIn(HARNESS_WRAPPER_FILENAME, result["created"])
            self.assertEqual(result["harness_parity"]["state"], "passed")
            self.assertEqual(result["harness_parity"]["phase_count"], 2)
            lift_shift = json.loads(
                (target / ".literate" / "legacy-lift-shift.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(lift_shift["phase_12_parity"]["timeout_seconds"], 37)
            self.assertEqual(
                lift_shift["phase_12_parity"]["diagnostic_limit_chars"],
                4096,
            )
            expected_shims = {
                "components/legacy-project-wrapper/component.md",
                "flavors/legacy-project-shim/flavor.md",
                "flavors/legacy-project-shim/openspec/spec.md",
                "routing/legacy-adoption.json",
                "skills/specification-to-source/legacy-project-shim/SKILL.md",
                "workflows/legacy-adoption/workflow.md",
            }
            self.assertEqual(set(result["shim_authority"]), expected_shims)
            self.assertTrue(all((target / path).is_file() for path in expected_shims))

            # The inventory document is on disk and every command cites evidence.
            document = json.loads(
                (target / ".literate" / "harness-inventory.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(document["schema"], HARNESS_INVENTORY_SCHEMA)
            self.assertEqual(document["commands"]["build"]["evidence"], "Makefile")
            baseline = json.loads(
                (target / ".literate" / "legacy-harness-baseline.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(baseline["state"], "passed")
            self.assertEqual(baseline["timeout_seconds"], 37)
            self.assertEqual(baseline["diagnostic_limit_chars"], 4096)
            self.assertEqual(
                [phase["phase"] for phase in baseline["phases"]],
                ["build", "test"],
            )
            self.assertTrue(
                all(
                    phase["exit_code"] == 0
                    and phase["timeout_seconds"] == 37
                    and phase["diagnostic_limit_chars"] == 4096
                    and phase["stdout_identity"].startswith("sha256:")
                    and phase["stderr_identity"].startswith("sha256:")
                    for phase in baseline["phases"]
                )
            )
            parity = json.loads(
                (target / ".literate" / "legacy-wrapper-parity.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(parity["state"], "passed")
            self.assertEqual(parity["timeout_seconds"], 37)
            self.assertEqual(parity["diagnostic_limit_chars"], 4096)
            self.assertTrue(all(phase["parity"] for phase in parity["phases"]))
            for phase in parity["phases"]:
                self.assertEqual(phase["timeout_seconds"], 37)
                self.assertEqual(phase["diagnostic_limit_chars"], 4096)
                self.assertEqual(phase["source_tree"], phase["baseline_source_tree"])

            # The wrapper is framework-owned at the top level and delegates to the
            # quarantined legacy tree with the detected commands.
            wrapper = (target / HARNESS_WRAPPER_FILENAME).read_text(encoding="utf-8")
            implementation_dir = result["lift_shift"]["implementation_directory"]
            self.assertIn(f"LITAI_LEGACY := {implementation_dir}", wrapper)
            self.assertIn("make -f Makefile", wrapper)
            self.assertTrue(result["lift_shift"]["quarantine_removed"])
            self.assertFalse((target / "_legacy").exists())
            self.assertTrue((target / result["lift_shift"]["adr"]).is_file())
            self.assertEqual(result["native_rewrite"]["state"], "planned")
            self.assertEqual(
                result["native_rewrite"]["next_action"], "boundary-inventory"
            )
            self.assertFalse(
                result["native_rewrite"]["source_to_specification_started"]
            )
            self.assertTrue((target / result["native_rewrite"]["adr"]).is_file())
            self.assertTrue((target / result["native_rewrite"]["roadmap"]).is_file())
            active_work = (target / "docs" / "roadmap" / "active-work.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("ADOPT-002", active_work)
            self.assertIn("boundary inventory", active_work)

            # Smoke: the generated wrapper really builds and tests the legacy tree;
            # commands run inside the lifted first-class implementation directory.
            legacy_root = target / implementation_dir
            for make_target, sentinel in (
                ("build", "build.sentinel"),
                ("test", "test.sentinel"),
            ):
                completed = subprocess.run(
                    ("make", "-f", HARNESS_WRAPPER_FILENAME, make_target),
                    cwd=target,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertTrue((legacy_root / sentinel).is_file())

    def test_refined_convert_installs_independent_retained_components(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            target = base / "legacy-monorepo"
            target.mkdir()
            (target / "Makefile").write_text(
                "all:\n\t@$(MAKE) -C kit\n\t@$(MAKE) -C runtime\n"
                "test:\n\t@$(MAKE) -C kit test\n\t@$(MAKE) -C runtime test\n",
                encoding="utf-8",
            )
            for name in ("kit", "runtime"):
                root = target / name
                root.mkdir()
                (root / "Makefile").write_text(
                    "all:\n\t@true\ntest:\n\t@python3 -m unittest discover -v\n",
                    encoding="utf-8",
                )
                (root / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
                (root / "test_value.py").write_text(
                    "import unittest\nfrom value import VALUE\n"
                    "class T(unittest.TestCase):\n"
                    "    def test_value(self): self.assertEqual(VALUE, 1)\n",
                    encoding="utf-8",
                )
            selection = base / "selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "schema": SELECTION_SCHEMA,
                        "components": [
                            {
                                "name": name,
                                "root": name,
                                "commands": [
                                    {
                                        "id": phase,
                                        "command": command,
                                        "cwd": name,
                                        "evidence": f"{name}/Makefile",
                                    }
                                    for phase, command in (
                                        ("build", "make -f Makefile"),
                                        ("test", "make -f Makefile test"),
                                    )
                                ],
                            }
                            for name in ("kit", "runtime")
                        ],
                        "shared_sources": [
                            {
                                "path": "Makefile",
                                "owner": "kit",
                                "consumers": ["runtime"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            original = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            with patch(
                "literate_ai.adapters.monorepo_components.install_monorepo_components",
                side_effect=MonorepoAdoptionError(
                    "injected_failure", "injected installation failure"
                ),
            ):
                with self.assertRaises(ProjectInitializationError) as raised:
                    _adapter().initialize(
                        target,
                        source_intelligence_provider="none",
                        convert=True,
                        run_baseline=True,
                        root_plan=selection,
                    )
            self.assertEqual(raised.exception.code, "monorepo.injected_failure")
            self.assertEqual(
                {
                    path.relative_to(target).as_posix(): path.read_bytes()
                    for path in target.rglob("*")
                    if path.is_file()
                },
                original,
            )

            result = _adapter().initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
                run_baseline=True,
                root_plan=selection,
            )

            installed = check_installed_monorepo_components(target)
            self.assertEqual(installed["components"], ["kit", "runtime"])
            self.assertEqual(installed["stage"], "retained")
            self.assertFalse(installed["source_copied"])
            self.assertEqual(set(installed["boundary_transfer"]), {"kit", "runtime"})
            self.assertIn("monorepo_components", result)
            retained = Path(result["lift_shift"]["implementation_directory"])
            self.assertEqual(installed["source_root"], retained.as_posix())
            self.assertTrue((target / retained / "kit" / "value.py").is_file())
            validation = _adapter()._validation.validate(
                target,
                require_authority_review=True,
                include_test_receipt=False,
                synchronize_source_intelligence=False,
            )
            self.assertEqual(
                validation["monorepo_components"]["identity"], installed["identity"]
            )

            (target / retained / "kit" / "new.py").write_text(
                "NEW = True\n", encoding="utf-8"
            )
            refresh_plan = plan_retained_scope_refresh(target)
            self.assertTrue(refresh_plan["monorepo_refresh_required"])
            with self.assertRaisesRegex(Exception, "run-component-baselines"):
                apply_retained_scope_refresh(
                    target,
                    expected_plan_identity=refresh_plan["plan_identity"],
                    acknowledge=True,
                )
            refreshed = apply_retained_scope_refresh(
                target,
                expected_plan_identity=refresh_plan["plan_identity"],
                acknowledge=True,
                run_component_baselines=True,
            )["monorepo_refresh"]
            self.assertEqual(refreshed["executed_components"], ["kit"])
            self.assertEqual(refreshed["reused_components"], ["runtime"])
            self.assertEqual(
                check_installed_monorepo_components(target)["state"], "current"
            )
            self.assertEqual(
                _adapter()._validation.validate(
                    target,
                    require_authority_review=True,
                    include_test_receipt=False,
                    synchronize_source_intelligence=False,
                )["monorepo_components"]["state"],
                "current",
            )

            receipt = (
                target / ".literate" / "monorepo-components" / "receipts" / "kit.json"
            )
            receipt.write_bytes(receipt.read_bytes() + b" ")
            with self.assertRaises(ProjectValidationError) as raised:
                _adapter()._validation.validate(
                    target,
                    require_authority_review=True,
                    include_test_receipt=False,
                    synchronize_source_intelligence=False,
                )
            self.assertEqual(raised.exception.code, "monorepo.receipt_changed")

    def test_convert_python_make_repo_validates_and_admits_derived_spec(
        self,
    ) -> None:
        """INIT-003: convert a synthetic Python+Make tree, then project-validate
        and parse a spec derived from observed behavior without implementation
        paths (skills/agent/convert-project spec-derivation principle)."""

        framework = Path(__file__).resolve().parents[2]
        convert_skill = framework / "skills" / "agent" / "convert-project" / "SKILL.md"
        self.assertTrue(convert_skill.is_file())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "python-make-app"
            target.mkdir()
            (target / "Makefile").write_text(
                "all:\n"
                "\t@echo built > build.sentinel\n"
                "test:\n"
                "\t@echo tested > test.sentinel\n",
                encoding="utf-8",
            )
            (target / "app.py").write_text("print('hi')\n", encoding="utf-8")
            _adapter().initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )
            validation = validate_project(
                target,
                require_authority_review=True,
                include_test_receipt=False,
                synchronize_source_intelligence=False,
            )
            self.assertEqual(validation["authority_review"]["state"], "current")
            onboarding = (target / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("conversion report as the exact follow-up", onboarding)
            self.assertIn("framework-only conversion skill", onboarding)
            self.assertFalse((target / "skills" / "agent" / "convert-project").exists())
            derived = """---
namespace: legacy-adoption
version: 1.0.0
display_name: Greeting Emitter
profiles:
  - application
sample: false
provides:
  - name: legacy.pipeline
    version: 1.0.0
requires: []
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/legacy-project-shim/SKILL.md
workflow_definition: workflows/legacy-adoption/workflow.md
routing_policy: routing/legacy-adoption.json
flavor_slots:
  - slot_id: build-system
    axis: build.system
    cardinality: exactly-one
    capability_contract: legacy.pipeline
entrypoints:
  - name: run
    kind: portable-application
    path: run
acceptance_contracts: []
source_dependencies: []
---
# Greeting Emitter

The application writes one greeting line to standard output when invoked with
no arguments. It does not read files, environment variables, or network
services.

## Application contract

| Concern | Decision |
| --- | --- |
| Kind | portable application |
| Output | exactly one line of greeting text |
"""
            self.assertNotIn("app.py", derived)
            self.assertNotIn("print(", derived)
            authoring = parse_component_markdown(
                target / "components" / "greeting-emitter" / "component.md",
                derived,
                project_root=target,
            )
            self.assertEqual(authoring.coordinate.name, "greeting-emitter")

    def test_failing_legacy_gate_restores_exact_pre_conversion_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "failing-project"
            target.mkdir()
            original = {
                "Makefile": "all:\n\t@exit 7\ntest:\n\t@exit 0\n",
                "src/app.py": "print('legacy')\n",
                "docs/guide.md": "# Legacy guide\n",
            }
            for relative, content in original.items():
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            with self.assertRaises(ProjectInitializationError) as raised:
                _adapter().initialize(
                    target,
                    source_intelligence_provider="none",
                    convert=True,
                )

            self.assertEqual(
                raised.exception.code, "project.convert_legacy_gate_failed"
            )
            self.assertIn("rolled back", raised.exception.message)
            self.assertIn("make -f Makefile", raised.exception.message)
            self.assertIn("status 2", raised.exception.message)
            self.assertIn("Error 7", raised.exception.message)
            self.assertFalse((target / "literate.project.json").exists())
            self.assertFalse((target / ".literate").exists())
            self.assertFalse((target / "_legacy").exists())
            self.assertFalse((target / HARNESS_WRAPPER_FILENAME).exists())
            restored = {
                path.relative_to(target).as_posix(): path.read_text(encoding="utf-8")
                for path in target.rglob("*")
                if path.is_file()
            }
            self.assertEqual(restored, original)


class DetectionToSelectorSliceTests(unittest.TestCase):
    """Detection feeds init: a CMake repo must convert with build-cmake selected."""

    def test_cmake_repository_converts_with_cmake_flavor_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "cmake-project"
            target.mkdir()
            (target / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.15)\n"
                "project(demo LANGUAGES NONE)\n"
                "enable_testing()\n"
                "add_test(NAME demo COMMAND ${CMAKE_COMMAND} -E true)\n",
                encoding="utf-8",
            )
            (target / "main.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            # CMake also generates Makefiles in practice; detection must not
            # mistake one for a plain Make project.
            (target / "Makefile").write_text("# generated\n", encoding="utf-8")

            selectors = detect_repo_flavors(target)
            self.assertIn("flavor://literate-ai/build-cmake", selectors)
            self.assertNotIn("flavor://literate-ai/build-make", selectors)

            # Multi-config Windows generators require an explicit configuration
            # when CTest locates the configured test executable.

            _adapter().initialize(
                target,
                flavor_selectors=(
                    *(
                        f"+{selector}" if not selector.startswith("+") else selector
                        for selector in selectors
                    ),
                    host_platform_selector(),
                ),
                source_intelligence_provider="none",
                convert=True,
            )

            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "+flavor://literate-ai/build-cmake",
                manifest["default_flavor_selectors"],
            )
            self.assertNotIn(
                "+flavor://literate-ai/build-make",
                manifest["default_flavor_selectors"],
            )


class RepoManConvertSliceTests(unittest.TestCase):
    """repo.sh monorepos plan and wrap without a local make ci gate."""

    def test_repo_man_convert_emits_nested_wrapper_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "kitish"
            target.mkdir()
            _write_repo_man_fixture(target)

            result = _adapter().initialize(
                target,
                flavor_selectors=_signed_selectors(target),
                source_intelligence_provider="none",
                convert=True,
            )

            self.assertTrue((target / "literate.project.json").is_file())
            self.assertEqual(result["harness_baseline"]["state"], "skipped")
            self.assertEqual(result["harness_parity"]["state"], "skipped")
            wrapper = (target / HARNESS_WRAPPER_FILENAME).read_text(encoding="utf-8")
            implementation_dir = result["lift_shift"]["implementation_directory"]
            self.assertIn(f"LITAI_LEGACY := {implementation_dir}", wrapper)
            self.assertIn("build.kit:", wrapper)
            self.assertIn("build.rendering:", wrapper)
            self.assertIn(
                "cd $(LITAI_LEGACY)/kit && ./repo.sh build --release", wrapper
            )
            self.assertIn(
                "cd $(LITAI_LEGACY)/rendering && ./repo.sh build --release", wrapper
            )
            self.assertIn("cd $(LITAI_LEGACY) && ./repo.sh build --release", wrapper)
            inventory = json.loads(
                (target / ".literate" / "harness-inventory.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(inventory["ci"]["class"], "remote-configured")
            self.assertEqual(
                [stage["id"] for stage in inventory["stages"]],
                ["build", "build.kit", "build.rendering"],
            )
            manifest = json.loads(
                (target / "literate.project.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "+flavor://literate-ai/build-repo-man",
                manifest["default_flavor_selectors"],
            )
            self.assertIn(
                "+flavor://literate-ai/lang-cpp",
                manifest["default_flavor_selectors"],
            )
            self.assertNotIn(
                "+flavor://literate-ai/lang-python",
                manifest["default_flavor_selectors"],
            )
            self.assertEqual(result["detected_languages"], ["cpp", "python"])
            self.assertEqual(result["lift_shift"]["state"], "passed")
            completed = subprocess.run(
                ("make", "-f", HARNESS_WRAPPER_FILENAME, "build.kit"),
                cwd=target,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("kit build", completed.stdout)

    def test_wrapper_recipe_rejects_cwd_escape(self) -> None:
        inventory = {
            "commands": {
                "build": {
                    "command": "true",
                    "evidence": "Makefile",
                    "cwd": "../../etc",
                }
            },
            "stages": [
                {
                    "id": "build",
                    "command": "true",
                    "evidence": "Makefile",
                    "cwd": "../../etc",
                    "cost": "local-cheap",
                }
            ],
        }
        wrapper = render_harness_wrapper(inventory, legacy_directory="_legacy/x")
        self.assertIn("$(error harness cwd escapes the legacy tree", wrapper)
        self.assertNotIn("../../etc", wrapper)
        self.assertNotIn("cd $(LITAI_LEGACY)/../", wrapper)

    def test_harness_diagnostic_excerpt_redacts_environment_secrets(self) -> None:
        secret = "conversion-secret-value"
        program = (
            "import os, sys; "
            "sys.stdout.buffer.write(os.environ['CONVERT_API_TOKEN'].encode() + b'\\n')"
        )
        command = (
            subprocess.list2cmdline([sys.executable, "-c", program])
            if os.name == "nt"
            else shlex.join([sys.executable, "-c", program])
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"CONVERT_API_TOKEN": secret}, clear=False),
        ):
            result = _run_harness_command(
                "build",
                {"command": command, "evidence": "synthetic"},
                legacy_root=Path(directory),
                timeout_seconds=30,
            )

        self.assertNotIn(secret, result["stdout_excerpt"])
        self.assertEqual(result["stdout_excerpt"], "<redacted>")
        self.assertEqual(
            result["stdout_identity"],
            "sha256:" + hashlib.sha256(f"{secret}\n".encode()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
