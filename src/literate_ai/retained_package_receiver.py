"""One-shot retained PACKAGE receiver selected by private operator registration.

The registration pins the exact public execution authorization. No project path,
command, tool selection or execution grant is accepted from the action wire.
Native attempt ownership is acquired before launching the execution child.
"""

import argparse
import copy
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_WIRE_BYTES,
    decode_attested_action_request,
    decode_attested_action_response,
    record_identity,
)
from literate_ai.adapters.action_retained_unit_package_worker import (
    ConfiguredRetainedUnitPackageWorker,
    retained_unit_package_input,
    retained_unit_verification_input,
)
from literate_ai.adapters.freebsd_owned_attempt import (
    FreeBsdOwnedAttemptError,
    OwnedFreeBsdChild,
    run_owned_freebsd_attempt,
)
from literate_ai.adapters.locked_generation_authority import (
    read_only_component_catalog_observations,
)
from literate_ai.adapters.retained_receiver_attempt import (
    reserve_retained_receiver_attempt,
)
from literate_ai.adapters.retained_unit_verification import (
    RetainedUnitVerificationSelection,
)
from literate_ai.cli.retained_project import _document
from literate_ai.cli.retained_unit_execution import (
    _prepare_retained_unit_execution,
    execute_retained_unit_package_from_args,
)
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.paths import canonical_relative_posix_path
from literate_ai.projects import read_only_project_configuration_parses

CONFIG_SCHEMA = "literate-ai/retained-unit-package-receiver@1"
VERIFICATION_CONFIG_SCHEMA = "literate-ai/retained-unit-package-receiver@2"
_FIELDS = {
    "schema",
    "project",
    "worker_config",
    "worker",
    "target",
    "retained_project",
    "retained_project_profile",
    "policy",
    "tools",
    "authorization_identity",
    "build_dir",
    "obj_dir",
    "child_environment",
}


def _direct_path(value):
    if not isinstance(value, str):
        raise ValueError("retained.receiver.private-absolute-path-required")
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("retained.receiver.private-absolute-path-required")
    return path


class _Registration:
    def __init__(self, path):
        self.path = _direct_path(str(path))
        raw, value = _document(self.path, maximum_bytes=65536)
        if type(value) is not dict or not (
            set(value) == _FIELDS
            and value["schema"] == CONFIG_SCHEMA
            or set(value) == _FIELDS | {"verification"}
            and value["schema"] == VERIFICATION_CONFIG_SCHEMA
        ):
            raise ValueError("retained.receiver.private-registration-invalid")
        self.identity = record_identity(raw)
        self.project = _direct_path(value["project"])
        self.worker_config = _direct_path(value["worker_config"])
        self.build = _direct_path(value["build_dir"])
        self.obj = _direct_path(value["obj_dir"])
        for path in (self.project, self.build, self.obj):
            require_safe_directory(path)
            if self.path.is_relative_to(path):
                raise ValueError("retained.receiver.registration-overlaps-execution")
        for name in ("retained_project", "retained_project_profile", "policy", "tools"):
            canonical_relative_posix_path(
                value[name], label="private retained selection"
            )
        if any(
            not isinstance(value[name], str) or not 1 <= len(value[name]) <= 4096
            for name in ("worker", "target")
        ):
            raise ValueError("retained.receiver.private-worker-required")
        self.authorization = ContentIdentity.parse_uri(value["authorization_identity"])
        environment = value["child_environment"]
        if (
            type(environment) is not dict
            or len(environment) > 500
            or any(
                not isinstance(key, str)
                or not key
                or "=" in key
                or "\x00" in key
                or not isinstance(item, str)
                or "\x00" in item
                for key, item in environment.items()
            )
            or sum(len(k.encode()) + len(v.encode()) for k, v in environment.items())
            > 60000
        ):
            raise ValueError("retained.receiver.bounded-private-environment-required")
        self.environment = dict(environment)
        self.verification_selection = None
        self.source_arguments = []
        self.source_token_env = None
        if value["schema"] == VERIFICATION_CONFIG_SCHEMA:
            verification = value["verification"]
            if type(verification) is not dict or set(verification) != {
                "selection",
                "source",
            }:
                raise ValueError("retained.receiver.private-verification-invalid")
            self.verification_selection = RetainedUnitVerificationSelection.from_dict(
                verification["selection"]
            )
            source = verification["source"]
            if source is not None:
                if (
                    type(source) is not dict
                    or set(source) != {"endpoint", "bearer_token_env", "allow_http"}
                    or not isinstance(source["endpoint"], str)
                    or not 1 <= len(source["endpoint"]) <= 4096
                    or type(source["allow_http"]) is not bool
                ):
                    raise ValueError("retained.receiver.private-source-invalid")
                self.source_arguments = ["--source-cas-url", source["endpoint"]]
                name = source["bearer_token_env"]
                if name is not None:
                    if (
                        not isinstance(name, str)
                        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,255}", name) is None
                        or not self.environment.get(name)
                    ):
                        raise ValueError(
                            "retained.receiver.private-source-credential-required"
                        )
                    self.source_token_env = name
                    self.source_arguments += ["--source-token-env", name]
                if source["allow_http"]:
                    self.source_arguments.append("--allow-http")
        self.cas = self.build / "retained-project-cas"
        require_safe_directory(self.cas)
        self.args = SimpleNamespace(
            project=str(self.project),
            worker_config=str(self.worker_config),
            worker=value["worker"],
            target=value["target"],
            worker_param=[],
            retained_project=value["retained_project"],
            retained_project_profile=value["retained_project_profile"],
            policy=value["policy"],
            tools=value["tools"],
            build_dir=str(self.build),
            obj_dir=str(self.obj),
            retained_unit_command="execution-plan"
            if self.verification_selection is None
            else "verify-plan",
            specification=".",
            force_regeneration=False,
            authorize_retained_project=None,
            worker_health_config=None,
        )

    def require_unchanged(self):
        raw, _value = _document(self.path, maximum_bytes=65536)
        if record_identity(raw) != self.identity:
            raise ValueError("retained.receiver.registration-changed")


