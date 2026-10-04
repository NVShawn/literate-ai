from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.cli import main


def invoke(*arguments: str) -> tuple[int, dict[str, object], str]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    payload = json.loads(output.getvalue() or errors.getvalue())
    return status, payload, errors.getvalue()


class SpecificationCorpusCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "spec"
        (self.root / "feature").mkdir(parents=True)
        (self.root / "spec.md").write_text(
            "---\nkind: app\nsummary: Root behavior\nname: Example\n---\n# Example\n",
            encoding="utf-8",
        )
        (self.root / "feature" / "spec.md").write_text(
            "---\nsummary: Feature behavior\nname: Feature\nkind: component\n---\n"
            "# Feature\n",
            encoding="utf-8",
        )

    def test_validate_and_explain_are_non_mutating(self) -> None:
        before = self._snapshot()
        status, payload, errors = invoke(
            "spec", "validate", str(self.root), "--id-prefix", "example"
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(payload["result"]["root_id"], "example")

        status, payload, errors = invoke(
            "spec",
            "explain",
            str(self.root / "spec.md"),
            "--id-prefix",
            "example",
            "--node",
            "example.feature",
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(payload["result"]["explanation"]["parent"], "example")
        self.assertEqual(self._snapshot(), before)

    def test_format_preview_check_and_write_are_explicit(self) -> None:
        before = self._snapshot()
        status, preview, errors = invoke(
            "spec", "format", str(self.root), "--id-prefix", "example"
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertTrue(preview["result"]["changed_paths"])
        self.assertEqual(preview["result"]["written_paths"], [])
        self.assertEqual(self._snapshot(), before)

        status, checked, errors = invoke(
            "spec",
            "format",
            str(self.root),
            "--id-prefix",
            "example",
            "--check",
        )
        self.assertEqual((status, errors), (1, ""))
        self.assertEqual(checked["result"]["written_paths"], [])

        status, written, errors = invoke(
            "spec",
            "format",
            str(self.root),
            "--id-prefix",
            "example",
            "--write",
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(
            written["result"]["written_paths"],
            ["spec.md", "feature/spec.md"],
        )
        status, checked, errors = invoke(
            "spec",
            "format",
            str(self.root),
            "--id-prefix",
            "example",
            "--check",
        )
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(checked["result"]["changed_paths"], [])

    def _snapshot(self) -> tuple[tuple[str, bytes], ...]:
        return tuple(
            (path.relative_to(self.root).as_posix(), path.read_bytes())
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        )


if __name__ == "__main__":
    unittest.main()
