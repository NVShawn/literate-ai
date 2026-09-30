"""Internal source-free staging and component-scoped retained qualification.

This is not an onboarding mutator: it neither moves source nor publishes project
receipts. The bundle is usable only after its completion manifest exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.harness_inventory import (
    HARNESS_INVENTORY_SCHEMA,
    classify_ci,
    execute_retained_harness,
    legacy_shim_authority,
    validate_harness_command_timeout,
)
from literate_ai.adapters.harness_tree import (
    SOURCE_SCOPE_SCHEMA,
    capture_retained_source_scope,
    copy_retained_source_tree,
)
from literate_ai.adapters.lifecycle_lock import project_lifecycle_lock
from literate_ai.adapters.monorepo_adoption import (
    PLAN_SCHEMA,
    MonorepoAdoptionError,
    _beneath,
    _direct,
    _fail,
    _fields,
    _read_direct_bytes,
    _relative,
    _source_members,
    _unique_object,
)
from literate_ai.adapters.retained_harness_receipts import (
    _sanitized_phases,
    _strict_test_total,
    retained_harness_worker_identity,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestReceiptPolicy,
    ProjectTestSummary,
    VersionedContentRef,
    canonical_identity,
    canonical_json_bytes,
)

BUNDLE_SCHEMA = "literate-ai/monorepo-component-bundle@1"
BINDING_SCHEMA = "literate-ai/monorepo-component-binding@1"
RECEIPT_SCHEMA = "literate-ai/monorepo-component-receipt@1"
INSTALLATION_SCHEMA = "literate-ai/monorepo-component-installation@1"
INSTALLATION_ROOT = ".literate/monorepo-components"
_MANIFEST = "bundle.json"
_SUITE = "monorepo-retained-harness"
_VERSION = "1.0.0"
_MAX_DOCUMENT = 16 * 1024 * 1024


def _bytes_identity(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _read(path: Path) -> bytes:
    try:
        return _read_direct_bytes(path, _MAX_DOCUMENT)
    except MonorepoAdoptionError as exc:
        if exc.code == "monorepo.input_too_large":
            _fail("bundle_invalid", "component evidence document exceeds 16 MiB")
        if exc.code == "monorepo.source_invalid":
            _fail("bundle_invalid", exc.message)
        raise


def _document(path: Path) -> dict[str, Any]:
    return _parse_document(_read(path))


def _parse_document(content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(content, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        _fail("bundle_invalid", f"invalid component evidence document: {exc}")
    if not isinstance(value, dict) or _json_bytes(value) != content:
        _fail("bundle_invalid", "component evidence must be a canonical JSON object")
    return value


def _write_new(path: Path, content: bytes) -> tuple[int, int]:
    if len(content) > _MAX_DOCUMENT:
        _fail("bundle_invalid", "component evidence document exceeds 16 MiB")
    require_safe_directory(path.parent)
    identity = None
    try:
        with path.open("xb") as stream:
            identity = os.fstat(stream.fileno())
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if identity is not None:
            with suppress(OSError, ValueError, UnsafeFilesystemPathError):
                current = path.lstat()
                if (
                    not stat_is_link_or_reparse(current)
                    and (current.st_dev, current.st_ino)
                    == (identity.st_dev, identity.st_ino)
                    and content.startswith(_read(path))
                ):
                    path.unlink()
        raise
    return identity.st_dev, identity.st_ino


def _remove_owned(path: Path, node: tuple[int, int], content: bytes) -> None:
    with suppress(OSError, ValueError, UnsafeFilesystemPathError):
        current = path.lstat()
        if (
            not stat_is_link_or_reparse(current)
            and (current.st_dev, current.st_ino) == node
            and _read(path) == content
        ):
            path.unlink()


def _external(source: Path, destination: Path) -> Path:
    configured = Path(destination).absolute()
    require_safe_directory(configured.parent)
    if configured.is_symlink() or configured.exists():
        _fail("destination_exists", "staging/evidence destination must be new")
    if configured.is_relative_to(source) or source.is_relative_to(configured):
        _fail("destination_invalid", "staging/evidence must be outside the source tree")
    return configured


def _source_root(path: Path) -> Path:
    root = Path(path).absolute()
    require_safe_directory(root)
    return root


def _scope(paths: list[str]) -> dict[str, Any]:
    return {
        "schema": SOURCE_SCOPE_SCHEMA,
        "policy": "monorepo-component-inputs@1",
        "paths": sorted(paths),
        "paths_identity": canonical_identity(sorted(paths)).uri,
        "excluded_roots": [],
        "excluded": [],
    }


def _inventory(component: dict[str, Any]) -> dict[str, Any]:
    stages = [{**stage, "cost": "host-heavy"} for stage in component["commands"]]
    commands = {stage["id"]: stage for stage in stages}
    return {
        "schema": HARNESS_INVENTORY_SCHEMA,
        "findings": [],
        "commands": commands,
        "stages": stages,
        "ci": classify_ci([], commands),
        "source_scope": _scope(component["owned_sources"] + component["shared_inputs"]),
    }


def _component_projection(component: dict[str, Any]) -> bytes:
    inventory = _inventory(component)
    rendered = legacy_shim_authority(inventory, {"phases": []})[
        "components/legacy-project-wrapper/component.md"
    ]
    header = rendered.split("\n---\n", 1)[0]
    header = header.replace(
        "namespace: legacy-adoption", "namespace: monorepo-adoption"
    )
    header = header.replace(
        "display_name: Legacy Project Pipeline Wrapper",
        "display_name: Retained " + component["name"],
    )
    header = header.replace(
        "entrypoints:\n  - name: run\n    kind: portable-application\n"
        "    path: litai.harness.mk",
        "entrypoints: []",
    )
    body = (
        f"\n---\n# Retained {component['name']}\n\n"
        f"This selected Component owns build root `{component['root']}`.\n"
        "Its exact source ownership, shared inputs and commands are declared in\n"
        "`binding.json`. Source remains original-source authority in one external\n"
        "retained tree; this projection contains no implementation copy.\n\n"
        "### Requirement: Preserve the selected boundary\n\n"
        "Retained qualification SHALL execute only the declared commands against\n"
        "owned source and explicitly shared inputs, preserving relative paths.\n"
        "A passing retained receipt is not native generation, conversion parity,\n"
        "independent semantic acceptance or permission to retire source.\n\n"
        "#### Scenario: A shared input changes\n\n"
        "- **WHEN** an owned or declared shared source input changes\n"
        "- **THEN** this Component's retained receipt becomes stale\n"
    )
    return (header + body).encode("utf-8")


def stage_monorepo_components(
    source_root: Path,
    selection_path: Path,
    destination: Path,
    *,
    expected_plan_identity: str,
    acknowledged: bool,
    default_branch: str | None = None,
) -> dict[str, Any]:
    """Publish a new source-free bundle, not an adopted project."""
    if not acknowledged:
        _fail("acknowledgement_required", "component staging requires acknowledgement")
    ContentIdentity.parse_uri(expected_plan_identity)
    source = _source_root(source_root)
    target = _external(source, destination)
    # Late import avoids making the converter depend on its staging consumer.
    from literate_ai.adapters.project_initialization import plan_convert

    def fresh_plan():
        return plan_convert(
            source, default_branch=default_branch, root_plan=selection_path
        )["root_refinement"]

    plan = fresh_plan()
    if plan["plan_identity"] != expected_plan_identity:
        _fail("plan_changed", "reviewed Component plan changed; review again")
    payloads = {"plan.json": _json_bytes(plan)}
    for component in plan["components"]:
        name = component["name"]
        binding = _component_binding(plan, component)
        payloads[f"components/{name}/binding.json"] = _json_bytes(binding)
        payloads[f"components/{name}/component.md"] = _component_projection(component)
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "plan_identity": expected_plan_identity,
        "components": [c["name"] for c in plan["components"]],
        "files": {
            path: _bytes_identity(data) for path, data in sorted(payloads.items())
        },
        "source_copied": False,
        "project_initialized": False,
    }
    created: list[tuple[Path, tuple[int, int], bytes]] = []
    directories: list[tuple[Path, tuple[int, int]]] = []
    try:
        target.mkdir()
        node = target.lstat()
        directories.append((target, (node.st_dev, node.st_ino)))
        for relative, content in payloads.items():
            path = target / relative
            missing = []
            parent = path.parent
            while parent != target and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for parent in reversed(missing):
                require_safe_directory(parent.parent)
                parent.mkdir()
                node = parent.lstat()
                directories.append((parent, (node.st_dev, node.st_ino)))
            created.append((path, _write_new(path, content), content))
        if fresh_plan()["plan_identity"] != expected_plan_identity:
            _fail("plan_changed", "source or selection changed during staging")
        content = _json_bytes(manifest)
        created.append(
            (target / _MANIFEST, _write_new(target / _MANIFEST, content), content)
        )
    except BaseException:
        for path, node, content in reversed(created):
            _remove_owned(path, node, content)
        for directory, identity in reversed(directories):
            with suppress(OSError, ValueError, UnsafeFilesystemPathError):
                require_safe_directory(directory.parent)
                node = directory.lstat()
                if (
                    not stat_is_link_or_reparse(node)
                    and (node.st_dev, node.st_ino) == identity
                ):
                    directory.rmdir()
        raise
    return {**manifest, "bundle_identity": canonical_identity(manifest).uri}


def _component_binding(
    plan: dict[str, Any], component: dict[str, Any]
) -> dict[str, Any]:
    name = component["name"]
    prefixes = [
        item["path"]
        for item in plan["selection"]["shared_sources"]
        if item["owner"] == name or name in item["consumers"]
    ]
    inventory = _inventory(component)
    return {
        "schema": BINDING_SCHEMA,
        "component": component,
        "shared_prefixes": sorted(prefixes),
        "inventory": inventory,
        "receipt_policy": _policy(inventory).to_dict(),
        "authority": "original-source",
        "stage": "staged",
    }


def _verified_bundle(bundle: Path, expected_identity: str) -> dict[str, Any]:
    """Reopen all staged authority against the caller's retained bundle identity."""
    ContentIdentity.parse_uri(expected_identity)
    require_safe_directory(bundle)
    manifest = _document(bundle / _MANIFEST)
    _fields(
        manifest,
        {
            "schema",
            "plan_identity",
            "components",
            "files",
            "source_copied",
            "project_initialized",
        },
        "bundle manifest",
    )
    if canonical_identity(manifest).uri != expected_identity:
        _fail("bundle_changed", "reviewed bundle identity changed")
    if (
        manifest["schema"] != BUNDLE_SCHEMA
        or manifest["source_copied"] is not False
        or manifest["project_initialized"] is not False
    ):
        _fail("bundle_invalid", "unsupported Component bundle")
    plan_bytes = _read(bundle / "plan.json")
    plan = _parse_document(plan_bytes)
    _fields(
        plan,
        {
            "schema",
            "selection",
            "components",
            "source_scope",
            "source_members",
            "selection_identity",
            "source_identity",
            "writes",
            "execution_performed",
            "apply_supported",
            "blockers",
            "plan_identity",
        },
        "staged adoption plan",
    )
    if (
        plan.get("schema") != PLAN_SCHEMA
        or plan["writes"] is not False
        or plan["execution_performed"] is not False
        or plan["apply_supported"] is not True
        or plan.get("plan_identity") != manifest["plan_identity"]
        or canonical_identity(
            {k: v for k, v in plan.items() if k != "plan_identity"}
        ).uri
        != manifest["plan_identity"]
    ):
        _fail("bundle_invalid", "bundle plan does not match its reviewed identity")
    names = manifest["components"]
    if (
        not isinstance(names, list)
        or len(names) < 2
        or not all(
            isinstance(name, str)
            and name != "."
            and "/" not in name
            and _relative(name) == name
            for name in names
        )
        or names != sorted(set(names))
        or not isinstance(plan.get("components"), list)
        or any(not isinstance(c, dict) for c in plan["components"])
        or [c.get("name") for c in plan["components"]] != names
    ):
        _fail("bundle_invalid", "bundle Component inventory differs from its plan")
    payloads = {"plan.json": plan_bytes}
    try:
        for component in plan["components"]:
            name = component["name"]
            payloads[f"components/{name}/binding.json"] = _json_bytes(
                _component_binding(plan, component)
            )
            payloads[f"components/{name}/component.md"] = _component_projection(
                component
            )
    except (KeyError, TypeError) as exc:
        _fail("bundle_invalid", f"invalid staged Component plan: {exc}")
    if manifest["files"] != {
        path: _bytes_identity(data) for path, data in sorted(payloads.items())
    }:
        _fail(
            "bundle_invalid",
            "bundle file inventory differs from its planned projections",
        )
    for relative, content in payloads.items():
        if _read(_direct(bundle, relative)) != content:
            _fail("binding_changed", "staged Component projection changed")
    return plan


