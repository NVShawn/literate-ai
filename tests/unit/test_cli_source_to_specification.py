from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.cli import main
from literate_ai.schema_catalog import SCHEMA_CATALOG_ROOT_ENVIRONMENT

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "source_to_specification"
STATE_CASE = FIXTURES / "state-machine" / "case.json"
REVISION_ONE = FIXTURES / "two-revision-refresh" / "revision-1" / "case.json"
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

    def test_persisted_bundle_reviews_with_an_explicit_embedded_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog = root / "embedded-catalog"
            shutil.copytree(REPO_ROOT / "schemas", catalog)
            source = root / "source"
            source.mkdir()
            (source / "counter.py").write_text(
                "def increment(value: int) -> int:\n    return value + 1\n",
                encoding="utf-8",
            )
            key_path = root / "review.key"
            key_path.write_bytes(b"embedded-catalog-review-key-material")
            bundle_path = root / "bundle.json"
            environment = {SCHEMA_CATALOG_ROOT_ENVIRONMENT: str(catalog)}
            with (
                patch.dict(os.environ, environment),
                patch(
                    "literate_ai.schema_catalog.sysconfig.get_path",
                    return_value=str(root / "unrelated-install"),
                ),
            ):
                status, bundle, errors = invoke("spec", "derive", str(source))
                self.assertEqual((status, errors), (0, ""))
                bundle_path.write_text(bundle, encoding="utf-8")

                status, review, errors = invoke(
                    "spec",
                    "review",
                    str(bundle_path),
                    "--actor",
                    "catalog-reviewer",
                    "--key",
                    str(key_path),
                )
                self.assertEqual((status, errors), (0, ""))
                self.assertEqual(
                    json.loads(review)["result"]["schema"],
                    "literate-ai/specification-review-decision@1",
                )

                (catalog / "v2" / "index.json").write_text("{}", encoding="utf-8")
                status, output, errors = invoke(
                    "spec",
                    "review",
                    str(bundle_path),
                    "--actor",
                    "catalog-reviewer",
                    "--key",
                    str(key_path),
                )
                self.assertEqual((status, output), (2, ""))
                self.assertEqual(
                    json.loads(errors)["error"]["code"],
                    "cli.invalid_bundle",
                )

    def test_audit_refresh_coverage_and_diff_use_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bootstrap_path = root / "bootstrap.json"
            audit_path = root / "audit.json"
            refresh_path = root / "refresh.json"

            status, bootstrap, errors = invoke("spec", "derive", str(REVISION_ONE))
            self.assertEqual((status, errors), (0, ""))
            bootstrap_path.write_text(bootstrap)

            status, audit, errors = invoke(
                "spec",
                "audit",
                str(REVISION_ONE),
                "--baseline",
                str(bootstrap_path),
            )
            self.assertEqual((status, errors), (0, ""))
            audit_path.write_text(audit)
            audit_payload = json.loads(audit)
            self.assertEqual(audit_payload["result"]["artifact_diff"]["changed"], [])

            status, refresh, errors = invoke(
                "spec",
                "refresh",
                str(REVISION_TWO),
                "--previous",
                str(audit_path),
            )
            self.assertEqual((status, errors), (0, ""))
            refresh_path.write_text(refresh)
            refresh_payload = json.loads(refresh)
            invalidation = refresh_payload["result"]["invalidation"]
            self.assertEqual(invalidation["invalidated_surface_ids"], ["retry-limit"])
            self.assertEqual(
                invalidation["retained_statement_ids"],
                ["statement:name-observation"],
            )

            status, coverage, errors = invoke("spec", "coverage", str(refresh_path))
            self.assertEqual((status, errors), (0, ""))
            self.assertEqual(json.loads(coverage)["result"]["counts"], {"covered": 2})

            status, difference, errors = invoke(
                "spec", "diff", str(bootstrap_path), str(bootstrap_path)
            )
            self.assertEqual((status, errors), (0, ""))
            diff = json.loads(difference)["result"]
            self.assertEqual(diff["changed"], [])
            self.assertEqual(diff["unchanged"], ["specs/derived/spec.md"])

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

    def test_usage_and_input_errors_are_stable_json(self):
        first = invoke("spec", "derive", "missing-case.json")
        second = invoke("spec", "derive", "missing-case.json")
        self.assertEqual(first, second)
        self.assertEqual((first[0], first[1]), (2, ""))
        payload = json.loads(first[2])
        self.assertEqual(payload["schema"], "literate-ai/cli-error@1")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "cli.input_unavailable")


if __name__ == "__main__":
    unittest.main()
