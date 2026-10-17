"""One-shot command receiver for supported production lifecycle phases."""

from __future__ import annotations

import argparse
import errno
import os
import secrets
import sys
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_accept_worker import ConfiguredAcceptWorker
from literate_ai.adapters.action_authorization import execute_authorization_action
from literate_ai.adapters.action_blob_source import HttpActionBlobSource
from literate_ai.adapters.action_build_intent import execute_build_intent_action
from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_capabilities import (
    CAPABILITY_REQUEST_SCHEMA,
    MAX_CAPABILITY_BYTES,
    decode_capability_request,
    decode_capability_response,
    encode_capability_response,
    receiver_code_identity,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_WIRE_BYTES,
    ActionWireError,
    attest_action_response,
    decode_attested_action_request,
    encode_action_response,
)
from literate_ai.adapters.action_execute_worker import ConfiguredExecuteWorker
from literate_ai.adapters.action_finalize_worker import ConfiguredFinalizeWorker
from literate_ai.adapters.action_generate_worker import ConfiguredGenerateWorker
from literate_ai.adapters.action_hardware import (
    decode_hardware_request,
    encode_hardware_response,
)
from literate_ai.adapters.action_link import execute_link_action
from literate_ai.adapters.action_observation_failure import encode_observation_failure
from literate_ai.adapters.action_package_worker import ConfiguredPackageWorker
from literate_ai.adapters.action_plan import execute_plan_action
from literate_ai.adapters.action_source_index import execute_source_index_action
from literate_ai.adapters.action_test_worker import ConfiguredTestWorker
from literate_ai.adapters.action_tool_dependencies import (
    decode_dependency_request,
    encode_dependency_response,
)
from literate_ai.adapters.action_tool_observation import (
    encode_tool_observation_response,
)
from literate_ai.adapters.action_tool_selectors import (
    decode_selector_request,
    encode_selector_response,
)
from literate_ai.adapters.cache.filesystem import _read_regular_file
from literate_ai.adapters.worker_tool_dependencies import (
    WorkerDependencyGraphLimitError,
)
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import canonical_json_bytes
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.storage import FileSystemCAS
from literate_ai.storage.cas import StorageError


def _describe(request, deadline, expected_worker, *, http_source, workers):
    """Encode this receiver's capability exactly as ``--describe`` reports it."""
    build_worker = workers["build_worker"]
    test_worker = workers["test_worker"]
    execute_worker = workers["execute_worker"]
    accept_worker = workers["accept_worker"]
    generate_worker = workers["generate_worker"]
    package_worker = workers["package_worker"]
    finalize_worker = workers["finalize_worker"]
    finalize_profile = finalize_worker.identity if finalize_worker else None
    if package_worker is not None:
        package_worker.require_current()
    response = encode_capability_response(
        request,
        deadline,
        expected_worker,
        http_source=http_source,
        test_profile=test_worker.identity if test_worker else None,
        test_toolchains=test_worker.tools.identities if test_worker else (),
        test_standard_tools=test_worker.standard_tools_identity
        if test_worker
        else None,
        execute_profile=execute_worker.identity if execute_worker else None,
        accept_profile=accept_worker.identity if accept_worker else None,
        generate_profile=generate_worker.identity if generate_worker else None,
        package_profile=package_worker.identity if package_worker else None,
        finalize_profile=finalize_profile,
        execute_toolchains=execute_worker.tools.identities if execute_worker else (),
        execute_standard_tools=execute_worker.standard_tools_identity
        if execute_worker
        else None,
        build_profile=build_worker.identity if build_worker else None,
        build_toolchains=build_worker.tools.identities if build_worker else (),
        build_standard_tools=build_worker.standard_tools_identity
        if build_worker
        else None,
    )
    if package_worker is not None:
        package_worker.require_current()
    if finalize_worker is not None and finalize_worker.identity != finalize_profile:
        raise ActionWireError(
            "action_finalize.profile_changed", "private startup changed"
        )
    return response


def _require_capability(expected, deadline, expected_worker, **describe):
    """Measure this receiver as a fresh ``--describe`` would, in process.

    A request that names its worker's admitted capability is refused unless the
    receiver measures exactly that capability before and after the action, so the
    controller need not launch separate probes at each action boundary.
    """
    request_content = canonical_json_bytes(
        {
            "schema": CAPABILITY_REQUEST_SCHEMA,
            "worker_identity": expected_worker.uri,
            "nonce": secrets.token_hex(16),
            "deadline": deadline.to_dict(),
        }
    )
    request, _ = decode_capability_request(request_content)
    measured = decode_capability_response(
        _describe(request, deadline, expected_worker, **describe),
        request_content,
        receiver_code_identity(deadline=deadline),
    ).capability_identity
    if measured != expected:
        raise ActionWireError(
            "action_admission.runtime_changed",
            "worker runtime or supported capabilities changed",
        )


