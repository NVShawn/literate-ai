"""Public signed replacement seals each explicitly selected authoring artifact."""

import hashlib
import json
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.authority import FileAuthorityProjectionStore
from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.application.source_promotion import SourcePromotionService
from tests.e2e import test_native_child_replacement as replacement_fixture
from tests.support.fixtures_test_cli_arbitrary_source import invoke


class PromotionAuthoringJourneyTests(unittest.TestCase):
    setUp = replacement_fixture.NativeChildReplacementJourneyTests.setUp
    success = replacement_fixture.NativeChildReplacementJourneyTests.success
    reviewed = replacement_fixture.NativeChildReplacementJourneyTests.reviewed
    arguments = replacement_fixture.NativeChildReplacementJourneyTests.arguments

    def test_public_selection_seals_all_three_inputs_and_mutations_refuse(self):
        from literate_ai.cli.source_to_specification import (
            _PROMOTION_TEMPLATE_AUTHORING,
        )

        lineage = (self.parent / ".literate/repository-lineage.json").read_bytes()
        bundle, review = self.reviewed("revised")
        args = self.arguments(bundle, review, "selected")
        args += ["--authoring-template", "installed"]
        plan = self.success(*args, "--plan-native-child-replacement")
        path = self.root / "selected-plan.json"
        path.write_text(json.dumps(plan))
        status, _, errors = invoke(
            *args,
            "--authoring-template",
            "inherited",
            "--replace-native-child",
            str(path),
        )
        self.assertEqual(status, 2)
        self.assertEqual(
            json.loads(errors)["error"]["code"], "promotion.replacement_plan_stale"
        )
        from literate_ai.cli.source_to_specification import _select_promotion_authoring

        # Supply actual canonical875 inherited files at the staging boundary;
        # public selection, locks, promotion audit and mutation checks stay real.
        old = json.loads(
            (
                Path(__file__).parents[1] / "fixtures/promotion_authoring_875.json"
            ).read_bytes()
        )
        old_contents = {row["path"]: row["utf8"].encode() for row in old["files"]}
        for row in old["files"]:
            self.assertEqual(
                hashlib.sha256(old_contents[row["path"]]).hexdigest(), row["sha256"]
            )

        def old_parent_selection(root, selection):
            for relative, content in old_contents.items():
                staged = root / relative
                self.assertTrue(staged.is_file())
                staged.write_bytes(content)
            return _select_promotion_authoring(root, selection)

        with patch(
            "literate_ai.cli.source_to_specification._select_promotion_authoring",
            side_effect=old_parent_selection,
        ):
            result = self.success(*args, "--replace-native-child", str(path))
        selected = result["promoted_project"]["authoring_template"]
        self.assertEqual(selected["selection"], "installed")
        self.assertEqual(
            (self.parent / ".literate/repository-lineage.json").read_bytes(), lineage
        )
        component = self.child / result["promoted_project"]["component"]
        authoring = parse_component_markdown(
            component, component.read_text(), project_root=self.child
        )
        projection = FileAuthorityProjectionStore(self.child).current(
            authoring.coordinate.uri
        )
        service = SourcePromotionService()
        verified = service.verify_evidence(self.child, projection)
        self.assertEqual(len(selected["files"]), 12)
        for relative in selected["files"]:
            original = (self.child / relative).read_bytes()
            if relative in _PROMOTION_TEMPLATE_AUTHORING:
                self.assertEqual(
                    original,
                    files("literate_ai.project_template")
                    .joinpath(relative)
                    .read_bytes(),
                )
            else:
                self.assertEqual(
                    original.split(b"\n---\n", 1)[1],
                    old_contents[relative].split(b"\n---\n", 1)[1],
                )
            verified.audit_containing(relative)
            (self.child / relative).write_bytes(b"!" + original[1:])
            try:
                with self.assertRaises(ValueError) as caught:
                    service.verify_evidence(self.child, projection)
                self.assertEqual(
                    caught.exception.code, "promotion.audit_target_changed"
                )
            finally:
                (self.child / relative).write_bytes(original)
        service.verify_evidence(self.child, projection)
