"""FINALIZE proof checks on actual returned command PACKAGE and accepted LINK data."""

import io
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_loader import load_finalize_records
from literate_ai.adapters.action_finalize_portable import (
    verify_portable_finalize_stages,
)
from literate_ai.adapters.action_finalize_proof import reopen_finalize_input
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.adapters.action_finalize_result import import_finalize_result
from literate_ai.adapters.action_finalize_result_record import FinalizeWorkerResult
from literate_ai.adapters.action_package_execution import PackageWorkerResult
from literate_ai.adapters.action_package_result import verify_deterministic_package
from literate_ai.adapters.packaging import DirectoryPackageAdapter
from literate_ai.application.standard_project_lifecycle import StandardProjectBuildPlan
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.storage import FileSystemCAS


def assert_finalize_proof(
    case,
    snapshot,
    execution,
    linker,
    accepted,
    graph,
    plan,
    result,
    source_cas,
    runtime_template,
):
    package_input, proof = linker.package_handoff(graph, plan)
    package_raw = PackageWorkerResult(
        record_identity(package_input.to_bytes()), result
    ).to_bytes()
    value = FinalizeWorkerInput(
        snapshot.authority.lock,
        StandardProjectBuildPlan(
            execution.identity,
            tuple(
                sorted(
                    (plan for plan, _ in accepted.values()),
                    key=lambda plan: plan.component_revision.uri,
                )
            ),
        ),
        package_input,
        record_identity(package_raw),
    )
    records = proof | {value.package_result_identity: package_raw}
    parent = linker.indexer.cas.root.parent
    staged_proof = FileSystemCAS(parent / "finalize-loader")
    for identity, content in records.items():
        case.assertEqual(staged_proof.put_bytes(content).identity, identity.uri)
    staged_proof.put_bytes(b"unrelated-cache-record")
    load = partial(
        load_finalize_records,
        value,
        staged_proof,
        linker.indexer.deadline,
        admission_guard=lambda: None,
    )
    case.assertEqual(load(), records)
    with (
        patch("literate_ai.adapters.action_finalize_loader.MAX_ACTION_RECORD_BYTES", 0),
        patch.object(staged_proof, "get_bytes", side_effect=AssertionError("read")),
        case.assertRaises(ActionWireError) as error,
    ):
        load()
    case.assertEqual(error.exception.code, "action_finalize.proof_oversized")
    from literate_ai.storage.cas import BlobIntegrityError

    package_ref = staged_proof.put_bytes(package_raw)
    package_path = staged_proof.path_for(package_ref)
    package_path.chmod(0o600)
    package_path.write_bytes(b"corrupt")
    with case.assertRaises(BlobIntegrityError):
        load()
    package_path.unlink()
    with case.assertRaises(FileNotFoundError):
        load()
    staged_proof.put_bytes(package_raw)
    with case.assertRaisesRegex(PermissionError, "revoked"):
        load_finalize_records(
            value,
            staged_proof,
            linker.indexer.deadline,
            admission_guard=Mock(side_effect=PermissionError("revoked")),
        )
    fetch = Mock(side_effect=linker.indexer.cas.get_bytes)
    args = dict(
        input_record=value.to_bytes(),
        input_identity=record_identity(value.to_bytes()),
        records=records,
        cas=FileSystemCAS(parent / "finalize-proof"),
        deadline=linker.indexer.deadline,
        admission_guard=lambda: None,
        verify_package=partial(
            verify_deterministic_package, adapter_factory=DirectoryPackageAdapter
        ),
        blob_source=fetch,
    )
    from literate_ai.adapters.action_finalize import reopen_finalize_action
    from tests.support.fixtures_test_action_finalize import make_finalize_request

    request, dispatch_records = make_finalize_request(
        value, records, linker.indexer.deadline
    )
    case.assertEqual(
        reopen_finalize_action(
            request,
            linker.indexer.deadline,
            dispatch_records,
            expected_worker_identity=request.worker.worker_identity,
            cas=args["cas"],
            admission_guard=args["admission_guard"],
            verify_package=args["verify_package"],
            blob_source=fetch,
        ),
        (value, result),
    )
    case.assertTrue(fetch.called)
    from literate_ai.adapters.action_finalize_inputs import (
        materialize_finalize_inputs,
    )

    workspace = parent / "finalize-package-workspace"
    workspace.mkdir()
    with materialize_finalize_inputs(
        **(args | {"blob_source": source_cas.get_bytes}), workspace_root=workspace
    ) as prepared:
        case.assertEqual((prepared.intent, prepared.package), (value, result))
        custody = prepared.package_tree
        staged_root = custody.root
        root_build = prepared.root_build
        source_root = prepared.source_trees.resolve(root_build.candidate.tree_identity)
        source_custody = prepared.source_trees.evidence(
            root_build.candidate.tree_identity
        )
        case.assertEqual(source_custody.identity, root_build.source_custody_identity)
        case.assertEqual(
            source_custody.generated_test_suite.content_identity,
            root_build.candidate.generated_test_suite_identity.uri,
        )
        case.assertEqual(len(prepared.component_builds), len(accepted))
        for item in value.package_input.plan.inputs:
            case.assertEqual(
                staged_root.joinpath(item.path).read_bytes(),
                linker.indexer.cas.get_bytes(item.blob),
            )
        custody.require_unchanged()

        from literate_ai.adapters.action_finalize_runtime import bind_finalize_runtime
        from literate_ai.adapters.action_finalize_stages import execute_finalize_stages
        from literate_ai.adapters.lifecycle.standard_local import (
            LocalIndependentAcceptanceCase,
            LocalStandardLifecyclePorts,
        )
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceRecorder,
        )

        class ExactOutputOracle:
            identity = canonical_identity("finalize-fixture-output-oracle")

            def cases(self, lock):
                case.assertEqual(lock.identity, snapshot.authority.lock.identity)
                return (
                    LocalIndependentAcceptanceCase.create(
                        "known-output", [], "known-output"
                    ),
                )

        ports = LocalStandardLifecyclePorts(
            source_trees=prepared.source_trees,
            object_root=workspace / "runtime",
            contracts=tuple(
                build.inputs.contract for build in prepared.component_builds
            ),
            tool_bindings=tuple(runtime_template.tool_bindings.values()),
            independent_acceptance_oracle=ExactOutputOracle(),
        )
        ports.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000)
        )
        revision = prepared.root_build.plan.component_revision.uri
        contract = ports.contracts[revision]
        ports.contracts[revision] = object()
        try:
            with case.assertRaises(ActionWireError) as error:
                with bind_finalize_runtime(
                    prepared, ports, admission_guard=lambda: None
                ):
                    case.fail("changed private contract was admitted")
            case.assertEqual(error.exception.code, "action_finalize.runtime_mismatch")
            case.assertEqual(ports._project_packages, {})
        finally:
            ports.contracts[revision] = contract
        with case.assertRaisesRegex(RuntimeError, "interrupted runtime"):
            with bind_finalize_runtime(prepared, ports, admission_guard=lambda: None):
                raise RuntimeError("interrupted runtime")
        case.assertEqual(ports._project_packages, {})
        case.assertEqual(ports._exports_by_identity, {})
        from tests.support.finalize_profile_fixture import assert_finalize_profile

        grant_guard = assert_finalize_profile(case, value, ports)
        authority = Mock(wraps=grant_guard.require_stage)
        with bind_finalize_runtime(prepared, ports, admission_guard=lambda: None):
            with case.assertRaises(ActionWireError) as error:
                with bind_finalize_runtime(
                    prepared, ports, admission_guard=lambda: None
                ):
                    case.fail("occupied runtime was admitted")
            case.assertEqual(error.exception.code, "action_finalize.runtime_occupied")
            finalized = execute_finalize_stages(
                prepared,
                ports=ports,
                deadline=linker.indexer.deadline,
                admission_guard=lambda: None,
                require_execution_authority=authority,
            )
            case.assertEqual(finalized.package_result, result)
            case.assertEqual(authority.call_count, 6)
            retained = dict(ports.retained_evidence_records())
            for identity in (
                finalized.root_generated_integration_test_identity,
                finalized.packaged_execution_identity,
                finalized.independent_acceptance_identity,
            ):
                case.assertIn(identity, retained)
            retained[finalized.identity] = canonical_json_bytes(finalized.to_dict())
            verifier = partial(
                verify_portable_finalize_stages,
                prepared=prepared,
                oracle=ports.independent_acceptance_oracle,
            )
            verifier(value, finalized, retained)
            # A self-consistent rehash cannot turn failed generated tests into proof.
            test_doc = json.loads(
                retained[finalized.root_generated_integration_test_identity]
            )
            failed_output = canonical_json_bytes(
                json.dumps(
                    {"schema": "literate-ai/generated-test-results@1", "cases": []}
                )
            )
            changed_test = canonical_json_bytes(
                test_doc | {"stdout_identity": record_identity(failed_output).uri}
            )
            with case.assertRaises(ActionWireError):
                verifier(
                    value,
                    replace(
                        finalized,
                        root_generated_integration_test_identity=record_identity(
                            changed_test
                        ),
                    ),
                    retained
                    | {
                        record_identity(failed_output): failed_output,
                        record_identity(changed_test): changed_test,
                    },
                )
            acceptance_doc = json.loads(
                retained[finalized.independent_acceptance_identity]
            )
            changed_acceptance = canonical_json_bytes(
                acceptance_doc
                | {"oracle_identity": canonical_identity("foreign-oracle").uri}
            )
            with case.assertRaises(ActionWireError):
                verifier(
                    value,
                    replace(
                        finalized,
                        independent_acceptance_identity=record_identity(
                            changed_acceptance
                        ),
                    ),
                    retained
                    | {record_identity(changed_acceptance): changed_acceptance},
                )
            wrong_result = canonical_json_bytes("wrong-output")
            wrong_stdout = canonical_json_bytes(json.dumps("wrong-output"))
            observations = acceptance_doc["observations"]
            changed_acceptance = canonical_json_bytes(
                acceptance_doc
                | {
                    "observations": [
                        observations[0]
                        | {
                            "stdout_identity": record_identity(wrong_stdout).uri,
                            "result_identity": record_identity(wrong_result).uri,
                        },
                        *observations[1:],
                    ]
                }
            )
            with case.assertRaises(ActionWireError):
                verifier(
                    value,
                    replace(
                        finalized,
                        independent_acceptance_identity=record_identity(
                            changed_acceptance
                        ),
                    ),
                    retained
                    | {
                        record_identity(changed_acceptance): changed_acceptance,
                        record_identity(wrong_result): wrong_result,
                        record_identity(wrong_stdout): wrong_stdout,
                    },
                )
            returned = FinalizeWorkerResult(
                args["input_identity"],
                finalized,
                tuple(
                    sorted(
                        (args["cas"].put_bytes(raw) for raw in retained.values()),
                        key=lambda ref: ref.identity,
                    )
                ),
            )
            raw_returned = returned.to_bytes()
            case.assertEqual(
                import_finalize_result(
                    content=raw_returned,
                    result_identity=record_identity(raw_returned),
                    verify_stages=verifier,
                    **args,
                ),
                finalized,
            )
            case.assertEqual(
                FinalizeWorkerResult.admit(
                    raw_returned,
                    record_identity(raw_returned),
                    input_record=args["input_record"],
                    input_identity=args["input_identity"],
                    deadline=linker.indexer.deadline,
                ),
                returned,
            )
        case.assertEqual(ports._project_packages, {})
        case.assertEqual(ports._exports_by_identity, {})
        # The runtime object root is factory-owned, not package/source staging.
        (workspace / "runtime").rmdir()
    case.assertFalse(source_root.exists())
    case.assertFalse(staged_root.exists())
    case.assertEqual(list(workspace.iterdir()), [])

    from literate_ai.adapters.action_finalize_execution import (
        FinalizeWorkerRuntime,
        execute_worker_finalize_from_cas,
    )

    @contextmanager
    def runtime_factory(prepared):
        runtime_root = workspace / "runtime"
        ports = LocalStandardLifecyclePorts(
            source_trees=prepared.source_trees,
            object_root=runtime_root,
            contracts=tuple(
                build.inputs.contract for build in prepared.component_builds
            ),
            tool_bindings=tuple(runtime_template.tool_bindings.values()),
            independent_acceptance_oracle=ExactOutputOracle(),
        )
        try:
            yield FinalizeWorkerRuntime(ports)
        finally:
            case.assertEqual(ports._project_packages, {})
            case.assertEqual(ports._exports_by_identity, {})
            runtime_root.rmdir()

    authority = Mock()
    worker_result = execute_worker_finalize_from_cas(
        **args,
        workspace_root=workspace,
        runtime_factory=runtime_factory,
        require_execution_authority=authority,
    )
    worker_returned = FinalizeWorkerResult.admit(
        worker_result,
        record_identity(worker_result),
        input_record=args["input_record"],
        input_identity=args["input_identity"],
        deadline=linker.indexer.deadline,
    )
    case.assertEqual(worker_returned.evidence, finalized)
    case.assertEqual(authority.call_count, 6)
    case.assertEqual(list(workspace.iterdir()), [])
    with case.assertRaisesRegex(PermissionError, "denied"):
        execute_worker_finalize_from_cas(
            **args,
            workspace_root=workspace,
            runtime_factory=runtime_factory,
            require_execution_authority=Mock(side_effect=PermissionError("denied")),
        )
    case.assertEqual(list(workspace.iterdir()), [])

    # Real isolated child: no patched execution/materialization/proof callbacks.
    from literate_ai.adapters.action_finalize_worker import ConfiguredFinalizeWorker
    from literate_ai.adapters.builders.python import discover_python_toolchain
    from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding

    for identity, content in records.items():
        case.assertEqual(args["cas"].put_bytes(content).identity, identity.uri)
    from literate_ai.adapters.action_finalize_grant import (
        FileFinalizeExecutionAuthority,
    )
    from literate_ai.adapters.action_finalize_profile import (
        portable_finalize_runtime_identity,
    )
    from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry

    grant_path = parent / "finalize-private-grant.json"
    grant = grant_guard.grant_provider()
    grant_path.write_bytes(canonical_json_bytes(grant.to_dict()))
    repository = Path(__file__).resolve().parents[2]
    private_profile = grant_guard.runtime_identity
    startup = (
        f"import sys; sys.path[:0]=[{str(repository)!r},{str(repository / 'src')!r}]; "
        "from tests.support.finalize_child_fixture import run_authorized; "
        f"raise SystemExit(run_authorized({str(grant_path)!r},{private_profile.uri!r}))"
    )
    environment = dict(os.environ)
    observe = partial(
        portable_finalize_runtime_identity,
        value,
        ports,
        startup=WorkerToolchainRegistry(tuple(ports.tool_bindings.values())),
        environment=environment,
    )
    case.assertEqual(observe(), private_profile)
    parent_authority = Mock(
        wraps=FileFinalizeExecutionAuthority(
            grant_path=grant_path,
            runtime_identity=private_profile,
            observe_runtime=observe,
        )
    )
    runtime = discover_python_toolchain(pinned_command=(sys.executable,))
    worker = ConfiguredFinalizeWorker(
        launcher=LocalComponentToolBinding(
            sys.executable,
            ("-I", "-B", "-c", startup),
            authority_identity=canonical_identity(
                {"runtime": runtime.identity, "code": startup}
            ),
            _authority_guard=runtime.require_unchanged,
        ),
        environment=environment,
        runtime_identity=private_profile,
        observe_runtime=observe,
        require_execution_authority=parent_authority,
        verify_package=args["verify_package"],
        verify_stages=lambda prepared, intent, evidence, proof: (
            verify_portable_finalize_stages(
                intent, evidence, proof, prepared=prepared, oracle=ExactOutputOracle()
            )
        ),
    )
    dispatch_args = dict(
        expected_worker_identity=request.worker.worker_identity,
        cas=args["cas"],
        workspace_root=workspace,
        admission_guard=lambda: None,
        blob_source=fetch,
    )
    with case.assertRaises(ActionWireError):
        worker.execute(
            request,
            linker.indexer.deadline,
            dispatch_records,
            **dispatch_args,
            cancelled=lambda: True,
        )
    case.assertEqual(list(workspace.iterdir()), [])
    from literate_ai import action_worker
    from literate_ai.adapters.action_dispatch_wire import (
        decode_action_response,
        encode_action_request,
    )

    def receive(configured):
        output = io.BytesIO()
        with (
            patch.dict(
                os.environ,
                LITAI_ACTION_WORKER_IDENTITY=request.worker.worker_identity.uri,
            ),
            patch.object(
                action_worker.sys,
                "stdin",
                SimpleNamespace(
                    buffer=io.BytesIO(
                        encode_action_request(
                            request, linker.indexer.deadline, dispatch_records
                        )
                    )
                ),
            ),
            patch.object(action_worker.sys, "stdout", SimpleNamespace(buffer=output)),
        ):
            status = action_worker.main(
                ["--cas", str(args["cas"].root), "--workspace", str(workspace)],
                finalize_worker=configured,
            )
        case.assertEqual(status, 0)
        return decode_action_response(output.getvalue(), request)

    refused, absent = receive(None)
    case.assertEqual(refused.failure_code, "action_finalize.not_configured")
    case.assertIsNone(absent)
    outcome, raw_child = receive(worker)
    case.assertIsNone(outcome.failure_code)
    case.assertGreaterEqual(parent_authority.call_count, 2)
    case.assertEqual(list(workspace.iterdir()), [])
    with materialize_finalize_inputs(**args, workspace_root=workspace) as received:
        verified = import_finalize_result(
            content=raw_child,
            result_identity=record_identity(raw_child),
            verify_stages=partial(
                verify_portable_finalize_stages,
                prepared=received,
                oracle=ExactOutputOracle(),
            ),
            **args,
        )
        case.assertEqual(verified, finalized)
    case.assertEqual(list(workspace.iterdir()), [])

    # Prove child stage authority independently of the parent's grant check.
    from literate_ai.adapters.action_finalize_process import run_finalize_worker_process

    grant_path.write_bytes(canonical_json_bytes(replace(grant, revoked=True).to_dict()))
    with case.assertRaises(ActionWireError):
        run_finalize_worker_process(
            launcher=worker.launcher,
            input_record=args["input_record"],
            input_identity=args["input_identity"],
            deadline=linker.indexer.deadline,
            cwd=workspace,
            environment=worker.environment,
            require_execution_authority=lambda intent: None,
            cas_root=args["cas"].root,
            workspace_root=workspace,
        )
    case.assertEqual(list(workspace.iterdir()), [])
    grant_path.unlink()

    # The controller now crosses the actual command transport and private receiver.
    from literate_ai.adapters.action_capabilities import (
        probe_command_action_capabilities,
    )
    from literate_ai.adapters.command_finalizer import CommandProjectFinalizer

    selected = linker.indexer.workers[0]
    capabilities = probe_command_action_capabilities(
        linker.indexer.catalog.worker(selected.worker_id),
        linker.indexer.deadline,
        cwd=linker.indexer.cwd,
        environment=linker.indexer.environment,
    )
    case.assertIsNotNone(capabilities.finalize_profile)
    linker.admission.supports_finalize = lambda worker, profile: (
        worker == selected and profile == capabilities.finalize_profile
    )
    from literate_ai.adapters.action_finalize_verification import (
        PortableFinalizeVerification,
    )

    configuration_guard = Mock()
    controller = CommandProjectFinalizer(
        linker,
        profile_identity=capabilities.finalize_profile,
        verify_package=args["verify_package"],
        stage_verifier_context=PortableFinalizeVerification(
            oracle=ExactOutputOracle(),
            workspace_root=workspace,
            require_configuration=configuration_guard,
        ),
        result_source=linker.result_source,
    )
    from literate_ai.adapters.action_finalize_parent import (
        PortableFinalizeParentAuthority,
        portable_child_environment,
    )
    from tests.support.finalize_profile_fixture import issue_finalize_grant

    private = parent / "finalize-private"
    grant_file = private / "grant.json"

    def run_controller():
        reservation = controller.try_reserve_finalize(
            value.component_lock, value.project_plan, graph, plan, result
        )
        case.assertIsNotNone(reservation)
        try:
            return reservation.run()
        finally:
            case.assertEqual(list(workspace.iterdir()), [])

    # The receiver refuses before staging any input when no grant exists.
    with case.assertRaises(ActionWireError) as refused:
        run_controller()
    case.assertEqual(refused.exception.code, "action_finalize.grant_unavailable")
    # The operator plans the exact request the receiver parent will measure.
    planner = PortableFinalizeParentAuthority(
        grant_path=grant_file,
        tool_bindings=(LocalComponentToolBinding(sys.executable),),
        oracle=ExactOutputOracle(),
    )
    child_environment = portable_child_environment(
        json.loads((private / "environment.json").read_text("utf-8"))
    )
    with (
        materialize_finalize_inputs(**args, workspace_root=workspace) as staged,
        tempfile.TemporaryDirectory() as measured,
    ):
        request_to_grant = planner.grant_request(
            staged, Path(measured).resolve(), child_environment
        )
    case.assertEqual(list(workspace.iterdir()), [])
    # The operator obtains the same request from the receiver itself through a
    # describe-only dispatch that stages and measures but executes nothing.
    case.assertEqual(
        controller.describe_grant(
            value.component_lock, value.project_plan, graph, plan, result
        ),
        request_to_grant,
    )
    case.assertFalse(grant_file.exists())
    # A grant naming another runtime passes scope admission, but the receiver
    # parent's own measurement refuses it. A child refusal would surface as a
    # process failure, so this code proves the parent guard decided.
    with (
        materialize_finalize_inputs(**args, workspace_root=workspace) as staged,
        tempfile.TemporaryDirectory() as measured,
    ):
        other_runtime = planner.grant_request(
            staged,
            Path(measured).resolve(),
            dict(child_environment, LITAI_FIXTURE_PROFILE="other"),
        )
    case.assertNotEqual(other_runtime, request_to_grant)
    grant_file.write_bytes(
        canonical_json_bytes(issue_finalize_grant(value, other_runtime).to_dict())
    )
    with case.assertRaises(ActionWireError) as refused:
        run_controller()
    case.assertEqual(refused.exception.code, "action_finalize.authority_invalid")
    grant_file.write_bytes(
        canonical_json_bytes(issue_finalize_grant(value, request_to_grant).to_dict())
    )
    case.assertEqual(run_controller(), finalized)
    case.assertGreater(configuration_guard.call_count, 2)
    # Atomic revocation stops the next dispatch at the receiver.
    revoked_grant = private / "grant.next"
    revoked_grant.write_bytes(
        canonical_json_bytes(
            issue_finalize_grant(value, request_to_grant, revoked=True).to_dict()
        )
    )
    os.replace(revoked_grant, grant_file)
    with case.assertRaises(ActionWireError) as refused:
        run_controller()
    case.assertEqual(refused.exception.code, "action_finalize.authority_invalid")
    released = linker.indexer.slots.try_reserve(lambda worker, slot: None)
    case.assertIsNotNone(released)
    released.release()

    # Same realized manifests, different full build-plan runtime.
    plans = value.project_plan.components
    changed = replace(
        value,
        project_plan=replace(
            value.project_plan,
            components=(
                replace(
                    plans[0],
                    request=replace(
                        plans[0].request,
                        language_runtime_identity=canonical_identity("foreign"),
                    ),
                ),
                *plans[1:],
            ),
        ),
    )
    raw = changed.to_bytes()
    case.assertEqual(
        FinalizeWorkerInput.admit(raw, record_identity(raw), linker.indexer.deadline),
        changed,
    )
    fetch.reset_mock()
    with case.assertRaises(ActionWireError) as error:
        reopen_finalize_input(
            **(
                args
                | {
                    "input_record": raw,
                    "input_identity": record_identity(raw),
                }
            )
        )
    case.assertEqual(error.exception.code, "action_finalize.plan_mismatch")
    fetch.assert_not_called()
    for changed_records in (
        proof,
        records | {record_identity(b"extra"): b"extra"},
        records | {value.package_result_identity: b"corrupt"},
    ):
        with case.assertRaises(ActionWireError):
            reopen_finalize_input(**(args | {"records": changed_records}))
    revoked = False

    def current():
        if revoked:
            raise ActionWireError("fixture.revoked", "FINALIZE authority changed")

    def fetch_and_revoke(reference):
        nonlocal revoked
        revoked = True
        return linker.indexer.cas.get_bytes(reference)

    with case.assertRaises(ActionWireError) as error:
        reopen_finalize_input(
            **(
                args
                | {
                    "cas": FileSystemCAS(parent / "finalize-revoked"),
                    "admission_guard": current,
                    "blob_source": fetch_and_revoke,
                }
            )
        )
    case.assertEqual(error.exception.code, "fixture.revoked")
