"""Owned provider artifact staging for one currently authorized worker BUILD."""

import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.action_build_record import validate_build_provider_receipts
from literate_ai.adapters.action_build_result import _remove_owned_stage
from literate_ai.adapters.action_provider_build import read_provider_build
from literate_ai.adapters.action_provider_record import validate_provider_transfers
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.retained_package_tree import write_staged_package
from literate_ai.contracts import ContentIdentity


@contextmanager
def materialize_build_providers(
    *, admitted, ports, cas, deadline, require_current, blob_source=None
):
    """Verify BUILD provider custody before exposing paths to the consumer."""
    with materialize_provider_artifacts(
        receipts=admitted.accepted_providers,
        transfers=admitted.provider_builds,
        execution_plan=admitted.execution_plan,
        providers=admitted.inputs.providers,
        ports=ports,
        cas=cas,
        deadline=deadline,
        require_current=require_current,
        blob_source=blob_source,
    ):
        yield


@contextmanager
def materialize_provider_artifacts(
    *,
    receipts,
    transfers,
    execution_plan,
    providers,
    ports,
    cas,
    deadline,
    require_current,
    blob_source=None,
):
    """Verify complete provider custody before exposing paths to the consumer."""
    require_current()
    validate_build_provider_receipts(providers, receipts)
    validate_provider_transfers(receipts, transfers)
    if not receipts:
        yield
        require_current()
        return
    generations = {
        item.component_revision: item for item in execution_plan.generation_plans
    }
    entries = []
    with ExitStack() as cleanup:
        for receipt, transfer in zip(receipts, transfers, strict=True):
            files, reader = read_provider_build(
                transfer=transfer,
                receipt=receipt,
                cas=cas,
                deadline=deadline,
                require_current=require_current,
                generation_plan=generations[receipt.component_revision],
                blob_source=blob_source,
            )
            require_current()
            require_safe_directory(ports.object_root)
            stage = Path(tempfile.mkdtemp(prefix="provider-", dir=ports.object_root))
            cleanup.callback(_remove_owned_stage, stage, directory_node(stage))
            custody = write_staged_package(stage, files)
            for reference in transfer.evidence_records:
                require_current()
                identity = ContentIdentity.parse_uri(reference.identity)
                ports.retain_evidence_record(identity, reader.read_bytes(identity))
            entries.append((receipt, reader, custody))
        with ports._provider_artifact_custody(
            providers, tuple(entries), require_current
        ):
            yield
