"""Real authority reads reuse parsing, never a guard or ended owner."""

import os
import shutil
import unittest
from contextlib import ExitStack, nullcontext
from unittest.mock import patch

from literate_ai import projects
from literate_ai.adapters import component_lock_planning as planning
from literate_ai.adapters import locked_generation_authority as catalogs
from literate_ai.adapters import retained_cargo_execution_inputs_v2 as execution_inputs
from literate_ai.adapters.component_lock_application import (
    ProjectComponentLockSetError,
    current_project_component_locks,
)
from literate_ai.adapters.retained_cargo_wrapper_v2 import RetainedCargoWrapperContextV2
from tests.critical import test_retained_cargo_receiving_v2 as receiving_fixture
from tests.support.fixtures_current_component_locks import (
    initialize_current_component_lock_fixture,
)


def _frames():
    return (
        projects._PROJECT_CONFIGURATION_PARSES.get(),
        catalogs._catalog_observations.get(),
    )


class IsolatedAuthorityParseFrameTests(unittest.TestCase):
    def setUp(self):
        initialize_current_component_lock_fixture(self)

    def test_nested_real_reads_restore_ambient_frames_on_success_and_exception(self):
        with (
            projects.read_only_project_configuration_parses(),
            catalogs.read_only_component_catalog_observations(),
        ):
            expected = current_project_component_locks(self.project)
            ambient = _frames()
            keys = (tuple(ambient[0].templates), tuple(ambient[1]))
            with (
                projects.read_only_project_configuration_parses(),
                catalogs.read_only_component_catalog_observations(),
            ):
                self.assertEqual(_frames(), ambient)
            for fail in (False, True):
                with self.subTest(fail=fail):
                    try:
                        with (
                            projects.read_only_project_configuration_parses(
                                isolated=True
                            ),
                            catalogs.read_only_component_catalog_observations(
                                isolated=True
                            ),
                        ):
                            inner = _frames()
                            self.assertIsNot(inner[0], ambient[0])
                            self.assertIsNot(inner[1], ambient[1])
                            self.assertEqual(inner[0].templates, {})
                            self.assertEqual(inner[1], {})
                            self.assertEqual(
                                current_project_component_locks(self.project), expected
                            )
                            if fail:
                                raise LookupError("owned nested-frame failure")
                    except LookupError as error:
                        self.assertTrue(fail)
                        self.assertEqual(str(error), "owned nested-frame failure")
                    self.assertIs(_frames()[0], ambient[0])
                    self.assertIs(_frames()[1], ambient[1])
                    self.assertEqual(
                        (tuple(ambient[0].templates), tuple(ambient[1])), keys
                    )
        self.assertEqual(_frames(), (None, None))

    def test_isolated_warm_catalog_checks_selected_and_flavor_dependencies(self):
        reader = catalogs.FilesystemLockedGenerationAuthorityReader()
        with (
            projects.read_only_project_configuration_parses(isolated=True),
            catalogs.read_only_component_catalog_observations(isolated=True),
        ):
            expected = current_project_component_locks(self.project)
            catalog = reader.load_catalog(self.component)
            paths = {
                self.component / "component.md",
                self.component / "specs/spec.md",
                self.component / "acceptance/execution.json",
                self.root / "skills/implement.json",
                self.root / "workflows/host.json",
                self.root / "routing/default.json",
            }
            for flavor in catalog._flavors:
                paths.add(flavor.path)
                paths.update(
                    flavor.path.parent / reference.uri
                    for reference in (
                        *flavor.definition.specification_fragments,
                        *flavor.definition.authoring_inputs,
                        *(item.content for item in flavor.definition.contributions),
                    )
                )
            for path in sorted(paths):
                with self.subTest(path=path.relative_to(self.root).as_posix()):
                    original = path.read_bytes()
                    try:
                        path.write_bytes(original + b"\nChanged exact input.\n")
                        with self.assertRaises(ProjectComponentLockSetError):
                            current_project_component_locks(self.project)
                        self.assertEqual(
                            path.read_bytes(), original + b"\nChanged exact input.\n"
                        )
                    finally:
                        path.write_bytes(original)
                    self.assertEqual(
                        current_project_component_locks(self.project), expected
                    )

    def test_isolated_catalog_observes_membership_nearest_project_and_redirects(self):
        with (
            projects.read_only_project_configuration_parses(isolated=True),
            catalogs.read_only_component_catalog_observations(isolated=True),
        ):
            expected = current_project_component_locks(self.project)
            added = self.component / "added/component.md"
            added.parent.mkdir()
            try:
                added.write_bytes((self.component / "component.md").read_bytes())
                with self.assertRaises(ProjectComponentLockSetError):
                    current_project_component_locks(self.project)
            finally:
                added.unlink()
                added.parent.rmdir()
            nearer = self.component / "literate.project.json"
            try:
                nearer.write_bytes(b"{}\n")
                with self.assertRaises(
                    (projects.ProjectError, ProjectComponentLockSetError)
                ):
                    current_project_component_locks(self.project)
            finally:
                nearer.unlink()
            if os.name != "nt":
                path = self.root / "flavors/lang-python/flavor.md"
                saved = path.with_name("saved-flavor.md")
                path.rename(saved)
                try:
                    path.symlink_to(saved.name)
                    with self.assertRaises(ProjectComponentLockSetError):
                        current_project_component_locks(self.project)
                finally:
                    path.unlink()
                    saved.rename(path)
            self.assertEqual(current_project_component_locks(self.project), expected)


