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

from literate_ai.adapters.builders import run_bounded_process
from literate_ai.adapters.builders.python import (
    controlled_python_environment,
    discover_python_toolchain,
)
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecyclePorts
from literate_ai.adapters.standard_lifecycle_binding import (
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.cli import main
from tests.e2e import test_cli_model_free_rebuild as fixture
from tests.support.fixtures_retained_native import author_retained_native_fixture


@unittest.skipUnless(shutil.which("make"), "the retained fixture needs Make")
class AcceptedRetainedSourceExportTests(unittest.TestCase):
    rebuild = fixture.ModelFreeRebuildTests.rebuild

    def setUp(self):
        fixture.ModelFreeRebuildTests.setUp(self)
        self.admitted_python = []
        original_accept = LocalStandardLifecyclePorts.accept

        def observe_compiler(ports, plan, *args, **kwargs):
            accepted = original_accept(ports, plan, *args, **kwargs)
            contract = ports.contracts[plan.component_revision.uri]
            binding = ports.tool_bindings[contract.language_compiler_identity.uri]
            binding.require_unchanged()
            self.admitted_python.append(
                (binding.command, binding.toolchain_identity.uri)
            )
            return accepted

        self.enterContext(
            patch.object(LocalStandardLifecyclePorts, "accept", observe_compiler)
        )

    def compile_expected(self, source, destination):
        # The unittest interpreter is not the admitted native compiler. Reopen
        # the actual accepted binding rather than selecting another PATH tool.
        self.assertTrue(self.admitted_python)
        command, identity = self.admitted_python[-1]
        environment = controlled_python_environment({**os.environ, **self.environment})
        compiler = discover_python_toolchain(environment, pinned_command=command)
        self.assertEqual(compiler.identity, identity)
        self.assertFalse(destination.exists())
        compiler.require_unchanged(environment)
        try:
            result = run_bounded_process(
                [
                    *compiler.command,
                    "-I",
                    "-S",
                    "-c",
                    "import py_compile,sys; py_compile.compile("
                    "sys.argv[1],cfile=sys.argv[2],dfile='main.py',doraise=True,"
                    "invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)",
                    str(source),
                    str(destination),
                ],
                cwd=self.root,
                environment=environment,
                timeout_seconds=60,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=4096,
                error_prefix="fixture.accepted-python-compile",
            )
        finally:
            compiler.require_unchanged(environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        return destination.read_bytes()

    def build(self, *, command="build", cache_entries=()):
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
                for entry in cache_entries:
                    arguments.extend(("--source-cache-entry", entry))
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

    def test_exact_entry_selection_preserves_successive_admitted_revisions(self):
        from pathlib import Path

        from literate_ai.adapters.cache import FileSystemSourceCache

        source = self.root / "retained"
        author_retained_native_fixture(self.project, source)
        (self.root / "tools/claude").unlink()
        self.environment["CODING_CLI"] = "no-generation-provider"
        cache = FileSystemSourceCache(
            "history", Path(self.environment["BUILD_DIR"]) / "accepted-source-cache"
        )
        admitted, expected = [], []
        with patch(
            "literate_ai.adapters.models.coding_cli.select_coding_cli",
            side_effect=AssertionError("accepted source invoked coding agent"),
        ):
            for revision in range(2):
                if revision:
                    with (source / "main.py").open("a") as stream:
                        stream.write(
                            "\n# independently reviewed second retained revision\n"
                        )
                # Retain an independent pre-build oracle. A builder must not
                # mutate the retained input and make both byte streams agree.
                expected_source = (source / "main.py").read_bytes()
                status, review = self.rebuild(
                    "--retained-source", str(source), "--retained-source-plan"
                )
                self.assertEqual(status, 0, review)
                self.assertEqual((source / "main.py").read_bytes(), expected_source)
                status, result = self.rebuild(
                    "--retained-source",
                    str(source),
                    "--authorize-retained-source",
                    review["result"]["authorization_identity"],
                )
                self.assertEqual(status, 0, result)
                self.assertEqual((source / "main.py").read_bytes(), expected_source)
                oracle_source = self.root / f"expected-source-{revision}.py"
                with oracle_source.open("xb") as stream:
                    stream.write(expected_source)
                expected.append(
                    self.compile_expected(
                        oracle_source, self.root / f"expected-{revision}.pyc"
                    )
                )
                self.assertEqual(oracle_source.read_bytes(), expected_source)
                entries = cache.verified_published_entries()
                fresh = [entry for entry in entries if entry not in admitted]
                self.assertEqual(len(fresh), 1)
                self.assertEqual(
                    result["result"]["source_cache_published_entry_identities"],
                    [fresh[0].identity.uri],
                )
                admitted.extend(fresh)
                status, built = self.build()
                if revision == 0:
                    self.assertEqual(status, 0, built)
                else:
                    self.assertNotEqual(status, 0, built)
                    self.assertEqual(
                        built["error"]["code"],
                        "execution.accepted_source_provider_ambiguous",
                    )
            for index in (1, 0):
                status, built = self.build(
                    cache_entries=(admitted[index].identity.uri,)
                )
                self.assertEqual(status, 0, built)
                self.assertEqual(
                    Path(built["result"]["artifact"]).read_bytes(), expected[index]
                )
                status, ran = self.build(command="run")
                self.assertEqual(status, 0, ran)
                self.assertEqual(ran["result"]["exit_status"], 0)
                record = Path(built["result"]["artifact"]).parent / "execution.json"
                original_record = record.read_bytes()
                tampered = json.loads(original_record)
                tampered["source_cache_entry_identities"] = [
                    admitted[1 - index].identity.uri
                ]
                try:
                    record.write_text(json.dumps(tampered))
                    status, refused = self.build(command="run")
                    self.assertNotEqual(status, 0, refused)
                    self.assertEqual(
                        refused["error"]["code"], "artifact_export.authority_stale"
                    )
                finally:
                    record.write_bytes(original_record)
            receipt = (self.project / "verification/current.json").read_bytes()
            for choices in (
                ("sha256:" + "0" * 64,),
                ("invalid",),
                (admitted[0].identity.uri,) * 2,
                tuple(entry.identity.uri for entry in admitted),
            ):
                with self.subTest(selection=choices):
                    status, refused = self.build(cache_entries=choices)
                    self.assertNotEqual(status, 0, refused)
                    self.assertEqual(
                        (self.project / "verification/current.json").read_bytes(),
                        receipt,
                    )
            self.assertEqual(
                sorted(
                    entry.identity.uri for entry in cache.verified_published_entries()
                ),
                sorted(entry.identity.uri for entry in admitted),
            )

    def test_other_component_lock_does_not_create_provider_ambiguity(self):
        from pathlib import Path

        from literate_ai.adapters.cache import FileSystemSourceCache
        from literate_ai.adapters.project_initialization import (
            record_project_authority_review,
        )

        original = self.project
        (self.root / "tools/claude").unlink()
        self.environment["CODING_CLI"] = "no-generation-provider"
        cache = FileSystemSourceCache(
            "multiple-components",
            Path(self.environment["BUILD_DIR"]) / "accepted-source-cache",
        )
        entries = []
        with patch(
            "literate_ai.adapters.models.coding_cli.select_coding_cli",
            side_effect=AssertionError("accepted source invoked coding agent"),
        ):
            for index in range(2):
                if index:
                    self.project = self.root / "other-project"
                    shutil.copytree(original, self.project)
                    component = self.project / "samples/hello-component"
                    spec = component / "component.md"
                    text = spec.read_text()
                    self.assertIn("namespace: example", text)
                    spec.write_text(
                        text.replace(
                            "namespace: example", "namespace: other-samples", 1
                        )
                    )
                    (component / "component.lock.json").unlink(missing_ok=True)
                    output = io.StringIO()
                    with patch.dict(os.environ, self.environment):
                        status = main(
                            ["--json", "lock", str(component)],
                            stdout=output,
                            stderr=output,
                        )
                    self.assertEqual(status, 0, output.getvalue())
                    # Only the namespace changed; independently reviewed JSON
                    # expectations remain identical and bind the new specification.
                    locked = json.loads((component / "component.lock.json").read_text())
                    specification = next(
                        node["revision"]["specification_set_identity"]
                        for node in locked["nodes"]
                        if node["revision"]["coordinate"]["name"] == "hello-component"
                    )
                    oracle = (
                        self.project / "verification/acceptance/hello-component.json"
                    )
                    cases = json.loads(oracle.read_text())
                    cases["specification_set_identity"] = (
                        f"{specification['algorithm']}:{specification['digest']}"
                    )
                    oracle.write_text(json.dumps(cases, indent=2) + "\n")
                    record_project_authority_review(self.project)
                source = self.root / f"retained-{index}"
                author_retained_native_fixture(self.project, source)
                status, review = self.rebuild(
                    "--retained-source", str(source), "--retained-source-plan"
                )
                self.assertEqual(status, 0, review)
                status, admitted = self.rebuild(
                    "--retained-source",
                    str(source),
                    "--authorize-retained-source",
                    review["result"]["authorization_identity"],
                )
                self.assertEqual(status, 0, admitted)
                selected = [
                    entry
                    for entry in cache.verified_published_entries()
                    if entry not in entries
                ]
                self.assertEqual(len(selected), 1)
                entries.extend(selected)
            self.assertNotEqual(
                entries[0].derivation.component_lock_identity,
                entries[1].derivation.component_lock_identity,
            )
            for project, foreign in (
                (original, entries[1]),
                (self.project, entries[0]),
            ):
                self.project = project
                status, result = self.build()
                self.assertEqual(status, 0, result)
                receipt = (project / "verification/current.json").read_bytes()
                status, refused = self.build(cache_entries=(foreign.identity.uri,))
                self.assertNotEqual(status, 0, refused)
                self.assertEqual(
                    refused["error"]["code"], "source-cache.selection-not-applicable"
                )
                self.assertEqual(
                    (project / "verification/current.json").read_bytes(), receipt
                )
            self.assertEqual(len(cache.verified_published_entries()), 2)

    def test_project_root_partitions_exact_entries_across_current_components(self):
        from pathlib import Path

        from literate_ai.adapters.cache import FileSystemSourceCache
        from literate_ai.adapters.lifecycle import LocalStandardLifecyclePorts
        from literate_ai.adapters.project_initialization import (
            record_project_authority_review,
        )

        def invoke(specification, *extra):
            output = io.StringIO()
            with (
                patch.dict(os.environ, self.environment),
                patch(
                    "literate_ai.cli.rebuild.resolve_standard_project_lifecycle_driver",
                    lambda driver: resolve_standard_project_lifecycle_driver(
                        driver, distribution_observer=lambda: self.distribution
                    ),
                ),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(output),
            ):
                status = main(
                    [
                        "--json",
                        "rebuild",
                        specification,
                        "--project",
                        str(self.project),
                        "--allow-host-execution",
                        "--update-receipt",
                        *extra,
                    ],
                    stdout=output,
                    stderr=output,
                )
            return status, json.loads(output.getvalue())

        original = self.project / "samples/hello-component"
        other = self.project / "components/other-component"
        shutil.copytree(original, other)
        spec = other / "component.md"
        spec.write_text(
            spec.read_text().replace(
                "namespace: example", "namespace: second-example", 1
            )
        )
        (other / "component.lock.json").unlink(missing_ok=True)
        with patch.dict(os.environ, self.environment):
            out = io.StringIO()
            status = main(["--json", "lock", str(other)], stdout=out, stderr=out)
        self.assertEqual(status, 0, out.getvalue())
        # Adding a catalog member changes the resolution audit for existing roots.
        with patch.dict(os.environ, self.environment):
            out = io.StringIO()
            status = main(["--json", "lock", str(original)], stdout=out, stderr=out)
        self.assertEqual(status, 0, out.getvalue())

        locked = json.loads((other / "component.lock.json").read_text())
        identity = next(
            node["revision"]["specification_set_identity"]
            for node in locked["nodes"]
            if node["revision"]["coordinate"]["name"] == "other-component"
        )
        oracle = json.loads(
            (self.project / "verification/acceptance/hello-component.json").read_text()
        )
        # Independent JSON expectations are unchanged by the new coordinate.
        oracle["component"] = "other-component"
        oracle["specification_set_identity"] = (
            f"{identity['algorithm']}:{identity['digest']}"
        )
        (self.project / "verification/acceptance/other-component.json").write_text(
            json.dumps(oracle, indent=2) + "\n"
        )
        record_project_authority_review(self.project)
        (self.root / "tools/claude").unlink()
        self.environment["CODING_CLI"] = "no-generation-provider"
        selected = []
        cache = FileSystemSourceCache(
            "project-roots",
            Path(self.environment["BUILD_DIR"]) / "accepted-source-cache",
        )
        with patch(
            "literate_ai.adapters.models.coding_cli.select_coding_cli",
            side_effect=AssertionError("project continuation invoked coding agent"),
        ):
            for index, component in enumerate(
                ("samples/hello-component", "components/other-component")
            ):
                source = self.root / f"project-retained-{index}"
                author_retained_native_fixture(
                    self.project, source, component_path=component
                )
                status, review = invoke(
                    component,
                    "--retained-source",
                    str(source),
                    "--retained-source-plan",
                )
                self.assertEqual(status, 0, review)
                status, admitted = invoke(
                    component,
                    "--retained-source",
                    str(source),
                    "--authorize-retained-source",
                    review["result"]["authorization_identity"],
                )
                self.assertEqual(status, 0, admitted)
                selected.extend(
                    admitted["result"]["source_cache_published_entry_identities"]
                )
            self.assertEqual(len(selected), 2)
            before = {
                p.relative_to(cache.root): p.read_bytes()
                for p in cache.root.rglob("*")
                if p.is_file()
            }
            arguments = [
                value for entry in selected for value in ("--source-cache-entry", entry)
            ]
            status, rebuilt = invoke(".", "--from-accepted-source", *arguments)
            self.assertEqual(status, 0, rebuilt)
            self.assertEqual(len(rebuilt["result"]["components"]), 2)
            self.assertEqual(rebuilt["result"]["test_summary"]["total"], 6)
            self.assertEqual(
                rebuilt["result"]["source_cache_published_entry_identities"], []
            )
            self.assertEqual(
                before,
                {
                    p.relative_to(cache.root): p.read_bytes()
                    for p in cache.root.rglob("*")
                    if p.is_file()
                },
            )
            # A selector supplements unique members of other roots; it does not
            # force every root to receive that same foreign identity.
            status, partial = invoke(
                ".", "--from-accepted-source", "--source-cache-entry", selected[0]
            )
            self.assertEqual(status, 0, partial)
            self.assertEqual(len(partial["result"]["components"]), 2)
            receipt = (self.project / "verification/current.json").read_bytes()
            with patch.object(
                LocalStandardLifecyclePorts,
                "build",
                side_effect=AssertionError("invalid selector reached host build"),
            ):
                status, refused = invoke(
                    ".",
                    "--from-accepted-source",
                    "--source-cache-entry",
                    "sha256:" + "0" * 64,
                )
            self.assertNotEqual(status, 0, refused)
            self.assertEqual(
                (self.project / "verification/current.json").read_bytes(), receipt
            )
            # A previously valid admission outside the currently selected lock
            # set is foreign, even though its immutable cache membership remains.
            lock = other / "component.lock.json"
            retained_lock = self.root / "other-retained.lock"
            lock.rename(retained_lock)
            try:
                with patch.object(
                    LocalStandardLifecyclePorts,
                    "build",
                    side_effect=AssertionError("foreign selector reached host build"),
                ):
                    status, refused = invoke(".", "--from-accepted-source", *arguments)
                self.assertNotEqual(status, 0, refused)
                self.assertEqual(
                    refused["error"]["code"], "source-cache.selection-not-applicable"
                )
                self.assertEqual(
                    (self.project / "verification/current.json").read_bytes(), receipt
                )
            finally:
                retained_lock.rename(lock)

    def test_admit_then_build_from_accepted_retained_source_without_agent(self):
        self.exercise_accepted_export(committed_target=False)

    def test_project_relative_target_admits_then_builds_without_agent(self):
        self.exercise_accepted_export(committed_target=True)

    def test_ignored_project_target_survives_materialization_without_agent(self):
        self.exercise_accepted_export(committed_target=True, materialize=True)

    def exercise_accepted_export(self, *, committed_target, materialize=False):
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

            if materialize:
                import subprocess

                subprocess.run(["git", "init", "-q", str(self.project)], check=True)
                (self.project / ".gitignore").write_text(
                    "generated/committed-source-cache/\nprivate-ignored/\n"
                )
                private = self.project / "private-ignored"
                private.mkdir()
                (private / "operator.txt").write_text("not cache transport input")

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
            if materialize:
                from literate_ai.adapters.source_materialization import (
                    capture_source_archive,
                )
                from literate_ai.contracts import LifecycleDispatchAction
                from literate_ai.remote_source_guard import extract_source_archive
                from tests.support.fixtures_test_remote_execution import request

                original_project = self.project
                before_cache = {
                    item.relative_to(cache_root): item.read_bytes()
                    for item in cache_root.rglob("*")
                    if item.is_file()
                }
                staging = self.root / "transport"
                staging.mkdir()
                with patch.dict(os.environ, self.environment):
                    captured = capture_source_archive(
                        self.project,
                        replace(
                            request(LifecycleDispatchAction.BUILD),
                            accepted_source_only=True,
                        ),
                        directory=staging,
                    )
                destination = self.root / "received"
                extract_source_archive(
                    captured.path,
                    destination,
                    captured.materialization.source_tree_identity.uri,
                )
                self.project = destination / "literate-ai"
                cache_root = self.project / "generated/committed-source-cache"
                self.assertTrue(
                    cache_root.is_dir(), "ignored declared cache was omitted"
                )
                self.assertFalse((self.project / "private-ignored").exists())
                self.assertEqual(
                    before_cache,
                    {
                        item.relative_to(cache_root): item.read_bytes()
                        for item in cache_root.rglob("*")
                        if item.is_file()
                    },
                )
                controller_cache = original_project / "generated/committed-source-cache"
                self.assertEqual(
                    before_cache,
                    {
                        item.relative_to(controller_cache): item.read_bytes()
                        for item in controller_cache.rglob("*")
                        if item.is_file()
                    },
                )
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
        self.compile_expected(source / "main.py", expected)
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
