"""Canonical preferred-provider resolution and lifecycle identity binding tests."""

from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.cli.component_locks import component_lock_from_args
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.component_locking import ComponentLock
from literate_ai.contracts.executable_components import ComponentGenerationKey
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.provider_resolution import (
    PROVIDER_RESOLVER_POLICY_IDENTITY,
    ProviderCapabilitySet,
    ProviderOverrideDeclaration,
    ProviderResolution,
    ProviderResolutionDeclaration,
    ProviderResolutionError,
    ProviderResolutionRequest,
    resolve_provider,
    resolve_provider_declaration,
)
from tests.unit.test_component_execution_planning import _diamond_lock
from tests.unit.test_component_lock_planning import _fixture


def capability_set(provider_id: str, *capabilities: str) -> ProviderCapabilitySet:
    return ProviderCapabilitySet(provider_id, tuple(sorted(capabilities)))


def request(
    *required: str,
    override: str | None = None,
) -> ProviderResolutionRequest:
    declaration = (
        None
        if override is None
        else canonical_identity({"flavor": "physics", "override": override})
    )
    provenance = (
        None
        if override is None
        else canonical_identity({"flavor_revision": "physics-1.0.0"})
    )
    return ProviderResolutionRequest(
        preferred_provider="newton",
        required_capabilities=tuple(sorted(required)),
        fallback_order=("physx", "bullet"),
        override_provider=override,
        override_declaration_identity=declaration,
        override_provenance_identity=provenance,
    )


