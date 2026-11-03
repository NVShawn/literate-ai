"""Bounded FreeBSD observation relocation joined to verified manifest members.

Historical workspace anchors come from the existing SDK launch inspector. This
projector creates assertions only and never opens a historical worker path.
Unknown workspace paths and component forms refuse normalization.
"""

import json
from pathlib import PurePosixPath

from literate_ai.adapters.native_sdk_freebsd import project_freebsd_loader_context
from literate_ai.contracts import canonical_identity, canonical_json_bytes

_ELF_PATH = "literate-ai:elf-path"
_STATE = "literate-ai:freebsd-loader-state"
_FILTEE = "literate-ai:freebsd-filtee"


def retained_unit_runtime_context(
    run, inputs, roots, policy, *, working_memory_bytes=None
):
    """Keep full image/loader graphs, replacing only proven workspace coordinates."""
    observations = (
        run.resolved_bom.tool_observation,
        run.resolved_bom.export_observation,
    )
    size = sum(map(len, observations))
    limit = (
        policy.max_provider_memory_bytes
        if working_memory_bytes is None
        else working_memory_bytes
    )
    if type(limit) is not int or not 1 <= limit <= policy.max_provider_memory_bytes:
        raise ValueError("retained.unit.runtime-comparison-memory-exceeded")
    memory = 32 * size
    steps = policy.max_closure_steps

    def reserve(amount):
        nonlocal memory, steps
        steps -= 1
        if amount < 0 or steps < 0 or memory + amount > limit:
            raise ValueError("retained.unit.runtime-comparison-budget-exceeded")
        memory += amount

    reserve(0)
    if not roots:
        roots = ordinary_retained_workspace_roots(
            run, inputs, policy, maximum_memory_bytes=limit - memory
        )
    if any(platform != "freebsd" for platform, _root, _placements in roots):
        raise ValueError("retained.unit.runtime-relocation-platform-unsupported")
    anchors = tuple(sorted({root for _platform, root, _placements in roots}))
    for root in anchors:
        if (
            not root.startswith("/")
            or str(PurePosixPath(root)) != root
            or ".." in PurePosixPath(root).parts
        ):
            raise ValueError("retained.unit.runtime-launch-anchor-invalid")
    # Index only the already bounded admitted manifests and portable SDK image
    # assertions. SDK bindings are read from the exact current captured policy.
    manifests = {
        "snapshot/tree": inputs.source_manifest,
        "outputs": run.output_manifest,
    }
    members, directories = {}, set()

    def add_member(path, digest):
        previous = members.get(path)
        if previous is not None:
            if previous != digest:
                raise ValueError("retained.unit.runtime-sdk-placement-collision")
            return
        reserve(1024 + 8 * len(path.encode("utf-8")))
        members[path] = digest

    def add_directory(path):
        if path not in directories:
            reserve(1024 + 8 * len(path.encode("utf-8")))
            directories.add(path)

    for prefix, manifest in manifests.items():
        add_directory(prefix)
        for item in manifest.members:
            if item.symlink_target is None:
                add_member(prefix + "/" + item.path, item.sha256)
        for path in manifest.directories:
            add_directory(prefix + "/" + path)
    if run.resolved_bom.sdk_dependencies:
        placements = {}
        for _platform, _root, rows in roots:
            for prefix, identity in rows:
                previous = placements.get(prefix)
                if previous is not None and previous != identity:
                    raise ValueError("retained.unit.runtime-sdk-placement-ambiguous")
                if previous is None:
                    reserve(1024 + 8 * len(prefix.encode("utf-8")))
                    placements[prefix] = identity
        reserve(1024 * len(run.resolved_bom.sdk_dependencies))
        by_input = {
            item.input_identity.uri: item for item in run.resolved_bom.sdk_dependencies
        }
        if set(placements.values()) != set(by_input):
            raise ValueError("retained.unit.runtime-sdk-placement-closure-differs")
        for prefix, input_identity in placements.items():
            dependency = by_input[input_identity]
            # Reserve before decoding this bounded immutable SDK assertion.
            reserve(32 * len(dependency._components_json))
            for component in dependency.observation.components:
                reserve(0)
                paths = [
                    p["value"]
                    for p in component.get("properties", [])
                    if p["name"] == "literate-ai:native-sdk-relative-path"
                ]
                if not paths:
                    continue
                if len(paths) != 1:
                    raise ValueError("retained.unit.runtime-sdk-image-ambiguous")
                hashes = [
                    h["content"]
                    for h in component.get("hashes", [])
                    if h["alg"] == "SHA-256"
                ]
                if len(hashes) != 1:
                    raise ValueError("retained.unit.runtime-sdk-image-hash-required")
                path = "snapshot/tree/" + prefix + "/" + paths[0]
                add_member(path, hashes[0])
                parent = PurePosixPath(path).parent
                while str(parent) != ".":
                    reserve(0)
                    add_directory(str(parent))
                    parent = parent.parent

    def portable(path, *, digest=None, directory=False):
        if not isinstance(path, str):
            raise ValueError("retained.unit.runtime-path-invalid")
        selected = [
            root for root in anchors if path == root or path.startswith(root + "/")
        ]
        if not selected:
            return path
        if len(selected) != 1:
            raise ValueError("retained.unit.runtime-launch-anchor-ambiguous")
        relative = path[len(selected[0]) + 1 :]
        if directory:
            valid = relative in directories
        else:
            valid = digest is not None and members.get(relative) == digest
        if not valid:
            raise ValueError("retained.unit.runtime-unproven-workspace-member")
        return "retained:///" + relative

    def graph(raw):
        value = json.loads(raw)
        if len(value["components"]) + len(value["edges"]) > policy.max_closure_steps:
            raise ValueError("retained.unit.runtime-comparison-steps-exceeded")
        refs = {
            run.source_bom.managed_graph.root_ref: run.source_bom.managed_graph.root_ref
        }
        components, loader = {}, []
        native_refs = set()
        for component in value["components"]:
            reserve(0)
            original_ref = component["bom-ref"]
            properties = component.get("properties", [])
            paths = [p["value"] for p in properties if p["name"] == _ELF_PATH]
            if not paths:
                if [p for p in properties if p["name"] == _STATE]:
                    loader.append(component)
                    continue
                raise ValueError("retained.unit.runtime-component-form-unsupported")
            if len(paths) != 1 or component["type"] != "file":
                raise ValueError("retained.unit.runtime-native-image-ambiguous")
            architecture = [
                p["value"]
                for p in properties
                if p["name"] == "literate-ai:elf-architecture"
            ]
            version = component["version"]
            if version.startswith("sha256-"):
                exact = version.replace("sha256-", "sha256:", 1)
            elif version.startswith("build-id-"):
                exact = version.replace("build-id-", "build-id:", 1)
            else:
                raise ValueError(
                    "retained.unit.runtime-native-image-version-unsupported"
                )
            expected = canonical_identity(
                {
                    "elf_path": paths[0],
                    "elf_identity": exact,
                    "architecture": architecture[0].split("/")
                    if len(architecture) == 1
                    else [],
                }
            )
            if original_ref != "urn:literate-ai:elf:" + expected.digest:
                raise ValueError("retained.unit.runtime-native-image-identity-differs")
            hashes = [
                h["content"]
                for h in component.get("hashes", [])
                if h["alg"] == "SHA-256"
            ]
            if len(hashes) != 1 or (
                exact.startswith("sha256:") and exact != "sha256:" + hashes[0]
            ):
                raise ValueError("retained.unit.runtime-native-image-hash-differs")
            path = portable(paths[0], digest=hashes[0])
            projected = {
                **component,
                "properties": [
                    {"name": p["name"], "value": path} if p["name"] == _ELF_PATH else p
                    for p in properties
                ],
            }
            # No other property may carry an unmodeled workspace path.
            if any(
                root
                in canonical_json_bytes(
                    {k: v for k, v in projected.items() if k != "bom-ref"}
                ).decode()
                for root in anchors
            ):
                raise ValueError("retained.unit.runtime-unmodeled-path-property")
            body = {k: v for k, v in projected.items() if k != "bom-ref"}
            ref = (
                "urn:literate-ai:retained-runtime-image:"
                + canonical_identity(body).digest
            )
            if original_ref in refs or ref in components:
                raise ValueError("retained.unit.runtime-normalization-collision")
            refs[original_ref] = ref
            native_refs.add(original_ref)
            components[ref] = {**projected, "bom-ref": ref}
        for component in loader:
            reserve(0)
            # Reuse the existing complete state/hash/filtee validation and graph
            # reference rewriting, without its SDK-root string substitution.
            projected = project_freebsd_loader_context(
                component,
                refs=refs,
                native_refs=frozenset(native_refs),
                root_prefix="\x00",
            )
            state = json.loads(
                next(p["value"] for p in projected["properties"] if p["name"] == _STATE)
            )
            if set(state["paths"]) - {"LD_LIBRARY_PATH"}:
                raise ValueError("retained.unit.runtime-loader-control-unsupported")
            if state["paths"]:
                state["paths"]["LD_LIBRARY_PATH"] = ":".join(
                    portable(path, directory=True)
                    for path in state["paths"]["LD_LIBRARY_PATH"].split(":")
                )
            # Other loader configuration must remain exact. Reject rather than
            # normalize an unfamiliar embedded path-bearing field.
            if any(root in canonical_json_bytes(state).decode() for root in anchors):
                raise ValueError("retained.unit.runtime-unmodeled-loader-path")
            state["schema"] = "literate-ai/retained-unit-runtime-loader-context@1"
            identity = canonical_identity(state)
            ref = "urn:literate-ai:retained-runtime-loader:" + identity.digest
            if component["bom-ref"] in refs or ref in components:
                raise ValueError("retained.unit.runtime-normalization-collision")
            refs[component["bom-ref"]] = ref
            components[ref] = {
                **projected,
                "bom-ref": ref,
                "version": identity.digest,
                "hashes": [{"alg": "SHA-256", "content": identity.digest}],
                "properties": [
                    *(
                        p
                        for p in projected["properties"]
                        if p["name"] not in {_STATE, _FILTEE}
                    ),
                    {"name": _STATE, "value": canonical_json_bytes(state).decode()},
                    *(
                        {
                            "name": _FILTEE,
                            "value": canonical_json_bytes(record).decode(),
                        }
                        for record in state["filtees"]
                    ),
                ],
            }
        edges = [[refs[parent], refs[child]] for parent, child in value["edges"]]
        return canonical_identity(
            {
                "components": [components[ref] for ref in sorted(components)],
                "edges": sorted(edges),
            }
        ).uri

    return tuple(graph(raw) for raw in observations)


