"""Standard-lifecycle conformance support for the composed invoice sample."""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from literate_ai.adapters.cache import (
    FileSystemSourceCache,
    SourceCacheMaterializer,
    SourceCacheResolver,
)
from literate_ai.adapters.dependencies import (
    PortableHostDependencyObserver,
    build_cyclonedx_bom,
    validate_cyclonedx_bom,
)
from literate_ai.adapters.generation_preparation import (
    LockedComponentModelSelectionAdapter,
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.intelligence import (
    DisabledGenerationIndexer,
    generated_source_tree_identity,
)
from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalIndependentAcceptanceCase,
    LocalSourceTreeRegistry,
    StandardBazelTarget,
    local_generated_source_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_local import LocalGeneratedSourceCustody
from literate_ai.adapters.models import CodingCliSelection
from literate_ai.adapters.models.coding_cli import (
    _acceptance_argument_vectors,
    _acceptance_result_shape,
)
from literate_ai.adapters.source_generation import (
    CachedCodingCliSourceGenerationRunner,
    CodingCliSourceGenerationInvocation,
)
from literate_ai.adapters.standard_project import (
    PlannedStandardProject,
    StandardProjectExecutionRequest,
    assemble_filesystem_standard_project_runtime,
    compose_filesystem_standard_lifecycle_checkpoints,
    compose_filesystem_standard_source_cache,
    project_standard_toolchain_closure,
)
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentLifecycleCommand,
    CycloneDxLifecycle,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheModelBinding,
    SourceCacheRootKind,
    SourceCacheTarget,
    SourceDerivationCacheKey,
    canonical_json_bytes,
    source_cache_model_selector,
)
from literate_ai.contracts.executable_components import (
    ComponentChangeSurface,
    ComponentInvalidationDecision,
    GeneratedSourceCandidate,
    GenerationComplexityBudget,
    SourceGenerationProvenance,
    SourceGenerationRunOutput,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    GENERATED_TEST_SUITE_SCHEMA,
    MAJOR_REBUILD_GENERATION_MODE,
    validate_generated_test_suite,
)
from literate_ai.storage import FileSystemCAS
from tests.conformance.support.runtime_oracles import create_post_build_probe

_SOURCE_RUNNER = (
    "import base64,os,runpy,sys; "
    "root=os.path.join(sys.argv[1],sys.argv[2]); os.chdir(root); "
    "decode=lambda value: base64.urlsafe_b64decode("
    "value.removeprefix('litai-b64:')).decode('utf-8') "
    "if value.startswith('litai-b64:') else value; "
    "runtime=sys.argv[4:]; "
    "sys.argv=[sys.argv[3],*(decode(value) for value in runtime[-1:])]; "
    "runpy.run_path(sys.argv[0], run_name='__main__')"
)
_ARCHIVE_RUNNER = (
    "import base64,os,runpy,sys; "
    "path=os.path.join(sys.argv[1],sys.argv[2]); "
    "decode=lambda value: base64.urlsafe_b64decode("
    "value.removeprefix('litai-b64:')).decode('utf-8') "
    "if value.startswith('litai-b64:') else value; "
    "runtime=sys.argv[3:]; "
    "sys.argv=[path,*(decode(value) for value in runtime[-1:])]; "
    "runpy.run_path(path,run_name='__main__')"
)


class _AuthoritySnapshot:
    def __init__(self, authority, catalog) -> None:
        self.authority = authority
        self.catalog = catalog

    def component_content(self, authoring_identity, reference):
        return self.catalog.component_content(authoring_identity, reference)

    def flavor_content(self, flavor_revision, reference):
        return self.catalog.flavor_content(flavor_revision, reference)

    def require_unchanged(self) -> None:
        self.catalog.require_unchanged()


class _ServiceStackProjectAcceptanceOracle:
    """Verifier-only live probe, created after the packaged application exists."""

    def __init__(self, root_revision: ContentIdentity) -> None:
        self.root_revision = root_revision

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/service-stack-project-oracle@1",
                "root_revision": self.root_revision.uri,
                "algorithm": "tests.conformance.runtime-oracles/service-stack@1",
            }
        )

    def cases(self, component_lock) -> tuple[LocalIndependentAcceptanceCase, ...]:
        if component_lock.root_revision != self.root_revision:
            raise RuntimeError("service-stack oracle received a different root")
        probe = create_post_build_probe("service-stack")
        return (
            LocalIndependentAcceptanceCase.create(
                probe.case_id, probe.arguments, probe.expected_result
            ),
        )


def _generated_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and ".codegraph" not in path.relative_to(root).parts
    }


class _GeneratedSourceTreeRegistry:
    """Custody for coding-CLI trees whose identity excludes index sidecars."""

    def __init__(self) -> None:
        self.paths: dict[str, Path] = {}
        self._evidence: dict[str, LocalGeneratedSourceCustody] = {}

    def register(
        self,
        candidate,
        root: Path,
        *,
        source_generation_identity=None,
        recipe=None,
    ) -> None:
        path = root.resolve(strict=True)
        identity = generated_source_tree_identity(_generated_files(path))
        if identity != candidate.tree_identity.uri:
            raise RuntimeError("coding-CLI candidate differs from generated files")
        self.paths[candidate.tree_identity.uri] = path
        if recipe is not None:
            source_content = path.joinpath(
                *Path(CYCLONEDX_SOURCE_SBOM_PATH).parts
            ).read_bytes()
            source_bom = validate_cyclonedx_bom(
                source_content,
                lifecycle=CycloneDxLifecycle.SOURCE,
                managed_graph=recipe.managed_sbom_graph,
            )
            suite_content = path.joinpath(
                *Path(GENERATED_TEST_SUITE_PATH).parts
            ).read_bytes()
            suite = validate_generated_test_suite(
                suite_content,
                recipe_identity=recipe.identity,
                specification_references=recipe.non_acceptance_document_paths,
                acceptance_arguments=_acceptance_argument_vectors(recipe),
                result_shape=_acceptance_result_shape(recipe),
            )
            self._evidence[candidate.tree_identity.uri] = LocalGeneratedSourceCustody(
                candidate,
                source_generation_identity or candidate.identity,
                recipe.managed_sbom_graph,
                source_bom,
                source_content,
                suite,
                suite_content,
            )

    def resolve(self, identity: ContentIdentity) -> Path:
        path = self.paths[identity.uri]
        if generated_source_tree_identity(_generated_files(path)) != identity.uri:
            raise RuntimeError("coding-CLI generated source changed")
        return path

    def evidence(self, identity: ContentIdentity) -> LocalGeneratedSourceCustody:
        self.resolve(identity)
        return self._evidence[identity.uri]


