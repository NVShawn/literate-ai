"""Actual Cargo adoption keeps retained execution on its delegation Flavor."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support.fixtures_test_project_update_adapter import _origin
from tests.support.root_parent_adapter import RootParentProjectInitializationAdapter


class CargoWrapperAdoptionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cargo"), "Cargo required for native adoption")
    def test_native_cargo_baseline_and_wrapper_lock_bind_retained_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cargo-project"
            (root / "src").mkdir(parents=True)
            (root / "Cargo.toml").write_text(
                '[package]\nname="litai-adoption-fixture"\nversion="0.1.0"\n'
                'edition="2021"\n',
                encoding="utf-8",
            )
            original = (
                "fn main() {}\n"
                "#[cfg(test)] mod tests { #[test] fn native_case() { "
                "assert_eq!(2+2,4); } }\n"
            )
            (root / "src/main.rs").write_text(original, encoding="utf-8")
            result = RootParentProjectInitializationAdapter(
                standard_binding_provider=lambda: None,
                initialization_origin_provider=lambda: _origin("a", "1.4.0"),
            ).initialize(root, convert=True, source_intelligence_provider="none")
            manifest = json.loads(
                (root / "literate.project.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "+flavor://literate-ai/lang-rust", manifest["default_flavor_selectors"]
            )
            wrapper = root / "components/legacy-project-wrapper"
            self.assertEqual(
                (wrapper / "implementation/src/main.rs").read_text(encoding="utf-8"),
                original,
            )
            lock = json.loads(
                (wrapper / "component.lock.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(lock["nodes"]), 1)
            self.assertEqual(
                {
                    slot["slot"]["axis"]: [item["value"] for item in slot["selected"]]
                    for slot in lock["nodes"][0]["target_flavor_selection"]["slots"]
                },
                {"build.system": ["legacy-shim"]},
            )
            self.assertEqual(result["harness_baseline"]["state"], "passed")
            self.assertEqual(result["harness_parity"]["state"], "passed")
