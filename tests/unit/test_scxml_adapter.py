"""Focused safety, structure, and replay tests for the narrow SCXML provider."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.specifications.registry import load_specification_provider
from literate_ai.adapters.specifications.scxml import (
    ScxmlError,
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

    def test_rejects_unsafe_paths_xml_and_wrong_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "outside.scxml"
            outside.write_text(CHART, encoding="utf-8")
            with self.assertRaisesRegex(ScxmlError, "Unsafe artifact path"):
                ScxmlProvider().load(root, ["../outside.scxml"])

            (root / "bad.scxml").write_text(
                '<!DOCTYPE scxml [<!ENTITY x "unsafe">]><scxml/>', encoding="utf-8"
            )
            with self.assertRaisesRegex(ScxmlError, "DTD and ENTITY"):
                ScxmlProvider().load(root, ["bad.scxml"])

            (root / "bad.scxml").write_text(
                '<scxml xmlns="urn:not-scxml" version="1.0"/>', encoding="utf-8"
            )
            with self.assertRaisesRegex(ScxmlError, "W3C SCXML namespace"):
                ScxmlProvider().load(root, ["bad.scxml"])

    def test_rejects_duplicate_ids_bad_targets_and_unreachable_states(self) -> None:
        cases = (
            (
                '<scxml xmlns="http://www.w3.org/2005/07/scxml" '
                'version="1.0" initial="a">'
                '<state id="a"/><state id="a"/></scxml>',
                "not unique",
            ),
            (
                '<scxml xmlns="http://www.w3.org/2005/07/scxml" '
                'version="1.0" initial="a"><state id="a"><transition '
                'event="go" target="missing"/></state></scxml>',
                "Unknown transition target",
            ),
            (
                '<scxml xmlns="http://www.w3.org/2005/07/scxml" '
                'version="1.0" initial="a">'
                '<state id="a"/><state id="never"/></scxml>',
                "Unreachable state IDs",
            ),
        )
        for content, message in cases:
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / "bad.scxml").write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ScxmlError, message):
                    ScxmlProvider().load(root, ["bad.scxml"])

    def test_trace_schema_and_replay_subset_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            sidecar = json.loads((root / paths[1]).read_text(encoding="utf-8"))
            sidecar["scxml"] = "other.scxml"
            (root / paths[1]).write_text(json.dumps(sidecar), encoding="utf-8")
            with self.assertRaisesRegex(ScxmlError, "wrong chart"):
                ScxmlProvider().load(root, paths)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            (root / "media.scxml").write_text(
                CHART.replace('event="play"', 'cond="ready" event="play"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ScxmlError, "does not support cond"):
                ScxmlProvider().load(root, paths)

    def test_drift_and_declaration_shape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            loaded = ScxmlProvider().load(root, paths)
            (root / "media.scxml").write_text(CHART + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ScxmlError, "changed during operation"):
                loaded.require_unchanged(root)
            with self.assertRaisesRegex(ScxmlError, "exactly one"):
                ScxmlProvider().load(root, ["media.scxml", "media.scxml"])

    def test_shared_registry_loads_scxml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            loaded = load_specification_provider(
                "scxml", root, paths, id_prefix="example.media"
            )
            self.assertEqual(loaded.specification_set.provider_kind, "scxml")

    def test_trace_requires_exact_leaf_configuration_and_claimed_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            sidecar = json.loads((root / paths[1]).read_text(encoding="utf-8"))
            sidecar["traces"][0]["steps"][0]["expect_active"] = ["playing"]
            (root / paths[1]).write_text(json.dumps(sidecar), encoding="utf-8")
            with self.assertRaisesRegex(ScxmlError, "exactly one leaf"):
                ScxmlProvider().load(root, paths)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            sidecar = json.loads((root / paths[1]).read_text(encoding="utf-8"))
            sidecar["traces"][0]["steps"][0]["via_history"] = "last"
            (root / paths[1]).write_text(json.dumps(sidecar), encoding="utf-8")
            with self.assertRaisesRegex(ScxmlError, "did not use history"):
                ScxmlProvider().load(root, paths)

    def test_trace_rejects_an_event_without_an_enabled_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            sidecar = json.loads((root / paths[1]).read_text(encoding="utf-8"))
            sidecar["traces"][0]["steps"][0] = {
                "event": "typo",
                "expect_active": ["stopped", "audible"],
            }
            (root / paths[1]).write_text(json.dumps(sidecar), encoding="utf-8")
            with self.assertRaisesRegex(ScxmlError, "no enabled transition"):
                ScxmlProvider().load(root, paths)

    def test_trace_enforces_auto_event_intent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            sidecar = json.loads((root / paths[1]).read_text(encoding="utf-8"))
            sidecar["traces"][0]["steps"][0]["auto"] = True
            (root / paths[1]).write_text(json.dumps(sidecar), encoding="utf-8")
            with self.assertRaisesRegex(ScxmlError, "auto-event intent"):
                ScxmlProvider().load(root, paths)

    def test_trace_rejects_a_premature_auto_completion_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            sidecar = json.loads((root / paths[1]).read_text(encoding="utf-8"))
            sidecar["traces"][0]["steps"][0] = {
                "event": "done.state.on",
                "auto": True,
                "expect_active": ["stopped", "audible"],
            }
            (root / paths[1]).write_text(json.dumps(sidecar), encoding="utf-8")
            with self.assertRaisesRegex(ScxmlError, "unavailable auto event"):
                ScxmlProvider().load(root, paths)

    def test_shallow_history_reenters_recorded_children_at_initial_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            (root / paths[0]).write_text(
                CHART.replace('type="deep"', 'type="shallow"'), encoding="utf-8"
            )
            sidecar = json.loads((root / paths[1]).read_text(encoding="utf-8"))
            final = sidecar["traces"][0]["steps"][-1]
            final["expect_active"] = ["stopped", "audible"]
            final["expect_regions"] = {
                "transport": "stopped",
                "audio": "audible",
            }
            (root / paths[1]).write_text(json.dumps(sidecar), encoding="utf-8")

            ScxmlProvider().load(root, paths)

    def test_trace_rejects_unsupported_executable_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            (root / paths[0]).write_text(
                CHART.replace('<parallel id="on">', '<parallel id="on"><onentry/>'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ScxmlError, "does not support <onentry>"):
                ScxmlProvider().load(root, paths)

            (root / paths[0]).write_text(
                CHART.replace('event="play"', 'type="internal" event="play"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ScxmlError, "external transitions"):
                ScxmlProvider().load(root, paths)

    def test_illegal_structure_and_history_without_default_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.scxml").write_text(
                '<scxml xmlns="http://www.w3.org/2005/07/scxml" '
                'version="1.0" initial="a"><state id="a"><transition '
                'event="go" target="a"><transition event="nested" '
                'target="missing"/></transition></state></scxml>',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ScxmlError, "does not permit <transition>"):
                ScxmlProvider().load(root, ["bad.scxml"])

            (root / "bad.scxml").write_text(
                '<scxml xmlns="http://www.w3.org/2005/07/scxml" version="1.0" '
                'initial="a"><state id="a" initial="leaf"><history id="h"/>'
                '<state id="leaf"/></state></scxml>',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ScxmlError, "exactly one default"):
                ScxmlProvider().load(root, ["bad.scxml"])

    def test_initial_event_and_nonorthogonal_multi_targets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bad.scxml").write_text(
                '<scxml xmlns="http://www.w3.org/2005/07/scxml" version="1.0">'
                '<initial><transition event="later" target="a"/></initial>'
                '<state id="a"/></scxml>',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ScxmlError, "Invalid initial transition"):
                ScxmlProvider().load(root, ["bad.scxml"])

            (root / "bad.scxml").write_text(
                '<scxml xmlns="http://www.w3.org/2005/07/scxml" version="1.0" '
                'initial="a"><state id="a"><transition event="go" '
                'target="b c"/></state><state id="b"/><state id="c"/></scxml>',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ScxmlError, "orthogonal parallel regions"):
                ScxmlProvider().load(root, ["bad.scxml"])


if __name__ == "__main__":
    unittest.main()
