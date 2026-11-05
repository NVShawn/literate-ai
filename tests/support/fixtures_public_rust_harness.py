"""Authored public project plus real offline verifier dependencies.

Source tests explicitly supply a synthetic framework binding and initialization
origin. Installed journeys omit these seams and use the real installed defaults.
The external coding CLI supplies source; locks and native acceptance stay real.
"""

import json
import shutil
import stat
import sys
from pathlib import Path

import literate_ai
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
)
from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter,
    host_platform_selector,
)
from literate_ai.application.library_artifacts import project_library_import_surface
from literate_ai.contracts import ProjectInitializationOrigin, RepositoryParentSelection
from tests.support.fixtures_rust_library_harness import NativeRustVerifierFixture


def author_public_project(
    root, seed, cargo, rustc, *, invoke, driver=None, receipt=None
):
    project = root / "project"
    project.mkdir()
    selectors = (
        "flavor://literate-ai/lang-rust",
        "flavor://literate-ai/build-cargo",
        "flavor://literate-ai/package-zip",
        host_platform_selector().removeprefix("+"),
    )
    if (driver is None) != (receipt is None):
        raise TypeError("source framework observation seams must be supplied together")
    observations = {}
    if driver is not None:
        observations = {
            "standard_binding_provider": lambda: (driver, receipt),
            "initialization_origin_provider": lambda: ProjectInitializationOrigin(
                repository_url="https://github.com/jordanhubbard/literate-ai.git",
                git_revision="a" * 40,
                distribution_name="literate-ai",
                distribution_version="1.1.0",
            ),
        }
    FilesystemProjectInitializationAdapter(**observations).initialize(
        project,
        project_type="library",
        flavor_selectors=selectors,
        parent_selection=RepositoryParentSelection.root(),
        bootstrap_tools=False,
    )
    component = project / "components/qualified-fixture"
    (component / "interfaces").mkdir(parents=True)
    template = (
        Path(literate_ai.__file__).parent
        / "project_template/samples/hello-component/component.md"
    ).read_text("utf-8")
    header = template.split("---", 2)[1]
    header = header.replace("version: 1.0.0\n", "version: 1.0.0\nkind: library\n", 1)
    header = header.replace('["application", "portable"]', '["library", "portable"]')
    header = header.replace(
        "    version: 1.0.0\nrequires:",
        "    version: 1.0.0\n    interface:\n      uri: interfaces/json.md\nrequires:",
    )
    start = header.index("entrypoints:")
    end = header.index("acceptance_contracts:", start)
    header = (
        header[:start]
        + (
            "entrypoints: []\n"
            "library_imports:\n  - language: rust\n    package: qualified_fixture\n"
            "    capability: application.portable-json\n    module: qualified_fixture\n"
            '    symbols: ["answer"]\n'
        )
        + header[end:]
    )
    (component / "component.md").write_text(
        "---" + header + "---\n# JSON echo library\n\n"
        "### Requirement: Preserve JSON input\n\n"
        "The Rust library SHALL expose `qualified_fixture::answer(&str) -> String`. "
        "The input string is JSON and the returned JSON object SHALL have exactly "
        "`answer` equal to 42 and `echo` equal to the decoded input.\n\n"
        "#### Scenario: Nested input\n\n"
        "- **WHEN** the caller supplies nested JSON values\n"
        "- **THEN** all nested values are preserved in `echo`.\n",
        encoding="utf-8",
    )
    (component / "interfaces/json.md").write_text(
        "# JSON library interface\n\n"
        "`qualified_fixture::answer(&str) -> String` SHALL preserve JSON input "
        "as `echo` and return the integer `answer` 42.\n",
        encoding="utf-8",
    )
    status, result = invoke(["lock", str(component)])
    if status != 0:
        raise AssertionError(result)
    authority = (
        FilesystemLockedGenerationAuthorityReader()
        .read(
            component,
            target_name="host",
        )
        .authority
    )
    node = next(
        row
        for row in authority.lock.nodes
        if row.revision.identity == authority.lock.root_revision
    )
    authoring = authority.root_authoring
    bindings = {
        row.capability: row.interface_identity for row in node.interface_bindings
    }
    surface = project_library_import_surface(
        authoring.coordinate.name,
        "rust",
        tuple((row.name, bindings[row.name]) for row in authoring.provides),
        declarations=authoring.library_imports,
    )
    # The seed's unused candidate lives outside the project. Only actual verifier
    # inputs are copied into the project's declared acceptance coordinates.
    private = root / "verifier-authoring"
    private.mkdir()
    fixture = NativeRustVerifierFixture(private, seed, cargo, rustc)
    acceptance = project / "verification/acceptance"
    acceptance.mkdir(parents=True, exist_ok=True)
    for name in ("vendor", "cargo-home", "workspace"):
        shutil.copytree(fixture.authority / name, acceptance / name)
    for name in ("harness.rs", "Cargo.lock"):
        shutil.copy2(fixture.authority / name, acceptance / name)
    (acceptance / "workspace/.cargo/config.toml").write_text(
        '[source.crates-io]\nreplace-with="fixture-vendor"\n'
        "[source.fixture-vendor]\ndirectory="
        + json.dumps(str(acceptance / "vendor"))
        + "\n[net]\noffline=true\n",
        encoding="utf-8",
    )
    fixture.authority = acceptance
    fixture.surface = surface
    fixture.interface = surface.capabilities[0].interface_identity
    fixture.author_oracle(
        specification_identity=node.revision.specification_set_identity,
        library_manifest="source/Cargo.toml",
    )
    tools = root / "tools"
    tools.mkdir()
    agent = tools / "claude"
    agent.write_text(
        f"#!{sys.executable}\n"
        + Path(__file__).with_name("fake_rust_library_cli.py").read_text("utf-8"),
        encoding="utf-8",
    )
    agent.chmod(agent.stat().st_mode | stat.S_IXUSR)
    status, result = invoke(
        ["project", "documentation-review", str(project), "--record"]
    )
    if status != 0:
        raise AssertionError(result)
    return project, component, fixture, tools
