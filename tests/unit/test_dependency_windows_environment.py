"""PE resolution uses an explicit runtime context without ambient repair."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.dependencies import observation


class WindowsDependencyEnvironmentTests(unittest.TestCase):
    def test_default_reads_current_host_environment_at_observation_time(self):
        observer = observation.WindowsPeDependencyObserver()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            stop = RuntimeError("fixture reached system schema")
            with (
                patch.object(observation.sys, "platform", "win32"),
                patch.dict(os.environ, {"SystemRoot": str(root)}),
                patch.object(observation, "_find_windows_pe_inspector"),
                patch.object(
                    observation, "_load_windows_api_set_schema", side_effect=stop
                ) as schema,
                self.assertRaises(RuntimeError) as failure,
            ):
                observer.observe({}, root_ref="fixture")
            self.assertIs(failure.exception, stop)
            schema.assert_called_once_with(root / "System32/apisetschema.dll")

    def test_portable_dispatch_snapshots_case_insensitive_windows_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            environment = {"SystemRoot": str(root), "Path": str(root / "runtime")}
            observer = observation.PortableHostDependencyObserver(
                windows_environment=environment
            )
            environment["Path"] = str(root / "changed")
            with (
                patch.object(observation.sys, "platform", "win32"),
                patch.object(observation, "WindowsPeDependencyObserver") as adapter,
            ):
                observer.observe({}, root_ref="fixture")
            self.assertEqual(
                adapter.call_args.kwargs["environment"],
                {"SYSTEMROOT": str(root), "PATH": str(root / "runtime")},
            )

    def test_explicit_system_schema_and_search_path_exclude_ambient_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            artifact = root / "artifact"
            artifact.mkdir()
            system = root / "selected-system"
            runtime = root / "selected-runtime"
            ambient = root / "ambient"
            observer = observation.WindowsPeDependencyObserver(
                environment={"SystemRoot": str(system), "Path": str(runtime)}
            )
            stop = RuntimeError("fixture reached native closure")
            with (
                patch.object(observation.sys, "platform", "win32"),
                patch.dict(
                    os.environ, {"SystemRoot": str(ambient), "PATH": str(ambient)}
                ),
                patch.object(
                    observation,
                    "_find_windows_pe_inspector",
                    return_value=SimpleNamespace(path=root / "inspector.exe"),
                ),
                patch.object(
                    observation,
                    "_load_windows_api_set_schema",
                    return_value=SimpleNamespace(
                        path=str(system / "System32/apisetschema.dll")
                    ),
                ) as schema,
                patch.object(observation, "_pe_closure", side_effect=stop) as closure,
                self.assertRaises(RuntimeError) as failure,
            ):
                observer.observe({"artifact_path": str(artifact)}, root_ref="fixture")
            self.assertIs(failure.exception, stop)
            schema.assert_called_once_with(system / "System32/apisetschema.dll")
            roots = closure.call_args.args[2]
            self.assertIn(runtime, roots)
            self.assertIn(system / "System32", roots)
            self.assertNotIn(ambient, roots)
            self.assertNotIn(ambient / "System32", roots)

    def test_missing_or_unsafe_explicit_context_refuses_before_inspection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            contexts = (
                {},
                {"SYSTEMROOT": "relative"},
                {"SYSTEMROOT": str(root), "PATH": "relative"},
                {"SYSTEMROOT": str(root), "PATH": str(root) + os.pathsep},
            )
            for context in contexts:
                observer = observation.WindowsPeDependencyObserver(environment=context)
                with (
                    self.subTest(context=context),
                    patch.object(observation.sys, "platform", "win32"),
                    patch.dict(
                        os.environ, {"SystemRoot": str(root), "PATH": str(root)}
                    ),
                    patch.object(
                        observation, "_find_windows_pe_inspector"
                    ) as inspector,
                ):
                    with self.assertRaises(
                        (ValueError, observation.DependencyObservationError)
                    ):
                        observer.observe({}, root_ref="fixture")
                    inspector.assert_not_called()

    def test_ambiguous_or_invalid_context_is_not_silently_normalized(self):
        for context in (
            {"PATH": "one", "Path": "two"},
            {"PATH": None},
            {"A": "bad\x00value"},
            {"A=B": "value"},
            {"X" * 129: "value"},
        ):
            with self.subTest(context=context), self.assertRaises(ValueError):
                observation.PortableHostDependencyObserver(windows_environment=context)
