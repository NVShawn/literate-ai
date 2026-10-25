from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).parents[2]
    / "docs"
    / "presentations"
    / "literate-ai-manager-overview"
    / "publish_google_workspace.py"
)
SPEC = importlib.util.spec_from_file_location("publish_google_workspace", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PUBLISHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLISHER)


def _metadata(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "id": "stable-id",
        "name": "Stable resource",
        "mimeType": PUBLISHER.SLIDES_GOOGLE,
        "version": "1",
        "parents": ["shared-drive"],
        "capabilities": {
            "canEdit": True,
            "canDownload": True,
            "canShare": True,
        },
    }


class GoogleWorkspacePublicationTests(unittest.TestCase):
    def test_preflight_rejects_anonymous_reader(self) -> None:
        permissions = [
            {
                "type": "domain",
                "domain": "nvidia.com",
                "role": "reader",
                "allowFileDiscovery": False,
            },
            {
                "type": "anyone",
                "domain": None,
                "role": "reader",
                "allowFileDiscovery": False,
            },
        ]
        with (
            mock.patch.object(PUBLISHER, "_json", _metadata),
            mock.patch.object(PUBLISHER, "_permissions", return_value=permissions),
            self.assertRaisesRegex(RuntimeError, "organization-restricted"),
        ):
            PUBLISHER._resource_preflight(
                "token",
                member="presentation",
                file_id="stable-id",
                expected_mime=PUBLISHER.SLIDES_GOOGLE,
            )

    def test_preflight_accepts_declared_domain_reader(self) -> None:
        permissions = [
            {
                "type": "domain",
                "domain": "nvidia.com",
                "role": "reader",
                "allowFileDiscovery": False,
            },
            {
                "type": "user",
                "domain": None,
                "role": "owner",
                "allowFileDiscovery": None,
            },
        ]
        with (
            mock.patch.object(PUBLISHER, "_json", _metadata),
            mock.patch.object(PUBLISHER, "_permissions", return_value=permissions),
        ):
            result = PUBLISHER._resource_preflight(
                "token",
                member="presentation",
                file_id="stable-id",
                expected_mime=PUBLISHER.SLIDES_GOOGLE,
            )

        self.assertIs(result["organization_reader_present"], True)


if __name__ == "__main__":
    unittest.main()
