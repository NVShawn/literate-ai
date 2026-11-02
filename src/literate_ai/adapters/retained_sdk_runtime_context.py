"""A reviewed captured loader projection, never an ambient-override permission."""

from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType

from literate_ai.adapters.retained_project_dependencies import (
    RetainedProjectDependencyGuard,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes

_PATHS = {"literate-ai:elf-path", "literate-ai:macho-path", "literate-ai:pe-path"}


def _native_graph(components, edges, *, sdk_root=None):
    images, refs, seeds = {}, {}, set()
    for component in components:
        properties = component.get("properties", [])
        paths = [p["value"] for p in properties if p["name"] in _PATHS]
        if not paths:
            continue
        hashes = [
            h["content"] for h in component.get("hashes", []) if h["alg"] == "SHA-256"
        ]
        uuids = tuple(
            sorted(
                (p["name"], p["value"])
                for p in properties
                if p["name"].startswith("literate-ai:macho-uuid:")
            )
        )
        # dyld shared-cache images have observed architecture/UUID identities,
        # not materialized standalone files. Preserve that native representation.
        if len(paths) != 1 or len(hashes) > 1 or (not hashes and not uuids):
            raise ValueError("retained.sdk.native-image-identity-ambiguous")
        identity = (tuple(hashes), component.get("version"), uuids)
        path = paths[0]
        if path.startswith("sdk:///"):
            if sdk_root is None:
                raise ValueError("retained.sdk.native-image-root-required")
            path = str(sdk_root / path[len("sdk:///") :])
            seeds.add(path)
        if path in images and images[path] != identity:
            raise ValueError("retained.sdk.native-image-identity-conflict")
        images[path] = identity
        refs[component["bom-ref"]] = path
    children = {path: set() for path in images}
    for parent, child in edges:
        if parent in refs and child in refs:
            children[refs[parent]].add(refs[child])
    return images, children, seeds


def _require_native_equivalence(
    actual,
    children,
    expected,
    expected_children,
    seeds,
    loader_paths,
    loader_members,
    *,
    path_type=Path,
    same_file=None,
    resolve_path=None,
):
    """Compare the reachable SDK graph, allowing reviewed external byte copies."""
    pending, mapped = set(seeds), {}
    while pending:
        path = pending.pop()
        if path in mapped:
            continue
        selected = path
        for directory in loader_paths:
            candidate = str(path_type(directory) / path_type(path).name)
            if candidate not in loader_members:
                continue
            same = candidate == path
            if not same:
                try:
                    same = (
                        Path(candidate).samefile(path)
                        if same_file is None
                        else same_file(candidate, path)
                    )
                except OSError:
                    same = False
            if path in seeds and not same:
                raise ValueError("retained.sdk.captured-loader-shadows-sdk-dependency")
            selected = (
                candidate
                if candidate in actual
                else str(Path(candidate).resolve())
                if resolve_path is None
                else resolve_path(candidate)
            )
            break
        if actual.get(selected) != expected[path]:
            raise ValueError("retained.sdk.captured-loader-shadows-sdk-dependency")
        mapped[path] = selected
        pending.update(expected_children[path] - mapped.keys())
    for path, selected in mapped.items():
        expected_targets = {mapped[p] for p in expected_children[path]} - {selected}
        if children.get(selected) != expected_targets:
            raise ValueError("retained.sdk.captured-loader-shadows-sdk-dependency")


def verify_historical_retained_sdk_runtime_context(
    document,
    *,
    plan,
    manifest,
    command_context,
    command_binding,
    tool_observation,
    sdk_values,
    guard,
    canonical_input_root=None,
):
    """Check captured loader facts using manifest aliases, never controller files."""

    def require(value):
        if not value:
            raise ValueError("retained.sdk.historical-runtime-context-differs")

    guard()
    path_type = (
        PureWindowsPath if command_context["platform"] == "windows" else PurePosixPath
    )
    input_root = path_type(command_context["workspace_root"]) / "snapshot" / "tree"
    canonical_root = (
        input_root if canonical_input_root is None else canonical_input_root
    )
    require(
        isinstance(canonical_root, path_type)
        and canonical_root.is_absolute()
        and ".." not in canonical_root.parts
    )
    environment = command_context["environment"]
    variable = (
        "DYLD_LIBRARY_PATH"
        if command_context["platform"] == "macos"
        else "LD_LIBRARY_PATH"
    )
    loader_environment = {
        variable: ":".join(
            str(input_root / name) for name in plan.profile.runtime_library_paths
        )
    }
    require(
        type(document) is dict
        and set(document)
        == {
            "schema",
            "plan_identity",
            "manifest_identity",
            "environment_identity",
            "captured_loader_environment",
            "input_root",
            "captured_runtime_members",
            "observation",
        }
    )
    require(
        document["schema"] == "literate-ai/retained-sdk-runtime-context@1"
        and document["plan_identity"] == plan.identity.to_dict()
        and document["manifest_identity"] == manifest.identity.to_dict()
        and document["environment_identity"]
        == canonical_identity(environment).to_dict()
        and command_binding["captured_runtime_identity"]
        == canonical_identity(document).to_dict()
        and document["input_root"] == str(input_root)
        and document["captured_loader_environment"] == loader_environment
        and all(
            environment.get(name) == value for name, value in loader_environment.items()
        )
    )
    members = tuple(
        member
        for member in manifest.members
        if member.path.rpartition("/")[0] in plan.profile.runtime_library_paths
    )
    require(
        document["captured_runtime_members"] == [member.to_dict() for member in members]
        and canonical_json_bytes(document["observation"]) == tool_observation
    )
    # The typed retained manifest already rejects escaping/cyclic symlinks and
    # requires hardlinks to their first physical member. Resolve only its exact
    # recorded aliases; never invoke samefile/resolve on a worker path here.
    by_path = {member.path: member for member in manifest.members}
    resolved = {}

    def physical(value):
        guard()
        path = path_type(value)
        try:
            relative = path.relative_to(input_root).as_posix()
        except ValueError:
            try:
                relative = path.relative_to(canonical_root).as_posix()
            except ValueError:
                return str(path)
        pending, seen = [], set()
        while relative not in resolved:
            require(relative not in seen)
            seen.add(relative)
            pending.append(relative)
            member = by_path.get(relative)
            if member is None or (
                member.symlink_target is None and member.hardlink_target is None
            ):
                resolved[relative] = str(canonical_root / relative)
                break
            relative = (
                member.symlink_destination
                if member.symlink_target is not None
                else member.hardlink_target
            )
        result = resolved[relative]
        for name in pending:
            resolved[name] = result
        return result

    observation = document["observation"]
    actual, children, _seeds = _native_graph(
        observation["components"], observation["edges"]
    )
    loader_members = {str(input_root / member.path) for member in members}
    loader_paths = loader_environment[variable].split(":")
    for prefix, dependency in sdk_values:
        guard()
        baseline = dependency.observation
        expected, expected_children, seeds = _native_graph(
            baseline.components,
            baseline.edges,
            sdk_root=canonical_root / prefix,
        )
        _require_native_equivalence(
            actual,
            children,
            expected,
            expected_children,
            seeds,
            loader_paths,
            loader_members,
            path_type=path_type,
            same_file=lambda left, right: physical(left) == physical(right),
            resolve_path=physical,
        )
    guard()


class RetainedSdkRuntimeContext:
    """Live dependency guard plus its immutable exact command-context evidence."""

    def __init__(self, guard, environment):
        if not isinstance(guard, RetainedProjectDependencyGuard):
            raise TypeError("retained.sdk.live-dependency-guard-required")
        self._guard = guard
        self._environment = MappingProxyType(dict(environment))
        self._content = canonical_json_bytes(
            guard.observe_sdk_runtime_context(environment)
        )

    @property
    def document(self):
        import json

        return json.loads(self._content)

    @property
    def identity(self):
        return canonical_identity(self.document)

    def require_current(
        self, execution, *, environment, source_identity, command_contract_identity
    ):
        document = self._guard.observe_sdk_runtime_context(environment)
        if canonical_json_bytes(document) != self._content:
            raise ValueError("retained.sdk.runtime-command-binding-changed")
        self.require_binding(
            execution,
            environment=environment,
            source_identity=source_identity,
            command_contract_identity=command_contract_identity,
        )

    def require_binding(
        self, execution, *, environment, source_identity, command_contract_identity
    ):
        """Check the already observed request before minting a command grant."""
        document = self.document
        if (
            dict(environment) != self._environment
            or document["manifest_identity"] != source_identity.to_dict()
            or document["plan_identity"] != command_contract_identity.to_dict()
        ):
            raise ValueError("retained.sdk.runtime-command-binding-changed")
        observation = document["observation"]
        actual, children, _ = _native_graph(
            observation["components"], observation["edges"]
        )
        loader_paths = next(
            iter(document["captured_loader_environment"].values())
        ).split(":")
        loader_members = {
            str(Path(document["input_root"]) / member["path"])
            for member in document["captured_runtime_members"]
        }
        # Preserve SDK-owned placements and the complete native code graph.
        # Reviewed copies of external runtime files may relocate only when their
        # native identities and complete dependency edges remain equivalent.
        for value in execution.values:
            baseline = value.dependencies.observation
            expected, expected_children, seeds = _native_graph(
                baseline.components, baseline.edges, sdk_root=Path(value.root)
            )
            # A native-free resource/header SDK has no loader edges to shadow;
            # its source/product and complete captured-context guards still apply.
            _require_native_equivalence(
                actual,
                children,
                expected,
                expected_children,
                seeds,
                loader_paths,
                loader_members,
            )