def _binding(bundle: Path, name: str) -> tuple[dict[str, Any], bytes]:
    require_safe_directory(bundle)
    manifest = _document(bundle / _MANIFEST)
    _fields(
        manifest,
        {
            "schema",
            "plan_identity",
            "components",
            "files",
            "source_copied",
            "project_initialized",
        },
        "bundle manifest",
    )
    if (
        manifest["schema"] != BUNDLE_SCHEMA
        or manifest["source_copied"] is not False
        or manifest["project_initialized"] is not False
    ):
        _fail("bundle_invalid", "unsupported Component bundle")
    if (
        not isinstance(name, str)
        or _relative(name) != name
        or "/" in name
        or name == "."
        or name not in manifest["components"]
    ):
        _fail("component_unknown", "Component is not part of this bundle")
    documents = []
    for filename in ("binding.json", "component.md"):
        relative = f"components/{name}/{filename}"
        content = _read(_direct(bundle, relative))
        if _bytes_identity(content) != manifest["files"].get(relative):
            _fail("binding_changed", "staged Component projection changed")
        documents.append(content)
    binding = _parse_document(documents[0])
    _fields(
        binding,
        {
            "schema",
            "component",
            "shared_prefixes",
            "inventory",
            "receipt_policy",
            "authority",
            "stage",
        },
        "Component binding",
    )
    if (
        binding["schema"] != BINDING_SCHEMA
        or binding["authority"] != "original-source"
        or binding["stage"] != "staged"
    ):
        _fail("binding_invalid", "unsupported Component custody state")
    if binding["component"]["name"] != name or binding["inventory"] != _inventory(
        binding["component"]
    ):
        _fail("binding_invalid", "Component inventory does not match its declaration")
    if binding["receipt_policy"] != _policy(binding["inventory"]).to_dict():
        _fail(
            "binding_invalid",
            "Component receipt policy differs from its admitted runner",
        )
    return binding, documents[1]


