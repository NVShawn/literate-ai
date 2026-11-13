"""Acknowledged authored execution, resource retirement and recoverable publication.

Only reference.json is a publication. Intent, stage and immutable archive bytes
are diagnostic/transport state. Recovery reads them under fresh authority and
never restores old cleanup/write rights. Receivers still need importer trust.
"""

import hashlib
import json
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.authored_publication_files import AuthoredPublicationFiles
from literate_ai.adapters.authored_qualification_capture import (
    capture_authored_qualification_products,
)
from literate_ai.adapters.authored_qualification_execution import (
    open_authored_qualification_execution,
)
from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
    _project_roots,
)
from literate_ai.adapters.authored_qualification_projection import (
    open_authored_execution_projection,
    open_current_authored_qualification_projection,
    reopen_projected_authored_qualification,
)
from literate_ai.adapters.qualification import FilesystemQualificationError
from literate_ai.adapters.qualification_archive import (
    encode_qualification_archive,
    reopen_qualification_archive,
)
from literate_ai.adapters.qualification_workspace_namespace import (
    qualification_workspace_identity,
    retire_qualification_workspace,
)
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts.authored_qualification_reference import (
    AuthoredQualificationReference,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)

METADATA_BYTES = 32 * 1024
MAX_RECORDS = 100_000
DEFAULT_BYTES = 256 * 1024 * 1024
INTENT = "literate-ai/authored-qualification-publication-intent@1"
STAGE = "literate-ai/authored-qualification-publication-stage@1"
RETIREMENT = "literate-ai/authored-qualification-retirement@1"


class AuthoredPublicationError(RuntimeError):
    def __init__(self, code, message, attempt):
        self.code, self.attempt = code, attempt
        self.message = f"{message}; attempt {attempt}: any staged state retained"
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class PublishedAuthoredQualification:
    reference: AuthoredQualificationReference
    reference_path: str
    archive_store_path: str


def _key(value):
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ValueError("qualification.authored.attempt_key_invalid")
    return value


def _base(current, attempt):
    return current.plan.output_root + "/a/" + _key(attempt)


def _blob_path(base, reference):
    return f"{base}/blobs/sha256/{reference.digest[:2]}/{reference.digest}"


def _json(files, path):
    content = files.read(path, max_bytes=METADATA_BYTES)
    value = json.loads(content)
    if not isinstance(value, dict) or canonical_json_bytes(value) != content:
        raise ValueError("qualification.authored.publication_record_invalid")
    return value


def _intent(files, current, attempt):
    data = _json(files, _base(current, attempt) + "/intent.json")
    if (
        set(data)
        != {
            "schema",
            "attempt",
            "plan_identity",
            "origin_identity",
            "scratch_root",
            "recovered_from",
        }
        or data["schema"] != INTENT
        or data["attempt"] != attempt
        or data["plan_identity"] != current.plan.identity.uri
        or data["origin_identity"] != current.plan.origin.identity.uri
        or (
            data["scratch_root"] is not None
            and not isinstance(data["scratch_root"], str)
        )
        or ((data["scratch_root"] is None) == (data["recovered_from"] is None))
    ):
        raise ValueError("qualification.authored.publication_intent_mismatch")
    if data["recovered_from"] is not None:
        _key(data["recovered_from"])
    return data


def _retired(files, current, attempt, seen=()):
    if attempt in seen or len(seen) >= 16:
        raise ValueError("qualification.authored.recovery_chain_invalid")
    intent = _intent(files, current, attempt)
    if intent["scratch_root"] is None:
        _retired(files, current, _key(intent["recovered_from"]), (*seen, attempt))
    else:
        path = Path(intent["scratch_root"])
        if (
            not path.is_absolute()
            or ".." in path.parts
            or any(path.is_relative_to(p) for p in _project_roots(current.oracles))
        ):
            raise ValueError("qualification.authored.scratch_path_invalid")
        # Only a completion emitted by the original creation owner permits
        # replay. Missing saved pathnames do not prove original retirement.
        try:
            retired = _json(files, _base(current, attempt) + "/retirement.json")
        except FileNotFoundError as error:
            raise ValueError(
                "qualification.authored.run_resources_not_retired"
            ) from error
        if (
            set(retired)
            != {"schema", "attempt", "intent_identity", "scratch_root", "identity"}
            or retired["schema"] != RETIREMENT
            or retired["attempt"] != attempt
            or retired["intent_identity"] != canonical_identity(intent).to_dict()
            or retired["scratch_root"] != str(path)
            or not isinstance(retired["identity"], list)
            or len(retired["identity"]) not in (2, 3)
            or any(type(value) is not int or value < 0 for value in retired["identity"])
        ):
            raise ValueError("qualification.authored.retirement_record_invalid")
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        else:
            # A saved path supplies no right to remove the old or replacement
            # directory, kill a process, or claim its original owner is current.
            raise ValueError("qualification.authored.run_resources_not_retired")
    return intent


