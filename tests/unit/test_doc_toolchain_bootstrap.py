from __future__ import annotations

import unittest

from scripts.bootstrap_doc_toolchain import install, main


class DocToolchainBootstrapTests(unittest.TestCase):
    def test_install_without_authorization_fails_closed(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            install(allow_install=False)
        self.assertIn("--allow-install", str(raised.exception))

    def test_cli_detect_returns_structured_status(self) -> None:
        status = main(["--detect"])
        self.assertIn(status, {0, 2})


if __name__ == "__main__":
    unittest.main()
