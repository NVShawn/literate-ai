from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.cli import main

CASE = (
    Path(__file__).resolve().parents[1]
    / "fixtures/source_to_specification/state-machine/case.json"
)
KEY = b"explicit-policy-fixture-trust-key-32-bytes-minimum"


def invoke(*arguments: str) -> tuple[int, str, str]:
    output, errors = io.StringIO(), io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    return status, output.getvalue(), errors.getvalue()


class ReviewAuthorityCliTests(unittest.TestCase):
    def derive_fixture(self, root: Path) -> Path:
        status, output, errors = invoke("spec", "derive", str(CASE))
        self.assertEqual((status, errors), (0, ""))
        path = root / "bundle.json"
        path.write_text(output)
        return path

    def test_explicit_policy_review_still_needs_separate_operator_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = self.derive_fixture(root)
            status, human, errors = invoke("spec", "review", str(bundle))
            self.assertEqual((status, errors), (0, ""))
            self.assertEqual(
                json.loads(human)["result"]["review"]["authority"], "human"
            )
            self.assertEqual(
                json.loads(human)["result"]["review"]["actor"], "local-human"
            )
            status, policy, errors = invoke(
                "spec",
                "review",
                str(bundle),
                "--authority",
                "authorized-policy",
                "--actor",
                "agent-fixture-reviewer",
            )
            self.assertEqual((status, errors), (0, ""))
            decision = json.loads(policy)["result"]["review"]
            self.assertEqual(decision["authority"], "authorized-policy")
            review = root / "review.json"
            review.write_text(policy)
            target = root / "accepted"
            arguments = (
                "spec",
                "accept",
                str(CASE),
                str(bundle),
                "--review",
                str(review),
                "--target",
                str(target),
            )
            status, output, errors = invoke(*arguments)
            self.assertEqual((status, output), (2, ""))
            self.assertEqual(
                json.loads(errors)["error"]["code"], "promotion.authority_denied"
            )
            self.assertFalse(target.exists())
            status, _, errors = invoke(*arguments, "--allow-authorized-policy-review")
            self.assertEqual((status, errors), (0, ""))
            manifest = json.loads((target / "specification-set.json").read_text())
            self.assertEqual(
                manifest["promotion_policy_id"], "human-or-authorized-policy-reviewed@1"
            )
            self.assertEqual(
                json.loads((target / "review.json").read_text())["review"]["authority"],
                "authorized-policy",
            )

    def test_policy_requires_explicit_actor_and_authority_changes_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = self.derive_fixture(root)
            for actor in ([], ["--actor", " "]):
                status, output, errors = invoke(
                    "spec",
                    "review",
                    str(bundle),
                    "--authority",
                    "authorized-policy",
                    *actor,
                )
                self.assertEqual((status, output), (2, ""))
                self.assertEqual(
                    json.loads(errors)["error"]["code"], "cli.review_actor_required"
                )
            decisions = []
            for authority in ("human", "authorized-policy"):
                status, output, errors = invoke(
                    "spec",
                    "review",
                    str(bundle),
                    "--authority",
                    authority,
                    "--actor",
                    "fixture-actor",
                )
                self.assertEqual((status, errors), (0, ""))
                decisions.append(json.loads(output)["result"]["review"])
            self.assertNotEqual(decisions[0]["review_id"], decisions[1]["review_id"])

    def test_standalone_signature_binds_policy_authority_and_human_default_remains(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "echo.py").write_text("def echo(value):\n    return value\n")
            key = root / "trust.key"
            key.write_bytes(KEY)
            attestation = root / "attestation.json"
            status, output, errors = invoke(
                "spec",
                "attest",
                str(source),
                "--signer",
                "fixture",
                "--machine",
                "fixture",
                "--key",
                str(key),
            )
            self.assertEqual((status, errors), (0, ""))
            attestation.write_text(output)
            bundle = root / "bundle.json"
            status, output, errors = invoke(
                "spec",
                "derive",
                str(source),
                "--attestation",
                str(attestation),
                "--trust-key",
                str(key),
            )
            self.assertEqual((status, errors), (0, ""))
            bundle.write_text(output)
            resolutions = []
            for unknown in json.loads(output)["result"]["review_gate"][
                "blocking_uncertainty_ids"
            ]:
                resolutions.extend(("--resolve", unknown))
            status, policy, errors = invoke(
                "spec",
                "review",
                str(bundle),
                "--authority",
                "authorized-policy",
                "--actor",
                "agent-fixture-reviewer",
                "--key",
                str(key),
                *resolutions,
            )
            self.assertEqual((status, errors), (0, ""))
            review = root / "review.json"
            review.write_text(policy)
            args = (
                "spec",
                "accept",
                str(source),
                str(bundle),
                "--review",
                str(review),
                "--trust-key",
                str(key),
            )
            status, _, errors = invoke(
                *args,
                "--target",
                str(root / "policy"),
                "--allow-authorized-policy-review",
            )
            self.assertEqual((status, errors), (0, ""))
            manifest = json.loads((root / "policy/specification-set.json").read_text())
            self.assertEqual(
                manifest["promotion_policy_id"],
                "signed-local-human-or-authorized-policy-reviewed@1",
            )
            altered = json.loads(policy)
            altered["result"]["review"]["authority"] = "human"
            review.write_text(json.dumps(altered))
            status, output, errors = invoke(
                *args,
                "--target",
                str(root / "tampered"),
                "--allow-authorized-policy-review",
            )
            self.assertEqual((status, output), (2, ""))
            self.assertEqual(
                json.loads(errors)["error"]["code"], "review.attestation_invalid"
            )
            self.assertFalse((root / "tampered").exists())
            status, human, errors = invoke(
                "spec",
                "review",
                str(bundle),
                "--actor",
                "fixture-human",
                "--key",
                str(key),
                *resolutions,
            )
            self.assertEqual((status, errors), (0, ""))
            review.write_text(human)
            status, _, errors = invoke(*args, "--target", str(root / "human"))
            self.assertEqual((status, errors), (0, ""))
            self.assertEqual(
                json.loads((root / "human/specification-set.json").read_text())[
                    "promotion_policy_id"
                ],
                "signed-local-human-reviewed@1",
            )


if __name__ == "__main__":
    unittest.main()