def _stage(files, current, attempt, max_bytes):
    intent = _intent(files, current, attempt)
    data = _json(files, _base(current, attempt) + "/stage.json")
    if (
        set(data)
        != {
            "schema",
            "intent_identity",
            "qualification_identity",
            "products_identity",
            "archive",
        }
        or data["schema"] != STAGE
    ):
        raise ValueError("qualification.authored.publication_stage_invalid")
    if ContentIdentity.from_dict(data["intent_identity"]) != canonical_identity(intent):
        raise ValueError("qualification.authored.publication_stage_mismatch")
    for name in ("qualification_identity", "products_identity"):
        ContentIdentity.from_dict(data[name])
    archive = BlobRef.from_dict(data["archive"])
    if archive.media_type != "application/zip" or not 0 < archive.size <= max_bytes:
        raise ValueError("qualification.authored.publication_byte_limit")
    return intent, data, archive


@contextmanager
def _replay(
    files, current, attempt, stage, archive, *, max_bytes, native_sdk_inputs=None
):
    payload = files.read(
        _blob_path(_base(current, attempt), archive), max_bytes=max_bytes
    )
    reader = reopen_qualification_archive(
        payload, archive, max_bytes=max_bytes, max_records=MAX_RECORDS
    )
    with open_current_authored_qualification_projection(
        current, native_sdk_inputs=native_sdk_inputs
    ) as projection:
        reopened = reopen_projected_authored_qualification(
            projection,
            reader,
            ContentIdentity.from_dict(stage["products_identity"]),
            max_package_bytes=max_bytes,
        )
        if (
            reopened.membership.result.identity
            != ContentIdentity.from_dict(stage["qualification_identity"])
            or reopened.membership.result.plan != current.plan
            or reopened.membership.products.origin != current.plan.origin
        ):
            raise ValueError("qualification.authored.publication_result_mismatch")
        projection.require_unchanged()
        yield projection
        projection.require_unchanged()


def read_published_authored_qualification(
    current, attempt, *, expected_identity=None, max_bytes=DEFAULT_BYTES
):
    """Reopen a real committed provider reference; grant no importer permission."""
    if not isinstance(current, CurrentAuthoredQualificationPlan):
        raise TypeError("original current authored plan required")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("qualification.authored.publication_byte_limit")
    attempt = _key(attempt)
    with AuthoredPublicationFiles(current, writable=False) as files:
        base = _base(current, attempt)
        reference = AuthoredQualificationReference.from_dict(
            _json(files, base + "/reference.json")
        )
        intent, stage, archive = _stage(files, current, attempt, max_bytes)
        if (
            reference.attempt != attempt
            or reference.origin != current.plan.origin
            or reference.plan_identity != current.plan.identity
            or reference.intent_identity != canonical_identity(intent)
            or reference.stage_identity != canonical_identity(stage)
            or reference.archive != archive
            or reference.qualification_identity
            != ContentIdentity.from_dict(stage["qualification_identity"])
            or reference.products_identity
            != ContentIdentity.from_dict(stage["products_identity"])
            or (
                expected_identity is not None
                and reference.identity != expected_identity
            )
        ):
            raise ValueError("qualification.authored.publication_reference_mismatch")
        _retired(files, current, attempt)
        with _replay(files, current, attempt, stage, archive, max_bytes=max_bytes):
            files.require_current()
            return PublishedAuthoredQualification(
                reference, base + "/reference.json", base
            )


