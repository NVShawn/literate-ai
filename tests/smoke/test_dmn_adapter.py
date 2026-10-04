"""Focused tests for the narrow production DMN adapter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.specifications.dmn import DmnError, DmnProvider

NS_13 = "https://www.omg.org/spec/DMN/20191111/MODEL/"


def dmn_document(
    *,
    namespace: str = NS_13,
    hit_policy: str = "UNIQUE",
    first: str = "[0..9]",
    second: str = "[10..20]",
    output_type: str = "string",
    first_output: str = '"low"',
    second_output: str = '"high"',
) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="{namespace}" id="definitions">
  <decision id="risk" name="Risk">
    <decisionTable id="risk-table" hitPolicy="{hit_policy}">
      <input id="age-input" label="Age">
        <inputExpression id="age-expression" typeRef="number">
          <text>Age</text>
        </inputExpression>
      </input>
      <output id="risk-output" label="Risk" typeRef="{output_type}"/>
      <rule id="young">
        <inputEntry><text>{first}</text></inputEntry>
        <outputEntry><text>{first_output}</text></outputEntry>
      </rule>
      <rule id="older">
        <inputEntry><text>{second}</text></inputEntry>
        <outputEntry><text>{second_output}</text></outputEntry>
      </rule>
    </decisionTable>
  </decision>
</definitions>
"""


class DmnProviderTests(unittest.TestCase):
    def assert_code(self, code: str, call) -> DmnError:
        with self.assertRaises(DmnError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_rejects_non_utf8_dtd_entity_xinclude_and_bad_xml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "nonutf.dmn": (b"\xff", "dmn.artifact_not_utf8"),
                "dtd.dmn": (
                    b'<!DOCTYPE definitions SYSTEM "x"><definitions/>',
                    "dmn.xml_unsafe",
                ),
                "entity.dmn": (b'<!ENTITY x "y"><definitions/>', "dmn.xml_unsafe"),
                "include.dmn": (
                    dmn_document()
                    .replace(
                        "<decision ",
                        '<xi:include xmlns:xi="http://www.w3.org/2001/XInclude" '
                        'href="x"/><decision ',
                    )
                    .encode(),
                    "dmn.xml_unsafe",
                ),
                "bad.dmn": (b"<definitions>", "dmn.xml_invalid"),
            }
            for name, (content, code) in cases.items():
                with self.subTest(name=name):
                    (root / name).write_bytes(content)
                    self.assert_code(
                        code, lambda name=name: DmnProvider().load(root, [name])
                    )

    def test_loads_the_reviewed_real_dmn_prototype(self) -> None:
        root = Path(__file__).resolve().parents[2]
        loaded = DmnProvider().load(
            root, ["docs/roadmap/0.6.0-dmn-prototype/risk-category.dmn"]
        )
        self.assertEqual(len(loaded.specification_set.requirements[0].scenarios), 5)


if __name__ == "__main__":
    unittest.main()
