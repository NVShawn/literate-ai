"""Canonical preferred-provider resolution and lifecycle identity binding tests."""

from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from literate_ai.cli.component_locks import component_lock_from_args
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.provider_resolution import (
    ProviderCapabilitySet,
    ProviderResolutionError,
    ProviderResolutionRequest,
    resolve_provider,
)
from tests.support.fixtures_test_component_lock_planning import _fixture


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


if __name__ == "__main__":
    unittest.main()
