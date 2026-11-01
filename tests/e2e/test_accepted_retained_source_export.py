"""Admitted retained source reaches public accepted-only build without an agent."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.standard_lifecycle_binding import (
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.cli import main
from tests.e2e import test_cli_model_free_rebuild as fixture
from tests.support.fixtures_retained_native import author_retained_native_fixture


@unittest.skipUnless(shutil.which("make"), "the retained fixture needs Make")
class AcceptedRetainedSourceExportTests(unittest.TestCase):
    setUp = fixture.ModelFreeRebuildTests.setUp
    rebuild = fixture.ModelFreeRebuildTests.rebuild

    def build(self, *, command="build"):
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch.dict(os.environ, self.environment),
            patch(
                "literate_ai.cli.rebuild.resolve_standard_project_lifecycle_driver",
                lambda driver: resolve_standard_project_lifecycle_driver(
                    driver, distribution_observer=lambda: self.distribution
                ),
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            arguments = [
                "--json",
                command,
                "samples/hello-component",
                "--project",
                str(self.project),
            ]
            if command == "build":
                arguments.append("--from-accepted-source")
            else:
                # This retained executable's explicit CLI takes one JSON argument
                # vector. It does not declare an argument-free application mode.
                payload = {"name": "Ada"}
                specification = self.project / "samples/hello-component/component.md"
                if "| `messages` |" in specification.read_text():
                    payload["messages"] = ["hello", "world"]
                arguments.insert(3, json.dumps([payload]))
            status = main(arguments, stdout=output, stderr=errors)
        try:
            return status, json.loads(output.getvalue() or errors.getvalue())
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"{command} returned {status}: "
                f"{(output.getvalue() or errors.getvalue())[:1024]!r}"
            ) from exc

    def test_admit_then_build_from_accepted_retained_source_without_agent(self):
        self.exercise_accepted_export(committed_target=False)

    def test_project_relative_target_admits_then_builds_without_agent(self):
        self.exercise_accepted_export(committed_target=True)

    def exercise_accepted_export(self, *, committed_target):
        import py_compile
        from pathlib import Path

        from literate_ai.adapters.artifact_exports import load_artifact_export
        from literate_ai.adapters.cache import FileSystemSourceCache
        from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
        from literate_ai.adapters.lifecycle.standard_local import (
            LocalStandardLifecycleError,
        )

        cache_root = Path(self.environment["BUILD_DIR"]) / "accepted-source-cache"
        if committed_target:
            from literate_ai.contracts import (
                SourceCacheConfiguration,
                SourceCacheMode,
                SourceCacheRootKind,
                SourceCacheTarget,
            )
            from literate_ai.projects import ProjectConfigurationStore

            store = ProjectConfigurationStore(self.project)
            snapshot = store.read()
            store.update(
                snapshot,
                replace(
                    snapshot.definition,
                    source_cache=SourceCacheConfiguration(
                        mode=SourceCacheMode.READ_WRITE,
                        targets=(
                            SourceCacheTarget(
                                target_id="project-committed",
                                root_kind=SourceCacheRootKind.PROJECT_RELATIVE,
                                root_reference="generated/committed-source-cache",
                            ),
                        ),
                        write_target_id="project-committed",
                        require_unique=True,
                    ),
                ),
            )
            from literate_ai.adapters.project_initialization import (
                record_project_authority_review,
            )

            record_project_authority_review(self.project)
            cache_root = self.project / "generated/committed-source-cache"

        acceptances = []
        original_accept = LocalStandardLifecyclePorts.accept

        def observe_acceptance(ports, *args):
            result = original_accept(ports, *args)
            acceptances.append(result.identity)
            return result

        source = self.root / "retained"
        author_retained_native_fixture(self.project, source)
        (self.root / "tools/claude").unlink()
        self.environment["CODING_CLI"] = "no-generation-provider"
        with (
            patch(
                "literate_ai.adapters.models.coding_cli.select_coding_cli",
                side_effect=AssertionError("retained cache discovered a coding agent"),
            ),
            patch.object(LocalStandardLifecyclePorts, "accept", observe_acceptance),
        ):
            status, plan = self.rebuild(
                "--retained-source", str(source), "--retained-source-plan"
            )
            self.assertEqual(status, 0, plan)
            status, admitted = self.rebuild(
                "--retained-source",
                str(source),
                "--authorize-retained-source",
                plan["result"]["authorization_identity"],
            )
            self.assertEqual(status, 0, admitted)
            self.assertTrue(admitted["result"]["passed"])
            self.assertTrue(admitted["result"]["receipt_committed"])
            prior_acceptance_count = len(acceptances)
            status, built = self.build()
            self.assertEqual(status, 0, built)
            run_status, ran = self.build(command="run")
            self.assertEqual(run_status, 0, ran)
            self.assertEqual(ran["result"]["exit_status"], 0, ran)
            self.assertIn("greeting", json.loads(ran["result"]["stdout"]))
        self.assertEqual(status, 0, built)
        self.assertEqual(len(acceptances), prior_acceptance_count + 1)
        self.assertNotEqual(acceptances[-1], acceptances[-2])
        self.assertTrue(built["result"]["passed"])
        export = load_artifact_export(self.project, "samples/hello-component")
        self.assertTrue(export.accepted_source_only)
        self.assertIsNone(export.model_selector)
        self.assertTrue(export.artifact_reference.identity.uri.startswith("sha256:"))
        expected = self.root / "expected.pyc"
        py_compile.compile(
            str(source / "main.py"),
            cfile=str(expected),
            dfile="main.py",
            doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )
        self.assertEqual(
            Path(built["result"]["artifact"]).read_bytes(), expected.read_bytes()
        )
        receipt = self.project / "verification/current.json"
        prior_receipt = receipt.read_bytes()
        prior_artifact = Path(built["result"]["artifact"]).read_bytes()
        with patch.object(
            LocalStandardLifecyclePorts,
            "accept",
            side_effect=LocalStandardLifecycleError("fresh acceptance refused"),
        ) as refused:
            status, failed = self.build()
        self.assertNotEqual(status, 0, failed)
        self.assertTrue(refused.called)
        self.assertIn("fresh acceptance refused", failed["error"]["message"])
        self.assertEqual(receipt.read_bytes(), prior_receipt)
        self.assertEqual(
            load_artifact_export(self.project, "samples/hello-component"), export
        )
        self.assertEqual(Path(built["result"]["artifact"]).read_bytes(), prior_artifact)

        cache = FileSystemSourceCache("upgrade", cache_root)
        admitted_entries = cache.verified_published_entries()
        self.assertEqual(len(admitted_entries), 1)
        entry = admitted_entries[0]
        self.assertIsNotNone(entry.source_admission_evidence)
        legacy = replace(entry, source_admission_evidence=None)
        cache.publish(legacy, caller_cas=cache.cas)
        cache.retire_other_memberships(
            legacy.derivation.cache_key, keep=legacy.identity
        )
        with patch(
            "literate_ai.adapters.models.coding_cli.select_coding_cli",
            side_effect=AssertionError("legacy entry invoked a coding agent"),
        ):
            status, missing = self.build()
        self.assertNotEqual(status, 0, missing)
        self.assertEqual(receipt.read_bytes(), prior_receipt)
        self.assertEqual(
            load_artifact_export(self.project, "samples/hello-component"), export
        )

        # A fresh reviewed lifecycle can upgrade the legacy entry while keeping
        # both immutable histories. Prior acceptance cannot perform that upgrade.
        status, reviewed = self.rebuild(
            "--retained-source", str(source), "--retained-source-plan"
        )
        self.assertEqual(status, 0, reviewed)
        status, renewed = self.rebuild(
            "--retained-source",
            str(source),
            "--authorize-retained-source",
            reviewed["result"]["authorization_identity"],
        )
        self.assertEqual(status, 0, renewed)
        status, rebuilt = self.build()
        self.assertEqual(status, 0, rebuilt)
        self.assertEqual(
            Path(rebuilt["result"]["artifact"]).read_bytes(), expected.read_bytes()
        )
        self.assertGreater(
            len(cache.published_entries()), len(cache.verified_published_entries())
        )

        import hashlib

        source_digest = (
            "sha256:" + hashlib.sha256((source / "main.py").read_bytes()).hexdigest()
        )
        source_files = [
            item
            for current in cache.verified_published_entries()
            for item in current.source_files
            if item.blob.identity == source_digest
        ]
        self.assertTrue(
            source_files,
            [
                [item.path for item in current.source_files]
                for current in cache.verified_published_entries()
            ],
        )
        source_file = source_files[0]
        blob_path = cache.cas.path_for(source_file.blob)
        blob_path.chmod(0o600)
        blob_path.write_bytes(b"changed retained cache bytes")
        prior_export = load_artifact_export(self.project, "samples/hello-component")
        prior_receipt = receipt.read_bytes()
        status, corrupt = self.build()
        self.assertNotEqual(status, 0, corrupt)
        self.assertEqual(receipt.read_bytes(), prior_receipt)
        self.assertEqual(
            load_artifact_export(self.project, "samples/hello-component"), prior_export
        )


if __name__ == "__main__":
    unittest.main()
