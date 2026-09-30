"""Bootstrap package boundary tests using only the Python standard library."""

from __future__ import annotations

import unittest

import literate_ai


class PackageBoundaryTests(unittest.TestCase):
    def test_package_exposes_bootstrap_version(self) -> None:
        self.assertRegex(literate_ai.__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
