"""Exercise Standard package custody through real command LINK/PACKAGE execution."""

from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.action_package_result import verify_deterministic_package
from literate_ai.adapters.command_packager import CommandProjectPackager
from literate_ai.adapters.packaging import DirectoryPackageAdapter
from literate_ai.application.artifact_graph import create_artifact_build_graph
from literate_ai.application.release_artifacts import (
    plan_accepted_assembly_dependencies,
)


def assert_standard_command_package(
    case, snapshot, execution, linker, accepted, local_ports, worker_cas, source_cas
):
    manifests = tuple(result.manifest for _, result in linker._completed.values())
    receipts = tuple(receipt for _, receipt in accepted.values())
    root = accepted[execution.root_revision][1].build.exports[0]
    graph = create_artifact_build_graph(
        build_system_driver_identity=manifests[0].build_system_driver_identity,
        manifests=manifests,
        assembly_dependencies=plan_accepted_assembly_dependencies(execution, receipts),
        link_roots=(root.identity,),
    )
    root_ports = local_ports[execution.root_revision]
    # The fixture builds each Component with separate local ports. Route only its
    # public artifact custody lookups to the actual owner; package creation and
    # command dispatch remain production code, as do every owner's byte checks.
    readers, paths = {}, {}
    for revision, (_, receipt) in accepted.items():
        owner = local_ports[revision]
        for export in receipt.build.exports:
            readers[export.blob] = owner.read_artifact_blob
            paths[export.identity] = owner.artifact_path
    root_ports.project_packager = CommandProjectPackager(
        linker,
        verify_package=partial(
            verify_deterministic_package, adapter_factory=DirectoryPackageAdapter
        ),
        result_source=lambda worker, reference: worker_cas.get_bytes(reference),
    )
    with (
        patch.object(
            root_ports, "read_artifact_blob", side_effect=lambda ref: readers[ref](ref)
        ),
        patch.object(
            root_ports,
            "artifact_path",
            side_effect=lambda export: paths[export.identity](export),
        ),
    ):
        plan, result = root_ports.create_project_package(
            snapshot.authority.lock,
            execution,
            SimpleNamespace(),
            graph,
            graph.link_plans[0],
        )
    custody = root_ports.project_package_custody(plan, result)
    case.assertEqual(result.package_plan_identity, plan.identity)
    case.assertEqual(len(result.files), len(paths))
    case.assertEqual(set(custody.artifact_paths), {item.uri for item in paths})
    for item in plan.inputs:
        case.assertEqual(
            custody.root.joinpath(item.path).read_bytes(), readers[item.blob](item.blob)
        )
    reservation = linker.indexer.slots.try_reserve(lambda worker, slot: None)
    case.assertIsNotNone(reservation)
    reservation.release()

    from tests.support.finalize_proof_fixture import assert_finalize_proof

    assert_finalize_proof(
        case,
        snapshot,
        execution,
        linker,
        accepted,
        graph,
        plan,
        result,
        source_cas,
        root_ports,
    )