class ProviderResolutionTests(unittest.TestCase):
    def test_authored_component_and_flavor_resolution_reaches_lock_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            manifest = component / "component.md"
            declaration = """
provider_resolutions:
  - resolution_id: physics
    preferred_provider: newton
    required_capabilities:
      - rigid-body
      - soft-body
    fallback_order:
      - physx
    capability_sets:
      - provider_id: newton
        capabilities:
          - rigid-body
      - provider_id: physx
        capabilities:
          - rigid-body
          - soft-body
"""
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "source_dependencies: []\n",
                    declaration + "source_dependencies: []\n",
                ),
                encoding="utf-8",
                newline="\n",
            )

            def lock() -> dict[str, object]:
                report, status = component_lock_from_args(
                    Namespace(
                        component=str(component),
                        target="macos-host",
                        flavor=["+macos", "+python"],
                        flavor_root=[str(flavors)],
                        check=False,
                        diff=False,
                    )
                )
                self.assertEqual(status, 0)
                return report

            fallback = lock()
            self.assertEqual(
                fallback["provider_resolutions"][0]["selected_provider"], "physx"
            )
            fallback_identity = fallback["component_lock_identity"]

            flavor_manifest = flavors / "lang-python" / "flavor.md"
            flavor, body = parse_authoring_markdown(
                flavor_manifest.read_bytes(), source=flavor_manifest.as_posix()
            )
            flavor["provider_overrides"] = [
                {"resolution_id": "physics", "provider_id": "physx"}
            ]
            flavor_manifest.write_bytes(render_authoring_markdown(flavor, body))
            overridden = lock()
            override_report = overridden["provider_resolutions"][0]
            self.assertEqual(override_report["selected_provider"], "physx")
            self.assertIsNotNone(override_report["override_declaration_identity"])
            self.assertIsNotNone(override_report["override_provenance_identity"])
            self.assertNotEqual(
                overridden["component_lock_identity"], fallback_identity
            )

            flavor.pop("provider_overrides")
            flavor_manifest.write_bytes(render_authoring_markdown(flavor, body))
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "      - provider_id: newton\n"
                    "        capabilities:\n"
                    "          - rigid-body\n",
                    "      - provider_id: newton\n"
                    "        capabilities:\n"
                    "          - rigid-body\n"
                    "          - soft-body\n",
                ),
                encoding="utf-8",
                newline="\n",
            )
            preferred = lock()
            self.assertEqual(
                preferred["provider_resolutions"][0]["selected_provider"], "newton"
            )

    def test_declaration_api_binds_named_flavor_override(self) -> None:
        declaration = ProviderResolutionDeclaration(
            resolution_id="physics",
            preferred_provider="newton",
            required_capabilities=("rigid-body",),
            fallback_order=("physx",),
            capability_sets=(
                capability_set("newton", "rigid-body"),
                capability_set("physx", "rigid-body"),
            ),
        )
        override = ProviderOverrideDeclaration("physics", "physx")
        provenance = canonical_identity({"flavor_revision": "physx-1.0.0"})

        resolved = resolve_provider_declaration(
            declaration,
            override,
            override_provenance_identity=provenance,
        )

        self.assertEqual(resolved.request.resolution_id, "physics")
        self.assertEqual(resolved.selected_provider, "physx")
        self.assertEqual(
            ProviderResolutionDeclaration.from_dict(declaration.to_dict()),
            declaration,
        )
        self.assertEqual(
            ProviderOverrideDeclaration.from_dict(override.to_dict()), override
        )

    def test_legacy_lock_and_plan_records_migrate_to_no_resolutions(self) -> None:
        lock = _diamond_lock()
        legacy_lock = lock.to_dict()
        legacy_lock.pop("provider_resolutions")
        migrated_lock = ComponentLock.from_dict(legacy_lock, authorings=lock.authorings)
        self.assertEqual(migrated_lock.provider_resolutions, ())

        model_identities = {
            node.revision.identity.uri: canonical_identity(
                {"model-for": node.revision.identity.uri}
            )
            for node in lock.nodes
        }
        key = (
            plan_component_execution(lock, model_identities=model_identities)
            .generation_plans[0]
            .generation_key
        )
        legacy_key = key.to_dict()
        legacy_key.pop("provider_resolution_identities")
        self.assertEqual(
            ComponentGenerationKey.from_dict(legacy_key).provider_resolution_identities,
            (),
        )

    def test_preferred_provider_wins_when_sufficient(self) -> None:
        resolution = resolve_provider(
            request("rigid-body", "soft-body"),
            (
                capability_set("newton", "rigid-body", "soft-body"),
                capability_set("physx", "rigid-body", "soft-body"),
                capability_set("bullet", "rigid-body", "soft-body"),
            ),
        )

        self.assertEqual(resolution.selected_provider, "newton")
        self.assertIsNone(resolution.fallback_reason)
        self.assertEqual(
            resolution.resolver_policy_identity,
            PROVIDER_RESOLVER_POLICY_IDENTITY,
        )
        self.assertEqual(len(resolution.evaluated_capability_set_identities), 1)

    def test_fallback_only_after_preferred_missing_capability(self) -> None:
        resolution = resolve_provider(
            request("rigid-body", "soft-body"),
            (
                capability_set("newton", "rigid-body"),
                capability_set("physx", "rigid-body", "soft-body"),
                capability_set("bullet", "rigid-body", "soft-body"),
            ),
        )

        self.assertEqual(resolution.selected_provider, "physx")
        self.assertEqual(
            resolution.fallback_reason,
            "preferred provider 'newton' lacks: soft-body",
        )
        self.assertEqual(len(resolution.evaluated_capability_set_identities), 2)

    def test_unsatisfied_requirements_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ProviderResolutionError,
            "no provider satisfies every required capability",
        ) as raised:
            resolve_provider(
                request("rigid-body", "soft-body"),
                (
                    capability_set("newton", "rigid-body"),
                    capability_set("physx", "soft-body"),
                    capability_set("bullet", "rigid-body"),
                ),
            )
        self.assertEqual(raised.exception.code, "provider_resolution.unsatisfied")

    def test_insufficient_flavor_override_is_rejected(self) -> None:
        with self.assertRaises(ProviderResolutionError) as raised:
            resolve_provider(
                request("rigid-body", "soft-body", override="bullet"),
                (
                    capability_set("newton", "rigid-body", "soft-body"),
                    capability_set("physx", "rigid-body", "soft-body"),
                    capability_set("bullet", "rigid-body"),
                ),
            )
        self.assertEqual(
            raised.exception.code, "provider_resolution.override_insufficient"
        )

    def test_valid_flavor_override_preserves_exact_provenance(self) -> None:
        requested = request("rigid-body", override="physx")
        resolution = resolve_provider(
            requested,
            (
                capability_set("newton", "rigid-body"),
                capability_set("physx", "rigid-body"),
                capability_set("bullet", "rigid-body"),
            ),
        )

        self.assertEqual(resolution.selected_provider, "physx")
        self.assertEqual(
            resolution.override_declaration_identity,
            requested.override_declaration_identity,
        )
        self.assertEqual(
            resolution.override_provenance_identity,
            requested.override_provenance_identity,
        )
        self.assertEqual(ProviderResolution.from_dict(resolution.to_dict()), resolution)

    def test_fallback_order_is_stable_across_catalog_input_order(self) -> None:
        requested = request("rigid-body", "soft-body")
        candidates = (
            capability_set("newton", "rigid-body"),
            capability_set("physx", "rigid-body", "soft-body"),
            capability_set("bullet", "rigid-body", "soft-body"),
        )

        first = resolve_provider(requested, candidates)
        second = resolve_provider(requested, tuple(reversed(candidates)))

        self.assertEqual(first.selected_provider, "physx")
        self.assertEqual(first, second)
        self.assertEqual(first.identity, second.identity)

    def test_catalog_drift_invalidates_and_returns_to_preferred(self) -> None:
        requested = request("rigid-body", "soft-body")
        fallback = resolve_provider(
            requested,
            (
                capability_set("newton", "rigid-body"),
                capability_set("physx", "rigid-body", "soft-body"),
                capability_set("bullet", "rigid-body"),
            ),
        )
        preferred = resolve_provider(
            requested,
            (
                capability_set("newton", "rigid-body", "soft-body"),
                capability_set("physx", "rigid-body", "soft-body"),
                capability_set("bullet", "rigid-body"),
            ),
        )

        self.assertEqual(fallback.selected_provider, "physx")
        self.assertEqual(preferred.selected_provider, "newton")
        self.assertNotEqual(fallback.catalog_identity, preferred.catalog_identity)
        self.assertNotEqual(fallback.identity, preferred.identity)

        base_lock = _diamond_lock()
        fallback_lock = replace(base_lock, provider_resolutions=(fallback,))
        preferred_lock = replace(base_lock, provider_resolutions=(preferred,))
        self.assertNotEqual(fallback_lock.identity, preferred_lock.identity)

        model_identities = {
            node.revision.identity.uri: canonical_identity(
                {"model-for": node.revision.identity.uri}
            )
            for node in base_lock.nodes
        }
        fallback_plan = plan_component_execution(
            fallback_lock, model_identities=model_identities
        )
        preferred_plan = plan_component_execution(
            preferred_lock, model_identities=model_identities
        )
        for plan in fallback_plan.generation_plans:
            self.assertEqual(
                plan.generation_key.provider_resolution_identities,
                (fallback.identity,),
            )
        self.assertNotEqual(fallback_plan.identity, preferred_plan.identity)


if __name__ == "__main__":
    unittest.main()
