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
COMPONENT = REPO_ROOT / "components" / "literate-ai-overview" / "component.md"
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


def build_presentation(
    path: Path,
    *,
    pages: int = 3,
    notes: int | None = None,
    escape: bool = False,
    overlap: bool = False,
) -> None:
    """Write a minimal OOXML presentation the oracle can read."""

    notes = pages if notes is None else notes
    width, height = 1280 * EMU_PER_PX, 720 * EMU_PER_PX
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "ppt/presentation.xml",
            f'<p:presentation xmlns:p="{_P}"><p:sldSz cx="{width}" cy="{height}"/>'
            "</p:presentation>",
        )
        for index in range(1, pages + 1):
            if overlap and index == 1:
                archive.writestr(
                    f"ppt/slides/slide{index}.xml",
                    f'<p:sld xmlns:p="{_P}" xmlns:a="{_A}">'
                    f"<p:sp><p:spPr><a:xfrm>"
                    f'<a:off x="{100 * EMU_PER_PX}" y="{100 * EMU_PER_PX}"/>'
                    f'<a:ext cx="{400 * EMU_PER_PX}" cy="{200 * EMU_PER_PX}"/>'
                    f"</a:xfrm></p:spPr><a:t>Title overlapping</a:t></p:sp>"
                    f"<p:sp><p:spPr><a:xfrm>"
                    f'<a:off x="{200 * EMU_PER_PX}" y="{150 * EMU_PER_PX}"/>'
                    f'<a:ext cx="{400 * EMU_PER_PX}" cy="{200 * EMU_PER_PX}"/>'
                    f"</a:xfrm></p:spPr><a:t>Subtitle overlapping</a:t></p:sp>"
                    "</p:sld>",
                )
                continue
            left = 2000 * EMU_PER_PX if (escape and index == 1) else 100 * EMU_PER_PX
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                f'<p:sld xmlns:p="{_P}" xmlns:a="{_A}"><a:xfrm>'
                f'<a:off x="{left}" y="{100 * EMU_PER_PX}"/>'
                f'<a:ext cx="{100 * EMU_PER_PX}" cy="{100 * EMU_PER_PX}"/>'
                f"</a:xfrm><a:t>Slide {index} body</a:t></p:sld>",
            )
        for index in range(1, notes + 1):
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

    def test_control_accepts_a_conforming_realization(self) -> None:
        report = self.verify(self.manifest)
        self.assertTrue(report["accepted"], report)
        self.assertEqual(report["counts"]["fail"], 0)

    def test_widened_audience_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["access"]["audience"] = "public"
        self.assertRejected(self.verify(mutated), "Audience is not widened")

    def test_narrower_audience_is_allowed(self) -> None:
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["access"]["audience"] = "private"
        self.assertTrue(self.verify(mutated)["accepted"])

    def test_credential_material_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["note"] = "ya29." + "A" * 40
        self.assertRejected(self.verify(mutated), "No credential material is present")

    def test_unauthorized_publication_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["published_location"] = (
            "https://example.com/d"
        )
        self.assertRejected(
            self.verify(mutated), "Unauthorized publication does not occur"
        )

    def test_authorized_publication_is_allowed(self) -> None:
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["published_location"] = (
            "https://example.com/d"
        )
        mutated["members"]["presentation"]["publication_authorized"] = True
        self.assertTrue(self.verify(mutated)["accepted"])

    def test_undeclared_member_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["narrative"] = copy.deepcopy(
            mutated["members"]["presentation"]
        )
        self.assertRejected(
            self.verify(mutated), "Realized members match the declaration"
        )

    def test_missing_artifact_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["local_artifact"] = str(
            self.directory / "absent.pptx"
        )
        self.assertRejected(
            self.verify(mutated), "Manifest is complete and well-formed"
        )

    def test_missing_package_element_is_rejected(self) -> None:
        mutated = copy.deepcopy(self.manifest)
        del mutated["authoring_package"]["elements"]["factual_ledger"]
        self.assertRejected(
            self.verify(mutated), "Every required package element is present"
        )

    def test_missing_notes_page_is_rejected(self) -> None:
        deck = self.directory / "thin-notes.pptx"
        build_presentation(deck, pages=3, notes=1)
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["local_artifact"] = str(deck)
        self.assertRejected(self.verify(mutated), "Every page carries notes")

    def test_element_outside_the_surface_is_rejected(self) -> None:
        deck = self.directory / "escaping.pptx"
        build_presentation(deck, escape=True)
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["local_artifact"] = str(deck)
        self.assertRejected(self.verify(mutated), "No element escapes the surface")

    def test_overlapping_text_frames_are_rejected(self) -> None:
        deck = self.directory / "overlapping.pptx"
        build_presentation(deck, overlap=True)
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["local_artifact"] = str(deck)
        self.assertRejected(self.verify(mutated), "Text-bearing frames do not overlap")

    def test_surface_disagreeing_with_the_declaration_is_rejected(self) -> None:
        component = self.directory / "wide.md"
        component.write_text(
            self.component.read_text(encoding="utf-8").replace(
                "1280 × 720", "1600 × 900"
            ),
            encoding="utf-8",
        )
        self.assertRejected(
            self.verify(self.manifest, component), "No element escapes the surface"
        )

    def test_unresolved_placeholder_is_rejected(self) -> None:
        deck = self.directory / "placeholder.pptx"
        build_presentation(deck)
        with zipfile.ZipFile(deck, "a") as archive:
            archive.writestr(
                "ppt/slides/slide9.xml",
                f'<p:sld xmlns:p="{_P}" xmlns:a="{_A}"><a:t>TODO write this</a:t>'
                "</p:sld>",
            )
        mutated = copy.deepcopy(self.manifest)
        mutated["members"]["presentation"]["local_artifact"] = str(deck)
        self.assertRejected(self.verify(mutated), "No unresolved placeholder ships")

    def test_repository_declaration_parses(self) -> None:
        """The real terminal Component must expose a declaration the oracle can read."""

        declared = ORACLE.parse_declaration(COMPONENT)
        self.assertEqual(declared["members"], ["narrative", "presentation"])
        self.assertEqual(declared["surface"], (1280, 720))
        self.assertIn(declared["audience"], ORACLE.AUDIENCES)
        self.assertIn(declared["permission"], ORACLE.PERMISSIONS)


if __name__ == "__main__":
    unittest.main()
