"""Shared test fixtures extracted from test_worker_storage."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace

from literate_ai import worker_storage_probe as probe
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.worker_storage import (
    WorkerStorageBindings,
)
from literate_ai.contracts import (
    ExecutionWorker,
    ExecutionWorkerKind,
)
from tests.support.fixtures_test_worker_capacity import ROLES, observation, policy

FAMILY = {"darwin": "macos", "linux": "linux", "win32": "windows"}[sys.platform]


def bindings(root):
    return WorkerStorageBindings(
        ExecutionWorker("worker.fixture", ExecutionWorkerKind.LOCAL),
        FAMILY,
        tuple((role, str(root / role)) for role in ROLES),
    )


def selected_policy(bound):
    return replace(policy(), storage_bindings_identity=bound.identity)


def reply(environment, selected):
    raw = environment[probe.REQUEST_ENVIRONMENT].encode()
    return {
        "schema": probe.PROTOCOL,
        "request_identity": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "os_family": FAMILY,
        "samples": [item.to_dict() for item in observation(selected).samples],
    }


def success(value):
    return BoundedProcessResult(0, json.dumps(value).encode(), b"")
