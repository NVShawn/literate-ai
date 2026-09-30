"""Pure prospective Git index bytes; never reads or writes a live repository.

Format authority: https://git-scm.com/docs/index-format . Root edits retain all
entry bytes except selected Gitlink OIDs and their invalidated stat caches.
"""

from __future__ import annotations

import hashlib
import struct

from literate_ai.contracts.repository_refresh import RepositoryRefreshRequest
from literate_ai.contracts.repository_tree import RepositoryTreeSnapshot

from .repository_orchestration import OrchestrationInventoryError

_MAX_BYTES = 16 * 1024 * 1024
_DERIVED = {b"TREE", b"EOIE", b"IEOT", b"FSMN", b"UNTR"}


def _fail():
    raise OrchestrationInventoryError(
        "refresh_index_invalid", "prospective index inputs are unsafe or unsupported"
    ) from None


def _digest(content, width):
    if width not in (40, 64):
        _fail()
    return (hashlib.sha1 if width == 40 else hashlib.sha256)(content).digest()


def tree_index_bytes(tree: RepositoryTreeSnapshot) -> bytes:
    """Build a stat-free v2 index from complete verified tree entries."""
    if not isinstance(tree, RepositoryTreeSnapshot):
        raise TypeError("index staging requires a verified tree snapshot")
    entries = sorted(
        (entry for entry in tree.entries if entry.mode != "040000"),
        key=lambda entry: entry.path.encode("utf-8"),
    )
    result = bytearray(struct.pack("!4sII", b"DIRC", 2, len(entries)))
    for entry in entries:
        name = entry.path.encode("utf-8")
        raw = (
            struct.pack("!10I", 0, 0, 0, 0, 0, 0, int(entry.mode, 8), 0, 0, 0)
            + bytes.fromhex(entry.object_id)
            + struct.pack("!H", min(len(name), 0xFFF))
            + name
            + b"\0"
        )
        result.extend(raw + b"\0" * (-len(raw) % 8))
    if len(result) + len(tree.commit) // 2 > _MAX_BYTES:
        _fail()
    return bytes(result) + _digest(result, len(tree.commit))


def refresh_root_index_bytes(
    content: bytes, request: RepositoryRefreshRequest, *, object_width: int
) -> bytes:
    """Change only named stage-zero Gitlinks; preserve unrelated entry state."""
    if not isinstance(content, bytes) or not isinstance(
        request, RepositoryRefreshRequest
    ):
        raise TypeError("root index staging requires bytes and a typed request")
    width = object_width // 2
    _digest(b"", object_width)
    try:
        if not 12 + width <= len(content) <= _MAX_BYTES:
            _fail()
        end = len(content) - width
        if _digest(content[:end], object_width) != content[end:]:
            _fail()
        magic, version, count = struct.unpack("!4sII", content[:12])
        if magic != b"DIRC" or version not in (2, 3, 4) or count > end // (42 + width):
            _fail()
        targets = {
            target.path.encode("utf-8"): target.commit for target in request.targets
        }
        if any(len(commit) != object_width for commit in targets.values()):
            _fail()
        found = set()
        changed = False
        result = bytearray(content[:12])
        cursor = 12
        previous_name = b""
        previous_key = None
        for _ in range(count):
            start = cursor
            if cursor + 42 + width > end:
                _fail()
            flags = int.from_bytes(content[cursor + 40 + width : cursor + 42 + width])
            mode = int.from_bytes(content[cursor + 24 : cursor + 28])
            cursor += 42 + width
            extended = 0
            if flags & 0x4000:
                if version == 2 or cursor + 2 > end:
                    _fail()
                extended = int.from_bytes(content[cursor : cursor + 2])
                cursor += 2
                if extended & ~0x6000:
                    _fail()
            removed = 0
            if version == 4:
                for position in range(10):
                    if cursor >= end:
                        _fail()
                    value = content[cursor]
                    cursor += 1
                    removed = ((removed + 1) << 7 if position else 0) + (value & 0x7F)
                    if not value & 0x80:
                        break
                else:
                    _fail()
                if removed > len(previous_name):
                    _fail()
            nul = content.find(b"\0", cursor, end)
            if nul < 0:
                _fail()
            name = content[cursor:nul]
            if version == 4:
                name = previous_name[: len(previous_name) - removed] + name
            stage = (flags >> 12) & 3
            key = (name, stage)
            if (
                not name
                or name.startswith(b"/")
                or any(part in (b"", b".", b"..", b".git") for part in name.split(b"/"))
                or flags & 0xFFF != min(len(name), 0xFFF)
                or (previous_key is not None and key <= previous_key)
                or mode not in (0o100644, 0o100755, 0o120000, 0o160000)
            ):
                _fail()
            cursor = nul + 1
            if version != 4:
                cursor = start + ((cursor - start + 7) // 8) * 8
                if cursor > end or any(content[nul:cursor]):
                    _fail()
            raw = bytearray(content[start:cursor])
            if name in targets:
                if stage or mode != 0o160000 or flags & 0x8000 or extended:
                    _fail()
                found.add(name)
                target_oid = bytes.fromhex(targets[name])
                if raw[40 : 40 + width] != target_oid:
                    changed = True
                    raw[:24] = b"\0" * 24
                    raw[28:40] = b"\0" * 12
                    raw[40 : 40 + width] = target_oid
            result.extend(raw)
            previous_name, previous_key = name, key
        if found != set(targets):
            _fail()
        seen = set()
        while cursor < end:
            if cursor + 8 > end:
                _fail()
            signature = content[cursor : cursor + 4]
            size = int.from_bytes(content[cursor + 4 : cursor + 8])
            limit = cursor + 8 + size
            if limit > end or signature in seen:
                _fail()
            seen.add(signature)
            if signature == b"REUC":
                result.extend(content[cursor:limit])
            elif signature not in _DERIVED:
                # Unknown optional data may carry recovery semantics. Refuse
                # instead of silently discarding it; split/sparse are mandatory.
                _fail()
            cursor = limit
        return bytes(result) + _digest(result, object_width) if changed else content
    except (ValueError, OverflowError, struct.error):
        _fail()