def _budget() -> GenerationComplexityBudget:
    return GenerationComplexityBudget(
        max_prompt_bytes=1_000_000,
        max_estimated_tokens=250_000,
        max_document_count=100,
        max_direct_interface_bytes=100_000,
        max_dependency_fan_in=32,
        max_model_attempts=1,
        max_wall_time_ms=60_000,
        max_model_tokens=100_000,
        max_cost_microunits=10_000_000,
    )


def _local_selection() -> CodingCliSelection:
    executable = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    return CodingCliSelection("codex", str(executable), f"sha256:{digest}")


def _command_contracts(
    execution_plan,
    by_revision: dict[str, str],
    *,
    live: bool,
    invocations: dict[str, str] | None = None,
    bazel_executable: str | None = None,
) -> tuple[
    tuple[ComponentCommandContract, ...],
    tuple[LocalComponentToolBinding, ...],
    tuple[StandardBazelTarget, ...],
]:
    python_binding = LocalComponentToolBinding(sys.executable)
    bazel_binding = (
        LocalComponentToolBinding(bazel_executable)
        if bazel_executable is not None
        else None
    )
    contracts = []
    targets = []
    for plan in execution_plan.generation_plans:
        name = by_revision[plan.component_revision.uri]
        export_id = f"python-{name}"

        def run_script(
            relative: str, *arguments: str, export: str = export_id
        ) -> tuple[str, ...]:
            return (
                "{tool}",
                "-c",
                _SOURCE_RUNNER,
                "{artifact_root}",
                export,
                relative,
                *arguments,
            )

        if live:
            if invocations is None or plan.component_revision.uri not in invocations:
                raise RuntimeError(f"missing verifier invocation for {name}")
            probe = run_script("source/main.py", "--litai-smoke")
        else:
            probe = (
                run_script("main.py")
                if plan.component_revision == execution_plan.root_revision
                else run_script("test_component.py")
            )
        if bazel_binding is None:
            build = (
                "{tool}",
                "-c",
                "import compileall,shutil,sys; "
                "shutil.copytree(sys.argv[1], sys.argv[3]); "
                "raise SystemExit("
                "not compileall.compile_dir(sys.argv[3], quiet=1))",
                "{source_root}",
                "{object_root}",
                "{export_path}",
            )
            build_system_resolver_identity = canonical_identity(
                {"resolver": "python-compileall@1"}
            )
            build_system_toolchain_identity = python_binding.toolchain_identity
            locked_build_authority_identity = canonical_identity(
                {
                    "service-stack-command-authority": name,
                    "commands": [
                        list(build),
                        list(probe if live else run_script("test_component.py")),
                        list(probe),
                    ],
                }
            )
        else:
            target = StandardBazelTarget(
                plan.component_revision,
                canonical_identity({"resolver": "bazel-local-target@1"}),
                bazel_binding.toolchain_identity,
                "//:litai_artifact",
                "run.pyz",
            )
            targets.append(target)
            build = (
                "{tool}",
                "build",
                target.target_label,
                "{source_root}",
                "{object_root}",
                "{export_path}",
            )
            build_system_resolver_identity = target.build_system_resolver_identity
            build_system_toolchain_identity = target.build_system_toolchain_identity
            locked_build_authority_identity = target.identity

            def run_archive(
                *arguments: str, export: str = export_id
            ) -> tuple[str, ...]:
                return (
                    "{tool}",
                    "-c",
                    _ARCHIVE_RUNNER,
                    "{artifact_root}",
                    export,
                    *arguments,
                )

            probe = (
                run_archive("--litai-smoke")
                if live
                else run_archive()
                if plan.component_revision == execution_plan.root_revision
                else run_archive("--litai-test")
            )
        commands = (
            ComponentLifecycleCommand(ComponentCommandPhase.BUILD, build),
            ComponentLifecycleCommand(
                ComponentCommandPhase.TEST,
                probe
                if live or bazel_binding is not None
                else run_script("test_component.py"),
            ),
            ComponentLifecycleCommand(ComponentCommandPhase.EXECUTE, probe),
        )
        if bazel_binding is not None:
            commands = (
                commands[0],
                ComponentLifecycleCommand(
                    ComponentCommandPhase.TEST,
                    run_archive("--litai-test"),
                ),
                commands[2],
            )
        contracts.append(
            ComponentCommandContract(
                component_revision=plan.component_revision,
                locked_build_authority_identity=locked_build_authority_identity,
                build_system_resolver_identity=build_system_resolver_identity,
                build_system_toolchain_identity=build_system_toolchain_identity,
                language_compiler_identity=python_binding.toolchain_identity,
                language_runtime_identity=python_binding.toolchain_identity,
                commands=commands,
                tool_bindings=tuple(
                    ComponentCommandToolBinding(
                        phase,
                        bazel_binding.toolchain_identity
                        if phase == ComponentCommandPhase.BUILD
                        and bazel_binding is not None
                        else python_binding.toolchain_identity,
                    )
                    for phase in ComponentCommandPhase
                ),
                artifact_export=ComponentArtifactExportShape(
                    export_id,
                    "executable"
                    if plan.component_revision == execution_plan.root_revision
                    else "python-library",
                    canonical_identity({"abi": "python-module@1"}),
                    canonical_identity({"target": "local-host"}),
                    "application/vnd.literate-ai.python-tree",
                    canonical_identity({"producer": "python-compileall@1"}),
                ),
            )
        )
    bindings = (
        (python_binding,) if bazel_binding is None else (bazel_binding, python_binding)
    )
    return tuple(contracts), bindings, tuple(targets)


