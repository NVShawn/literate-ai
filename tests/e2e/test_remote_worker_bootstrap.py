from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from literate_ai.cli.worker import worker_from_args
from literate_ai.remote_worker_bootstrap import (
    WorkerBootstrapError,
    _inspect_wheel,
    _manifest_identity,
    _run_with_tree_kill,
    bootstrap_worker_distribution,
    verify_worker_wheelhouse,
)


def _record_digest(content: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(content).digest()
    ).rstrip(b"=").decode("ascii")


def _wheel(
    path: Path,
    *,
    name: str = "literate-ai",
    startup_path: bool = False,
    corrupt_record: bool = False,
    reverse_members: bool = False,
    requires_dist: tuple[str, ...] = (),
) -> str:
    dist_info = f"{name.replace('-', '_')}-0.3.0.dist-info"
    entries = {
        "literate_ai/__init__.py": b"",
        "literate_ai/standard_policies/default.json": b"{}\n",
        "literate_ai-0.3.0.data/data/share/literate-ai/schemas/v1/core.json": b"{}\n",
        f"{dist_info}/METADATA": (
            (
                f"Metadata-Version: 2.4\nName: {name}\nVersion: 0.3.0\n"
                + "".join(f"Requires-Dist: {item}\n" for item in requires_dist)
                + "\n"
            ).encode()
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n"
        ),
    }
    if startup_path:
        entries["bootstrap.pth"] = b"import untrusted\n"
    record_path = f"{dist_info}/RECORD"
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for member in sorted(entries):
        content = entries[member]
        digest = (
            "sha256=" + "A" * 43
            if corrupt_record and member == "literate_ai/__init__.py"
            else _record_digest(content)
        )
        writer.writerow((member, digest, str(len(content))))
    writer.writerow((record_path, "", ""))
    entries[record_path] = stream.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in sorted(entries, reverse=reverse_members):
            info = zipfile.ZipInfo(member)
            info.create_system = 3
            info.external_attr = (0o100644) << 16
            archive.writestr(info, entries[member])
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(
    root: Path,
    wheels: tuple[tuple[Path, str], ...],
    *,
    framework: str,
    distribution_identity: str = "sha256:" + "1" * 64,
) -> tuple[Path, str]:
    entries = [
        _inspect_wheel(path, digest, required_name=None) for path, digest in wheels
    ]
    entries.sort(key=lambda item: str(item["filename"]))
    document = {
        "schema": "literate-ai/worker-wheelhouse-manifest@1",
        "framework": framework,
        "framework_distribution_identity": distribution_identity,
        "target": {
            "abi": None,
            "implementation": None,
            "platform": None,
            "python_version": None,
        },
        "wheels": entries,
    }
    identity = _manifest_identity(document)
    path = root / "worker-wheelhouse.json"
    path.write_bytes(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    return path, identity


class RemoteWorkerBootstrapTests(unittest.TestCase):
    def test_worker_bootstrap_verb_returns_verified_launcher(self) -> None:
        args = Namespace(
            worker_command="bootstrap",
            wheel="framework.whl",
            wheelhouse=None,
            wheel_sha256="1" * 64,
            manifest=None,
            closure_identity=None,
            capability_root=None,
            capability_manifest=None,
            capability_identity=None,
            distribution_identity="sha256:" + "2" * 64,
            install_root="isolated",
            replace_existing=False,
        )
        with patch(
            "literate_ai.remote_worker_bootstrap.bootstrap_worker_distribution",
            return_value=Path("isolated/bin/litai"),
        ) as bootstrap:
            result, status = worker_from_args(args)
        self.assertEqual(status, 0)
        self.assertEqual(result["schema"], "literate-ai/worker-bootstrap-result@1")
        self.assertEqual(result["distribution_identity"], "sha256:" + "2" * 64)
        bootstrap.assert_called_once()

    def test_digest_mismatch_fails_before_wheel_parsing_or_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "literate_ai.whl"
            digest = _wheel(wheel)
            with (
                self.assertRaises(WorkerBootstrapError) as raised,
                patch("literate_ai.remote_worker_bootstrap._run_with_tree_kill") as run,
            ):
                bootstrap_worker_distribution(
                    wheel,
                    expected_wheel_sha256="0" * 64,
                    expected_distribution_identity="sha256:" + "1" * 64,
                    install_root=Path(directory) / "environment",
                )
            self.assertNotEqual(digest, "0" * 64)
            self.assertEqual(raised.exception.code, "worker.bootstrap_digest_mismatch")
            run.assert_not_called()

    def test_tampered_dependency_and_omitted_transitive_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            framework = root / "literate_ai.whl"
            framework_digest = _wheel(framework, requires_dist=("jsonschema==4.26.0",))
            dependency = root / "jsonschema.whl"
            dependency_digest = _wheel(
                dependency, name="jsonschema", requires_dist=("attrs==26.1.0",)
            )
            manifest, identity = _manifest(
                root,
                (
                    (framework, framework_digest),
                    (dependency, dependency_digest),
                ),
                framework=framework.name,
            )
            with self.assertRaises(WorkerBootstrapError) as raised:
                verify_worker_wheelhouse(root, manifest, identity)
            self.assertEqual(
                raised.exception.code, "worker.bootstrap_dependency_missing"
            )

            transitive = root / "attrs.whl"
            transitive_digest = _wheel(transitive, name="attrs")
            manifest, identity = _manifest(
                root,
                (
                    (framework, framework_digest),
                    (dependency, dependency_digest),
                    (transitive, transitive_digest),
                ),
                framework=framework.name,
            )
            transitive.write_bytes(transitive.read_bytes() + b"tampered")
            with self.assertRaises(WorkerBootstrapError) as raised:
                verify_worker_wheelhouse(root, manifest, identity)
            self.assertEqual(raised.exception.code, "worker.bootstrap_digest_mismatch")

    def test_existing_install_drift_does_not_start_an_installer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "literate_ai.whl"
            digest = _wheel(wheel)
            install_root = root / "environment"
            install_root.mkdir()
            with (
                patch(
                    "literate_ai.remote_worker_bootstrap.installed_distribution_identity",
                    return_value="sha256:" + "2" * 64,
                ),
                patch("literate_ai.remote_worker_bootstrap._run_with_tree_kill") as run,
                self.assertRaises(WorkerBootstrapError) as raised,
            ):
                bootstrap_worker_distribution(
                    wheel,
                    expected_wheel_sha256=digest,
                    expected_distribution_identity="sha256:" + "1" * 64,
                    install_root=install_root,
                )
            self.assertEqual(
                raised.exception.code, "worker.bootstrap_existing_mismatch"
            )
            run.assert_not_called()


class RunWithTreeKillTests(unittest.TestCase):
    """Regression coverage for #79: this module intentionally uses only the
    Python standard library (staged and executed before an ambient
    literate-ai installation can be trusted), so it cannot import the shared
    `literate_ai.adapters._processes.run_with_tree_kill` helper and instead
    carries its own self-contained implementation. That implementation must
    still kill the whole process tree -- not just the direct child -- on
    timeout."""

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_timeout_kills_the_whole_process_tree_not_just_the_direct_child(
        self,
    ) -> None:
        """Reproducer from the issue: a wrapper that daemonizes a sleeper
        (here, a `pip`/`node`-shaped grandchild), then the runner times out.
        The sleeper must not remain."""

        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "grandchild.pid"
            grandchild_script = Path(temporary) / "grandchild.py"
            grandchild_script.write_text(
                "import os, time\n"
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            parent_script = Path(temporary) / "parent.py"
            parent_script.write_text(
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, {str(grandchild_script)!r}])\n"
                "time.sleep(60)\n"
            )

            with self.assertRaises(subprocess.TimeoutExpired):
                _run_with_tree_kill(
                    (sys.executable, str(parent_script)),
                    timeout=1,
                )

            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "grandchild never started")
            grandchild_pid = int(pid_file.read_text().strip())

            deadline = time.monotonic() + 5
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(
                alive, "grandchild survived timeout; only the direct child was killed"
            )


