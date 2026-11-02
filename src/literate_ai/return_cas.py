"""Bounded read-only CAS delivery selected by private receiver configuration.

The request selects an exact byte reference, never a path, executable, credential
or storage root. Returned bytes are assertions until the controller independently
verifies their package closure. This module runs no retained or SDK lifecycle.
"""

import argparse
import json
import signal
import sys
from datetime import datetime
from pathlib import Path

from literate_ai.adapters.action_dispatch_wire import ActionDispatchDeadline
from literate_ai.adapters.cache.filesystem import _read_regular_file
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.storage.cas import FileSystemCAS

READ_SCHEMA = "literate-ai/action-return-blob-read@1"
CONFIG_SCHEMA = "literate-ai/return-cas-reader@1"
MAX_READ_REQUEST_BYTES = 16384
MAX_RETURN_BLOB_BYTES = 16 * 1024**3


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate returned-CAS field")
        result[key] = value
    return result


def read_return_blob(
    content,
    *,
    cas_root,
    worker_identity,
    registration_identity,
    maximum_bytes,
    registration_guard,
    output,
):
    """Copy verified bytes with bounded memory and a non-extensible deadline."""
    if (
        not isinstance(content, bytes)
        or not 0 < len(content) <= MAX_READ_REQUEST_BYTES
        or not isinstance(worker_identity, ContentIdentity)
        or not isinstance(registration_identity, ContentIdentity)
        or type(maximum_bytes) is not int
        or not 1 <= maximum_bytes <= MAX_RETURN_BLOB_BYTES
        or not callable(registration_guard)
        or not callable(getattr(output, "write", None))
        or not callable(getattr(output, "flush", None))
    ):
        raise ValueError("returned CAS requires private bounded reader bindings")
    document = json.loads(content, object_pairs_hook=_pairs)
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schema",
            "worker_identity",
            "registration_identity",
            "reference",
            "deadline",
        }
        or document["schema"] != READ_SCHEMA
        or document["worker_identity"] != worker_identity.uri
        or document["registration_identity"] != registration_identity.uri
        or canonical_json_bytes(document) != content
    ):
        raise ValueError("returned CAS request binding differs")
    reference = BlobRef.from_dict(document["reference"])
    if reference.size > maximum_bytes or len(reference.media_type.encode()) > 512:
        raise ValueError("returned CAS reference exceeds private policy")
    raw_deadline = document["deadline"]
    if (
        not isinstance(raw_deadline, dict)
        or set(raw_deadline) != {"schema", "expires_at"}
        or raw_deadline["schema"] != "literate-ai/lifecycle-action-deadline@1"
    ):
        raise ValueError("returned CAS deadline differs")
    deadline = ActionDispatchDeadline(
        datetime.fromisoformat(raw_deadline["expires_at"])
    )
    if deadline.to_dict() != raw_deadline or not 0 < deadline.remaining() <= 86400:
        raise ValueError("returned CAS deadline is expired or unbounded")

    def current():
        deadline.remaining()
        registration_guard()
        deadline.remaining()

    class Sink:
        def write(self, block):
            current()
            remaining = memoryview(block)
            while remaining:
                current()
                count = output.write(remaining)
                if type(count) is not int or not 0 < count <= len(remaining):
                    raise OSError("returned CAS sink refused bytes")
                remaining = remaining[count:]
            return len(block)

    # The dedicated POSIX reader must also interrupt a blocked stdout pipe after
    # transport loss. Per-chunk checks alone cannot interrupt a blocked write.
    if not hasattr(signal, "setitimer"):
        raise ValueError("returned CAS reader requires a bounded POSIX writer")

    def expired(_number, _frame):
        raise TimeoutError("returned CAS deadline expired")

    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("returned CAS reader already has a deadline owner")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, deadline.remaining())
    try:
        current()
        store = FileSystemCAS(cas_root, create=False)
        store._read_verified(reference, collect=False, sink=Sink())
        current()
        output.flush()
        current()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _configuration(path):
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("returned CAS configuration must be direct and absolute")
    raw = _read_regular_file(
        path, maximum_bytes=65536, code="return_cas.configuration_invalid"
    )
    value = json.loads(raw, object_pairs_hook=_pairs)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "cas", "worker_identity", "maximum_bytes"}
        or value["schema"] != CONFIG_SCHEMA
        or not isinstance(value["cas"], str)
        or not Path(value["cas"]).is_absolute()
        or Path(value["cas"]).resolve(strict=True) != Path(value["cas"])
        or type(value["maximum_bytes"]) is not int
        or not 1 <= value["maximum_bytes"] <= MAX_RETURN_BLOB_BYTES
    ):
        raise ValueError("returned CAS configuration is invalid")
    ContentIdentity.parse_uri(value["worker_identity"])
    return value, canonical_identity(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--read-return-blob", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        configuration, identity = _configuration(args.config)

        def current():
            if _configuration(args.config)[1] != identity:
                raise ValueError("returned CAS registration changed")

        read_return_blob(
            sys.stdin.buffer.read(MAX_READ_REQUEST_BYTES + 1),
            cas_root=Path(configuration["cas"]),
            worker_identity=ContentIdentity.parse_uri(configuration["worker_identity"]),
            registration_identity=identity,
            maximum_bytes=configuration["maximum_bytes"],
            registration_guard=current,
            output=sys.stdout.buffer,
        )
        return 0
    except (ValueError, TypeError, OSError, RuntimeError):
        print("Returned CAS read refused", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
