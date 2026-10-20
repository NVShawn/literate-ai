"""Public retained-project planning, delegated to typed framework owners."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.adapters.retained_project_planning import (
    plan_retained_project_with_graph,
)
from literate_ai.contracts.retained_project import RetainedProjectManifest
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectProfile

from .errors import CliFailure
from .execution_workers import (
    admit_execution_worker_health,
    dispatch_with_worker_health_poll,
    select_execution_worker,
)


def retained_project_requested(args: Any) -> bool:
    """Keep explicit, even empty, retained selectors out of generated execution."""
    return bool(getattr(args, "retained_project_plan", False)) or any(
        getattr(args, key, None) is not None
        for key in (
            "retained_project",
            "retained_project_profile",
            "authorize_retained_project",
        )
    )


def human_retained_project_text(result: dict[str, Any], *, command: str) -> str:
    """Distinguish a read-only plan from live retained admission and publication."""
    admitted = result.get("admitted") is True
    status = "admitted" if admitted else "planned (read-only)"
    identity = result.get("plan_identity") or result.get("authorization_identity")
    lines = [f"Literate AI {command}: retained project {status}", f"Plan: {identity}"]
    if admitted:
        lines.append(f"Retained receipt: {result.get('retained_receipt_identity')}")
        lines.append(f"Custody: {result.get('custody_root')}")
        lines.append("Package and release qualification remain separate.")
    else:
        lines.append("Review and authorize this exact plan before execution.")
    return "\n".join(lines) + "\n"


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Retained authority contains duplicate JSON keys")
        result[key] = value
    return result


def _document(path: Path) -> tuple[bytes, object]:
    if path.absolute() != path.resolve(strict=True) or path_is_link_or_reparse(path):
        raise ValueError("Retained authority may not use redirected paths")
    with path.open("rb") as stream:
        content = stream.read(64 * 1024 * 1024 + 1)
    if len(content) > 64 * 1024 * 1024:
        raise ValueError("Retained authority document exceeds metadata bound")
    return content, json.loads(content, object_pairs_hook=_unique_object)


def retained_project_from_args(
    project: Any, binding: Any, args: Any
) -> dict[str, object]:
    try:
        if not args.retained_project_profile:
            raise ValueError(
                "Retained-project mode requires an explicit locked profile"
            )
        if (
            getattr(args, "retained_source", None)
            or getattr(args, "from_accepted_source", False)
            or args.force_regeneration
            or getattr(args, "update_receipt", False)
        ):
            raise ValueError(
                "Retained authority cannot replace existing source/receipt modes"
            )
        overrides = [
            "--" + key.replace("_", "-")
            for key in ("flavor", "model", "jobs", "source_cache_entry")
            if getattr(args, key, None) is not None
            and (key in {"model", "jobs"} or getattr(args, key))
        ]
        if overrides:
            raise ValueError(
                "Retained-project execution uses its locked profile; unsupported "
                "overrides: " + ", ".join(overrides)
            )
        if args.specification != ".":
            raise ValueError("Retained-project admission requires the complete project")
        profile_path = project.root / args.retained_project_profile

        def current_authority():
            _manifest_bytes, manifest_data = _document(
                project.root / args.retained_project
            )
            profile_bytes, profile_data = _document(profile_path)
            manifest = RetainedProjectManifest.from_dict(manifest_data)
            profile = RetainedProjectProfile.from_dict(profile_data)
            worker = select_execution_worker(
                args, project_root=project.root, target_profile=args.target
            )
            planning = plan_retained_project_with_graph(
                project,
                binding,
                manifest,
                profile,
                worker_identity=worker.worker.identity,
                target=args.target,
                profile_path=profile_path,
                profile_bytes=profile_bytes,
            )
            return manifest, worker, planning

        manifest, worker, planning = current_authority()
        plan = planning.plan
        if args.retained_project_plan:
            return {"authority_mode": "retained-project", **planning.to_dict()}
        plan.require_authorization(args.authorize_retained_project)
        from literate_ai.adapters.component_lock_application import (
            current_project_component_locks,
        )
        from literate_ai.adapters.retained_project_runtime import (
            execute_local_retained_project,
        )
        from literate_ai.cache_directories import (
            CacheDirectoryError,
            select_cache_directories,
        )
        from literate_ai.storage.cas import StorageError

        health = admit_execution_worker_health(args, worker, plan.identity)
        try:
            directories = select_cache_directories(
                project.root,
                build_dir=getattr(args, "build_dir", None),
                obj_dir=getattr(args, "obj_dir", None),
            )
            (admission, workspace), polls = dispatch_with_worker_health_poll(
                args,
                worker,
                plan.identity,
                lambda: execute_local_retained_project(
                    project.root,
                    manifest,
                    planning,
                    component_locks=current_project_component_locks(project),
                    worker=worker.worker,
                    current_plan=lambda: current_authority()[2].plan,
                    test_receipt_policy=project.definition.test_receipt_policy,
                    cache_directories=directories,
                    authorization=args.authorize_retained_project,
                    allow_host_execution=args.allow_host_execution,
                    operator_environment=dict(os.environ),
                ),
            )
        except (
            ValueError,
            TypeError,
            OSError,
            CacheDirectoryError,
            StorageError,
        ) as exc:
            raise CliFailure("retained_project.execution_failed", str(exc)) from exc
        return {
            "authority_mode": "retained-project",
            "plan_identity": plan.identity.uri,
            "admitted": True,
            "retained_receipt": admission.receipt.to_dict(),
            "retained_receipt_identity": admission.receipt.identity.uri,
            "custody_root": str(admission.store.root),
            "workspace": str(workspace.root),
            "source_authority": "original",
            "release_authority": "original-source",
            "conversion_advanced": False,
            "package_qualified": False,
            "release_authorized": False,
            "worker_health": health,
            "worker_health_polls": list(polls),
        }
    except CliFailure:
        raise
    except (ValueError, TypeError, OSError) as exc:
        raise CliFailure("retained_project.admission_invalid", str(exc)) from exc