def ordinary_retained_workspace_roots(run, inputs, policy, *, maximum_memory_bytes):
    """Prove an ordinary POSIX launch root against its exact environment digest.

    Paths and file hashes suggest candidates only. Every ordinary command must
    bind the canonical clean environment reconstructed from that candidate root.
    SDK launches use their richer inspector and never take this fallback.
    """
    if run.resolved_bom.sdk_dependencies:
        raise ValueError("retained.unit.runtime-verified-sdk-launch-required")
    candidates = set()
    wanted = {"snapshot/tree": {}, "outputs": {}}
    steps, memory = policy.max_closure_steps, 0
    for raw in (run.resolved_bom.tool_observation, run.resolved_bom.export_observation):
        for component in json.loads(raw)["components"]:
            steps -= 1
            if steps < 0:
                raise ValueError("retained.unit.runtime-comparison-steps-exceeded")
            properties = component.get("properties", [])
            paths = [p["value"] for p in properties if p["name"] == _ELF_PATH]
            if not paths:
                continue
            if not any(
                p == {"name": "literate-ai:native-platform", "value": "freebsd"}
                for p in properties
            ):
                raise ValueError(
                    "retained.unit.runtime-relocation-platform-unsupported"
                )
            hashes = [
                h["content"]
                for h in component.get("hashes", [])
                if h["alg"] == "SHA-256"
            ]
            if len(paths) != 1 or len(hashes) != 1:
                raise ValueError("retained.unit.runtime-launch-candidate-ambiguous")
            for role in wanted:
                root, separator, relative = paths[0].rpartition("/" + role + "/")
                if not separator:
                    continue
                candidate = (root, relative, hashes[0])
                if candidate not in wanted[role]:
                    memory += 1024 + 8 * len(paths[0].encode("utf-8"))
                    if memory > maximum_memory_bytes:
                        raise ValueError(
                            "retained.unit.runtime-comparison-memory-exceeded"
                        )
                    wanted[role][candidate] = None
    for role, manifest in (
        ("snapshot/tree", inputs.source_manifest),
        ("outputs", run.output_manifest),
    ):
        selected = {relative for _root, relative, _digest in wanted[role]}
        known = {}
        for member in manifest.members:
            steps -= 1
            if steps < 0:
                raise ValueError("retained.unit.runtime-comparison-steps-exceeded")
            if member.path in selected and member.symlink_target is None:
                known[member.path] = member.sha256
        for root, relative, digest in wanted[role]:
            if known.get(relative) == digest:
                candidates.add(root)
    verified = []
    profile = run.source_bom.plan.profile
    path_count = (
        len(profile.output_command_paths)
        + len(profile.tools)
        + len(profile.runtime_library_paths)
    )
    steps -= path_count
    memory += 128 * path_count
    if steps < 0 or memory > maximum_memory_bytes:
        raise ValueError("retained.unit.runtime-comparison-budget-exceeded")
    path_rows = (
        *profile.output_command_paths,
        *(path for _name, path, _identity in profile.tools),
        *profile.runtime_library_paths,
    )
    profile_path_bytes = sum(len(path.encode("utf-8")) for path in path_rows)
    for root in sorted(candidates):
        # Bound the full candidate × profile work before allocating PATH or
        # hashing its environment. Temporary space covers two consecutive views.
        encoded_bound = (
            4096
            + 4 * profile_path_bytes
            + (16 + len(path_rows)) * 4 * len(root.encode("utf-8"))
        )
        steps -= len(path_rows) + len(run.observations) + 1 + encoded_bound // 64
        if steps < 0:
            raise ValueError("retained.unit.runtime-comparison-steps-exceeded")
        if memory + 32 * encoded_bound > maximum_memory_bytes:
            raise ValueError("retained.unit.runtime-comparison-memory-exceeded")
        workspace = PurePosixPath(root)
        if (
            not workspace.is_absolute()
            or ".." in workspace.parts
            or str(workspace) != root
        ):
            continue
        source, outputs = workspace / "snapshot/tree", workspace / "outputs"
        temporary = str(workspace / "tmp")
        environment = {
            "PATH": ":".join(
                [
                    *(str(outputs / path) for path in profile.output_command_paths),
                    *sorted(
                        {
                            str((source / path).parent)
                            for _name, path, _identity in profile.tools
                        }
                    ),
                ]
            ),
            "HOME": str(workspace / "home"),
            "TMPDIR": temporary,
            "TMP": temporary,
            "TEMP": temporary,
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if profile.runtime_library_paths:
            environment["LD_LIBRARY_PATH"] = ":".join(
                str(source / path) for path in profile.runtime_library_paths
            )
        identity = canonical_identity(environment)
        if run.observations and all(
            item.environment_identity == identity for item in run.observations
        ):
            verified.append(("freebsd", root, ()))
    if len(verified) != 1:
        raise ValueError("retained.unit.runtime-exact-launch-environment-required")
    return tuple(verified)
