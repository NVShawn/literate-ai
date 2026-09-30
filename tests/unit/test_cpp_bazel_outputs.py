"""Bazel native-product custody keeps generated tests out of consumer exports."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.builders.bazel import _remove_bazel_directory
from literate_ai.adapters.builders.cpp import (
    bazel_sdk_build_options,
    discover_cpp_toolchain,
)
from literate_ai.adapters.builders.python import BuildError
from literate_ai.adapters.component_acceptance import (
    DeclaredLibraryAcceptanceCase,
    LibraryAcceptance,
)
from literate_ai.adapters.conan_packaging import (
    ConanPackageAdapter,
    ConanToolBinding,
    _require_resolved_reference,
    _restored_reference,
)
from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.lifecycle.cpp_acceptance import (
    CppAcceptanceError,
    compile_cpp_verifier,
)
from literate_ai.adapters.lifecycle.standard_bazel import (
    StandardBazelTarget,
    _copy_cpp_library_outputs,
)
from literate_ai.adapters.lifecycle.standard_local import (
    LocalStandardLifecycleError,
    RegisteredSourceGenerationRunner,
    local_generated_source_tree_identity,
)
from literate_ai.adapters.models import CodingCliSelection
from literate_ai.adapters.models.coding_cli import (
    _acceptance_argument_vectors,
    _acceptance_result_shape,
)
from literate_ai.adapters.standard_project import (
    PlannedStandardProject,
    StandardProjectExecutionRequest,
    assemble_filesystem_standard_project_runtime,
    project_locked_standard_toolchain_closure,
)
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts import (
    CYCLONEDX_SOURCE_SBOM_PATH,
    CppLibraryLayout,
    CycloneDxLifecycle,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.executable_components import (
    ComponentChangeSurface,
    ComponentInvalidationDecision,
    GeneratedSourceCandidate,
    PackageKind,
    SourceGenerationProvenance,
    SourceGenerationRunOutput,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    GENERATED_TEST_SUITE_SCHEMA,
    MAJOR_REBUILD_GENERATION_MODE,
    validate_generated_test_suite,
)
from tests.unit.test_standard_command_projection import _locked_snapshot


def target(layout, test="tests/run"):
    identity = canonical_identity("cpp-fixture")
    return StandardBazelTarget(
        identity,
        identity,
        identity,
        "//:library",
        "stage",
        cpp_layout=layout,
        cpp_test_output=test,
    )


def _write_bazel_producer(
    root: Path,
    *,
    windows: bool,
    kind: str,
    cases: tuple[tuple[str, int, int, int], ...] = (("fixture-example", 19, 23, 42),),
    lifecycle_target: bool = False,
) -> None:
    if kind not in {"static", "shared"}:
        raise ValueError("fixture kind must be static or shared")
    shared_name = (
        "sample.dll"
        if windows
        else "libsample.dylib"
        if sys.platform == "darwin"
        else "libsample.so"
    )
    link_basenames = (
        (("sample.lib" if windows else "libsample.a"),)
        if kind == "static"
        else ("sample.dll.if.lib",)
        if windows
        else (shared_name,)
    )
    runtime_basenames = () if kind == "static" else (shared_name,)
    link_output = "lib/" + ("sample.lib" if windows else link_basenames[0])
    runtime_output = (
        "" if kind == "static" else ("bin/" if windows else "lib/") + shared_name
    )
    test_name = "run.exe" if windows else "run"
    (root / "include/sample").mkdir(parents=True)
    (root / "MODULE.bazel").write_text(
        'module(name = "cpp_library_fixture", version = "1.0.0")\n'
        'bazel_dep(name = "rules_cc", version = "0.2.22")\n',
        encoding="utf-8",
    )
    (root / "include/sample/api.hpp").write_text(
        "#pragma once\n"
        "#if defined(_WIN32) && defined(SAMPLE_SHARED)\n"
        "#ifdef SAMPLE_BUILD\n"
        "#define SAMPLE_API __declspec(dllexport)\n"
        "#else\n"
        "#define SAMPLE_API __declspec(dllimport)\n"
        "#endif\n"
        "#else\n"
        "#define SAMPLE_API\n"
        "#endif\n"
        "namespace sample { SAMPLE_API int add(int, int); }\n",
        encoding="utf-8",
    )
    (root / "api.cpp").write_text(
        '#include "include/sample/api.hpp"\n'
        "int sample::add(int a, int b) { return a + b; }\n",
        encoding="utf-8",
    )
    checks = "".join(
        f"  if (sample::add({left}, {right}) != {expected}) return {index};\n"
        for index, (_case_id, left, right, expected) in enumerate(cases, start=1)
    )
    result = json.dumps(
        {
            "schema": "literate-ai/generated-test-results@1",
            "cases": [
                {"case_id": case_id, "outcome": "passed"}
                for case_id, _left, _right, _expected in cases
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    (root / "generated_test.cpp").write_text(
        '#include "include/sample/api.hpp"\n#include <iostream>\n'
        "int main() {\n"
        + checks
        + f"  std::cout << R\"litai({result})litai\" << '\\n';\n"
        + "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "stager.cpp").write_text(
        "#include <algorithm>\n#include <filesystem>\n#include <fstream>\n"
        "#include <iostream>\n#include <iterator>\n#include <limits>\n"
        "#include <string>\n"
        "#include <system_error>\n#include <vector>\n"
        "bool archive_error(const char* reason, std::size_t offset) {\n"
        '  std::cerr << "invalid COFF archive at " << offset << ": " << reason '
        "<< '\\n';\n"
        "  return false;\n"
        "}\n"
        "bool normalize_archive_timestamps(const std::filesystem::path& path) {\n"
        "  std::ifstream input(path, std::ios::binary);\n"
        '  if (!input) return archive_error("open input", 0);\n'
        "  std::vector<char> bytes((std::istreambuf_iterator<char>(input)), "
        "std::istreambuf_iterator<char>());\n"
        '  if (input.bad()) return archive_error("read input", 0);\n'
        "  input.close();\n"
        '  if (input.fail()) return archive_error("close input", 0);\n'
        '  const std::string magic = "!<arch>\\n";\n'
        "  if (bytes.size() < magic.size() || "
        "!std::equal(magic.begin(), magic.end(), bytes.begin())) "
        'return archive_error("magic", 0);\n'
        "  std::size_t offset = magic.size();\n"
        "  while (offset < bytes.size()) {\n"
        "    constexpr std::size_t header_size = 60;\n"
        "    if (bytes.size() - offset < header_size || bytes[offset + 58] != '`' "
        "|| bytes[offset + 59] != '\\n') "
        'return archive_error("member header", offset);\n'
        "    std::size_t member_size = 0;\n"
        "    bool saw_digit = false;\n"
        "    for (std::size_t index = offset + 48; index < offset + 58; ++index) {\n"
        "      const char value = bytes[index];\n"
        "      if (value >= '0' && value <= '9') {\n"
        "        const std::size_t digit = static_cast<std::size_t>(value - '0');\n"
        "        if (member_size > "
        "(std::numeric_limits<std::size_t>::max() - digit) / 10) "
        'return archive_error("member size overflow", index);\n'
        "        member_size = member_size * 10 + digit;\n"
        "        saw_digit = true;\n"
        "      } else if (value != ' ') {\n"
        '        return archive_error("member size field", index);\n'
        "      }\n"
        "    }\n"
        '    if (!saw_digit) return archive_error("empty member size", offset);\n'
        "    std::fill(bytes.begin() + offset + 16, "
        "bytes.begin() + offset + 28, ' ');\n"
        "    bytes[offset + 16] = '0';\n"
        "    const std::size_t content = offset + header_size;\n"
        "    if (member_size > bytes.size() - content) "
        'return archive_error("member exceeds archive", offset);\n'
        "    offset = content + member_size;\n"
        "    if (member_size % 2 != 0) {\n"
        "      if (offset < bytes.size()) {\n"
        "        if (bytes[offset] != '\\n') "
        'return archive_error("member padding", offset);\n'
        "        ++offset;\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "  std::ofstream output(path, std::ios::binary | std::ios::trunc);\n"
        '  if (!output) return archive_error("open output", 0);\n'
        "  output.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));\n"
        '  if (!output.good()) return archive_error("write output", 0);\n'
        "  return true;\n"
        "}\n"
        "int main(int argc, char **argv) {\n"
        "  if (argc < 4 || argc % 2 != 0) return 2;\n"
        "  namespace fs = std::filesystem;\n"
        "  const fs::path output(argv[1]);\n"
        "  std::error_code error;\n"
        "  for (int i = 2; i < argc; i += 2) {\n"
        "    const fs::path destination = output / argv[i + 1];\n"
        "    fs::create_directories(destination.parent_path(), error);\n"
        "    if (error) return 3;\n"
        "    fs::copy_file(argv[i], destination, fs::copy_options::none, error);\n"
        "    if (error) return 4;\n"
        "    const auto source_permissions = "
        "fs::status(argv[i], error).permissions();\n"
        "    if (error) return 5;\n"
        '    if (destination.extension() == ".lib") {\n'
        "      fs::permissions(destination, fs::perms::owner_write, "
        "fs::perm_options::add, error);\n"
        "      if (error) return 5;\n"
        "      if (!normalize_archive_timestamps(destination)) return 6;\n"
        "    }\n"
        "    fs::permissions(destination, source_permissions, "
        "fs::perm_options::replace, error);\n"
        "    if (error) return 5;\n"
        "  }\n"
        "  return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "stage.bzl").write_text(
        "def _stage_cpp_library_impl(ctx):\n"
        "    output = ctx.actions.declare_directory(ctx.label.name)\n"
        "    candidates = ctx.attr.library[DefaultInfo].files.to_list()\n"
        "    link_candidates = ctx.attr.link_library[DefaultInfo].files.to_list()\n"
        "    link_files = [item for item in link_candidates "
        "if item.basename in ctx.attr.link_basenames]\n"
        "    runtime_files = [item for item in candidates "
        "if item.basename in ctx.attr.runtime_basenames]\n"
        "    if len(link_files) != 1:\n"
        '        fail("expected one platform link file, got %s" % link_candidates)\n'
        "    if ctx.attr.runtime_basenames and len(runtime_files) != 1:\n"
        '        fail("expected one platform runtime file, got %s" % candidates)\n'
        "    arguments = ctx.actions.args()\n"
        "    arguments.add(output.path)\n"
        "    arguments.add(ctx.file.header.path)\n"
        "    arguments.add(ctx.attr.header_output)\n"
        "    arguments.add(link_files[0].path)\n"
        "    arguments.add(ctx.attr.library_output)\n"
        "    if runtime_files and ctx.attr.runtime_output != ctx.attr.library_output:\n"
        "        arguments.add(runtime_files[0].path)\n"
        "        arguments.add(ctx.attr.runtime_output)\n"
        "    arguments.add(ctx.executable.test.path)\n"
        "    arguments.add(ctx.attr.test_output)\n"
        "    inputs = [ctx.file.header, link_files[0], ctx.executable.test]\n"
        "    if runtime_files:\n"
        "        inputs.append(runtime_files[0])\n"
        "    ctx.actions.run(\n"
        "        executable = ctx.executable._stager,\n"
        "        arguments = [arguments],\n"
        "        inputs = depset(inputs),\n"
        "        tools = [ctx.executable._stager],\n"
        "        outputs = [output],\n"
        "    )\n"
        "    return [DefaultInfo(files = depset([output]))]\n\n"
        "stage_cpp_library = rule(\n"
        "    implementation = _stage_cpp_library_impl,\n"
        "    attrs = {\n"
        '        "header": attr.label(allow_single_file = True, mandatory = True),\n'
        '        "library": attr.label(mandatory = True),\n'
        '        "link_library": attr.label(mandatory = True),\n'
        '        "test": attr.label('
        'executable = True, cfg = "target", mandatory = True),\n'
        '        "link_basenames": attr.string_list(mandatory = True),\n'
        '        "runtime_basenames": attr.string_list(),\n'
        '        "header_output": attr.string(mandatory = True),\n'
        '        "library_output": attr.string(mandatory = True),\n'
        '        "runtime_output": attr.string(),\n'
        '        "test_output": attr.string(mandatory = True),\n'
        '        "_stager": attr.label(\n'
        '            default = Label("//:stager"), executable = True, cfg = "exec"\n'
        "        ),\n"
        "    },\n"
        ")\n",
        encoding="utf-8",
    )
    library_rule = (
        'cc_library(\n    name = "sample",\n    srcs = ["api.cpp"],\n'
        '    hdrs = ["include/sample/api.hpp"],\n'
        '    strip_include_prefix = "include",\n)\n\n'
        if kind == "static"
        else 'cc_library(\n    name = "sample_objects",\n    srcs = ["api.cpp"],\n'
        '    hdrs = ["include/sample/api.hpp"],\n'
        '    strip_include_prefix = "include",\n'
        '    defines = ["SAMPLE_SHARED"],\n'
        '    local_defines = ["SAMPLE_BUILD"],\n'
        "    alwayslink = True,\n)\n\n"
        f'cc_binary(\n    name = "{shared_name}",\n'
        '    deps = [":sample_objects"],\n'
        "    linkshared = True,\n)\n\n"
        'filegroup(\n    name = "sample_shared_interface",\n'
        f'    srcs = [":{shared_name}"],\n'
        '    output_group = "interface_library",\n)\n\n'
    )
    generated_test_dep = ":sample" if kind == "static" else ":sample_objects"
    generated_test_defines = (
        "" if kind == "static" else '    local_defines = ["SAMPLE_BUILD"],\n'
    )
    library_target = ":sample" if kind == "static" else f":{shared_name}"
    link_library_target = (
        ":sample_shared_interface" if kind == "shared" and windows else library_target
    )
    staged_target = "library" if lifecycle_target else "cpp_library"
    lifecycle_alias = (
        '\nalias(name = "litai_artifact", actual = ":library")\n'
        if lifecycle_target
        else ""
    )
    (root / "BUILD.bazel").write_text(
        "".join(
            (
                'load("@rules_cc//cc:cc_binary.bzl", "cc_binary")\n',
                'load("@rules_cc//cc:cc_library.bzl", "cc_library")\n',
                'load(":stage.bzl", "stage_cpp_library")\n\n',
                library_rule,
                'cc_binary(\n    name = "generated_test",\n',
                '    srcs = ["generated_test.cpp"],\n',
                f'    deps = ["{generated_test_dep}"],\n',
                generated_test_defines,
                ")\n\n",
                'cc_binary(\n    name = "stager",\n',
                '    srcs = ["stager.cpp"],\n',
                "    copts = select({\n",
                '        "@bazel_tools//src/conditions:darwin": '
                '["-mmacosx-version-min=10.15"],\n',
                '        "//conditions:default": [],\n',
                "    }),\n",
                "    linkopts = select({\n",
                '        "@bazel_tools//src/conditions:darwin": '
                '["-mmacosx-version-min=10.15"],\n',
                '        "//conditions:default": [],\n',
                "    }),\n",
                ")\n\n",
                f'stage_cpp_library(\n    name = "{staged_target}",\n',
                '    header = "include/sample/api.hpp",\n',
                f'    library = "{library_target}",\n',
                f'    link_library = "{link_library_target}",\n',
                '    test = ":generated_test",\n',
                f"    link_basenames = {list(link_basenames)!r},\n",
                f"    runtime_basenames = {list(runtime_basenames)!r},\n",
                '    header_output = "include/sample/api.hpp",\n',
                f'    library_output = "{link_output}",\n',
                f'    runtime_output = "{runtime_output}",\n',
                f'    test_output = "tests/{test_name}",\n)\n',
                lifecycle_alias,
            )
        ),
        encoding="utf-8",
    )


def _execution_log_action_count(path: Path) -> int:
    """Count the concatenated JSON action records emitted by Bazel."""

    content = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    offset = 0
    count = 0
    while offset < len(content):
        while offset < len(content) and content[offset].isspace():
            offset += 1
        if offset == len(content):
            break
        _record, offset = decoder.raw_decode(content, offset)
        count += 1
    return count


_CPP_CASES = (
    ("add-example", 19, 23, 42, "example"),
    ("add-boundary", -7, 7, 0, "boundary"),
    ("add-invariant", 101, 202, 303, "invariant"),
)


class _SpecificationBoundCppLibraryGenerator:
    """Deterministic test model returning one exact production runner output."""

    def __init__(self, root_revision: ContentIdentity, kind: str) -> None:
        self.root_revision = root_revision
        self.kind = kind
        self.calls: list[str] = []

    def __call__(self, prepared) -> SourceGenerationRunOutput:
        recipe = prepared.recipe
        if recipe.cpp_library_build is None:
            raise RuntimeError("C++ library generation requires native build authority")
        if recipe.cpp_library_build.layout.kind != self.kind:
            raise RuntimeError("C++ library generation received another product kind")
        root = Path(prepared.workspace.locator)
        source = root / "source"
        source.mkdir(parents=True)
        _write_bazel_producer(
            source,
            windows=os.name == "nt",
            kind=self.kind,
            cases=tuple(case[:4] for case in _CPP_CASES),
            lifecycle_target=True,
        )
        reference = recipe.non_acceptance_document_paths[0]
        suite_content = canonical_json_bytes(
            {
                "schema": GENERATED_TEST_SUITE_SCHEMA,
                "recipe_identity": recipe.identity,
                "generation_mode": MAJOR_REBUILD_GENERATION_MODE,
                "cases": [
                    {
                        "case_id": case_id,
                        "category": category,
                        "specification_refs": [reference],
                        "arguments": [left, right],
                        "expected_result": expected,
                    }
                    for case_id, left, right, expected, category in _CPP_CASES
                ],
            }
        )
        suite_path = root.joinpath(*Path(GENERATED_TEST_SUITE_PATH).parts)
        suite_path.parent.mkdir(parents=True, exist_ok=True)
        suite_path.write_bytes(suite_content)
        suite = validate_generated_test_suite(
            suite_content,
            recipe_identity=recipe.identity,
            specification_references=recipe.non_acceptance_document_paths,
            acceptance_arguments=_acceptance_argument_vectors(recipe),
            result_shape=_acceptance_result_shape(recipe),
        )
        rules_ref = "pkg:generic/rules_cc"
        source_bom_content, source_bom = build_cyclonedx_bom(
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=recipe.managed_sbom_graph,
            additional_components=(
                {
                    "type": "library",
                    "bom-ref": rules_ref,
                    "name": "rules_cc",
                    "purl": rules_ref,
                    "versionRange": "vers:generic/>=0.2.22",
                    "isExternal": True,
                    "properties": [
                        {"name": "literate-ai:dependency-kind", "value": "build"},
                        {"name": "literate-ai:dependency-scope", "value": "build"},
                        {
                            "name": "literate-ai:bzlmod-requested-version",
                            "value": "0.2.22",
                        },
                    ],
                },
            ),
            additional_edges=((recipe.managed_sbom_graph.root_ref, rules_ref),),
            composition_aggregate="incomplete_third_party_only",
        )
        source_bom_path = root.joinpath(*Path(CYCLONEDX_SOURCE_SBOM_PATH).parts)
        source_bom_path.parent.mkdir(parents=True, exist_ok=True)
        source_bom_path.write_bytes(source_bom_content)
        tree = local_generated_source_tree_identity(root)
        request = prepared.request.request
        recipe_identity = ContentIdentity.parse_uri(recipe.identity)
        planned_request_identity = canonical_identity(
            {
                "schema": "literate-ai/cpp-library-fixture-request@1",
                "prompt_identity": request.prompt_identity.uri,
            }
        )
        candidate = GeneratedSourceCandidate(
            prepared.plan.component_revision,
            request.identity,
            planned_request_identity,
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
            planned_request_identity,
            recipe.component_lock_identity,
            self.root_revision,
            prepared.plan.component_revision,
            prepared.plan.identity,
            prepared.plan.generation_key.identity,
            request.context_manifest_identity,
            request.prompt_identity,
            recipe_identity,
            prepared.workspace.allocation_identity,
            canonical_identity({"readiness": prepared.plan.component_revision.uri}),
            (canonical_identity({"route": "cpp-library-fixture"}),),
            (canonical_identity({"model-output": tree.uri}),),
            candidate.identity,
        )
        self.calls.append(prepared.definition.coordinate.name)
        return SourceGenerationRunOutput(
            candidate, candidate.identity, provenance, provenance.identity
        )


def _cpp_library_oracle(snapshot, contract) -> LibraryAcceptance:
    surface = contract.library_import_surface
    assert surface is not None
    capability = surface.capabilities[0].capability
    output = [
        '#include "sample/api.hpp"',
        "#include <iostream>",
        "int main(int argc, char**) {",
        "  if (argc != 4) return 2;",
        '  std::cout << R"litai({"cases":[)litai";',
    ]
    for index, (case_id, left, right, _expected, _category) in enumerate(_CPP_CASES):
        if index:
            output.append("  std::cout << ',';")
        output.append(
            "  std::cout << "
            f'R"litai({{"capability":"{capability}","case_id":"{case_id}",'
            f'"result":)litai" << sample::add({left}, {right}) << "}}";'
        )
    output.extend(
        (
            '  std::cout << R"litai(],"schema":'
            '"literate-ai/library-acceptance-results@1"})litai";',
            "  return 0;",
            "}",
            "",
        )
    )
    harness = "\n".join(output).encode("utf-8")
    root_node = next(
        item
        for item in snapshot.authority.lock.nodes
        if item.revision.identity == snapshot.authority.lock.root_revision
    )
    return LibraryAcceptance(
        root_node.revision.coordinate.name,
        root_node.revision.specification_set_identity,
        tuple(
            sorted(
                (item.identity for item in root_node.revision.public_interfaces),
                key=lambda item: item.uri,
            )
        ),
        surface.identity,
        "cpp",
        ContentIdentity.parse_uri("sha256:" + hashlib.sha256(harness).hexdigest()),
        harness,
        tuple(
            DeclaredLibraryAcceptanceCase(case_id, capability, [left, right], expected)
            for case_id, left, right, expected, _category in _CPP_CASES
        ),
    )


def _local_coding_cli_selection() -> CodingCliSelection:
    executable = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    return CodingCliSelection("codex", str(executable), f"sha256:{digest}")


class CppBazelOutputTests(unittest.TestCase):
    @staticmethod
    def _selected_sdk_bazel_options():
        tool = discover_cpp_toolchain()
        tool.require_unchanged()
        return bazel_sdk_build_options(tool.environment)

    def _assert_fresh_conan_bazel_consumer(
        self,
        *,
        root: Path,
        runtime,
        integration,
        layout: CppLibraryLayout,
        kind: str,
    ) -> None:
        conan = Path(sys.executable).parent / (
            "conan.exe" if os.name == "nt" else "conan"
        )
        if not conan.is_file():
            self.fail("Conan must be installed in the contributor test environment")
        tool = ConanToolBinding.discover((str(conan),))
        adapter = ConanPackageAdapter("sample_component", "1.0.0", tool)
        directory_plan = integration.package_plan
        directory_result = integration.package_result
        custody = runtime.lifecycle_ports.project_package_custody(
            directory_plan, directory_result
        )
        plan = replace(
            directory_plan,
            package_kind=PackageKind.ARCHIVE,
            packager_identity=adapter.packager_identity,
        )
        object_root = root / f"conan-{kind}"
        object_root.mkdir()
        result = adapter.package(
            plan,
            materialized_root=custody.root,
            object_root=object_root,
        )
        archive = adapter.read_created_blob(result.artifacts[0].blob)
        consumer = root / f"conan-bazel-consumer-{kind}"
        consumer.mkdir()
        (consumer / "MODULE.bazel").write_text(
            'module(name = "conan_consumer", version = "1.0.0")\n'
            'bazel_dep(name = "rules_cc", version = "0.2.22")\n',
            encoding="utf-8",
        )
        (consumer / "main.cpp").write_text(
            '#include "package/include/sample/api.hpp"\n'
            "int main() { return sample::add(19, 23) == 42 ? 0 : 1; }\n",
            encoding="utf-8",
        )
        with adapter.restore_verified(
            result, archive, object_root=object_root
        ) as restored:
            assert result.native_library_root is not None
            package = restored.joinpath(*Path(result.native_library_root).parts)
            shutil.copytree(package, consumer / "package")
            link = "package/" + layout.link_files[0]
            attributes = (
                f'    static_library = "{link}",\n'
                if kind == "static"
                else (
                    f'    interface_library = "{link}",\n'
                    f'    shared_library = "package/{layout.runtime_files[0]}",\n'
                )
                if os.name == "nt"
                else f'    shared_library = "{link}",\n'
            )
            (consumer / "BUILD.bazel").write_text(
                'load("@rules_cc//cc:cc_binary.bzl", "cc_binary")\n'
                'load("@rules_cc//cc:cc_import.bzl", "cc_import")\n\n'
                'cc_import(\n    name = "sample_package",\n'
                '    hdrs = glob(["package/include/**"]),\n'
                + attributes
                + ')\n\ncc_binary(\n    name = "consumer",\n'
                '    srcs = ["main.cpp"],\n'
                '    deps = [":sample_package"],\n)\n',
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    "bazel",
                    "--batch",
                    "run",
                    *self._selected_sdk_bazel_options(),
                    "//:consumer",
                ],
                cwd=consumer,
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self._assert_restored_conan_bazel_dependency(
            root=root,
            result=result,
            archive=archive,
            layout=layout,
            adapter=adapter,
            bazel=shutil.which("bazel"),
        )

    def test_locked_cpp_library_runs_through_complete_filesystem_runtime(self):
        bazel = shutil.which("bazel")
        if bazel is None:
            self.skipTest("Bazel is unavailable")
        preflight = subprocess.run(
            [bazel, "--version"],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(
            preflight.returncode,
            0,
            preflight.stdout + preflight.stderr,
        )
        platform = (
            "windows"
            if os.name == "nt"
            else "macos"
            if sys.platform == "darwin"
            else "linux"
        )
        for kind in ("static", "shared"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _component, snapshot, execution = _locked_snapshot(
                    root / "authority",
                    language="cpp",
                    platform=platform,
                    no_entrypoint=True,
                    build_system="bazel",
                    cpp_library_kind=kind,
                )
                try:
                    closure = project_locked_standard_toolchain_closure(
                        snapshot, execution, host_platform=platform
                    )
                except BuildError as exc:
                    if exc.code not in {
                        "builder.cpp_toolchain_unavailable",
                        "builder.bazel_toolchain_unavailable",
                        "builder.bazel_direct_toolchain_unavailable",
                    }:
                        raise
                    self.skipTest(f"required native toolchain unavailable: {exc}")
                contract = closure.contracts[0]
                generator = _SpecificationBoundCppLibraryGenerator(
                    snapshot.authority.lock.root_revision, kind
                )
                runtime = assemble_filesystem_standard_project_runtime(
                    generator=generator,
                    object_root=root / "objects",
                    toolchain_closure=closure,
                    independent_acceptance_oracle=_cpp_library_oracle(
                        snapshot, contract
                    ),
                )
                revisions = tuple(
                    plan.component_revision for plan in execution.generation_plans
                )
                invalidation = ComponentInvalidationDecision(
                    "cpp-library-production-runtime",
                    snapshot.authority.lock.root_revision,
                    ComponentChangeSurface.LOCAL_AUTHORITY,
                    revisions,
                    revisions,
                    revisions,
                )
                result = runtime.execute(
                    snapshot,
                    StandardProjectExecutionRequest(
                        PlannedStandardProject(
                            _local_coding_cli_selection(), execution
                        ),
                        root / "workspaces",
                        invalidation,
                    ),
                )

                self.assertTrue(
                    result.lifecycle.successful,
                    (
                        tuple(
                            (item.failure_code, item.failure_evidence)
                            for item in result.lifecycle.node_results
                        ),
                        runtime.lifecycle_ports.failure_diagnostics,
                    ),
                )
                self.assertIsInstance(
                    runtime.application.lifecycle.generator,
                    RegisteredSourceGenerationRunner,
                )
                self.assertEqual(generator.calls, ["greeting"])
                node = result.lifecycle.node_results[0]
                self.assertIsNotNone(node.source_output)
                self.assertIsNotNone(node.build_evidence)
                self.assertIsNotNone(node.generated_test_evidence)
                self.assertEqual(node.generated_test_evidence.passed_count, 3)
                self.assertIsNotNone(node.execution_evidence)
                self.assertIsNotNone(node.acceptance_evidence)
                self.assertIsNotNone(result.lifecycle.root_integration)
                package_plan = result.lifecycle.root_integration.package_plan
                package_result = result.lifecycle.root_integration.package_result
                self.assertEqual(
                    package_plan.native_library_layout, contract.native_layout
                )
                self.assertEqual(
                    package_result.native_library_layout,
                    contract.native_layout,
                )
                self.assertEqual(
                    package_plan.native_library_root,
                    package_result.native_library_root,
                )
                self.assertIsNotNone(
                    result.lifecycle.root_integration.independent_acceptance_identity
                )
                self._assert_fresh_conan_bazel_consumer(
                    root=root,
                    runtime=runtime,
                    integration=result.lifecycle.root_integration,
                    layout=contract.native_layout,
                    kind=kind,
                )
                self.assertEqual(result.local_build_cache_report["misses"], 1)

    def test_windows_shared_fixture_uses_bazel_interface_output_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bazel_producer(root, windows=True, kind="shared")
            build = (root / "BUILD.bazel").read_text(encoding="utf-8")
            stager = (root / "stage.bzl").read_text(encoding="utf-8")
        self.assertIn('name = "sample.dll"', build)
        self.assertIn('output_group = "interface_library"', build)
        self.assertIn('link_library = ":sample_shared_interface"', build)
        self.assertIn("link_basenames = ['sample.dll.if.lib']", build)
        self.assertIn('library_output = "lib/sample.lib"', build)
        self.assertIn("ctx.attr.link_library[DefaultInfo].files.to_list()", stager)
        self.assertNotIn("CcInfo", stager)

    def test_windows_stager_canonicalizes_archive_member_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bazel_producer(root, windows=True, kind="static")
            stager = (root / "stager.cpp").read_text(encoding="utf-8")
        self.assertIn("normalize_archive_timestamps", stager)
        self.assertIn('destination.extension() == ".lib"', stager)
        self.assertIn("bytes.begin() + offset + 16", stager)
        self.assertIn("bytes.begin() + offset + 28", stager)
        self.assertIn("bytes[offset + 16] = '0'", stager)
        self.assertLess(
            stager.index("input.close();"), stager.index("std::ofstream output")
        )
        self.assertLess(
            stager.index("fs::perms::owner_write"),
            stager.index("normalize_archive_timestamps(destination)"),
        )
        self.assertLess(
            stager.index("normalize_archive_timestamps(destination)"),
            stager.index("fs::perm_options::replace"),
        )

    def test_macos_stager_declares_cpp_filesystem_deployment_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bazel_producer(root, windows=False, kind="static")
            build = (root / "BUILD.bazel").read_text(encoding="utf-8")

        self.assertEqual(build.count('"-mmacosx-version-min=10.15"'), 2)
        self.assertIn('"@bazel_tools//src/conditions:darwin"', build)
        self.assertIn("copts = select({", build)
        self.assertIn("linkopts = select({", build)

    def test_stager_executes_coff_archive_timestamp_canonicalization(self):
        try:
            tool = discover_cpp_toolchain()
        except BuildError as exc:
            if exc.code != "builder.cpp_toolchain_unavailable":
                raise
            self.skipTest(f"C++ toolchain unavailable: {exc}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_bazel_producer(root, windows=os.name == "nt", kind="static")
            stager = root / ("stager.exe" if os.name == "nt" else "stager")
            command = (
                (
                    *tool.command,
                    "/nologo",
                    "/std:c++17",
                    "/EHsc",
                    str(root / "stager.cpp"),
                    f"/Fe:{stager}",
                )
                if tool.family == "msvc"
                else (
                    *tool.command,
                    "-std=c++17",
                    str(root / "stager.cpp"),
                    "-o",
                    str(stager),
                )
            )
            compiled = subprocess.run(
                command,
                env={**os.environ, **dict(tool.environment)},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)

            def member(name, timestamp, content, *, terminal=False):
                header = (
                    name.encode("ascii").ljust(16)
                    + str(timestamp).encode("ascii").ljust(12)
                    + b"0".ljust(6)
                    + b"0".ljust(6)
                    + b"100644".ljust(8)
                    + str(len(content)).encode("ascii").ljust(10)
                    + b"`\n"
                )
                padding = b"\n" if len(content) % 2 and not terminal else b""
                return header + content + padding

            original = (
                b"!<arch>\n"
                + member("first/", 1789542234, b"abc")
                + member("second/", 1789542365, b"final", terminal=True)
            )
            source = root / "source.lib"
            source.write_bytes(original)
            source.chmod(stat.S_IREAD)
            output = root / "output"
            destination = output / "lib/sample.lib"
            try:
                staged = subprocess.run(
                    [str(stager), str(output), str(source), "lib/sample.lib"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(staged.returncode, 0, staged.stdout + staged.stderr)
                expected = bytearray(original)
                offset = len(b"!<arch>\n")
                for content_size in (3, 5):
                    expected[offset + 16 : offset + 28] = b"0" + b" " * 11
                    offset += 60 + content_size + content_size % 2
                self.assertEqual(destination.read_bytes(), expected)
                self.assertFalse(destination.stat().st_mode & stat.S_IWUSR)
            finally:
                source.chmod(stat.S_IREAD | stat.S_IWRITE)
                if destination.exists():
                    destination.chmod(stat.S_IREAD | stat.S_IWRITE)

            malformed = bytearray(original)
            first_padding = len(b"!<arch>\n") + 60 + len(b"abc")
            malformed[first_padding] = ord("X")
            malformed_source = root / "malformed.lib"
            malformed_source.write_bytes(malformed)
            rejected = subprocess.run(
                [
                    str(stager),
                    str(root / "malformed-output"),
                    str(malformed_source),
                    "lib/sample.lib",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(rejected.returncode, 6)

    def test_target_requires_exact_disjoint_test_and_product_paths(self):
        layout = CppLibraryLayout(
            "static", ("include/sample/api.hpp",), ("lib/sample.a",)
        )
        declared = target(layout)
        for changes in (
            {"cpp_test_output": None},
            {"cpp_layout": None},
            {"cpp_test_output": "lib/sample.a"},
            {"cpp_test_output": "tests/../run"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(declared, **changes)
        self.assertNotEqual(
            declared.identity, replace(declared, cpp_test_output="tests/other").identity
        )

    def test_output_closure_rejects_extra_missing_and_linked_files_before_copy(self):
        layout = CppLibraryLayout(
            "static", ("include/sample/api.hpp",), ("lib/sample.a",)
        )
        for mutation in ("extra", "missing", "symlink"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                stage = root / "stage"
                for name in (*layout.files, "tests/run"):
                    path = stage / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"fixture")
                if mutation == "extra":
                    (stage / "implementation.cpp").write_text("private source")
                elif mutation == "missing":
                    (stage / "lib/sample.a").unlink()
                else:
                    path = stage / "lib/sample.a"
                    path.unlink()
                    try:
                        path.symlink_to(stage / "tests/run")
                    except OSError:
                        self.skipTest("host does not permit symbolic links")
                export = root / "export"
                with self.assertRaises(LocalStandardLifecycleError):
                    _copy_cpp_library_outputs(
                        stage, export, root / "test-run", target(layout)
                    )
                self.assertFalse(export.exists())

    def test_real_bazel_static_product_survives_source_and_object_disposal(self):
        self.exercise_real_bazel_product("static")

    def test_real_bazel_shared_product_survives_source_and_object_disposal(self):
        self.exercise_real_bazel_product("shared")

    def exercise_real_bazel_product(self, kind):
        bazel = shutil.which("bazel")
        if bazel is None:
            self.skipTest("Bazel is unavailable")
        try:
            tool = discover_cpp_toolchain()
        except BuildError as exc:
            if exc.code != "builder.cpp_toolchain_unavailable":
                raise
            self.skipTest(f"C++ toolchain unavailable: {exc}")
        tool.require_unchanged()
        sdk_options = bazel_sdk_build_options(tool.environment)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "generated" / "source"
            object_root = root / "bazel-objects"
            _write_bazel_producer(workspace, windows=os.name == "nt", kind=kind)
            startup = (
                bazel,
                "--batch",
                "--nosystem_rc",
                "--nohome_rc",
                "--noworkspace_rc",
                f"--output_base={object_root}",
            )

            def bazel_run(*arguments):
                result = subprocess.run(
                    (*startup, *arguments),
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result

            def build_with_log(name):
                execution_log = root / f"{name}-execution.json"
                result = bazel_run(
                    "build",
                    "--lockfile_mode=error",
                    "--symlink_prefix=/",
                    f"--execution_log_json_file={execution_log}",
                    *sdk_options,
                    "//:cpp_library",
                )
                return result, _execution_log_action_count(execution_log)

            bazel_run("mod", "graph", "--lockfile_mode=update", *sdk_options)
            _clean_build, clean_actions = build_with_log("clean")
            self.assertGreater(clean_actions, 0)
            bazel_bin_lines = tuple(
                line.strip()
                for line in bazel_run("info", "bazel-bin").stdout.splitlines()
                if line.strip()
            )
            self.assertEqual(len(bazel_bin_lines), 1)
            produced = Path(bazel_bin_lines[0]) / "cpp_library"
            shared_name = (
                "sample.dll"
                if os.name == "nt"
                else "libsample.dylib"
                if sys.platform == "darwin"
                else "libsample.so"
            )
            library_name = (
                "sample.lib"
                if os.name == "nt"
                else "libsample.a"
                if kind == "static"
                else shared_name
            )
            runtime_files = (
                ()
                if kind == "static"
                else (f"bin/{shared_name}",)
                if os.name == "nt"
                else (f"lib/{shared_name}",)
            )
            test_name = "run.exe" if os.name == "nt" else "run"
            layout = CppLibraryLayout(
                kind,
                ("include/sample/api.hpp",),
                (f"lib/{library_name}",),
                runtime_files,
            )
            original_product = {
                relative: (produced / relative).read_bytes()
                for relative in layout.files
            }
            _no_op_build, no_op_actions = build_with_log("no-op")
            self.assertEqual(no_op_actions, 0)
            (workspace / "unrelated.txt").write_text(
                "not an input to the native product\n", encoding="utf-8"
            )
            _unrelated_build, unrelated_actions = build_with_log("unrelated-edit")
            self.assertEqual(unrelated_actions, 0)
            public_header = workspace / "include/sample/api.hpp"
            original_header = public_header.read_text(encoding="utf-8")
            public_header.write_text(
                original_header.replace(
                    "namespace sample { SAMPLE_API int add(int, int); }",
                    "namespace sample { inline constexpr int api_version = 2; "
                    "SAMPLE_API int add(int, int); }",
                ),
                encoding="utf-8",
            )
            _header_build, header_actions = build_with_log("header-edit")
            self.assertGreater(header_actions, 0)
            header_product = {
                relative: (produced / relative).read_bytes()
                for relative in layout.files
            }
            self.assertNotEqual(header_product, original_product)
            for header in layout.headers:
                self.assertNotEqual(header_product[header], original_product[header])
            public_header.write_text(original_header, encoding="utf-8")
            _header_restored_build, header_restored_actions = build_with_log(
                "header-restored"
            )
            self.assertGreater(header_restored_actions, 0)
            header_restored_product = {
                relative: (produced / relative).read_bytes()
                for relative in layout.files
            }
            self.assertEqual(header_restored_product, original_product)
            implementation = workspace / "api.cpp"
            original_implementation = implementation.read_text(encoding="utf-8")
            implementation.write_text(
                original_implementation.replace("return a + b;", "return a + b + 1;"),
                encoding="utf-8",
            )
            _changed_build, changed_actions = build_with_log("implementation-edit")
            self.assertGreater(changed_actions, 0)
            changed_product = {
                relative: (produced / relative).read_bytes()
                for relative in layout.files
            }
            for header in layout.headers:
                self.assertEqual(changed_product[header], original_product[header])
            native_files = set((*layout.link_files, *layout.runtime_files))
            self.assertTrue(
                any(
                    changed_product[library] != original_product[library]
                    for library in native_files
                )
            )
            implementation.write_text(original_implementation, encoding="utf-8")
            bazel_run("clean")
            _restored_build, restored_actions = build_with_log("restored-clean")
            self.assertGreater(restored_actions, 0)
            restored_product = {
                relative: (produced / relative).read_bytes()
                for relative in layout.files
            }
            self.assertEqual(restored_product, original_product)
            print(
                json.dumps(
                    {
                        "schema": "literate-ai/cpp-bazel-incremental-evidence@1",
                        "platform": sys.platform,
                        "product_kind": kind,
                        "action_counts": {
                            "clean": clean_actions,
                            "no_op": no_op_actions,
                            "unrelated_edit": unrelated_actions,
                            "header_edit": header_actions,
                            "header_restored": header_restored_actions,
                            "implementation_edit": changed_actions,
                            "restored_clean": restored_actions,
                        },
                        "original_sha256": {
                            relative: hashlib.sha256(content).hexdigest()
                            for relative, content in original_product.items()
                        },
                        "implementation_edit_sha256": {
                            relative: hashlib.sha256(content).hexdigest()
                            for relative, content in changed_product.items()
                        },
                        "header_edit_sha256": {
                            relative: hashlib.sha256(content).hexdigest()
                            for relative, content in header_product.items()
                        },
                        "header_restored_sha256": {
                            relative: hashlib.sha256(content).hexdigest()
                            for relative, content in header_restored_product.items()
                        },
                        "restored_clean_sha256": {
                            relative: hashlib.sha256(content).hexdigest()
                            for relative, content in restored_product.items()
                        },
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
            export = root / "consumer-export"
            test_export = root / "generated-test" / test_name
            _copy_cpp_library_outputs(
                produced,
                export,
                test_export,
                target(layout, f"tests/{test_name}"),
            )
            shutil.rmtree(root / "generated")
            _remove_bazel_directory(object_root)

            generated_test = subprocess.run(
                [str(test_export)], capture_output=True, text=True, timeout=60
            )
            self.assertEqual(
                generated_test.returncode,
                0,
                generated_test.stdout + generated_test.stderr,
            )
            self.assertIn('"fixture-example"', generated_test.stdout)
            consumer = root / "consumer.cpp"
            consumer.write_text(
                '#include "sample/api.hpp"\n'
                "int main() { return sample::add(19, 23) == 42 ? 0 : 1; }\n",
                encoding="utf-8",
            )
            executable = root / ("consumer.exe" if os.name == "nt" else "consumer")
            environment = {**os.environ, **dict(tool.environment)}
            run_environment = compile_cpp_verifier(
                compiler=tool.command,
                environment=environment,
                export=export,
                layout=layout,
                harness=consumer,
                executable=executable,
            )
            consumed = subprocess.run(
                [str(executable)],
                env=run_environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(consumed.returncode, 0, consumed.stdout + consumed.stderr)

    def _assert_restored_conan_bazel_dependency(
        self, *, root, result, archive, layout, adapter, bazel
    ):
        self.assertIsNotNone(bazel)
        consumer = root / "conan-bazel-consumer"
        consumer.mkdir()
        archive_path = consumer / result.artifacts[0].path
        archive_path.write_bytes(archive)
        consumer_home = root / "conan-consumer-home"
        adapter._run(
            ("profile", "detect", "--force"), cwd=consumer, conan_home=consumer_home
        )
        shown_profile = adapter._run(
            ("profile", "show", "--format=json"),
            cwd=consumer,
            conan_home=consumer_home,
        )
        profile = json.loads(shown_profile.stdout)
        settings = profile["host"]["settings"]

        def alternate_setting(name, candidates):
            current = settings.get(name)
            for candidate in candidates:
                if candidate == current:
                    continue
                try:
                    shown = adapter._run(
                        (
                            "profile",
                            "show",
                            "--format=json",
                            "-s:h",
                            f"{name}={candidate}",
                        ),
                        cwd=consumer,
                        conan_home=consumer_home,
                    )
                except PackagingError:
                    continue
                selected = json.loads(shown.stdout)["host"]["settings"].get(name)
                if selected == candidate:
                    return f"{name}={candidate}"
            self.fail(f"Conan exposes no alternate valid {name} setting")

        adapter._run(
            ("cache", "restore", str(archive_path)),
            cwd=consumer,
            conan_home=consumer_home,
        )
        listed = adapter._run(
            ("list", "sample_component/1.0.0#*:*#*", "--format=json"),
            cwd=consumer,
            conan_home=consumer_home,
        )
        expected_reference = _restored_reference(
            json.loads(listed.stdout), "sample_component/1.0.0"
        )
        archive_path.unlink()
        (consumer / "conanfile.py").write_text(
            "from conan import ConanFile\n"
            "from conan.tools.google import BazelDeps\n\n"
            "class Consumer(ConanFile):\n"
            "    settings = 'os', 'arch', 'compiler', 'build_type'\n"
            "    requires = 'sample_component/1.0.0'\n\n"
            "    def generate(self):\n"
            "        BazelDeps(self).generate()\n",
            encoding="utf-8",
        )
        substitute = consumer / "substitute"
        substitute.mkdir()
        (substitute / "conanfile.py").write_text(
            "from conan import ConanFile\n\n"
            "class Substitute(ConanFile):\n"
            "    name = 'sample_component'\n"
            "    version = '1.0.0'\n"
            "    settings = 'os', 'arch', 'compiler', 'build_type'\n",
            encoding="utf-8",
        )
        adapter._run(
            ("export-pkg", ".", "--no-remote", "--test-folder="),
            cwd=substitute,
            conan_home=consumer_home,
        )
        adapter._run(
            (
                "install",
                ".",
                "--output-folder=conan-shadow",
                "--build=never",
                "--no-remote",
                "--format=json",
                "--out-file=conan-shadow.json",
            ),
            cwd=consumer,
            conan_home=consumer_home,
        )
        shadow_graph = json.loads(
            (consumer / "conan-shadow.json").read_text(encoding="utf-8")
        )
        with self.assertRaisesRegex(PackagingError, "another package revision"):
            _require_resolved_reference(
                shadow_graph, "sample_component/1.0.0", expected_reference
            )
        consumer_recipe = consumer / "conanfile.py"
        consumer_recipe.write_text(
            consumer_recipe.read_text(encoding="utf-8").replace(
                "sample_component/1.0.0",
                expected_reference.split(":", 1)[0],
            ),
            encoding="utf-8",
        )
        compiler_major = settings["compiler.version"].partition(".")[0]
        self.assertTrue(compiler_major.isdecimal())
        self.assertGreater(int(compiler_major), 1)
        if "compiler.libcxx" in settings:
            abi_setting = alternate_setting(
                "compiler.libcxx", ("libc++", "libstdc++", "libstdc++11")
            )
        else:
            self.assertIn(settings.get("compiler.runtime"), {"dynamic", "static"})
            abi_setting = alternate_setting("compiler.runtime", ("dynamic", "static"))
        version_candidates = tuple(
            candidate
            for version in range(int(compiler_major) - 1, 0, -1)
            for candidate in (str(version), f"{version}.0")
        )
        compiler_setting = alternate_setting("compiler.version", version_candidates)
        mismatches = (
            (
                "target",
                "arch=x86_64" if settings["arch"] != "x86_64" else "arch=armv8",
            ),
            (
                "build-type",
                "build_type=Debug"
                if settings["build_type"] != "Debug"
                else "build_type=Release",
            ),
            ("compiler", compiler_setting),
            ("abi", abi_setting),
        )
        for mismatch, setting in mismatches:
            with (
                self.subTest(mismatch=mismatch),
                self.assertRaisesRegex(PackagingError, "Missing prebuilt package"),
            ):
                adapter._run(
                    (
                        "install",
                        ".",
                        f"--output-folder=conan-{mismatch}",
                        "--build=never",
                        "--no-remote",
                        "-s:h",
                        setting,
                    ),
                    cwd=consumer,
                    conan_home=consumer_home,
                )
        adapter._run(
            (
                "install",
                ".",
                "--output-folder=conan",
                "--build=never",
                "--no-remote",
                "--format=json",
                "--out-file=conan-install.json",
            ),
            cwd=consumer,
            conan_home=consumer_home,
        )
        _require_resolved_reference(
            json.loads((consumer / "conan-install.json").read_text(encoding="utf-8")),
            "sample_component/1.0.0",
            expected_reference,
        )
        (consumer / "MODULE.bazel").write_text(
            'module(name = "conan_bazel_consumer", version = "1.0.0")\n'
            'include("//conan:conan_deps.MODULE.bazel")\n',
            encoding="utf-8",
        )
        data = (
            '    data = ["@sample_component//:sample_component_binaries"],\n'
            if os.name == "nt" and layout.kind == "shared"
            else ""
        )
        (consumer / "BUILD.bazel").write_text(
            'load("@rules_cc//cc:cc_binary.bzl", "cc_binary")\n\n'
            "cc_binary(\n"
            '    name = "consumer",\n'
            '    srcs = ["main.cpp"],\n'
            '    deps = ["@sample_component//:sample_component"],\n' + data + ")\n",
            encoding="utf-8",
        )
        (consumer / "main.cpp").write_text(
            '#include "sample/api.hpp"\n'
            "#include <iostream>\n"
            "int main() {\n"
            "  if (sample::add(19, 23) != 42) return 1;\n"
            '  std::cout << "conan-bazel-ok\\n";\n'
            "  return 0;\n"
            "}\n",
            encoding="utf-8",
        )
        startup = (
            bazel,
            "--batch",
            "--nosystem_rc",
            "--nohome_rc",
            "--noworkspace_rc",
            f"--output_base={root / 'conan-consumer-bazel-objects'}",
        )
        first = subprocess.run(
            (*startup, "mod", "graph", "--lockfile_mode=update"),
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        consumed = subprocess.run(
            (
                *startup,
                "run",
                "--lockfile_mode=error",
                *self._selected_sdk_bazel_options(),
                "//:consumer",
            ),
            cwd=consumer,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(consumed.returncode, 0, consumed.stdout + consumed.stderr)
        self.assertIn("conan-bazel-ok", consumed.stdout)

    def test_real_static_and_shared_products_link_without_producer_source(self):
        try:
            tool = discover_cpp_toolchain()
        except BuildError as exc:
            if exc.code != "builder.cpp_toolchain_unavailable":
                raise
            self.skipTest(f"C++ toolchain unavailable: {exc}")
        environment = {**os.environ, **dict(tool.environment)}
        archiver = shutil.which(
            "lib.exe" if tool.family == "msvc" else "ar", path=environment.get("PATH")
        )
        if archiver is None:
            self.skipTest("native archiver unavailable")
        for kind in ("static", "shared"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "producer"
                source.mkdir()
                stage = root / "stage"
                for subdir in ("include/sample", "lib", "bin", "tests"):
                    (stage / subdir).mkdir(parents=True, exist_ok=True)
                header = stage / "include/sample/api.hpp"
                header.write_text("""#pragma once