@unittest.skipUnless(
    os.name != "nt" and shutil.which("git") and shutil.which("make"),
    "requires actual adopted POSIX source and native Git/Make",
)
class RetainedCargoIsolatedObservationTests(unittest.TestCase):
    def setUp(self):
        fixture = receiving_fixture.RetainedCargoReceivingV2Tests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.callback_error = None

        def read_current():
            if self.callback_error is not None:
                raise self.callback_error
            return fixture.current

        materialized = fixture.receive(read_current=read_current)
        home = fixture.base / "execution-cargo-home"
        home.mkdir()
        self.environment = {"CARGO_HOME": str(home), "PATH": os.environ["PATH"]}
        self.inputs = execution_inputs.read_retained_cargo_execution_inputs_v2(
            materialized, environment=self.environment
        )

    def observe(self, *, reuse, mutate=None):
        events = []
        original = RetainedCargoWrapperContextV2.require_unchanged
        calls = 0

        def guarded(context):
            nonlocal calls
            calls += 1
            ordinal = calls
            events.append(("enter", ordinal))
            result = original(context)
            events.append(("exit", ordinal))
            if ordinal == 1 and mutate is not None:
                mutate()
            return result

        with ExitStack() as stack:
            if not reuse:
                # Exact original body without the newly added parse-only scopes;
                # every actual owner/guard/read still runs.
                for name in (
                    "read_only_project_configuration_parses",
                    "read_only_component_catalog_observations",
                ):
                    stack.enter_context(
                        patch.object(execution_inputs, name, return_value=nullcontext())
                    )
            stack.enter_context(
                patch.object(
                    RetainedCargoWrapperContextV2, "require_unchanged", guarded
                )
            )
            parser = stack.enter_context(
                patch.object(
                    planning,
                    "_load_authoring_entry",
                    wraps=planning._load_authoring_entry,
                )
            )
            error = None
            value = None
            try:
                value = self.inputs.current_identity(environment=self.environment)
            except (ValueError, RuntimeError) as observed:
                error = observed
            return value, error, events, parser.call_count

    def test_identity_keeps_seventeen_real_fences_and_only_reuses_parsing(self):
        before = _frames()
        baseline = self.observe(reuse=False)
        candidate = self.observe(reuse=True)
        self.assertIsNone(baseline[1])
        self.assertIsNone(candidate[1])
        self.assertEqual(candidate[0], baseline[0])
        self.assertEqual(candidate[2], baseline[2])
        self.assertEqual(len([x for x in candidate[2] if x[0] == "enter"]), 17)
        self.assertGreater(candidate[3], 0)  # Each isolated observation starts cold.
        self.assertLess(candidate[3], baseline[3])
        self.assertEqual(_frames(), before)
        again = self.observe(reuse=True)
        self.assertEqual(again[0], baseline[0])
        self.assertEqual(again[3], candidate[3])
        self.assertEqual(_frames(), before)

    def test_warm_observation_preserves_refusal_stage_for_real_authority_mutations(
        self,
    ):
        context = self.fixture.context
        paths = [
            context.reviewed.project.root
            / "components/legacy-project-wrapper/component.md",
            next(iter(context.bindings[0][1]._flavors)).path,
        ]
        # Selected payloads are independently closed after catalog checks.
        closure = context._closures[0]
        paths.append(
            next(
                entry.path
                for entry in closure._entries.values()
                if entry.path.suffix == ".md" and entry.path.name != "component.md"
            )
        )
        for path in paths:
            self.assertFalse(path.is_relative_to(context.workspace))
            original = path.read_bytes()
            results = []
            for reuse in (False, True):
                with self.subTest(path=path.name, reuse=reuse):
                    try:
                        result = self.observe(
                            reuse=reuse,
                            mutate=lambda path=path, original=original: (
                                path.write_bytes(original + b"\nChanged.\n")
                            ),
                        )
                        self.assertIsNone(result[0])
                        self.assertIsNotNone(result[1])
                        results.append(
                            (
                                type(result[1]),
                                getattr(result[1], "code", None),
                                str(result[1]),
                                result[2],
                            )
                        )
                    finally:
                        path.write_bytes(original)
                    self.assertEqual(_frames(), (None, None))
            self.assertEqual(results[0], results[1])

    def test_real_callback_failure_restores_nested_ambient_frames(self):
        with (
            projects.read_only_project_configuration_parses(),
            catalogs.read_only_component_catalog_observations(),
        ):
            ambient = _frames()
            self.callback_error = RuntimeError("original provider read failed")
            try:
                with self.assertRaises(RuntimeError) as observed:
                    self.inputs.current_identity(environment=self.environment)
                self.assertIs(observed.exception, self.callback_error)
            finally:
                self.callback_error = None
            self.assertIs(_frames()[0], ambient[0])
            self.assertIs(_frames()[1], ambient[1])
            self.inputs.current_identity(environment=self.environment)
        self.assertEqual(_frames(), (None, None))

    def test_ended_actual_scratch_still_refuses_and_clears_isolated_frames(self):
        self.fixture.scratch_manager.__exit__(None, None, None)
        with self.assertRaisesRegex(ValueError, "scratch-operation-ended"):
            self.inputs.current_identity(environment=self.environment)
        self.assertEqual(_frames(), (None, None))
