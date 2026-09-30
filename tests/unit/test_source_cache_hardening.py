"""Adversarial filesystem and resource-bound checks for accepted-source caches."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.cache import (
    FileSystemSourceCache,
    SourceCacheError,
    SourceCacheMaterializer,
    SourceCacheResolver,
)
from literate_ai.contracts import (
    ProjectDefinition,
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheRootKind,
    SourceCacheTarget,
)
from literate_ai.projects import LoadedProject
from literate_ai.storage import FileSystemCAS
from tests.unit.test_source_cache import (
    _accepted_entry,
    _accepted_entry_with_attachment,
    _cache_key,
    _configuration,
    _source_intelligence_policy,
    _target,
)


def _project(root: Path) -> LoadedProject:
    return LoadedProject(
        root,
        ProjectDefinition(
            project_id="cache-hardening",
            version="1.0.0",
            profile="canonical",
            agent_skill="SKILL.md",
            component_roots=("components",),
            flavor_roots=("flavors",),
            skill_roots=("skills",),
            workflow_roots=("workflows",),
            routing_roots=(),
            documentation_roots=("docs",),
            source_intelligence=_source_intelligence_policy(),
        ),
    )


class _ReplacingFailingVerifier:
    def __init__(self, displaced: Path) -> None:
        self.displaced = displaced

    def finalize(self, source_root, files):
        source_root.replace(self.displaced)
        source_root.mkdir()
        (source_root / "replacement-sentinel").write_text(
            "preserve replacement", encoding="utf-8"
        )
        raise RuntimeError("injected failure after destination replacement")


class ReadOnlyAndPathSafetyTests(unittest.TestCase):
    def test_missing_read_only_target_is_a_non_mutating_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cache_root = root / "missing-cache"
            configuration = SourceCacheConfiguration(
                SourceCacheMode.READ_ONLY,
                (
                    SourceCacheTarget(
                        "read",
                        SourceCacheRootKind.OPERATOR_BOUND,
                        "cache-root",
                    ),
                ),
            )

            resolver = SourceCacheResolver.from_configuration(
                configuration,
                operator_roots={"cache-root": cache_root},
            )

            self.assertEqual(resolver.resolve(_cache_key()), ())
            self.assertFalse(cache_root.exists())

    def test_format_only_read_only_target_is_a_non_mutating_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cache_root = root / "committed-cache"
            cache_root.mkdir()
            format_path = cache_root / "format.json"
            format_path.write_bytes(
                b'{"format":"filesystem-v2","schema":'
                b'"literate-ai/source-cache-layout@2"}'
            )
            original = format_path.read_bytes()

            cache = FileSystemSourceCache("committed", cache_root, writable=False)

            self.assertFalse(cache.available)
            self.assertIsNone(cache.cas)
            self.assertEqual(cache.candidates(_cache_key()), ())
            self.assertEqual(tuple(cache_root.iterdir()), (format_path,))
            self.assertEqual(format_path.read_bytes(), original)

    def test_partial_read_only_target_names_the_missing_managed_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cache_root = root / "partial-cache"
            (cache_root / "entries" / "sha256").mkdir(parents=True)
            (cache_root / "format.json").write_bytes(
                b'{"format":"filesystem-v2","schema":'
                b'"literate-ai/source-cache-layout@2"}'
            )

            with self.assertRaises(SourceCacheError) as captured:
                FileSystemSourceCache("partial", cache_root, writable=False)

            self.assertEqual(captured.exception.code, "source-cache.path-unsafe")
            self.assertIn(
                str(cache_root / "keys" / "sha256"),
                captured.exception.message,
            )

    def test_existing_read_only_target_does_not_recreate_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            caller_cas = FileSystemCAS(root / "caller")
            entry = _accepted_entry(caller_cas)
            cache_root = root / "cache"
            writable = FileSystemSourceCache("local", cache_root)
            writable.publish(entry, caller_cas=caller_cas)
            writable.staging.rmdir()

            read_only = FileSystemSourceCache("local", cache_root, writable=False)

            self.assertEqual(read_only.candidates(entry.derivation.cache_key), (entry,))
            self.assertFalse(read_only.staging.exists())
            with self.assertRaises(SourceCacheError) as captured:
                read_only.publish(entry, caller_cas=caller_cas)
            self.assertEqual(captured.exception.code, "source-cache.write-disabled")

    def test_read_only_target_allows_absent_empty_intelligence_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            caller_cas = FileSystemCAS(root / "caller")
            entry = _accepted_entry(caller_cas)
            cache_root = root / "cache"
            writable = FileSystemSourceCache("local", cache_root)
            writable.publish(entry, caller_cas=caller_cas)
            writable.intelligence.rmdir()
            writable.intelligence.parent.rmdir()
            writable.intelligence_by_source.rmdir()
            writable.intelligence_by_source.parent.rmdir()

            read_only = FileSystemSourceCache("local", cache_root, writable=False)

            self.assertTrue(read_only.available)
            self.assertEqual(read_only.candidates(entry.derivation.cache_key), (entry,))
            self.assertEqual(
                read_only.intelligence_attachments(
                    entry.derivation.source_tree_identity
                ),
                (),
            )
            self.assertFalse(read_only.intelligence.parent.exists())
            self.assertFalse(read_only.intelligence_by_source.parent.exists())

    def test_casefold_alias_cannot_overlap_project_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "docs").mkdir()
            configuration = SourceCacheConfiguration(
                SourceCacheMode.READ_WRITE,
                (
                    SourceCacheTarget(
                        "project",
                        SourceCacheRootKind.PROJECT_RELATIVE,
                        "DOCS/cache",
                    ),
                ),
                write_target_id="project",
            )

            with self.assertRaises(SourceCacheError) as captured:
                SourceCacheResolver.from_configuration(
                    configuration, project=_project(root)
                )

            self.assertEqual(captured.exception.code, "source-cache.authority-overlap")
            self.assertFalse((root / "docs" / "cache").exists())

    def test_casefold_alias_targets_are_the_same_portable_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            targets = (
                _target("first", binding="first-root"),
                _target("second", binding="second-root"),
            )
            configuration = _configuration(SourceCacheMode.READ_ONLY, *targets)

            with self.assertRaises(SourceCacheError) as captured:
                SourceCacheResolver.from_configuration(
                    configuration,
                    operator_roots={
                        "first-root": root / "Cache",
                        "second-root": root / "cache",
                    },
                )

            self.assertEqual(captured.exception.code, "source-cache.target-overlap")


class ResourceAndParserSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.caller_cas = FileSystemCAS(self.root / "caller")
        self.entry = _accepted_entry(self.caller_cas)
        self.store = FileSystemSourceCache("local", self.root / "cache")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_provider_discovery_requires_valid_key_membership(self) -> None:
        self.store.publish(self.entry, caller_cas=self.caller_cas)
        self.assertEqual(self.store.verified_published_entries(), (self.entry,))
        membership = (
            self.store.keys
            / self.entry.derivation.cache_key.accepted_source_lookup_identity.digest
            / f"{self.entry.identity.digest}.json"
        )
        membership.unlink()

        with self.assertRaises(SourceCacheError) as captured:
            self.store.verified_published_entries()

        self.assertEqual(captured.exception.code, "source-cache.membership-invalid")

    def test_candidate_count_is_bounded_before_bulk_verification(self) -> None:
        other = _accepted_entry(
            self.caller_cas,
            suffix="two",
            key=self.entry.derivation.cache_key,
        )
        self.store.publish(self.entry, caller_cas=self.caller_cas)
        self.store.publish(other, caller_cas=self.caller_cas)

        with (
            patch("literate_ai.adapters.cache.filesystem._MAX_CACHE_CANDIDATES", 1),
            self.assertRaises(SourceCacheError) as captured,
        ):
            self.store.candidates(self.entry.derivation.cache_key)

        self.assertEqual(captured.exception.code, "source-cache.candidate-limit")

    def test_source_and_intelligence_sizes_are_bounded_before_reads(self) -> None:
        with (
            patch("literate_ai.adapters.cache.filesystem._MAX_SOURCE_FILE_BYTES", 1),
            self.assertRaises(SourceCacheError) as source_error,
        ):
            self.store.publish(self.entry, caller_cas=self.caller_cas)
        self.assertEqual(source_error.exception.code, "source-cache.source-limit")

        entry, attachment = _accepted_entry_with_attachment(
            self.caller_cas,
            suffix="bounded-intelligence",
        )
        with (
            patch(
                "literate_ai.adapters.cache.filesystem."
                "_MAX_SOURCE_INTELLIGENCE_ARTIFACT_BYTES",
                1,
            ),
            self.assertRaises(SourceCacheError) as database_error,
        ):
            self.store.publish(
                entry,
                caller_cas=self.caller_cas,
                intelligence_attachments=(attachment,),
            )
        self.assertEqual(
            database_error.exception.code, "source-cache.intelligence-limit"
        )

    def test_deep_json_is_normalized_to_a_stable_cache_error(self) -> None:
        self.store.publish(self.entry, caller_cas=self.caller_cas)
        membership = (
            self.store.keys
            / self.entry.derivation.cache_key.accepted_source_lookup_identity.digest
            / f"{self.entry.identity.digest}.json"
        )
        depth = 1500
        membership.write_text(
            '{"value":' + "[" * depth + "0" + "]" * depth + "}",
            encoding="utf-8",
        )

        with self.assertRaises(SourceCacheError) as captured:
            self.store.candidates(self.entry.derivation.cache_key)

        self.assertEqual(captured.exception.code, "source-cache.membership-invalid")


class DescriptorAndMaterializationRaceTests(unittest.TestCase):
    def test_cas_returns_verified_bytes_from_the_opened_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cas = FileSystemCAS(Path(temporary) / "cas")
            reference = cas.put_bytes(b"original")
            target = cas.path_for(reference)
            displaced = target.with_name(target.name + ".displaced")
            real_open = os.open
            swapped = False
            replacement_blocked = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal replacement_blocked, swapped
                descriptor = real_open(path, flags, *args, **kwargs)
                if Path(path) == target and not swapped:
                    swapped = True
                    try:
                        target.replace(displaced)
                    except PermissionError:
                        # Windows opens without delete sharing, so the platform
                        # itself prevents replacement while this handle is live.
                        replacement_blocked = True
                    else:
                        target.write_bytes(b"tampered")
                return descriptor

            with patch("literate_ai.storage.cas.os.open", side_effect=racing_open):
                content = cas.get_bytes(reference)

            self.assertEqual(content, b"original")
            self.assertTrue(swapped)
            if replacement_blocked:
                self.assertEqual(target.read_bytes(), b"original")
                self.assertFalse(displaced.exists())
            else:
                self.assertEqual(target.read_bytes(), b"tampered")

    def test_source_only_materialization_is_explicit_and_hit_is_untrusted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            caller_cas = FileSystemCAS(root / "caller")
            entry = _accepted_entry(caller_cas)
            store = FileSystemSourceCache("local", root / "cache")
            store.publish(entry, caller_cas=caller_cas)
            candidate = SourceCacheResolver(
                _configuration(SourceCacheMode.READ_ONLY, _target()),
                {"local": store},
            ).resolve(entry.derivation.cache_key)[0]

            materialized = SourceCacheMaterializer().materialize(
                candidate, root / "materialized"
            )

            self.assertIsNone(materialized.intelligence)
            self.assertFalse((root / "materialized" / ".codegraph").exists())
            self.assertFalse(candidate.current_acceptance_trusted)
            self.assertFalse(materialized.current_acceptance_trusted)

    def test_cleanup_preserves_a_replacement_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            caller_cas = FileSystemCAS(root / "caller")
            entry = _accepted_entry(caller_cas)
            store = FileSystemSourceCache("local", root / "cache")
            store.publish(entry, caller_cas=caller_cas)
            candidate = SourceCacheResolver(
                _configuration(SourceCacheMode.READ_ONLY, _target()),
                {"local": store},
            ).resolve(entry.derivation.cache_key)[0]
            destination = root / "materialized"
            displaced = root / "original-materialization"

            with self.assertRaises(SourceCacheError) as captured:
                SourceCacheMaterializer().materialize(
                    candidate,
                    destination,
                    intelligence_verifier=_ReplacingFailingVerifier(displaced),
                )

            self.assertEqual(
                captured.exception.code, "source-cache.final-index-invalid"
            )
            self.assertEqual(
                (destination / "replacement-sentinel").read_text(encoding="utf-8"),
                "preserve replacement",
            )
            self.assertTrue(displaced.is_dir())


if __name__ == "__main__":
    unittest.main()