def component_retained_revision(
    source_root: Path, bundle_root: Path, name: str
) -> dict[str, Any]:
    """Inspect one boundary; other Components' private source is not its input."""
    source = _source_root(source_root)
    bundle = Path(bundle_root).absolute()
    binding, specification = _binding(bundle, name)
    component = binding["component"]
    paths = binding["inventory"]["source_scope"]["paths"]
    prefixes = [component["root"], *binding["shared_prefixes"]]
    current_scope = capture_retained_source_scope(source)
    current_paths = [
        path
        for path in current_scope["paths"]
        if any(_beneath(path, prefix) for prefix in prefixes)
    ]
    if current_paths != paths:
        _fail(
            "scope_changed",
            "Component input membership changed; reviewed refresh required",
        )
    members = _source_members(source, paths)
    revision = {
        "schema": "literate-ai/monorepo-component-revision@1",
        "binding_identity": canonical_identity(binding).uri,
        "specification_identity": _bytes_identity(specification),
        "source_members": members,
    }
    return {**revision, "identity": canonical_identity(revision).uri}


def _runner(inventory: dict[str, Any]) -> ContentIdentity:
    return canonical_identity(
        {
            "schema": "literate-ai/monorepo-component-runner@1",
            "suite": _SUITE,
            "version": _VERSION,
            "inventory_identity": canonical_identity(inventory).uri,
            "execution": "strict-retained-harness-disposable-copy",
            "trust_boundary": "supported-api-tcb",
        }
    )


