"""Immutable bundle and complete closure tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.artifacts import (
    ArtifactResolver,
    BundleDependency,
    BundleKind,
    BundleManifest,
    BundleStore,
)
from literate_ai.contracts import (
    ComponentCoordinate,
    ComponentRevisionRef,
    ContentIdentity,
)
from literate_ai.storage import FileSystemCAS

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def component_ref(name: str, digest: str) -> ComponentRevisionRef:
    return ComponentRevisionRef(
        ComponentCoordinate("test", name),
        "1.0.0",
        ContentIdentity.parse_uri(digest),
    )


class BundleTests(unittest.TestCase):
    def test_required_dependency_is_exact_and_closure_is_dependency_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = FileSystemCAS(Path(directory))
            store = BundleStore(cas)
            source = cas.put_bytes(b"source")
            provenance = cas.put_bytes(b"provenance")
            dependency_manifest = BundleManifest(
                kind=BundleKind.SOURCE,
                component_revision_digest=DIGEST_A,
                effective_revision_digest=DIGEST_A,
                flavor_set_digest=None,
                roots={"source": source},
                dependencies=(),
                provenance=(provenance,),
                component_ref=component_ref("dependency", DIGEST_A),
            )
            dependency_ref = store.put(dependency_manifest)
            root_manifest = BundleManifest(
                kind=BundleKind.ARTIFACT,
                component_revision_digest=DIGEST_B,
                effective_revision_digest=DIGEST_C,
                flavor_set_digest=DIGEST_A,
                roots={"artifact": source},
                dependencies=(
                    BundleDependency(
                        DIGEST_A,
                        "runtime",
                        True,
                        dependency_ref,
                        component_ref("dependency", DIGEST_A),
                    ),
                ),
                provenance=(provenance,),
                component_ref=component_ref("root", DIGEST_B),
            )
            root_ref = store.put(root_manifest)
            self.assertEqual(
                ArtifactResolver(store).closure(root_ref),
                (dependency_ref, root_ref),
            )

    def test_required_dependency_cannot_be_silently_omitted(self) -> None:
        with self.assertRaisesRegex(ValueError, "required dependency"):
            BundleDependency(DIGEST_A, "runtime", True, None)

    def test_build_bundle_requires_authorization_and_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cas = FileSystemCAS(Path(directory))
            source = cas.put_bytes(b"source")
            with self.assertRaisesRegex(ValueError, "authorization"):
                BundleManifest(
                    kind=BundleKind.BUILD,
                    component_revision_digest=DIGEST_A,
                    effective_revision_digest=DIGEST_A,
                    flavor_set_digest=None,
                    roots={"build": source},
                    dependencies=(),
                    provenance=(),
                    toolchain_digest=DIGEST_B,
                    component_ref=component_ref("build", DIGEST_A),
                )


if __name__ == "__main__":
    unittest.main()