def _read_registered_return_blob(config, content, output):
    """Deliver assertions under private byte custody, without execution admission."""
    from literate_ai.adapters.execution_dispatch import MAX_CATALOG_BYTES
    from literate_ai.cli.execution_workers import select_execution_worker
    from literate_ai.contracts.execution_dispatch import ExecutionWorkerCatalog
    from literate_ai.return_cas import MAX_RETURN_BLOB_BYTES, read_return_blob

    raw, document = _document(config.worker_config, maximum_bytes=MAX_CATALOG_BYTES)
    catalog = ExecutionWorkerCatalog.from_dict(document)
    selected = select_execution_worker(
        config.args, project_root=config.project, target_profile=config.args.target
    )
    if (
        not selected.worker.is_remote
        or selected.configuration != config.worker_config
        or selected.catalog_identity != catalog.identity
    ):
        raise ValueError("retained.receiver.exact-return-worker-required")
    catalog_bytes_identity = record_identity(raw)
    require_safe_directory(config.cas)
    node = config.cas.stat()
    cas_node = (node.st_dev, node.st_ino)

    def current():
        config.require_unchanged()
        fresh, _ = _document(config.worker_config, maximum_bytes=MAX_CATALOG_BYTES)
        if record_identity(fresh) != catalog_bytes_identity:
            raise ValueError("retained.receiver.return-catalog-changed")
        require_safe_directory(config.cas)
        observed = config.cas.stat()
        if (observed.st_dev, observed.st_ino) != cas_node:
            raise ValueError("retained.receiver.return-custody-changed")

    current()
    read_return_blob(
        content,
        cas_root=config.cas,
        worker_identity=selected.worker.identity,
        registration_identity=config.identity,
        maximum_bytes=MAX_RETURN_BLOB_BYTES,
        registration_guard=current,
        output=output,
    )
    current()


def _configured_worker(config, *, owned_child=None):
    def preview_current():
        return execute_retained_unit_package_from_args(
            config.args, verification_selection=config.verification_selection
        )

    prepared = _prepare_retained_unit_execution(
        config.args, verification_selection=config.verification_selection
    )
    preview = prepared.preview()
    if preview["authorization_identity"] != config.authorization.uri:
        raise ValueError("retained.receiver.exact-private-authorization-required")
    selected, planning = prepared.selected, prepared.planning
    if (
        not selected.worker.is_remote
        or selected.worker.identity != planning.plan.profile.worker_identity
        or planning.plan.identity.uri != preview["request"]["retained_plan_identity"]
    ):
        raise ValueError("retained.receiver.original-ssh-plan-required")

    def registered_current():
        config.require_unchanged()
        if owned_child is not None:
            owned_child.require_current()
            if (
                config.source_token_env is not None
                and os.environ.get(config.source_token_env)
                != config.environment[config.source_token_env]
            ):
                raise ValueError("retained.receiver.source-credential-changed")

    def current():
        registered_current()
        fresh, code = preview_current()
        if code != 0 or fresh != preview:
            raise ValueError("retained.receiver.authorized-intent-changed")

    def run(context):
        if owned_child is None:
            raise ValueError("retained.receiver.native-owned-child-required")
        current()
        args = copy.copy(config.args)
        args.retained_unit_command = (
            "execute" if config.verification_selection is None else "verify"
        )
        args.authorize_retained_project = planning.plan.identity.uri
        args.authorize_unit_packages = config.authorization.uri
        args.allow_host_execution = True
        result, code = execute_retained_unit_package_from_args(
            args,
            receiver_context=context,
            verification_selection=config.verification_selection,
        )
        if code != 0 or Path(result["custody_root"]) != config.cas:
            raise ValueError("retained.receiver.private-custody-differs")
        current()
        return result

    # Startup uses one canonical preparation and its actual planning objects.
    # Publication and execution still call the full currentness guard; close
    # this constructor with fresh private registration and child custody.
    registered_current()
    return ConfiguredRetainedUnitPackageWorker(
        selected.worker,
        selected.catalog_identity,
        planning.plan.identity,
        config.identity,
        config.authorization,
        (
            retained_unit_package_input(preview["request"])
            if config.verification_selection is None
            else retained_unit_verification_input(preview["request"])
        ),
        run,
        current,
        poll_guard=registered_current,
        verification_selection=config.verification_selection,
    )


