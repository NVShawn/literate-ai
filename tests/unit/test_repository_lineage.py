from __future__ import annotations

import unittest

from literate_ai.contracts import ContractValidationError, RepositoryParentReference


class RepositoryLineageContractTests(unittest.TestCase):
    def test_parent_urls_and_revision_selectors_reject_credential_and_option_inputs(
        self,
    ) -> None:
        with self.assertRaisesRegex(ContractValidationError, "credentials"):
            RepositoryParentReference(
                "https://user:secret@example.test/parent.git", "main"
            )
        with self.assertRaisesRegex(ContractValidationError, "safe Git revision"):
            RepositoryParentReference(
                "https://example.test/parent.git", "--upload-pack"
            )


if __name__ == "__main__":
    unittest.main()
