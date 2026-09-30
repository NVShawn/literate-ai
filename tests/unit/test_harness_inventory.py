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
    HARNESS_DIAGNOSTIC_CHARS,
    HARNESS_INVENTORY_SCHEMA,
    HARNESS_WRAPPER_FILENAME,
    HarnessBaselineError,
    _gate_failure_message,
    _run_harness_command,
    _sanitized_git_environment,
    execute_harness_baseline,
    execute_harness_wrapper_parity,
    execute_retained_harness,
    inspect_harness,
    legacy_shim_authority,
    makefile_recipe_is_environment_bound,
    render_harness_wrapper,
    stages_requiring_execution,
    validate_harness_diagnostic_limit,
)
from literate_ai.adapters.harness_tree import (
    capture_retained_source_scope,
    copy_retained_source_tree,
    observe_retained_tree,
)
from literate_ai.adapters.harness_workspace import resolve_harness_workspace_links
from literate_ai.adapters.monorepo_adoption import (
    SELECTION_SCHEMA,
    MonorepoAdoptionError,
)
from literate_ai.adapters.monorepo_components import (
    check_installed_monorepo_components,
)
from literate_ai.adapters.project_initialization import (
    CONVERT_PLAN_SCHEMA,
    ProjectInitializationError,
    detect_repo_flavors,
    detected_language_flavors,
    host_platform_selector,
    plan_convert,
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
from tests.unit.root_parent_adapter import (
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


def _configure_git_fixture(root: Path) -> None:
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    subprocess.run(
        ("git", "-C", str(root), "config", "user.name", "Fixture"), check=True
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(root),
            "config",
            "user.email",
            "fixture@example.test",
        ),
        check=True,
    )


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

    def test_convert_projects_explicit_source_linked_sibling_topology(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve() / "workspace"
            sibling = workspace / "omniverse-kit" / "kit" / "_build" / "release"
            sibling.mkdir(parents=True)
            (sibling / "sentinel").write_text("ready\n", encoding="utf-8")
            check = (
                "from pathlib import Path; "
                "p=Path('tools/deps/kit-sdk.packman.xml'); "
                "q=(p.parent/p.read_text().strip()).resolve(); "
                "assert q.is_file(), q; print(q)"
            )

            def write_project(name: str) -> Path:
                target = workspace / name
                (target / "tools" / "deps").mkdir(parents=True)
                (target / "tools" / "deps" / "kit-sdk.packman.xml").write_text(
                    "../../../omniverse-kit/kit/_build/release/sentinel\n",
                    encoding="utf-8",
                )
                (target / "Makefile").write_text(
                    f'all:\n\t@"$(PYTHON_FOR_TEST)" -c "{check}"\n'
                    f'test:\n\t@"$(PYTHON_FOR_TEST)" -c "{check}; '
                    "print('Ran 1 test'); "
                    "print('OK')\"\n",
                    encoding="utf-8",
                )
                return target

            unmapped = write_project("unmapped")
            direct = subprocess.run(
                ("make", "-s"),
                cwd=unmapped,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHON_FOR_TEST": sys.executable},
            )
            self.assertEqual(direct.returncode, 0, direct.stderr)
            with (
                patch.dict(
                    os.environ, {"PYTHON_FOR_TEST": sys.executable}, clear=False
                ),
                self.assertRaisesRegex(
                    ProjectInitializationError, "legacy build gate failed"
                ),
            ):
                _adapter().initialize(
                    unmapped,
                    flavor_selectors=_signed_selectors(unmapped),
                    source_intelligence_provider="none",
                    convert=True,
                )
            self.assertTrue((unmapped / "Makefile").is_file())
            self.assertFalse((unmapped / "_legacy").exists())

            mapped = write_project("mapped")
            links = resolve_harness_workspace_links(
                (f"omniverse-kit={workspace / 'omniverse-kit'}",),
                base=workspace,
                project_root=mapped,
            )
            with patch.dict(
                os.environ, {"PYTHON_FOR_TEST": sys.executable}, clear=False
            ):
                result = _adapter().initialize(
                    mapped,
                    flavor_selectors=_signed_selectors(mapped),
                    source_intelligence_provider="none",
                    convert=True,
                    harness_workspace_links=links,
                )

            self.assertEqual(result["harness_baseline"]["state"], "passed")
            self.assertEqual(result["harness_parity"]["state"], "passed")
            self.assertEqual(
                result["harness_inventory"]["workspace_link_destinations"],
                ["omniverse-kit"],
            )
            inventory = json.loads(
                (mapped / ".literate/harness-inventory.json").read_text(
                    encoding="utf-8"
                )
            )
            serialized = json.dumps(inventory, sort_keys=True)
            self.assertEqual(
                inventory["workspace_links"]["destinations"], ["omniverse-kit"]
            )
            self.assertNotIn(str(workspace), serialized)
            lift_shift = json.loads(
                (mapped / ".literate/legacy-lift-shift.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(lift_shift["phase_12_parity"]["state"], "passed")
            self.assertEqual(list(mapped.glob(".literate-ai-convert-*")), [])
            direct_wrapper = subprocess.run(
                ("make", "-f", HARNESS_WRAPPER_FILENAME, "build"),
                cwd=mapped,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(direct_wrapper.returncode, 0)
            self.assertIn(
                "requires external workspace links",
                direct_wrapper.stderr + direct_wrapper.stdout,
            )

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

    def test_makefile_without_test_target_does_not_invent_a_make_test_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            (root / "tests").mkdir()
            document = inspect_harness(root)
            self.assertEqual(
                document["commands"]["build"]["command"], "make -f Makefile"
            )
            self.assertNotIn("test", document["commands"])
            self.assertTrue(
                any(
                    finding["detector_id"] == "test-runner.unproven"
                    for finding in document["findings"]
                )
            )

    def test_python_poe_task_preempts_unproven_pytest_convention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[project]\nname = 'converter'\nversion = '1.0.0'\n"
                "[dependency-groups]\ndev = ['poethepoet>=0.34']\n"
                "[tool.uv]\npackage = true\n"
                "[tool.poe.tasks.test]\n"
                "cmd = 'python -m unittest discover -v -s ./tests'\n",
                encoding="utf-8",
            )
            tests = root / "tests"
            tests.mkdir()
            (tests / "testCamelCase.py").write_text(
                "import unittest\n"
                "class TestExample(unittest.TestCase):\n"
                "    def test_value(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )

            document = inspect_harness(root)

            self.assertEqual(
                document["commands"]["test"]["command"],
                "uv run --group dev poe test",
            )
            self.assertEqual(document["commands"]["test"]["evidence"], "pyproject.toml")

    @unittest.skipIf(os.name == "nt", "POSIX aggregate-script fixture")
    def test_nested_aggregate_test_script_preserves_its_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[project]\nname = 'mixed'\nversion = '1.0.0'\n",
                encoding="utf-8",
            )
            script = root / "tests" / "docs" / "run_tests.sh"
            _write_executable(script, "#!/bin/sh\nexit 0\n")

            document = inspect_harness(root)

            self.assertEqual(document["commands"]["test"]["command"], "./run_tests.sh")
            self.assertEqual(document["commands"]["test"]["cwd"], "tests/docs")
            self.assertEqual(document["commands"]["test"]["cost"], "host-heavy")

    def test_pytest_requires_configuration_and_collectable_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[project]\nname = 'pytest-demo'\nversion = '1.0.0'\n"
                "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
                encoding="utf-8",
            )
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_demo.py").write_text(
                "def test_value():\n    assert True\n", encoding="utf-8"
            )

            document = inspect_harness(root)

            self.assertEqual(
                document["commands"]["test"]["command"], "python -m pytest tests"
            )

    def test_pytest_testpaths_cannot_inject_a_shell_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[project]\nname = 'pytest-demo'\nversion = '1.0.0'\n"
                "[tool.pytest.ini_options]\n"
                "testpaths = ['tests; touch escaped']\n",
                encoding="utf-8",
            )
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_demo.py").write_text(
                "def test_value():\n    assert True\n", encoding="utf-8"
            )

            document = inspect_harness(root)

            self.assertEqual(
                document["commands"]["test"]["command"], "python -m pytest tests"
            )

    def test_pytest_testpaths_cannot_escape_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / "pyproject.toml").write_text(
                "[project]\nname = 'pytest-demo'\nversion = '1.0.0'\n"
                "[tool.pytest.ini_options]\n"
                "testpaths = ['tests/../../outside']\n",
                encoding="utf-8",
            )
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_demo.py").write_text(
                "def test_value():\n    assert True\n", encoding="utf-8"
            )

            document = inspect_harness(root)

            self.assertEqual(
                document["commands"]["test"]["command"], "python -m pytest tests"
            )

    def test_pytest_ini_is_the_recorded_runner_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").write_text(
                "[project]\nname = 'pytest-demo'\nversion = '1.0.0'\n",
                encoding="utf-8",
            )
            (root / "pytest.ini").write_text(
                "[pytest]\ntestpaths = tests\n", encoding="utf-8"
            )
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_demo.py").write_text(
                "def test_value():\n    assert True\n", encoding="utf-8"
            )

            document = inspect_harness(root)

            self.assertEqual(document["commands"]["test"]["evidence"], "pytest.ini")

    def test_successful_test_gate_with_zero_collection_fails(self) -> None:
        inventory = {
            "findings": [],
            "commands": {
                "test": {
                    "command": (
                        "python3 -c \"print('collected 0 items'); "
                        "print('no tests ran')\""
                    ),
                    "evidence": "pyproject.toml",
                    "cost": "local-cheap",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HarnessBaselineError) as raised:
                execute_harness_baseline(inventory, legacy_root=Path(directory))
        self.assertEqual(raised.exception.code, "project.convert_empty_test_suite")

    def test_git_ignored_outputs_are_accounted_without_hashing_their_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            _configure_git_fixture(root)
            (root / ".gitignore").write_text("huge-output/\n", encoding="utf-8")
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(root), "add", "."), check=True)
            subprocess.run(
                ("git", "-C", str(root), "commit", "-qm", "fixture"),
                check=True,
            )
            ignored = root / "huge-output" / "cache.bin"
            ignored.parent.mkdir()
            ignored.write_bytes(b"x" * 1024 * 1024)

            scope = capture_retained_source_scope(root)
            destination = Path(directory) / "copy"
            original_read_bytes = Path.read_bytes

            def guarded_read_bytes(path: Path) -> bytes:
                if "huge-output" in path.parts:
                    raise AssertionError("ignored output was hashed")
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", guarded_read_bytes):
                copy_retained_source_tree(root, destination, scope)
                before = observe_retained_tree(destination, scope)
                cache = destination / "target" / "debug" / "cache.bin"
                cache.parent.mkdir(parents=True)
                cache.write_bytes(b"new output")
                after = observe_retained_tree(destination, scope)

            self.assertNotIn("huge-output/cache.bin", scope["paths"])
            self.assertFalse((destination / "huge-output").exists())
            self.assertEqual(before["source_tree"], after["source_tree"])
            self.assertNotEqual(before["tree"], after["tree"])
            self.assertTrue(
                any(
                    item["class"] == "build-output" and item["present_root_count"] == 1
                    for item in after["excluded"]
                )
            )

            (destination / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            mutated = observe_retained_tree(destination, scope)
            self.assertNotEqual(after["source_tree"], mutated["source_tree"])

    def test_initialized_gitlink_contents_are_captured_and_copied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            child_source = Path(directory) / "child-source"
            root.mkdir()
            child_source.mkdir()
            for repository in (root, child_source):
                _configure_git_fixture(repository)
            (child_source / ".gitignore").write_text("ignored/\n", encoding="utf-8")
            (child_source / "tracked.rs").write_text(
                "pub fn nested() {}\n", encoding="utf-8"
            )
            subprocess.run(("git", "-C", str(child_source), "add", "."), check=True)
            subprocess.run(
                ("git", "-C", str(child_source), "commit", "-qm", "child"),
                check=True,
            )
            (root / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(root), "add", "."), check=True)
            subprocess.run(
                ("git", "-C", str(root), "commit", "-qm", "root"), check=True
            )
            subprocess.run(
                (
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "-C",
                    str(root),
                    "submodule",
                    "add",
                    "-q",
                    str(child_source),
                    "deps/lib",
                ),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(root), "commit", "-qam", "gitlink"), check=True
            )
            child = root / "deps/lib"
            (child / "visible.local").write_text("captured\n", encoding="utf-8")
            ignored = child / "ignored/cache.bin"
            ignored.parent.mkdir()
            ignored.write_bytes(b"not source")

            scope = capture_retained_source_scope(root)
            destination = Path(directory) / "copy"
            copy_retained_source_tree(root, destination, scope)
            observed = observe_retained_tree(destination, scope)

            self.assertIn("deps/lib/tracked.rs", scope["paths"])
            self.assertIn("deps/lib/visible.local", scope["paths"])
            self.assertNotIn("deps/lib/.git", scope["paths"])
            self.assertNotIn("deps/lib/ignored/cache.bin", scope["paths"])
            self.assertIn("deps/lib/ignored", scope["excluded_roots"])
            self.assertEqual(
                (destination / "deps/lib/tracked.rs").read_text(),
                "pub fn nested() {}\n",
            )
            self.assertEqual(observed["source_tree"]["missing_count"], 0)

    def test_uninitialized_gitlink_remains_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            child_source = Path(directory) / "child-source"
            root.mkdir()
            child_source.mkdir()
            for repository in (root, child_source):
                _configure_git_fixture(repository)
            (child_source / "tracked.rs").write_text("nested\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(child_source), "add", "."), check=True)
            subprocess.run(
                ("git", "-C", str(child_source), "commit", "-qm", "child"),
                check=True,
            )
            (root / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
            subprocess.run(("git", "-C", str(root), "add", "."), check=True)
            subprocess.run(
                ("git", "-C", str(root), "commit", "-qm", "root"), check=True
            )
            subprocess.run(
                (
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "-C",
                    str(root),
                    "submodule",
                    "add",
                    "-q",
                    str(child_source),
                    "deps/lib",
                ),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(root), "commit", "-qam", "gitlink"), check=True
            )
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(root),
                    "submodule",
                    "deinit",
                    "-q",
                    "--force",
                    "deps/lib",
                ),
                check=True,
            )

            scope = capture_retained_source_scope(root)

            self.assertFalse(
                any(path.startswith("deps/lib/") for path in scope["paths"])
            )

    def test_repo_runtime_state_does_not_hide_tracked_repo_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            _configure_git_fixture(root)
            (root / ".gitignore").write_text("_*/\n", encoding="utf-8")
            policy = root / "_repo" / "policy.toml"
            policy.parent.mkdir()
            policy.write_text('mode = "authored"\n', encoding="utf-8")
            subprocess.run(("git", "-C", str(root), "add", ".gitignore"), check=True)
            subprocess.run(
                ("git", "-C", str(root), "add", "-f", "_repo/policy.toml"),
                check=True,
            )
            subprocess.run(
                ("git", "-C", str(root), "commit", "-qm", "fixture"),
                check=True,
            )

            scope = capture_retained_source_scope(root)
            destination = Path(directory) / "copy"
            copy_retained_source_tree(root, destination, scope)
            before = observe_retained_tree(destination, scope)
            (destination / "_repo" / "repo.log").write_text(
                "generated runtime log\n", encoding="utf-8"
            )
            with_log = observe_retained_tree(destination, scope)

            self.assertEqual(before["source_tree"], with_log["source_tree"])
            self.assertTrue(
                any(
                    item["class"] == "build-output" and item["present_root_count"] == 1
                    for item in with_log["excluded"]
                )
            )
            unexpected = destination / "unexpected.generated"
            unexpected.write_text("not declared runtime state\n", encoding="utf-8")
            with_unexpected = observe_retained_tree(destination, scope)
            self.assertEqual(with_log["source_tree"], with_unexpected["source_tree"])
            self.assertNotEqual(with_log["tree"], with_unexpected["tree"])
            unexpected.unlink()
            (destination / "_repo" / "policy.toml").write_text(
                'mode = "mutated"\n', encoding="utf-8"
            )
            mutated = observe_retained_tree(destination, scope)
            self.assertNotEqual(with_log["source_tree"], mutated["source_tree"])

    @unittest.skipUnless(os.name == "posix", "shell wrapper execution requires POSIX")
    def test_repo_runtime_log_churn_does_not_fail_wrapper_parity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "repo-man-runtime-log"
            target.mkdir()
            _configure_git_fixture(target)
            (target / ".gitignore").write_text("_*/\n", encoding="utf-8")
            _write_executable(
                target / "repo.sh",
                "#!/bin/sh\n"
                "set -eu\n"
                'test "${1:-}" = "build"\n'
                'test "${2:-}" = "--release"\n'
                "mkdir -p _repo\n"
                'printf "%s\\n" "$$" > _repo/repo.log\n'
                'printf "stable\\n" > generated.lock\n',
            )
            (target / "repo.toml").write_text(
                'name = "runtime-log"\n', encoding="utf-8"
            )
            subprocess.run(("git", "-C", str(target), "add", "."), check=True)
            subprocess.run(
                ("git", "-C", str(target), "commit", "-qm", "fixture"),
                check=True,
            )

            inventory = inspect_harness(target)
            legacy_root = Path(directory) / "legacy"
            copy_retained_source_tree(target, legacy_root, inventory["source_scope"])
            baseline = execute_harness_baseline(
                inventory, legacy_root=legacy_root, run_baseline=True
            )
            (target / HARNESS_WRAPPER_FILENAME).write_text(
                render_harness_wrapper(
                    inventory, legacy_directory=".", legacy_root=target
                ),
                encoding="utf-8",
            )
            parity = execute_harness_wrapper_parity(
                inventory,
                baseline,
                project_root=target,
                legacy_root=legacy_root,
            )

            self.assertEqual(parity["state"], "passed")
            self.assertEqual(parity["phase_count"], 1)
            phase = parity["phases"][0]
            self.assertTrue(phase["parity"])
            self.assertTrue(phase["artifact_tree_equal"])
            self.assertTrue(
                any(
                    item["class"] == "build-output" and item["present_root_count"] == 1
                    for item in phase["excluded"]
                )
            )

    @unittest.skipUnless(os.name == "posix", "shell wrapper execution requires POSIX")
    def test_generated_root_files_are_artifacts_not_source_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "generated-root-output"
            target.mkdir()
            _configure_git_fixture(target)
            _write_executable(
                target / "repo.sh",
                "#!/bin/sh\n"
                "set -eu\n"
                'test "${1:-}" = "build"\n'
                'test "${2:-}" = "--release"\n'
                'printf "generated by %s\\n" "$$" > "cache-$$.lock"\n',
            )
            (target / "repo.toml").write_text(
                'name = "generated-root-output"\n', encoding="utf-8"
            )
            subprocess.run(("git", "-C", str(target), "add", "."), check=True)
            subprocess.run(
                ("git", "-C", str(target), "commit", "-qm", "fixture"),
                check=True,
            )

            inventory = inspect_harness(target)
            legacy_root = Path(directory) / "legacy"
            copy_retained_source_tree(target, legacy_root, inventory["source_scope"])
            baseline = execute_harness_baseline(
                inventory, legacy_root=legacy_root, run_baseline=True
            )
            (target / HARNESS_WRAPPER_FILENAME).write_text(
                render_harness_wrapper(
                    inventory, legacy_directory=".", legacy_root=target
                ),
                encoding="utf-8",
            )
            parity = execute_harness_wrapper_parity(
                inventory,
                baseline,
                project_root=target,
                legacy_root=legacy_root,
            )

            self.assertEqual(parity["state"], "passed")
            phase = parity["phases"][0]
            self.assertTrue(phase["parity"])
            self.assertFalse(phase["artifact_tree_equal"])
            self.assertEqual(phase["source_tree"], baseline["phases"][0]["source_tree"])

    def test_filesystem_fallback_prunes_git_and_legacy_build_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / ".git" / "objects").mkdir(parents=True)
            (root / ".git" / "objects" / "large").write_bytes(b"x" * 1024)
            (root / "_build" / "cache").mkdir(parents=True)
            (root / "_build" / "cache" / "large").write_bytes(b"x" * 1024)

            with patch(
                "literate_ai.adapters.harness_tree.shutil.which", return_value=None
            ):
                scope = capture_retained_source_scope(root)

            self.assertEqual(scope["paths"], ["app.py"])
            self.assertEqual(scope["excluded_roots"], [".git", "_build"])

    def test_failing_make_test_gate_names_the_command_and_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "make-test-miss"
            target.mkdir()
            (target / "Makefile").write_text(
                "all:\n"
                "\t@true\n"
                "test:\n"
                "\t@printf '%s\\n' 'recipe failed: missing fixture' >&2\n"
                "\t@exit 2\n",
                encoding="utf-8",
            )
            with self.assertRaises(ProjectInitializationError) as raised:
                _adapter().initialize(
                    target,
                    source_intelligence_provider="none",
                    convert=True,
                )
            self.assertEqual(
                raised.exception.code, "project.convert_legacy_gate_failed"
            )
            self.assertIn("make -f Makefile test", raised.exception.message)
            self.assertIn("status 2", raised.exception.message)
            self.assertIn("recipe failed: missing fixture", raised.exception.message)
            self.assertIn("rolled back", raised.exception.message)

    def test_convert_omits_local_venv_and_names_environment_bound_recipes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv-bound"
            target.mkdir()
            venv_python = target / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text(
                "#!/original/checkout/.venv/bin/python\n", encoding="utf-8"
            )
            venv_python.chmod(venv_python.stat().st_mode | stat.S_IXUSR)
            pytest = target / ".venv" / "bin" / "pytest"
            pytest.write_text(
                "#!/original/checkout/.venv/bin/python\nimport sys\nsys.exit(0)\n",
                encoding="utf-8",
            )
            pytest.chmod(pytest.stat().st_mode | stat.S_IXUSR)
            (target / "Makefile").write_text(
                "all:\n\t@true\ntest:\n\t.venv/bin/pytest tests\n",
                encoding="utf-8",
            )
            with self.assertRaises(ProjectInitializationError) as raised:
                _adapter().initialize(
                    target,
                    source_intelligence_provider="none",
                    convert=True,
                )
            self.assertEqual(
                raised.exception.code, "project.convert_environment_bound_command"
            )
            self.assertIn("make -f Makefile test", raised.exception.message)
            self.assertIn(".venv/", raised.exception.message)
            self.assertIn("rolled back", raised.exception.message)
            self.assertTrue((target / ".venv" / "bin" / "pytest").is_file())
            self.assertFalse((target / "literate.project.json").exists())

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv-ignored"
            target.mkdir()
            venv_python = target / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("#!/unused\n", encoding="utf-8")
            (target / "Makefile").write_text(
                "all:\n\t@true\ntest:\n\t@true\n", encoding="utf-8"
            )
            result = _adapter().initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )
            self.assertTrue((target / "literate.project.json").is_file())
            self.assertIsNotNone(result)

    def test_convert_names_environment_bound_recipe_reached_through_prerequisite(
        self,
    ) -> None:
        """Aggregate targets that delegate the environment-bound command to a
        prerequisite must still fail closed with the typed diagnosis rather than
        a copied-interpreter Error 127 (regression for issue #187)."""

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv-bound-prereq"
            target.mkdir()
            pytest = target / ".venv" / "bin" / "pytest"
            pytest.parent.mkdir(parents=True)
            pytest.write_text(
                "#!/original/checkout/.venv/bin/python\nimport sys\nsys.exit(0)\n",
                encoding="utf-8",
            )
            pytest.chmod(pytest.stat().st_mode | stat.S_IXUSR)
            # ``test`` is an aggregate target: its own recipe is clean, but it
            # delegates the environment-bound command to a prerequisite goal.
            (target / "Makefile").write_text(
                "all:\n\t@true\n"
                "test: test-python\n\t@echo aggregate\n"
                "test-python:\n\t.venv/bin/pytest tests\n",
                encoding="utf-8",
            )
            with self.assertRaises(ProjectInitializationError) as raised:
                _adapter().initialize(
                    target,
                    source_intelligence_provider="none",
                    convert=True,
                )
            self.assertEqual(
                raised.exception.code, "project.convert_environment_bound_command"
            )
            self.assertIn("make -f Makefile test", raised.exception.message)
            self.assertIn(".venv/", raised.exception.message)
            self.assertIn("rolled back", raised.exception.message)
            self.assertTrue((target / ".venv" / "bin" / "pytest").is_file())
            self.assertFalse((target / "literate.project.json").exists())

    def test_makefile_recipe_environment_bound_follows_prerequisite_closure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bound = root / "Makefile-bound"
            bound.write_text(
                "test: test-python test-node\n\t@echo aggregate\n"
                "test-python:\n\t.venv/bin/pytest -q\n"
                "test-node:\n\tnpm run test:node\n",
                encoding="utf-8",
            )
            self.assertTrue(makefile_recipe_is_environment_bound(bound, goal="test"))

            clean = root / "Makefile-clean"
            clean.write_text(
                "test: test-python\n\t@echo aggregate\ntest-python:\n\tpytest -q\n",
                encoding="utf-8",
            )
            self.assertFalse(makefile_recipe_is_environment_bound(clean, goal="test"))

            cyclic = root / "Makefile-cyclic"
            cyclic.write_text("a: b\nb: a\na:\n\t@true\n", encoding="utf-8")
            self.assertFalse(makefile_recipe_is_environment_bound(cyclic, goal="a"))

            aliased = root / "Makefile-aliased"
            aliased.write_text(
                "VENV := .venv/bin\ntest: FOO=.venv/bin/pytest\ntest:\n\t@true\n",
                encoding="utf-8",
            )
            self.assertFalse(makefile_recipe_is_environment_bound(aliased, goal="test"))

    def test_ci_configuration_without_executable_ci_gate_converts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ci-project"
            target.mkdir()
            (target / "Makefile").write_text(
                "all:\n\t@true\ntest:\n\t@true\n", encoding="utf-8"
            )
            workflow = target / ".github" / "workflows"
            workflow.mkdir(parents=True)
            (workflow / "ci.yml").write_text(
                "jobs:\n  test:\n    runs-on: ubuntu-latest\n",
                encoding="utf-8",
            )

            result = _adapter().initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )

            self.assertTrue((target / "literate.project.json").is_file())
            self.assertEqual(result["harness_baseline"]["state"], "passed")
            inventory = json.loads(
                (target / ".literate" / "harness-inventory.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(inventory["ci"]["class"], "remote-configured")
            baseline = json.loads(
                (target / ".literate" / "legacy-harness-baseline.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(baseline["ci"]["state"], "recorded")
            self.assertNotIn("ci", [phase["phase"] for phase in baseline["phases"]])

    def test_wrapper_fails_closed_for_undetected_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "bare-project"
            target.mkdir()
            # No detectable build system at all.
            (target / "notes.txt").write_text("nothing here\n", encoding="utf-8")

            _adapter().initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )

            wrapper = (target / HARNESS_WRAPPER_FILENAME).read_text(encoding="utf-8")
            self.assertIn("$(error no build command", wrapper)

            completed = subprocess.run(
                ("make", "-f", HARNESS_WRAPPER_FILENAME, "build"),
                cwd=target,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("no build command was detected", completed.stderr)


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

    def test_cmake_convert_without_selectors_stamps_detected_flavors(self) -> None:
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
            (target / "Makefile").write_text("# generated\n", encoding="utf-8")

            _adapter().initialize(
                target,
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
            self.assertIn(
                "+flavor://literate-ai/lang-cpp",
                manifest["default_flavor_selectors"],
            )
            self.assertNotIn(
                "+flavor://literate-ai/build-make",
                manifest["default_flavor_selectors"],
            )
            self.assertNotIn(
                "+flavor://literate-ai/lang-python",
                manifest["default_flavor_selectors"],
            )

    def test_detect_repo_flavors_picks_one_language_on_mixed_trees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "app.py").write_text("print('hi')\n", encoding="utf-8")
            (target / "main.cpp").write_text(
                "int main() { return 0; }\n", encoding="utf-8"
            )
            selectors = detect_repo_flavors(target)
            self.assertEqual(
                [
                    item
                    for item in selectors
                    if item.startswith("flavor://literate-ai/lang-")
                ],
                ["flavor://literate-ai/lang-cpp"],
            )
            self.assertEqual(
                detected_language_flavors(target),
                [
                    "flavor://literate-ai/lang-cpp",
                    "flavor://literate-ai/lang-python",
                ],
            )


class RepoManConvertSliceTests(unittest.TestCase):
    """repo.sh monorepos plan and wrap without a local make ci gate."""

    def test_repo_man_prefers_platform_native_build_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "repo-man-wrapper"
            target.mkdir()
            if os.name == "nt":
                (target / "repo.bat").write_text(
                    "@echo off\r\nexit /b 0\r\n", encoding="utf-8"
                )
                (target / "build.bat").write_text(
                    '@echo off\r\ncall "%~dp0repo.bat" build %*\r\n',
                    encoding="utf-8",
                )
                expected_command = ".\\build.bat --release"
                expected_evidence = "build.bat"
            else:
                _write_executable(target / "repo.sh", "#!/bin/sh\nexit 0\n")
                _write_executable(
                    target / "build.sh",
                    '#!/bin/sh\nexec "$(dirname "$0")/repo.sh" build "$@"\n',
                )
                expected_command = "./build.sh --release"
                expected_evidence = "build.sh"
            (target / "repo.toml").write_text(
                'name = "repo-man-wrapper"\n', encoding="utf-8"
            )

            inventory = inspect_harness(target)

            self.assertEqual(
                inventory["commands"]["build"],
                {
                    "command": expected_command,
                    "evidence": expected_evidence,
                    "cwd": ".",
                    "cost": "host-heavy",
                    "id": "build",
                },
            )
            self.assertEqual(inventory["stages"][0]["command"], expected_command)
            self.assertEqual(inventory["stages"][0]["evidence"], expected_evidence)

    @unittest.skipUnless(os.name == "posix", "shell wrapper execution requires POSIX")
    def test_repo_man_root_covers_nested_automatic_baseline_and_parity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "repo-man-wrapper-flow"
            target.mkdir()
            for relative in (Path(), Path("kit")):
                project_root = target / relative
                project_root.mkdir(parents=True, exist_ok=True)
                nested_build = (
                    "(cd kit && ./build.sh --release)\n" if relative == Path() else ""
                )
                _write_executable(
                    project_root / "repo.sh",
                    "#!/bin/sh\n"
                    "set -eu\n"
                    '[ "${1:-}" = "build" ]\n'
                    '[ "${2:-}" = "--release" ]\n'
                    'printf "repo\\n" > repo.sentinel\n',
                )
                _write_executable(
                    project_root / "build.sh",
                    "#!/bin/sh\n"
                    "set -eu\n"
                    '[ "${1:-}" = "--release" ]\n'
                    'printf "wrapper\\n" > wrapper.sentinel\n'
                    './repo.sh build "$@"\n' + nested_build,
                )
                (project_root / "repo.toml").write_text(
                    f'name = "{relative.name or "root"}"\n', encoding="utf-8"
                )

            inventory = inspect_harness(target)
            expected = [
                {
                    "id": "build",
                    "command": "./build.sh --release",
                    "evidence": "build.sh",
                    "cwd": ".",
                    "cost": "host-heavy",
                },
                {
                    "id": "build.kit",
                    "command": "./build.sh --release",
                    "evidence": "kit/build.sh",
                    "cwd": "kit",
                    "cost": "host-heavy",
                },
            ]
            self.assertEqual(inventory["stages"], expected)
            baseline = execute_harness_baseline(
                inventory, legacy_root=target, run_baseline=True
            )
            self.assertEqual(baseline["state"], "passed")
            self.assertEqual(
                [phase["command"] for phase in baseline["phases"]],
                [expected[0]["command"]],
            )
            self.assertEqual(
                baseline["omitted"],
                [
                    {
                        "id": "build.kit",
                        "command": "./build.sh --release",
                        "reason": "covered-by-root-stage",
                    }
                ],
            )

            wrapper = render_harness_wrapper(
                inventory, legacy_directory=".", legacy_root=target
            )
            (target / HARNESS_WRAPPER_FILENAME).write_text(wrapper, encoding="utf-8")
            self.assertIn("cd $(LITAI_LEGACY) && ./build.sh --release", wrapper)
            self.assertIn("cd $(LITAI_LEGACY)/kit && ./build.sh --release", wrapper)
            parity = execute_harness_wrapper_parity(
                inventory,
                baseline,
                project_root=target,
                legacy_root=target,
            )
            self.assertEqual(parity["state"], "passed")
            self.assertEqual([phase["phase"] for phase in parity["phases"]], ["build"])
            self.assertTrue(all(phase["parity"] for phase in parity["phases"]))
            nested = subprocess.run(
                ("make", "-f", HARNESS_WRAPPER_FILENAME, "build.kit"),
                cwd=target,
                capture_output=True,
                text=True,
            )
            self.assertEqual(nested.returncode, 0, nested.stderr)
            self.assertTrue((target / "kit" / "wrapper.sentinel").is_file())

    def test_non_repo_dotted_stage_is_not_covered_by_an_undotted_stage(self) -> None:
        inventory = {
            "findings": [],
            "commands": {
                "build": {
                    "command": "root-build",
                    "evidence": "root.build",
                    "cost": "local-cheap",
                }
            },
            "stages": [
                {
                    "id": "build",
                    "command": "root-build",
                    "evidence": "root.build",
                    "cwd": ".",
                    "cost": "local-cheap",
                },
                {
                    "id": "build.child",
                    "command": "child-build",
                    "evidence": "child/build",
                    "cwd": "child",
                    "cost": "local-cheap",
                },
            ],
        }
        with patch(
            "literate_ai.adapters.harness_inventory._stage_driver_available",
            return_value=True,
        ):
            runnable = stages_requiring_execution(inventory, run_baseline=True)
        self.assertEqual([stage["id"] for stage in runnable], ["build", "build.child"])

    def test_rootless_repo_man_nested_stage_remains_automatic(self) -> None:
        inventory = {
            "findings": [
                {
                    "detector_id": "build-system.repo-man",
                    "path": "runtime/repo.sh",
                    "detail": "repo.sh / packman project",
                }
            ],
            "commands": {},
            "stages": [
                {
                    "id": "build.runtime",
                    "command": "./repo.sh build --release",
                    "evidence": "runtime/repo.sh",
                    "cwd": "runtime",
                    "cost": "host-heavy",
                }
            ],
        }
        with patch(
            "literate_ai.adapters.harness_inventory._stage_driver_available",
            return_value=True,
        ):
            runnable = stages_requiring_execution(inventory, run_baseline=True)
        self.assertEqual([stage["id"] for stage in runnable], ["build.runtime"])

    @unittest.skipUnless(os.name == "posix", "shell harness execution requires POSIX")
    def test_retained_repo_man_run_does_not_reexecute_covered_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "runtime"
            child.mkdir()
            _write_executable(root / "build.sh", "#!/bin/sh\nset -eu\n")
            _write_executable(
                child / "repo.sh",
                "#!/bin/sh\nprintf 'nested stage must be manual\\n' >&2\nexit 91\n",
            )
            _write_executable(
                root / "test.sh",
                "#!/bin/sh\nprintf 'Ran 1 test\\nOK\\n'\n",
            )
            inventory = {
                "findings": [
                    {
                        "detector_id": "build-system.repo-man",
                        "path": "build.sh",
                        "detail": "repo.sh / packman project",
                    }
                ],
                "commands": {
                    "build": {
                        "command": "./build.sh --release",
                        "evidence": "build.sh",
                        "cwd": ".",
                        "cost": "host-heavy",
                    },
                    "test": {
                        "command": "./test.sh",
                        "evidence": "test.sh",
                        "cwd": ".",
                        "cost": "local-cheap",
                    },
                },
                "stages": [
                    {
                        "id": "build",
                        "command": "./build.sh --release",
                        "evidence": "build.sh",
                        "cwd": ".",
                        "cost": "host-heavy",
                    },
                    {
                        "id": "build.runtime",
                        "command": "./repo.sh build --release",
                        "evidence": "runtime/repo.sh",
                        "cwd": "runtime",
                        "cost": "host-heavy",
                    },
                    {
                        "id": "test",
                        "command": "./test.sh",
                        "evidence": "test.sh",
                        "cwd": ".",
                        "cost": "local-cheap",
                    },
                ],
            }

            report = execute_retained_harness(inventory, legacy_root=root)

        self.assertEqual(
            [phase["phase"] for phase in report["phases"]], ["build", "test"]
        )
        self.assertEqual(report["omitted"][0]["id"], "build.runtime")
        self.assertEqual(report["omitted"][0]["reason"], "covered-by-root-stage")

    @unittest.skipUnless(os.name == "posix", "executable bits require POSIX")
    def test_repo_man_ignores_nonexecutable_build_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            _write_executable(target / "repo.sh", "#!/bin/sh\nexit 0\n")
            (target / "build.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

            inventory = inspect_harness(target)

            self.assertEqual(
                inventory["commands"]["build"]["command"],
                "./repo.sh build --release",
            )
            self.assertEqual(inventory["commands"]["build"]["evidence"], "repo.sh")

    @unittest.skipUnless(os.name == "posix", "repo.sh execution requires POSIX")
    def test_repo_man_baseline_selects_an_explicit_configuration_under_ci(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "repo-man-ci"
            target.mkdir()
            _write_executable(
                target / "repo.sh",
                "#!/bin/sh\n"
                "set -eu\n"
                'if [ "${CI:-}" = "true" ] && [ "${2:-}" != "--release" ]; then\n'
                '  echo "configuration required under CI" >&2\n'
                "  exit 64\n"
                "fi\n"
                '[ "${1:-}" = "build" ]\n',
            )
            (target / "repo.toml").write_text(
                'name = "repo-man-ci"\n', encoding="utf-8"
            )

            rejected = subprocess.run(
                ("./repo.sh", "build"),
                cwd=target,
                env={**os.environ, "CI": "true"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(rejected.returncode, 64)
            self.assertIn("configuration required under CI", rejected.stderr)

            inventory = inspect_harness(target)
            stages = inventory["stages"]
            self.assertIsInstance(stages, list)
            self.assertEqual(
                [stage["command"] for stage in stages],
                ["./repo.sh build --release"],
            )
            baseline = execute_harness_baseline(
                inventory,
                legacy_root=target,
                run_baseline=True,
            )

            self.assertEqual(baseline["state"], "passed")
            self.assertEqual(baseline["phase_count"], 1)
            self.assertEqual(
                baseline["phases"][0]["command"], "./repo.sh build --release"
            )

    @unittest.skipUnless(os.name == "posix", "repo.sh execution requires POSIX")
    def test_repo_man_disposable_phases_stay_in_the_shared_ci_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "repo-man-nested-container"
            target.mkdir()
            target = target.resolve()
            _write_executable(
                target / "repo.sh",
                "#!/bin/sh\n"
                "set -eu\n"
                '[ "${1:-}" = "build" ]\n'
                '[ "${2:-}" = "--release" ]\n'
                '[ "$CI_PROJECT_DIR" = "$PWD" ]\n'
                'case "$CI_PROJECT_DIR" in\n'
                '  "$LITAI_SHARED_ROOT"/.literate-ai-convert-*/legacy) ;;\n'
                '  *) echo "disposable source is outside shared root" >&2; exit 65 ;;\n'
                "esac\n"
                'printf "LITAI_WORKSPACE=%s\\n" "$CI_PROJECT_DIR"\n',
            )
            (target / "repo.toml").write_text(
                'name = "repo-man-nested-container"\n', encoding="utf-8"
            )
            with patch.dict(
                os.environ,
                {
                    "CI_PROJECT_DIR": str(target),
                    "LITAI_SHARED_ROOT": str(target),
                },
                clear=False,
            ):
                result = _adapter().initialize(
                    target,
                    flavor_selectors=_signed_selectors(target),
                    source_intelligence_provider="none",
                    convert=True,
                    run_baseline=True,
                )

            self.assertEqual(result["harness_baseline"]["state"], "passed")
            self.assertEqual(result["harness_parity"]["state"], "passed")
            baseline = json.loads(
                (target / ".literate" / "legacy-harness-baseline.json").read_text(
                    encoding="utf-8"
                )
            )
            parity = json.loads(
                (target / ".literate" / "legacy-wrapper-parity.json").read_text(
                    encoding="utf-8"
                )
            )

            def reported_workspace(document: dict[str, object]) -> Path:
                phases = document["phases"]
                self.assertIsInstance(phases, list)
                phase = phases[0]
                self.assertIsInstance(phase, dict)
                output = phase["stdout_excerpt"]
                self.assertIsInstance(output, str)
                prefix = "LITAI_WORKSPACE="
                matches = [
                    line.removeprefix(prefix)
                    for line in output.splitlines()
                    if line.startswith(prefix)
                ]
                self.assertEqual(len(matches), 1)
                return Path(matches[0])

            baseline_root = reported_workspace(baseline)
            parity_root = reported_workspace(parity)
            self.assertEqual(baseline_root.parent.parent, target)
            self.assertEqual(parity_root.parent.parent, target)
            self.assertTrue(
                baseline_root.parent.name.startswith(".literate-ai-convert-baseline-")
            )
            self.assertTrue(
                parity_root.parent.name.startswith(".literate-ai-convert-wrapper-")
            )
            self.assertFalse(baseline_root.exists())
            self.assertFalse(parity_root.exists())
            self.assertEqual(list(target.glob(".literate-ai-convert-*")), [])

    def test_convert_plan_reports_repo_man_readiness_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "kitish"
            target.mkdir()
            _write_repo_man_fixture(target)
            before = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }

            report = plan_convert(target)

            after = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)
            self.assertFalse((target / "_legacy").exists())
            self.assertFalse((target / "literate.project.json").exists())
            self.assertEqual(report["schema"], CONVERT_PLAN_SCHEMA)
            self.assertEqual(report["readiness"], "ready")
            self.assertFalse(report["writes"])
            self.assertEqual(report["catalog_gaps"], [])
            self.assertIn(
                "flavor://literate-ai/lang-python", report["proposed_flavors"]
            )
            self.assertIn("flavor://literate-ai/lang-cpp", report["proposed_flavors"])
            self.assertIn(
                "flavor://literate-ai/build-repo-man", report["proposed_flavors"]
            )
            self.assertNotIn(
                "flavor://literate-ai/build-make", report["proposed_flavors"]
            )
            stage_ids = [stage["id"] for stage in report["proposed_wrapper_stages"]]
            self.assertEqual(stage_ids, ["build", "build.kit", "build.rendering"])
            self.assertEqual(report["ci"]["class"], "remote-configured")
            detector_ids = {item["detector_id"] for item in report["findings"]}
            self.assertIn("build-system.repo-man", detector_ids)
            self.assertIn("ci.os-matrix", detector_ids)
            self.assertIn("ci.gitlab", detector_ids)
            self.assertIn("ubuntu-latest", str(report["findings"]))
            self.assertIn("windows-latest", str(report["findings"]))
            self.assertEqual(report["languages"], ["cpp", "python"])
            init_languages = [
                item
                for item in detect_repo_flavors(target)
                if item.startswith("flavor://literate-ai/lang-")
            ]
            self.assertEqual(init_languages, ["flavor://literate-ai/lang-cpp"])

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

    def test_inspect_harness_records_nested_bazel_and_gitlab(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "third_party").mkdir()
            (root / "third_party" / "MODULE.bazel").write_text("", encoding="utf-8")
            (root / "third_party" / "BUILD.bazel").write_text("", encoding="utf-8")
            (root / ".gitlab-ci.yml").write_text(
                "test:\n  script: true\n", encoding="utf-8"
            )
            document = inspect_harness(root)
            detector_ids = {item["detector_id"] for item in document["findings"]}
            self.assertIn("build-system.bazel", detector_ids)
            self.assertIn("ci.gitlab", detector_ids)
            self.assertEqual(document["ci"]["class"], "remote-configured")

    def test_standalone_bazel_build_file_is_not_a_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "BUILD.bazel").write_text("# packaging stub\n", encoding="utf-8")

            document = inspect_harness(root)

            detector_ids = {item["detector_id"] for item in document["findings"]}
            self.assertNotIn("build-system.bazel", detector_ids)
            self.assertNotIn("build", document["commands"])

    def test_python_and_bazel_stages_are_local_cheap_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            python_root = Path(directory) / "py"
            python_root.mkdir()
            (python_root / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.1.0"\n'
                '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
                encoding="utf-8",
            )
            (python_root / "tests").mkdir()
            (python_root / "tests" / "test_demo.py").write_text(
                "def test_demo():\n    assert True\n", encoding="utf-8"
            )
            python_inventory = inspect_harness(python_root)
            python_costs = {
                stage["id"]: stage["cost"] for stage in python_inventory["stages"]
            }
            self.assertEqual(python_costs["build"], "local-cheap")
            self.assertEqual(python_costs["test"], "local-cheap")
            with patch(
                "literate_ai.adapters.harness_inventory.importlib.util.find_spec",
                return_value=None,
            ) as find_spec:
                self.assertEqual(
                    stages_requiring_execution(python_inventory, run_baseline=False),
                    [],
                )
                find_spec.assert_any_call("build")
                find_spec.assert_any_call("pytest")

            bazel_root = Path(directory) / "bz"
            bazel_root.mkdir()
            (bazel_root / "MODULE.bazel").write_text("", encoding="utf-8")
            bazel_inventory = inspect_harness(bazel_root)
            bazel_costs = {
                stage["id"]: stage["cost"] for stage in bazel_inventory["stages"]
            }
            self.assertEqual(bazel_costs["build"], "local-cheap")
            with patch(
                "literate_ai.adapters.harness_inventory.shutil.which",
                return_value=None,
            ):
                self.assertEqual(
                    stages_requiring_execution(bazel_inventory, run_baseline=False),
                    [],
                )

    def test_requirements_txt_is_not_a_python_build_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requirements.txt").write_text("rich\n", encoding="utf-8")
            document = inspect_harness(root)
            self.assertNotIn("build", document["commands"])
            self.assertEqual(document["stages"], [])

    def test_pyproject_convert_skips_missing_python_build_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "py-only"
            target.mkdir()
            (target / "pyproject.toml").write_text(
                '[project]\nname = "demo"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            with patch(
                "literate_ai.adapters.harness_inventory.importlib.util.find_spec",
                return_value=None,
            ):
                result = _adapter().initialize(
                    target,
                    source_intelligence_provider="none",
                    convert=True,
                )
            self.assertTrue((target / "literate.project.json").is_file())
            self.assertEqual(result["harness_baseline"]["state"], "skipped")
            inventory = json.loads(
                (target / ".literate" / "harness-inventory.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                inventory["commands"]["build"]["command"], "python -m build"
            )

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

    def test_escaping_cwd_fails_baseline_as_harness_error(self) -> None:
        inventory = {
            "findings": [],
            "commands": {
                "build": {
                    "command": "true",
                    "evidence": "Makefile",
                    "cwd": "../../etc",
                    "cost": "local-cheap",
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
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HarnessBaselineError) as raised:
                execute_harness_baseline(inventory, legacy_root=Path(directory))
        self.assertEqual(raised.exception.code, "project.convert_cwd_escapes_legacy")
        self.assertEqual(raised.exception.report["state"], "failed")

    def test_baseline_projects_gitlab_workspace_to_disposable_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            legacy_root = Path(directory)
            program = "import os; print(os.environ['CI_PROJECT_DIR'], end='')"
            command = (
                subprocess.list2cmdline([sys.executable, "-c", program])
                if os.name == "nt"
                else shlex.join([sys.executable, "-c", program])
            )
            with patch.dict(
                os.environ, {"CI_PROJECT_DIR": "/original/project"}, clear=False
            ):
                result = _run_harness_command(
                    "build",
                    {
                        "command": command,
                        "evidence": "repo.sh",
                    },
                    legacy_root=legacy_root,
                    timeout_seconds=10,
                )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["timeout_seconds"], 10)
        self.assertEqual(result["stdout_excerpt"], str(legacy_root))

    def test_disposable_git_policy_discards_ambient_git_controls(self) -> None:
        controls = {
            "GIT_DIR": "unused-ambient-selector",
            "GIT_WORK_TREE": "unused-ambient-worktree",
            "GIT_INDEX_FILE": "unused-ambient-index",
            "GIT_CONFIG_COUNT": "0",
            "GIT_CONFIG_PARAMETERS": "unused-ambient-configuration",
            "GIT_SSH_COMMAND": "unused-ambient-transport",
            "GIT_FUTURE_CONTROL": "unused-ambient-control",
            "LITAI_TEST_TOOLCHAIN_SETTING": "preserved",
        }
        with patch.dict(os.environ, controls):
            before = dict(os.environ)
            environment = _sanitized_git_environment()
            self.assertTrue(dict(os.environ) == before, "caller environment changed")
        for key in controls:
            if key.startswith("GIT_"):
                self.assertFalse(key in environment, f"ambient control retained: {key}")
        self.assertEqual(environment["LITAI_TEST_TOOLCHAIN_SETTING"], "preserved")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(
            environment["GIT_AUTHOR_EMAIL"], "disposable-harness@literate-ai.invalid"
        )

    def test_phase_command_uses_disposable_git_policy_without_changing_host(
        self,
    ) -> None:
        controls = {
            "GIT_WORK_TREE": "unused-ambient-worktree",
            "GIT_CONFIG_COUNT": "0",
            "GIT_FUTURE_CONTROL": "unused-ambient-control",
            "LITAI_TEST_TOOLCHAIN_SETTING": "preserved",
        }
        program = (
            "import json, os; print(json.dumps({"
            "'ambient': [name for name in "
            "['GIT_WORK_TREE', 'GIT_CONFIG_COUNT', 'GIT_FUTURE_CONTROL'] "
            "if name in os.environ], "
            "'toolchain': os.environ.get('LITAI_TEST_TOOLCHAIN_SETTING'), "
            "'global': os.environ.get('GIT_CONFIG_GLOBAL'), "
            "'system': os.environ.get('GIT_CONFIG_NOSYSTEM'), "
            "'project': os.environ['CI_PROJECT_DIR'], "
            "'omni': os.environ['OMNI_REPO_ROOT']}))"
        )
        command = (
            subprocess.list2cmdline([sys.executable, "-c", program])
            if os.name == "nt"
            else shlex.join([sys.executable, "-c", program])
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            entry = {"command": command, "evidence": "fixture"}
            # Prepare only this fixture's Git identity before adding benign ambient
            # controls; the command itself runs Python, never a remote/helper.
            _run_harness_command("build", entry, legacy_root=root, timeout_seconds=10)
            with patch.dict(os.environ, controls):
                before = dict(os.environ)
                result = _run_harness_command(
                    "build", entry, legacy_root=root, timeout_seconds=10
                )
                self.assertTrue(
                    dict(os.environ) == before, "caller environment changed"
                )
            self.assertEqual(result["exit_code"], 0)
            observed = json.loads(result["stdout_excerpt"])
            self.assertEqual(observed["ambient"], [])
            self.assertEqual(observed["toolchain"], "preserved")
            self.assertEqual(observed["global"], os.devnull)
            self.assertEqual(observed["system"], "1")
            self.assertEqual(observed["project"], str(root))
            self.assertEqual(observed["omni"], str(root))

    @unittest.skipUnless(os.name == "posix", "shells out to the git binary")
    def test_disposable_phase_root_gets_sanitized_git_identity_not_outer_checkout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outer_root = Path(directory) / "outer-checkout"
            outer_root.mkdir()
            _git_command = (
                "git",
                "-c",
                "user.name=Outer Host Operator",
                "-c",
                "user.email=outer-host-operator@example.test",
                "-C",
                str(outer_root),
            )
            subprocess.run((*_git_command, "init", "--quiet"), check=True)
            (outer_root / "README.md").write_text("outer\n", encoding="utf-8")
            subprocess.run((*_git_command, "add", "--all"), check=True)
            subprocess.run(
                (*_git_command, "commit", "--quiet", "-m", "outer checkout"),
                check=True,
            )
            outer_revision = subprocess.run(
                (*_git_command, "rev-parse", "HEAD"),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            phase_root = outer_root / ".literate-ai-convert-baseline-x" / "legacy"
            phase_root.mkdir(parents=True)
            (phase_root / "repo.toml").write_text('name = "legacy"\n', encoding="utf-8")

            program = (
                "import os, subprocess; "
                "cwd = os.getcwd(); "
                "revision = subprocess.run(['git', 'rev-parse', 'HEAD'], "
                "capture_output=True, text=True, check=True).stdout.strip(); "
                "email = subprocess.run(['git', 'config', 'user.email'], "
                "capture_output=True, text=True, check=True).stdout.strip(); "
                "print(cwd); print(revision); print(email); "
                "print(os.environ['CI_PROJECT_DIR']); "
                "print(os.environ['OMNI_REPO_ROOT'])"
            )
            command = shlex.join([sys.executable, "-c", program])

            result = _run_harness_command(
                "build",
                {"command": command, "evidence": "repo.toml"},
                legacy_root=phase_root,
                timeout_seconds=10,
            )

        self.assertEqual(result["exit_code"], 0)
        lines = result["stdout_excerpt"].splitlines()
        reported_cwd, reported_revision, reported_email, ci_project_dir, omni_root = (
            lines
        )
        self.assertEqual(Path(reported_cwd), phase_root.resolve())
        self.assertEqual(ci_project_dir, str(phase_root))
        self.assertEqual(omni_root, str(phase_root))
        # The disposable phase root resolves its own Git revision -- it does not
        # walk up and inherit the outer checkout's HEAD.
        self.assertNotEqual(reported_revision, outer_revision)
        self.assertRegex(reported_revision, r"^[0-9a-f]{40}$")
        # The identity is framework-owned and sanitized -- never the host/outer
        # checkout's committer identity or any copied host credential.
        self.assertEqual(reported_email, "disposable-harness@literate-ai.invalid")

    def test_long_failure_preserves_full_identity_root_error_and_physical_tail(
        self,
    ) -> None:
        root_error = b"ROOT-ERROR compiler API mismatch\n"
        tail = b"ACTUAL-TAIL archive command failed\n"
        stdout = root_error + (b"x" * 1_100_000) + b"\n" + tail
        stderr = b"GENERIC-WRAPPER exception\n"
        program = (
            "import sys; "
            "sys.stdout.buffer.write("
            "b'ROOT-ERROR compiler API mismatch\\n' + b'x' * 1100000 + "
            "b'\\nACTUAL-TAIL archive command failed\\n'); "
            "sys.stderr.buffer.write(b'GENERIC-WRAPPER exception\\n'); "
            "raise SystemExit(7)"
        )
        command = (
            subprocess.list2cmdline([sys.executable, "-c", program])
            if os.name == "nt"
            else shlex.join([sys.executable, "-c", program])
        )
        with tempfile.TemporaryDirectory() as directory:
            phase = _run_harness_command(
                "build",
                {"command": command, "evidence": "synthetic"},
                legacy_root=Path(directory),
                timeout_seconds=30,
                diagnostic_limit_chars=4096,
            )

        self.assertEqual(phase["stdout_size"], len(stdout))
        self.assertEqual(phase["stderr_size"], len(stderr))
        self.assertEqual(
            phase["stdout_identity"],
            "sha256:" + hashlib.sha256(stdout).hexdigest(),
        )
        self.assertEqual(
            phase["stderr_identity"],
            "sha256:" + hashlib.sha256(stderr).hexdigest(),
        )
        self.assertFalse(phase["output_within_limits"])
        self.assertEqual(phase["diagnostic_limit_chars"], 4096)
        self.assertIn("ROOT-ERROR compiler API mismatch", phase["stdout_excerpt"])
        self.assertIn("ACTUAL-TAIL archive command failed", phase["stdout_excerpt"])
        message = _gate_failure_message("build", phase)
        self.assertIn("[stderr]", message)
        self.assertIn("GENERIC-WRAPPER exception", message)
        self.assertIn("[stdout]", message)
        self.assertIn("ROOT-ERROR compiler API mismatch", message)
        self.assertIn("ACTUAL-TAIL archive command failed", message)
        self.assertIn("exceeded its bounded output policy", message)
        diagnostic = (
            f"[stderr]\n{phase['stderr_excerpt']}\n[stdout]\n{phase['stdout_excerpt']}"
        )
        self.assertLessEqual(len(diagnostic), 4096)

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

    def test_large_test_output_observes_summary_from_physical_tail(self) -> None:
        program = (
            "import sys; sys.stdout.buffer.write(b'x' * 1100000 + b'\\n17 passed\\n')"
        )
        command = (
            subprocess.list2cmdline([sys.executable, "-c", program])
            if os.name == "nt"
            else shlex.join([sys.executable, "-c", program])
        )
        with tempfile.TemporaryDirectory() as directory:
            result = _run_harness_command(
                "test",
                {"command": command, "evidence": "synthetic"},
                legacy_root=Path(directory),
                timeout_seconds=30,
            )

        self.assertEqual(result["test_collection"]["state"], "nonempty")
        self.assertEqual(result["test_collection"]["total"], 17)
        self.assertEqual(result["test_collection"]["passed"], 17)
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["output_within_limits"])
        message = _gate_failure_message("test", result)
        self.assertIn("status 0", message)
        self.assertIn("exceeded its bounded output policy", message)

    def test_repo_test_aggregate_overrides_zero_test_startup_probe(self) -> None:
        program = (
            "print('Ran 0 tests in 0.000s'); "
            "print('[OK] All 2 tests processes returned 0.'); "
            "print('[OK] All 17 tests processes returned 0.')"
        )
        command = (
            subprocess.list2cmdline([sys.executable, "-c", program])
            if os.name == "nt"
            else shlex.join([sys.executable, "-c", program])
        )
        with tempfile.TemporaryDirectory() as directory:
            result = _run_harness_command(
                "test",
                {"command": command, "evidence": "synthetic"},
                legacy_root=Path(directory),
                timeout_seconds=30,
            )

        self.assertEqual(result["test_collection"]["state"], "nonempty")
        self.assertEqual(result["test_collection"]["total"], 17)
        self.assertEqual(result["test_collection"]["passed"], 17)
        self.assertEqual(result["test_collection"]["failed"], 0)

    def test_large_successful_output_is_observable_but_does_not_fail_baseline(
        self,
    ) -> None:
        program = "import sys; sys.stdout.buffer.write(b'x' * 1100000)"
        interpreter = str(Path(sys.executable).resolve())
        command = (
            subprocess.list2cmdline([interpreter, "-c", program])
            if os.name == "nt"
            else shlex.join([interpreter, "-c", program])
        )
        inventory = {
            "findings": [],
            "commands": {
                "build": {
                    "command": command,
                    "evidence": "synthetic",
                    "cost": "local-cheap",
                }
            },
            "stages": [
                {
                    "id": "build",
                    "command": command,
                    "evidence": "synthetic",
                    "cost": "local-cheap",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            result = execute_harness_baseline(
                inventory,
                legacy_root=Path(directory),
                timeout_seconds=30,
            )

        self.assertEqual(result["state"], "passed")
        self.assertEqual(result["phase_count"], 1)
        phase = result["phases"][0]
        self.assertEqual(phase["exit_code"], 0)
        self.assertFalse(phase["output_within_limits"])
        self.assertEqual(phase["stdout_size"], 1_100_000)
        self.assertEqual(
            phase["stdout_identity"],
            "sha256:" + hashlib.sha256(b"x" * 1_100_000).hexdigest(),
        )
        self.assertIn("[output tail]", phase["stdout_excerpt"])

    def test_harness_diagnostic_limit_is_finite_and_bounded(self) -> None:
        self.assertEqual(
            validate_harness_diagnostic_limit(HARNESS_DIAGNOSTIC_CHARS),
            HARNESS_DIAGNOSTIC_CHARS,
        )
        for invalid in (True, "8192", 511, 65_537):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_harness_diagnostic_limit(invalid)

    def test_partial_driver_skip_is_omitted_not_proven(self) -> None:
        inventory = {
            "findings": [],
            "commands": {
                "build": {
                    "command": "python -m build",
                    "evidence": "pyproject.toml",
                    "cost": "local-cheap",
                },
                "test": {
                    "command": "python -m pytest tests",
                    "evidence": "pyproject.toml",
                    "cost": "local-cheap",
                },
            },
            "stages": [
                {
                    "id": "build",
                    "command": "python -m build",
                    "evidence": "pyproject.toml",
                    "cwd": ".",
                    "cost": "local-cheap",
                },
                {
                    "id": "test",
                    "command": "python -m pytest tests",
                    "evidence": "pyproject.toml",
                    "cwd": ".",
                    "cost": "local-cheap",
                },
            ],
        }
        success = {
            "phase": "build",
            "command": "python -m build",
            "evidence": "pyproject.toml",
            "exit_code": 0,
            "timed_out": False,
            "output_within_limits": True,
            "tree": {},
            "source_tree": {},
        }

        def available(command: str) -> bool:
            return "build" in command and "pytest" not in command

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "literate_ai.adapters.harness_inventory._stage_driver_available",
                    side_effect=available,
                ),
                patch(
                    "literate_ai.adapters.harness_inventory._run_harness_command",
                    return_value=success,
                ),
            ):
                report = execute_harness_baseline(
                    inventory, legacy_root=Path(directory)
                )
        self.assertEqual(report["state"], "passed")
        self.assertEqual(
            report["omitted"],
            [
                {
                    "id": "test",
                    "command": "python -m pytest tests",
                    "reason": "driver-unavailable",
                }
            ],
        )
        component = legacy_shim_authority(inventory, report)[
            "components/legacy-project-wrapper/component.md"
        ]
        self.assertIn("Proven stages (direct baseline ran) are", component)
        self.assertIn("`build`", component)
        self.assertIn("recorded but not executed are `test`", component)
        self.assertNotIn("`test`", component.split("recorded but not executed")[0])

    def test_nested_stage_does_not_prove_undotted_build(self) -> None:
        inventory = {
            "commands": {
                "build": {
                    "command": "./repo.sh build",
                    "evidence": "repo.sh",
                    "cost": "host-heavy",
                }
            },
            "stages": [
                {
                    "id": "build.kit",
                    "command": "./repo.sh build",
                    "evidence": "kit/repo.sh",
                    "cwd": "kit",
                    "cost": "host-heavy",
                }
            ],
        }
        baseline = {
            "state": "passed",
            "phases": [{"phase": "build.kit", "exit_code": 0}],
            "omitted": [],
        }
        component = legacy_shim_authority(inventory, baseline)[
            "components/legacy-project-wrapper/component.md"
        ]
        proven, _, rest = component.partition("recorded but not executed")
        self.assertNotIn("`build`", proven)
        self.assertIn("`build`", rest)

    def test_empty_evidence_fails_baseline_as_harness_error(self) -> None:
        inventory = {
            "findings": [],
            "commands": {
                "build": {
                    "command": "true",
                    "evidence": "",
                    "cost": "local-cheap",
                }
            },
            "stages": [
                {
                    "id": "build",
                    "command": "true",
                    "evidence": "",
                    "cwd": ".",
                    "cost": "local-cheap",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HarnessBaselineError) as raised:
                execute_harness_baseline(inventory, legacy_root=Path(directory))
        self.assertEqual(raised.exception.code, "project.convert_evidence_missing")
        self.assertEqual(raised.exception.report["state"], "failed")

    def test_empty_command_fails_baseline_instead_of_driver_skip(self) -> None:
        inventory = {
            "findings": [],
            "commands": {
                "build": {
                    "command": "   ",
                    "evidence": "Makefile",
                    "cost": "local-cheap",
                }
            },
            "stages": [
                {
                    "id": "build",
                    "command": "   ",
                    "evidence": "Makefile",
                    "cwd": ".",
                    "cost": "local-cheap",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HarnessBaselineError) as raised:
                execute_harness_baseline(inventory, legacy_root=Path(directory))
        self.assertEqual(raised.exception.code, "project.convert_command_invalid")
        self.assertEqual(
            stages_requiring_execution(inventory, run_baseline=False),
            [],
        )

    def test_whitespace_evidence_fails_baseline_as_harness_error(self) -> None:
        inventory = {
            "findings": [],
            "commands": {
                "build": {
                    "command": "true",
                    "evidence": "  ",
                    "cost": "local-cheap",
                }
            },
            "stages": [
                {
                    "id": "build",
                    "command": "true",
                    "evidence": "  ",
                    "cwd": ".",
                    "cost": "local-cheap",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HarnessBaselineError) as raised:
                execute_harness_baseline(inventory, legacy_root=Path(directory))
        self.assertEqual(raised.exception.code, "project.convert_evidence_missing")

    def test_wrapper_recipe_rejects_symlink_cwd_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "legacy"
            outside = Path(directory) / "outside"
            legacy.mkdir()
            outside.mkdir()
            (legacy / "kit").symlink_to(outside)
            inventory = {
                "commands": {
                    "build": {
                        "command": "true",
                        "evidence": "repo.sh",
                        "cwd": "kit",
                    }
                },
                "stages": [
                    {
                        "id": "build",
                        "command": "true",
                        "evidence": "repo.sh",
                        "cwd": "kit",
                        "cost": "host-heavy",
                    }
                ],
            }
            wrapper = render_harness_wrapper(
                inventory,
                legacy_directory="_legacy/x",
                legacy_root=legacy,
            )
            self.assertIn("$(error harness cwd escapes the legacy tree", wrapper)
            self.assertNotIn("cd $(LITAI_LEGACY)/kit", wrapper)

    def test_convert_plan_reports_blocked_no_driver_for_notes_only_trees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "notes"
            target.mkdir()
            (target / "README.md").write_text("# notes\n", encoding="utf-8")
            report = plan_convert(target)
            self.assertEqual(report["readiness"], "blocked-no-driver")
            self.assertEqual(report["proposed_wrapper_stages"], [])
            result = _adapter().initialize(
                target,
                source_intelligence_provider="none",
                convert=True,
            )
            self.assertTrue((target / "literate.project.json").is_file())
            self.assertTrue((target / HARNESS_WRAPPER_FILENAME).is_file())
            self.assertEqual(result["detected_languages"], [])


if __name__ == "__main__":
    unittest.main()