def _receive(config, mode, *, owned_child=None):
    from literate_ai.action_worker import main as receive

    if config.source_token_env is not None:
        os.environ[config.source_token_env] = config.environment[
            config.source_token_env
        ]
    worker = _configured_worker(config, owned_child=owned_child)
    # The private worker identity overrides ambient SSH environment assertions.
    os.environ["LITAI_ACTION_WORKER_IDENTITY"] = worker.worker.identity.uri
    return receive(
        [
            "--cas",
            str(config.cas),
            "--workspace",
            str(config.obj),
            *config.source_arguments,
            *mode,
        ],
        package_worker=worker,
    )


def main(argv=None):
    with (
        read_only_component_catalog_observations(),
        read_only_project_configuration_parses(),
    ):
        return _main(argv)


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--config-identity")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--describe", action="store_true")
    modes.add_argument("--describe-hardware", action="store_true")
    modes.add_argument("--read-return-blob", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not sys.platform.startswith("freebsd"):
            raise ValueError("retained.receiver.native-freebsd-required")
        config = _Registration(args.config)
        if args.child:
            if (
                args.config_identity != config.identity.uri
                or args.describe
                or args.describe_hardware
                or args.read_return_blob
            ):
                raise ValueError("retained.receiver.private-child-binding-differs")
            return _receive(config, [], owned_child=OwnedFreeBsdChild())
        if args.config_identity is not None:
            raise ValueError("retained.receiver.child-only-config-identity")
        if args.describe or args.describe_hardware:
            return _receive(
                config, ["--describe" if args.describe else "--describe-hardware"]
            )
        if args.read_return_blob:
            from literate_ai.return_cas import MAX_READ_REQUEST_BYTES

            _read_registered_return_blob(
                config,
                sys.stdin.buffer.read(MAX_READ_REQUEST_BYTES + 1),
                sys.stdout.buffer,
            )
            return 0
        content = sys.stdin.buffer.read(MAX_ACTION_WIRE_BYTES + 1)
        request, deadline, _records, capability = decode_attested_action_request(
            content
        )
        worker = _configured_worker(config)
        if (
            request.worker.worker_identity != worker.worker.identity
            or capability is None
        ):
            raise ValueError("retained.receiver.attested-private-worker-required")
        worker.admit_request(
            request, deadline, _records, worker.worker.identity, capability
        )
        with reserve_retained_receiver_attempt(
            config.path,
            request,
            config.identity,
            deadline,
            preflight=worker.require_current,
        ) as attempt:

            def current_attempt():
                attempt.require_current()
                config.require_unchanged()

            # Full intent admission brackets execution. The child independently
            # checks live authority at its execution boundaries; the parent's
            # frequent supervision polls retain the attempt and registration
            # fences without repeatedly reconstructing the entire public plan.
            try:
                result = run_owned_freebsd_attempt(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-m",
                        "literate_ai.retained_package_receiver",
                        "--config",
                        str(config.path),
                        "--child",
                        "--config-identity",
                        config.identity.uri,
                    ],
                    cwd=config.project,
                    staging_root=config.obj,
                    environment=config.environment,
                    expires_at=deadline.expires_at,
                    admission_guard=current_attempt,
                    input_bytes=content,
                    stdout_limit_bytes=16 * 1024**2,
                    stderr_limit_bytes=65536,
                )
            except FreeBsdOwnedAttemptError as exc:
                try:
                    attempt.retain_diagnostic(exc)
                finally:
                    # A failed diagnostic write cannot erase genuine native
                    # empty-subtree proof or strand a proven-clean reservation.
                    if exc.cleanup_verified:
                        if exc.guardian_pid is None:
                            attempt.release_before_native_start(exc)
                        else:
                            attempt.release_after_native_empty()
                raise
            else:
                try:
                    attempt.retain_completion(result)
                finally:
                    attempt.release_after_native_empty()
        if result.returncode != 0:
            raise ValueError("retained.receiver.execution-child-failed")
        # Only an actually empty native subtree permits forwarding a response.
        # Controller transport loss still means unknown remote ownership there.
        _outcome, _record, measured = decode_attested_action_response(
            result.stdout, request
        )
        if measured != capability:
            raise ValueError("retained.receiver.unattested-child-result")
        worker.require_current()
        deadline.remaining()
        sys.stdout.buffer.write(result.stdout)
        return 0
    except (ValueError, TypeError, OSError, RuntimeError):
        print("Retained PACKAGE receiver refused", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
