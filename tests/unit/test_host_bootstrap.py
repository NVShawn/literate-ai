"""Worker bootstrap contracts over the shared host-install adapter."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPOSITORY = Path(__file__).resolve().parents[2]
BOOTSTRAP = (
    REPOSITORY
    / "skills"
    / "agent"
    / "detect-before-install"
    / "scripts"
    / "bootstrap.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("litai_host_bootstrap", BOOTSTRAP)
    if spec is None or spec.loader is None:
        raise AssertionError("host bootstrap module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@dataclass(frozen=True)
class _Requirement:
    capability: str


@dataclass(frozen=True)
class _Observation:
    requirement: _Requirement
    ready: bool
    executable: Path | None


class _Warning:
    def to_dict(self) -> dict[str, object]:
        return {
            "code": "host-install.duplicate-capability",
            "capability": "python",
            "sources": ["os-base", "lang-python"],
        }


class _Report:
    def __init__(self, *, ready: bool, capabilities: tuple[str, ...]) -> None:
        self.ready = ready
        self.coding_agent = "codex"
        self.managed_paths = (Path("/managed/bin"),)
        self.observations = tuple(
            _Observation(
                _Requirement(capability),
                ready,
                Path(f"/tools/{capability}"),
            )
            for capability in capabilities
        )
        self.missing = () if ready else self.observations
        self.sbom = SimpleNamespace(base=SimpleNamespace(warnings=(_Warning(),)))

    def to_dict(self) -> dict[str, object]:
        return {"schema": "literate-ai/host-install-report@2", "ready": self.ready}


class HostBootstrapTests(unittest.TestCase):
    def _invoke(
        self,
        *,
        argv: list[str],
        before: _Report,
        after: _Report | None = None,
        platform: str = "linux",
        load_error=None,
    ) -> tuple[int, dict[str, object], object]:
        bootstrap = _module()
        stdout = io.StringIO()
        target = object()
        sbom = object()
        load = (
            mock.Mock(side_effect=load_error)
            if load_error is not None
            else mock.Mock(return_value=(Path("/flavors/tuple.cdx.json"), sbom))
        )
        ensured = after or before
        with (
            mock.patch.dict(
                os.environ, {"PATH": "/bin", "HOME": "/home/test"}, clear=True
            ),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(bootstrap, "_platform_family", return_value=platform),
            mock.patch.object(
                bootstrap, "detect_current_install_target", return_value=target
            ),
            mock.patch.object(bootstrap, "load_host_install_sbom", load),
            mock.patch.object(
                bootstrap, "observe_host_install_dependencies", return_value=before
            ) as observe,
            mock.patch.object(
                bootstrap, "ensure_host_install_dependencies", return_value=ensured
            ) as ensure,
            mock.patch.object(bootstrap, "apply_host_install_path") as apply_path,
            mock.patch.object(
                bootstrap,
                "_synchronize_clock",
                return_value={"attempted": True, "status": "failed"},
            ),
        ):
            code = bootstrap.main()
        return (
            code,
            json.loads(stdout.getvalue()),
            SimpleNamespace(
                load=load,
                observe=observe,
                ensure=ensure,
                apply_path=apply_path,
                target=target,
                sbom=sbom,
            ),
        )

    def test_flavor_selector_reduces_only_host_toolchain_contributors(self) -> None:
        bootstrap = _module()
        self.assertEqual(
            bootstrap._flavor_names(
                [
                    "+flavor://literate-ai/lang-javascript",
                    "language:lang-javascript",
                    "+flavor://literate-ai/build-cmake",
                    "+flavor://literate-ai/spec-style",
                    "-flavor://literate-ai/lang-rust",
                ]
            ),
            ("lang-javascript", "build-cmake"),
        )

    def test_base_stage_delegates_without_language_or_build_capabilities(self) -> None:
        before = _Report(
            ready=True,
            capabilities=("python", "python-venv", "git", "git-lfs", "codex"),
        )
        code, report, calls = self._invoke(
            argv=["bootstrap.py", "--platform", "linux"], before=before
        )

        self.assertEqual(code, 0)
        self.assertTrue(report["passed"])
        self.assertEqual(report["selected_flavors"], [])
        self.assertNotIn("node", report["after"])
        self.assertNotIn("make", report["after"])
        calls.ensure.assert_not_called()
        calls.load.assert_called_once_with(
            REPOSITORY / "flavors", calls.target, flavor_names=()
        )

    def test_selected_stage_forwards_flavor_mixins_and_exact_agent(self) -> None:
        before = _Report(ready=False, capabilities=("node", "make", "codex"))
        after = _Report(ready=True, capabilities=("node", "make", "codex"))
        code, report, calls = self._invoke(
            argv=[
                "bootstrap.py",
                "--platform",
                "linux",
                "--install-missing",
                "--coding-cli",
                "codex",
                "--flavor",
                "lang-javascript",
                "--flavor",
                "build-make",
            ],
            before=before,
            after=after,
        )

        self.assertEqual(code, 0)
        self.assertEqual(report["selected_flavors"], ["lang-javascript", "build-make"])
        self.assertEqual(report["coding_agent"], "codex")
        self.assertEqual(
            report["composition_warnings"][0]["code"],
            "host-install.duplicate-capability",
        )
        calls.ensure.assert_called_once()
        arguments = calls.ensure.call_args.kwargs
        self.assertEqual(arguments["flavor_names"], ("lang-javascript", "build-make"))
        self.assertEqual(arguments["environment"]["CODING_CLI"], "codex")
        self.assertEqual(arguments["environment"]["LITAI_INSTALL_DEPENDENCIES"], "yes")
        calls.apply_path.assert_called_once_with(after)

    def test_no_install_reports_missing_without_mutating_host(self) -> None:
        before = _Report(ready=False, capabilities=("python", "git", "codex"))
        code, report, calls = self._invoke(
            argv=["bootstrap.py", "--platform", "linux"], before=before
        )

        self.assertEqual(code, 1)
        self.assertFalse(report["passed"])
        self.assertEqual(report["missing"], ["python", "git", "codex"])
        calls.ensure.assert_not_called()

    def test_platform_mismatch_is_a_structured_failure(self) -> None:
        before = _Report(ready=True, capabilities=("python",))
        code, report, calls = self._invoke(
            argv=["bootstrap.py", "--platform", "windows"],
            before=before,
            platform="linux",
        )

        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "host-install.platform-mismatch")
        calls.load.assert_not_called()

    def test_adapter_load_failure_is_a_structured_failure(self) -> None:
        bootstrap = _module()
        before = _Report(ready=True, capabilities=("python",))
        error = bootstrap.HostInstallError("host-install.test", "failure")
        code, report, calls = self._invoke(
            argv=["bootstrap.py", "--platform", "linux"],
            before=before,
            load_error=error,
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            report["error"],
            {"code": "host-install.test", "message": "failure"},
        )
        calls.observe.assert_not_called()

    def test_clock_sync_failure_does_not_override_toolchain_readiness(self) -> None:
        before = _Report(ready=True, capabilities=("python", "git", "codex"))
        code, report, _calls = self._invoke(
            argv=["bootstrap.py", "--platform", "linux"], before=before
        )

        self.assertEqual(code, 0)
        self.assertTrue(report["passed"])
        self.assertEqual(report["clock_sync"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