def _project_toolchain_closure(
    execution_plan,
    *,
    contracts: tuple[ComponentCommandContract, ...],
    tool_bindings: tuple[LocalComponentToolBinding, ...],
    bazel_targets: tuple[StandardBazelTarget, ...],
    provider_environment: dict[str, tuple[str, str]],
    observation_root: Path,
):
    observation_root.mkdir(parents=True, exist_ok=True)
    observation = PortableHostDependencyObserver(
        toolchain_commands=tuple(binding.command for binding in tool_bindings),
        lifecycle_commands=(),
    ).observe(
        {"artifact_path": str(observation_root)},
        root_ref=f"urn:literate-ai:component:{execution_plan.root_revision.digest}",
    )
    return project_standard_toolchain_closure(
        execution_plan,
        contracts=contracts,
        tool_bindings=tool_bindings,
        dependency_observation=observation,
        observer_identity=canonical_identity(
            {
                "schema": "literate-ai/standard-toolchain-observer@1",
                "adapter": "portable-host-dependency-observer@1",
            }
        ),
        bazel_targets=bazel_targets,
        provider_environment=provider_environment,
    )


_SOURCES = {
    "money-calculation": {
        "main.py": """import json, sys
def calculate(subtotal_cents, discount_basis_points):
    discount = (subtotal_cents * discount_basis_points + 5000) // 10000
    return {"subtotal_cents": subtotal_cents, "discount_cents": discount,
            "total_cents": subtotal_cents - discount}
if __name__ == "__main__":
    print(json.dumps(calculate(*json.loads(sys.argv[1])), sort_keys=True,
                     separators=(",", ":")))
""",
        "test_component.py": """from main import calculate
expected = {"subtotal_cents": 999, "discount_cents": 125,
            "total_cents": 874}
assert calculate(999, 1250) == expected
print("money-calculation-ok")
""",
    },
    "invoice-service": {
        "main.py": """import json, os, subprocess, sys
def calculate(subtotal_cents, discount_basis_points):
    provider = os.environ["LITAI_CAPABILITY_MONEY_CALCULATION"]
    output = subprocess.check_output(
        [sys.executable, provider,
         json.dumps([subtotal_cents, discount_basis_points], separators=(",", ":"))],
        text=True)
    return json.loads(output)
def invoice(items, discount_basis_points):
    subtotal = sum(item["quantity"] * item["unit_price_cents"] for item in items)
    result = calculate(subtotal, discount_basis_points)
    return {**result, "line_count": len(items),
            "unit_count": sum(item["quantity"] for item in items)}
if __name__ == "__main__":
    print(json.dumps(invoice(**json.loads(sys.argv[1])[0]), sort_keys=True,
                     separators=(",", ":")))
""",
        "test_component.py": """from main import invoice
items = [{"sku":"widget", "quantity":2, "unit_price_cents":1299},
         {"sku":"cable", "quantity":3, "unit_price_cents":499}]
expected = {"subtotal_cents":4095, "discount_cents":410,
            "total_cents":3685, "line_count":2, "unit_count":5}
assert invoice(items, 1000) == expected
print("invoice-service-ok")
""",
    },
    "service-stack": {
        "main.py": """import json, os, subprocess, sys
REQUEST = {"items":[
    {"sku":"widget", "quantity":2, "unit_price_cents":1299},
    {"sku":"cable", "quantity":3, "unit_price_cents":499}],
    "discount_basis_points":1000}
def service(request):
    provider = os.environ["LITAI_CAPABILITY_INVOICE_SERVICE"]
    output = subprocess.check_output(
        [sys.executable, provider, json.dumps([request], separators=(",", ":"))],
        text=True)
    return json.loads(output)
if __name__ == "__main__":
    request = REQUEST if len(sys.argv) == 1 else json.loads(sys.argv[1])[0]
    print(json.dumps(service(request), sort_keys=True, separators=(",", ":")))
""",
        "test_component.py": """import json, subprocess, sys
observed = json.loads(subprocess.check_output([sys.executable, "main.py"], text=True))
expected = {"subtotal_cents":4095, "discount_cents":410,
            "total_cents":3685, "line_count":2, "unit_count":5}
assert observed == expected
print("service-stack-ok")
""",
    },
}

_SPECIFICATION_TITLES = {
    "money-calculation": "Money Calculation",
    "invoice-service": "Invoice Service",
    "service-stack": "Composable Invoice Service",
}


