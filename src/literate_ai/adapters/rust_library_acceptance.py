"""Current opt-in Rust oracle values and original-instance verifier custody.

The versioned public dispatcher selects this loader only for an explicit v2
oracle. Values and historical records cannot issue independent acceptance: the
execution boundary enters the capture below for the exact originally loaded
instance, then joins its actual locked library, native tools and bounded Cargo.
"""

from __future__ import annotations

import json
import os
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai.adapters.component_acceptance import (
    ComponentAcceptanceError,
    LibraryAcceptance,
    load_library_acceptance,
)
from literate_ai.adapters.rust_library_harness_inputs import (
    capture_rust_harness_verifier_inputs,
)
from literate_ai.contracts.product_json import product_json_bytes
from literate_ai.contracts.rust_library_harness import (
    MAX_ORACLE_BYTES,
    RustLibraryHarnessDependencies,
)
from literate_ai.projects import PinnedInputClosure

RUST_LIBRARY_SCHEMA = "literate-ai/library-acceptance-oracle@2"
_LIBRARY_FIELDS = frozenset(
    {
        "schema",
        "component",
        "specification_set_identity",
        "public_interface_identities",
        "import_surface_identity",
        "language",
        "harness",
        "harness_identity",
        "cases",
    }
)
_LOADED = {}


def _refuse():
    raise ComponentAcceptanceError(
        "component_acceptance.rust_verifier_authority_unavailable",
        "Rust dependency acceptance requires the exact current loaded oracle",
    )


def _pairs(rows):
    value = dict(rows)
    if len(value) != len(rows):
        raise ValueError("duplicate Rust library oracle field")
    return value


def _nonfinite(_value):
    raise ValueError("Rust library oracle must contain finite product JSON")


@dataclass(frozen=True, slots=True, weakref_slot=True)
class RustLibraryAcceptance(LibraryAcceptance):
    """A typed v2 assertion; construction alone creates no live permission."""

    rust_harness: RustLibraryHarnessDependencies

    def __post_init__(self):
        LibraryAcceptance.__post_init__(self)
        if (
            self.language != "rust"
            or type(self.rust_harness) is not RustLibraryHarnessDependencies
        ):
            _refuse()

    def identity_document(self):
        document = LibraryAcceptance.identity_document(self)
        document["schema"] = RUST_LIBRARY_SCHEMA
        document["rust_harness"] = self.rust_harness.to_dict()
        return document


class _LoadedOracle:
    __slots__ = ("original", "path", "raw", "harness_name", "authority", "identity")

    def __init__(self, original, path, raw, harness_name, authority):
        key = id(original)

        def retire(reference):
            saved = _LOADED.get(key)
            if saved is not None and saved.original is reference:
                _LOADED.pop(key, None)

        self.original = weakref.ref(original, retire)
        self.path = path
        self.raw = raw
        self.harness_name = harness_name
        self.authority = authority
        self.identity = original.identity

    def require_original(self, oracle):
        if self.original() is not oracle or oracle.identity != self.identity:
            _refuse()
        self.authority.require_unchanged()


def load_rust_library_acceptance(path: Path, component_name: str, *, read_bytes=None):
    """Load exact current v2 files; do not upgrade or reinterpret a v1 oracle.

    The unchanged v1 reader validates the shared semantic fields through an
    explicit in-memory projection. The v2 authority remains the original raw
    document pinned here, never that projection or a caller-provided reader.
    """
    path = Path(os.path.abspath(path))
    authority = PinnedInputClosure(
        maximum_files=2,
        maximum_file_bytes=MAX_ORACLE_BYTES,
        maximum_total_bytes=2 * MAX_ORACLE_BYTES,
    )
    raw = authority.pin(path, boundary=path.parent, label="Rust library oracle")
    if read_bytes is not None and read_bytes(path) != raw:
        _refuse()
    try:
        document = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_nonfinite)
        if (
            type(document) is not dict
            or set(document) != _LIBRARY_FIELDS | {"rust_harness"}
            or document["schema"] != RUST_LIBRARY_SCHEMA
            or document["language"] != "rust"
        ):
            _refuse()
        declaration = RustLibraryHarnessDependencies.from_dict(document["rust_harness"])
        harness_name = document["harness"]
        if (
            type(harness_name) is not str
            or not harness_name
            or PurePosixPath(harness_name).name != harness_name
            or "\\" in harness_name
        ):
            _refuse()
        shared = {
            key: value for key, value in document.items() if key != "rust_harness"
        }
        shared["schema"] = "literate-ai/library-acceptance-oracle@1"
        projected = product_json_bytes(shared)
        harness_path = path.parent / harness_name

        def read_shared(selected):
            if selected == path:
                return projected
            if selected != harness_path:
                _refuse()
            content = authority.pin(
                harness_path, boundary=path.parent, label="Rust library harness"
            )
            if read_bytes is not None and read_bytes(harness_path) != content:
                _refuse()
            return content

        base = load_library_acceptance(path, component_name, read_bytes=read_shared)
        result = RustLibraryAcceptance(
            base.component,
            base.specification_set_identity,
            base.public_interface_identities,
            base.import_surface_identity,
            base.language,
            base.harness_identity,
            base.harness_content,
            base.cases,
            declaration,
        )
        authority.require_unchanged()
        witness = _LoadedOracle(result, path, raw, harness_name, authority)
        _LOADED[id(result)] = witness
        witness.require_original(result)
        return result
    except ComponentAcceptanceError:
        raise
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        UnicodeError,
        RecursionError,
    ) as exc:
        raise ComponentAcceptanceError(
            "component_acceptance.rust_oracle_invalid",
            "Rust library oracle or its exact current inputs are invalid",
        ) from exc


@contextmanager
def capture_loaded_rust_library_verifier_inputs(oracle):
    """Reopen original current files and complete provisioned roots before use.

    Neither dataclass replacement, an archive decoder nor a candidate-constructed
    assertion appears in the private original-instance registry. This context
    still grants no command execution, artifact admission, or SDK authority.
    """
    if type(oracle) is not RustLibraryAcceptance:
        _refuse()
    witness = _LOADED.get(id(oracle))
    if witness is None:
        _refuse()
    witness.require_original(oracle)
    with capture_rust_harness_verifier_inputs(
        witness.path,
        oracle_content=witness.raw,
        harness_name=witness.harness_name,
        harness_content=oracle.harness_content,
        harness_identity=oracle.harness_identity,
        declaration=oracle.rust_harness,
    ) as scope:
        witness.require_original(oracle)
        yield scope
        witness.require_original(oracle)
