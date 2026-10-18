"""Native FreeBSD ELF closure inspection without running target images."""

from __future__ import annotations

import os
import re
import sys
from collections import deque
from pathlib import Path

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters.freebsd_loader_paths import FreeBsdLoaderPaths
from literate_ai.adapters.user_paths import resolve_freebsd_loader_system_paths
from literate_ai.contracts import canonical_identity, canonical_json_bytes

from . import observation as elf
from .freebsd_loader import FreeBsdLoaderState, _fail


class FreeBsdElfDependencyObserver:
    def __init__(
        self,
        *,
        toolchain_commands=(),
        lifecycle_commands=(),
        library_roots=(),
        loader_paths=None,
        artifact_files=None,
    ):
        self.toolchain_commands = tuple(tuple(c) for c in toolchain_commands)
        self.lifecycle_commands = tuple(lifecycle_commands)
        self.library_roots = elf._explicit_library_roots(library_roots)
        if loader_paths is not None and not isinstance(
            loader_paths, FreeBsdLoaderPaths
        ):
            raise ValueError("FreeBSD loader paths must be a typed projection")
        self.loader_paths = loader_paths
        self.artifact_files = elf._explicit_artifact_files(artifact_files)

    def observe(self, build, *, root_ref):
        try:
            return self._observe(build, root_ref=root_ref)
        except (OSError, UnsafeFilesystemPathError, ValueError) as exc:
            _fail(
                "observation-unavailable",
                f"Native observation failed: {type(exc).__name__}",
            )

    def _observe(self, build, *, root_ref):
        if not sys.platform.startswith("freebsd"):
            _fail("host-required", "FreeBSD dependency observation requires FreeBSD")
        paths = resolve_freebsd_loader_system_paths()
        readelf = elf._resolved_bound_tool("readelf")
        ldconfig = Path(paths.ldconfig).resolve(strict=True)
        tool_hashes = {path: elf._file_digest(path) for path in (readelf, ldconfig)}
        state = FreeBsdLoaderState(paths)
        state.bind_hints(
            elf._run_bounded_tool(
                ldconfig, ("-r",), code="dependencies.freebsd-ldconfig-failed"
            )
        )
        try:
            selected = self.loader_paths or FreeBsdLoaderPaths.from_environment(
                os.environ
            )
        except ValueError as exc:
            _fail("environment-unsupported", str(exc))
        if self.library_roots:
            # Provider roots are the explicit LD_LIBRARY_PATH prefix supplied by
            # Standard. An explicit projection must already include those roots.
            roots = tuple(str(p) for p in self.library_roots)
            if self.loader_paths is not None and not set(roots) <= set(
                selected.library
            ):
                _fail("loader-paths-mismatch", "Provider roots lack loader authority")
            selected = FreeBsdLoaderPaths(
                tuple(dict.fromkeys((*roots, *selected.library)))
            )
        artifact_root = elf._artifact_root(build)
        seeds = {}
        direct = set()
        components = []
        edges = []
        owners = {}
        runtime_edges = []

        def add(path, scopes):
            name = str(path)
            seeds.setdefault(name, set()).update(scopes)
            direct.add(name)

        for path in elf._native_artifact_files(
            artifact_root, self.artifact_files, elf._is_elf
        ):
            add(path.resolve(strict=True), {"runtime", "system"})
        for command in self.toolchain_commands:
            path = elf._resolve_command(command)
            if elf._is_elf(path):
                add(path, {"build", "toolchain"})
                continue
            observed = elf._npm_toolchain_launcher_dependencies(
                path, command[0], root_ref=root_ref, native_predicate=elf._is_elf
            )
            if observed is None:
                _fail(
                    "toolchain-not-elf", "Toolchain command lacks a native realization"
                )
            observation, native_owners, launcher_edges = observed
            components.extend(observation.components)
            edges.extend(observation.edges)
            owners.update(native_owners)
            runtime_edges.extend(launcher_edges)
            for name in (*native_owners, *(e[1] for e in launcher_edges)):
                seeds.setdefault(name, set()).update({"build", "toolchain"})
        for command in self.lifecycle_commands:
            path = elf._resolve_command((command,))
            if elf._is_elf(path):
                add(path, {"build", "toolchain"})
                continue
            observation, native_owners, launcher_edges = elf._npm_launcher_dependencies(
                path,
                command,
                root_ref=root_ref,
                prefix="lifecycle-launcher",
                native_predicate=elf._is_elf,
            )
            components.extend(observation.components)
            edges.extend(observation.edges)
            owners.update(native_owners)
            runtime_edges.extend(launcher_edges)
            for name in (*native_owners, *(e[1] for e in launcher_edges)):
                seeds.setdefault(name, set()).update({"build", "toolchain"})
        add(readelf, {"build", "toolchain"})
        add(ldconfig, {"build", "toolchain"})
        images, image_edges, hashes, candidates = self._closure(
            readelf, seeds, state, selected
        )
        refs = {path: elf._elf_ref(image) for path, image in images.items()}
        for path, image in images.items():
            component = elf._elf_component(
                image, refs[path], scopes=tuple(sorted(seeds.get(path, {"system"})))
            )
            component["properties"].append(
                {"name": "literate-ai:native-platform", "value": "freebsd"}
            )
            components.append(component)
        edges.extend(
            elf._native_dependency_edges(
                root_ref,
                refs=refs,
                direct_seed_paths=direct,
                image_edges=image_edges,
                native_owners=owners,
                launcher_runtime_edges=runtime_edges,
            )
        )
        projection = {"loader": state.projection(), "paths": selected.to_environment()}
        identity = canonical_identity(projection)
        ref = "urn:literate-ai:freebsd-loader-state:" + identity.digest
        components.append(
            {
                "type": "data",
                "bom-ref": ref,
                "name": "freebsd-loader-configuration",
                "version": identity.digest,
                "hashes": [{"alg": "SHA-256", "content": identity.digest}],
                "properties": [
                    {"name": "literate-ai:dependency-kind", "value": "system"},
                    {"name": "literate-ai:dependency-scope", "value": "system"},
                    {"name": "literate-ai:dependency-scope", "value": "runtime"},
                    {
                        "name": "literate-ai:freebsd-loader-state",
                        "value": canonical_json_bytes(projection).decode("utf-8"),
                    },
                ],
            }
        )
        edges.append((root_ref, ref))
        state.require_unchanged()
        for path, expected in hashes.items():
            if elf._file_digest(Path(path)) != expected:
                _fail("image-changed", "Native ELF image changed during observation")
        for path, expected in candidates.items():
            if self._candidate_fact(path) != expected:
                _fail("resolution-changed", "Native library search candidates changed")
        for path, digest in tool_hashes.items():
            if elf._file_digest(path) != digest:
                _fail("inspector-changed", "FreeBSD dependency inspector changed")
        return elf._normalized_observation(components, edges)

    @staticmethod
    def _expand(value, image_path):
        value = re.sub(
            r"\$(?:\{ORIGIN\}|ORIGIN(?![A-Za-z0-9_]))",
            lambda _match: str(Path(image_path).parent),
            value,
        )
        if "$" in value:
            _fail("token-unsupported", "ELF import uses an unqualified dynamic token")
        if not value.startswith("/") or value.startswith("//"):
            _fail("path-unsafe", "ELF loader path is relative or ambiguous")
        return value

    @staticmethod
    def _candidate_fact(path):
        path = Path(path)
        try:
            node = path.lstat()
            resolved = path.resolve(strict=True)
            target = resolved.stat()
            return (
                str(resolved),
                node.st_mode,
                node.st_ino,
                node.st_mtime_ns,
                target.st_dev,
                target.st_ino,
                target.st_size,
                target.st_mtime_ns,
                target.st_ctime_ns,
            )
        except FileNotFoundError:
            return None

    def _closure(self, readelf, seeds, state, loader_paths):
        digest = elf._file_digest(readelf)
        images = {}
        hashes = {}
        dynamic = {}
        edges = set()
        processed = set()
        witnesses = {}
        sonames = {}
        pending = deque((path, path) for path in sorted(seeds))

        def image(path):
            if path not in images:
                if len(images) >= elf._MAX_OBSERVED_FILES:
                    _fail("image-limit", "Native ELF closure exceeds its image bound")
                hashes[path] = elf._file_digest(Path(path))
                header = elf._run_bounded_tool(
                    readelf, ("-h", path), code="dependencies.freebsd-readelf-header"
                )
                if not re.search(r"OS/ABI:\s+.*FreeBSD", header, re.IGNORECASE):
                    _fail("abi-unsupported", "ELF image lacks a native FreeBSD ABI")
                dynamic[path] = elf._run_bounded_tool(
                    readelf, ("-d", path), code="dependencies.freebsd-readelf-dynamic"
                )
                if re.search(
                    r"\((?:FILTER|AUXILIARY)\)|NODEFLIB|NODEFAULTLIB", dynamic[path]
                ):
                    _fail(
                        "dynamic-unsupported", "ELF image uses unqualified loader flags"
                    )
                images[path] = elf._inspect_elf(
                    readelf, Path(path), readelf_digest=digest
                )
            return images[path]

        while pending:
            path, program = pending.popleft()
            if (path, program) in processed:
                continue
            if len(processed) >= elf._MAX_OBSERVED_FILES:
                _fail("context-limit", "Native loader contexts exceed their bound")
            processed.add((path, program))
            current = image(path)
            main = image(program)
            own_paths = tuple(self._expand(p, path) for p in current.search_paths)
            program_paths = (
                tuple(self._expand(p, program) for p in main.search_paths)
                if not main.runpath_present
                else ()
            )
            before = () if current.runpath_present else (*own_paths, *program_paths)
            after = own_paths if current.runpath_present else ()
            directories = tuple(
                dict.fromkeys(
                    (
                        *before,
                        *loader_paths.library,
                        *after,
                        *state.hints,
                        *(str(p) for p in state.paths.defaults),
                    )
                )
            )
            if current.interpreter is not None:
                if current.interpreter != str(state.paths.interpreter):
                    _fail(
                        "interpreter-unsupported", "ELF uses another ABI's interpreter"
                    )
                fact = self._candidate_fact(current.interpreter)
                if witnesses.get(current.interpreter, fact) != fact:
                    _fail("resolution-changed", "Native interpreter selection changed")
                witnesses[current.interpreter] = fact
                interpreter = elf._resolve_exact_elf_candidate(
                    (current.interpreter,),
                    current.architecture,
                    readelf,
                    readelf_digest=digest,
                )
                edges.add((path, interpreter))
                pending.append((interpreter, interpreter))
            for name in current.needed:
                name = state.maps.get(name, name)
                if any(ord(c) < 32 for c in name):
                    _fail("import-unsupported", "ELF import uses an unqualified token")
                if "$" in name:
                    name = self._expand(name, path)
                if name in state.maps and state.maps[name] != name:
                    _fail("libmap-chain", "Chained library mappings are not qualified")
                candidates = (
                    (self._expand(name, path),)
                    if "/" in name
                    else tuple(str(Path(d) / name) for d in directories)
                )
                for candidate in candidates:
                    if (
                        candidate not in witnesses
                        and len(witnesses) >= elf._MAX_OBSERVED_FILES
                    ):
                        _fail(
                            "candidate-limit",
                            "Library search exceeds its candidate bound",
                        )
                    fact = self._candidate_fact(candidate)
                    if candidate in witnesses and witnesses[candidate] != fact:
                        _fail("resolution-changed", "Native library search changed")
                    witnesses[candidate] = fact
                target = elf._resolve_exact_elf_candidate(
                    candidates, current.architecture, readelf, readelf_digest=digest
                )
                key = (program, name)
                if key in sonames and sonames[key] != target:
                    _fail(
                        "soname-ambiguous",
                        "One process would select conflicting libraries",
                    )
                sonames[key] = target
                edges.add((path, target))
                pending.append((target, program))
        for path, expected in hashes.items():
            if elf._file_digest(Path(path)) != expected:
                _fail("image-changed", "Native ELF image changed during inspection")
        return images, tuple(sorted(edges)), hashes, witnesses
