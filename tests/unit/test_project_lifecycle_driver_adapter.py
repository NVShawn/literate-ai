"""Focused tests for the public external lifecycle-driver adapter."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.project_lifecycle_driver import (
    ProjectLifecycleDriverAdapterError,
    bind_external_project_lifecycle_driver,
    lifecycle_driver_environment_identity_material,
    lifecycle_driver_implementation_identity,
)
from literate_ai.contracts import (
    MINIMUM_PROJECT_REBUILD_PHASES,
    ProjectLifecycleDriver,
    canonical_identity,
)


class ProjectLifecycleDriverAdapterTests(unittest.TestCase):
    def _binding(self, root: Path):
        implementation = root / "driver.py"
        implementation.write_text("print('driver')\n", encoding="utf-8")
        project = SimpleNamespace(root=root)
        provisional = ProjectLifecycleDriver(
            driver_id="fixture",
            version="1.0.0",
            implementation_paths=("driver.py",),
            implementation_identity=canonical_identity({"provisional": True}),
            argv=(
                "{python}",
                "driver.py",
                "{project}",
                "{specification}",
                "{runtime_root}",
                "{candidate_receipt}",
                "{project_revision_identity}",
                "{lifecycle_request_identity}",
                "{flavor_args}",
                "{allow_host_execution}",
            ),
            environment_keys=("PATH", "OPENAI_API_KEY"),
            phases=MINIMUM_PROJECT_REBUILD_PHASES,
            specification_scope="project",
        )
        identity = lifecycle_driver_implementation_identity(project, provisional)
        driver = ProjectLifecycleDriver(
            driver_id=provisional.driver_id,
            version=provisional.version,
            implementation_paths=provisional.implementation_paths,
            implementation_identity=identity,
            argv=provisional.argv,
            environment_keys=provisional.environment_keys,
            phases=provisional.phases,
            specification_scope=provisional.specification_scope,
        )
        return bind_external_project_lifecycle_driver(
            project,
            driver,
            {
                "CODING_CLI": "codex",
                "PATH": os.environ.get("PATH", ""),
                "OPENAI_API_KEY": "secret",
            },
        )

    def test_factory_binds_exact_capabilities_and_expands_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding = self._binding(root)
            identity = canonical_identity({"fixture": "request"})
            command = binding.command(
                specification=root,
                runtime_root=root / "runtime",
                candidate_receipt=root / "candidate.json",
                project_revision=identity,
                request_identity=identity,
                flavor_selectors=("+python", "-bazel"),
                allow_host_execution=False,
            )

            self.assertEqual(command[0], str(binding.executable))
            self.assertEqual(
                command[-4:], ("--flavor", "+python", "--flavor", "-bazel")
            )
            self.assertNotIn("--allow-host-execution", command)
            self.assertEqual(
                binding.implementation_identity,
                binding.current_implementation_identity(),
            )
            self.assertNotIn(
                "secret", json.dumps(binding.environment_identity_material)
            )
            self.assertEqual(
                binding.environment_identity_material["credential_key_presence"],
                ["OPENAI_API_KEY"],
            )

    def test_factory_rejects_implementation_drift_with_stable_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binding = self._binding(root)
            (root / "driver.py").write_text("print('changed')\n", encoding="utf-8")

            with self.assertRaises(ProjectLifecycleDriverAdapterError) as raised:
                bind_external_project_lifecycle_driver(
                    binding.project,
                    binding.driver,
                    {"PATH": os.environ.get("PATH", "")},
                )

            self.assertEqual(
                raised.exception.code, "rebuild.driver_implementation_mismatch"
            )

    @unittest.skipIf(
        sys.platform == "win32", "Windows venv launchers are regular files"
    )
    def test_python_binding_detects_invocation_symlink_retargeting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = root / "python-real-one"
            second = root / "python-real-two"
            first.write_bytes(b"same launcher bytes")
            second.write_bytes(b"same launcher bytes")
            first.chmod(0o755)
            second.chmod(0o755)
            invocation = root / "python"
            invocation.symlink_to(first)
            with mock.patch(
                "literate_ai.adapters.project_lifecycle_driver.sys.executable",
                str(invocation),
            ):
                binding = self._binding(root)

            self.assertEqual(binding.executable, invocation)
            binding.require_executable_unchanged()
            invocation.unlink()
            invocation.symlink_to(second)
            with self.assertRaises(ProjectLifecycleDriverAdapterError):
                binding.require_executable_unchanged()

    def test_failed_process_preserves_rebuild_error_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binding = self._binding(Path(temporary).resolve())

            with self.assertRaises(ProjectLifecycleDriverAdapterError) as raised:
                binding.run(
                    (
                        sys.executable,
                        "-c",
                        "import sys; print('specific failure', file=sys.stderr); "
                        "raise SystemExit(7)",
                    )
                )

            self.assertEqual(raised.exception.code, "rebuild.driver_failed")
            self.assertIn("exit status 7", raised.exception.message)
            self.assertIn("specific failure", raised.exception.message)

    def test_implementation_identity_ignores_dot_prefixed_filesystem_cruft(
        self,
    ) -> None:
        # Regression for issue #125: the TCB digest churned between measurements of
        # a byte-for-byte-clean git tree, on two independent machines, then reverted.
        # Root cause: the directory scan walked the raw filesystem and picked up a
        # file that a *global* `core.excludesFile` (not this repo's own .gitignore)
        # hides from `git status`/`git diff` -- e.g. an editor/OS/tool artifact such
        # as `.DS_Store` or a nested `.claude/settings.local.json`. Such a file is
        # invisible to `scripts/review_lifecycle_driver.py`'s git-diff-based drift
        # report yet silently changed the hashed member set. Dot-prefixed entries
        # under a declared implementation path must never affect the digest.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            implementation_root = root / "driver_impl"
            implementation_root.mkdir()
            (implementation_root / "driver.py").write_text(
                "print('driver')\n", encoding="utf-8"
            )
            project = SimpleNamespace(root=root)
            driver = ProjectLifecycleDriver(
                driver_id="fixture",
                version="1.0.0",
                implementation_paths=("driver_impl",),
                implementation_identity=canonical_identity({"provisional": True}),
                argv=(
                    "{python}",
                    "driver_impl/driver.py",
                    "{project}",
                    "{specification}",
                    "{runtime_root}",
                    "{candidate_receipt}",
                    "{project_revision_identity}",
                    "{lifecycle_request_identity}",
                    "{allow_host_execution}",
                ),
                environment_keys=("PATH",),
                phases=MINIMUM_PROJECT_REBUILD_PHASES,
                specification_scope="project",
            )

            baseline = lifecycle_driver_implementation_identity(project, driver)

            cruft_directory = implementation_root / ".claude"
            cruft_directory.mkdir()
            (cruft_directory / "settings.local.json").write_text(
                '{"probe": true}\n', encoding="utf-8"
            )
            (implementation_root / ".DS_Store").write_bytes(b"not real source")

            with_cruft = lifecycle_driver_implementation_identity(project, driver)
            self.assertEqual(
                baseline,
                with_cruft,
                "dot-prefixed filesystem entries must not affect the TCB digest",
            )

            for entry in (
                cruft_directory / "settings.local.json",
                cruft_directory,
                implementation_root / ".DS_Store",
            ):
                if entry.is_dir():
                    entry.rmdir()
                else:
                    entry.unlink()

            after_removal = lifecycle_driver_implementation_identity(project, driver)
            self.assertEqual(baseline, after_removal)

    def test_environment_identity_never_binds_credential_values(self) -> None:
        first = lifecycle_driver_environment_identity_material(
            {
                "PATH": "/one",
                "CODEX_API_KEY": "secret-one",
                "LITAI_INHERITED_SESSION_AUTH_KEY": "aa" * 32,
            }
        )
        second = lifecycle_driver_environment_identity_material(
            {
                "PATH": "/one",
                "CODEX_API_KEY": "secret-two",
                "LITAI_INHERITED_SESSION_AUTH_KEY": "bb" * 32,
            }
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first["credential_key_presence"],
            ["CODEX_API_KEY", "LITAI_INHERITED_SESSION_AUTH_KEY"],
        )
        self.assertNotIn("secret-one", json.dumps(first))

    def test_standard_driver_implementation_identity_uses_distribution(self) -> None:
        # #215: a Standard-bound driver has no implementation_paths; the
        # implementation identity is its framework distribution identity, and it
        # must not raise AttributeError (which crashed the checkpointed runner).
        from literate_ai.contracts import (
            ContentIdentity,
            StandardProjectLifecycleDriver,
        )

        driver = StandardProjectLifecycleDriver(
            framework_distribution_identity=ContentIdentity.parse_uri(
                "sha256:" + "c" * 64
            ),
            policy_identity=ContentIdentity.parse_uri("sha256:" + "a" * 64),
        )
        identity = lifecycle_driver_implementation_identity(
            SimpleNamespace(root=Path("/tmp")), driver
        )
        # Deterministic and derived from the distribution; a different
        # distribution yields a different identity.
        other = StandardProjectLifecycleDriver(
            framework_distribution_identity=ContentIdentity.parse_uri(
                "sha256:" + "d" * 64
            ),
            policy_identity=ContentIdentity.parse_uri("sha256:" + "a" * 64),
        )
        other_identity = lifecycle_driver_implementation_identity(
            SimpleNamespace(root=Path("/tmp")), other
        )
        self.assertNotEqual(identity, other_identity)
        self.assertEqual(
            identity,
            lifecycle_driver_implementation_identity(
                SimpleNamespace(root=Path("/tmp")), driver
            ),
        )


if __name__ == "__main__":
    unittest.main()
