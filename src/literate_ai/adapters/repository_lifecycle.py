"""Plan and explicitly execute retained child commands without changing Git pins.

Execution receipts record command outcomes and product bytes, not independent
product acceptance. Pin refresh remains a separate transactional operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_lifecycle import OPERATIONS, RepositoryLifecycle
from literate_ai.contracts.repository_orchestration import RepositoryOrchestration

from ._processes import create_process_tree_ownership, terminate_process_tree
from ._write_reservations import (
    WriteReservationSet,
    WriteReservationTarget,
    acquire_write_reservations,
)
from .orchestration_planning import _unique
from .repository_lfs import hydrated_lfs_entry
from .repository_orchestration import (
    OrchestrationInventoryError,
    _git,
    inspect_gitlink_inventory,
)


def _fail(message: str) -> None:
    raise OrchestrationInventoryError("lifecycle_invalid", message)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _inside(base: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(base.resolve(strict=True)):
        _fail("lifecycle path escapes its owning directory")
    return resolved


def _target() -> str:
    system = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(
        platform.system(), "unsupported"
    )
    machine = platform.machine().lower()
    machine = {"arm64": "aarch64", "amd64": "x86_64"}.get(machine, machine)
    return f"{system}-{machine}"


def _environment_identity() -> str:
    # Bind ambient build inputs without copying credentials into a receipt.
    return canonical_identity(
        {key: value for key, value in os.environ.items() if key not in {"_", "SHLVL"}}
    ).uri


def _executor_identity() -> str:
    source = Path(__file__)
    return canonical_identity(
        {
            path.relative_to(source.parent.parent).as_posix(): _digest(path)
            for path in (
                source,
                source.with_name("repository_lfs.py"),
                source.with_name("_write_reservations.py"),
                source.parent.parent / "contracts/repository_lifecycle.py",
            )
        }
    ).uri


def _clean_child(root: Path) -> bool:
    """Recognize hydrated LFS bytes without executing global clean filters.

    Inventory Git deliberately ignores system/global configuration. A fresh
    LFS checkout can therefore appear modified when its clean filter is defined
    there. Match those bytes directly to the indexed pointer; genuine edits,
    missing objects, index changes and untracked inputs still refuse.
    """
    configuration = _git(root, "config", "--null", "--list", "--includes")
    for setting in configuration.split(b"\0"):
        key, _, value = setting.partition(b"\n")
        if (
            key.lower().startswith(b"filter.")
            and key.lower().endswith((b".clean", b".process"))
            and value
        ):
            _fail("read-only lifecycle planning refuses configured external filters")
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    for entry in status.split(b"\0"):
        if not entry:
            continue
        if not hydrated_lfs_entry(root, entry):
            return False
    return True


def plan_repository_lifecycle(
    root: Path,
    declaration: Path,
    operation: str,
    *,
    selected: tuple[str, ...] = (),
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if operation not in OPERATIONS:
        _fail("unknown lifecycle operation; Git refresh uses its separate transaction")
    raw = declaration.read_bytes()
    contract = RepositoryLifecycle.from_dict(json.loads(raw, object_pairs_hook=_unique))
    inventory = inspect_gitlink_inventory(root)
    if {child.path for child in contract.children} != {
        child.path for child in inventory.children
    }:
        _fail(
            "lifecycle declaration must account for every indexed Gitlink exactly once"
        )
    manifest = json.loads((root / "literate.project.json").read_bytes())
    authority = RepositoryOrchestration.from_dict(manifest["repository_orchestration"])
    observed = {child.path: child for child in inventory.children}
    if (
        authority.gitmodules_identity != inventory.gitmodules_identity
        or {child.path for child in authority.repositories} != set(observed)
        or any(
            asdict(child)
            != {key: getattr(observed[child.path], key) for key in asdict(child)}
            for child in authority.repositories
        )
    ):
        _fail("root authority is stale; refresh pins through the supported transaction")
    nodes = []
    for child in contract.ordered(selected):
        pin = observed[child.path]
        if pin.checked_out_commit != pin.commit:
            _fail(f"{child.path}: child must be initialized at its exact indexed pin")
        child_root = _inside(root, root / child.path)
        if not _clean_child(child_root):
            _fail(
                f"{child.path}: preserve local changes before executing a pinned build"
            )
        recipe = child.operations[operation]
        if recipe is None:
            _fail(f"{child.path}: {operation} is not implemented")
        cwd = _inside(child_root, child_root / recipe.cwd)
        if not cwd.is_dir():
            _fail(f"{child.path}: command working directory is not a directory")
        nodes.append(
            {
                "path": child.path,
                "role": child.role,
                "commit": pin.commit,
                "dependencies": list(child.dependencies),
                "recipe": asdict(recipe),
            }
        )
    plan = {
        "schema": "literate-ai/repository-lifecycle-plan@1",
        "operation": operation,
        "target": contract.target,
        "host_target": _target(),
        "declaration_identity": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "root_inputs": {
            path: _digest(_inside(root, root / path)) for path in contract.inputs
        },
        "repository_authority_identity": authority.identity,
        "executor_identity": _executor_identity(),
        "environment_identity": _environment_identity(),
        "scope": "selected" if selected else "all",
        "selected": list(selected),
        "nodes": nodes,
        "execution": False,
        "acceptance": "not-qualified",
    }
    return {**plan, "plan_identity": canonical_identity(plan).uri}


def _products(
    child: Path, outputs: list[str] | tuple[str, ...]
) -> list[dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    for output in outputs:
        path = _inside(child, child / output)
        files = [path] if path.is_file() else sorted(path.rglob("*"))
        count = 0
        for file in files:
            # Directory links are not traversed by pathlib; never claim that an
            # unobserved linked subtree is a retained product.
            if file.is_symlink() and file.is_dir():
                _fail("product directory symlinks require an explicit exported layout")
            resolved = _inside(child, file)
            if resolved.is_dir():
                continue
            if not resolved.is_file():
                _fail("product contains a non-regular file")
            name = file.relative_to(child).as_posix()
            products[name] = {
                "path": name,
                "identity": _digest(resolved),
                "size": resolved.stat().st_size,
                "mode": resolved.stat().st_mode & 0o777,
            }
            count += 1
        if not count:
            _fail(f"declared product is empty: {output}")
    return [products[key] for key in sorted(products)]


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def _expand(value: str, root: Path, child: Path) -> str:
    # Literal substitution keeps embedded Python/JSON braces untouched. No shell
    # evaluation is introduced by the executor.
    return value.replace("{root}", str(root)).replace("{child}", str(child))


def _execute(
    argv: list[str], cwd: Path, environment: dict[str, str], log: Path, timeout: int
) -> int:
    ownership = create_process_tree_ownership()
    process = None
    try:
        with log.open("xb") as stream:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                **ownership.popen_options,
            )
            ownership.bind(process.pid)
            return process.wait(timeout=timeout)
    finally:
        if process is not None:
            terminate_process_tree(process, ownership=ownership)
            process.wait(timeout=10)
        ownership.release()


def run_repository_lifecycle(
    root: Path,
    declaration: Path,
    operation: str,
    *,
    expected_plan_identity: str,
    acknowledged: bool,
    selected: tuple[str, ...] = (),
    resume: Path | None = None,
) -> dict[str, Any]:
    if not acknowledged:
        _fail("child execution requires explicit acknowledgement")
    root = root.resolve(strict=True)
    plan = plan_repository_lifecycle(root, declaration, operation, selected=selected)
    if plan["plan_identity"] != expected_plan_identity:
        _fail("reviewed lifecycle plan is stale")
    if plan["host_target"] != plan["target"]:
        _fail(f"run on {plan['target']}; this host is {plan['host_target']}")
    prior: dict[str, Any] = {}
    if resume is not None:
        prior = json.loads(resume.read_bytes())
        if prior.get("plan_identity") != expected_plan_identity:
            _fail("resume receipt does not match the exact current plan")
    output = root / "_build" / "lifecycle"
    for directory in (output.parent, output):
        if directory.is_symlink():
            _fail("lifecycle output directories cannot be symlinks")
        directory.mkdir(exist_ok=True)
        _inside(root, directory)
    anchor = output.lstat()
    target = WriteReservationTarget(
        output / "execution.lock",
        output,
        (anchor.st_dev, anchor.st_ino, anchor.st_mode),
    )
    with acquire_write_reservations((target,)) as ownership:
        return _run_reserved_lifecycle(
            root,
            declaration,
            operation,
            expected_plan_identity,
            selected,
            plan,
            prior,
            output,
            ownership,
        )


def _run_reserved_lifecycle(
    root: Path,
    declaration: Path,
    operation: str,
    expected_plan_identity: str,
    selected: tuple[str, ...],
    plan: dict[str, Any],
    prior: dict[str, Any],
    output: Path,
    ownership: WriteReservationSet,
) -> dict[str, Any]:
    run_dir = output / uuid.uuid4().hex
    run_dir.mkdir()
    receipt: dict[str, Any] = {
        "schema": "literate-ai/repository-lifecycle-receipt@1",
        "pid": os.getpid(),
        "plan_identity": expected_plan_identity,
        "operation": operation,
        "scope": plan["scope"],
        "target": plan["target"],
        "status": "running",
        "acceptance": "not-qualified",
        "nodes": [],
        "receipt": str(run_dir / "receipt.json"),
    }
    receipt_path = run_dir / "receipt.json"
    _write(receipt_path, receipt)
    previous_term = signal.getsignal(signal.SIGTERM)

    def cancelled(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancelled)
    try:
        for index, node in enumerate(plan["nodes"]):
            ownership.verify_all()
            if any(
                _digest(_inside(root, root / path)) != identity
                for path, identity in plan["root_inputs"].items()
            ):
                _fail("root recipe inputs changed during execution")
            child = root / node["path"]
            recipe = node["recipe"]
            record: dict[str, Any] = {
                "path": node["path"],
                "status": "running",
                "logs": [],
            }
            receipt["nodes"].append(record)
            previous = next(
                (
                    item
                    for item in prior.get("nodes", [])
                    if item["path"] == node["path"]
                ),
                None,
            )
            if previous and previous.get("status") in {"passed", "reused"}:
                try:
                    products = _products(child, recipe["outputs"])
                except (OSError, ValueError):
                    products = []
                if products and products == previous.get("products"):
                    record.update(status="reused", products=products)
                    _write(receipt_path, receipt)
                    continue
            # Any execution invalidates later receipt reuse, including consumers
            # that happened to complete in a different earlier partial run.
            prior = {}
            if _git(child, "rev-parse", "HEAD").decode().strip() != node[
                "commit"
            ] or not _clean_child(child):
                _fail(f"{node['path']}: child inputs changed before execution")
            print(
                f"{operation}: {node['path']} (logs: {run_dir})",
                file=sys.stderr,
                flush=True,
            )
            environment = dict(os.environ)
            environment.update(
                {
                    key: _expand(value, root, child)
                    for key, value in recipe["environment"].items()
                }
            )
            environment.update(
                LITAI_ROOT=str(root),
                LITAI_CHILD=str(child),
                LITAI_TARGET=plan["target"],
            )
            started = time.monotonic()
            for step, command in enumerate(recipe["commands"]):
                ownership.verify_all()
                log = run_dir / f"{index:03d}-{step:03d}.log"
                record["logs"].append(str(log))
                _write(receipt_path, receipt)
                try:
                    code = _execute(
                        [_expand(arg, root, child) for arg in command],
                        _inside(child, child / recipe["cwd"]),
                        environment,
                        log,
                        recipe["timeout_seconds"],
                    )
                finally:
                    record["elapsed_seconds"] = time.monotonic() - started
                ownership.verify_all()
                record["returncode"] = code
                if code:
                    record["status"] = "failed"
                    receipt["status"] = "failed"
                    break
            if record["status"] == "failed":
                break
            record.update(products=_products(child, recipe["outputs"]), status="passed")
            _write(receipt_path, receipt)
        if receipt["status"] == "running":
            ownership.verify_all()
            current = plan_repository_lifecycle(
                root, declaration, operation, selected=selected
            )
            if current["plan_identity"] != expected_plan_identity:
                _fail("lifecycle inputs changed during execution")
            receipt["status"] = "passed"
    except BaseException as exc:
        receipt["status"] = (
            "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
        )
        if receipt["nodes"]:
            receipt["nodes"][-1]["status"] = receipt["status"]
        receipt["error"] = type(exc).__name__
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        attempted = {node["path"] for node in receipt["nodes"]}
        receipt["nodes"].extend(
            {"path": node["path"], "status": "blocked"}
            for node in plan["nodes"]
            if node["path"] not in attempted
        )
        _write(receipt_path, receipt)
        print(
            f"Lifecycle {receipt['status']}; receipt: {receipt_path}",
            file=sys.stderr,
            flush=True,
        )
    return receipt
