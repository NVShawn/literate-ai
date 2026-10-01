"""One-shot command receiver for supported production lifecycle phases."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_accept_worker import ConfiguredAcceptWorker
from literate_ai.adapters.action_authorization import execute_authorization_action
from literate_ai.adapters.action_blob_source import HttpActionBlobSource
from literate_ai.adapters.action_build_intent import execute_build_intent_action
from literate_ai.adapters.action_build_worker import ConfiguredBuildWorker
from literate_ai.adapters.action_capabilities import (
    MAX_CAPABILITY_BYTES,
    decode_capability_request,
    encode_capability_response,
)
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_WIRE_BYTES,
    ActionWireError,
    decode_action_request,
    encode_action_response,
)
from literate_ai.adapters.action_execute_worker import ConfiguredExecuteWorker
from literate_ai.adapters.action_hardware import (
    decode_hardware_request,
    encode_hardware_response,
)
from literate_ai.adapters.action_observation_failure import encode_observation_failure
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
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.storage import FileSystemCAS
from literate_ai.storage.cas import StorageError


def main(
    argv=None,
    *,
    build_worker: ConfiguredBuildWorker | None = None,
    test_worker: ConfiguredTestWorker | None = None,
    execute_worker: ConfiguredExecuteWorker | None = None,
    accept_worker: ConfiguredAcceptWorker | None = None,
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
    parser.add_argument("--request-file", type=Path)
    args = parser.parse_args(argv)
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
            request, deadline, records = decode_action_request(content)
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
        if args.describe:
            response = encode_capability_response(
                request,
                deadline,
                expected_worker,
                http_source=source is not None,
                test_profile=test_worker.identity if test_worker else None,
                test_toolchains=test_worker.tools.identities if test_worker else (),
                test_standard_tools=test_worker.standard_tools_identity
                if test_worker
                else None,
                execute_profile=execute_worker.identity if execute_worker else None,
                accept_profile=accept_worker.identity if accept_worker else None,
                execute_toolchains=execute_worker.tools.identities
                if execute_worker
                else (),
                execute_standard_tools=execute_worker.standard_tools_identity
                if execute_worker
                else None,
                build_profile=build_worker.identity if build_worker else None,
                build_toolchains=build_worker.tools.identities if build_worker else (),
                build_standard_tools=build_worker.standard_tools_identity
                if build_worker
                else None,
            )
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
                http_source=source is not None,
            )
            sys.stdout.buffer.write(response)
            return 0
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
    except (StorageError, OSError, ValueError, RuntimeError):
        if describing:
            sys.stdout.buffer.write(
                encode_observation_failure("action_capability.custody_unavailable")
            )
            print("Action capability custody unavailable", file=sys.stderr)
            return 2
        # Return a bounded code, never private host paths or blob contents.
        response = encode_action_response(
            request, failure_code="action_source.custody_unavailable"
        )
    sys.stdout.buffer.write(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
