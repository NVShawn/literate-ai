"""Explicit retained-Cargo verification and package provisioning."""

import os
import re
from contextlib import nullcontext
from pathlib import Path

from literate_ai.adapters.builders.cargo import discover_cargo_toolchain
from literate_ai.adapters.builders.make import discover_make_toolchain
from literate_ai.adapters.builders.rust import discover_rust_toolchain
from literate_ai.adapters.directory_artifacts import read_directory_export
from literate_ai.adapters.evidence_storage import HttpsEvidenceStore
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.lifecycle_lock import project_lifecycle_lock
from literate_ai.adapters.retained_cargo_admission import (
    admit_retained_cargo_consumer,
    read_retained_cargo_retirement_authority,
)
from literate_ai.adapters.retained_cargo_current import (
    compose_retained_cargo_current_inputs,
    read_retained_cargo_importer_authority,
)
from literate_ai.adapters.retained_cargo_execution_inputs import (
    read_retained_cargo_execution_inputs,
)
from literate_ai.adapters.retained_cargo_files import read_retained_cargo_files
from literate_ai.adapters.retained_cargo_import import verify_retained_cargo_archive
from literate_ai.adapters.retained_cargo_materialization import (
    materialize_retained_cargo_archive,
)
from literate_ai.adapters.retained_cargo_test_authority import (
    read_retained_cargo_test_authority,
)
from literate_ai.adapters.retained_package_tree import RetainedPackageTree
from literate_ai.adapters.retained_provider_authority import (
    read_retained_provider_authority,
)
from literate_ai.adapters.retained_provider_native import read_retained_provider_native
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.projects import PinnedInputClosure
from literate_ai.security.evidence.storage import EvidenceReadLimits

from .errors import CliFailure

_FILE_LIMIT = 16 * 1024 * 1024
_ARCHIVE_LIMIT = 64 * 1024 * 1024
_RECORD_LIMIT = 10000


def _reviewed_json(identity, size):
    selected = ContentIdentity.parse_uri(identity)
    if type(size) is not int or not 0 < size <= _FILE_LIMIT:
        raise ValueError("retained.cargo.review-size-invalid")
    return BlobRef(selected.digest, size, media_type="application/json")


def retained_cargo_from_args(args):
    """Check, provision, or explicitly admit an exact retained Cargo consumer.

    Review identities and provider selection are explicit operator inputs.
    Archive delivery uses only the explicit local file or HTTPS CAS endpoint.
    Native tool measurement may invoke installed tools, but never generated code.
    """
    try:
        return _run(args)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        # Candidate paths/content and transport details must not enter diagnostics.
        raise CliFailure(
            "retained.cargo.refused",
            "Retained Cargo inputs failed current-authority, byte-custody, or "
            "package validation; no importer admission was issued.",
        ) from exc


