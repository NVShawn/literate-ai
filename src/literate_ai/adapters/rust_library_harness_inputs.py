"""Current verifier files and complete provisioned roots, without execution rights.

The independent-acceptance boundary must still join these facts to its real locked
library, generated harness stage, native tool owners and command policy. Neither
this capture nor a decoded declaration admits an artifact or runs Cargo.
"""

from __future__ import annotations

import json
import os
import weakref
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from literate_ai.adapters.retained_cargo_input_owners_v2 import (
    RetainedCargoInputRootV2,
    capture_retained_cargo_input_owners_v2,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.rust_library_harness import (
    MAX_LOCKFILE_BYTES,
    MAX_ORACLE_BYTES,
    RustLibraryHarnessDependencies,
)
from literate_ai.projects import PinnedInputClosure

_ISSUER = object()
_ISSUED = {}


def _unique_object(rows):
    result = dict(rows)
    if len(result) != len(rows):
        raise ValueError("rust.harness.duplicate-authority-field")
    return result


def _nonfinite(value):
    raise ValueError("rust.harness.nonfinite-authority-value")


def _current_issuance(scope):
    issued = _ISSUED.get(id(scope))
    if (
        type(scope) is not RustHarnessVerifierInputs
        or issued is None
        or issued[0]() is not scope
    ):
        raise ValueError("rust.harness.verifier-inputs-unavailable")
    issued[1]()
    if _ISSUED.get(id(scope)) is not issued or issued[0]() is not scope:
        raise ValueError("rust.harness.verifier-inputs-unavailable")
    return issued[2:]


class RustHarnessVerifierInputs:
    """One process-local, finite-lifetime capture; never a serialized grant."""

    __slots__ = ("__weakref__",)

    def __init__(self, issuer):
        if issuer is not _ISSUER:
            raise TypeError("current verifier capture required")

    def __setattr__(self, name, value):
        raise TypeError("live verifier inputs are immutable")

    def __delattr__(self, name):
        raise TypeError("live verifier inputs are immutable")

    def __copy__(self):
        raise TypeError("live verifier inputs cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("live verifier inputs cannot be copied")

    def __reduce_ex__(self, protocol):
        raise TypeError("live verifier inputs cannot be serialized")

    def __getstate__(self):
        raise TypeError("live verifier inputs cannot be serialized")

    def require_current(self):
        _current_issuance(self)

    @property
    def identity(self):
        return ContentIdentity.parse_uri(_current_issuance(self)[0])

    @property
    def lockfile(self):
        return _current_issuance(self)[1]

    @property
    def roots(self):
        """Current captured coordinates, never a reconstructed source owner."""
        return _current_issuance(self)[2]


@contextmanager
def capture_rust_harness_verifier_inputs(
    oracle_path: Path,
    *,
    oracle_content: bytes,
    harness_name: str,
    harness_content: bytes,
    harness_identity: ContentIdentity,
    declaration: RustLibraryHarnessDependencies,
):
    """Reopen current authority and every declared source/configuration byte.

    The existing oracle loader supplies the exact bytes it consumed, never a path
    from a candidate manifest. Roots are verifier-relative and no output/scratch
    exclusions are accepted. In particular CARGO_HOME is captured completely;
    bookkeeping drift refuses instead of silently omitting cache metadata.
    """
    if (
        type(declaration) is not RustLibraryHarnessDependencies
        or type(harness_identity) is not ContentIdentity
        or not isinstance(oracle_path, Path)
        or not oracle_path.is_absolute()
        or type(oracle_content) is not bytes
        or not 0 < len(oracle_content) <= MAX_ORACLE_BYTES
        or type(harness_content) is not bytes
        or not 0 < len(harness_content) <= MAX_ORACLE_BYTES
        or not isinstance(harness_name, str)
        or not harness_name
        or PurePosixPath(harness_name).name != harness_name
        or "\\" in harness_name
    ):
        raise ValueError("rust.harness.verifier-authority-invalid")
    document = json.loads(
        oracle_content,
        object_pairs_hook=_unique_object,
        parse_constant=_nonfinite,
    )
    if (
        not isinstance(document, dict)
        or document.get("schema") != "literate-ai/library-acceptance-oracle@2"
        or document.get("rust_harness") != declaration.to_dict()
        or document.get("harness") != harness_name
        or document.get("harness_identity") != harness_identity.uri
    ):
        raise ValueError("rust.harness.verifier-declaration-mismatch")
    root = oracle_path.parent
    authority = PinnedInputClosure(
        maximum_files=2,
        maximum_file_bytes=MAX_ORACLE_BYTES,
        maximum_total_bytes=2 * MAX_ORACLE_BYTES,
    )
    authority.pin(
        oracle_path, boundary=root, label="oracle", expected_content=oracle_content
    )
    authority.pin(
        root / harness_name,
        boundary=root,
        label="harness",
        expected_content=harness_content,
        expected_identity=harness_identity,
    )
    lock = PinnedInputClosure(
        maximum_files=1,
        maximum_file_bytes=MAX_LOCKFILE_BYTES,
        maximum_total_bytes=MAX_LOCKFILE_BYTES,
    )
    lockfile = lock.pin(
        root / declaration.lockfile.file,
        boundary=root,
        label="Cargo.lock",
        expected_identity=declaration.lockfile.identity,
    )
    if len(lockfile) != declaration.lockfile.size:
        raise ValueError("rust.harness.lockfile-size-changed")
    owners = capture_retained_cargo_input_owners_v2(
        tuple(
            RetainedCargoInputRootV2(scope, root.joinpath(*PurePosixPath(path).parts))
            for scope, path in declaration.inputs.roots
        ),
        policy=declaration.inputs.custody,
        maximum_metadata_bytes=declaration.inputs.maximum_metadata_bytes,
    )
    if owners.identity != declaration.inputs.owners_identity:
        raise ValueError("rust.harness.provisioned-inputs-changed")
    plan_identity = canonical_identity(declaration.to_dict())
    identity = canonical_identity(
        {
            "schema": "literate-ai/rust-library-verifier-inputs@1",
            "declaration_identity": plan_identity.uri,
            "authority_identity": authority.identity,
            "lockfile_authority_identity": lock.identity,
            "input_owners_identity": owners.identity.uri,
        }
    ).uri
    active, pid = True, os.getpid()

    def guard():
        def require_lifetime():
            if (
                not active
                or os.getpid() != pid
                or canonical_identity(declaration.to_dict()) != plan_identity
            ):
                raise ValueError("rust.harness.verifier-inputs-unavailable")

        require_lifetime()
        authority.require_unchanged()
        lock.require_unchanged()
        owners.require_unchanged()
        require_lifetime()

    scope = RustHarnessVerifierInputs(_ISSUER)
    key = id(scope)

    def retire(reference):
        issued = _ISSUED.get(key)
        if issued is not None and issued[0] is reference:
            _ISSUED.pop(key, None)

    issuance = (
        weakref.ref(scope, retire),
        guard,
        identity,
        lockfile,
        tuple((name, tree.root) for name, tree in owners.trees),
    )
    _ISSUED[key] = issuance
    try:
        scope.require_current()
        yield scope
        scope.require_current()
    finally:
        active = False
        if _ISSUED.get(key) is issuance:
            _ISSUED.pop(key, None)
