"""Exercise PACKAGE proof using an actual completed command LINK chain."""

from dataclasses import replace

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_package_proof import reopen_package_input
from literate_ai.adapters.action_package_record import PackageWorkerInput
from literate_ai.application.action_dag_planning import plan_lifecycle_action_dag
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    create_package_plan,
)
from literate_ai.application.release_artifacts import (
    plan_accepted_assembly_dependencies,
)
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.executable_components import PackageEntrypoint, PackageKind
from literate_ai.storage import FileSystemCAS


def assert_package_proof(case, execution, linker, accepted, cas, deadline, source_cas):
    pairs = linker._completed
    manifests = tuple(result.manifest for _, result in pairs.values())
    receipts = tuple(receipt for _, receipt in accepted.values())
    root = accepted[execution.root_revision][1].build.exports[0]
    bindings = plan_accepted_assembly_dependencies(execution, receipts)
    case.assertTrue(bindings)
    graph = create_artifact_build_graph(
        build_system_driver_identity=manifests[0].build_system_driver_identity,
        manifests=manifests,
        assembly_dependencies=bindings,
        link_roots=(root.identity,),
    )

    def package_plan(graph):
        exports = {
            export.identity.uri: export
            for manifest in graph.manifests
            for export in manifest.exports
        }
        destinations = {
            identity.uri: (
                f"components/{exports[identity.uri].component_revision.digest[:16]}/app"
            )
            for identity in next(
                link
                for link in graph.link_plans
                if link.root_artifact_identity == root.identity
            ).ordered_artifact_identities
        }
        return create_package_plan(
            graph,
            root_component_revision=execution.root_revision,
            component_lock_identity=execution.component_lock_identity,
            target_identity=root.target_identity,
            root_artifact_identity=root.identity,
            package_kind=PackageKind.RUNTIME_BUNDLE,
            packager_identity=canonical_identity("fixture-packager"),
            destinations=destinations,
            entrypoints=(
                PackageEntrypoint(
                    "app", "application", destinations[root.identity.uri], root.identity
                ),
            ),
        )

    node = next(
        node
        for node in plan_lifecycle_action_dag(execution, worker_ids=("package",))
        if node.kind is LifecycleActionKind.PACKAGE
    )
    records = {
        record_identity(raw): raw
        for pair in pairs.values()
        for raw in (pair[0].to_bytes(), pair[1].to_bytes())
    }
    value = PackageWorkerInput(
        execution,
        graph,
        package_plan(graph),
        tuple(
            (action, record_identity(pairs[action][1].to_bytes()))
            for action in node.predecessor_ids
        ),
    )
    from unittest.mock import patch

    captured, captured_records = linker.package_handoff(graph, value.plan)
    case.assertEqual(captured, value)
    case.assertEqual(captured_records, records)
    captured_records.clear()
    case.assertEqual(linker.package_handoff(graph, value.plan)[1], records)
    removed = next(iter(pairs))
    previous = pairs.pop(removed)
    try:
        with case.assertRaises(ActionWireError) as error:
            linker.package_handoff(graph, value.plan)
        case.assertEqual(error.exception.code, "action_package.links_incomplete")
    finally:
        pairs[removed] = previous
    with patch.object(
        linker,
        "handoff_for",
        side_effect=ActionWireError("fixture.changed", "accepted handoff changed"),
    ):
        with case.assertRaises(ActionWireError) as error:
            linker.package_handoff(graph, value.plan)
        case.assertEqual(error.exception.code, "fixture.changed")
    value, records = captured, linker.package_handoff(graph, captured.plan)[1]
    target = FileSystemCAS(cas.root.parent / "package-proof-cas")

    def reopen(value=value, records=records, guard=lambda: None):
        raw = value.to_bytes()
        return reopen_package_input(
            input_record=raw,
            input_identity=record_identity(raw),
            records=records,
            cas=target,
            deadline=deadline,
            admission_guard=guard,
            blob_source=cas.get_bytes,
        )

    case.assertEqual(reopen(), value)
    # A self-consistent graph and plan can still omit required locked packaging edges.
    changed_graph = create_artifact_build_graph(
        build_system_driver_identity=graph.build_system_driver_identity,
        manifests=manifests,
        link_roots=(root.identity,),
    )
    changed = replace(
        value, artifact_graph=changed_graph, plan=package_plan(changed_graph)
    )
    raw = changed.to_bytes()
    case.assertEqual(
        PackageWorkerInput.admit(raw, record_identity(raw), deadline), changed
    )
    with case.assertRaises(ActionWireError) as error:
        reopen(changed)
    case.assertEqual(error.exception.code, "action_package.graph_mismatch")
    provider = next(
        manifest.exports[0]
        for manifest in manifests
        if manifest.component_revision != execution.root_revision
    )
    extra_roots = create_artifact_build_graph(
        build_system_driver_identity=graph.build_system_driver_identity,
        manifests=manifests,
        assembly_dependencies=bindings,
        link_roots=(root.identity, provider.identity),
    )
    with case.assertRaises(ActionWireError) as error:
        reopen(
            replace(value, artifact_graph=extra_roots, plan=package_plan(extra_roots))
        )
    case.assertEqual(error.exception.code, "action_package.graph_mismatch")
    missing = dict(records)
    missing.pop(value.link_results[0][1])
    with case.assertRaises(ActionWireError):
        reopen(records=missing)

    def revoked():
        raise ActionWireError("fixture.revoked", "PACKAGE admission revoked")

    with case.assertRaises(ActionWireError) as error:
        reopen(guard=revoked)
    case.assertEqual(error.exception.code, "fixture.revoked")

    from literate_ai.adapters.action_dispatch_wire import (
        decode_action_response,
        encode_action_request,
    )
    from literate_ai.adapters.action_package_execution import (
        PackageWorkerResult,
        execute_package_input,
    )
    from literate_ai.adapters.packaging import (
        DeterministicZipPackageAdapter,
        DirectoryPackageAdapter,
    )
    from tests.support.fixtures_test_action_package import make_package_request

    for adapter, kind in (
        (DirectoryPackageAdapter(), PackageKind.RUNTIME_BUNDLE),
        (DeterministicZipPackageAdapter(), PackageKind.ARCHIVE),
    ):
        packaged_input = replace(value, plan=replace(value.plan, package_kind=kind))
        raw = packaged_input.to_bytes()
        destination = FileSystemCAS(cas.root.parent / ("packaged-" + kind.value))
        request, dispatch_records = make_package_request(
            packaged_input, records, deadline
        )
        import os
        import subprocess
        import sys

        from tests.support.fixtures_test_action_blob_source import (
            blob_path,
            source_cas_server,
        )

        refs = {item.blob for item in packaged_input.plan.inputs}
        for linked_input, _ in pairs.values():
            refs.update(linked_input.acceptance_result.evidence_records)
            refs.add(
                linked_input.acceptance_input.execution_input.build_result.artifact_archive
            )
        blobs = {blob_path(ref): source_cas.get_bytes(ref) for ref in refs}
        jobs = cas.root.parent / ("package-jobs-" + kind.value)
        jobs.mkdir()
        bootstrap = f"""
from literate_ai.action_worker import main
from literate_ai.adapters.action_package_worker import ConfiguredPackageWorker
from literate_ai.adapters.packaging import {type(adapter).__name__}
from literate_ai.contracts import canonical_identity
worker = ConfiguredPackageWorker(canonical_identity('fixture-packager'),
    {type(adapter).__name__}, lambda: None)
raise SystemExit(main(package_worker=worker))
"""
        arguments = ["--cas", str(destination.root), "--workspace", str(jobs)]
        environment = dict(
            os.environ, LITAI_ACTION_WORKER_IDENTITY=request.worker.worker_identity.uri
        )
        from literate_ai.adapters.action_capabilities import (
            decode_capability_response,
            receiver_code_identity,
        )
        from literate_ai.adapters.action_package_worker import ConfiguredPackageWorker
        from literate_ai.contracts import canonical_json_bytes

        capability_request = canonical_json_bytes(
            {
                "schema": "literate-ai/action-capability-request@1",
                "worker_identity": request.worker.worker_identity.uri,
                "nonce": "a" * 32,
                "deadline": deadline.to_dict(),
            }
        )
        described = subprocess.run(
            [sys.executable, "-I", "-c", bootstrap, *arguments, "--describe"],
            input=capability_request,
            capture_output=True,
            timeout=30,
            env=environment,
        )
        case.assertEqual(described.returncode, 0, described.stderr.decode())
        facts = decode_capability_response(
            described.stdout, capability_request, receiver_code_identity()
        )
        case.assertIn(LifecycleActionKind.PACKAGE, facts.actions)
        case.assertEqual(
            facts.package_profile,
            ConfiguredPackageWorker(
                packaged_input.plan.packager_identity, type(adapter), lambda: None
            ).identity,
        )
        with source_cas_server(blobs) as (url, reads):
            process = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    bootstrap,
                    *arguments,
                    "--source-cas-url",
                    url,
                    "--allow-http",
                ],
                input=encode_action_request(request, deadline, dispatch_records),
                capture_output=True,
                timeout=60,
                env=environment,
            )
        case.assertEqual(process.returncode, 0, process.stderr.decode())
        outcome, result_raw = decode_action_response(process.stdout, request)
        case.assertIsNone(outcome.failure_code)
        case.assertTrue(reads)
        case.assertEqual(list(jobs.iterdir()), [])
        unconfigured = subprocess.run(
            [sys.executable, "-I", "-m", "literate_ai.action_worker", *arguments],
            input=encode_action_request(request, deadline, dispatch_records),
            capture_output=True,
            timeout=30,
            env=environment,
        )
        refused, absent = decode_action_response(unconfigured.stdout, request)
        case.assertEqual(refused.failure_code, "action_package.not_configured")
        case.assertIsNone(absent)
        result = PackageWorkerResult.admit(
            result_raw,
            record_identity(result_raw),
            input_record=raw,
            input_identity=record_identity(raw),
            deadline=deadline,
            read_blob=destination.get_bytes,
        )
        case.assertEqual(
            result.package.package_plan_identity, packaged_input.plan.identity
        )
        case.assertEqual(result.package.package_kind, kind)

        from functools import partial

        from literate_ai.adapters.action_package_result import (
            import_package_result,
            verify_deterministic_package,
        )

        returned = FileSystemCAS(cas.root.parent / ("returned-" + kind.value))
        verifier = partial(verify_deterministic_package, adapter_factory=type(adapter))
        returned_package = import_package_result(
            content=result_raw,
            result_identity=record_identity(result_raw),
            input_record=raw,
            input_identity=record_identity(raw),
            records=records,
            cas=returned,
            deadline=deadline,
            admission_guard=lambda: None,
            verify_package=verifier,
            blob_source=destination.get_bytes,
        )
        case.assertEqual(returned_package, result.package)
        if kind is PackageKind.ARCHIVE:
            malicious_blob = destination.put_bytes(
                b"not the declared archive", media_type="application/zip"
            )
            forged = replace(
                result,
                package=replace(
                    result.package,
                    artifacts=(
                        replace(result.package.artifacts[0], blob=malicious_blob),
                    ),
                ),
            )
            forged_raw = forged.to_bytes()
            # Metadata and byte hashes alone cannot prove archive semantics.
            case.assertEqual(
                PackageWorkerResult.admit(
                    forged_raw,
                    record_identity(forged_raw),
                    input_record=raw,
                    input_identity=record_identity(raw),
                    deadline=deadline,
                    read_blob=destination.get_bytes,
                ),
                forged,
            )
            with case.assertRaises(ActionWireError) as error:
                import_package_result(
                    content=forged_raw,
                    result_identity=record_identity(forged_raw),
                    input_record=raw,
                    input_identity=record_identity(raw),
                    records=records,
                    cas=returned,
                    deadline=deadline,
                    admission_guard=lambda: None,
                    verify_package=verifier,
                    blob_source=destination.get_bytes,
                )
            case.assertEqual(error.exception.code, "action_package.format_mismatch")
        revoked = [False]

        def require_current(state=revoked):
            if state[0]:
                raise ActionWireError("fixture.revoked", "return authority changed")

        def fetch_and_revoke(reference, state=revoked, source=destination):
            state[0] = True
            return source.get_bytes(reference)

        with case.assertRaises(ActionWireError) as error:
            import_package_result(
                content=result_raw,
                result_identity=record_identity(result_raw),
                input_record=raw,
                input_identity=record_identity(raw),
                records=records,
                cas=FileSystemCAS(cas.root.parent / ("revoked-" + kind.value)),
                deadline=deadline,
                admission_guard=require_current,
                verify_package=verifier,
                blob_source=fetch_and_revoke,
            )
        case.assertEqual(error.exception.code, "fixture.revoked")
        with case.assertRaises(ActionWireError):
            PackageWorkerResult.admit(
                result_raw,
                record_identity(result_raw),
                input_record=raw,
                input_identity=record_identity(raw),
                deadline=deadline,
                read_blob=lambda ref: b"corrupt",
            )
        with case.assertRaises(ActionWireError) as error:
            execute_package_input(
                input_record=raw,
                input_identity=record_identity(raw),
                records=records,
                cas=destination,
                deadline=deadline,
                admission_guard=lambda: None,
                package_adapter=adapter,
                packager_identity=canonical_identity("different-packager"),
            )
        case.assertEqual(error.exception.code, "action_package.packager_mismatch")

        from unittest.mock import Mock

        from literate_ai.storage.cas import BlobIntegrityError

        bad_input = packaged_input.plan.inputs[0].blob
        destination.path_for(bad_input).write_bytes(b"corrupt cached package input")
        packager = Mock(wraps=adapter)
        fetch = Mock(wraps=source_cas.get_bytes)
        with case.assertRaises(BlobIntegrityError):
            execute_package_input(
                input_record=raw,
                input_identity=record_identity(raw),
                records=records,
                cas=destination,
                deadline=deadline,
                admission_guard=lambda: None,
                package_adapter=packager,
                packager_identity=packaged_input.plan.packager_identity,
                blob_source=fetch,
                read_created_blob=getattr(adapter, "read_created_blob", None),
            )
        packager.package.assert_not_called()
        fetch.assert_not_called()
