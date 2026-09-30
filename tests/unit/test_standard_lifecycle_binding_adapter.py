"""Exact installed-wheel authority for the Standard lifecycle driver."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path, PurePosixPath

from literate_ai.adapters.standard_lifecycle_binding import (
    _DIST_INFO_EXCLUSIONS,
    InstalledFrameworkDistribution,
    StandardLifecycleBindingError,
    observe_installed_framework_distribution,
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.contracts import (
    LifecycleDriverTrust,
    StandardProjectLifecycleDriver,
    load_current_standard_lifecycle_policy,
)
from literate_ai.remote_worker_bootstrap import _EXCLUDED_METADATA


class FakeDistribution:
    def __init__(
        self,
        root: Path,
        *,
        name: str = "literate-ai",
        version: str = "0.0.0",
        entries: dict[str, bytes] | None = None,
        direct_url: object | None = None,
    ) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.metadata = {"Name": name}
        self.version = version
        self._direct_url = direct_url
        payload = entries or {
            "literate_ai/__init__.py": b'"""framework"""\n',
            "literate_ai/standard_policies/standard-lifecycle-v1.json": b"{}\n",
            "../../../share/literate-ai/schemas/v2/projects.json": b"{}\n",
            "literate_ai-0.0.0.dist-info/METADATA": (
                b"Metadata-Version: 2.4\nName: literate-ai\nVersion: 0.0.0\n"
            ),
            "literate_ai-0.0.0.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
            # Mutable installation records and generated launchers are excluded.
            "literate_ai-0.0.0.dist-info/RECORD": b"host-specific\n",
            "../../../bin/litai": b"generated launcher\n",
            "../../../bin/litai-mcp": b"generated mcp launcher\n",
        }
        self.files = tuple(PurePosixPath(path) for path in payload)
        self.locations: dict[str, Path] = {}
        for index, (entry, content) in enumerate(payload.items()):
            location = root / f"payload-{index}"
            location.write_bytes(content)
            self.locations[entry] = location

    def locate_file(self, path: object) -> Path:
        return self.locations[str(path)]

    def read_text(self, filename: str) -> str | None:
        if filename != "direct_url.json" or self._direct_url is None:
            return None
        if isinstance(self._direct_url, str):
            return self._direct_url
        return json.dumps(self._direct_url)


def observe(distribution: FakeDistribution) -> InstalledFrameworkDistribution:
    return observe_installed_framework_distribution(
        distribution_finder=lambda _name: (distribution,)
    )


class StandardLifecycleBindingAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.distribution = FakeDistribution(self.root)
        self.observation = observe(self.distribution)
        self.policy = load_current_standard_lifecycle_policy()

    def test_observation_is_logical_canonical_and_host_path_free(self) -> None:
        material = self.observation.identity_material()
        encoded = json.dumps(material, sort_keys=True)

        self.assertNotIn(str(self.root), encoded)
        self.assertEqual(material["distribution_name"], "literate-ai")
        paths = [item["path"] for item in material["members"]]
        self.assertEqual(paths, sorted(paths))
        self.assertIn("literate_ai/__init__.py", paths)
        self.assertIn("share/literate-ai/schemas/v2/projects.json", paths)
        self.assertNotIn("literate_ai-0.0.0.dist-info/RECORD", paths)
        self.assertFalse(any(path.endswith("/litai") for path in paths))
        self.assertFalse(any(path.endswith("/litai-mcp") for path in paths))

    def test_installer_metadata_exclusions_match_worker_bootstrap(self) -> None:
        self.assertEqual(_DIST_INFO_EXCLUSIONS, _EXCLUDED_METADATA)

        entries = {
            str(path): self.distribution.locations[str(path)].read_bytes()
            for path in self.distribution.files
        }
        entries["literate_ai-0.0.0.dist-info/uv_cache.json"] = (
            b'{"cache_info":{"timestamp":1}}\n'
        )
        entries["literate_ai-0.0.0.dist-info/uv_build.json"] = b"{}\n"
        uv_observation = observe(FakeDistribution(self.root / "uv", entries=entries))

        self.assertEqual(uv_observation.identity, self.observation.identity)

    def test_exact_distribution_and_policy_resolve_to_standard_trust(self) -> None:
        driver = StandardProjectLifecycleDriver(
            self.observation.identity, self.policy.identity
        )
        resolved = resolve_standard_project_lifecycle_driver(
            driver,
            distribution_observer=lambda: self.observation,
            policy_loader=lambda: self.policy,
        )

        self.assertEqual(resolved.distribution, self.observation)
        self.assertEqual(resolved.policy, self.policy)
        self.assertEqual(resolved.trust_binding.trust, LifecycleDriverTrust.STANDARD)
        self.assertEqual(resolved.trust_binding.driver_identity, driver.identity)
        self.assertEqual(
            resolved.trust_binding.framework_distribution_identity,
            self.observation.identity,
        )
        self.assertEqual(resolved.trust_binding.policy_identity, self.policy.identity)
        resolved.require_unchanged()

    def test_wrong_distribution_and_policy_fail_with_distinct_codes(self) -> None:
        changed_distribution = replace(
            self.observation,
            members=(
                *self.observation.members[:-1],
                replace(self.observation.members[-1], digest="sha256:" + "a" * 64),
            ),
        )
        driver = StandardProjectLifecycleDriver(
            changed_distribution.identity, self.policy.identity
        )
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            resolve_standard_project_lifecycle_driver(
                driver,
                distribution_observer=lambda: self.observation,
                policy_loader=lambda: self.policy,
            )
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_mismatch"
        )

        driver = StandardProjectLifecycleDriver(
            self.observation.identity, changed_distribution.identity
        )
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            resolve_standard_project_lifecycle_driver(
                driver,
                distribution_observer=lambda: self.observation,
                policy_loader=lambda: self.policy,
            )
        self.assertEqual(caught.exception.code, "standard_binding.policy_mismatch")

    def test_missing_and_duplicate_distributions_fail_closed(self) -> None:
        for candidates, code in (
            ((), "standard_binding.distribution_unavailable"),
            (
                (self.distribution, self.distribution),
                "standard_binding.distribution_ambiguous",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(StandardLifecycleBindingError) as caught:
                    observe_installed_framework_distribution(
                        distribution_finder=lambda _name, value=candidates: value
                    )
                self.assertEqual(caught.exception.code, code)

    def test_editable_installations_are_explicitly_rejected(self) -> None:
        editable = FakeDistribution(
            self.root / "editable",
            direct_url={"url": "file:///source", "dir_info": {"editable": True}},
        )
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(editable)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_editable"
        )

        pth = FakeDistribution(
            self.root / "pth",
            entries={
                "__editable__.literate_ai-0.0.0.pth": b"/source\n",
                "literate_ai/__init__.py": b"",
                "literate_ai/standard_policies/standard-lifecycle-v1.json": b"{}",
                "../../../share/literate-ai/schemas/v2/projects.json": b"{}",
            },
        )
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(pth)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_editable"
        )

    def test_missing_symlinked_and_incomplete_payload_members_are_rejected(
        self,
    ) -> None:
        missing = FakeDistribution(self.root / "missing")
        missing.locations["literate_ai/__init__.py"].unlink()
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(missing)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_payload_unavailable"
        )

        symlinked = FakeDistribution(self.root / "symlinked")
        member = symlinked.locations["literate_ai/__init__.py"]
        target = symlinked.root / "target"
        target.write_bytes(b"framework")
        member.unlink()
        member.symlink_to(target)
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(symlinked)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_payload_unsafe"
        )

        incomplete = FakeDistribution(
            self.root / "incomplete",
            entries={
                "literate_ai/__init__.py": b"",
                "literate_ai-0.0.0.dist-info/METADATA": b"Name: literate-ai\n",
            },
        )
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(incomplete)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_payload_incomplete"
        )

    def test_undeclared_console_launchers_fail_closed(self) -> None:
        entries = {
            str(path): self.distribution.locations[str(path)].read_bytes()
            for path in self.distribution.files
        }
        entries["../../../bin/other-tool"] = b"surprise\n"
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(FakeDistribution(self.root / "unknown-launcher", entries=entries))
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_payload_invalid"
        )
        self.assertIn("unsupported payload location", caught.exception.message)

    def test_derived_bytecode_and_mutable_install_metadata_do_not_change_identity(
        self,
    ) -> None:
        entries = {
            str(path): self.distribution.locations[str(path)].read_bytes()
            for path in self.distribution.files
        }
        entries["literate_ai/__pycache__/__init__.cpython-313.pyc"] = b"derived"
        entries["literate_ai-0.0.0.dist-info/INSTALLER"] = b"different-installer\n"
        with_derived = FakeDistribution(self.root / "derived", entries=entries)

        self.assertEqual(observe(with_derived).identity, self.observation.identity)

    def test_post_resolution_distribution_and_policy_drift_are_rejected(self) -> None:
        driver = StandardProjectLifecycleDriver(
            self.observation.identity, self.policy.identity
        )
        current_policy = [self.policy]
        resolved = resolve_standard_project_lifecycle_driver(
            driver,
            distribution_observer=lambda: observe(self.distribution),
            policy_loader=lambda: current_policy[0],
        )
        package = self.distribution.locations["literate_ai/__init__.py"]
        original = package.read_bytes()
        package.write_bytes(original + b"# drift\n")
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            resolved.require_unchanged()
        self.assertEqual(caught.exception.code, "standard_binding.changed")

        package.write_bytes(original)
        current_policy[0] = replace(self.policy, minimum_test_count=2)
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            resolved.require_unchanged()
        self.assertEqual(caught.exception.code, "standard_binding.changed")

    def test_policy_loader_and_type_failures_have_stable_codes(self) -> None:
        driver = StandardProjectLifecycleDriver(
            self.observation.identity, self.policy.identity
        )
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            resolve_standard_project_lifecycle_driver(
                driver,
                distribution_observer=lambda: self.observation,
                policy_loader=lambda: (_ for _ in ()).throw(OSError("missing")),
            )
        self.assertEqual(caught.exception.code, "standard_binding.policy_unavailable")

        with self.assertRaises(StandardLifecycleBindingError) as caught:
            resolve_standard_project_lifecycle_driver(
                driver,
                distribution_observer=lambda: self.observation,
                policy_loader=lambda: object(),  # type: ignore[return-value]
            )
        self.assertEqual(caught.exception.code, "standard_binding.policy_invalid")


if __name__ == "__main__":
    unittest.main()