def _run(args):
    if args.retained_cargo_command not in {"check", "materialize", "admit"}:
        raise ValueError("retained.cargo.operation-invalid")
    if args.retained_cargo_command == "admit" and (
        not args.allow_host_execution or not args.acknowledge_source_retirement
    ):
        raise ValueError("retained.cargo.admission-authorization-required")
    if bool(args.archive) == bool(args.archive_https):
        raise ValueError("retained.cargo.archive-selection-invalid")
    if args.offline and args.archive_https:
        raise ValueError("retained.cargo.offline")
    transport = None
    token = None
    if args.archive_token_env is not None:
        if not args.archive_https or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", args.archive_token_env
        ):
            raise ValueError("retained.cargo.credentials-selection-invalid")
        token = os.environ.get(args.archive_token_env)
        if not token:
            raise ValueError("retained.cargo.credentials-required")
    if args.archive_https:
        transport = HttpsEvidenceStore(
            args.archive_https,
            bearer_token=token,
            limits=EvidenceReadLimits(_ARCHIVE_LIMIT, _ARCHIVE_LIMIT, 1),
        )
    binding_reference = _reviewed_json(args.reviewed_binding, args.binding_size)
    gate_reference = _reviewed_json(args.reviewed_gates, args.gate_size)
    root = Path(args.project).absolute()
    provider_root = Path(args.provider_project).absolute()
    archive_path = Path(args.archive).absolute() if args.archive else None
    archive_inputs = PinnedInputClosure(
        maximum_files=1,
        maximum_file_bytes=_ARCHIVE_LIMIT,
        maximum_total_bytes=_ARCHIVE_LIMIT,
    )
    limits = dict(
        max_plan_bytes=_FILE_LIMIT,
        max_archive_bytes=_ARCHIVE_LIMIT,
        max_records=_RECORD_LIMIT,
        max_package_bytes=_ARCHIVE_LIMIT,
    )
    context = (
        project_lifecycle_lock(
            root, operation="retained-cargo." + args.retained_cargo_command
        )
        if args.retained_cargo_command != "check"
        else nullcontext()
    )
    with context:
        # Check the reviewed importer files before measuring the provider's tools.
        files = read_retained_cargo_files(
            root,
            binding_path=args.binding,
            reviewed_binding=binding_reference,
            plan_path=args.plan,
            manifest_state="after",
            package_state="provision",
        )
        reviewed_identity = ContentIdentity.parse_uri(args.reviewed_binding)
        if files.binding.identity != reviewed_identity:
            raise ValueError("retained.cargo.canonical-binding-required")
        importer = read_retained_cargo_importer_authority(
            root,
            reviewed_binding_identity=reviewed_identity,
            configured_store_id=args.store_id,
            gate_plan_path=args.gates,
            reviewed_gate_plan=gate_reference,
        )
        provider = read_retained_provider_authority(
            provider_root,
            GenerationPreparationRequest(
                provider_root / args.provider_component, args.target
            ),
            provider_id=args.provider_id,
            profile_path=args.provider_profile,
            pipeline_model=args.model,
        )
        native = read_retained_provider_native(provider)

        def current():
            archive_inputs.require_unchanged()
            return compose_retained_cargo_current_inputs(importer, native)

        def archive(store, reference, maximum):
            if store != args.store_id or not 0 < reference.size <= maximum:
                raise ValueError("retained.cargo.archive-selection-invalid")
            if transport is not None:
                # The transport verifies exact bytes; qualification and package
                # admission still belong to the caller's archive verifier.
                return transport.get_bytes(reference)
            assert archive_path is not None
            content = archive_inputs.pin(
                archive_path,
                boundary=archive_path.parent,
                label="retained qualification archive",
                expected_identity=reference.identity,
            )
            if len(content) != reference.size:
                raise ValueError("retained.cargo.archive-size-mismatch")
            return content

        if args.retained_cargo_command in {"materialize", "admit"}:
            materialized = materialize_retained_cargo_archive(
                files, read_current=current, read_archive=archive, **limits
            )
            materialized.require_unchanged()
            package_count = len(materialized.packages)
            missing = 0
        else:
            _, capture = verify_retained_cargo_archive(
                files.binding,
                files.plan_content,
                read_current=current,
                read_archive=archive,
                **limits,
            )
            destinations = dict(files.binding.destinations)
            blobs = dict(capture.blobs)
            trees = []
            missing = 0
            remaining_bytes, remaining_entries = _ARCHIVE_LIMIT, _RECORD_LIMIT
            for manifest in capture.exports.graph.manifests:
                for export in manifest.exports:
                    destination = root / destinations[export.identity]
                    if destinations[export.identity] in files.absent_paths:
                        missing += 1
                        continue
                    content = read_directory_export(
                        blobs[export.blob],
                        export.blob,
                        max_bytes=remaining_bytes,
                        max_entries=remaining_entries,
                    )
                    remaining_bytes -= export.blob.size
                    remaining_entries -= len(content)
                    trees.append(RetainedPackageTree.capture(destination, content))
            files.require_unchanged()
            for tree in trees:
                tree.require_unchanged()
            package_count = len(trees)
        importer.require_unchanged()
        native.require_unchanged()
        archive_inputs.require_unchanged()
        if args.retained_cargo_command in {"materialize", "admit"}:
            materialized.require_unchanged()
        else:
            files.require_unchanged()
            for tree in trees:
                tree.require_unchanged()
        result = {
            "schema": "literate-ai/retained-cargo-provisioning@1",
            "operation": args.retained_cargo_command,
            "binding_identity": files.binding.identity.uri,
            "plan_identity": files.plan.identity.uri,
            "qualification_identity": files.binding.qualification_identity.uri,
            "verified_present_packages": package_count,
            "missing_packages": missing,
            "consumer_gates_executed": False,
            "importer_admission": False,
            "source_retirement": False,
        }
        if args.retained_cargo_command == "admit":
            test_reference = _reviewed_json(args.reviewed_tests, args.tests_size)
            retirement_reference = _reviewed_json(
                args.reviewed_retirement, args.retirement_size
            )
            test_authority = read_retained_cargo_test_authority(
                materialized,
                importer,
                inventory_path=args.tests,
                reviewed_inventory=test_reference,
            )
            retirement_authority = read_retained_cargo_retirement_authority(
                materialized,
                retirement_path=args.retirement,
                reviewed_retirement=retirement_reference,
            )
            environment = dict(os.environ)
            cargo = LocalComponentToolBinding.from_observed_toolchain(
                discover_cargo_toolchain(environment)
            )
            rustc = LocalComponentToolBinding.from_observed_toolchain(
                discover_rust_toolchain(environment)
            )
            gate_tools = {}
            for command in importer.gates.commands:
                name = command.argv[0]
                if name == "cargo":
                    tool = cargo
                elif name in {"make", "gmake"}:
                    tool = LocalComponentToolBinding.from_observed_toolchain(
                        discover_make_toolchain(environment, pinned_command=name)
                    )
                else:
                    raise ValueError("retained.cargo.gate-tool-unsupported")
                gate_tools[name] = tool
            consumer_inputs = read_retained_cargo_execution_inputs(
                materialized,
                environment=environment,
                gate_policy=importer.gates,
            )
            receipt = admit_retained_cargo_consumer(
                materialized,
                importer,
                retirement_authority=retirement_authority,
                test_authority=test_authority,
                cargo=cargo,
                rustc=rustc,
                gate_tools=gate_tools,
                environment=environment,
                consumer_inputs=consumer_inputs,
                allow_host_execution=args.allow_host_execution,
                acknowledge_source_retirement=args.acknowledge_source_retirement,
                offline=args.offline,
                timeout_seconds=args.timeout_seconds,
            )
            result.update(
                consumer_gates_executed=True,
                importer_admission=True,
                source_retirement=True,
                admission_receipt=receipt.to_dict(),
                admission_identity=receipt.identity.uri,
            )
        return result
