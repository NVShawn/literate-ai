"""Worker health inspection and exact-authority cleanup boundaries."""

import json
import shutil
import time
from pathlib import Path

from literate_ai.adapters.user_assets import resolve_worker_config_path
from literate_ai.adapters.worker_alerts import WorkerAlertHistoryError
from literate_ai.adapters.worker_cleanup import investigate_worker_cleanup
from literate_ai.adapters.worker_health import (
    inspect_worker_storage,
    load_worker_health_inputs,
)
from literate_ai.adapters.worker_storage import _unique_object
from literate_ai.application.worker_cleanup_execution import (
    CleanupAuthorization,
    create_cleanup_proposal,
    create_remote_cleanup_proposal,
    execute_authorized_cleanup,
)
from literate_ai.contracts import ContentIdentity, ExecutionWorkerKind
from literate_ai.contracts._validation import (
    contract_fields,
    int_value,
    list_value,
    string_value,
)
from literate_ai.projects import ProjectError

from .errors import CliFailure


def _cleanup_inputs(args):
    return load_worker_health_inputs(
        args.health_config,
        resolve_worker_config_path(explicit=args.worker_config),
        worker_id=args.worker_id,
    )


def _public_proposal(proposal):
    return {
        "schema": "literate-ai/worker-cleanup-proposal@1",
        "worker_id": proposal.worker_id,
        "policy_identity": proposal.policy_identity.uri,
        "proposal_identity": proposal.identity.uri,
        "proposal_created_at_ms": proposal.created_at_ms,
        "operation": "configured-cleanup-tool",
        "targets": [item.candidate.to_dict() for item in proposal.targets],
        "deletion_authorized": False,
    }


def worker_cleanup_from_args(args):
    try:
        inputs = _cleanup_inputs(args)
        if inputs.cleanup is None:
            raise ValueError("worker.cleanup_not_configured")
        if args.worker_cleanup_command == "plan":
            arguments = {
                "worker_id": inputs.bindings.worker.worker_id,
                "policy_identity": inputs.policy.identity,
                "created_at_ms": time.time_ns() // 1_000_000,
            }
            if inputs.bindings.worker.kind is ExecutionWorkerKind.LOCAL:
                proposal = create_cleanup_proposal(inputs.cleanup, **arguments)
            else:
                investigation = investigate_worker_cleanup(
                    inputs.bindings, inputs.cleanup
                )
                proposal = create_remote_cleanup_proposal(
                    inputs.cleanup, investigation, **arguments
                )
            inputs.custody.require_unchanged()
            return _public_proposal(proposal), 0
        if inputs.bindings.worker.kind is not ExecutionWorkerKind.LOCAL:
            raise ValueError("worker.cleanup_remote_tool_unsupported")
        raw = Path(args.authorization).read_bytes()
        if len(raw) > 64 * 1024:
            raise ValueError("worker.cleanup_authorization_oversized")
        data = contract_fields(
            json.loads(raw, object_pairs_hook=_unique_object),
            path="WorkerCleanupAuthorization",
            schema_uri="literate-ai/worker-cleanup-authorization@1",
            required=frozenset(
                {
                    "worker_id",
                    "policy_identity",
                    "proposal_identity",
                    "proposal_created_at_ms",
                    "target_ids",
                    "operation",
                    "expires_at_ms",
                }
            ),
        )
        authorization = CleanupAuthorization(
            string_value(data["worker_id"], "WorkerCleanupAuthorization.worker_id"),
            ContentIdentity.parse_uri(data["policy_identity"]),
            ContentIdentity.parse_uri(data["proposal_identity"]),
            int_value(
                data["proposal_created_at_ms"],
                "WorkerCleanupAuthorization.proposal_created_at_ms",
            ),
            tuple(
                string_value(item, "WorkerCleanupAuthorization.target_ids")
                for item in list_value(
                    data["target_ids"], "WorkerCleanupAuthorization.target_ids"
                )
            ),
            string_value(data["operation"], "WorkerCleanupAuthorization.operation"),
            int_value(
                data["expires_at_ms"], "WorkerCleanupAuthorization.expires_at_ms"
            ),
        )
        proposal = create_cleanup_proposal(
            inputs.cleanup,
            worker_id=inputs.bindings.worker.worker_id,
            policy_identity=inputs.policy.identity,
            created_at_ms=authorization.proposal_created_at_ms,
        )
        roots = {root.alias: root for root in inputs.cleanup.roots}

        def measure(alias):
            return shutil.disk_usage(roots[alias].path).free

        receipts = execute_authorized_cleanup(
            proposal,
            authorization,
            now_ms=time.time_ns() // 1_000_000,
            measure_available=measure,
        )
        inputs.custody.require_unchanged()
        return {
            "schema": "literate-ai/worker-cleanup-result@1",
            "worker_id": proposal.worker_id,
            "proposal_identity": proposal.identity.uri,
            "receipts": [item.to_dict() for item in receipts],
        }, 0
    except (OSError, TypeError, ValueError, RecursionError, ProjectError) as exc:
        raise CliFailure(
            "worker.cleanup_invalid",
            "cleanup inputs, proposal, authorization, or exact targets are invalid",
        ) from exc