def _policy(inventory: dict[str, Any]) -> ProjectTestReceiptPolicy:
    return ProjectTestReceiptPolicy(
        suite_id=_SUITE,
        suite_version=_VERSION,
        runner_identity=_runner(inventory),
        required_evidence_kinds=("observation-result", "test-runner"),
        minimum_test_count=1,
    )


def _receipt(
    revision: dict[str, Any],
    name: str,
    inventory: dict[str, Any],
    evidence: dict[str, Any],
) -> ProjectTestReceipt:
    phases = evidence["phases"]
    expected = inventory["stages"]
    if not isinstance(phases, list) or len(phases) != len(expected):
        _fail("receipt_invalid", "receipt must include every admitted phase")
    source_tree = _fields(
        evidence["source_tree_before"],
        {
            "identity",
            "file_count",
            "missing_count",
            "total_bytes",
            "scope",
        },
        "retained source observation",
    )
    ContentIdentity.parse_uri(source_tree["identity"])
    if (
        source_tree["scope"] != "authored-source"
        or type(source_tree["file_count"]) is not int
        or source_tree["file_count"] != len(inventory["source_scope"]["paths"])
        or type(source_tree["missing_count"]) is not int
        or source_tree["missing_count"] != 0
        or type(source_tree["total_bytes"]) is not int
        or source_tree["total_bytes"] < 0
    ):
        _fail("receipt_invalid", "retained source observation is incomplete")
    for phase, stage in zip(phases, expected, strict=True):
        if (
            not isinstance(phase, dict)
            or phase.get("phase") != stage["id"]
            or type(phase.get("exit_code")) is not int
            or phase["exit_code"] != 0
            or phase.get("timed_out") is not False
            or phase.get("output_within_limits") is not True
            or phase.get("evidence") != stage["evidence"]
            or phase.get("source_tree") != evidence["source_tree_before"]
        ):
            _fail("receipt_invalid", "receipt phase is missing or not passing")
        if phase.get("command") != stage["command"]:
            _fail("receipt_invalid", "receipt command differs from admitted command")
    tests = _strict_test_total(
        phases,
        project_revision=ContentIdentity.parse_uri(revision["identity"]),
        worker_identity=ContentIdentity.parse_uri(evidence["worker_identity"]),
        known_failure_report=None,
    )
    for phase in phases:
        if phase["phase"] == "test":
            counts = phase["test_collection"]
            if (
                type(counts.get("total")) is not int
                or counts["total"] < 1
                or type(counts.get("passed")) is not int
                or counts.get("passed") != counts["total"]
                or any(
                    counts.get(key, 0) for key in ("failed", "skipped", "known_failed")
                )
            ):
                _fail(
                    "receipt_invalid", "all selected tests must pass without omissions"
                )
    runner = _runner(inventory)
    result = canonical_identity(evidence)
    return ProjectTestReceipt(
        project_id=f"monorepo-component/{name}",
        project_revision_identity=ContentIdentity.parse_uri(revision["identity"]),
        subject_identity=canonical_identity(revision["source_members"]),
        suite=VersionedContentRef("test-suite", _SUITE, _VERSION, runner),
        outcome="passed",
        summary=ProjectTestSummary(tests, tests, 0, 0),
        result_identity=result,
        evidence=(
            ProjectTestEvidence("observation-result", result),
            ProjectTestEvidence("test-runner", runner),
        ),
    )


