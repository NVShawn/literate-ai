"""Shared Elixir/Mix test fixtures from test_elixir_standard_mix (PR #45)."""

from __future__ import annotations

import base64
import json
import sys
import zlib
from dataclasses import replace

from literate_ai.adapters.builders.elixir import ElixirToolchain
from literate_ai.adapters.builders.hex import HexToolchain
from literate_ai.adapters.builders.mix import MixToolchain
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
)
from literate_ai.adapters.lifecycle.standard_mix import (
    StandardMixLifecyclePorts,
    StandardMixTarget,
)
from literate_ai.contracts import (
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentLifecycleCommand,
    LibraryCapabilityImport,
    LibraryImportSurface,
    canonical_identity,
)
from literate_ai.contracts.elixir_libraries import elixir_namespace
from literate_ai.contracts.mix_projects import MixDependencyIntent, MixProjectIntent
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.hex_pins import exact_hex_component
from tests.support.standard_source_evidence_fixture import register_strict_source


def _tool(root):
    digest = canonical_identity({"fixture": "mix-tool-bytes"}).uri
    elixir = ElixirToolchain(
        (sys.executable,), "1.18.0", (1, 18, 0), "27", ((sys.executable, digest),)
    )
    mix = MixToolchain(
        elixir,
        "1.18.0",
        tuple((str(root / name), digest) for name in ("mix", "a", "b", "c")),
    )
    return HexToolchain(
        mix,
        str(root / "hex"),
        "2.5.1",
        tuple((str(root / "hex" / f"file{n}.beam"), digest) for n in range(11)),
    )