def _generated_suite_cases(
    name: str, specification_ref: str
) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for index, category in enumerate(("example", "boundary", "invariant"), start=1):
        if name == "money-calculation":
            subtotal = 700 + index * 137
            basis_points = 300 + index * 211
            discount = (subtotal * basis_points + 5000) // 10000
            arguments = [
                {
                    "subtotal_cents": subtotal,
                    "discount_basis_points": basis_points,
                }
            ]
            expected = {
                "subtotal_cents": subtotal,
                "discount_cents": discount,
                "total_cents": subtotal - discount,
            }
        else:
            quantity = index + 1
            unit_price = 211 + index * 73
            basis_points = 250 + index * 175
            subtotal = quantity * unit_price
            discount = (subtotal * basis_points + 5000) // 10000
            arguments = [
                {
                    "items": [
                        {
                            "sku": f"generated-{index}",
                            "quantity": quantity,
                            "unit_price_cents": unit_price,
                        }
                    ],
                    "discount_basis_points": basis_points,
                }
            ]
            expected = {
                "subtotal_cents": subtotal,
                "discount_cents": discount,
                "total_cents": subtotal - discount,
                "line_count": 1,
                "unit_count": quantity,
            }
        cases.append(
            {
                "case_id": f"{name}-{category}",
                "category": category,
                "specification_refs": [specification_ref],
                "arguments": arguments,
                "expected_result": expected,
            }
        )
    return cases


def _generated_test_source(name: str, cases: list[dict[str, object]]) -> str:
    callable_name = {
        "money-calculation": "calculate",
        "invoice-service": "invoice",
        "service-stack": "service",
    }[name]
    invocation = (
        f"{callable_name}(case['arguments'][0])"
        if name == "service-stack"
        else f"{callable_name}(**case['arguments'][0])"
    )
    return (
        "import json\n"
        f"from main import {callable_name}\n"
        f"CASES = {cases!r}\n"
        "results = []\n"
        "for case in CASES:\n"
        f"    observed = {invocation}\n"
        "    assert observed == case['expected_result']\n"
        "    results.append({'case_id': case['case_id'], 'outcome': 'passed'})\n"
        "print(json.dumps({'schema': 'literate-ai/generated-test-results@1', "
        "'cases': results}, sort_keys=True, separators=(',', ':')))\n"
    )


_BAZEL_ARCHIVE_MAIN = """import runpy, sys
testing = len(sys.argv) > 1 and sys.argv[1] == "--litai-test"
sys.argv = [sys.argv[0], *sys.argv[2:]] if testing else sys.argv
runpy.run_module("test_component" if testing else "main", run_name="__main__")
"""

_BAZEL_ARCHIVE_COMPILER = """import pathlib, py_compile, sys, tempfile, zipfile
output = pathlib.Path(sys.argv[1])
sources = sorted((pathlib.Path(value) for value in sys.argv[2:]), key=lambda p: p.name)
with tempfile.TemporaryDirectory() as directory:
    compiled = pathlib.Path(directory)
    for source in sources:
        py_compile.compile(
            str(source), cfile=str(compiled / (source.name + "c")),
            dfile=source.name, doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
        )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for bytecode in sorted(compiled.iterdir()):
            entry = zipfile.ZipInfo(bytecode.name, (1980, 1, 1, 0, 0, 0))
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, bytecode.read_bytes())
"""

_BAZEL_SERVICE_TEST = """import contextlib, io, json, runpy
stream = io.StringIO()
with contextlib.redirect_stdout(stream):
    runpy.run_module("main", run_name="__main__")
observed = json.loads(stream.getvalue())
expected = {"subtotal_cents":4095, "discount_cents":410,
            "total_cents":3685, "line_count":2, "unit_count":5}
assert observed == expected
print("service-stack-ok")
"""


def _write_bazel_archive_authority(root: Path) -> None:
    """Generate one host-Python bytecode archive target into source custody."""

    executable = Path(sys.executable).resolve(strict=True)
    command = (
        f"{shlex.quote(executable.as_posix())} $(location compile_archive.py) $@ "
        "$(location main.py) $(location test_component.py) "
        "$(location __main__.py)"
    )
    command_bat = (
        f"{subprocess.list2cmdline([str(executable)])} "
        "$(location compile_archive.py) $@ $(location main.py) "
        "$(location test_component.py) $(location __main__.py)"
    )
    (root / "MODULE.bazel").write_text(
        'module(name = "litai_generated_component", version = "1.0.0")\n',
        encoding="utf-8",
    )
    (root / "BUILD.bazel").write_text(
        "genrule(\n"
        '    name = "litai_artifact",\n'
        '    srcs = ["main.py", "test_component.py", "__main__.py", '
        '"compile_archive.py"],\n'
        '    outs = ["run.pyz"],\n'
        f"    cmd = {json.dumps(command)},\n"
        f"    cmd_bat = {json.dumps(command_bat)},\n"
        ")\n",
        encoding="utf-8",
    )
    (root / "__main__.py").write_text(_BAZEL_ARCHIVE_MAIN, encoding="utf-8")
    (root / "compile_archive.py").write_text(_BAZEL_ARCHIVE_COMPILER, encoding="utf-8")


