"""Stdlib-only bounded cleanup investigation receiver for remote workers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

REQUEST_PROTOCOL = "literate-ai/worker-cleanup-request@1"
RESPONSE_PROTOCOL = "literate-ai/worker-cleanup-response@1"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 64 * 1024


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _candidate_id(root, relative):
    value = {
        "schema": "literate-ai/worker-cleanup-candidate@1",
        "root": root["alias"],
        "binding": hashlib.sha256(os.fsencode(root["path"])).hexdigest(),
        "relative": relative,
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _linked(path):
    current = path
    while True:
        metadata = current.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & 0x0400
        ):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _scan(root, candidate, deadline, remaining, maximum_depth):
    total = entries = skipped = 0
    active = inactive = False
    stack = [(candidate, 0)]
    while stack:
        if time.monotonic() >= deadline or entries >= remaining:
            return total, entries, skipped, active, inactive, False, False
        path, depth = stack.pop()
        try:
            metadata = path.lstat()
        except OSError:
            return total, entries, skipped, active, inactive, False, False
        entries += 1
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & 0x0400
        ):
            skipped += 1
            if path == candidate:
                return total, entries, skipped, active, inactive, True, True
            continue
        if path.name in root["active_markers"]:
            active = True
        if path.name in root["inactive_markers"]:
            inactive = True
        if stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        if depth >= maximum_depth:
            return total, entries, skipped, active, inactive, False, False
        try:
            children = tuple(path.iterdir())
        except OSError:
            return total, entries, skipped, active, inactive, False, False
        stack.extend((child, depth + 1) for child in reversed(children))
    return total, entries, skipped, active, inactive, True, False


def investigate(request):
    deadline = time.monotonic() + request["deadline_ms"] / 1000
    scanned = skipped = 0
    complete = True
    candidates = []
    for root in request["roots"]:
        if time.monotonic() >= deadline or scanned >= request["maximum_entries"]:
            complete = False
            break
        root_path = Path(root["path"])
        try:
            metadata = root_path.lstat()
            if (
                _linked(root_path)
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & 0x0400
            ):
                complete = False
                continue
            children = tuple(root_path.iterdir())
        except OSError:
            complete = False
            continue
        for child in children:
            if time.monotonic() >= deadline or scanned >= request["maximum_entries"]:
                complete = False
                break
            result = _scan(
                root,
                child,
                deadline,
                request["maximum_entries"] - scanned,
                request["maximum_depth"],
            )
            (
                measured,
                entries,
                links,
                active,
                inactive,
                finished,
                candidate_link,
            ) = result
            scanned += entries
            skipped += links
            complete = complete and finished
            if candidate_link or measured < request["minimum_candidate_bytes"]:
                continue
            relative = child.relative_to(root_path).as_posix()
            candidates.append(
                {
                    "candidate_id": _candidate_id(root, relative),
                    "root": root["alias"],
                    "kind": "directory" if child.is_dir() else "file",
                    "bytes": measured,
                    "entries": entries,
                    "ownership": root["ownership"],
                    "active_use": (
                        "active" if active else "inactive" if inactive else "uncertain"
                    ),
                    "uncertainty": (
                        "configured active-use marker present"
                        if active
                        else "configured completed-use marker present"
                        if inactive
                        else "no process ownership proof was collected"
                    ),
                    "recovery": root["recovery"],
                }
            )
    candidates.sort(
        key=lambda item: (-item["bytes"], item["root"], item["candidate_id"])
    )
    return {
        "schema": "literate-ai/worker-cleanup-investigation@1",
        "status": "complete" if complete else "bounded-partial",
        "scanned_entries": scanned,
        "skipped_links": skipped,
        "candidates": candidates,
        "deletion_authorized": False,
    }


def _valid_request(request):
    if not isinstance(request, dict) or set(request) != {
        "schema",
        "os_family",
        "roots",
        "deadline_ms",
        "maximum_entries",
        "maximum_depth",
        "minimum_candidate_bytes",
        "nonce",
    }:
        return False
    family = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform)
    if (
        request["schema"] != REQUEST_PROTOCOL
        or request["os_family"] != family
        or not isinstance(request["roots"], list)
        or not 1 <= len(request["roots"]) <= 16
        or isinstance(request["deadline_ms"], bool)
        or not isinstance(request["deadline_ms"], int)
        or not 1 <= request["deadline_ms"] <= 60000
        or isinstance(request["maximum_entries"], bool)
        or not isinstance(request["maximum_entries"], int)
        or not 1 <= request["maximum_entries"] <= 100000
        or isinstance(request["maximum_depth"], bool)
        or not isinstance(request["maximum_depth"], int)
        or not 0 <= request["maximum_depth"] <= 16
        or isinstance(request["minimum_candidate_bytes"], bool)
        or not isinstance(request["minimum_candidate_bytes"], int)
        or not 0 <= request["minimum_candidate_bytes"] <= 2**63 - 1
        or not isinstance(request["nonce"], str)
        or re.fullmatch(r"[0-9a-f]{32}", request["nonce"]) is None
    ):
        return False
    pure_path = PureWindowsPath if family == "windows" else PurePosixPath
    aliases = []
    for root in request["roots"]:
        if not isinstance(root, dict) or set(root) != {
            "alias",
            "path",
            "ownership",
            "recovery",
            "active_markers",
            "inactive_markers",
        }:
            return False
        if (
            not isinstance(root["alias"], str)
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?", root["alias"])
            is None
            or not isinstance(root["path"], str)
            or "\x00" in root["path"]
            or not pure_path(root["path"]).is_absolute()
            or root["ownership"] != "task-owned"
            or not isinstance(root["recovery"], str)
            or not 1 <= len(root["recovery"]) <= 256
        ):
            return False
        for key in ("active_markers", "inactive_markers"):
            markers = root[key]
            if (
                not isinstance(markers, list)
                or len(markers) > 32
                or markers != sorted(set(markers))
                or any(
                    not isinstance(marker, str)
                    or not 1 <= len(marker) <= 128
                    or "/" in marker
                    or "\\" in marker
                    for marker in markers
                )
            ):
                return False
        if set(root["active_markers"]) & set(root["inactive_markers"]):
            return False
        aliases.append(root["alias"])
    return aliases == sorted(set(aliases))


def receive():
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        return 2
    try:
        request = json.loads(raw, object_pairs_hook=_unique_object)
        if not _valid_request(request):
            return 2
        response = {
            "schema": RESPONSE_PROTOCOL,
            "request_identity": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "os_family": request["os_family"],
            "investigation": investigate(request),
        }
        encoded = json.dumps(response, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > MAX_RESPONSE_BYTES:
            return 2
        sys.stdout.buffer.write(encoded)
        return 0
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(receive())
