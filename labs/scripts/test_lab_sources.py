#!/usr/bin/env python3
"""Refuse misleading or stale source inspection, beyond a successful rebuild."""

import json
import unittest

from build_labs import ROOT
from verify_labs import verify_lab


class SourceIntegrity(unittest.TestCase):
    def setUp(self):
        self.lab = ROOT / "00-grumpy-counter"
        self.page = (self.lab / "index.html").read_text()

    def test_current_bundle(self):
        verify_lab(self.lab, self.page)

    def test_abridged_markdown_refused(self):
        page = self.page.replace(
            "## Canonical authority and interface", "## Omitted interface", 1
        )
        with self.assertRaises(AssertionError):
            verify_lab(self.lab, page)

    def test_executable_drift_refused(self):
        page = self.page.replace("let modulus = 37;", "let modulus = 38;", 1)
        with self.assertRaises(AssertionError):
            verify_lab(self.lab, page)

    def test_source_omission_refused(self):
        start = self.page.index('<script id="lab-config" type="application/json">')
        content = self.page.index(">", start) + 1
        end = self.page.index("</script>", content)
        config = json.loads(self.page[content:end])
        config["sources"].pop()
        replacement = json.dumps(config).replace("<", "\\u003c")
        page = self.page[:content] + replacement + self.page[end:]
        with self.assertRaises(AssertionError):
            verify_lab(self.lab, page)

    def test_false_graph_claim_refused(self):
        page = self.page.replace(
            "Resolved authority graph", "Invented Component graph", 1
        )
        with self.assertRaises(AssertionError):
            verify_lab(self.lab, page)


if __name__ == "__main__":
    unittest.main()
