"""Current file custody for materialized parts of a native dependency observation.

Unmaterialized/system-image records remain explicit: this capture alone is not
complete host authority, a dependency observation, or consumer admission.
"""

import hashlib
import json
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from literate_ai.adapters.dependencies.observation import _symlink_chain_snapshot
from literate_ai.adapters.dependencies.types import HostDependencyObservation
from literate_ai.adapters.retained_native_file_custody import (
    NativeFileClosure,
    NativeFileDigest,
    native_file_digest,
)
from literate_ai.adapters.retained_package_tree import _signature
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes

_FILE_PATHS = {
    "literate-ai:launcher-path",
    "literate-ai:macho-resolved-path",
    "literate-ai:elf-path",
    "literate-ai:pe-path",
    "literate-ai:pe-api-set-schema-path",
}


def _path(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("retained.native.path-invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("retained.native.path-invalid")
    return path


def _node(path):
    node = path.lstat()
    if stat.S_ISDIR(node.st_mode):
        return node.st_dev, node.st_ino, node.st_mode
    return _signature(node)


@dataclass(frozen=True)
class RetainedNativeFiles:
    observation_content: bytes
    closure: NativeFileClosure
    nodes: tuple
    loaders: tuple
    unmaterialized_components: tuple[str, ...]

    @property
    def observation_identity(self):
        return "sha256:" + hashlib.sha256(self.observation_content).hexdigest()

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/retained-native-files@1",
                "observation": self.observation_identity,
                "files": self.closure.identity,
                "unmaterialized_components": list(self.unmaterialized_components),
            }
        )

    def require_unchanged(self):
        self.closure.require_unchanged()
        if any(_node(path) != node for path, node in self.nodes) or any(
            path.resolve(strict=True) != resolved
            or _symlink_chain_snapshot(path) != links
            for path, resolved, links in self.loaders
        ):
            raise ValueError("retained.native.custody-changed")
        self.closure.require_unchanged()


def capture_retained_native_files(
    observation,
    *,
    maximum_entries=10000,
    maximum_file_bytes=512 * 1024 * 1024,
    maximum_total_bytes=2 * 1024 * 1024 * 1024,
):
    if (
        not isinstance(observation, HostDependencyObservation)
        or any(
            type(n) is not int or n < 1
            for n in (
                maximum_entries,
                maximum_file_bytes,
                maximum_total_bytes,
            )
        )
        or maximum_file_bytes > maximum_total_bytes
        or len(observation.components) > maximum_entries
    ):
        raise ValueError("retained.native.configuration-invalid")
    content = canonical_json_bytes(
        {
            "components": list(observation.components),
            "edges": [list(e) for e in observation.edges],
        }
    )
    if len(content) > 16 * 1024 * 1024:
        raise ValueError("retained.native.observation-limit")
    files = {}
    total_bytes = 0
    nodes, loaders, missing, refs = {}, [], [], set()

    def capture_nodes(path):
        for item in (path, *path.parents):
            current = _node(item)
            if item in nodes and nodes[item] != current:
                raise ValueError("retained.native.custody-changed")
            nodes[item] = current
            if len(nodes) > maximum_entries:
                raise ValueError("retained.native.entry-limit")

    for component in json.loads(content)["components"]:
        if not isinstance(component, dict):
            raise ValueError("retained.native.component-invalid")
        ref = component.get("bom-ref")
        if (
            not isinstance(ref, str)
            or not ref
            or len(ref.encode("utf-8")) > 1024
            or ref in refs
        ):
            raise ValueError("retained.native.component-invalid")
        refs.add(ref)
        properties = {}
        raw_properties = component.get("properties", [])
        if not isinstance(raw_properties, list):
            raise ValueError("retained.native.properties-invalid")
        for entry in raw_properties:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not isinstance(entry.get("value"), str)
            ):
                raise ValueError("retained.native.properties-invalid")
            properties.setdefault(entry["name"], []).append(entry["value"])
        paths = [value for key in _FILE_PATHS for value in properties.get(key, ())]
        if not paths:
            missing.append(ref)
            continue
        if len(paths) != 1:
            raise ValueError("retained.native.file-path-ambiguous")
        path = _path(paths[0])
        hashes = component.get("hashes", [])
        if not isinstance(hashes, list):
            raise ValueError("retained.native.file-hash-required")
        digests = [
            entry.get("content")
            for entry in hashes
            if isinstance(entry, dict) and entry.get("alg") == "SHA-256"
        ]
        if len(digests) != 1 or not isinstance(digests[0], str):
            raise ValueError("retained.native.file-hash-required")
        capture_nodes(path)
        prior = files.get(path)
        remaining = maximum_total_bytes - total_bytes
        digest, size = native_file_digest(
            path, min(maximum_file_bytes, remaining) if prior is None else prior.size
        )
        if digest != digests[0]:
            raise ValueError("retained.native.observed-file-changed")
        if prior is None:
            files[path] = NativeFileDigest(path, (ref,), digest, size)
            total_bytes += size
        else:
            if (digest, size) != (prior.digest, prior.size):
                raise ValueError("retained.native.custody-changed")
            files[path] = replace(prior, labels=tuple(sorted((*prior.labels, ref))))
        if "literate-ai:macho-resolved-path" in properties:
            values = properties.get("literate-ai:macho-path", [])
            if len(values) != 1:
                raise ValueError("retained.native.loader-path-required")
            loader = _path(values[0])
            links = _symlink_chain_snapshot(loader)
            expected = []
            for value in properties.get("literate-ai:macho-symlink", []):
                entry = json.loads(value)
                if (
                    not isinstance(entry, dict)
                    or set(entry) != {"path", "target"}
                    or not isinstance(entry["target"], str)
                ):
                    raise ValueError("retained.native.loader-link-invalid")
                expected.append((str(_path(entry["path"])), entry["target"]))
            if loader.resolve(strict=True) != path or sorted(expected) != [
                (p, target) for p, target, _ in links
            ]:
                raise ValueError("retained.native.observed-loader-changed")
            capture_nodes(loader)
            for link, _target, _identity in links:
                capture_nodes(Path(link))
            loaders.append((loader, path, links))
    result = RetainedNativeFiles(
        content,
        NativeFileClosure(tuple(files.values())),
        tuple(nodes.items()),
        tuple(loaders),
        tuple(sorted(missing)),
    )
    result.require_unchanged()
    return result