def _custody_failure_code(exc: BaseException) -> str:
    """Name the failure class and errno symbol, never its message or paths."""

    def os_kind(error: OSError) -> str:
        code = error.errno if isinstance(error.errno, int) else 0
        name = errno.errorcode.get(code, "")
        return "os" + ("." + name.lower() if name else "")

    if isinstance(exc, StorageError):
        kind = "storage"
        if isinstance(exc.__cause__, OSError):
            kind += "." + os_kind(exc.__cause__)
    elif isinstance(exc, OSError):
        kind = os_kind(exc)
    elif isinstance(exc, ValueError):
        kind = "value"
    else:
        kind = "runtime"
    return f"action_capability.custody_unavailable.{kind}"


def main(
    argv=None,
    *,
    build_worker: ConfiguredBuildWorker | None = None,
    test_worker: ConfiguredTestWorker | None = None,
    execute_worker: ConfiguredExecuteWorker | None = None,
    accept_worker: ConfiguredAcceptWorker | None = None,
    generate_worker: ConfiguredGenerateWorker | None = None,
    package_worker: ConfiguredPackageWorker | None = None,
    finalize_worker: ConfiguredFinalizeWorker | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-identity-env", default="LITAI_ACTION_WORKER_IDENTITY")
    parser.add_argument("--cas", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--source-cas-url")
    parser.add_argument("--source-token-env")
    parser.add_argument("--allow-http", action="store_true")
    observations = parser.add_mutually_exclusive_group()
    observations.add_argument("--describe", action="store_true")
    observations.add_argument("--describe-hardware", action="store_true")
    observations.add_argument("--describe-tools", action="store_true")
    observations.add_argument("--describe-tool-dependencies", action="store_true")
    observations.add_argument("--verify-tool-selectors", action="store_true")
    # Read-only: stage a FINALIZE request and report the grant it needs.
    observations.add_argument("--describe-finalize-grant", action="store_true")
    parser.add_argument("--request-file", type=Path)
    args = parser.parse_args(argv)
    capability = None
    measured = False
    describing = (
        args.describe
        or args.describe_hardware
        or args.describe_tools
        or args.describe_tool_dependencies
        or args.verify_tool_selectors
    )
    try:
        if build_worker is not None and not isinstance(
            build_worker, ConfiguredBuildWorker
        ):
            raise ValueError("invalid private BUILD configuration")
        if test_worker is not None and not isinstance(
            test_worker, ConfiguredTestWorker
        ):
            raise ValueError("invalid private TEST configuration")
        if execute_worker is not None and not isinstance(
            execute_worker, ConfiguredExecuteWorker
        ):
            raise ValueError("invalid private EXECUTE configuration")
        if accept_worker is not None and not isinstance(
            accept_worker, ConfiguredAcceptWorker
        ):
            raise ValueError("invalid private ACCEPT configuration")
        if generate_worker is not None and not isinstance(
            generate_worker, ConfiguredGenerateWorker
        ):
            raise ValueError("invalid private GENERATE configuration")
        if package_worker is not None and not isinstance(
            package_worker, ConfiguredPackageWorker
        ):
            raise ValueError("invalid private PACKAGE configuration")
        if finalize_worker is not None and not isinstance(
            finalize_worker, ConfiguredFinalizeWorker
        ):
            raise ValueError("invalid private FINALIZE configuration")
        expected_worker = ContentIdentity.parse_uri(
            os.environ.get(args.worker_identity_env, "")
        )
        limit = MAX_CAPABILITY_BYTES if describing else MAX_ACTION_WIRE_BYTES
        content = (
            sys.stdin.buffer.read(limit + 1)
            if args.request_file is None
            else _read_regular_file(
                args.request_file,
                maximum_bytes=limit,
                code="action_source.request_invalid",
            )
        )
        if args.verify_tool_selectors:
            request, deadline = decode_selector_request(content)
        elif args.describe_tool_dependencies:
            request, deadline = decode_dependency_request(content)
        elif args.describe_hardware:
            request, deadline = decode_hardware_request(content)
        elif args.describe or args.describe_tools:
            request, deadline = decode_capability_request(content)
        else:
            request, deadline, records, capability = decode_attested_action_request(
                content
            )
    except (ValueError, OSError, RuntimeError):
        print("Invalid lifecycle-action request or receiver binding", file=sys.stderr)
        return 2
    try:
        if not args.cas.is_absolute() or not args.workspace.is_absolute():
            raise ActionWireError(
                "action_source.private_path_invalid",
                "worker CAS and workspace bindings must be absolute",
            )
        source = None
        if args.source_token_env and not args.source_cas_url:
            raise ActionWireError(
                "action_source.endpoint_missing",
                "source credential requires an endpoint",
            )
        if args.source_cas_url:
            source = HttpActionBlobSource(
                args.source_cas_url,
                deadline,
                bearer_token=(
                    os.environ.get(args.source_token_env, "")
                    if args.source_token_env
                    else None
                ),
                allow_http=args.allow_http,
            )
        cas = FileSystemCAS(args.cas, create=False)
        require_safe_directory(args.workspace)
        if args.describe_hardware:
            response = encode_hardware_response(request, deadline, expected_worker)
            sys.stdout.buffer.write(response)
            return 0
        describe = {
            "http_source": source is not None,
            "workers": {
                "build_worker": build_worker,
                "test_worker": test_worker,
                "execute_worker": execute_worker,
                "accept_worker": accept_worker,
                "generate_worker": generate_worker,
                "package_worker": package_worker,
                "finalize_worker": finalize_worker,
            },
        }
        if args.describe:
            response = _describe(request, deadline, expected_worker, **describe)
            sys.stdout.buffer.write(response)
            return 0
        if args.verify_tool_selectors:
            response = encode_selector_response(
                request,
                deadline,
                expected_worker,
                build_worker=build_worker,
                test_worker=test_worker,
                execute_worker=execute_worker,
                accept_worker=accept_worker,
                generate_worker=generate_worker,
                package_worker=package_worker,
                finalize_worker=finalize_worker,
                http_source=source is not None,
            )
            sys.stdout.buffer.write(response)
            return 0
        if args.describe_tool_dependencies:
            response = encode_dependency_response(
                request,
                deadline,
                expected_worker,
                build_worker=build_worker,
                test_worker=test_worker,
                execute_worker=execute_worker,
                accept_worker=accept_worker,
                generate_worker=generate_worker,
                package_worker=package_worker,
                finalize_worker=finalize_worker,
                http_source=source is not None,
            )
            sys.stdout.buffer.write(response)
            return 0
        if args.describe_tools:
            response = encode_tool_observation_response(
                request,
                deadline,
                expected_worker,
                build_worker=build_worker,
                test_worker=test_worker,
                execute_worker=execute_worker,
                accept_worker=accept_worker,
                generate_worker=generate_worker,
                package_worker=package_worker,
                finalize_worker=finalize_worker,
                http_source=source is not None,
            )
            sys.stdout.buffer.write(response)
            return 0
        if capability is not None:
            _require_capability(capability, deadline, expected_worker, **describe)
            measured = True
        if (
            args.describe_finalize_grant
            and request.action.kind is not LifecycleActionKind.FINALIZE
        ):
            raise ActionWireError(
                "action_transport.mode_invalid", "grant description is FINALIZE only"
            )
        if request.action.kind is LifecycleActionKind.BUILD:
            if build_worker is None:
                raise ActionWireError(
                    "action_build.not_configured", "BUILD is not configured"
                )
            result = build_worker.execute(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker,
                cas=cas,
                workspace_root=args.workspace,
                blob_source=None if source is None else source.fetch,
            )
        elif request.action.kind is LifecycleActionKind.TEST:
            if test_worker is None:
                raise ActionWireError(
                    "action_test.not_configured", "TEST is not configured"
                )
            result = test_worker.execute(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker,
                cas=cas,
                workspace_root=args.workspace,
                blob_source=None if source is None else source.fetch,
            )
        elif request.action.kind is LifecycleActionKind.EXECUTE:
            if execute_worker is None:
                raise ActionWireError(
                    "action_execute.not_configured", "EXECUTE is not configured"
                )
            result = execute_worker.execute(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker,
                cas=cas,
                workspace_root=args.workspace,
                blob_source=None if source is None else source.fetch,
            )
        elif request.action.kind is LifecycleActionKind.ACCEPT:
            if accept_worker is None:
                raise ActionWireError(
                    "action_accept.not_configured", "ACCEPT is not configured"
                )
            result = accept_worker.execute(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker,
                cas=cas,
                workspace_root=args.workspace,
                blob_source=None if source is None else source.fetch,
            )
        elif request.action.kind is LifecycleActionKind.GENERATE:
            if generate_worker is None:
                raise ActionWireError(
                    "action_generate.not_configured", "GENERATE is not configured"
                )
            result = generate_worker.execute(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker,
                cas=cas,
                workspace_root=args.workspace,
                blob_source=None if source is None else source.fetch,
            )
        elif request.action.kind is LifecycleActionKind.PACKAGE:
            if package_worker is None:
                raise ActionWireError(
                    "action_package.not_configured", "PACKAGE is not configured"
                )

            def package_current():
                if (
                    ContentIdentity.parse_uri(
                        os.environ.get(args.worker_identity_env, "")
                    )
                    != expected_worker
                ):
                    raise ActionWireError(
                        "action_package.worker_mismatch", "receiver binding changed"
                    )

            result = package_worker.execute(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker,
                cas=cas,
                admission_guard=package_current,
                blob_source=None if source is None else source.fetch,
            )
        elif request.action.kind is LifecycleActionKind.FINALIZE:
            if finalize_worker is None:
                raise ActionWireError(
                    "action_finalize.not_configured", "FINALIZE is not configured"
                )

            def finalize_current():
                if (
                    ContentIdentity.parse_uri(
                        os.environ.get(args.worker_identity_env, "")
                    )
                    != expected_worker
                ):
                    raise ActionWireError(
                        "action_finalize.worker_mismatch", "receiver binding changed"
                    )

            result = (
                finalize_worker.describe_grant
                if args.describe_finalize_grant
                else finalize_worker.execute
            )(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker,
                cas=cas,
                workspace_root=args.workspace,
                admission_guard=finalize_current,
                blob_source=None if source is None else source.fetch,
            )
        elif request.action.kind is LifecycleActionKind.LINK:

            def link_current():
                if (
                    ContentIdentity.parse_uri(
                        os.environ.get(args.worker_identity_env, "")
                    )
                    != expected_worker
                ):
                    raise ActionWireError(
                        "action_link.worker_mismatch", "receiver binding changed"
                    )

            result = execute_link_action(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker,
                cas=cas,
                admission_guard=link_current,
                blob_source=None if source is None else source.fetch,
            )
        elif request.action.kind is LifecycleActionKind.AUTHORIZE:
            result = execute_authorization_action(
                request, deadline, records, expected_worker_identity=expected_worker
            )
        elif request.action.kind is LifecycleActionKind.BUILD_INTENT:
            result = execute_build_intent_action(
                request, deadline, records, expected_worker_identity=expected_worker
            )
        elif request.action.kind is LifecycleActionKind.PLAN:
            result = execute_plan_action(
                request, deadline, records, expected_worker_identity=expected_worker
            )
        else:
            result = execute_source_index_action(
                request,
                deadline,
                records,
                expected_worker_identity=expected_worker,
                cas=cas,
                workspace_root=args.workspace,
                blob_source=None if source is None else source.fetch,
            )
        response = encode_action_response(request, result_record=result)
    except WorkerDependencyGraphLimitError as exc:
        if not describing:
            response = encode_action_response(
                request, failure_code="action_dependencies.graph_limit"
            )
        else:
            sys.stdout.buffer.write(
                encode_observation_failure(
                    "action_dependencies.graph_limit", exc.metrics
                )
            )
            return 2
    except ActionWireError as exc:
        if describing:
            sys.stdout.buffer.write(encode_observation_failure(exc.code))
            print("Action capability request refused", file=sys.stderr)
            return 2
        response = encode_action_response(request, failure_code=exc.code)
    except (StorageError, OSError, ValueError, RuntimeError) as exc:
        if describing:
            sys.stdout.buffer.write(
                encode_observation_failure(_custody_failure_code(exc))
            )
            print("Action capability custody unavailable", file=sys.stderr)
            return 2
        # Return a bounded code, never private host paths or blob contents.
        response = encode_action_response(
            request, failure_code="action_source.custody_unavailable"
        )
    if measured:
        # Attest only when the admitted capability held at both boundaries. An
        # unattested response makes the controller probe the worker instead.
        try:
            _require_capability(capability, deadline, expected_worker, **describe)
            response = attest_action_response(response, request, capability)
        except (ActionWireError, StorageError, OSError, ValueError, RuntimeError):
            pass
    sys.stdout.buffer.write(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
