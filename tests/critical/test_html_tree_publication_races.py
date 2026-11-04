"""Actual owned POSIX publication faults; framework measurement is a stand-in.

No installed-distribution, SDK, browser, scale or complete-ADR admission is claimed.
All capture, CAS, output-custody and publication paths are the production owners.
"""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import html_tree_apply as apply
from literate_ai.adapters import html_tree_inputs as inputs
from literate_ai.adapters import html_tree_output_initialization as output
from literate_ai.adapters import html_tree_render as render
from literate_ai.adapters import native_sdk_tree_custody as capture
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.html_trees import HtmlTreeLimits, HtmlTreeViewDeclaration
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.projects import PinnedInputClosure
from literate_ai.storage import FileSystemCAS


@unittest.skipUnless(os.name == "posix", "native initial output requires POSIX custody")
class HtmlTreePublicationRaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        framework = SimpleNamespace(
            framework_distribution_identity=canonical_identity("test measurement only"),
            schema_catalog_release="2.0.0",
        )
        self.enterContext(patch.object(render, "_framework", return_value=framework))
        self.enterContext(patch.object(apply, "_framework", return_value=framework))
        self.limits = HtmlTreeLimits(
            RetainedProjectLimits(100, 1048576, 16777216),
            32768,
            524288,
            65536,
            67108864,
            16,
            2,
            1,
            128,
            32768,
            4194304,
            8388608,
        )

    def case(self, name, *, limits=None):
        root = self.root / name
        root.mkdir(mode=0o700)
        source = root / "source"
        source.mkdir(mode=0o700)
        (source / "empty").mkdir()
        (source / "payload.txt").write_bytes(b"original payload\n")
        declaration = HtmlTreeViewDeclaration(
            "fixture-tree", ".", ".", self.limits if limits is None else limits, True
        )
        path = root / "declaration.json"
        path.write_bytes(canonical_json_bytes(declaration.to_dict()))
        pinned = PinnedInputClosure(
            maximum_files=1, maximum_file_bytes=65536, maximum_total_bytes=65536
        )
        raw = pinned.pin(path, boundary=root, label="owned-test-declaration")
        declaration = HtmlTreeViewDeclaration.from_dict(json.loads(raw))
        plan = render.plan_html_tree_render(source, declaration)
        return SimpleNamespace(
            source=source,
            declaration=declaration,
            guard=pinned.require_unchanged,
            acknowledgement=canonical_identity(plan),
            state=root / "state",
            namespace=source / ".litai-tree-fixture-tree",
            index=source / "index.html",
        )

    def publish(self, case):
        return apply.apply_initial_html_tree(
            case.source,
            case.declaration,
            acknowledged_plan_identity=case.acknowledgement,
            state_root=case.state,
            require_declaration_current=case.guard,
        )

    def require_unpublished(self, case):
        self.assertFalse(case.index.exists())
        self.assertTrue((case.namespace / "owner.json").is_file())
        request = case.declaration.resolve(
            render._renderer_identity(render._framework())
        )
        with self.assertRaisesRegex(ValueError, "initial-output-already-exists"):
            output.plan_initial_html_tree_output(case.source, request)

    def test_source_edit_during_real_recapture_refuses_without_index(self):
        case = self.case("recapture")
        real_capture = inputs._capture_composed_native_tree
        real_stream = capture._stream
        calls, edits = [], []

        def recapture(*args, **options):
            calls.append(True)
            return real_capture(*args, **options)

        def stream_then_edit(*args, **options):
            result = real_stream(*args, **options)
            if len(calls) >= 2 and not edits:
                (case.source / "payload.txt").write_bytes(
                    b"test-owned concurrent edit\n"
                )
                edits.append(True)
            return result

        with (
            patch.object(inputs, "_capture_composed_native_tree", recapture),
            patch.object(capture, "_stream", stream_then_edit),
            self.assertRaises(ValueError),
        ):
            self.publish(case)
        self.assertTrue(edits)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(
            (case.source / "payload.txt").read_bytes(), b"test-owned concurrent edit\n"
        )
        self.require_unpublished(case)

    def test_equal_byte_foreign_index_before_commit_is_preserved(self):
        case = self.case("before")
        real_publish = output.publish_html
        foreign = []

        def inject(target, content, previous, **options):
            staged = options["revalidate_staged"]

            def stage_then_race(*args):
                staged(*args)
                case.index.write_bytes(content)
                foreign.append((case.index.stat().st_ino, content))

            options["revalidate_staged"] = stage_then_race
            return real_publish(target, content, previous, **options)

        with (
            patch.object(output, "publish_html", inject),
            self.assertRaises(ValueError),
        ):
            self.publish(case)
        self.assertEqual(len(foreign), 1)
        self.assertEqual(
            (case.index.stat().st_ino, case.index.read_bytes()), foreign[0]
        )
        self.assertEqual(
            (case.source / "payload.txt").read_bytes(), b"original payload\n"
        )

    def test_equal_byte_foreign_index_after_commit_cannot_report_success(self):
        case = self.case("after")
        real_publish = output.publish_html
        foreign = []

        def inject(target, content, previous, **options):
            snapshot = real_publish(target, content, previous, **options)
            replacement = self.root / "foreign-index"
            replacement.write_bytes(content)
            inode = replacement.stat().st_ino
            self.assertNotEqual(inode, target.stat().st_ino)
            replacement.replace(target)
            foreign.append((inode, content))
            return snapshot

        with (
            patch.object(output, "publish_html", inject),
            self.assertRaisesRegex(ValueError, "initial-index-publication-differs"),
        ):
            self.publish(case)
        self.assertEqual(len(foreign), 1)
        self.assertEqual(
            (case.index.stat().st_ino, case.index.read_bytes()), foreign[0]
        )

    def test_generation_tamper_after_owned_move_refuses_before_index(self):
        case = self.case("generation")
        real_move = output.publish_directory_between_owned_parents_exclusive
        edits = []

        def move_then_tamper(source, destination, **options):
            real_move(source, destination, **options)
            page = next(destination.glob("*.html"))
            page.write_bytes(b"test-owned generation mutation\n")
            edits.append(page)

        with (
            patch.object(
                output,
                "publish_directory_between_owned_parents_exclusive",
                move_then_tamper,
            ),
            self.assertRaises(ValueError),
        ):
            self.publish(case)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].read_bytes(), b"test-owned generation mutation\n")
        self.require_unpublished(case)

    def test_excluded_namespace_cannot_hide_foreign_member(self):
        case = self.case("excluded")
        real_capture = inputs._capture_composed_native_tree
        foreign = case.namespace / "foreign.txt"
        edits = []

        def capture_then_tamper(*args, **options):
            tree = real_capture(*args, **options)
            if not edits:
                foreign.write_bytes(b"foreign excluded bytes\n")
                edits.append(True)
            return tree

        with (
            patch.object(inputs, "_capture_composed_native_tree", capture_then_tamper),
            self.assertRaisesRegex(ValueError, "foreign-initial-output-member"),
        ):
            self.publish(case)
        self.assertTrue(edits)
        self.assertEqual(foreign.read_bytes(), b"foreign excluded bytes\n")
        self.require_unpublished(case)

    def test_partial_cas_output_budget_retains_evidence_without_index(self):
        baseline = self.case("baseline")
        published = self.publish(baseline)
        self.assertTrue(published["published"])
        generation = next(baseline.namespace.glob("g-*"))
        pages = list(generation.glob("*.html"))
        self.assertGreater(len(pages), 1)
        maximum = max(p.stat().st_size for p in pages)
        self.assertGreater(sum(p.stat().st_size for p in pages), maximum)
        bounded = self.case(
            "bounded",
            limits=replace(
                self.limits, max_page_bytes=maximum, max_total_generated_bytes=maximum
            ),
        )
        real_put = FileSystemCAS.put_bytes
        retained = []

        def put(*args, **options):
            reference = real_put(*args, **options)
            if options.get("media_type") == "text/html":
                retained.append((args[0], reference))
            return reference

        with (
            patch.object(FileSystemCAS, "put_bytes", put),
            self.assertRaisesRegex(ValueError, "generated-bytes-policy-exceeded"),
        ):
            self.publish(bounded)
        self.assertTrue(retained)
        for store, reference in retained:
            store.verify(reference)
        self.assertEqual(
            (bounded.source / "payload.txt").read_bytes(), b"original payload\n"
        )
        self.require_unpublished(bounded)


if __name__ == "__main__":
    unittest.main()
