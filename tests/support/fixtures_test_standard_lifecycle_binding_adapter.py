"""Shared test fixtures extracted from test_standard_lifecycle_binding_adapter."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from literate_ai.adapters.standard_lifecycle_binding import (
    InstalledFrameworkDistribution,
    observe_installed_framework_distribution,
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
