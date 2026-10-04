"""Focused safety, structure, and replay tests for the narrow SCXML provider."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.specifications.registry import load_specification_provider
from literate_ai.adapters.specifications.scxml import (
    SCXMLProvider,
    ScxmlProvider,
)

CHART = """<?xml version="1.0" encoding="UTF-8"?>
<scxml xmlns="http://www.w3.org/2005/07/scxml" version="1.0" initial="on">
  <parallel id="on">
    <history id="last" type="deep"><transition target="transport audio"/></history>
    <state id="transport" initial="stopped">
      <state id="stopped"><transition event="play" target="playing"/></state>
      <state id="playing"/>
    </state>
    <state id="audio" initial="audible">
      <state id="audible"><transition event="mute" target="muted"/></state>
      <state id="muted"/>
    </state>
    <transition event="power" target="off"/>
  </parallel>
  <state id="off"><transition event="resume" target="last"/></state>
</scxml>
"""


class ScxmlProviderTests(unittest.TestCase):
    def _fixture(self, root: Path) -> list[str]:
        (root / "media.scxml").write_bytes(CHART.encode("utf-8"))
        sidecar = {
            "$schema": "litai-scxml-trace-sidecar/v1",
            "scxml": "media.scxml",
            "traces": [
                {
                    "id": "parallel-and-shallow-history",
                    "steps": [
                        {"event": "play", "expect_active": ["playing", "audible"]},
                        {"event": "mute", "expect_active": ["playing", "muted"]},
                        {"event": "power", "expect_active": ["off"]},
                        {
                            "event": "resume",
                            "expect_active": ["playing", "muted"],
                            "via_history": "last",
                            "expect_regions": {
                                "transport": "playing",
                                "audio": "muted",
                            },
                        },
                    ],
                }
            ],
        }
        (root / "media.trace.json").write_text(json.dumps(sidecar), encoding="utf-8")
        return ["media.scxml", "media.trace.json"]

    def test_parallel_and_shallow_history_replay_produces_exact_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            loaded = ScxmlProvider().load(root, paths, baseline_id="base")

            self.assertEqual(
                ScxmlProvider.provider_id, "specification-provider:scxml@1"
            )
            self.assertIs(SCXMLProvider, ScxmlProvider)
            self.assertEqual(
                [item.uri for item in loaded.specification_set.artifacts], paths
            )
            self.assertEqual(loaded.specification_set.provider_kind, "scxml")
            self.assertEqual(len(loaded.specification_set.requirements), 2)
            self.assertIsNone(loaded.context_document)
            self.assertEqual(loaded.contents[0][1].decode("utf-8"), CHART)
            loaded.require_unchanged(root)

    def test_shared_registry_loads_scxml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            loaded = load_specification_provider(
                "scxml", root, paths, id_prefix="example.media"
            )
            self.assertEqual(loaded.specification_set.provider_kind, "scxml")


if __name__ == "__main__":
    unittest.main()
