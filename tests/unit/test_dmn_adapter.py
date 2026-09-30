"""Focused tests for the narrow production DMN adapter."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.specifications.dmn import DmnError, DmnProvider
from literate_ai.adapters.specifications.registry import load_specification_provider

NS_13 = "https://www.omg.org/spec/DMN/20191111/MODEL/"
NS_15 = "https://www.omg.org/spec/DMN/20230324/MODEL/"


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
    def load_text(self, text: str, path: str = "decisions/risk.dmn"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        return root, target, DmnProvider().load(root, [path])

    def assert_code(self, code: str, call) -> DmnError:
        with self.assertRaises(DmnError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_loads_supported_namespaces_into_exact_deterministic_contracts(
        self,
    ) -> None:
        for namespace in (NS_13, NS_15):
            with self.subTest(namespace=namespace):
                text = dmn_document(namespace=namespace)
                root, _target, loaded = self.load_text(text)
                specification_set = loaded.specification_set
                self.assertEqual(
                    DmnProvider.provider_id, "specification-provider:dmn@1"
                )
                self.assertEqual(specification_set.provider_kind, "dmn")
                self.assertEqual(specification_set.provider_version, "1")
                self.assertIsNone(loaded.context_document)
                self.assertEqual(
                    loaded.contents, (("decisions/risk.dmn", text.encode()),)
                )
                self.assertEqual(
                    specification_set.artifacts[0].identity.digest,
                    hashlib.sha256(text.encode()).hexdigest(),
                )
                requirement = specification_set.requirements[0]
                self.assertEqual(len(requirement.scenarios), 2)
                self.assertEqual(requirement.scenarios[0].when, ("Age matches [0..9]",))
                self.assertEqual(requirement.scenarios[0].then, ('Risk = "low"',))
                repeated = DmnProvider().load(root, ["decisions/risk.dmn"])
                self.assertEqual(specification_set, repeated.specification_set)

    def test_identical_artifacts_have_distinct_artifact_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = dmn_document()
            (root / "first.dmn").write_text(text, encoding="utf-8")
            (root / "second.dmn.xml").write_text(text, encoding="utf-8")
            loaded = DmnProvider().load(root, ["first.dmn", "second.dmn.xml"])
            requirements = loaded.specification_set.requirements
            self.assertEqual(len(requirements), 2)
            self.assertNotEqual(
                requirements[0].requirement_id, requirements[1].requirement_id
            )

    def test_supports_multiple_decisions_and_typed_outputs(self) -> None:
        second = (
            dmn_document(
                namespace=NS_15,
                output_type="boolean",
                first_output="false",
                second_output="true",
            )
            .replace("<definitions", "<fragment", 1)
            .replace("</definitions>", "</fragment>")
        )
        decision = second[second.index("  <decision") : second.index("</fragment>")]
        number_decision = (
            decision.replace('id="risk"', 'id="score"', 1)
            .replace('name="Risk"', 'name="Score"', 1)
            .replace('id="young"', 'id="score-low"', 1)
            .replace('id="older"', 'id="score-high"', 1)
            .replace('typeRef="boolean"', 'typeRef="number"')
            .replace("<text>false</text>", "<text>1.5</text>")
            .replace("<text>true</text>", "<text>2</text>")
        )
        text = dmn_document(
            namespace=NS_15,
            output_type="boolean",
            first_output="false",
            second_output="true",
        )
        text = text.replace("</definitions>", number_decision + "</definitions>")
        _root, _target, loaded = self.load_text(text, "all.dmn.xml")
        requirement = loaded.specification_set.requirements[0]
        self.assertEqual(len(requirement.scenarios), 4)

    def test_rejects_unsafe_non_exact_and_escaping_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (
                "../risk.dmn",
                "risk.xml",
                "./risk.dmn",
                "risk.DMN",
                "a\\risk.dmn",
            ):
                with self.subTest(path=path):
                    self.assert_code(
                        "dmn.path_invalid",
                        lambda path=path: DmnProvider().load(root, [path]),
                    )
            (root / "risk.dmn").write_text(dmn_document(), encoding="utf-8")
            self.assert_code(
                "dmn.artifact_duplicate",
                lambda: DmnProvider().load(root, ["risk.dmn", "risk.dmn"]),
            )

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

    def test_rejects_structure_policy_types_ids_arity_and_feel_outside_profile(
        self,
    ) -> None:
        cases = {
            "dmn.namespace_unsupported": dmn_document(namespace="urn:wrong"),
            "dmn.expression_language_unsupported": dmn_document().replace(
                '<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/"',
                '<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/" '
                'expressionLanguage="urn:not-feel"',
            ),
            "dmn.decisions_empty": dmn_document().replace(
                dmn_document()[
                    dmn_document().index("  <decision") : dmn_document().index(
                        "</definitions>"
                    )
                ],
                "",
            ),
            "dmn.decision_table_count": dmn_document().replace(
                '<decisionTable id="risk-table" hitPolicy="UNIQUE">',
                '<decisionTable id="risk-table" hitPolicy="UNIQUE">'
                "</decisionTable><decisionTable>",
            ),
            "dmn.hit_policy_unsupported": dmn_document(hit_policy="FIRST"),
            "dmn.input_type_unsupported": dmn_document().replace(
                'typeRef="number">\n          <text>Age',
                'typeRef="string">\n          <text>Age',
            ),
            "dmn.input_expression_invalid": dmn_document().replace(
                "<text>Age</text>", ""
            ),
            "dmn.input_expression_unsupported": dmn_document().replace(
                "<text>Age</text>", "<text>customer.age + unknown()</text>"
            ),
            "dmn.attribute_unsupported": dmn_document().replace(
                'typeRef="string"/>', 'typeRef="string" isCollection="true"/>'
            ),
            "dmn.output_type_unsupported": dmn_document(output_type="date"),
            "dmn.id_missing": dmn_document().replace(' id="risk"', "", 1),
            "dmn.rule_id_duplicate": dmn_document().replace('id="older"', 'id="young"'),
            "dmn.rule_arity": dmn_document().replace(
                "<inputEntry><text>[10..20]</text></inputEntry>", ""
            ),
            "dmn.feel_unsupported": dmn_document(first="> 3"),
            "dmn.feel_interval_invalid": dmn_document(first="[9..0]"),
            "dmn.output_literal_invalid": dmn_document(first_output="low"),
        }
        for code, text in cases.items():
            with self.subTest(code=code):
                self.assert_code(code, lambda text=text: self.load_text(text))

    def test_rejects_pairwise_unique_overlap_inclusive_boundaries_and_wildcards(
        self,
    ) -> None:
        for first, second in (("[0..10]", "[10..20]"), ("-", "[10..20]")):
            with self.subTest(first=first):
                error = self.assert_code(
                    "dmn.unique_overlap",
                    lambda first=first, second=second: self.load_text(
                        dmn_document(first=first, second=second)
                    ),
                )
                self.assertEqual(
                    str(error),
                    "UNIQUE rules 'young' and 'older' overlap in decision "
                    "'risk': decisions/risk.dmn",
                )

    def test_enforces_budgets_and_detects_drift(self) -> None:
        text = dmn_document()
        root, target, loaded = self.load_text(text)
        self.assert_code(
            "dmn.artifact_size_limit",
            lambda: DmnProvider(maximum_artifact_bytes=10).load(
                root, ["decisions/risk.dmn"]
            ),
        )
        target.write_text(text + " ", encoding="utf-8")
        self.assert_code("dmn.artifact_changed", lambda: loaded.require_unchanged(root))
        target.unlink()
        self.assert_code("dmn.artifact_missing", lambda: loaded.require_unchanged(root))

    def test_shared_registry_loads_dmn(self) -> None:
        root, _target, _loaded = self.load_text(dmn_document())
        loaded = load_specification_provider(
            "dmn", root, ["decisions/risk.dmn"], id_prefix="example.risk"
        )
        self.assertEqual(loaded.specification_set.provider_kind, "dmn")

    def test_loads_the_reviewed_real_dmn_prototype(self) -> None:
        root = Path(__file__).resolve().parents[2]
        loaded = DmnProvider().load(
            root, ["docs/roadmap/0.6.0-dmn-prototype/risk-category.dmn"]
        )
        self.assertEqual(len(loaded.specification_set.requirements[0].scenarios), 5)


if __name__ == "__main__":
    unittest.main()