class _FreshScratch:
    """Cleanup provenance exists only for this actual exclusive creation."""

    def __init__(self, parent):
        require_safe_directory(parent)
        self.container = Path(tempfile.mkdtemp(prefix="lq-", dir=parent)).resolve(
            strict=True
        )
        self.path = self.container / "run"
        self.path.mkdir(mode=0o700)
        self.container_identity = qualification_workspace_identity(self.container)
        self.identity = qualification_workspace_identity(self.path)
        self.active = True

    def retire(self):
        if not self.active:
            raise ValueError("qualification.authored.scratch_owner_expired")
        retire_qualification_workspace(
            self.path, self.identity, self.container, self.container_identity
        )
        self.active = False
        return list(self.identity)


def apply_authored_qualification(
    current,
    selection,
    *,
    acknowledged_plan,
    allow_host_execution,
    scratch_parent=None,
    recover_attempt=None,
    max_parallelism=1,
    max_bytes=DEFAULT_BYTES,
):
    """Run or recover exactly acknowledged authored work; publish only after replay."""
    if not isinstance(current, CurrentAuthoredQualificationPlan) or not isinstance(
        selection, GenerationPreparationRequest
    ):
        raise TypeError("original current authored plan and selection required")
    if acknowledged_plan != current.plan.identity or not isinstance(
        acknowledged_plan, ContentIdentity
    ):
        raise FilesystemQualificationError(
            "qualification.authored.acknowledgement_required",
            "apply requires acknowledgement of the exact current plan",
        )
    if allow_host_execution is not True:
        raise FilesystemQualificationError(
            "qualification.authored.host_execution_required",
            "apply requires explicit permission to execute generated host code",
        )
    if (
        type(max_bytes) is not int
        or max_bytes <= 0
        or type(max_parallelism) is not int
        or not 1 <= max_parallelism <= 256
    ):
        raise ValueError("qualification.authored.publication_limits_invalid")
    current.require_unchanged()
    if recover_attempt is not None:
        recover_attempt = _key(recover_attempt)
        parent = None
    else:
        parent = Path(
            tempfile.gettempdir() if scratch_parent is None else scratch_parent
        ).absolute()
        require_safe_directory(parent)
        if any(parent.is_relative_to(p) for p in _project_roots(current.oracles)):
            raise ValueError("qualification.authored.scratch_in_authority")
    attempt, scratch = uuid.uuid4().hex, None
    with AuthoredPublicationFiles(current, writable=True) as files:
        try:
            prior = None
            if recover_attempt is not None:
                old_base = _base(current, recover_attempt)
                try:
                    _json(files, old_base + "/reference.json")
                except FileNotFoundError:
                    pass
                else:
                    return read_published_authored_qualification(
                        current, recover_attempt, max_bytes=max_bytes
                    )
                _retired(files, current, recover_attempt)
                prior = _stage(files, current, recover_attempt, max_bytes)
            base = _base(current, attempt)
            files.mkdir(base, exclusive=True)
            # This private subtree may contain original diagnostic paths and
            # verifier evidence. It is never an implicit Git contribution.
            files.write(
                current.plan.output_root + "/a/.gitignore",
                b"*\n",
                max_bytes=32,
                reuse=True,
            )
            if prior is None:
                scratch = _FreshScratch(parent)
                if any(
                    scratch.path.is_relative_to(p)
                    for p in _project_roots(current.oracles)
                ):
                    raise ValueError("qualification.authored.scratch_in_authority")
            intent = {
                "schema": INTENT,
                "attempt": attempt,
                "plan_identity": current.plan.identity.uri,
                "origin_identity": current.plan.origin.identity.uri,
                "scratch_root": None if scratch is None else str(scratch.path),
                "recovered_from": recover_attempt,
            }
            files.write(
                base + "/intent.json",
                canonical_json_bytes(intent),
                max_bytes=METADATA_BYTES,
            )
            native_sdk_inputs = None
            if prior is None:
                with open_authored_qualification_execution(
                    current,
                    selection,
                    acknowledged_plan=acknowledged_plan,
                    allow_host_execution=allow_host_execution,
                    scratch_root=scratch.path,
                    max_parallelism=max_parallelism,
                    maximum_capture_bytes=max_bytes,
                ) as owner:
                    capture = capture_authored_qualification_products(owner)
                    with open_authored_execution_projection(owner) as projection:
                        reader = reopen_qualification_archive(
                            archive_bytes := encode_qualification_archive(
                                capture.records,
                                max_bytes=max_bytes,
                                max_records=MAX_RECORDS,
                            ),
                            archive := BlobRef(
                                hashlib.sha256(archive_bytes).hexdigest(),
                                len(archive_bytes),
                                media_type="application/zip",
                            ),
                            max_bytes=max_bytes,
                            max_records=MAX_RECORDS,
                        )
                        checked = reopen_projected_authored_qualification(
                            projection,
                            reader,
                            capture.products.identity,
                            max_package_bytes=max_bytes,
                        )
                        if (
                            checked.membership.result != owner.result
                            or checked.membership.products != capture.products
                        ):
                            raise ValueError(
                                "qualification.authored.publication_result_mismatch"
                            )
                    qualification_id, products_id = (
                        owner.result.identity,
                        capture.products.identity,
                    )
                    native_sdk_inputs = owner._lifecycle.prepared.native_sdk_inputs
                    owner.require_unchanged()
                    files.write(
                        _blob_path(base, archive),
                        archive_bytes,
                        max_bytes=max_bytes,
                        reuse=True,
                    )
                    stage = {
                        "schema": STAGE,
                        "intent_identity": canonical_identity(intent).to_dict(),
                        "qualification_identity": qualification_id.to_dict(),
                        "products_identity": products_id.to_dict(),
                        "archive": archive.to_dict(),
                    }
                    files.write(
                        base + "/stage.json",
                        canonical_json_bytes(stage),
                        max_bytes=METADATA_BYTES,
                    )
                current.require_unchanged()
                retired_identity = scratch.retire()
                files.write(
                    base + "/retirement.json",
                    canonical_json_bytes(
                        {
                            "schema": RETIREMENT,
                            "attempt": attempt,
                            "intent_identity": canonical_identity(intent).to_dict(),
                            "scratch_root": str(scratch.path),
                            "identity": retired_identity,
                        }
                    ),
                    max_bytes=METADATA_BYTES,
                )
            else:
                _, old_stage, archive = prior
                archive_bytes = files.read(
                    _blob_path(old_base, archive), max_bytes=max_bytes
                )
                files.write(
                    _blob_path(base, archive),
                    archive_bytes,
                    max_bytes=max_bytes,
                    reuse=True,
                )
                stage = {
                    **old_stage,
                    "intent_identity": canonical_identity(intent).to_dict(),
                }
                files.write(
                    base + "/stage.json",
                    canonical_json_bytes(stage),
                    max_bytes=METADATA_BYTES,
                )
                qualification_id = ContentIdentity.from_dict(
                    stage["qualification_identity"]
                )
                products_id = ContentIdentity.from_dict(stage["products_identity"])
            _retired(files, current, attempt)
            with _replay(
                files,
                current,
                attempt,
                stage,
                archive,
                max_bytes=max_bytes,
                native_sdk_inputs=native_sdk_inputs,
            ) as projection:
                reference = AuthoredQualificationReference(
                    current.plan.origin,
                    current.plan.identity,
                    qualification_id,
                    products_id,
                    canonical_identity(intent),
                    canonical_identity(stage),
                    archive,
                    attempt,
                )
                files.require_current()
                files.write(
                    base + "/reference.json",
                    canonical_json_bytes(reference.to_dict()),
                    max_bytes=METADATA_BYTES,
                    before_link=projection.require_unchanged,
                )
                files.require_current()
                return PublishedAuthoredQualification(
                    reference, base + "/reference.json", base
                )
        except (OSError, ValueError, RuntimeError) as exc:
            raise AuthoredPublicationError(
                getattr(exc, "code", "qualification.authored.publication_failed"),
                str(exc),
                attempt,
            ) from exc