#if defined(_WIN32) && defined(SAMPLE_SHARED)
#ifdef SAMPLE_BUILD
#define SAMPLE_API __declspec(dllexport)
#else
#define SAMPLE_API __declspec(dllimport)
#endif
#else
#define SAMPLE_API
#endif
namespace sample { SAMPLE_API int add(int, int); }
""")
                implementation = source / "api.cpp"
                implementation.write_text(
                    '#include "sample/api.hpp"\n'
                    "int sample::add(int a,int b){return a+b;}\n"
                )

                def run(command, *, env=environment, cwd=root):
                    tool.require_unchanged()
                    result = subprocess.run(
                        command,
                        cwd=cwd,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    tool.require_unchanged()
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    return result

                object_file = source / ("api.obj" if tool.family == "msvc" else "api.o")
                if tool.family == "msvc":
                    defines = (
                        ["/DSAMPLE_SHARED", "/DSAMPLE_BUILD"]
                        if kind == "shared"
                        else []
                    )
                    run(
                        [
                            *tool.command,
                            "/nologo",
                            "/std:c++17",
                            "/EHsc",
                            "/MD",
                            *defines,
                            "/I" + str(stage / "include"),
                            "/c",
                            str(implementation),
                            "/Fo" + str(object_file),
                        ]
                    )
                    link_file = "lib/sample.lib"
                    runtime = ("bin/sample.dll",) if kind == "shared" else ()
                    if kind == "static":
                        run(
                            [
                                archiver,
                                "/nologo",
                                "/OUT:" + str(stage / link_file),
                                str(object_file),
                            ]
                        )
                    else:
                        run(
                            [
                                *tool.command,
                                "/nologo",
                                "/LD",
                                str(object_file),
                                "/Fe:" + str(stage / runtime[0]),
                                "/link",
                                "/IMPLIB:" + str(stage / link_file),
                            ]
                        )
                        # Remove the MSVC linker intermediate from the fixture.
                        for intermediate in stage.rglob("*.exp"):
                            intermediate.unlink()
                else:
                    run(
                        [
                            *tool.command,
                            "-std=c++17",
                            "-fPIC",
                            "-I" + str(stage / "include"),
                            "-c",
                            str(implementation),
                            "-o",
                            str(object_file),
                        ]
                    )
                    suffix = ".dylib" if sys.platform == "darwin" else ".so"
                    link_file = "lib/libsample" + (".a" if kind == "static" else suffix)
                    runtime = (link_file,) if kind == "shared" else ()
                    if kind == "static":
                        run([archiver, "rcs", str(stage / link_file), str(object_file)])
                    else:
                        flags = (
                            ["-dynamiclib", "-Wl,-install_name,@rpath/libsample.dylib"]
                            if sys.platform == "darwin"
                            else ["-shared", "-Wl,-soname,libsample.so"]
                        )
                        run(
                            [
                                *tool.command,
                                *flags,
                                str(object_file),
                                "-o",
                                str(stage / link_file),
                            ]
                        )
                layout = CppLibraryLayout(
                    kind, ("include/sample/api.hpp",), (link_file,), runtime
                )
                # The custody boundary treats generated tests as a distinct artifact.
                test_path = "tests/run.exe" if os.name == "nt" else "tests/run"
                (stage / test_path).write_bytes(b"separate generated test artifact")
                export = root / "export"
                _copy_cpp_library_outputs(
                    stage, export, root / "test-artifact", target(layout, test_path)
                )
                self.assertEqual(
                    {
                        p.relative_to(export).as_posix()
                        for p in export.rglob("*")
                        if p.is_file()
                    },
                    set(layout.files),
                )
                original = {name: (export / name).read_bytes() for name in layout.files}
                shutil.rmtree(source)
                shutil.rmtree(stage)
                consumer = root / "consumer.cpp"
                consumer.write_text(
                    '#include "sample/api.hpp"\n'
                    "int main(){return sample::add(19,23)==42?0:1;}\n"
                )
                executable = root / ("consumer.exe" if os.name == "nt" else "consumer")
                run_environment = compile_cpp_verifier(
                    compiler=tool.command,
                    environment=environment,
                    export=export,
                    layout=layout,
                    harness=consumer,
                    executable=executable,
                )
                run([str(executable)], env=run_environment)
                self.assertEqual(
                    {name: (export / name).read_bytes() for name in layout.files},
                    original,
                )
                self.exercise_native_acceptance(root, export, layout, tool)
                consumer.write_text(
                    '#include "sample/api.hpp"\nint main(){return sample::missing();}\n'
                )
                with self.assertRaisesRegex(CppAcceptanceError, "compilation failed"):
                    compile_cpp_verifier(
                        compiler=tool.command,
                        environment=environment,
                        export=export,
                        layout=layout,
                        harness=consumer,
                        executable=root / "wrong-api",
                    )

    def exercise_native_acceptance(self, root, export, layout, tool):
        import hashlib
        from types import SimpleNamespace

        from literate_ai.adapters.component_acceptance import (
            DeclaredLibraryAcceptanceCase,
            LibraryAcceptance,
        )
        from literate_ai.adapters.lifecycle.standard_local import (
            LocalComponentToolBinding,
            LocalSourceTreeRegistry,
            LocalStandardLifecyclePorts,
            local_tree_identity,
        )
        from literate_ai.contracts import (
            ComponentCommandPhase,
            ComponentCommandToolBinding,
            ContentIdentity,
        )
        from tests.unit.test_component_command_contracts import contract
        from tests.unit.test_library_products import library_product

        binding = LocalComponentToolBinding(
            tool.command[0],
            tool.command[1:],
            environment=tool.environment,
            _authority_guard=tool.require_unchanged,
        )
        tool_id = binding.toolchain_identity
        base = contract()
        product = library_product()
        capability = replace(
            product.import_surface.capabilities[0],
            module="sample/api.hpp",
            symbols=("sample::add",),
        )
        surface = replace(
            product.import_surface,
            language="cpp",
            package="sample",
            capabilities=(capability,),
        )
        import json

        from literate_ai.adapters.standard_project import (
            _STANDARD_CPP_LIBRARY_IMPORT_DRIVER,
            _encoded_cpp_layout,
            _encoded_library_import_surface,
            _encoded_toolchain_environment,
        )

        def import_check(selected):
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _STANDARD_CPP_LIBRARY_IMPORT_DRIVER,
                    json.dumps(tool.command),
                    _encoded_library_import_surface(selected),
                    _encoded_cpp_layout(layout),
                    str(export),
                    _encoded_toolchain_environment(tool.environment),
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

        imported = import_check(surface)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(
            json.loads(imported.stdout)["capabilities"], [capability.capability]
        )
        missing = replace(
            surface, capabilities=(replace(capability, symbols=("sample::missing",)),)
        )
        self.assertNotEqual(import_check(missing).returncode, 0)
        native = replace(
            base,
            language_compiler_identity=tool_id,
            language_runtime_identity=tool_id,
            build_system_toolchain_identity=tool_id,
            tool_bindings=tuple(
                ComponentCommandToolBinding(phase, tool_id)
                for phase in ComponentCommandPhase
            ),
            artifact_export=replace(base.artifact_export, role="library"),
            library_import_surface=surface,
            native_layout=layout,
        )
        ports = LocalStandardLifecyclePorts(
            source_trees=LocalSourceTreeRegistry(),
            object_root=root / "objects",
            contracts=(native,),
            tool_bindings=(binding,),
        )
        artifact = replace(
            product.artifact_export, component_revision=native.component_revision
        )
        ports._planned_exports[native.component_revision.uri] = artifact
        specification = canonical_identity("reviewed fixture specification")
        revision = SimpleNamespace(
            identity=native.component_revision,
            coordinate=SimpleNamespace(name="sample"),
            specification_set_identity=specification,
            public_interfaces=(
                SimpleNamespace(identity=capability.interface_identity),
            ),
        )
        lock = SimpleNamespace(
            root_revision=revision.identity, nodes=(SimpleNamespace(revision=revision),)
        )
        custody = SimpleNamespace(
            artifact_paths={artifact.identity.uri: export},
            root=export,
            tree_identity=local_tree_identity(export),
        )
        harness = (
            b'#include "sample/api.hpp"\n#include <iostream>\n'
            b'int main(){std::cout << R"({"schema":'
            b'"literate-ai/library-acceptance-results@1","cases":['
            b'{"case_id":"add","capability":"fixture.logic","result":)"'
            b' << sample::add(19,23) << "}]}";}\n'
        )
        oracle = LibraryAcceptance(
            "sample",
            specification,
            (capability.interface_identity,),
            surface.identity,
            "cpp",
            ContentIdentity.parse_uri("sha256:" + hashlib.sha256(harness).hexdigest()),
            harness,
            (DeclaredLibraryAcceptanceCase("add", "fixture.logic", [19, 23], 42),),
        )
        package = SimpleNamespace(identity=canonical_identity("package"))
        accepted = ports._accept_library(
            lock, custody, package, package, oracle, package.identity, package.identity
        )
        self.assertIsInstance(accepted, ContentIdentity)
        wrong = replace(oracle, cases=(replace(oracle.cases[0], expected_result=43),))
        with self.assertRaisesRegex(LocalStandardLifecycleError, "result differs"):
            ports._accept_library(
                lock,
                custody,
                package,
                package,
                wrong,
                package.identity,
                package.identity,
            )
