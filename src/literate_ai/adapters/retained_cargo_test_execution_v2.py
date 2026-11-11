"""Execute reviewed v2 target protocols under the existing native binary owner."""

import tomllib
from dataclasses import replace

from literate_ai.adapters.retained_cargo_test_execution import (
    _prepare_retained_native_tests,
)
from literate_ai.adapters.retained_cargo_test_observation_v2 import (
    verify_libtest_observation_v2,
)
from literate_ai.adapters.retained_cargo_test_selection_v2 import (
    select_cargo_test_executables_v2,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_cargo_test_evidence import (
    RetainedCargoTargetEvidenceV2,
    require_retained_cargo_coverage,
)
from literate_ai.contracts.retained_cargo_test_protocol import (
    RetainedCargoCustomProtocol,
    RetainedCargoExcludedProtocol,
    RetainedCargoLibtestProtocol,
)
from literate_ai.projects import PinnedInputClosure


def _require_harnesses(materialized):
    reviewed = materialized.files.reviewed
    workspace = materialized.scratch.context.workspace
    packages = {p.root: p for p in reviewed.plan.graph.common.packages}
    for target in reviewed.inventory.targets:
        package = packages[target.package_root]
        document = tomllib.loads((workspace / package.root / "Cargo.toml").read_text())
        if set(target.target_kinds) & {
            "lib",
            "rlib",
            "dylib",
            "proc-macro",
            "cdylib",
            "staticlib",
        }:
            rows = [document.get("lib", {})]
        else:
            kinds = set(target.target_kinds) & {"bin", "test", "bench", "example"}
            if len(kinds) != 1:
                raise ValueError("retained.tests.v2-target-harness-unknown")
            rows = [
                row
                for row in document.get(next(iter(kinds)), [])
                if row.get("name") == target.target_name
            ]
        if len(rows) > 1:
            raise ValueError("retained.tests.v2-target-harness-ambiguous")
        harness = rows[0].get("harness", True) if rows else True
        if type(harness) is not bool or harness != target.harness:
            raise ValueError("retained.tests.v2-target-harness-drift")
    materialized.require_unchanged()


def observe_retained_cargo_tests_v2(
    materialized,
    metadata,
    cargo,
    *,
    rustc,
    environment,
    offline,
    run,
    observation_identity,
    source_guard,
    gate_tools,
    read_exclusion_permission,
    registry_sources=None,
):
    """Attribute every target; exclusions never become successful test cases.

    read_exclusion_permission independently reopens retained gate authority and
    returns its exact current permission identity. Candidate inventory alone
    cannot grant permission. Custom inputs/executable bytes are exact reviewed
    identities, and every command still uses the session's bounded guarded owner.
    """
    source_guard()
    reviewed = materialized.files.reviewed
    plan, inventory = reviewed.plan, reviewed.inventory
    root = reviewed.project.root
    common = plan.common
    workspace = materialized.scratch.context.workspace
    inventory.require_graph(plan.graph)
    _require_harnesses(materialized)

    def permissions():
        for target in inventory.targets:
            protocol = target.protocol
            if isinstance(protocol, RetainedCargoExcludedProtocol):
                if (
                    not callable(read_exclusion_permission)
                    or read_exclusion_permission(protocol.permission_identity)
                    != protocol.permission_identity
                ):
                    raise ValueError("retained.tests.v2-exclusion-permission-refused")

    permissions()

    def compile_command(fresh):
        argv = [
            "cargo",
            "test",
            "--workspace",
            "--all-targets",
            "--no-run",
            "--locked",
            "--message-format=json",
            "--target",
            common.target,
            "--target-dir",
            str(fresh),
        ]
        if offline:
            argv.append("--offline")
        if common.features:
            argv.append("--features=" + ",".join(common.features))
        if common.all_features:
            argv.append("--all-features")
        if common.no_default_features:
            argv.append("--no-default-features")
        return RepositoryBuildCommand(
            "retained-test-compile",
            tuple(argv),
            common.workspace_root,
            (),
            network=not offline,
        )

    def select(result, fresh):
        source_guard()
        value = select_cargo_test_executables_v2(
            result,
            metadata,
            workspace_root=workspace,
            output_root=fresh,
            plan=plan,
            workspace_manifest=(workspace / "Cargo.toml").read_bytes(),
            lockfile=(workspace / "Cargo.lock").read_bytes(),
            registry_sources=registry_sources,
        )
        source_guard()
        return value

    sessions, binaries_guard = _prepare_retained_native_tests(
        project_root=root,
        workspace=workspace,
        output=workspace / plan.graph.common.output_directory,
        target=common.target,
        # run() owns complete consumer-root/tool/permission checks around every
        # subprocess. Nested binary/runtime checks still require the live
        # original source, package and scratch owner, without recursively
        # rescanning all external consumer roots at that same boundary.
        source_guard=materialized.require_unchanged,
        inventory_identity=inventory.identity,
        compile_command=compile_command,
        select=select,
        metadata=metadata,
        cargo=cargo,
        rustc=rustc,
        environment=environment,
        run=run,
    )
    selected = {
        (b.package_root, b.target_name, tuple(sorted(b.target_kinds))): (b, t, g, h)
        for b, t, g, h in sessions
    }
    if set(selected) != {target.key for target in inventory.targets}:
        raise ValueError("retained.tests.v2-target-set-drift")
    outcomes = []
    for index, target in enumerate(inventory.targets):
        source_guard()
        permissions()
        binary, tool, target_guard, digest = selected[target.key]
        protocol = target.protocol
        common_values = (target.package_root, target.target_name, target.target_kinds)
        if isinstance(protocol, RetainedCargoExcludedProtocol):
            outcomes.append(
                RetainedCargoTargetEvidenceV2(
                    *common_values, "excluded", (), (), (), protocol.permission_identity
                )
            )
            continue
        if isinstance(protocol, RetainedCargoLibtestProtocol):
            results, identities = [], []
            for phase, arguments in (
                ("list", ("--list", "--format=terse")),
                ("run", ("--format=pretty", "--color=never")),
            ):
                results.append(
                    run(
                        RepositoryBuildCommand(
                            f"retained-test-{index}-{phase}",
                            (str(binary.executable), *arguments),
                            (workspace / binary.package_root)
                            .relative_to(root)
                            .as_posix(),
                            (),
                            network=False,
                        ),
                        tool,
                        extra_guard=target_guard,
                    )
                )
                identities.append(observation_identity())
            observation = verify_libtest_observation_v2(*results, protocol)
            outcomes.append(
                RetainedCargoTargetEvidenceV2(
                    *common_values,
                    "libtest",
                    observation.passed,
                    observation.ignored,
                    tuple(identities),
                    None,
                )
            )
        elif isinstance(protocol, RetainedCargoCustomProtocol):
            if (
                ContentIdentity.parse_uri("sha256:" + digest)
                != protocol.executable_identity
            ):
                raise ValueError("retained.tests.v2-custom-executable-mismatch")
            inputs = PinnedInputClosure(
                maximum_files=4096,
                maximum_file_bytes=64 * 1024**2,
                maximum_total_bytes=256 * 1024**2,
            )
            for item in protocol.inputs:
                inputs.pin(
                    root / item.path,
                    boundary=root,
                    label=item.path,
                    expected_identity=item.identity,
                )

            def custom_guard(target_guard=target_guard, inputs=inputs):
                target_guard()
                inputs.require_unchanged()
                permissions()

            argv = tuple(
                str(binary.executable) if x == "{target_executable}" else x
                for x in protocol.command.argv
            )
            command = replace(
                protocol.command, step_id=f"retained-test-{index}-custom", argv=argv
            )
            selected_tool = (
                tool
                if protocol.command.argv[0] == "{target_executable}"
                else gate_tools[protocol.command.argv[0]]
            )
            run(
                command,
                selected_tool,
                extra_guard=custom_guard,
                timeout_seconds=protocol.timeout_seconds,
                max_output_bytes=protocol.maximum_output_bytes,
            )
            outcomes.append(
                RetainedCargoTargetEvidenceV2(
                    *common_values,
                    "custom-harness",
                    (),
                    (),
                    (observation_identity(),),
                    None,
                )
            )
        else:
            raise ValueError("retained.tests.v2-protocol-required")
        source_guard()
        permissions()
    binaries_guard()
    permissions()
    outcomes = tuple(outcomes)
    require_retained_cargo_coverage(outcomes, inventory)
    return outcomes
