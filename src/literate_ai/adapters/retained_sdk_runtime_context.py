"""A reviewed captured loader projection, never an ambient-override permission."""

from pathlib import Path
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
    actual, children, expected, expected_children, seeds, loader_paths, loader_members
):
    """Compare the reachable SDK graph, allowing reviewed external byte copies."""
    pending, mapped = set(seeds), {}
    while pending:
        path = pending.pop()
        if path in mapped:
            continue
        selected = path
        for directory in loader_paths:
            candidate = str(Path(directory) / Path(path).name)
            if candidate not in loader_members:
                continue
            same = candidate == path
            if not same:
                try:
                    same = Path(candidate).samefile(path)
                except OSError:
                    same = False
            if path in seeds and not same:
                raise ValueError("retained.sdk.captured-loader-shadows-sdk-dependency")
            selected = (
                candidate if candidate in actual else str(Path(candidate).resolve())
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