def _system(
    root,
    *,
    tool=None,
    verifier=None,
    dependencies=False,
    library=False,
    library_package="provider_api",
    mix_library=False,
    evidence_recorder=None,
    package_root=False,
):
    snapshot, execution = _fixture(
        library_names=frozenset({"storage"}) if library else frozenset(),
        root_name="service" if package_root else "application",
    )
    generation = execution.generation_plans[1 if library else 0]
    provider_module = elixir_namespace(library_package) + ".Math"
    tool = tool or _tool(root)
    hex_binding = LocalComponentToolBinding.from_observed_toolchain(tool)
    elixir_binding = LocalComponentToolBinding.from_observed_toolchain(tool.mix.elixir)
    target = StandardMixTarget(
        generation.component_revision,
        canonical_identity({"fixture": "mix-resolver"}),
        tool,
    )
    contract = ComponentCommandContract(
        component_revision=generation.component_revision,
        locked_build_authority_identity=target.identity,
        build_system_resolver_identity=target.build_system_resolver_identity,
        build_system_toolchain_identity=hex_binding.toolchain_identity,
        language_compiler_identity=elixir_binding.toolchain_identity,
        language_runtime_identity=elixir_binding.toolchain_identity,
        commands=tuple(
            ComponentLifecycleCommand(
                phase,
                ("{tool}", "{source_root}", "{object_root}", "{export_path}")
                if phase is ComponentCommandPhase.BUILD
                else (
                    "{tool}",
                    "-e",
                    "[root | args] = System.argv(); System.argv(args); "
                    'Code.eval_file(Path.join(root, "app/source/main.exs"))',
                    "--",
                    "{artifact_root}",
                    "--litai-test"
                    if phase is ComponentCommandPhase.TEST
                    else "--litai-smoke",
                ),
            )
            for phase in ComponentCommandPhase
        ),
        tool_bindings=tuple(
            ComponentCommandToolBinding(
                phase,
                hex_binding.toolchain_identity
                if phase is ComponentCommandPhase.BUILD
                else elixir_binding.toolchain_identity,
            )
            for phase in ComponentCommandPhase
        ),
        artifact_export=ComponentArtifactExportShape(
            "app",
            "executable",
            canonical_identity({"abi": "elixir"}),
            canonical_identity({"platform": "fixture"}),
            "text/x-elixir",
            canonical_identity({"producer": "mix"}),
        ),
    )
    source = root / "generated"
    (source / "source").mkdir(parents=True)
    project = MixProjectIntent(
        "litai_fixture",
        "1.0.0",
        "Standard Mix fixture",
        ("MIT",),
        (
            MixDependencyIntent("decimal", "== 2.3.0"),
            MixDependencyIntent("jason", "== 1.4.4"),
        )
        if dependencies
        else (),
        links=(("Source", "https://example.invalid/fixture"),),
    )
    (source / "source/mix-project.json").write_text(
        json.dumps(project.to_dict()), encoding="utf-8"
    )
    (source / "source/main.exs").write_text(
        "case System.argv() do\n"
        ' ["--litai-test"] -> IO.puts(JSON.encode!(%{\n'
        ' schema: "literate-ai/generated-test-results@1",\n'
        ' cases: Enum.map(["example", "boundary", "invariant"], fn k ->\n'
        ' %{case_id: "fixture-" <> k, outcome: "passed"} end)}))\n'
        ' ["--litai-smoke"] -> IO.puts(JSON.encode!(42))\n'
        " [input] -> [a,b] = JSON.decode!(input); IO.puts(JSON.encode!(a+b))\n"
        "end\n",
        encoding="utf-8",
    )
    if dependencies:
        (source / "source/main.exs").write_text(
            "[a,b] = Jason.decode!(hd(System.argv()))\n"
            "IO.puts(Jason.encode!(Decimal.to_string(Decimal.add(a,b))))\n",
            encoding="utf-8",
        )
    if library:
        main = source / "source/main.exs"
        main.write_bytes(
            main.read_bytes()
            .replace(b"a+b", b"ProviderApi.Math.add(a,b)")
            .replace(b"JSON.encode!(42)", b"JSON.encode!(ProviderApi.Math.add(20,22))")
            .replace(b"ProviderApi.Math", provider_module.encode())
        )
        if mix_library:
            (source / "source/consumer.ex").write_bytes(
                (
                    "defmodule StandardLibraryConsumer do\n"
                    f" @answer {provider_module}.add(20,22)\n"
                    " def answer, do: @answer\nend\n"
                ).encode()
            )
            main.write_bytes(
                main.read_bytes().replace(
                    f"{provider_module}.add(20,22)".encode(),
                    b"StandardLibraryConsumer.answer()",
                )
            )
    registry = LocalSourceTreeRegistry()
    components = (
        tuple(exact_hex_component(name) for name in ("decimal", "jason"))
        if dependencies
        else ()
    )
    candidate = register_strict_source(
        registry,
        source,
        snapshot=snapshot,
        generation_plan=generation,
        identity_namespace="standard-mix",
        additional_components=components,
        root_dependency_refs=("hex-decimal", "hex-jason") if dependencies else (),
        additional_edges=(("hex-jason", "hex-decimal"),) if dependencies else (),
    )
    contracts = (contract,)
    bindings = (hex_binding, elixir_binding)
    mix_targets = (target,)
    if library:
        from literate_ai.adapters.standard_project import (
            _STANDARD_BUILD_DRIVER,
            _STANDARD_LIBRARY_IMPORT_DRIVER,
            _STANDARD_LIBRARY_TEST_DRIVER,
            _encoded_library_import_surface,
        )

        python = LocalComponentToolBinding(sys.executable)
        provider_generation = execution.generation_plans[0]
        edge = generation.direct_generation_edges[0]
        surface = LibraryImportSurface(
            "elixir",
            library_package,
            (
                LibraryCapabilityImport(
                    edge.capability,
                    edge.public_interface_identity,
                    provider_module,
                    ("add",),
                ),
            ),
        )
        provider_shape = replace(
            contract.artifact_export,
            export_id="provider-library",
            role="library",
            media_type="application/vnd.literate-ai.elixir-library-tree",
            producer_identity=canonical_identity("default-elixir-library-producer"),
        )
        provider_contract = replace(
            contract,
            component_revision=provider_generation.component_revision,
            locked_build_authority_identity=canonical_identity(
                "default-elixir-library-build"
            ),
            build_system_resolver_identity=canonical_identity(
                "default-elixir-library-resolver"
            ),
            build_system_toolchain_identity=python.toolchain_identity,
            language_runtime_identity=python.toolchain_identity,
            artifact_export=provider_shape,
            library_import_surface=surface,
            commands=(
                ComponentLifecycleCommand(
                    ComponentCommandPhase.BUILD,
                    (
                        "{tool}",
                        "-c",
                        _STANDARD_BUILD_DRIVER,
                        "elixir-tree",
                        json.dumps(tool.mix.elixir.command),
                        base64.urlsafe_b64encode(zlib.compress(b"[]")).decode(),
                        "{source_root}",
                        ".",
                        "{object_root}",
                        "{export_path}",
                    ),
                ),
                ComponentLifecycleCommand(
                    ComponentCommandPhase.TEST,
                    (
                        "{tool}",
                        "-c",
                        _STANDARD_LIBRARY_TEST_DRIVER,
                        "elixir",
                        json.dumps(tool.mix.elixir.command),
                        "{export_path}",
                        "-",
                        "-",
                        "{artifact_root}",
                    ),
                ),
                ComponentLifecycleCommand(
                    ComponentCommandPhase.EXECUTE,
                    (
                        "{tool}",
                        "-c",
                        _STANDARD_LIBRARY_IMPORT_DRIVER,
                        "elixir",
                        json.dumps(tool.mix.elixir.command),
                        _encoded_library_import_surface(surface),
                        "{export_path}",
                        "-",
                        "{artifact_root}",
                    ),
                ),
            ),
            tool_bindings=tuple(
                ComponentCommandToolBinding(phase, python.toolchain_identity)
                for phase in ComponentCommandPhase
            ),
        )
        contracts = (*contracts, provider_contract)
        bindings = (*bindings, python)
        if mix_library:
            provider_target = StandardMixTarget(
                provider_generation.component_revision,
                canonical_identity("mix-library-resolver"),
                tool,
            )
            provider_contract = replace(
                provider_contract,
                locked_build_authority_identity=provider_target.identity,
                build_system_resolver_identity=provider_target.build_system_resolver_identity,
                build_system_toolchain_identity=hex_binding.toolchain_identity,
                commands=(
                    ComponentLifecycleCommand(
                        ComponentCommandPhase.BUILD,
                        (
                            "{tool}",
                            "compile",
                            "{source_root}",
                            "{object_root}",
                            "{export_path}",
                        ),
                    ),
                    provider_contract.command(ComponentCommandPhase.TEST),
                    replace(
                        provider_contract.command(ComponentCommandPhase.EXECUTE),
                        argv=tuple(
                            "elixir-mix" if token == "elixir" else token
                            for token in provider_contract.command(
                                ComponentCommandPhase.EXECUTE
                            ).argv
                        ),
                    ),
                ),
                tool_bindings=(
                    ComponentCommandToolBinding(
                        ComponentCommandPhase.BUILD, hex_binding.toolchain_identity
                    ),
                    ComponentCommandToolBinding(
                        ComponentCommandPhase.TEST, python.toolchain_identity
                    ),
                    ComponentCommandToolBinding(
                        ComponentCommandPhase.EXECUTE, python.toolchain_identity
                    ),
                ),
            )
            contracts = (contract, provider_contract)
            mix_targets = (*mix_targets, provider_target)
    ports = StandardMixLifecyclePorts(
        source_trees=registry,
        object_root=root / "objects",
        contracts=contracts,
        tool_bindings=bindings,
        mix_targets=mix_targets,
        mix_authorization_verifier=verifier,
    )
    providers = ()
    if evidence_recorder is not None:
        ports.retain_evidence_with(evidence_recorder)
    if library:
        provider_source = root / "provider-source"
        (provider_source / "source" / library_package).mkdir(parents=True)
        (provider_source / "source" / library_package / "math.ex").write_bytes(
            f"defmodule {provider_module} do\n  def add(a,b), do: a+b\nend\n".encode()
        )
        provider_components = ()
        if mix_library:
            native_project = MixProjectIntent(
                library_package,
                "1.0.0",
                "Standard Mix library fixture",
                ("MIT",),
                (MixDependencyIntent("decimal", "== 2.3.0"),),
                links=(("Source", "https://example.invalid/mix-library"),),
            )
            (provider_source / "source/mix-project.json").write_text(
                json.dumps(native_project.to_dict()), encoding="utf-8"
            )
            (provider_source / "source" / library_package / "math.ex").write_bytes(
                (
                    f"defmodule {provider_module} do\n"
                    " def add(a,b), do: Decimal.to_integer(Decimal.add(a,b))\nend\n"
                ).encode()
            )
            (provider_source / "source/main.exs").write_bytes(
                b"IO.puts(JSON.encode!(%{"
                b'schema: "literate-ai/generated-test-results@1",'
                b' cases: [%{case_id: "fixture-example", outcome: "passed"}]}))\n'
            )
            provider_components = (exact_hex_component("decimal"),)
        provider_candidate = register_strict_source(
            registry,
            provider_source,
            snapshot=snapshot,
            generation_plan=provider_generation,
            identity_namespace="standard-provider-source",
            additional_components=provider_components,
            root_dependency_refs=("hex-decimal",) if mix_library else (),
        )
        provider_intent = ports.create(
            execution, provider_generation, provider_candidate, (), ()
        )
        provider_index = ports.index(
            provider_candidate.component_revision, provider_candidate.tree_identity
        )
        provider_plan = ports.finalize(
            provider_intent, ports.authorize(provider_intent, provider_index)
        )
        providers = ports.build(provider_plan, ()).exports
    intent = ports.create(execution, generation, candidate, providers, ())
    index = ports.index(candidate.component_revision, candidate.tree_identity)
    authorization = ports.authorize(intent, index)
    plan = ports.finalize(intent, authorization)
    return ports, plan, intent, authorization, source
