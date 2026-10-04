from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "source_to_specification"
STATE_CASE = FIXTURES / "state-machine" / "case.json"
REVISION_TWO = FIXTURES / "two-revision-refresh" / "revision-2" / "case.json"
CASES = (
    STATE_CASE,
    FIXTURES / "library-consumer" / "case.json",
    REVISION_TWO,
    FIXTURES / "contradictory-tests" / "case.json",
    FIXTURES / "prompt-injection" / "case.json",
    FIXTURES / "flavor-split" / "case.json",
)


def invoke(*arguments: str) -> tuple[int, str, str]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    return status, output.getvalue(), errors.getvalue()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class SourceToSpecificationCliTests(unittest.TestCase):
    def test_every_fixture_conforms_deterministically_without_source_mutation(self):
        for case in CASES:
            with self.subTest(case=case.parent.name):
                descriptor = json.loads(case.read_text())
                source = (case.parent / descriptor["source_root"]).resolve()
                before = tree_digest(source)
                first = invoke("spec", "conformance", str(case))
                second = invoke("spec", "conformance", str(case))
                self.assertEqual(first, second)
                self.assertEqual(first[0], 0, first[2])
                payload = json.loads(first[1])
                self.assertTrue(payload["ok"])
                self.assertTrue(payload["result"]["passed"])
                self.assertEqual(payload["result"]["mismatches"], [])
                self.assertEqual(tree_digest(source), before)

    def test_derive_review_accept_is_atomic_and_requires_separate_target(self):
        source = STATE_CASE.parent / "source"
        before = tree_digest(source)
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            bundle_path = temporary_root / "bundle.json"
            review_path = temporary_root / "review.json"
            target = temporary_root / "accepted"

            status, output, errors = invoke("spec", "derive", str(STATE_CASE))
            self.assertEqual((status, errors), (0, ""))
            bundle_path.write_text(output)
            bundle = json.loads(output)
            self.assertEqual(
                bundle["result"]["schema"],
                "literate-ai/source-to-specification-result-bundle@5",
            )

            status, output, errors = invoke("spec", "review", str(bundle_path))
            self.assertEqual((status, errors), (0, ""))
            review_path.write_text(output)

            status, output, errors = invoke(
                "spec",
                "accept",
                str(STATE_CASE),
                str(bundle_path),
                "--review",
                str(review_path),
                "--target",
                str(target),
            )
            self.assertEqual((status, errors), (0, ""))
            self.assertTrue((target / "specification-set.json").is_file())
            self.assertTrue((target / "review.json").is_file())
            self.assertTrue((target / "specs" / "derived" / "spec.md").is_file())

            overlapping = source / "accepted-specs"
            status, output, errors = invoke(
                "spec",
                "accept",
                str(STATE_CASE),
                str(bundle_path),
                "--review",
                str(review_path),
                "--target",
                str(overlapping),
            )
            self.assertEqual((status, output), (2, ""))
            error = json.loads(errors)
            self.assertEqual(error["error"]["code"], "cli.target_overlaps_source")
            self.assertFalse(overlapping.exists())
        self.assertEqual(tree_digest(source), before)

    def test_prompt_injection_cannot_select_skills_or_change_policy(self):
        case = FIXTURES / "prompt-injection" / "case.json"
        status, output, errors = invoke("spec", "derive", str(case))
        self.assertEqual((status, errors), (0, ""))
        result = json.loads(output)["result"]["result"]
        self.assertEqual(result["request"]["egress_policy_id"], "none@1")
        self.assertEqual(
            result["request"]["routing_policy_id"], "sample-static-local@1"
        )
        observation = result["observations"][0]
        self.assertEqual(observation["skill"]["skill_id"], "security")


if __name__ == "__main__":
    unittest.main()
