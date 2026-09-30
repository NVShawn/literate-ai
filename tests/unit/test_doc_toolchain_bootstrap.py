from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.bootstrap_doc_toolchain import detect, install, main, toolchain_python


class DocToolchainBootstrapTests(unittest.TestCase):
    def test_manifest_pins_portable_ooxml_packages(self) -> None:
        manifest = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "tools/doc-toolchain/authoring-toolchain.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["backend"], "python-ooxml")
        names = [item["name"] for item in manifest["packages"]]
        self.assertEqual(names, ["python-pptx", "python-docx", "lxml", "Pillow"])

    def test_install_without_authorization_fails_closed(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            install(allow_install=False)
        self.assertIn("--allow-install", str(raised.exception))

    def test_detect_reports_a_stable_status_envelope(self) -> None:
        status = detect()
        self.assertEqual(
            status["schema"],
            "literate-ai/document-pair-authoring-toolchain-status@1",
        )
        self.assertIn(status["state"], {"ready", "missing"})
        self.assertIsInstance(status["missing"], list)
        self.assertIn("python", status)
        python = toolchain_python()
        if python is None:
            self.assertIsNone(status["python"])
        else:
            self.assertEqual(status["python"], str(python))

    def test_cli_detect_returns_structured_status(self) -> None:
        status = main(["--detect"])
        self.assertIn(status, {0, 2})


if __name__ == "__main__":
    unittest.main()
