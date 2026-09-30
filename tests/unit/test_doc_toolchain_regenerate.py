"""Vanilla workers regenerate the overview pair without a Codex plugin."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "docs" / "presentations" / "literate-ai-manager-overview"
REGENERATE = PACKAGE / "regenerate.sh"
COMPONENT = REPO_ROOT / "components" / "literate-ai-overview" / "component.md"
ORACLE_PATH = REPO_ROOT / "scripts" / "verify_document_pair.py"
PPTX = PACKAGE / "literate-ai-manager-and-engineering-overview.pptx"
DOCX = PACKAGE / "literate-ai-manager-and-engineering-overview.docx"


def load_oracle():
    spec = importlib.util.spec_from_file_location("document_pair_oracle", ORACLE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ORACLE = load_oracle()


def _bash() -> str | None:
    return shutil.which("bash")


def _isolated_env(obj_dir: Path, *, codex_home: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["OBJ_DIR"] = str(obj_dir)
    environment["CODEX_HOME"] = str(codex_home)
    environment["HOME"] = str(codex_home.parent)
    environment["LITERATE_AI_DECK_SKIP_RENDER"] = "1"
    environment.pop("LITERATE_AI_DECK_OUTPUT", None)
    environment.pop("LITERATE_AI_NARRATIVE_OUTPUT", None)
    return environment


def _locate_doc_toolchain_obj() -> Path | None:
    roots: list[Path] = []
    explicit = os.environ.get("LITAI_DOC_TOOLCHAIN_OBJ")
    if explicit:
        roots.append(Path(explicit))
    roots.append(REPO_ROOT / "_build")
    for root in roots:
        for relative in (
            "doc-toolchain/bin/python3",
            "doc-toolchain/bin/python",
            "doc-toolchain/Scripts/python.exe",
        ):
            python = root / relative
            if not python.is_file():
                continue
            probe = subprocess.run(
                [str(python), "-c", "import pptx, docx, PIL, lxml"],
                capture_output=True,
                check=False,
            )
            if probe.returncode == 0:
                return root
    return None


class DocToolchainRegenerateTests(unittest.TestCase):
    def test_regenerate_sh_keeps_codex_plugin_discovery(self) -> None:
        text = REGENERATE.read_text(encoding="utf-8")
        self.assertIn("setup_artifact_tool_workspace.mjs", text)
        self.assertIn("regenerate_python.sh", text)
        self.assertIn("--check", text)

    def test_regenerate_sh_without_plugin_or_toolchain_directs_bootstrap(self) -> None:
        bash = _bash()
        if bash is None:
            self.skipTest("bash is unavailable")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            obj = root / "obj"
            obj.mkdir()
            home = root / "home"
            home.mkdir()
            result = subprocess.run(
                [bash, str(REGENERATE)],
                cwd=str(PACKAGE),
                capture_output=True,
                text=True,
                check=False,
                env=_isolated_env(obj, codex_home=home / ".codex"),
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        combined = result.stderr + result.stdout
        self.assertIn("doc-toolchain-bootstrap", combined)
        self.assertNotIn(
            "Install or refresh the presentations plugin, then rerun this script.",
            combined,
        )

    def test_checked_in_package_passes_the_independent_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            manifest = {
                "schema": "literate-ai/document-pair-manifest@1",
                "ecosystem": "google-workspace",
                "members": {
                    "presentation": {
                        "local_artifact": str(PPTX),
                        "published_location": None,
                        "publication_authorized": False,
                        "access": {
                            "audience": "organization",
                            "principals": [],
                            "permission": "view",
                            "link_sharing": "organization-restricted",
                        },
                    },
                    "narrative": {
                        "local_artifact": str(DOCX),
                        "published_location": None,
                        "publication_authorized": False,
                        "access": {
                            "audience": "organization",
                            "principals": [],
                            "permission": "view",
                            "link_sharing": "organization-restricted",
                        },
                    },
                },
                "authoring_package": {
                    "root": str(PACKAGE),
                    "elements": {
                        "narrative_specification": "narrative-specification.md",
                        "factual_ledger": "source-notes.md",
                        "generation_prompts": "prompts",
                        "build_source": "build_deck.py",
                        "assets": "assets",
                        "regeneration_entry_point": "regenerate.sh",
                        "deliverable_links": "current-deliverables.md",
                        "qa_record": "qa-ledger.md",
                    },
                },
            }
            path = directory / "manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            report_path = directory / "report.json"
            status = ORACLE.main(
                [
                    "--manifest",
                    str(path),
                    "--component",
                    str(COMPONENT),
                    "--json",
                    str(report_path),
                    "--quiet",
                ]
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(status, 0, report)
        self.assertTrue(report["accepted"], report)
        self.assertEqual(report["counts"]["fail"], 0, report)

    def test_vanilla_worker_regenerates_checked_in_package_and_passes_oracle(
        self,
    ) -> None:
        bash = _bash()
        if bash is None:
            self.skipTest("bash is unavailable")
        toolchain_obj = _locate_doc_toolchain_obj()
        if toolchain_obj is None:
            self.skipTest(
                "pinned doc-toolchain is not installed; "
                "run make doc-toolchain-bootstrap"
            )
        original_pptx = PPTX.read_bytes()
        original_docx = DOCX.read_bytes()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            home.mkdir()
            output_pptx = root / "deck.pptx"
            output_docx = root / "narrative.docx"
            environment = _isolated_env(toolchain_obj, codex_home=home / ".codex")
            environment["LITERATE_AI_DECK_OUTPUT"] = str(output_pptx)
            environment["LITERATE_AI_NARRATIVE_OUTPUT"] = str(output_docx)
            result = subprocess.run(
                [bash, str(REGENERATE)],
                cwd=str(PACKAGE),
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
            self.addCleanup(PPTX.write_bytes, original_pptx)
            self.addCleanup(DOCX.write_bytes, original_docx)
            self.assertEqual(
                PPTX.read_bytes(),
                original_pptx,
                "regeneration must not mutate the checked-in presentation",
            )
            self.assertEqual(
                DOCX.read_bytes(),
                original_docx,
                "regeneration must not mutate the checked-in narrative",
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(output_pptx.is_file(), result.stdout)
            self.assertTrue(output_docx.is_file(), result.stdout)
            report_path = (
                toolchain_obj / "literate-ai-manager-overview" / "acceptance.json"
            )
            self.assertTrue(report_path.is_file(), result.stdout)
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["accepted"], report)
        self.assertEqual(report["counts"]["fail"], 0, report)
        self.assertNotIn("node build_deck.mjs", result.stdout)


if __name__ == "__main__":
    unittest.main()
