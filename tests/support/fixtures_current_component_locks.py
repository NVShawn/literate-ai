"""Shared real catalog/lock fixture; no execution or validation trust supplied."""

import json
import shutil
import tempfile
from pathlib import Path

from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.contracts import (
    ProjectDefinition,
    RepositoryLineage,
    RepositoryParentSelection,
)
from literate_ai.projects import LoadedProject
from tests.support.fixtures_test_cli_component_locks import _run
from tests.support.fixtures_test_component_lock_planning import _fixture


def initialize_current_component_lock_fixture(case, *, canonical_project=False):
    temporary = tempfile.TemporaryDirectory()
    case.addCleanup(temporary.cleanup)
    case.root = Path(temporary.name).resolve()
    if canonical_project:
        from literate_ai.adapters.project_initialization import (
            FilesystemProjectInitializationAdapter,
        )
        from literate_ai.contracts import ProjectInitializationOrigin

        case.root = case.root / "project"
        FilesystemProjectInitializationAdapter(
            standard_binding_provider=lambda: None,
            initialization_origin_provider=lambda: ProjectInitializationOrigin(
                "https://example.test/framework.git", "a" * 40, "literate-ai", "1.4.0"
            ),
        ).initialize(
            case.root,
            flavor_selectors=("+python", "+macos"),
            source_intelligence_provider="none",
            empty=True,
            parent_selection=RepositoryParentSelection.root(),
        )
    if canonical_project:
        catalog = Path(temporary.name).resolve() / "catalog"
        _fixture(catalog)
        onboarding = (case.root / "SKILL.md").read_bytes()
        shutil.copytree(catalog, case.root, dirs_exist_ok=True)
        (case.root / "SKILL.md").write_bytes(onboarding)
        case.component, flavors = case.root / "greeting", case.root / "flavors"
        from literate_ai.contracts.authoring_markdown import (
            parse_authoring_markdown,
            render_authoring_markdown,
        )

        document, body = parse_authoring_markdown(
            (case.component / "component.md").read_bytes(),
            source=(case.component / "component.md").as_posix(),
        )
        document["workflow_definition"] = "workflows/sample-host.md"
        document["routing_policy"] = "routing/sample-host.json"
        document["authoring_inputs"] = [
            {
                "kind": "specification-to-source-skill",
                "uri": (
                    "skills/specification-to-source/"
                    "python-portable-application/SKILL.md"
                ),
            }
        ]
        (case.component / "component.md").write_bytes(
            render_authoring_markdown(document, body)
        )
        for name in ("workflows/sample-host.md", "routing/sample-host.json"):
            (case.root / name).write_bytes(
                (Path(__file__).resolve().parents[2] / name).read_bytes()
            )
        for name in (
            "skills/implement.json",
            "workflows/host.json",
            "routing/default.json",
        ):
            (case.root / name).unlink()
    else:
        case.component, flavors = _fixture(case.root)
    definition = json.loads(
        (
            case.root / "literate.project.json"
            if canonical_project
            else Path(__file__).resolve().parents[2] / "literate.project.json"
        ).read_bytes()
    )
    if "test_receipt_policy" not in definition:
        definition["test_receipt_policy"] = json.loads(
            (Path(__file__).resolve().parents[2] / "literate.project.json").read_bytes()
        )["test_receipt_policy"]
    definition["project_id"] = "typed-lock-fixture"
    definition["component_roots"] = ["greeting"]
    definition["default_flavor_selectors"] = []
    definition["test_receipt_policy"]["suite_id"] = "typed-lock-fixture"
    definition["test_receipt_policy"]["minimum_test_count"] = 1
    (case.root / "literate.project.json").write_text(json.dumps(definition))
    case.project = LoadedProject(case.root, ProjectDefinition.from_dict(definition))
    selection = RepositoryParentSelection.root()
    if not canonical_project:
        FilesystemRepositoryLineageStore(case.root).replace(
            selection, RepositoryLineage(selection, (), ()), expected_absent=True
        )
    status, record, error = _run(
        [
            "lock",
            str(case.component),
            "--target",
            "host",
            "--flavor",
            "+python",
            "--flavor",
            "+macos",
            "--flavor-root",
            str(flavors),
            "--json",
        ]
    )
    case.assertEqual(status, 0, (record, error))