class _SpecificationBoundGenerator:
    """Deterministic sample model: generate only from one node's bounded prompt."""

    def __init__(self, root_revision, *, build_system: str = "native") -> None:
        self.root_revision = root_revision
        self.build_system = build_system
        self.calls: list[str] = []
        self._cache_keys: dict[str, SourceDerivationCacheKey] = {}

    def planned_cache_key(self, prepared) -> SourceDerivationCacheKey:
        return SourceDerivationCacheKey(
            recipe_identity=ContentIdentity.parse_uri(prepared.recipe.identity),
            execution_plan_identity=canonical_identity(
                {
                    "schema": "literate-ai/deterministic-sample-model-plan@1",
                    "component_generation_plan_identity": prepared.plan.identity.uri,
                    "build_system": self.build_system,
                }
            ),
            coding_cli_tool_binding_identity=canonical_identity(
                {"tool": "deterministic-specification-bound-sample-model@1"}
            ),
            model_binding=SourceCacheModelBinding(
                "deterministic-sample", "specification-bound-v1"
            ),
            request_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(prepared.request.prompt).hexdigest()
            ),
        )

    def cache_key_for_candidate(
        self, candidate: GeneratedSourceCandidate
    ) -> SourceDerivationCacheKey:
        return self._cache_keys[candidate.identity.uri]

    def __call__(self, prepared) -> SourceGenerationRunOutput:
        name = prepared.definition.coordinate.name
        prompt = prepared.request.prompt.decode("utf-8")
        if _SPECIFICATION_TITLES.get(name) not in prompt or name not in _SOURCES:
            raise RuntimeError(
                "node generation prompt does not contain its own specification"
            )
        foreign_titles = {
            title
            for component, title in _SPECIFICATION_TITLES.items()
            if component != name and f"# {title}\n" in prompt
        }
        if foreign_titles:
            raise RuntimeError(
                "node generation prompt flattened another Component spec"
            )
        root = Path(prepared.workspace.locator)
        source_root = root / "source" if self.build_system == "bazel" else root
        source_root.mkdir(parents=True, exist_ok=True)
        for relative, content in _SOURCES[name].items():
            if (
                self.build_system == "bazel"
                and name == "service-stack"
                and relative == "test_component.py"
            ):
                content = _BAZEL_SERVICE_TEST
            (source_root / relative).write_text(content, encoding="utf-8")
        if self.build_system == "bazel":
            _write_bazel_archive_authority(source_root)
        specification_refs = tuple(prepared.recipe.non_acceptance_document_paths)
        if not specification_refs:
            raise RuntimeError("deterministic generation recipe has no specification")
        generated_cases = _generated_suite_cases(name, specification_refs[0])
        suite_document = {
            "schema": GENERATED_TEST_SUITE_SCHEMA,
            "recipe_identity": prepared.recipe.identity,
            "generation_mode": MAJOR_REBUILD_GENERATION_MODE,
            "cases": generated_cases,
        }
        suite_content = canonical_json_bytes(suite_document)
        suite_path = root.joinpath(*Path(GENERATED_TEST_SUITE_PATH).parts)
        suite_path.parent.mkdir(parents=True, exist_ok=True)
        suite_path.write_bytes(suite_content)
        suite = validate_generated_test_suite(
            suite_content,
            recipe_identity=prepared.recipe.identity,
            specification_references=specification_refs,
            acceptance_arguments=_acceptance_argument_vectors(prepared.recipe),
            result_shape=_acceptance_result_shape(prepared.recipe),
        )
        (source_root / "test_component.py").write_text(
            _generated_test_source(name, generated_cases), encoding="utf-8"
        )
        source_bom_content, source_bom = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=prepared.recipe.managed_sbom_graph,
        )
        source_bom_path = root.joinpath(*Path(CYCLONEDX_SOURCE_SBOM_PATH).parts)
        source_bom_path.parent.mkdir(parents=True, exist_ok=True)
        source_bom_path.write_bytes(source_bom_content)
        tree = local_generated_source_tree_identity(root)
        request = prepared.request.request
        recipe_identity = ContentIdentity.parse_uri(prepared.recipe.identity)
        candidate = GeneratedSourceCandidate(
            prepared.plan.component_revision,
            request.identity,
            self.planned_cache_key(prepared).request_identity,
            prepared.plan.identity,
            prepared.plan.generation_key.identity,
            request.context_manifest_identity,
            request.prompt_identity,
            recipe_identity,
            prepared.workspace.allocation_identity,
            tree,
            canonical_identity({"bundle": tree.uri}),
            canonical_identity({"manifest": tree.uri}),
            source_bom.bom_identity,
            ContentIdentity.parse_uri(suite.content_identity),
        )
        provenance = SourceGenerationProvenance(
            request.identity,
            candidate.planned_coding_cli_request_identity,
            prepared.recipe.component_lock_identity,
            self.root_revision,
            prepared.plan.component_revision,
            prepared.plan.identity,
            prepared.plan.generation_key.identity,
            request.context_manifest_identity,
            request.prompt_identity,
            recipe_identity,
            prepared.workspace.allocation_identity,
            canonical_identity({"readiness": prepared.plan.component_revision.uri}),
            (canonical_identity({"route": name}),),
            (canonical_identity({"model-output": tree.uri}),),
            candidate.identity,
        )
        self.calls.append(name)
        output = SourceGenerationRunOutput(
            candidate, candidate.identity, provenance, provenance.identity
        )
        self._cache_keys[candidate.identity.uri] = self.planned_cache_key(prepared)
        return output


