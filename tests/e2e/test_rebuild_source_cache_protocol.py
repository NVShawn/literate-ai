"""Typed rebuild/source-cache negotiation and post-acceptance publication."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.cache import SourceCacheResolver
from literate_ai.adapters.cache.rebuild import (
    RebuildSourceCacheProtocolError,
    RebuildSourceCacheSession,
)
from literate_ai.contracts import (
    ContentIdentity,
    ContractValidationError,
    SourceCacheMode,
    canonical_identity,
)
from literate_ai.contracts.rebuild_cache import (
    RebuildSourceCacheControl,
    RebuildSourceCacheDecisionItem,
    RebuildSourceCacheDerivationManifest,
    RebuildSourceCacheOperatorRoot,
    RebuildSourceCacheOutcome,
)
from literate_ai.projects import LoadedProject
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.support.fixtures_test_source_cache import (
    _accepted_entry,
    _cache_key,
    _configuration,
    _target,
)
from tests.support.fixtures_test_source_cache_hardening import _project


def _identity(label: str) -> ContentIdentity:
    return canonical_identity({"label": label})


def _manifest(
    *,
    project_revision_identity: ContentIdentity | None = None,
    key=None,
) -> RebuildSourceCacheDerivationManifest:
    selected_key = _cache_key() if key is None else key
    component_locks = (_identity("component-lock"),)
    plan_identity = canonical_identity(
        {
            "schema": "literate-ai/rebuild-derivation-plan@2",
            "component_lock_identities": [identity.uri for identity in component_locks],
            "cache_key_identities": [selected_key.identity.uri],
        }
    )
    return RebuildSourceCacheDerivationManifest(
        planning_request_identity=_identity("planning-request"),
        project_revision_identity=(
            _identity("project")
            if project_revision_identity is None
            else project_revision_identity
        ),
        lifecycle_driver_identity=_identity("lifecycle-driver"),
        lifecycle_plan_identity=plan_identity,
        component_lock_identities=component_locks,
        cache_keys=(selected_key,),
    )


def _accepted_entry_for_lock(
    cas: FileSystemCAS,
    *,
    suffix: str = "one",
    key=None,
    component_lock_identity: ContentIdentity | None = None,
):
    selected_lock = (
        _identity("component-lock")
        if component_lock_identity is None
        else component_lock_identity
    )
    seed = _accepted_entry(cas, suffix=suffix, key=key)
    managed_graph = replace(
        seed.managed_sbom_graph,
        resolved_graph_identity=selected_lock,
    )
    return _accepted_entry(
        cas,
        suffix=suffix,
        key=key,
        managed_graph=managed_graph,
    )


class RebuildSourceCacheContractTests(unittest.TestCase):
    def test_hit_can_skip_only_generation_and_never_claim_acceptance(self) -> None:
        key = _cache_key()
        entry = _identity("entry")
        item = RebuildSourceCacheDecisionItem(
            key,
            RebuildSourceCacheOutcome.HIT,
            candidate_identities=(entry,),
            selected_entry_identity=entry,
            source_tree_identity=_identity("tree"),
            current_acceptance_trusted=False,
            generation_skipped=True,
        )
        self.assertTrue(item.generation_skipped)
        self.assertFalse(item.current_acceptance_trusted)
        SchemaCatalog().validate(item.SCHEMA, item.to_dict())
        with self.assertRaises(ContractValidationError):
            replace(item, current_acceptance_trusted=True)

    def test_cache_hit_rejects_a_foreign_component_lock_before_materialization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cache_root = root / "cache"
            caller_cas = FileSystemCAS(root / "caller-cas")
            key = _cache_key()
            foreign = _accepted_entry_for_lock(
                caller_cas,
                key=key,
                component_lock_identity=_identity("foreign-component-lock"),
            )
            writable = _configuration(SourceCacheMode.READ_WRITE, _target())
            project = _project(root / "project")
            writable_project = LoadedProject(
                project.root,
                replace(project.definition, source_cache=writable),
            )
            SourceCacheResolver.from_configuration(
                writable,
                project=writable_project,
                operator_roots={"cache-root": cache_root},
            ).publish(foreign, caller_cas=caller_cas)
            readable = _configuration(SourceCacheMode.READ_ONLY, _target())
            project = LoadedProject(
                project.root,
                replace(project.definition, source_cache=readable),
            )
            control = RebuildSourceCacheControl(
                _identity("request"),
                _identity("project"),
                readable,
                _manifest(key=key),
                (_identity("component-lock"),),
                operator_roots=(
                    RebuildSourceCacheOperatorRoot("cache-root", str(cache_root)),
                ),
            )
            destination = root / "cached-source"

            with self.assertRaisesRegex(
                RebuildSourceCacheProtocolError, "outside the outer-owned rebuild plan"
            ):
                RebuildSourceCacheSession(control, project=project).resolve(
                    key, destination=destination
                )
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
