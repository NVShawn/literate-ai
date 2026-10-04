"""Shared test fixtures extracted from test_standard_local_command_adapter."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

from literate_ai.adapters.lifecycle import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.lifecycle.standard_local import (
    _local_tree_identity,
)
from literate_ai.contracts import (
    ArtifactExport,
    BlobRef,
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentLifecycleCommand,
    canonical_identity,
    canonical_json_bytes,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.unit.standard_source_evidence_fixture import register_strict_source


def _identity(label: str):
    return canonical_identity({"standard-local-command-test": label})


def rewrite_self_authenticating_artifact(
    artifact_root: Path, export_id: str, payload: bytes
) -> None:
    """Rewrite export bytes and the self-declared tree field together."""

    export = artifact_root / export_id
    export.write_bytes(payload)
    manifest_path = artifact_root / "artifact-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["tree"] = _local_tree_identity(
        artifact_root, excluded=frozenset({"artifact-manifest.json"})
    ).uri
    manifest_path.write_bytes(canonical_json_bytes(manifest))


def copy_digest_cache_without_sidecars(source: Path, destination: Path) -> None:
    """Copy digest-named artifact directories and omit sibling checkpoint files."""

    destination.mkdir(parents=True)
    for child in source.iterdir():
        if not child.is_dir() or len(child.name) != 64:
            continue
        shutil.copytree(child, destination / child.name)


def _python_copy_lifecycle(root: Path):
    snapshot, execution = _fixture()
    generation_plan = execution.generation_plans[0]
    binding = LocalComponentToolBinding(sys.executable)
    build_script = (
        "from pathlib import Path; import shutil,sys; "
        "shutil.copy2(Path(sys.argv[1])/'app.py', Path(sys.argv[2]))"
    )
    commands = (
        ComponentLifecycleCommand(
            ComponentCommandPhase.BUILD,
            (
                "{tool}",
                "-c",
                build_script,
                "{source_root}",
                "{export_path}",
                "{object_root}",
                "{provider_artifacts}",
            ),
        ),
        ComponentLifecycleCommand(
            ComponentCommandPhase.TEST,
            (
                "{tool}",
                "-c",
                "from pathlib import Path; import json,sys; "
                "print(json.dumps(dict(schema="
                "'literate-ai/generated-test-results@1',cases=["
                "dict(case_id='fixture-example',outcome='passed')]),"
                "sort_keys=True,separators=(',',':')))",
                "{artifact_root}",
            ),
        ),
        ComponentLifecycleCommand(
            ComponentCommandPhase.EXECUTE,
            (
                "{tool}",
                "-c",
                "from pathlib import Path; import sys; "
                "print((Path(sys.argv[1])/'app').read_text().strip())",
                "{artifact_root}",
                "--litai-smoke",
            ),
        ),
    )
    contract = ComponentCommandContract(
        component_revision=generation_plan.component_revision,
        locked_build_authority_identity=_identity("locked-build-authority"),
        build_system_resolver_identity=_identity("build-system-resolver"),
        build_system_toolchain_identity=_identity("build-system-toolchain"),
        language_compiler_identity=binding.toolchain_identity,
        language_runtime_identity=_identity("language-runtime"),
        commands=commands,
        tool_bindings=tuple(
            ComponentCommandToolBinding(phase, binding.toolchain_identity)
            for phase in ComponentCommandPhase
        ),
        artifact_export=ComponentArtifactExportShape(
            "app",
            "executable",
            _identity("abi"),
            _identity("target"),
            "application/vnd.literate-ai.executable",
            _identity("producer"),
        ),
    )
    source = root / "source"
    source.mkdir()
    (source / "app.py").write_bytes(b"known-output\n")
    registry = LocalSourceTreeRegistry()
    candidate = register_strict_source(
        registry,
        source,
        snapshot=snapshot,
        generation_plan=generation_plan,
        identity_namespace="standard-local-command-test",
    )
    ports = LocalStandardLifecyclePorts(
        source_trees=registry,
        object_root=root / "objects",
        contracts=(contract,),
        tool_bindings=(binding,),
    )
    intent = ports.create(execution, generation_plan, candidate, (), ())
    index = ports.index(candidate.component_revision, candidate.tree_identity)
    plan = ports.finalize(intent, ports.authorize(intent, index))
    return ports, plan, candidate, intent


def _provider_export(export_id: str) -> ArtifactExport:
    payload = export_id.encode()
    return ArtifactExport(
        export_id=export_id,
        component_revision=_identity(f"{export_id}-revision"),
        role="static-library",
        abi_identity=_identity("abi"),
        target_identity=_identity("target"),
        media_type="application/x-native",
        producer_identity=_identity("producer"),
        source_tree_identity=_identity("source-tree"),
        toolchain_identity=_identity("toolchain"),
        authorization_identity=_identity("authorization"),
        dependency_artifact_identities=(),
        blob=BlobRef(
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            media_type=("application/x-native"),
        ),
    )