def execute_standard_service_stack(
    *,
    authority,
    catalog,
    scratch: Path,
    object_root: Path | None = None,
    build_system: str = "bazel",
    source_cache_root: Path | None = None,
    checkpoint_root: Path | None = None,
    include_lifecycle_result: bool = False,
) -> dict[str, object]:
    """Generate, compile, node-test, and run the Python three-Component stack."""

    if build_system not in {"native", "bazel"}:
        raise ValueError("build_system must be 'native' or 'bazel'")
    bazel_executable = None
    if build_system == "bazel":
        bazel_executable = shutil.which("bazel") or shutil.which("bazelisk")
        if bazel_executable is None:
            raise RuntimeError("Bazel build selected but bazel was not found on PATH")
    snapshot = _AuthoritySnapshot(authority, catalog)
    models = {
        node.revision.identity.uri: canonical_identity(
            {"sample-model": node.revision.coordinate.name}
        )
        for node in authority.lock.nodes
    }
    source_registry = LocalSourceTreeRegistry()
    generator = _SpecificationBoundGenerator(
        authority.lock.root_revision, build_system=build_system
    )
    from literate_ai.application.standard_project_services import (
        StandardProjectApplicationService,
    )

    execution_plan = StandardProjectApplicationService.plan(
        authority.lock, model_identities=models
    )
    by_revision = {
        node.revision.identity.uri: node.revision.coordinate.name
        for node in authority.lock.nodes
    }
    contracts, tool_bindings, bazel_targets = _command_contracts(
        execution_plan,
        by_revision,
        live=False,
        bazel_executable=bazel_executable,
    )
    provider_environment = {
        "python-money-calculation": (
            "LITAI_CAPABILITY_MONEY_CALCULATION",
            "python-money-calculation"
            if build_system == "bazel"
            else "python-money-calculation/main.py",
        ),
        "python-invoice-service": (
            "LITAI_CAPABILITY_INVOICE_SERVICE",
            "python-invoice-service"
            if build_system == "bazel"
            else "python-invoice-service/main.py",
        ),
    }
    toolchain_closure = _project_toolchain_closure(
        execution_plan,
        contracts=contracts,
        tool_bindings=tool_bindings,
        bazel_targets=bazel_targets,
        provider_environment=provider_environment,
        observation_root=scratch / "standard-toolchain-observation",
    )
    standard_indexer = (
        None
        if checkpoint_root is None
        else DisabledGenerationIndexer(
            source_registry,
            artifact_root=None,
        )
    )
    runtime = assemble_filesystem_standard_project_runtime(
        generator=generator,
        object_root=(scratch / "standard-objects")
        if object_root is None
        else object_root,
        toolchain_closure=toolchain_closure,
        source_trees=source_registry,
        indexer=standard_indexer,
        independent_acceptance_oracle=_ServiceStackProjectAcceptanceOracle(
            authority.lock.root_revision
        ),
    )
    if checkpoint_root is not None:
        runtime = compose_filesystem_standard_lifecycle_checkpoints(
            runtime,
            checkpoint_root=checkpoint_root,
        )
    if source_cache_root is not None:
        target = SourceCacheTarget(
            target_id="standard-service-stack",
            root_kind=SourceCacheRootKind.OPERATOR_BOUND,
            root_reference="standard-service-stack-cache",
        )
        configuration = SourceCacheConfiguration(
            mode=SourceCacheMode.READ_WRITE,
            targets=(target,),
            write_target_id=target.target_id,
            require_unique=True,
        )
        store = FileSystemSourceCache(target.target_id, source_cache_root)
        resolver = SourceCacheResolver(configuration, {target.target_id: store})
        runtime = compose_filesystem_standard_source_cache(
            runtime,
            generator=generator,
            resolver=resolver,
            caller_cas=FileSystemCAS(scratch / "standard-cas"),
            indexer=DisabledGenerationIndexer(
                source_registry,
                artifact_root=None,
            ),
            materializer=SourceCacheMaterializer(),
        )
    revisions = tuple(
        plan.component_revision for plan in execution_plan.generation_plans
    )
    invalidation = ComponentInvalidationDecision(
        "service-stack-standard-adoption",
        authority.lock.root_revision,
        (
            ComponentChangeSurface.LOCAL_AUTHORITY
            if source_cache_root is None and checkpoint_root is None
            else ComponentChangeSurface.SOURCE_REPLACEMENT
        ),
        revisions if source_cache_root is None and checkpoint_root is None else (),
        revisions,
        revisions,
    )
    executed = runtime.execute(
        snapshot,
        StandardProjectExecutionRequest(
            PlannedStandardProject(_local_selection(), execution_plan),
            scratch / "standard-workspaces",
            invalidation,
            max_parallelism=2,
            budget=_budget(),
        ),
    )
    result = executed.lifecycle
    ports = runtime.lifecycle_ports
    if not result.successful:
        failures = {
            by_revision[item.component_revision.uri]: (
                None
                if item.failure_evidence is None
                else item.failure_evidence.to_dict()
            )
            for item in result.node_results
        }
        raise RuntimeError(
            "Standard service-stack lifecycle failed: "
            f"{failures}; generation calls={generator.calls}; "
            f"diagnostics={ports.failure_diagnostics}"
        )
    root_stdout = ports.execution_stdout[authority.lock.root_revision.uri]
    expected = {
        "discount_cents": 410,
        "line_count": 2,
        "subtotal_cents": 4095,
        "total_cents": 3685,
        "unit_count": 5,
    }
    if json.loads(root_stdout) != expected:
        raise RuntimeError(
            "Standard service-stack executable returned the wrong invoice"
        )
    results = {
        by_revision[item.component_revision.uri]: item for item in result.node_results
    }
    if any(
        item.build_evidence is None
        or item.generated_test_evidence is None
        or item.execution_evidence is None
        or item.acceptance_evidence is None
        for item in results.values()
    ):
        raise RuntimeError(
            "Standard service-stack omitted strict post-source stage evidence"
        )
    exports = {name: item.exports[0] for name, item in results.items()}
    report: dict[str, object] = {
        "build_system": build_system,
        "bazel_target_identities": tuple(
            target.identity.uri for target in bazel_targets
        ),
        "generation_calls": tuple(generator.calls),
        "source_generation_dispositions": {
            name: item.source_generation.disposition.value
            for name, item in sorted(results.items())
        },
        "source_cache_publications": {
            name: (
                None
                if item.source_cache_publication_identity is None
                else item.source_cache_publication_identity.uri
            )
            for name, item in sorted(results.items())
        },
        "source_index_identities": {
            name: item.index_identity.uri for name, item in sorted(results.items())
        },
        "root_result": json.loads(root_stdout),
        "build_cache": ports.build_cache_report(),
        "tested_components": tuple(
            sorted(
                name for name, item in results.items() if item.test_identity is not None
            )
        ),
        "provider_artifact_edges": {
            name: tuple(
                identity.uri for identity in export.dependency_artifact_identities
            )
            for name, export in exports.items()
        },
        "dependency_artifact_counts": {
            name: len(export.dependency_artifact_identities)
            for name, export in exports.items()
        },
        "artifact_identities": {
            name: export.identity.uri for name, export in exports.items()
        },
        "post_source_evidence": {
            name: {
                "build": item.build_evidence.identity.uri,
                "source_sbom": item.build_evidence.source_sbom.bom_identity.uri,
                "resolved_sbom": item.build_evidence.resolved_sbom.bom_identity.uri,
                "generated_tests": item.generated_test_evidence.identity.uri,
                "generated_test_case_count": len(item.generated_test_evidence.cases),
                "execution": item.execution_evidence.identity.uri,
                "acceptance": item.acceptance_evidence.identity.uri,
            }
            for name, item in sorted(results.items())
        },
    }
    if include_lifecycle_result:
        report["_lifecycle_result"] = result
    return report