def run_component_retained_harness(
    source_root: Path,
    bundle_root: Path,
    name: str,
    output: Path,
    *,
    acknowledged: bool,
    worker_id: str,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Earn one scoped receipt using real execution, never relabel a project receipt."""
    if not acknowledged:
        _fail(
            "execution_acknowledgement_required",
            "retained host execution requires acknowledgement",
        )
    timeout = validate_harness_command_timeout(timeout_seconds)
    worker = retained_harness_worker_identity(worker_id)
    source = _source_root(source_root)
    bundle = Path(bundle_root).absolute()
    destination = _external(source, output)
    if destination.is_relative_to(bundle):
        _fail(
            "destination_invalid", "receipt output must be outside the immutable bundle"
        )
    with project_lifecycle_lock(bundle, operation=f"monorepo-retained:{name}"):
        before = component_retained_revision(source, bundle, name)
        binding, _ = _binding(bundle, name)
        inventory = binding["inventory"]
        with tempfile.TemporaryDirectory(
            prefix="litai-component-retained-"
        ) as temporary:
            # Canonicalize our newly created temporary parent (macOS /var is an
            # OS alias), not any operator-supplied source or evidence path.
            execution = Path(temporary).resolve() / "source"
            copy_retained_source_tree(source, execution, inventory["source_scope"])
            if (
                _source_members(execution, inventory["source_scope"]["paths"])
                != before["source_members"]
            ):
                _fail(
                    "source_changed",
                    "disposable copy differs from reviewed Component inputs",
                )
            report = execute_retained_harness(
                inventory, legacy_root=execution, timeout_seconds=timeout
            )
            if (
                _source_members(execution, inventory["source_scope"]["paths"])
                != before["source_members"]
            ):
                _fail("source_changed", "retained commands changed exact source bytes")
        after = component_retained_revision(source, bundle, name)
        if after != before:
            _fail("source_changed", "Component inputs changed during execution")
        evidence = {
            "schema": "literate-ai/monorepo-component-run@1",
            "component": name,
            "revision_identity": before["identity"],
            "runner_identity": _runner(inventory).uri,
            "worker_identity": worker.uri,
            "timeout_seconds": timeout,
            "classification": "retained-component-execution",
            "native_generation": False,
            "independent_acceptance": False,
            "source_tree_before": report["legacy_source_tree_before"],
            "phases": _sanitized_phases(report),
        }
        receipt = _receipt(before, name, inventory, evidence)
        envelope = {
            "schema": RECEIPT_SCHEMA,
            "receipt": receipt.to_dict(),
            "evidence": evidence,
        }
        _write_new(destination, _json_bytes(envelope))
        return envelope


def check_component_retained_receipt(
    source_root: Path, bundle_root: Path, name: str, receipt_path: Path
) -> dict[str, Any]:
    """Read-only verification against exact current scoped inputs and evidence."""
    revision = component_retained_revision(source_root, bundle_root, name)
    binding, _ = _binding(Path(bundle_root).absolute(), name)
    envelope = _document(Path(receipt_path).absolute())
    _fields(envelope, {"schema", "receipt", "evidence"}, "Component receipt")
    if envelope["schema"] != RECEIPT_SCHEMA:
        _fail("receipt_invalid", "unsupported Component receipt schema")
    evidence = envelope["evidence"]
    _fields(
        evidence,
        {
            "schema",
            "component",
            "revision_identity",
            "runner_identity",
            "worker_identity",
            "timeout_seconds",
            "classification",
            "native_generation",
            "independent_acceptance",
            "source_tree_before",
            "phases",
        },
        "Component run evidence",
    )
    if (
        evidence["schema"] != "literate-ai/monorepo-component-run@1"
        or evidence["classification"] != "retained-component-execution"
        or evidence["native_generation"] is not False
        or evidence["independent_acceptance"] is not False
    ):
        _fail("receipt_invalid", "retained evidence must not claim native acceptance")
    validate_harness_command_timeout(evidence["timeout_seconds"])
    if (
        evidence.get("component") != name
        or evidence.get("revision_identity") != revision["identity"]
    ):
        _fail("receipt_stale", "receipt does not bind the current Component revision")
    if evidence.get("runner_identity") != _runner(binding["inventory"]).uri:
        _fail("receipt_invalid", "receipt runner differs from admitted runner")
    expected = _receipt(revision, name, binding["inventory"], evidence)
    if ProjectTestReceipt.from_dict(envelope["receipt"]) != expected:
        _fail(
            "receipt_invalid", "receipt does not match its exact retained run evidence"
        )
    if component_retained_revision(source_root, bundle_root, name) != revision:
        _fail("source_changed", "Component inputs changed during receipt inspection")
    return {
        "state": "current",
        "component": name,
        "receipt_identity": expected.identity.uri,
    }


def check_monorepo_retained_receipts(
    source_root: Path,
    bundle_root: Path,
    receipts: dict[str, Path],
    *,
    expected_bundle_identity: str,
) -> dict[str, Any]:
    """Check the entire staged boundary set; do not mint project release evidence."""
    source = _source_root(source_root)
    bundle = Path(bundle_root).absolute()
    plan = _verified_bundle(bundle, expected_bundle_identity)
    names = [component["name"] for component in plan["components"]]
    if not isinstance(receipts, dict) or set(receipts) != set(names):
        _fail(
            "receipts_incomplete",
            "exactly one receipt per selected Component is required",
        )

    def source_observation():
        scope = capture_retained_source_scope(source)
        if scope["paths"] != plan["source_scope"]["paths"]:
            _fail(
                "scope_changed",
                "whole-source membership changed; reviewed refresh required",
            )
        members = _source_members(source, scope["paths"])
        if capture_retained_source_scope(source)["paths"] != scope["paths"]:
            _fail(
                "source_changed", "source membership changed during bundle inspection"
            )
        return members

    before = source_observation()
    receipt_paths = {name: Path(receipts[name]).absolute() for name in names}
    receipt_bytes = {name: _read(path) for name, path in receipt_paths.items()}
    results = [
        check_component_retained_receipt(source, bundle, name, receipt_paths[name])
        for name in names
    ]
    # Local checks intentionally ignore other Components. The aggregate may not:
    # changes to a previously checked boundary must invalidate this observation.
    _verified_bundle(bundle, expected_bundle_identity)
    if any(_read(receipt_paths[name]) != receipt_bytes[name] for name in names):
        _fail("receipt_changed", "Component receipt changed during bundle inspection")
    if source_observation() != before:
        _fail("source_changed", "source changed during bundle inspection")
    result = {
        "schema": "literate-ai/monorepo-retained-check@1",
        "state": "current",
        "stage": "staged",
        "authority": "original-source",
        "project_initialized": False,
        "bundle_identity": expected_bundle_identity,
        "source_identity": canonical_identity(before).uri,
        "components": results,
        "writes": False,
        "execution_performed": False,
    }
    return {**result, "identity": canonical_identity(result).uri}


def _publish_payloads(root: Path, payloads: dict[str, bytes]) -> None:
    created: list[tuple[Path, tuple[int, int], bytes]] = []
    directories: list[tuple[Path, tuple[int, int]]] = []
    try:
        for relative, content in sorted(payloads.items()):
            path = root.joinpath(*Path(_relative(relative)).parts)
            missing = []
            parent = path.parent
            while parent != root and not parent.exists():
                missing.append(parent)
                parent = parent.parent
            if parent != root:
                require_safe_directory(parent)
            for directory in reversed(missing):
                require_safe_directory(directory.parent)
                directory.mkdir()
                node = directory.lstat()
                directories.append((directory, (node.st_dev, node.st_ino)))
            if path.exists() or path.is_symlink():
                _fail("destination_exists", "installed Component path must be new")
            created.append((path, _write_new(path, content), content))
    except BaseException:
        for path, node, content in reversed(created):
            _remove_owned(path, node, content)
        for directory, identity in reversed(directories):
            with suppress(OSError, ValueError, UnsafeFilesystemPathError):
                require_safe_directory(directory.parent)
                node = directory.lstat()
                if (
                    not stat_is_link_or_reparse(node)
                    and (node.st_dev, node.st_ino) == identity
                ):
                    directory.rmdir()
        raise


def _replace_payloads(
    root: Path,
    payloads: dict[str, bytes],
    *,
    validate: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Replace one closed installed payload set, restoring owned bytes on failure."""

    paths = {relative: _direct(root, relative) for relative in sorted(payloads)}
    before = {relative: _read(path) for relative, path in paths.items()}
    written: list[str] = []

    def replace(path: Path, content: bytes) -> None:
        require_safe_directory(path.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    try:
        for relative, content in sorted(payloads.items()):
            replace(paths[relative], content)
            written.append(relative)
        return None if validate is None else validate()
    except BaseException:
        for relative in reversed(written):
            if _read(paths[relative]) == payloads[relative]:
                replace(paths[relative], before[relative])
        raise


def _installation_payloads(
    source_root: Path,
    bundle_root: Path,
    project_root: Path,
    receipts: dict[str, Path],
    *,
    expected_bundle_identity: str,
) -> tuple[Path, dict[str, bytes], dict[str, Any]]:
    project = _source_root(project_root)
    source = _source_root(source_root)
    if source == project or not source.is_relative_to(project):
        _fail(
            "source_invalid",
            "installed retained source must be a project-relative directory",
        )
    bundle = Path(bundle_root).absolute()
    check = check_monorepo_retained_receipts(
        source,
        bundle,
        receipts,
        expected_bundle_identity=expected_bundle_identity,
    )
    manifest_bytes = _read(bundle / _MANIFEST)
    manifest = _parse_document(manifest_bytes)
    names = list(manifest["components"])
    receipt_bytes = {name: _read(Path(receipts[name]).absolute()) for name in names}
    payloads: dict[str, bytes] = {
        f"{INSTALLATION_ROOT}/bundle/{_MANIFEST}": manifest_bytes,
    }
    for relative in manifest["files"]:
        content = _read(_direct(bundle, relative))
        payloads[f"{INSTALLATION_ROOT}/bundle/{relative}"] = content
    for name in names:
        payloads[f"{INSTALLATION_ROOT}/receipts/{name}.json"] = receipt_bytes[name]
        for filename in ("binding.json", "component.md"):
            content = _read(_direct(bundle, f"components/{name}/{filename}"))
            payloads[f"components/{name}/{filename}"] = content
    installation = {
        "schema": INSTALLATION_SCHEMA,
        "state": "current",
        "stage": "retained",
        "authority": "original-source",
        "source_root": source.relative_to(project).as_posix(),
        "bundle_identity": expected_bundle_identity,
        "receipt_identities": {
            name: _bytes_identity(receipt_bytes[name]) for name in names
        },
        "retained_check_identity": check["identity"],
        "components": names,
        "boundary_transfer": {
            name: {
                "state": "retained-source",
                "authority": "original-source",
                "refresh_state": "current",
                "revision_identity": _parse_document(receipt_bytes[name])["evidence"][
                    "revision_identity"
                ],
                "receipt_identity": next(
                    item["receipt_identity"]
                    for item in check["components"]
                    if item["component"] == name
                ),
            }
            for name in names
        },
        "source_copied": False,
    }
    payloads[f"{INSTALLATION_ROOT}/installation.json"] = _json_bytes(installation)
    return project, payloads, installation


def install_monorepo_components(
    source_root: Path,
    bundle_root: Path,
    project_root: Path,
    receipts: dict[str, Path],
    *,
    expected_bundle_identity: str,
) -> dict[str, Any]:
    """Publish verified source-free projections and retained receipt custody."""
    project, payloads, installation = _installation_payloads(
        source_root,
        bundle_root,
        project_root,
        receipts,
        expected_bundle_identity=expected_bundle_identity,
    )
    destinations = [
        project.joinpath(*Path(INSTALLATION_ROOT).parts),
        *(project / "components" / name for name in installation["components"]),
    ]
    if any(path.exists() or path.is_symlink() for path in destinations):
        _fail("destination_exists", "installed Component paths must be new")
    _publish_payloads(project, payloads)
    return check_installed_monorepo_components(project)


def check_installed_monorepo_components(project_root: Path) -> dict[str, Any]:
    """Reopen an installed refined-adoption projection without executing commands."""
    project = _source_root(project_root)
    installation_path = _direct(project, f"{INSTALLATION_ROOT}/installation.json")
    before = _read(installation_path)
    installation = _parse_document(before)
    _fields(
        installation,
        {
            "schema",
            "state",
            "stage",
            "authority",
            "source_root",
            "bundle_identity",
            "receipt_identities",
            "retained_check_identity",
            "components",
            "boundary_transfer",
            "source_copied",
        },
        "installed Component custody",
    )
    if (
        installation["schema"] != INSTALLATION_SCHEMA
        or installation["state"] != "current"
        or installation["stage"] != "retained"
        or installation["authority"] != "original-source"
        or installation["source_copied"] is not False
    ):
        _fail("installation_invalid", "unsupported installed Component custody")
    source = _direct(project, installation["source_root"])
    bundle = _direct(project, f"{INSTALLATION_ROOT}/bundle")
    names = installation["components"]
    if not isinstance(names, list) or names != sorted(set(names)):
        _fail("installation_invalid", "installed Component inventory is invalid")
    receipts = {
        name: _direct(project, f"{INSTALLATION_ROOT}/receipts/{name}.json")
        for name in names
    }
    if installation["receipt_identities"] != {
        name: _bytes_identity(_read(receipts[name])) for name in names
    }:
        _fail("receipt_changed", "installed Component receipt changed")
    checked = check_monorepo_retained_receipts(
        source,
        bundle,
        receipts,
        expected_bundle_identity=installation["bundle_identity"],
    )
    if checked["identity"] != installation["retained_check_identity"]:
        _fail("installation_invalid", "installed retained check identity changed")
    expected_transfer = {
        name: {
            "state": "retained-source",
            "authority": "original-source",
            "refresh_state": "current",
            "revision_identity": _parse_document(_read(receipts[name]))["evidence"][
                "revision_identity"
            ],
            "receipt_identity": next(
                item["receipt_identity"]
                for item in checked["components"]
                if item["component"] == name
            ),
        }
        for name in names
    }
    if installation["boundary_transfer"] != expected_transfer:
        _fail("installation_invalid", "Component boundary-transfer state is invalid")
    for name in names:
        for filename in ("binding.json", "component.md"):
            if _read(_direct(project, f"components/{name}/{filename}")) != _read(
                _direct(bundle, f"components/{name}/{filename}")
            ):
                _fail("binding_changed", "installed Component projection changed")
    if _read(installation_path) != before:
        _fail("installation_changed", "installed Component custody changed")
    return {**installation, "identity": canonical_identity(installation).uri}


def refresh_installed_monorepo_components(
    project_root: Path,
    *,
    acknowledged: bool,
    worker_id: str = "scope-refresh-local",
    timeout_seconds: int = 1800,
    default_branch: str | None = None,
) -> dict[str, Any]:
    """Replan current retained membership and requalify only stale Components."""

    if not acknowledged:
        _fail(
            "execution_acknowledgement_required",
            "Component refresh execution requires acknowledgement",
        )
    project = _source_root(project_root)
    installation = _document(_direct(project, f"{INSTALLATION_ROOT}/installation.json"))
    old_bundle = _direct(project, f"{INSTALLATION_ROOT}/bundle")
    old_plan = _verified_bundle(old_bundle, installation["bundle_identity"])
    source = _direct(project, installation["source_root"])
    names = list(installation["components"])
    old_receipts = {
        name: _direct(project, f"{INSTALLATION_ROOT}/receipts/{name}.json")
        for name in names
    }
    with tempfile.TemporaryDirectory(
        prefix="litai-monorepo-refresh-", dir=project.parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        selection = temporary / "selection.json"
        _write_new(selection, _json_bytes(old_plan["selection"]))
        from literate_ai.adapters.project_initialization import plan_convert

        refinement = plan_convert(
            source, default_branch=default_branch, root_plan=selection
        )["root_refinement"]
        bundle = temporary / "bundle"
        manifest = stage_monorepo_components(
            source,
            selection,
            bundle,
            expected_plan_identity=refinement["plan_identity"],
            acknowledged=True,
            default_branch=default_branch,
        )
        receipts_root = temporary / "receipts"
        receipts_root.mkdir()
        receipts: dict[str, Path] = {}
        reused: list[str] = []
        executed: list[str] = []
        for name in names:
            receipt = receipts_root / f"{name}.json"
            try:
                check_component_retained_receipt(
                    source, bundle, name, old_receipts[name]
                )
            except MonorepoAdoptionError:
                run_component_retained_harness(
                    source,
                    bundle,
                    name,
                    receipt,
                    acknowledged=True,
                    worker_id=worker_id,
                    timeout_seconds=timeout_seconds,
                )
                executed.append(name)
            else:
                _write_new(receipt, _read(old_receipts[name]))
                reused.append(name)
            receipts[name] = receipt
        _project, payloads, _installation = _installation_payloads(
            source,
            bundle,
            project,
            receipts,
            expected_bundle_identity=manifest["bundle_identity"],
        )
        expected_paths = set(payloads)
        current_paths = {
            path.relative_to(project).as_posix()
            for path in _direct(project, INSTALLATION_ROOT).rglob("*")
            if path.is_file()
        } | {
            f"components/{name}/{filename}"
            for name in names
            for filename in ("binding.json", "component.md")
        }
        if current_paths != expected_paths:
            _fail(
                "installation_invalid",
                "installed Component payload inventory changed outside refresh",
            )
        try:
            refreshed = _replace_payloads(
                project,
                payloads,
                validate=lambda: check_installed_monorepo_components(project),
            )
        except MonorepoAdoptionError:
            _fail("refresh_invalid", "refreshed Component custody did not reopen")
        assert refreshed is not None
    return {**refreshed, "reused_components": reused, "executed_components": executed}