if __name__ == "__main__":
    unittest.main()


class FreeBSDWheelhouseTests(unittest.TestCase):
    def test_offline_native_wheels_export_and_tampering_is_rejected(self):
        from literate_ai.remote_worker_bootstrap import export_worker_wheelhouse

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "native"
            source.mkdir()
            dependency = source / "native_dep-0.3.0-py3-none-any.whl"
            _wheel(dependency, name="native-dep")
            framework = root / "literate_ai-0.3.0-py3-none-any.whl"
            _wheel(framework, requires_dist=("native-dep==0.3.0",))
            original = dependency.read_bytes()
            manifest, identity = export_worker_wheelhouse(
                framework,
                root / "export",
                framework_distribution_identity="sha256:" + "4" * 64,
                platform="freebsd_16_0_current_amd64",
                python_version="3.12",
                implementation="cp",
                abi="cp312",
                wheel_directory=source,
            )
            value, _identity, _framework = verify_worker_wheelhouse(
                manifest.parent,
                manifest,
                identity,
                require_host_compatible=False,
            )
            self.assertEqual(
                {w["distribution_name"] for w in value["wheels"]},
                {"literate-ai", "native-dep"},
            )
            self.assertEqual(dependency.read_bytes(), original)
            self.assertNotIn(str(source), manifest.read_text())
            copied = manifest.parent / dependency.name
            copied.write_bytes(copied.read_bytes() + b"changed")
            with self.assertRaises(WorkerBootstrapError):
                verify_worker_wheelhouse(
                    manifest.parent, manifest, identity, require_host_compatible=False
                )

    def test_offline_source_refuses_urls_before_invoking_pip(self):
        from literate_ai.remote_worker_bootstrap import export_worker_wheelhouse

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "native"
            source.mkdir()
            framework = root / "literate_ai-0.3.0-py3-none-any.whl"
            _wheel(framework)
            dependency = source / "native_dep-0.3.0-py3-none-any.whl"
            _wheel(
                dependency,
                name="native-dep",
                requires_dist=("other @ https://example.invalid/other.whl",),
            )
            with patch(
                "literate_ai.remote_worker_bootstrap._wheelhouse_downloader"
            ) as downloader:
                with self.assertRaises(WorkerBootstrapError) as caught:
                    export_worker_wheelhouse(
                        framework,
                        root / "export",
                        framework_distribution_identity="sha256:" + "4" * 64,
                        wheel_directory=source,
                    )
            downloader.assert_not_called()
            self.assertEqual(
                caught.exception.code, "worker.wheelhouse_requirement_invalid"
            )
            self.assertFalse((root / "export").exists())

    def test_foreign_kernel_markers_cannot_silently_omit_a_dependency(self):
        from literate_ai.remote_worker_bootstrap import _close_target_requirements

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            framework = root / "literate_ai-0.3.0-py3-none-any.whl"
            _wheel(framework, requires_dist=('native-dep; platform_release == "16.0"',))
            with (
                patch("sys.platform", "darwin"),
                patch(
                    "literate_ai.remote_worker_bootstrap._download_wheels"
                ) as download,
            ):
                with self.assertRaises(WorkerBootstrapError) as caught:
                    _close_target_requirements(
                        root,
                        downloader=("unused",),
                        framework_name=framework.name,
                        platform="freebsd_16_0_current_amd64",
                        python_version="3.12",
                        implementation="cp",
                        abi="cp312",
                    )
            self.assertEqual(caught.exception.code, "worker.wheelhouse_marker_unproven")
            download.assert_not_called()

    def test_freebsd_host_tags_and_dependency_markers_do_not_become_linux(self):
        from literate_ai.remote_worker_bootstrap import (
            _host_target_compatible,
            _target_marker_environment,
        )

        target = {
            "python_version": None,
            "implementation": None,
            "abi": None,
            "platform": "freebsd_16_0_current_amd64",
        }
        with (
            patch("os.name", "posix"),
            patch("sys.platform", "freebsd16"),
            patch("sysconfig.get_platform", return_value="freebsd-16.0-CURRENT-amd64"),
        ):
            self.assertTrue(_host_target_compatible(target))
            self.assertFalse(
                _host_target_compatible({**target, "platform": "manylinux_2_17_x86_64"})
            )
            self.assertFalse(
                _host_target_compatible({**target, "platform": "freebsd_15_0_amd64"})
            )
        environment = _target_marker_environment(
            platform=target["platform"], python_version="3.12", implementation="cp"
        )
        self.assertEqual(environment["sys_platform"], "freebsd16")
        self.assertEqual(environment["platform_system"], "FreeBSD")
        self.assertEqual(environment["platform_machine"], "amd64")
        with self.assertRaises(WorkerBootstrapError):
            _target_marker_environment(
                platform="unknown_amd64", python_version=None, implementation=None
            )


@unittest.skipIf(os.name == "nt", "POSIX symlink source refusal")
class OfflineWheelLinkTests(unittest.TestCase):
    def test_redirected_native_wheel_is_rejected_before_resolver(self):
        from literate_ai.remote_worker_bootstrap import export_worker_wheelhouse

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "native"
            source.mkdir()
            framework = root / "literate_ai-0.3.0-py3-none-any.whl"
            _wheel(framework)
            (source / framework.name).symlink_to(framework)
            with patch(
                "literate_ai.remote_worker_bootstrap._wheelhouse_downloader"
            ) as downloader:
                with self.assertRaises(WorkerBootstrapError):
                    export_worker_wheelhouse(
                        framework,
                        root / "export",
                        framework_distribution_identity="sha256:" + "4" * 64,
                        wheel_directory=source,
                    )
            downloader.assert_not_called()
            self.assertFalse((root / "export").exists())