def worker_health_from_args(args):
    try:
        job = (
            None
            if args.job_identity is None
            else ContentIdentity.parse_uri(args.job_identity)
        )
        inputs = load_worker_health_inputs(
            args.health_config,
            resolve_worker_config_path(explicit=args.worker_config),
            worker_id=args.worker_id,
        )
        return inspect_worker_storage(
            inputs, job_identity=job, alert_state_path=args.alert_state
        )
    except WorkerAlertHistoryError as exc:
        raise CliFailure("worker.health_alert_state_invalid", str(exc)) from exc
    except (ValueError, TypeError, RecursionError, OSError, ProjectError) as exc:
        # Private paths, arbitrary JSON keys and provider errors are not alerts.
        raise CliFailure(
            "worker.health_input_invalid",
            "Worker health inputs are invalid, unavailable or changed; "
            "check the private worker and health configuration.",
        ) from exc


def human_worker_health(result):
    assessment = result["assessment"]
    lines = [
        f"Worker {assessment['worker_id']}: storage {assessment['health']}; "
        f"{assessment['decision']}."
    ]
    investigation = result["cleanup_investigation"]
    pressure = result["pressure"]

    def append_pressure():
        if pressure["status"] == "not-configured":
            lines.append("Pressure inspection: not-configured.")
            return
        lines.append(f"Pressure inspection: {pressure['status']}.")
        for finding in pressure["findings"]:
            lines.append(
                f"  {finding['resource']}: {finding['health']} "
                f"({finding['reason']}); measured={finding['measured']} "
                f"threshold={finding['threshold']}."
            )

    def append_cleanup_investigation():
        status = investigation["status"]
        if status in {"not-required", "not-configured"}:
            lines.append(f"Cleanup investigation: {status}.")
            return
        lines.append(
            f"Cleanup investigation: {status}; "
            f"{len(investigation['candidates'])} candidate(s), "
            f"{investigation['scanned_entries']} entries measured, "
            f"{investigation['skipped_links']} links skipped."
        )
        for candidate in investigation["candidates"]:
            lines.append(
                f"  {candidate['candidate_id']} ({candidate['root']}): "
                f"{candidate['bytes']} bytes, {candidate['active_use']}; "
                f"{candidate['uncertainty']}. Recovery: {candidate['recovery']}."
            )
        lines.append("Candidate discovery never authorizes deletion.")

    if "events" in result:
        for event in result["events"]:
            lines.append(
                f"{event['role']}: {event['resource']} {event['transition']} "
                f"to {event['health']} ({', '.join(event['reasons'])}); "
                f"job decision: {event['impact']}. {event['next_action']}"
            )
            for finding in event["findings"]:
                if (
                    finding.get("available") is not None
                    and finding["health"] == event["health"]
                ):
                    lines.append(
                        f"  available={finding['available']} "
                        f"required={finding['minimum']} "
                        f"deficit={finding['deficit']}"
                    )
                elif (
                    finding.get("measured") is not None
                    and finding["health"] == event["health"]
                ):
                    lines.append(
                        f"  measured={finding['measured']} "
                        f"threshold={finding['threshold']}"
                    )
        history = result["alert_history"]
        if history["reset_reason"]:
            lines.append(f"Alert history reset: {history['reset_reason']}.")
        if not result["events"]:
            lines.append("No new alert transitions.")
        append_pressure()
        append_cleanup_investigation()
        lines.append("Storage inspection only; no dispatch or cleanup is authorized.")
        return "\n".join(lines) + "\n"
    for alert in result["alerts"]:
        metrics = ""
        if alert.get("available") is not None:
            metrics = (
                f" available={alert['available']} required={alert['minimum']}"
                f" deficit={alert['deficit']}"
            )
        elif alert.get("measured") is not None:
            metrics = f" measured={alert['measured']} threshold={alert['threshold']}"
        lines.append(
            f"{', '.join(alert['roles'])}: {alert['resource']} {alert['health']} "
            f"({alert['reason']}){metrics}. {alert['next_action']}"
        )
    append_pressure()
    append_cleanup_investigation()
    lines.append(
        "Storage inspection only; this does not authorize dispatch or cleanup."
    )
    return "\n".join(lines) + "\n"
