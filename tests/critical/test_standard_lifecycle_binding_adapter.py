"""Exact installed-wheel authority for the Standard lifecycle driver."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from literate_ai.adapters.standard_lifecycle_binding import (
    InstalledFrameworkDistribution,
    StandardLifecycleBindingError,
    observe_installed_framework_distribution,
)
from literate_ai.contracts import (
    load_current_standard_lifecycle_policy,
)


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


if __name__ == "__main__":
    unittest.main()