def execute_live_standard_service_stack(
    *,
    sample_root: Path,
    authority,
    catalog,
    scratch: Path,
    object_root: Path,
    source_generator,
    invocation_arguments: dict[str, list[object]],
    pipeline_model: str | None = None,
    include_lifecycle_result: bool = False,
) -> dict[str, object]:
    """Run the authenticated Python stack as three genuine coding-CLI nodes."""

    from tests.conformance.support.sample_runner import (
        _locked_standard_sample_model_bindings,
        _standard_sample_stage_request,
    )

    started = time.monotonic()
    snapshot = _AuthoritySnapshot(authority, catalog)
    selection = source_generator.selection
    model_bindings = _locked_standard_sample_model_bindings(
        snapshot,
        coding_cli=selection.name,
        pipeline_model=pipeline_model,
    )
    models = {
        revision: binding.identity for revision, binding in model_bindings.items()
    }
    from literate_ai.application.standard_project_services import (
        StandardProjectApplicationService,
    )

    execution_plan = StandardProjectApplicationService.plan(
        authority.lock, model_identities=models
    )
    locked_by_revision = {
        node.revision.identity.uri: node.revision for node in authority.lock.nodes
    }
    by_revision = {
        node.revision.identity.uri: node.revision.coordinate.name
        for node in authority.lock.nodes
    }
    missing_invocations = set(by_revision.values()) - set(invocation_arguments)
    if missing_invocations:
        raise RuntimeError(
            "missing verifier-owned service-stack invocations: "
            + ", ".join(sorted(missing_invocations))
        )
    invocation_vectors = {
        revision: invocation_arguments[name] for revision, name in by_revision.items()
    }
    invocations = {
        revision: "litai-b64:"
        + base64.urlsafe_b64encode(canonical_json_bytes(arguments)).decode("ascii")
        for revision, arguments in invocation_vectors.items()
    }
    execution_plans = {}

    def invocation(node):
        revision = locked_by_revision[node.plan.component_revision.uri]
        model_plan, request = _standard_sample_stage_request(
            sample_root=sample_root,
            node=node,
            revision=revision,
            source_generator=source_generator,
            forbidden_acceptance_arguments=invocation_vectors[
                node.plan.component_revision.uri
            ],
        )
        execution_plans[node.plan.component_revision.uri] = model_plan
        return CodingCliSourceGenerationInvocation.create(
            model_plan,
            request,
            application_root_revision_identity=authority.lock.root_revision,
            readiness_identity=canonical_identity(
                {"live-standard-readiness": node.plan.component_revision.uri}
            ),
        )

    cas = FileSystemCAS(scratch / "live-standard-cas")
    coding_runner = CachedCodingCliSourceGenerationRunner(
        source_generator,
        cas=cas,
        invocation_provider=invocation,
    )
    generation_errors: dict[str, str] = {}

    def generate_node(node):
        try:
            return coding_runner(node)
        except Exception as error:
            code = getattr(error, "code", None)
            detail = f"{type(error).__name__}: {error}"
            generation_errors[node.plan.component_revision.uri] = (
                detail if code is None else f"{code}: {detail}"
            )
            raise

    source_registry = _GeneratedSourceTreeRegistry()
    indexer = DisabledGenerationIndexer(source_registry)
    bazel_executable = shutil.which("bazel") or shutil.which("bazelisk")
    if bazel_executable is None:
        raise RuntimeError("live Standard service-stack requires Bazel on PATH")
    contracts, tool_bindings, bazel_targets = _command_contracts(
        execution_plan,
        by_revision,
        live=True,
        invocations=invocations,
        bazel_executable=bazel_executable,
    )
    provider_environment = {
        "python-money-calculation": (
            "LITAI_CAPABILITY_MONEY_CALCULATION",
            "python-money-calculation",
        ),
        "python-invoice-service": (
            "LITAI_CAPABILITY_INVOICE_SERVICE",
            "python-invoice-service",
        ),
    }
    toolchain_closure = _project_toolchain_closure(
        execution_plan,
        contracts=contracts,
        tool_bindings=tool_bindings,
        bazel_targets=bazel_targets,
        provider_environment=provider_environment,
        observation_root=scratch / "live-standard-toolchain-observation",
    )
    runtime = assemble_filesystem_standard_project_runtime(
        generator=generate_node,
        object_root=object_root,
        toolchain_closure=toolchain_closure,
        source_trees=source_registry,
        indexer=indexer,
        source_cache_publisher=coding_runner,
        independent_acceptance_oracle=_ServiceStackProjectAcceptanceOracle(
            authority.lock.root_revision
        ),
        node_preparation=LockedComponentNodePreparationAdapter(
            model_selector=LockedComponentModelSelectionAdapter(
                pipeline_model=pipeline_model
            ),
            coding_cli=selection.name,
        ),
    )
    revisions = tuple(
        plan.component_revision for plan in execution_plan.generation_plans
    )
    invalidation = ComponentInvalidationDecision(
        "service-stack-live-standard",
        authority.lock.root_revision,
        ComponentChangeSurface.LOCAL_AUTHORITY,
        revisions,
        revisions,
        revisions,
    )
    executed = runtime.execute(
        snapshot,
        StandardProjectExecutionRequest(
            PlannedStandardProject(selection, execution_plan),
            scratch / "live-standard-workspaces",
            invalidation,
            max_parallelism=2,
            budget=_budget(),
        ),
    )
    prepared = executed.prepared
    result = executed.lifecycle
    ports = runtime.lifecycle_ports
    if not result.successful:
        failures = {
            by_revision[item.component_revision.uri]: item.failure_code
            for item in result.node_results
        }
        details = {
            by_revision[revision]: message
            for revision, message in generation_errors.items()
        }
        build_details = {
            by_revision[revision]: message
            for revision, message in ports.failure_diagnostics.items()
        }
        raise RuntimeError(
            "live Standard service-stack failed: "
            f"{failures}; generation errors={details}; build errors={build_details}"
        )
    expected = {
        "money-calculation": {
            "discount_cents": 125,
            "subtotal_cents": 999,
            "total_cents": 874,
        },
        "invoice-service": {
            "discount_cents": 410,
            "line_count": 2,
            "subtotal_cents": 4095,
            "total_cents": 3685,
            "unit_count": 5,
        },
        "service-stack": {
            "discount_cents": 410,
            "line_count": 2,
            "subtotal_cents": 4095,
            "total_cents": 3685,
            "unit_count": 5,
        },
    }
    observed = {
        by_revision[revision]: json.loads(stdout)
        for revision, stdout in ports.execution_stdout.items()
    }
    if set(observed) != set(expected) or any(
        not isinstance(value, dict) or set(value) != set(expected[name])
        for name, value in observed.items()
    ):
        raise RuntimeError(
            f"live Standard service-stack smoke output shape mismatch: {observed!r}"
        )
    node_results = {
        by_revision[item.component_revision.uri]: item for item in result.node_results
    }
    first_recipe = prepared.nodes[0].recipe
    report: dict[str, object] = {
        "schema": "literate-ai/live-standard-service-stack@1",
        "lifecycle": "standard-component-project",
        "successful": True,
        "proof_status": "passed",
        "receipt_admissible": False,
        "receipt_blockers": [
            "generated-test-manifest execution is not yet wired into Standard testing",
            "post-build CycloneDX reconciliation is not yet wired into Standard builds",
        ],
        "component_lock_identity": authority.lock.identity.uri,
        "recipe_identity": prepared.identity.uri,
        "execution_plan_identity": execution_plan.identity.uri,
        "coding_cli": selection.name,
        "coding_model": source_cache_model_selector(
            first_recipe.model_for(selection.name)
        ),
        "coding_cli_tool_binding_identity": selection.tool_binding_identity,
        "request_identity": canonical_identity(
            {
                "standard-generation-requests": [
                    item.source_output.candidate.source_generation_request_identity.uri
                    for item in result.node_results
                ]
            }
        ).uri,
        "accepted_tree_identity": canonical_identity(
            {
                "standard-source-candidates": [
                    item.source_candidate_identity.uri for item in result.node_results
                ]
            }
        ).uri,
        "build_artifact_identity": canonical_identity(
            {
                "standard-artifacts": [
                    item.exports[0].identity.uri for item in result.node_results
                ]
            }
        ).uri,
        "implementation_language": "python",
        "implementation_languages": ["python"],
        "generated_from_specification": True,
        "node_smoke_probe_count": 3,
        "node_acceptance_probe_count": 3,
        "source_bom_member_identities": [
            item.source_output.candidate.source_bom_identity.uri
            for item in result.node_results
        ],
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "generation_calls": tuple(sorted(node_results)),
        "source_cache": source_generator.report(),
        "_operational_build_cache": ports.build_cache_report(),
        # Independent packaged acceptance just observed this exact verifier-owned
        # result. Node execution above is deliberately a separate generated smoke case.
        "root_result": expected["service-stack"],
        "component_results": {
            name: {
                "candidate_identity": item.source_candidate_identity.uri,
                "build_identity": item.build_identity.uri,
                "test_identity": item.test_identity.uri,
                "execution_identity": item.execution_identity.uri,
                "artifact_identity": item.exports[0].identity.uri,
                "provider_artifact_identities": [
                    identity.uri
                    for identity in item.exports[0].dependency_artifact_identities
                ],
                "direct_public_interface_identities": [
                    identity.uri
                    for identity in next(
                        plan
                        for plan in execution_plan.generation_plans
                        if plan.component_revision == item.component_revision
                    ).generation_key.direct_public_interface_identities
                ],
                "source_index_identity": item.index_identity.uri,
            }
            for name, item in node_results.items()
        },
    }
    if include_lifecycle_result:
        report["_lifecycle_result"] = result
    return report


__all__ = [
    "execute_live_standard_service_stack",
    "execute_standard_service_stack",
]
