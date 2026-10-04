"""The document-pair acceptance oracle must reject every contract violation.

A passing oracle proves nothing unless it can fail, so each scenario in
``components/document-pair/acceptance/document-pair.md`` gets a mutation that must be
caught. The control asserts the unmutated realization is accepted, which keeps these
from passing merely because the oracle rejects everything.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ORACLE_PATH = REPO_ROOT / "scripts" / "verify_document_pair.py"
EMU_PER_PX = 9525
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def load_oracle():
    spec = importlib.util.spec_from_file_location("document_pair_oracle", ORACLE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORACLE = load_oracle()


def build_presentation(path: Path, *, pages: int = 3) -> None:
    """Write a minimal OOXML presentation the oracle can read."""

    width, height = 1280 * EMU_PER_PX, 720 * EMU_PER_PX
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "ppt/presentation.xml",
            f'<p:presentation xmlns:p="{_P}"><p:sldSz cx="{width}" cy="{height}"/>'
            "</p:presentation>",
        )
        for index in range(1, pages + 1):
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                f'<p:sld xmlns:p="{_P}" xmlns:a="{_A}"><a:xfrm>'
                f'<a:off x="{100 * EMU_PER_PX}" y="{100 * EMU_PER_PX}"/>'
                f'<a:ext cx="{100 * EMU_PER_PX}" cy="{100 * EMU_PER_PX}"/>'
                f"</a:xfrm><a:t>Slide {index} body</a:t></p:sld>",
            )
            archive.writestr(
                f"ppt/notesSlides/notesSlide{index}.xml",
                f'<p:notes xmlns:p="{_P}" xmlns:a="{_A}">'
                f"<a:t>Authority for slide {index}.</a:t></p:notes>",
            )


class DocumentPairOracleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.deck = self.directory / "deck.pptx"
        build_presentation(self.deck)

        package = self.directory / "package"
        package.mkdir()
        self.elements = {
            "narrative_specification": "deck-specification.md",
            "factual_ledger": "source-notes.md",
            "generation_prompts": "prompts",
            "build_source": "build_deck.mjs",
            "assets": "assets",
            "regeneration_entry_point": "regenerate.sh",
            "deliverable_links": "current-deliverables.md",
            "qa_record": "qa-ledger.md",
        }
        for value in self.elements.values():
            target = package / value
            if "." in value:
                target.write_text("content\n", encoding="utf-8")
            else:
                target.mkdir()

        self.component = self.directory / "component.md"
        self.component.write_text(
            "# Consumer\n\n"
            "| Concern | Decision |\n| --- | --- |\n"
            "| Members declared | `presentation` only |\n"
            "| Surface geometry | 1280 × 720 |\n"
            "| Access audience | `organization` |\n"
            "| Permission | `view` |\n"
            "| Link sharing | disabled |\n",
            encoding="utf-8",
        )
        self.manifest = {
            "schema": "literate-ai/document-pair-manifest@1",
            "ecosystem": "google-workspace",
            "members": {
                "presentation": {
                    "local_artifact": str(self.deck),
                    "published_location": None,
                    "publication_authorized": False,
                    "access": {
                        "audience": "organization",
                        "principals": [],
                        "permission": "view",
                        "link_sharing": "organization-restricted",
                    },
                }
            },
            "authoring_package": {
                "root": str(package),
                "elements": dict(self.elements),
            },
        }

    def verify(self, manifest: dict, component: Path | None = None) -> dict:
        path = self.directory / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        report_path = self.directory / "report.json"
        status = ORACLE.main(
            [
                "--manifest",
                str(path),
                "--component",
                str(component or self.component),
                "--json",
                str(report_path),
                "--quiet",
            ]
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(status, 0 if report["accepted"] else 1)
        return report

    def assertRejected(self, report: dict, scenario: str) -> None:
        self.assertFalse(report["accepted"])
        failed = {
            item["scenario"] for item in report["scenarios"] if item["state"] == "fail"
        }
        self.assertIn(scenario, failed)

    def test_credential_material_is_rejected(self) -> None:
        control = self.verify(self.manifest)
        self.assertTrue(control["accepted"], control)
        self.assertEqual(control["counts"]["fail"], 0)
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["note"] = "ya29." + "A" * 40
        self.assertRejected(self.verify(mutated), "No credential material is present")

    def test_unauthorized_publication_and_widened_audience_are_rejected(
        self,
    ) -> None:
        cases = (
            (
                "published_location",
                "https://example.com/d",
                "Unauthorized publication does not occur",
            ),
            ("audience", "public", "Audience is not widened"),
        )
        for field, value, scenario in cases:
            with self.subTest(scenario=scenario):
                mutated = copy.deepcopy(self.manifest)
                member = mutated["members"]["presentation"]
                if field == "audience":
                    member["access"]["audience"] = value
                else:
                    member[field] = value
                self.assertRejected(self.verify(mutated), scenario)


if __name__ == "__main__":
    unittest.main()
