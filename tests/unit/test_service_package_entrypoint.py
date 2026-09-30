"""Regression for ACCEPTANCE-SERVICE-001 (#213).

The project package plan must carry the Component's real declared entrypoint
kind. Hard-coding "application" made a persistent-service Component fall through
to the independent-acceptance-exempt path so its service acceptance oracle never
ran -- a silent false-pass.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from literate_ai.adapters.lifecycle.standard_local import (
    LocalStandardLifecycleError,
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.standard_project import _runtime_command
from literate_ai.contracts import (
    StandardArtifactLayout,
    StandardLanguageRuntimeStrategy,
)
from literate_ai.contracts.components import (
    LITAI_SERVE_MODE_FLAG,
    LITAI_SMOKE_MODE_FLAG,
    LITAI_TEST_MODE_FLAG,
    PERSISTENT_SERVICE_ENTRYPOINT_KIND,
)
from literate_ai.contracts.executable_components.commands import ComponentCommandPhase


def _lock(root_id: str, entrypoints: list[SimpleNamespace]) -> SimpleNamespace:
    """A stub ComponentLock exposing only what _root_package_entrypoint reads."""

    revision = SimpleNamespace(
        identity=root_id,
        definition=SimpleNamespace(entrypoints=tuple(entrypoints)),
    )
    node = SimpleNamespace(revision=revision)
    return SimpleNamespace(root_revision=root_id, nodes=(node,))


class RootPackageEntrypointKindTests(unittest.TestCase):
    def setUp(self) -> None:
        # _root_package_entrypoint only reads its argument; no adapter state.
        self.ports = LocalStandardLifecyclePorts.__new__(LocalStandardLifecyclePorts)

    def test_persistent_service_kind_is_carried_into_the_package_plan(self) -> None:
        entrypoint = SimpleNamespace(
            name="service", kind=PERSISTENT_SERVICE_ENTRYPOINT_KIND, path="server.mjs"
        )
        name, kind = self.ports._root_package_entrypoint(_lock("root", [entrypoint]))
        # This is the fix: the real declared kind flows through, so independent
        # acceptance routes to the persistent-service oracle instead of exempt.
        self.assertEqual(name, "service")
        self.assertEqual(kind, PERSISTENT_SERVICE_ENTRYPOINT_KIND)

    def test_portable_application_kind_is_preserved(self) -> None:
        entrypoint = SimpleNamespace(
            name="run", kind="portable-application", path="main.py"
        )
        name, kind = self.ports._root_package_entrypoint(_lock("root", [entrypoint]))
        self.assertEqual(name, "run")
        self.assertEqual(kind, "portable-application")

    def test_no_entrypoint_falls_back_to_application(self) -> None:
        name, kind = self.ports._root_package_entrypoint(_lock("root", []))
        self.assertEqual((name, kind), ("application", "application"))

    def test_missing_root_node_falls_back_to_application(self) -> None:
        lock = _lock("root", [SimpleNamespace(name="x", kind="y", path="z")])
        lock.root_revision = "different-root"
        name, kind = self.ports._root_package_entrypoint(lock)
        self.assertEqual((name, kind), ("application", "application"))


def _language_profile(strategy: StandardLanguageRuntimeStrategy) -> SimpleNamespace:
    """Minimal stub carrying only what _runtime_command reads."""

    return SimpleNamespace(
        runtime_strategy=strategy,
        artifact_layout=StandardArtifactLayout.TREE,
        artifact_entrypoint="source/main",
    )


class RuntimeCommandModeTests(unittest.TestCase):
    """Regression for ACCEPTANCE-SERVICE-002 (#214): serve vs one-shot modes."""

    def test_portable_execute_stays_one_shot_smoke(self) -> None:
        for strategy in StandardLanguageRuntimeStrategy:
            with self.subTest(strategy=strategy):
                argv = _runtime_command(
                    _language_profile(strategy),
                    single_file=False,
                    phase=ComponentCommandPhase.EXECUTE,
                )
                # The portable-application EXECUTE mode must remain one-shot smoke;
                # only the persistent-service serve launch is changed by #214.
                self.assertEqual(argv[-1], LITAI_SMOKE_MODE_FLAG)
                self.assertNotIn(LITAI_SERVE_MODE_FLAG, argv)

    def test_test_phase_stays_litai_test(self) -> None:
        for strategy in StandardLanguageRuntimeStrategy:
            with self.subTest(strategy=strategy):
                argv = _runtime_command(
                    _language_profile(strategy),
                    single_file=False,
                    phase=ComponentCommandPhase.TEST,
                )
                self.assertEqual(argv[-1], LITAI_TEST_MODE_FLAG)


class PackagedServiceArgvTests(unittest.TestCase):
    """The persistent-service launch must serve, not run one smoke case (#214)."""

    def setUp(self) -> None:
        self.ports = LocalStandardLifecyclePorts.__new__(LocalStandardLifecyclePorts)

    def test_serve_argv_swaps_trailing_smoke_for_serve(self) -> None:
        execute_argv = (
            "{tool}",
            "-c",
            "driver",
            "{artifact_root}",
            "{export_path}",
            "tree",
            "source/main.py",
            LITAI_SMOKE_MODE_FLAG,
        )
        custody = object()
        packaged_argv = Mock(return_value=execute_argv)
        self.ports._packaged_argv = packaged_argv
        serve_argv = self.ports._packaged_service_argv(custody)
        packaged_argv.assert_called_once_with(
            custody, ComponentCommandPhase.EXECUTE, None
        )
        # The one-shot smoke mode is replaced by the served mode; nothing else
        # in the launch command changes, so the exact same artifact binds and
        # listens instead of running one case and exiting.
        self.assertEqual(serve_argv[-1], LITAI_SERVE_MODE_FLAG)
        self.assertNotIn(LITAI_SMOKE_MODE_FLAG, serve_argv)
        self.assertEqual(serve_argv[:-1], execute_argv[:-1])

    def test_serve_argv_requires_the_expected_trailing_mode(self) -> None:
        # Fail closed if the packaged EXECUTE argv is not the expected one-shot
        # smoke command, rather than launching an unknown command as a server.
        custody = object()
        packaged_argv = Mock(return_value=("{tool}", "-c", "driver", "{artifact_root}"))
        self.ports._packaged_argv = packaged_argv
        with self.assertRaises(LocalStandardLifecycleError):
            self.ports._packaged_service_argv(custody)
        packaged_argv.assert_called_once_with(
            custody, ComponentCommandPhase.EXECUTE, None
        )


if __name__ == "__main__":
    unittest.main()
