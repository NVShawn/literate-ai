"""Secondary output requires the same live ownership as all other scratch."""

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.retained_cargo_input_scope_v2 import (
    capture_retained_cargo_input_scope_v2,
)
from literate_ai.adapters.retained_cargo_scratch_v2 import (
    reserve_retained_cargo_scratch_v2,
)
from literate_ai.adapters.retained_cargo_wrapper_v2 import (
    retained_cargo_wrapper_context_v2,
)
from literate_ai.contracts.retained_cargo_workspace_v2 import RetainedCargoScratchRoot
from tests.support.fixtures_retained_cargo_scratch import scratch_review_fixture


@unittest.skipUnless(
    os.name != "nt" and shutil.which("git") and shutil.which("make"),
    "requires native POSIX adoption and Git",
)
class RetainedCargoBuildOutputV2Tests(unittest.TestCase):
    def setUp(self):
        self.base = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="bo2-"))
        ).resolve()
        self.reviewed = self.enterContext(
            scratch_review_fixture(self.base, build_outputs=("asset-output",))
        )

    def test_role_roundtrip_and_unknown_kind_refusal(self):
        role = RetainedCargoScratchRoot("app/pkg", "build-output")
        self.assertEqual(RetainedCargoScratchRoot.from_dict(role.to_dict()), role)
        self.assertEqual(
            type(self.reviewed.plan).from_dict(self.reviewed.plan.to_dict()),
            self.reviewed.plan,
        )
        with self.assertRaises(ValueError):
            RetainedCargoScratchRoot("app/pkg", "unknown-output")

    def test_secondary_output_never_hides_manifest_or_target_source(self):
        common = self.reviewed.plan.common
        protected = {
            *(x.path for x in common.manifests),
            *(t.source for p in common.graph.packages for t in p.targets),
        }
        for source in sorted(protected):
            candidates = {source, source + "/child"}
            parent = Path(source).parent.as_posix()
            if parent != ".":
                candidates.add(parent)
            for path in sorted(candidates):
                with self.subTest(source=source, output=path):
                    roots = (RetainedCargoScratchRoot(path, "build-output"),)
                    with self.assertRaises(ValueError):
                        replace(self.reviewed.plan, scratch_roots=roots)

    def test_nested_output_is_not_relabelled_as_cargo_output(self):
        # Preserve Cargo's deliberate prohibition on nested target directories.
        package = self.reviewed.plan.graph.common.packages[0]
        output = package.root + "/compiled-assets"
        with self.assertRaises(ValueError):
            replace(self.reviewed.plan.graph.common, output_directory=output)
        # This is a pure role/graph contract check. A complete receiving
        # operation must independently supply all of its disjoint scratch roots.
        roots = (RetainedCargoScratchRoot(output, "build-output"),)
        planned = replace(self.reviewed.plan, scratch_roots=roots)
        self.assertEqual(planned.graph, self.reviewed.plan.graph)
        self.assertEqual(planned.common.gates, self.reviewed.plan.common.gates)

    def test_existing_output_is_refused_and_foreign_bytes_survive(self):
        with retained_cargo_wrapper_context_v2(self.reviewed) as context:
            output = context.workspace / "asset-output"
            output.mkdir()
            foreign = output / "foreign"
            foreign.write_bytes(b"preserve")
            with self.assertRaisesRegex(ValueError, "scratch-existing-root"):
                with reserve_retained_cargo_scratch_v2(context):
                    self.fail("existing output was adopted")
            self.assertEqual(foreign.read_bytes(), b"preserve")

    def test_owned_output_changes_are_distinct_from_source_input_changes(self):
        home = self.base / "cargo-home"
        home.mkdir()
        env = {"CARGO_HOME": str(home), "PATH": os.environ["PATH"]}
        with retained_cargo_wrapper_context_v2(self.reviewed) as context:
            with reserve_retained_cargo_scratch_v2(context) as scratch:
                inputs = capture_retained_cargo_input_scope_v2(
                    context.workspace,
                    policy=self.reviewed.policy,
                    environment=env,
                    scratch=scratch,
                )
                output = context.workspace / "asset-output/assets.js"
                output.write_bytes(b"owned secondary output")
                inputs.require_unchanged(environment=env)
                foreign_source = context.workspace / "foreign-source.rs"
                foreign_source.write_bytes(b"source remains an input")
                try:
                    with self.assertRaisesRegex(ValueError, "custody-changed"):
                        inputs.require_unchanged(environment=env)
                finally:
                    foreign_source.unlink()
                inputs.require_unchanged(environment=env)
            self.assertEqual(output.read_bytes(), b"owned secondary output")
            with self.assertRaisesRegex(ValueError, "scratch-operation-ended"):
                inputs.require_unchanged(environment=env)

    def test_replaced_or_redirected_output_never_remains_owned(self):
        with retained_cargo_wrapper_context_v2(self.reviewed) as context:
            with self.assertRaisesRegex(ValueError, "scratch-root-changed"):
                with reserve_retained_cargo_scratch_v2(context) as scratch:
                    output = context.workspace / "asset-output"
                    saved = context.workspace / "preserved-output"
                    output.rename(saved)
                    output.mkdir()
                    (output / "foreign").write_bytes(b"preserve replacement")
                    scratch.require_unchanged()
            self.assertEqual(
                (context.workspace / "asset-output/foreign").read_bytes(),
                b"preserve replacement",
            )

    def test_symlink_output_refuses_without_touching_its_target(self):
        outside = self.base / "foreign"
        outside.mkdir()
        marker = outside / "original"
        marker.write_bytes(b"preserve target")
        with retained_cargo_wrapper_context_v2(self.reviewed) as context:
            output = context.workspace / "asset-output"
            output.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                with reserve_retained_cargo_scratch_v2(context):
                    self.fail("symlink output was adopted")
            self.assertTrue(output.is_symlink())
            self.assertEqual(marker.read_bytes(), b"preserve target")

    def test_portable_alias_collision_refuses_with_foreign_contents_preserved(self):
        with retained_cargo_wrapper_context_v2(self.reviewed) as context:
            output = context.workspace / "ASSET-OUTPUT"
            output.mkdir()
            marker = output / "original"
            marker.write_bytes(b"preserve alias")
            with self.assertRaisesRegex(ValueError, "scratch-existing-root"):
                with reserve_retained_cargo_scratch_v2(context):
                    self.fail("portable alias was replaced")
            self.assertEqual(marker.read_bytes(), b"preserve alias")
