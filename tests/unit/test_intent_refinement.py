"""Public-API tests for intent-refinement contracts and litai design."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.application.intent_refinement import (
    IntentRefinementError,
    IntentRefinementService,
)
from literate_ai.cli import main
from literate_ai.contracts.intent_refinement import (
    DesignAcceptRequest,
    DesignDraft,
    DesignStatus,
    IntentRefinementRequest,
    UnresolvedKind,
)

_PI_MISSION = (
    "Write a full-stack application which calculates pi as a service on one or "
    "more servers with specific CPU/GPU configurations while running a web front "
    "end on another machine which displays the current computations in progress, "
    "their elapsed CPU/GPU time, and the results of calculations as they are "
    "streamed across."
)


def _run(arguments: list[str]) -> tuple[int, dict[str, object], str]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    payload = output.getvalue() or errors.getvalue()
    document = json.loads(payload)
    return status, document, errors.getvalue()


class IntentRefinementContractTests(unittest.TestCase):
    def test_simple_request_is_one_sufficient_component(self) -> None:
        request = IntentRefinementRequest(
            mission="Print a greeting card from a name and message list.",
            source="request.md",
        )
        round_tripped = IntentRefinementRequest.from_dict(request.to_dict())
        self.assertEqual(round_tripped, request)
        draft = IntentRefinementService().refine(request)
        self.assertEqual(draft.status, DesignStatus.COMPLETE)
        self.assertEqual(len(draft.components), 1)
        self.assertEqual(draft.components[0].component_id, "application")
        self.assertFalse(draft.blocking_questions())
        self.assertEqual(DesignDraft.from_dict(draft.to_dict()), draft)

    def test_mixed_depth_request_blocks_on_undecided_authority(self) -> None:
        request = IntentRefinementRequest(mission=_PI_MISSION, source="pi.md")
        draft = IntentRefinementService().refine(request)
        self.assertEqual(draft.status, DesignStatus.NEEDS_DECISIONS)
        self.assertGreater(len(draft.components), 1)
        blocking = draft.blocking_questions()
        self.assertGreaterEqual(len(blocking), 4)
        self.assertTrue(all(item.kind is UnresolvedKind.BLOCKING for item in blocking))
        target_bound = [
            item
            for item in draft.unresolved
            if item.kind is UnresolvedKind.TARGET_BOUND
        ]
        self.assertTrue(target_bound)
        with self.assertRaises(IntentRefinementError) as raised:
            IntentRefinementService().accept(
                draft,
                DesignAcceptRequest(draft_identity=draft.identity.uri, decisions=()),
            )
        self.assertEqual(raised.exception.code, "intent_refinement.blocking_unanswered")

    def test_accept_requires_the_exact_draft_identity(self) -> None:
        request = IntentRefinementRequest(mission=_PI_MISSION, source="pi.md")
        draft = IntentRefinementService().refine(request)
        with self.assertRaises(IntentRefinementError) as raised:
            IntentRefinementService().accept(
                draft,
                DesignAcceptRequest(draft_identity="sha256:" + "0" * 64, decisions=()),
            )
        self.assertEqual(
            raised.exception.code, "intent_refinement.draft_identity_mismatch"
        )


class DesignCliTests(unittest.TestCase):
    def test_refine_explain_and_accept_are_public_cli_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            simple = root / "hello.md"
            simple.write_text("Print a greeting card.\n", encoding="utf-8")
            status, refined, errors = _run(["design", "refine", str(simple)])
            self.assertEqual((status, errors), (0, ""))
            self.assertEqual(refined["result"]["status"], "complete")
            self.assertEqual(len(refined["result"]["components"]), 1)

            mixed = root / "pi.md"
            mixed.write_text(_PI_MISSION + "\n", encoding="utf-8")
            status, draft_document, errors = _run(["design", "refine", str(mixed)])
            self.assertEqual(status, 1)
            self.assertEqual(errors, "")
            draft = draft_document["result"]
            self.assertEqual(draft["status"], "needs-decisions")
            draft_path = root / "draft.json"
            draft_path.write_text(json.dumps(draft) + "\n", encoding="utf-8")
            status, explained, errors = _run(
                ["design", "explain", str(draft_path), "--concern", "trust-boundary"]
            )
            self.assertEqual(status, 1)
            self.assertEqual(errors, "")
            self.assertEqual(explained["result"]["status"], "needs-decisions")
            self.assertTrue(explained["result"]["blocking"])

            status, premature, errors = _run(
                [
                    "design",
                    "accept",
                    str(draft_path),
                    "--decisions",
                    str(root / "missing.json"),
                ]
            )
            self.assertEqual(status, 2)
            self.assertEqual(
                premature["error"]["code"], "intent_refinement.invalid_input"
            )

            decisions = [
                {"question_id": item["question_id"], "answer": "specified"}
                for item in draft["unresolved"]
                if item["kind"] == "blocking"
            ]
            decisions_path = root / "decisions.json"
            decisions_path.write_text(json.dumps(decisions) + "\n", encoding="utf-8")
            status, accepted, errors = _run(
                [
                    "design",
                    "accept",
                    str(draft_path),
                    "--decisions",
                    str(decisions_path),
                ]
            )
            self.assertEqual((status, errors), (0, ""))
            self.assertEqual(
                accepted["result"]["schema"],
                "literate-ai/design-acceptance-receipt@1",
            )
            self.assertIn("pi-kernel", accepted["result"]["accepted_component_ids"])
            self.assertNotIn("generation", json.dumps(accepted["result"]))


if __name__ == "__main__":
    unittest.main()
