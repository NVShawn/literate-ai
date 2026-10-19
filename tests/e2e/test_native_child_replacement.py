"""Real signed promotion and reviewed replacement retain exact prior authority."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import native_child_replacement as replacement
from literate_ai.adapters.conversion_authority import (
    NATIVE_PROJECTS_FILE,
    _native_project_roots,
)
from literate_ai.projects import load_project
from tests.support.fixtures_test_cli_arbitrary_source import (
    KEY,
    invoke,
    reviewed_static_graph,
)
from tests.support.fixtures_test_retained_harness_receipts import (
    _adapter,
    _legacy_project,
    _selectors,
)


class NativeChildReplacementJourneyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.parent = self.root / "adopted"
        _legacy_project(self.parent)
        (self.parent / "app.py").write_text("def value():\n    return 7\n")
        package = self.parent / "tiny"
        package.mkdir()
        (package / "__init__.py").write_text("")
        for module in ("app", "api"):
            (package / (module + ".py")).write_text("def value():\n    return 7\n")
        _adapter().initialize(
            self.parent,
            flavor_selectors=_selectors(self.parent),
            source_intelligence_provider="none",
            convert=True,
        )
        candidate = self.root / "retained.json"
        self.success(
            "project",
            "test-receipt",
            "run-retained",
            str(candidate),
            "--project",
            str(self.parent),
            "--worker-id",
            "local",
        )
        self.success(
            "project",
            "test-receipt",
            "update",
            str(candidate),
            "--project",
            str(self.parent),
        )
        self.success(
            "project",
            "convert-stage",
            "advance",
            "--to",
            "retained",
            "--project",
            str(self.parent),
        )
        self.source = self.parent / "components/legacy-project-wrapper/implementation"
        self.key = self.root / "trust.key"
        self.key.write_bytes(KEY)
        bundle, review = self.reviewed("initial")
        if (
            self._testMethodName
            == "test_recorded_rebind_preserves_original_promotion_audit_as_history"
        ):
            from tests.smoke.test_standard_lifecycle_rebind import distribution

            with patch(
                "literate_ai.adapters.project_initialization.observe_installed_framework_distribution",
                return_value=distribution("old"),
            ):
                first = self.success(*self.arguments(bundle, review, "first"))
        else:
            first = self.success(*self.arguments(bundle, review, "first"))
        self.child = Path(first["promoted_project"]["project_target"])
        self.registry = self.parent / NATIVE_PROJECTS_FILE
        self.success(
            "project",
            "convert-stage",
            "advance",
            "--to",
            "drafted",
            "--project",
            str(self.parent),
        )

    def success(self, *args):
        status, output, errors = invoke(*args)
        self.assertEqual((status, errors), (0, ""), output or errors)
        return json.loads(output)["result"]

    def reviewed(self, name):
        attestation = self.success(
            "spec",
            "attest",
            str(self.source),
            "--signer",
            "reviewer",
            "--machine",
            "fixture",
            "--key",
            str(self.key),
        )
        attested = self.root / (name + "-attestation.json")
        attested.write_text(json.dumps(attestation))
        bundle = self.success(
            "spec",
            "derive",
            str(self.source),
            "--attestation",
            str(attested),
            "--trust-key",
            str(self.key),
        )
        bundle_path = self.root / (name + "-bundle.json")
        bundle_path.write_text(json.dumps(bundle))
        graph = reviewed_static_graph(bundle)
        graph["nodes"][0]["library_imports"] = [
            {
                "language": "python",
                "package": "tiny",
                "capability": capability,
                "module": "tiny.app" if name == "initial" else "tiny.api",
                "symbols": ["value"],
            }
            for capability in graph["nodes"][0]["provided_capabilities"]
        ]
        graph["nodes"][0]["capability_contracts"][0]["contract"] += (
            " Reviewed revision " + name + "."
        )
        graph_path = self.root / (name + "-graph.json")
        graph_path.write_text(json.dumps(graph))
        arguments = [
            "spec",
            "review",
            str(bundle_path),
            "--actor",
            "reviewer",
            "--key",
            str(self.key),
            "--component-graph",
            str(graph_path),
        ]
        for uncertainty in bundle["review_gate"]["blocking_uncertainty_ids"]:
            arguments.extend(("--resolve", uncertainty))
        review = self.success(*arguments)
        review_path = self.root / (name + "-review.json")
        review_path.write_text(json.dumps(review))
        return bundle_path, review_path

    def arguments(self, bundle, review, target):
        return [
            "spec",
            "accept",
            str(self.source),
            str(bundle),
            "--review",
            str(review),
            "--target",
            str(self.root / target),
            "--integrate-project",
            str(self.parent),
            "--trust-key",
            str(self.key),
        ]

    def planned(self):
        bundle, review = self.reviewed("revised")
        args = self.arguments(bundle, review, "second")
        plan = self.success(*args, "--plan-native-child-replacement")
        path = self.root / "replacement.json"
        path.write_text(json.dumps(plan))
        return args, path, plan

    def test_signed_replacement_retains_exact_old_promotion_and_current_registry(self):
        before = replacement._tree(self.child)
        registry = self.registry.read_bytes()
        args, path, plan = self.planned()
        self.assertFalse((self.root / "second").exists())
        self.assertEqual(replacement._tree(self.child), before)
        self.assertEqual(self.registry.read_bytes(), registry)
        accepted = self.success(*args, "--replace-native-child", str(path))
        result = accepted["promoted_project"]
        retained = self.parent / result["replacement"]["recovery_path"]
        self.assertEqual(replacement._tree(retained / "old"), before)
        self.assertEqual(
            json.loads((retained / "record.json").read_text())["plan"], plan
        )
        self.assertTrue((retained / "completed.json").is_file())
        self.assertEqual(
            _native_project_roots(load_project(self.parent)), (self.child,)
        )
        self.assertNotEqual(replacement._tree(self.child), before)
        from literate_ai.adapters.component_markdown import parse_component_markdown

        component = self.child / result["component"]
        authoring = parse_component_markdown(
            component, component.read_text(), project_root=self.child
        )
        self.assertEqual(
            {item.module for item in authoring.library_imports}, {"tiny.api"}
        )

    def test_changed_registry_refuses_before_creating_the_new_accepted_set(self):
        args, path, _plan = self.planned()
        self.registry.write_bytes(b"\n" + self.registry.read_bytes())
        before = self.registry.read_bytes(), replacement._tree(self.child)
        status, _output, _errors = invoke(*args, "--replace-native-child", str(path))
        self.assertEqual(status, 2)
        self.assertFalse((self.root / "second").exists())
        self.assertEqual(
            (self.registry.read_bytes(), replacement._tree(self.child)), before
        )

    def test_recorded_rebind_preserves_original_promotion_audit_as_history(self):
        from literate_ai.adapters.native_project_updates import (
            apply_registered_standard_rebind,
            plan_registered_standard_rebind,
        )
        from tests.smoke.test_standard_lifecycle_rebind import distribution, origin

        observers = {
            "distribution_observer": lambda: distribution("new"),
            "origin_observer": origin,
        }
        plan = plan_registered_standard_rebind(self.parent, self.child, **observers)
        rebound = apply_registered_standard_rebind(
            self.parent, self.child, plan, authorize_rebind=True, **observers
        )
        original = replacement._tree(self.child)
        args, path, planned = self.planned()
        self.assertEqual(len(planned["promotion"]["historical_rebind_records"]), 1)
        self.assertTrue((self.parent / rebound["recovery"]["path"]).is_file())
        accepted = self.success(*args, "--replace-native-child", str(path))
        retained = (
            self.parent / accepted["promoted_project"]["replacement"]["recovery_path"]
        )
        self.assertEqual(replacement._tree(retained / "old"), original)

    def test_source_drift_refuses_before_replacement(self):
        args, path, _plan = self.planned()
        before = self.registry.read_bytes(), replacement._tree(self.child)
        (self.source / "app.py").write_text("def value():\n    return 8\n")
        status, _output, errors = invoke(*args, "--replace-native-child", str(path))
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(errors)["error"]["code"], "cli.source_drift")
        self.assertEqual(
            (self.registry.read_bytes(), replacement._tree(self.child)), before
        )
        self.assertFalse((self.root / "second").exists())

    def test_indirect_child_content_cannot_be_retained_or_replaced(self):
        bundle, review = self.reviewed("revised")
        link = self.child / "unsafe-link"
        try:
            link.symlink_to(self.source, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        before = self.registry.read_bytes()
        status, _output, errors = invoke(
            *self.arguments(bundle, review, "second"), "--plan-native-child-replacement"
        )
        self.assertEqual(status, 2)
        self.assertEqual(
            json.loads(errors)["error"]["code"], "promotion.native_child_unsafe_tree"
        )
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertFalse((self.root / "second").exists())

    def test_qualified_projection_is_refused_without_overwriting_child(self):
        from types import SimpleNamespace

        from literate_ai.contracts.authority import ComponentAuthorityState

        bundle, review = self.reviewed("revised")
        before = self.registry.read_bytes(), replacement._tree(self.child)
        with patch.object(
            replacement.FileAuthorityProjectionStore,
            "history",
            return_value=(
                SimpleNamespace(
                    state=ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE
                ),
            ),
        ):
            status, _output, errors = invoke(
                *self.arguments(bundle, review, "second"),
                "--plan-native-child-replacement",
            )
        self.assertEqual(status, 2)
        self.assertEqual(
            json.loads(errors)["error"]["code"], "promotion.native_child_qualified"
        )
        self.assertEqual(
            (self.registry.read_bytes(), replacement._tree(self.child)), before
        )

    def test_post_registration_failure_restores_exact_old_tree_and_registry(self):
        from literate_ai.adapters.conversion_authority import ConversionAuthorityError

        args, path, _plan = self.planned()
        before = self.registry.read_bytes(), replacement._tree(self.child)
        original = replacement._native_project_roots

        def reject_new_registry(parent):
            if replacement._tree(self.child) != before[1]:
                raise ConversionAuthorityError(
                    "conversion_authority.native_registry_invalid",
                    "injected final validation failure",
                )
            return original(parent)

        with patch.object(
            replacement, "_native_project_roots", side_effect=reject_new_registry
        ):
            status, _output, _errors = invoke(
                *args, "--replace-native-child", str(path)
            )
        self.assertEqual(status, 2)
        self.assertEqual(
            (self.registry.read_bytes(), replacement._tree(self.child)), before
        )
        self.assertFalse((self.root / "second").exists())

    def test_changed_child_content_is_never_silently_replaced(self):
        args, path, _plan = self.planned()
        manifest = next(self.child.glob("components/*/component.md"))
        manifest.write_bytes(manifest.read_bytes() + b"\nConcurrent authored change.\n")
        before = self.registry.read_bytes(), replacement._tree(self.child)
        status, _output, _errors = invoke(*args, "--replace-native-child", str(path))
        self.assertEqual(status, 2)
        self.assertEqual(
            (self.registry.read_bytes(), replacement._tree(self.child)), before
        )
        self.assertFalse((self.root / "second").exists())

    def test_changed_signed_review_refuses_before_replacement(self):
        args, path, _plan = self.planned()
        review_path = Path(args[args.index("--review") + 1])
        review = json.loads(review_path.read_text())
        review["review"]["actor"] = "unsigned-actor"
        review_path.write_text(json.dumps(review))
        before = self.registry.read_bytes(), replacement._tree(self.child)
        status, _output, errors = invoke(*args, "--replace-native-child", str(path))
        self.assertEqual(status, 2)
        self.assertEqual(
            json.loads(errors)["error"]["code"], "review.attestation_invalid"
        )
        self.assertEqual(
            (self.registry.read_bytes(), replacement._tree(self.child)), before
        )
        self.assertFalse((self.root / "second").exists())

    def test_concurrent_new_child_edit_preserves_both_trees_and_reports_conflict(self):
        args, path, plan = self.planned()
        before = replacement._tree(self.child)
        original = replacement._replace_metadata

        def register_then_concurrent_edit(*arguments):
            original(*arguments)
            (self.child / "concurrent.txt").write_text("external edit")

        with patch.object(
            replacement, "_replace_metadata", side_effect=register_then_concurrent_edit
        ):
            status, _output, errors = invoke(*args, "--replace-native-child", str(path))
        self.assertEqual(status, 2)
        self.assertEqual(
            json.loads(errors)["error"]["code"],
            "promotion.native_child_rollback_conflict",
        )
        self.assertEqual((self.child / "concurrent.txt").read_text(), "external edit")
        records = list(
            (self.parent / ".literate/native-updates").glob("r-*/record.json")
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())["plan"], plan)
        self.assertEqual(replacement._tree(records[0].parent / "old"), before)
        self.assertFalse((records[0].parent / "completed.json").exists())

    def test_failed_registry_write_restores_old_child_and_registration(self):
        args, path, _plan = self.planned()
        before = self.registry.read_bytes(), replacement._tree(self.child)
        with patch.object(
            replacement,
            "_replace_metadata",
            side_effect=OSError("injected registry failure"),
        ):
            status, _output, _errors = invoke(
                *args, "--replace-native-child", str(path)
            )
        self.assertEqual(status, 2)
        self.assertEqual(
            (self.registry.read_bytes(), replacement._tree(self.child)), before
        )
        self.assertFalse((self.root / "second").exists())
        self.assertEqual(
            _native_project_roots(load_project(self.parent)), (self.child,)
        )
        accepted = self.success(*args, "--replace-native-child", str(path))
        self.assertTrue(
            (
                self.parent
                / accepted["promoted_project"]["replacement"]["recovery_path"]
                / "old"
            ).is_dir()
        )
