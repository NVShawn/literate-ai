"""Filesystem Component-lock planning adapter tests."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import literate_ai.adapters.component_lock_planning as lock_planning
from literate_ai.adapters.component_lock_planning import (
    ComponentLockPlanningError,
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.specifications import OpenSpecError
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.flavors import CandidateStatus
from literate_ai.storage import FileSystemCAS

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _component_document() -> str:
    return """---
namespace: examples
version: 1.0.0
display_name: Portable Greeting
profiles:
  - application
  - portable
sample: true
provides:
  - name: sample.portable-app
    version: 1.0.0
requires: []
specification_provider: openspec
specification_roots:
  - specs/spec.md
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/implement.json
workflow_definition: workflows/host.json
routing_policy: routing/default.json
flavor_slots:
  - slot_id: language
    axis: implementation.language-ecosystem
    cardinality: exactly-one
    capability_contract: sample.portable-app
  - slot_id: os
    axis: platform.os
    cardinality: exactly-one
    capability_contract: sample.portable-app
entrypoints:
  - name: run
    kind: portable-application
    path: run
acceptance_contracts:
  - acceptance/execution.json
source_dependencies: []
---
A small portable greeting application with deterministic behavior.
"""


def _fixture(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: fixture-project\n"
        "description: Component-lock planning fixture.\n"
        "---\n"
        "# Fixture project\n",
        encoding="utf-8",
        newline="\n",
    )
    component = root / "greeting"
    for relative, content in {
        "component.md": _component_document(),
        "specs/spec.md": (
            "# Greeting\n\n### Requirement: Deterministic greeting\n\n"
            "The application SHALL print a stable greeting.\n\n"
            "#### Scenario: Run greeting\n\n- **WHEN** the application runs\n"
            "- **THEN** it prints `hello`\n"
        ),
        "acceptance/execution.json": '{"stdout":"hello\\n"}\n',
    }.items():
        path = component / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    for relative, content in {
        "skills/implement.json": '{"skill":"portable"}\n',
        "workflows/host.json": '{"workflow":"host"}\n',
        "routing/default.json": '{"routing":"default"}\n',
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    flavors = root / "flavors"
    for flavor in ("os-macos", "lang-python"):
        shutil.copytree(_REPOSITORY_ROOT / "flavors" / flavor, flavors / flavor)
    for skill in (
        "portable-specification-planning",
        "portable-application-implementation",
        "python-portable-application",
    ):
        shutil.copytree(
            _REPOSITORY_ROOT / "skills" / "specification-to-source" / skill,
            root / "skills" / "specification-to-source" / skill,
        )
    return component, flavors


def _flavor_variant(
    source: Path,
    destination: Path,
    *,
    name: str,
    value: str,
    conflicts: tuple[str, ...] = (),
) -> None:
    shutil.copytree(source, destination)
    manifest = destination / "flavor.md"
    document, body = parse_authoring_markdown(
        manifest.read_bytes(), source=manifest.as_posix()
    )
    document["name"] = name
    document["display_name"] = name
    document["target"] = value
    document["conflicts"] = list(conflicts)
    manifest.write_bytes(render_authoring_markdown(document, body))


def _set_flavor_requirement(
    flavor: Path,
    *,
    requirement_id: str,
    capability: str,
) -> None:
    manifest = flavor / "flavor.md"
    document, body = parse_authoring_markdown(
        manifest.read_bytes(), source=manifest.as_posix()
    )
    document["requires"] = [
        {
            "requirement_id": requirement_id,
            "capability": capability,
            "version_range": ">=1,<2",
            "dependency_kind": "packaging",
            "optional": False,
            "constraints": [],
        }
    ]
    manifest.write_bytes(render_authoring_markdown(document, body))


def _provider_component(
    root: Path,
    *,
    name: str,
    capability: str,
    requirement: tuple[str, str] | None = None,
) -> Path:
    component = root / name
    component.mkdir()
    requires = "requires: []"
    if requirement is not None:
        requirement_id, required_capability = requirement
        requires = (
            "requires:\n"
            f"  - requirement_id: {requirement_id}\n"
            f"    capability: {required_capability}\n"
            '    version_range: ">=1,<2"\n'
            "    dependency_kind: packaging\n"
            "    optional: false\n"
            "    constraints: []"
        )
    (component / "component.md").write_text(
        "---\n"
        "namespace: examples\n"
        "version: 1.0.0\n"
        f"display_name: {name}\n"
        "profiles:\n"
        "  - portable\n"
        "sample: false\n"
        "provides:\n"
        f"  - name: {capability}\n"
        "    version: 1.0.0\n"
        "    interface: null\n"
        f"{requires}\n"
        "specification_provider: openspec\n"
        "specification_roots:\n"
        "  - specs/spec.md\n"
        "authoring_inputs:\n"
        "  - kind: specification-to-source-skill\n"
        "    uri: skills/implement.json\n"
        "workflow_definition: workflows/host.json\n"
        "routing_policy: routing/default.json\n"
        "flavor_slots: []\n"
        "entrypoints:\n"
        "  - name: publish\n"
        "    kind: packaging\n"
        "    path: publish\n"
        "acceptance_contracts: []\n"
        "source_dependencies: []\n"
        "---\n"
        f"# {name}\n",
        encoding="utf-8",
        newline="\n",
    )
    specification = component / "specs" / "spec.md"
    specification.parent.mkdir()
    specification.write_text(
        f"# {name}\n\n### Requirement: Publish\n\n"
        f"The Component SHALL provide `{capability}`.\n\n"
        "#### Scenario: Invocation\n\n"
        "- **WHEN** packaging runs\n"
        "- **THEN** publication succeeds\n",
        encoding="utf-8",
        newline="\n",
    )
    return component


def _create_windows_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.decode(errors="replace"))


class ComponentLockPlanningTests(unittest.TestCase):
    def test_structured_specification_providers_lock_exact_artifacts(self) -> None:
        for provider in ("dmn", "scxml"):
            with (
                self.subTest(provider=provider),
                tempfile.TemporaryDirectory() as temporary,
            ):
                component, flavors = _fixture(Path(temporary))
                manifest = component / "component.md"
                if provider == "dmn":
                    relative = "specs/table.dmn"
                    content = (
                        '<?xml version="1.0"?><definitions xmlns="https://www.omg.org/'
                        'spec/DMN/20191111/MODEL/"><decision id="d"><decisionTable '
                        'id="t" hitPolicy="UNIQUE"><input><inputExpression '
                        'typeRef="number"><text>Age</text></inputExpression></input>'
                        '<output typeRef="string"/><rule id="r"><inputEntry>'
                        '<text>[0..1]</text></inputEntry><outputEntry><text>"ok"</text>'
                        "</outputEntry></rule></decisionTable></decision></definitions>"
                    )
                else:
                    relative = "specs/chart.scxml"
                    content = (
                        '<scxml xmlns="http://www.w3.org/2005/07/scxml" '
                        'version="1.0" initial="ready"><state id="ready"/></scxml>'
                    )
                old = component / "specs/spec.md"
                old.unlink()
                artifact = component / relative
                artifact.write_text(content, encoding="utf-8")
                manifest.write_text(
                    manifest.read_text(encoding="utf-8")
                    .replace(
                        "specification_provider: openspec",
                        f"specification_provider: {provider}",
                    )
                    .replace("  - specs/spec.md", f"  - {relative}"),
                    encoding="utf-8",
                )

                plan = FilesystemComponentLockPlanner().plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python"),
                    flavor_roots=(flavors,),
                )

                self.assertEqual(plan.nodes[0].specifications[0].uri, relative)
                self.assertEqual(
                    plan.nodes[0].specifications[0].identity.digest,
                    hashlib.sha256(content.encode()).hexdigest(),
                )

    def test_selected_flavor_requirements_extend_exact_component_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component, flavors = _fixture(root)
            _provider_component(
                component,
                name="publisher",
                capability="sample.publisher",
                requirement=("sign-package", "sample.signer"),
            )
            _provider_component(component, name="signer", capability="sample.signer")
            _flavor_variant(
                flavors / "lang-python",
                flavors / "python-clean",
                name="implementation-python-clean",
                value="python-clean",
            )
            _set_flavor_requirement(
                flavors / "lang-python",
                requirement_id="publish-package",
                capability="sample.publisher",
            )

            planner = FilesystemComponentLockPlanner()
            selected = planner.plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
                flavor_roots=(flavors,),
            )
            resolved = ComponentLockResolver().resolve(
                selected, expected_input_evidence_identity=selected.identity
            )
            unselected = planner.plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python-clean"),
                flavor_roots=(flavors,),
            )

        self.assertEqual(
            {node.authoring.coordinate.name for node in selected.nodes},
            {"greeting", "publisher", "signer"},
        )
        root_input = next(
            node
            for node in selected.nodes
            if node.authoring.coordinate.name == "greeting"
        )
        self.assertEqual(
            tuple(
                item.requirement.requirement_id
                for item in root_input.flavor_requirements
            ),
            ("publish-package",),
        )
        self.assertEqual(
            {(edge.requirement_id, edge.kind.value) for edge in resolved.lock.edges},
            {("publish-package", "packaging"), ("sign-package", "packaging")},
        )
        self.assertEqual(
            ComponentLock.from_dict(
                resolved.lock.to_dict(), authorings=resolved.lock.authorings
            ),
            resolved.lock,
        )
        self.assertEqual(len(unselected.nodes), 1)
        self.assertNotIn("flavor_requirements", unselected.nodes[0].to_dict())

    def test_selected_flavor_requirement_must_resolve_uniquely(self) -> None:
        cases = ((0, "unresolved"), (2, "ambiguous"))
        for provider_count, reason in cases:
            with (
                self.subTest(reason=reason),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                component, flavors = _fixture(root)
                for index in range(provider_count):
                    _provider_component(
                        component,
                        name=f"publisher-{index}",
                        capability="sample.publisher",
                    )
                _set_flavor_requirement(
                    flavors / "lang-python",
                    requirement_id="publish-package",
                    capability="sample.publisher",
                )

                with self.assertRaises(ComponentLockPlanningError) as caught:
                    FilesystemComponentLockPlanner().plan(
                        component,
                        target_name="macos-host",
                        flavor_selectors=("+macos", "+python"),
                        flavor_roots=(flavors,),
                    )

            self.assertEqual(
                caught.exception.code, f"component_lock.requirement_{reason}"
            )

    def test_component_and_selected_flavor_requirement_ids_cannot_collide(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component, flavors = _fixture(root)
            _provider_component(
                component, name="publisher", capability="sample.publisher"
            )
            _set_flavor_requirement(
                flavors / "lang-python",
                requirement_id="publish-package",
                capability="sample.publisher",
            )
            manifest = component / "component.md"
            document, body = parse_authoring_markdown(
                manifest.read_bytes(), source=manifest.as_posix()
            )
            document["requires"] = [
                {
                    "requirement_id": "publish-package",
                    "capability": "sample.publisher",
                    "version_range": ">=1,<2",
                    "dependency_kind": "packaging",
                    "optional": False,
                    "constraints": [],
                }
            ]
            manifest.write_bytes(render_authoring_markdown(document, body))

            with self.assertRaises(ComponentLockPlanningError) as caught:
                FilesystemComponentLockPlanner().plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python"),
                    flavor_roots=(flavors,),
                )

        self.assertEqual(caught.exception.code, "component_lock.requirement_duplicate")

    def test_http_asset_must_be_reachable_and_matches_its_pin(self) -> None:
        class Response(io.BytesIO):
            headers: dict[str, str] = {}

            def geturl(self) -> str:
                return "https://assets.example.test/reference.bin"

        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            content = b"external-reference-data"
            pin = "sha256:" + hashlib.sha256(content).hexdigest()
            manifest = component / "component.md"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "source_dependencies: []\n---",
                    "source_dependencies: []\n"
                    "assets:\n"
                    "  - asset_id: external-reference\n"
                    "    source: https://assets.example.test/reference.bin\n"
                    "    path: source/data/reference.bin\n"
                    "    role: runtime-data\n"
                    f"    pin: {pin}\n"
                    "---",
                ),
                encoding="utf-8",
                newline="\n",
            )

            with mock.patch.object(
                lock_planning.urllib.request,
                "urlopen",
                side_effect=lambda *_args, **_kwargs: Response(content),
            ) as fetch:
                plan = FilesystemComponentLockPlanner().plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python"),
                    flavor_roots=(flavors,),
                )

            self.assertGreaterEqual(fetch.call_count, 2)
            self.assertEqual(plan.nodes[0].assets[0].blob.identity, pin)

    def test_assets_are_reachable_hashed_and_locked_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            content = b"\x89PNG\r\n\x1a\nreference-image"
            asset_path = component / "assets" / "reference.png"
            asset_path.parent.mkdir()
            asset_path.write_bytes(content)
            pin = "sha256:" + hashlib.sha256(content).hexdigest()
            manifest = component / "component.md"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "source_dependencies: []\n---",
                    "source_dependencies: []\n"
                    "assets:\n"
                    "  - asset_id: reference-image\n"
                    "    source: assets/reference.png\n"
                    "    path: source/assets/reference.png\n"
                    "    role: runtime-data\n"
                    "    media_type: image/png\n"
                    f"    pin: {pin}\n"
                    "---",
                ),
                encoding="utf-8",
                newline="\n",
            )
            plan, snapshot = FilesystemComponentLockPlanner().plan_with_snapshot(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
                flavor_roots=(flavors,),
            )
            result = ComponentLockResolver().resolve(
                plan, expected_input_evidence_identity=plan.identity
            )

            locked = result.lock.nodes[0].revision.assets[0]
            self.assertEqual(locked.selector.asset_id, "reference-image")
            self.assertEqual(locked.selector.path, "source/assets/reference.png")
            self.assertEqual(locked.blob.identity, pin)
            self.assertEqual(locked.blob.size, len(content))
            cas = FileSystemCAS(Path(temporary) / "asset-cas")
            admitted = snapshot.admit_component_asset(
                result.lock.nodes[0].revision.authoring_identity,
                locked,
                cas,
            )
            self.assertEqual(admitted, locked.blob)
            self.assertEqual(cas.get_bytes(admitted), content)

            asset_path.unlink()
            with self.assertRaises(ComponentLockPlanningError) as raised:
                FilesystemComponentLockPlanner().plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python"),
                    flavor_roots=(flavors,),
                )
            self.assertEqual(raised.exception.code, "component_lock.asset_unavailable")

    def test_single_file_behavior_is_exact_locked_specification_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            shutil.rmtree(component / "specs")
            shutil.rmtree(component / "acceptance")
            path = component / "component.md"
            first_bytes = (
                path.read_bytes()
                .replace(
                    b"specification_provider: openspec\n"
                    b"specification_roots:\n"
                    b"  - specs/spec.md\n",
                    b"",
                )
                .replace(
                    b"acceptance_contracts:\n  - acceptance/execution.json\n",
                    b"acceptance_contracts: []\n",
                )
            )
            path.write_bytes(first_bytes)
            planner = FilesystemComponentLockPlanner()
            first = planner.plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
                flavor_roots=(flavors,),
            )
            first_lock = (
                ComponentLockResolver()
                .resolve(first, expected_input_evidence_identity=first.identity)
                .lock
            )
            first_node = first.nodes[0]

            self.assertEqual(
                tuple(item.uri for item in first_node.specifications),
                ("component.md",),
            )
            self.assertEqual(
                first_node.specifications[0].identity.uri,
                "sha256:" + hashlib.sha256(first_bytes).hexdigest(),
            )

            second_bytes = first_bytes.replace(
                b"deterministic behavior", b"deterministic observable behavior"
            )
            path.write_bytes(second_bytes)
            second = planner.plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
                flavor_roots=(flavors,),
            )
            second_lock = (
                ComponentLockResolver()
                .resolve(second, expected_input_evidence_identity=second.identity)
                .lock
            )

        self.assertNotEqual(first.identity, second.identity)
        self.assertNotEqual(
            first_node.specifications[0].identity,
            second.nodes[0].specifications[0].identity,
        )
        self.assertNotEqual(first_lock.root_revision, second_lock.root_revision)

    def test_plan_resolves_readable_authoring_and_exact_target_flavors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            planner = FilesystemComponentLockPlanner()
            plan = planner.plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
                flavor_roots=(flavors,),
            )
            repeated = planner.plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
                flavor_roots=(flavors,),
            )
            result = ComponentLockResolver().resolve(
                plan, expected_input_evidence_identity=plan.identity
            )

        self.assertEqual(plan.identity, repeated.identity)
        self.assertEqual(result.lock.target_name, "macos-host")
        self.assertEqual(len(result.lock.nodes), 1)
        slots = result.lock.nodes[0].target_flavor_selection.slots
        self.assertEqual(
            {slot.slot.slot_id: slot.selected[0].value for slot in slots},
            {"language": "python", "os": "macos"},
        )
        self.assertEqual(
            result.catalog_audit.component_lock_identity, result.lock.identity
        )

    def test_catalog_identity_binds_effective_prelock_authority_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            planner = FilesystemComponentLockPlanner()
            with mock.patch.object(
                lock_planning,
                "_effective_authority_graph_identity",
                side_effect=("sha256:" + "a" * 64, "sha256:" + "b" * 64),
            ) as effective_graph:
                first = planner.plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python"),
                    flavor_roots=(flavors,),
                )
                second = planner.plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python"),
                    flavor_roots=(flavors,),
                )

        self.assertEqual(effective_graph.call_count, 2)
        self.assertNotEqual(first.catalog_identity, second.catalog_identity)
        self.assertNotEqual(first.identity, second.identity)

    def test_coordinate_qualified_selector_targets_one_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            planner = FilesystemComponentLockPlanner()
            plan = planner.plan(
                component,
                target_name="macos-host",
                flavor_selectors=(
                    "+macos",
                    "component://examples/greeting::+language:python",
                ),
                flavor_roots=(flavors,),
            )
            result = ComponentLockResolver().resolve(
                plan, expected_input_evidence_identity=plan.identity
            )
            slots = result.lock.nodes[0].target_flavor_selection.slots
            self.assertEqual(
                {slot.slot.slot_id: slot.selected[0].value for slot in slots},
                {"language": "python", "os": "macos"},
            )

            with self.assertRaisesRegex(
                ComponentLockPlanningError, "not reachable from the locked root"
            ) as caught:
                planner.plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=(
                        "+macos",
                        "component://examples/other::+language:python",
                    ),
                    flavor_roots=(flavors,),
                )
            self.assertEqual(
                caught.exception.code,
                "component_lock.flavor_selector_component_unknown",
            )

    def test_changed_content_pin_and_missing_flavor_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            planner = FilesystemComponentLockPlanner()
            with self.assertRaisesRegex(
                ComponentLockPlanningError, "Flavor slot 'language' has 0"
            ):
                planner.plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos",),
                    flavor_roots=(flavors,),
                )

            component_md = component / "component.md"
            value = component_md.read_text(encoding="utf-8")
            value = value.replace(
                "uri: skills/implement.json",
                "uri: skills/implement.json\n    pin: sha256:" + "0" * 64,
            )
            component_md.write_text(value, encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(ComponentLockPlanningError, "pin changed"):
                planner.plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python"),
                    flavor_roots=(flavors,),
                )

    def test_explicit_selector_must_apply_and_alias_must_be_unambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary).resolve())
            planner = FilesystemComponentLockPlanner()
            with self.assertRaisesRegex(
                ComponentLockPlanningError, "no effect on any reachable"
            ):
                planner.plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python", "-typo:python"),
                    flavor_roots=(flavors,),
                )

            _flavor_variant(
                flavors / "lang-python",
                flavors / "python-unselected",
                name="implementation-python-unselected",
                value="python-unselected",
            )
            with self.assertRaisesRegex(
                ComponentLockPlanningError, "no effect on any reachable"
            ):
                planner.plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=(
                        "+macos",
                        "+python-unselected",
                        "-python",
                    ),
                    flavor_roots=(flavors,),
                )

            _flavor_variant(
                flavors / "lang-python",
                flavors / "python-alias-collision",
                name="implementation-python-alternate",
                value="python",
            )
            with self.assertRaisesRegex(ComponentLockPlanningError, "ambiguous"):
                planner.plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python"),
                    flavor_roots=(flavors,),
                )

    def test_authority_directory_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            component, flavors = _fixture(root)
            workflow = root / "workflows"
            actual = root / "actual-workflows"
            workflow.rename(actual)
            try:
                workflow.symlink_to(actual, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")

            with self.assertRaisesRegex(ComponentLockPlanningError, "unsafe directory"):
                FilesystemComponentLockPlanner().plan(
                    component,
                    target_name="macos-host",
                    flavor_selectors=("+macos", "+python"),
                    flavor_roots=(flavors,),
                )

    def test_retained_implementation_tree_is_not_cataloged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            component, flavors = _fixture(root)
            retained = component / "implementation" / "source"
            retained.mkdir(parents=True)
            (retained / "legacy.cpp").write_text("int main() {}\n", encoding="utf-8")
            decoy = component / "implementation" / "nested" / "component.md"
            decoy.parent.mkdir(parents=True)
            decoy.write_text(_component_document(), encoding="utf-8", newline="\n")
            link = component / "implementation" / "skills"
            try:
                link.symlink_to(component / "specs", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")

            snapshot = FilesystemComponentLockPlanner().snapshot(
                component, flavor_roots=(flavors,)
            )
            self.assertEqual(
                tuple(
                    path.relative_to(root).as_posix()
                    for path in snapshot.authoring_paths
                ),
                ("greeting/component.md",),
            )
            FilesystemComponentLockPlanner().plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
                flavor_roots=(flavors,),
            )

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_authority_directory_junction_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            component, flavors = _fixture(root)
            workflow = root / "workflows"
            actual = root / "actual-workflows"
            workflow.rename(actual)
            try:
                _create_windows_junction(workflow, actual)
            except OSError as exc:
                self.skipTest(f"junctions are unavailable: {exc}")
            try:
                with self.assertRaisesRegex(
                    ComponentLockPlanningError, "unsafe directory"
                ):
                    FilesystemComponentLockPlanner().plan(
                        component,
                        target_name="windows-host",
                        flavor_selectors=("+macos", "+python"),
                        flavor_roots=(flavors,),
                    )
            finally:
                workflow.rmdir()

    def test_catalog_limits_are_enforced_before_authoring_parse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary).resolve())
            planner = FilesystemComponentLockPlanner()
            cases = (
                ("_MAXIMUM_CATALOG_ENTRIES", 1),
                ("_MAXIMUM_CATALOG_TOTAL_BYTES", 1),
            )
            for constant, limit in cases:
                with (
                    self.subTest(constant=constant),
                    mock.patch.object(lock_planning, constant, limit),
                    self.assertRaisesRegex(ComponentLockPlanningError, "catalog"),
                ):
                    planner.plan(
                        component,
                        target_name="macos-host",
                        flavor_selectors=("+macos", "+python"),
                        flavor_roots=(flavors,),
                    )

    def test_specification_provider_errors_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary).resolve())
            failures = (
                OpenSpecError("openspec.invalid", "injected"),
                OSError("injected concurrent disappearance"),
            )
            for failure in failures:
                with (
                    self.subTest(failure=type(failure).__name__),
                    mock.patch.object(
                        lock_planning.OpenSpecProvider,
                        "load",
                        side_effect=failure,
                    ),
                    self.assertRaises(ComponentLockPlanningError) as raised,
                ):
                    FilesystemComponentLockPlanner().plan(
                        component,
                        target_name="macos-host",
                        flavor_selectors=("+macos", "+python"),
                        flavor_roots=(flavors,),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "component_lock.flavor_specification_invalid",
                )

    def test_selected_flavor_identity_is_independent_of_catalog_paths(self) -> None:
        def plan(root: Path, *, reverse: bool):
            component, flavors = _fixture(root)
            component_path = component / "component.md"
            document = component_path.read_text(encoding="utf-8").replace(
                "  - slot_id: language\n"
                "    axis: implementation.language-ecosystem\n"
                "    cardinality: exactly-one\n",
                "  - slot_id: language\n"
                "    axis: implementation.language-ecosystem\n"
                "    cardinality: one-or-more\n",
            )
            component_path.write_text(document, encoding="utf-8", newline="\n")
            original = flavors / "lang-python"
            original.rename(flavors / ("z-python" if reverse else "a-python"))
            _flavor_variant(
                flavors / ("z-python" if reverse else "a-python"),
                flavors / ("a-alternate" if reverse else "z-alternate"),
                name="implementation-python-alternate",
                value="python-alternate",
            )
            return FilesystemComponentLockPlanner().plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python", "+python-alternate"),
                flavor_roots=(flavors,),
            )

        with (
            tempfile.TemporaryDirectory() as first_temporary,
            tempfile.TemporaryDirectory() as second_temporary,
        ):
            first = plan(Path(first_temporary).resolve(), reverse=False)
            second = plan(Path(second_temporary).resolve(), reverse=True)

        self.assertEqual(first.target_profile_identity, second.target_profile_identity)
        self.assertEqual(
            first.selection_policy_identity, second.selection_policy_identity
        )

    def test_candidate_audit_distinguishes_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary).resolve())
            _flavor_variant(
                flavors / "lang-python",
                flavors / "python-conflict",
                name="implementation-python-conflict",
                value="python-conflict",
                conflicts=("flavor://literate-ai/lang-python",),
            )
            plan = FilesystemComponentLockPlanner().plan(
                component,
                target_name="macos-host",
                flavor_selectors=("+macos", "+python"),
                flavor_roots=(flavors,),
            )
            conflict = next(
                item
                for item in plan.nodes[0].flavor_candidates
                if item.value == "python-conflict"
            )

        self.assertIs(conflict.status, CandidateStatus.CONFLICT)
        self.assertIn("conflicts with selected", conflict.reasons[0])


if __name__ == "__main__":
    unittest.main()
