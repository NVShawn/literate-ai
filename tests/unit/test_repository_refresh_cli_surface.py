"""Fast parser, help, and canonical request checks for public refresh."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.repository_orchestration import OrchestrationInventoryError
from literate_ai.adapters.repository_refresh_planning import (
    load_repository_refresh_request,
)
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.contracts.repository_refresh import (
    RepositoryRefreshRequest,
    RepositoryRefreshTarget,
)


class RepositoryRefreshCliSurfaceTests(unittest.TestCase):
    def test_request_file_requires_exact_canonical_typed_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "request.json"
            request = RepositoryRefreshRequest(
                (RepositoryRefreshTarget("app", "1" * 40),)
            )
            canonical = canonical_json_bytes(request.to_dict()) + b"\n"
            path.write_bytes(canonical)
            self.assertEqual(load_repository_refresh_request(path), request)
            path.write_bytes(canonical + b"\n")
            with self.assertRaises(OrchestrationInventoryError) as caught:
                load_repository_refresh_request(path)
            self.assertEqual(
                caught.exception.code, "orchestration.refresh_request_invalid"
            )


if __name__ == "__main__":
    unittest.main()
