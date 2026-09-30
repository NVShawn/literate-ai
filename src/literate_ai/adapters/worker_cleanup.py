"""Bounded local and remote read-only cleanup investigation."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import zlib
from pathlib import Path

from literate_ai import worker_cleanup_probe
from literate_ai.adapters.builders.python import BuildError
from literate_ai.adapters.ssh_transport import ssh_arguments
from literate_ai.adapters.worker_storage import (
    _command_environment,
    _run_probe,
    _unique_object,
)
from literate_ai.application.worker_cleanup import (
    CleanupCandidate,
    CleanupInvestigation,
    CleanupScanPolicy,
    investigate_cleanup_candidates,
)
from literate_ai.contracts import ExecutionWorkerKind, canonical_json_bytes


def _probe_command(executable):
    source = Path(worker_cleanup_probe.__file__).read_bytes()
    if len(source) > 64 * 1024:
        raise ValueError("worker cleanup probe source exceeds its staging bound")
    encoded = base64.b64encode(zlib.compress(source)).decode("ascii")
    script = (
        "import base64,zlib;s={'__name__':'worker_cleanup_receiver'};exec("
        "compile(zlib.decompress(base64.b64decode("
        + repr(encoded)
        + ")), '<worker-cleanup-probe>', 'exec'),s);"
        "raise SystemExit(s['receive']())"
    )
    return (executable, "-I", "-S", "-B", "-c", script)


def _failed(status):
    return CleanupInvestigation(status, 0, 0, ())


def _parse_investigation(value, policy):
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "status",
        "scanned_entries",
        "skipped_links",
        "candidates",
        "deletion_authorized",
    }:
        raise ValueError("worker.cleanup_response_invalid")
    if (
        value["schema"] != "literate-ai/worker-cleanup-investigation@1"
        or value["status"] not in {"complete", "bounded-partial"}
        or value["deletion_authorized"] is not False
        or isinstance(value["scanned_entries"], bool)
        or not isinstance(value["scanned_entries"], int)
        or not 0 <= value["scanned_entries"] <= policy.maximum_entries
        or isinstance(value["skipped_links"], bool)
        or not isinstance(value["skipped_links"], int)
        or not 0 <= value["skipped_links"] <= value["scanned_entries"]
        or not isinstance(value["candidates"], list)
        or len(value["candidates"]) > policy.maximum_entries
    ):
        raise ValueError("worker.cleanup_response_invalid")
    roots = {root.alias: root for root in policy.roots}
    candidates = []
    for item in value["candidates"]:
        if not isinstance(item, dict) or set(item) != {
            "candidate_id",
            "root",
            "kind",
            "bytes",
            "entries",
            "ownership",
            "active_use",
            "uncertainty",
            "recovery",
        }:
            raise ValueError("worker.cleanup_response_invalid")
        root = roots.get(item["root"])
        if (
            root is None
            or not isinstance(item["candidate_id"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", item["candidate_id"]) is None
            or item["kind"] not in {"file", "directory"}
            or isinstance(item["bytes"], bool)
            or not isinstance(item["bytes"], int)
            or item["bytes"] < policy.minimum_candidate_bytes
            or isinstance(item["entries"], bool)
            or not isinstance(item["entries"], int)
            or not 1 <= item["entries"] <= policy.maximum_entries
            or item["ownership"] != root.ownership
            or item["active_use"] not in {"active", "inactive", "uncertain"}
            or not isinstance(item["uncertainty"], str)
            or not 1 <= len(item["uncertainty"]) <= 256
            or item["recovery"] != root.recovery
        ):
            raise ValueError("worker.cleanup_response_invalid")
        candidates.append(CleanupCandidate(**item))
    expected = sorted(
        candidates, key=lambda item: (-item.bytes, item.root, item.candidate_id)
    )
    if candidates != expected or len({item.candidate_id for item in candidates}) != len(
        candidates
    ):
        raise ValueError("worker.cleanup_response_invalid")
    return CleanupInvestigation(
        value["status"],
        value["scanned_entries"],
        value["skipped_links"],
        tuple(candidates),
    )


def investigate_worker_cleanup(
    bindings, policy: CleanupScanPolicy, *, runner=_run_probe
):
    """Investigate on the selected worker without exposing paths or deleting data."""

    if bindings.worker.kind is ExecutionWorkerKind.LOCAL:
        return investigate_cleanup_candidates(policy)
    request = {
        "schema": worker_cleanup_probe.REQUEST_PROTOCOL,
        "os_family": bindings.os_family,
        "roots": [
            {
                "alias": root.alias,
                "path": os.fspath(root.path),
                "ownership": root.ownership,
                "recovery": root.recovery,
                "active_markers": list(root.active_markers),
                "inactive_markers": list(root.inactive_markers),
            }
            for root in policy.roots
        ],
        "deadline_ms": policy.deadline_ms,
        "maximum_entries": policy.maximum_entries,
        "maximum_depth": policy.maximum_depth,
        "minimum_candidate_bytes": policy.minimum_candidate_bytes,
        "nonce": secrets.token_hex(16),
    }
    payload = canonical_json_bytes(request)
    expected_identity = "sha256:" + hashlib.sha256(payload).hexdigest()
    environment = {
        key: os.environ[key]
        for key in ("SystemRoot", "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
        if key in os.environ
    }
    timeout = min(65.0, policy.deadline_ms / 1000 + 5)
    if bindings.worker.kind is ExecutionWorkerKind.SSH:
        windows = bindings.os_family == "windows"
        receiver = _probe_command(
            bindings.python_executable or ("python" if windows else "python3")
        )
        remote_command = (
            subprocess.list2cmdline(receiver) if windows else shlex.join(receiver)
        )
        command = ssh_arguments(
            bindings.worker.endpoint,
            remote_command,
            timeout,
            transport=bindings.worker.transport,
            login_shell=not windows,
        )
        if "SSH_AUTH_SOCK" in os.environ:
            environment["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    elif bindings.worker.kind is ExecutionWorkerKind.COMMAND:
        if bindings.health_command is None:
            return _failed("unsupported")
        command = (*bindings.health_command.command, "--cleanup-investigate")
        try:
            environment.update(_command_environment(bindings.health_command))
        except PermissionError:
            return _failed("denied")
        except ValueError:
            return _failed("malformed")
    else:
        raise AssertionError("remote cleanup requires SSH or command worker")
    try:
        completed = runner(
            command,
            environment=environment,
            timeout_seconds=timeout,
            input_bytes=payload,
        )
        if completed.returncode:
            return _failed("unreachable")
        response = json.loads(completed.stdout, object_pairs_hook=_unique_object)
        if (
            not isinstance(response, dict)
            or set(response)
            != {"schema", "request_identity", "os_family", "investigation"}
            or response["schema"] != worker_cleanup_probe.RESPONSE_PROTOCOL
            or response["request_identity"] != expected_identity
            or response["os_family"] != bindings.os_family
        ):
            return _failed("malformed")
        return _parse_investigation(response["investigation"], policy)
    except BuildError:
        return _failed("unreachable")
    except (OSError, TypeError, ValueError):
        return _failed("malformed")


__all__ = ["investigate_worker_cleanup"]
