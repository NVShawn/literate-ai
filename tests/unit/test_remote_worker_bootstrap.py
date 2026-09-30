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

from literate_ai.cli.errors import CliFailure
from literate_ai.cli.worker import worker_from_args
from literate_ai.remote_worker_bootstrap import (
    WorkerBootstrapError,
    _close_target_requirements,
    _inspect_wheel,
    _manifest_identity,
    _run_with_tree_kill,
    bootstrap_worker_distribution,
    bootstrap_worker_wheelhouse,
    installed_distribution_identity,
    verify_bootstrap_wheel,
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


def _installed_framework_tree(
    environment: Path,
    *,
    package_content: bytes = b"framework payload\n",
    uv_cache: bytes | None = None,
    uv_build: bytes | None = None,
) -> Path:
    site_packages = environment / "lib/python3.12/site-packages"
    package = site_packages / "literate_ai/__init__.py"
    dist_info = site_packages / "literate_ai-0.3.0.dist-info"
    package.parent.mkdir(parents=True)
    dist_info.mkdir(parents=True)
    package.write_bytes(package_content)
    metadata = dist_info / "METADATA"
    metadata.write_bytes(
        b"Metadata-Version: 2.4\nName: literate-ai\nVersion: 0.3.0\n\n"
    )
    wheel = dist_info / "WHEEL"
    wheel.write_bytes(b"Wheel-Version: 1.0\nTag: py3-none-any\n")
    entries = [package, metadata, wheel]
    if uv_cache is not None:
        cache = dist_info / "uv_cache.json"
        cache.write_bytes(uv_cache)
        entries.append(cache)
    if uv_build is not None:
        build = dist_info / "uv_build.json"
        build.write_bytes(uv_build)
        entries.append(build)
    record = dist_info / "RECORD"
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for entry in entries:
        relative = entry.relative_to(site_packages).as_posix()
        content = entry.read_bytes()
        writer.writerow((relative, _record_digest(content), str(len(content))))
    writer.writerow((record.relative_to(site_packages).as_posix(), "", ""))
    record.write_text(stream.getvalue(), encoding="utf-8")
    return environment


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

    def test_wheelhouse_bootstrap_rejects_leftover_capability_arguments(self) -> None:
        args = Namespace(
            worker_command="bootstrap",
            wheel=None,
            wheelhouse="wheelhouse",
            wheel_sha256=None,
            manifest="wheelhouse/worker-wheelhouse.json",
            closure_identity="sha256:" + "3" * 64,
            capability_root="capability",
            capability_manifest="capability/manifest.json",
            capability_identity="sha256:" + "4" * 64,
            distribution_identity="sha256:" + "2" * 64,
            install_root="isolated",
            replace_existing=False,
        )
        with self.assertRaises(CliFailure) as raised:
            worker_from_args(args)
        self.assertEqual(
            raised.exception.code, "worker.bootstrap_capability_unsupported"
        )

    def test_wheelhouse_bootstrap_records_empty_capabilities(self) -> None:
        args = Namespace(
            worker_command="bootstrap",
            wheel=None,
            wheelhouse="wheelhouse",
            wheel_sha256=None,
            manifest="wheelhouse/worker-wheelhouse.json",
            closure_identity="sha256:" + "3" * 64,
            capability_root=None,
            capability_manifest=None,
            capability_identity=None,
            distribution_identity="sha256:" + "2" * 64,
            install_root="isolated",
            replace_existing=False,
        )
        installed = [{"distribution_name": "jsonschema"}]
        with patch(
            "literate_ai.remote_worker_bootstrap.bootstrap_worker_wheelhouse",
            return_value=(Path("isolated/bin/litai"), installed),
        ) as bootstrap:
            result, status = worker_from_args(args)
        self.assertEqual(status, 0)
        self.assertEqual(result["capabilities"], [])
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

    def test_single_wheel_with_jsonschema_dependency_requires_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "literate_ai.whl"
            digest = _wheel(wheel, requires_dist=("jsonschema==4.26.0",))
            with (
                self.assertRaises(WorkerBootstrapError) as raised,
                patch("literate_ai.remote_worker_bootstrap._run_with_tree_kill") as run,
            ):
                bootstrap_worker_distribution(
                    wheel,
                    expected_wheel_sha256=digest,
                    expected_distribution_identity="sha256:" + "1" * 64,
                    install_root=root / "environment",
                )
            self.assertEqual(
                raised.exception.code,
                "worker.bootstrap_dependency_closure_required",
            )
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

    def test_linux_windows_and_unsupported_wheel_rollback(self) -> None:
        for platform_name, executable in (
            ("posix", Path("bin/python")),
            ("nt", Path("Scripts/python.exe")),
        ):
            with self.subTest(platform=platform_name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    framework = root / "literate_ai.whl"
                    framework_digest = _wheel(
                        framework, requires_dist=("jsonschema==4.26.0",)
                    )
                    dependency = root / "jsonschema.whl"
                    dependency_digest = _wheel(dependency, name="jsonschema")
                    manifest, identity = _manifest(
                        root,
                        (
                            (framework, framework_digest),
                            (dependency, dependency_digest),
                        ),
                        framework=framework.name,
                    )
                    target = root / "environment"
                    selected_executable = executable
                    selected_litai = Path(
                        "Scripts/litai.cmd" if platform_name == "nt" else "bin/litai"
                    )

                    def create(
                        environment: Path, selected: Path = selected_executable
                    ) -> None:
                        (environment / selected).parent.mkdir(parents=True)
                        (environment / selected).write_bytes(b"python")

                    def python_launcher(
                        environment: Path, selected: Path = selected_executable
                    ) -> Path:
                        return environment / selected

                    def litai_launcher(
                        environment: Path, selected: Path = selected_litai
                    ) -> Path:
                        return environment / selected

                    with (
                        patch(
                            "literate_ai.remote_worker_bootstrap._python_launcher",
                            side_effect=python_launcher,
                        ),
                        patch(
                            "literate_ai.remote_worker_bootstrap._litai_launcher",
                            side_effect=litai_launcher,
                        ),
                        patch(
                            "literate_ai.remote_worker_bootstrap.venv.EnvBuilder.create",
                            side_effect=create,
                        ),
                        patch(
                            "literate_ai.remote_worker_bootstrap._run_with_tree_kill"
                        ) as run,
                        patch(
                            "literate_ai.remote_worker_bootstrap.installed_distribution_identity",
                            return_value="sha256:" + "1" * 64,
                        ),
                        patch(
                            "literate_ai.remote_worker_bootstrap._installed_distribution_record_identity",
                            return_value="sha256:" + "3" * 64,
                        ),
                    ):
                        launcher, installed = bootstrap_worker_wheelhouse(
                            root,
                            manifest,
                            expected_closure_identity=identity,
                            expected_distribution_identity="sha256:" + "1" * 64,
                            install_root=target,
                        )
                    self.assertEqual(len(installed), 2)
                    self.assertIn("--no-index", run.call_args.args[0])
                    self.assertIn("--find-links", run.call_args.args[0])
                    self.assertTrue(
                        str(launcher).endswith(
                            "litai.cmd" if platform_name == "nt" else "litai"
                        )
                    )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            framework = wheelhouse / "literate_ai.whl"
            framework_digest = _wheel(framework, requires_dist=("jsonschema==4.26.0",))
            dependency = wheelhouse / "jsonschema.whl"
            dependency_digest = _wheel(dependency, name="jsonschema")
            manifest, identity = _manifest(
                wheelhouse,
                ((framework, framework_digest), (dependency, dependency_digest)),
                framework=framework.name,
            )
            target = root / "environment"
            target.mkdir()
            marker = target / "old"
            marker.write_text("old", encoding="utf-8")
            with (
                patch("literate_ai.remote_worker_bootstrap.venv.EnvBuilder.create"),
                patch(
                    "literate_ai.remote_worker_bootstrap._run_with_tree_kill",
                    side_effect=subprocess.CalledProcessError(1, ("pip",)),
                ),
                self.assertRaises(WorkerBootstrapError) as raised,
            ):
                bootstrap_worker_wheelhouse(
                    wheelhouse,
                    manifest,
                    expected_closure_identity=identity,
                    expected_distribution_identity="sha256:" + "1" * 64,
                    install_root=target,
                    replace_existing=True,
                )
            self.assertEqual(raised.exception.code, "worker.bootstrap_install_failed")
            self.assertEqual(marker.read_text(encoding="utf-8"), "old")

    def test_unsupported_target_wheelhouse_fails_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            framework = root / "literate_ai.whl"
            digest = _wheel(framework)
            manifest, _ = _manifest(
                root, ((framework, digest),), framework=framework.name
            )
            document = json.loads(manifest.read_text(encoding="utf-8"))
            document["target"]["python_version"] = "0.0"
            identity = _manifest_identity(document)
            manifest.write_bytes(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
                + b"\n"
            )
            with (
                patch("literate_ai.remote_worker_bootstrap._run_with_tree_kill") as run,
                self.assertRaises(WorkerBootstrapError) as raised,
            ):
                bootstrap_worker_wheelhouse(
                    root,
                    manifest,
                    expected_closure_identity=identity,
                    expected_distribution_identity="sha256:" + "1" * 64,
                    install_root=root.parent / "environment",
                )
            self.assertEqual(
                raised.exception.code, "worker.bootstrap_target_incompatible"
            )
            run.assert_not_called()

    def test_export_closes_markers_against_declared_target_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            framework = root / "literate_ai.whl"
            _wheel(
                framework,
                requires_dist=('typing_extensions<5.0,>=4.6; python_version < "3.13"',),
            )

            def download(*_args, **_kwargs) -> None:
                _wheel(root / "typing_extensions.whl", name="typing-extensions")

            with patch(
                "literate_ai.remote_worker_bootstrap._run_with_tree_kill",
                side_effect=download,
            ) as run:
                _close_target_requirements(
                    root,
                    downloader=("resolver-python", "-m", "pip"),
                    framework_name=framework.name,
                    platform="manylinux_2_17_x86_64",
                    python_version="3.12",
                    implementation="cp",
                    abi="cp312",
                )
            run.assert_called_once()
            self.assertEqual(
                run.call_args.args[0][:4],
                ("resolver-python", "-m", "pip", "download"),
            )
            self.assertTrue((root / "typing_extensions.whl").is_file())

    def test_installer_bookkeeping_is_not_distribution_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pip_environment = _installed_framework_tree(root / "pip")
            uv_environment = _installed_framework_tree(
                root / "uv",
                uv_cache=b'{"cache_info":{"timestamp":1}}\n',
                uv_build=b"{}\n",
            )

            pip_identity = installed_distribution_identity(pip_environment)
            self.assertEqual(
                installed_distribution_identity(uv_environment), pip_identity
            )

            package = (
                uv_environment / "lib/python3.12/site-packages/literate_ai/__init__.py"
            )
            package.write_bytes(b"changed framework payload\n")
            self.assertNotEqual(
                installed_distribution_identity(uv_environment), pip_identity
            )

    def test_framework_metadata_remains_part_of_distribution_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = _installed_framework_tree(Path(directory) / "environment")
            original = installed_distribution_identity(environment)
            metadata = (
                environment
                / "lib/python3.12/site-packages/literate_ai-0.3.0.dist-info/METADATA"
            )
            metadata.write_bytes(metadata.read_bytes() + b"Summary: changed\n")

            self.assertNotEqual(installed_distribution_identity(environment), original)

    def test_wrong_distribution_metadata_and_startup_path_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrong = root / "wrong.whl"
            wrong_digest = _wheel(wrong, name="another-package")
            with self.assertRaises(WorkerBootstrapError) as raised:
                verify_bootstrap_wheel(wrong, wrong_digest)
            self.assertEqual(raised.exception.code, "worker.bootstrap_metadata_invalid")

            startup = root / "startup.whl"
            startup_digest = _wheel(startup, startup_path=True)
            with self.assertRaises(WorkerBootstrapError) as raised:
                verify_bootstrap_wheel(startup, startup_digest)
            self.assertEqual(raised.exception.code, "worker.bootstrap_wheel_unsafe")

    def test_canonical_empty_zip_directories_do_not_need_record_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "framework.whl"
            _wheel(wheel)
            with zipfile.ZipFile(wheel, "a") as archive:
                for name in (
                    "literate_ai/",
                    "literate_ai-0.3.0.dist-info/",
                    "literate_ai-0.3.0.dist-info/licenses/",
                ):
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = 0o040755 << 16
                    archive.writestr(info, b"")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            self.assertEqual(
                verify_bootstrap_wheel(wheel, digest), ("literate-ai", "0.3.0")
            )

    def test_directory_entries_cannot_hide_payloads_links_or_path_collisions(
        self,
    ) -> None:
        cases = (
            ("extra/", 0o040755, b"hidden payload"),
            ("extra/", 0o120777, b""),
            ("extra/", 0o100644, b""),
            ("../outside/", 0o040755, b""),
            ("C:/outside/", 0o040755, b""),
            ("literate_ai//", 0o040755, b""),
            ("LITERATE_AI/", 0o040755, b""),
            ("literate_ai/__init__.py/", 0o040755, b""),
            ("literate_ai", 0o100644, b"file instead of parent directory"),
            ("bootstrap.PTH", 0o100644, b"import untrusted"),
        )
        for name, mode, payload in cases:
            with self.subTest(name=name, mode=mode):
                with tempfile.TemporaryDirectory() as directory:
                    wheel = Path(directory) / "framework.whl"
                    _wheel(wheel)
                    with zipfile.ZipFile(wheel, "a") as archive:
                        info = zipfile.ZipInfo(name)
                        info.create_system = 3
                        info.external_attr = mode << 16
                        archive.writestr(info, payload)
                    with self.assertRaises(WorkerBootstrapError) as raised:
                        verify_bootstrap_wheel(
                            wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()
                        )
                    self.assertEqual(
                        raised.exception.code, "worker.bootstrap_wheel_unsafe"
                    )

    def test_collision_checks_include_implicit_parents_and_dotted_siblings(
        self,
    ) -> None:
        cases = (
            ("extra/a/", "EXTRA/z/"),
            ("extra", "extra.sibling/", "extra/child/"),
        )
        for names in cases:
            with self.subTest(names=names), tempfile.TemporaryDirectory() as directory:
                wheel = Path(directory) / "framework.whl"
                _wheel(wheel)
                with zipfile.ZipFile(wheel, "a") as archive:
                    for name in names:
                        info = zipfile.ZipInfo(name)
                        info.create_system = 3
                        mode = 0o040755 if name.endswith("/") else 0o100644
                        info.external_attr = mode << 16
                        archive.writestr(info, b"")
                with self.assertRaises(WorkerBootstrapError) as raised:
                    verify_bootstrap_wheel(
                        wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()
                    )
                self.assertEqual(raised.exception.code, "worker.bootstrap_wheel_unsafe")

    def test_regular_files_still_require_exact_record_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "framework.whl"
            _wheel(wheel)
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.writestr("unrecorded.py", b"print('not admitted')")
            with self.assertRaises(WorkerBootstrapError) as raised:
                verify_bootstrap_wheel(
                    wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()
                )
            self.assertEqual(raised.exception.code, "worker.bootstrap_record_invalid")

    def test_deep_archive_names_do_not_amplify_collision_check_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "framework.whl"
            _wheel(wheel)
            with zipfile.ZipFile(wheel, "a") as archive:
                info = zipfile.ZipInfo("x/" * 4096)
                info.create_system = 3
                info.external_attr = 0o040755 << 16
                archive.writestr(info, b"")
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            # An 8 KiB name must not allocate tens of MiB of copied prefixes.
            # Refusing unsupported depth is also safe; no extraction is attempted.
            probe = """
import sys
import tracemalloc
from pathlib import Path
from literate_ai.remote_worker_bootstrap import (
    WorkerBootstrapError,
    verify_bootstrap_wheel,
)
tracemalloc.start()
try:
    verify_bootstrap_wheel(Path(sys.argv[1]), sys.argv[2])
except WorkerBootstrapError:
    pass
print(tracemalloc.get_traced_memory()[1])
"""
            result = subprocess.run(
                [sys.executable, "-c", probe, str(wheel), digest],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(int(result.stdout.strip()), 8 * 1024 * 1024)

    def test_non_wheel_and_corrupt_record_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "not-a-wheel.whl"
            invalid.write_bytes(b"not a ZIP archive")
            with self.assertRaises(WorkerBootstrapError) as raised:
                verify_bootstrap_wheel(
                    invalid, hashlib.sha256(invalid.read_bytes()).hexdigest()
                )
            self.assertEqual(raised.exception.code, "worker.bootstrap_wheel_invalid")

            corrupt = root / "corrupt.whl"
            corrupt_digest = _wheel(corrupt, corrupt_record=True)
            with self.assertRaises(WorkerBootstrapError) as raised:
                verify_bootstrap_wheel(corrupt, corrupt_digest)
            self.assertEqual(raised.exception.code, "worker.bootstrap_record_invalid")

    def test_wheel_archive_member_order_is_not_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "literate_ai.whl"
            digest = _wheel(wheel, reverse_members=True)
            self.assertEqual(
                verify_bootstrap_wheel(wheel, digest),
                ("literate-ai", "0.3.0"),
            )

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

    def test_explicit_replacement_activates_only_verified_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "literate_ai.whl"
            digest = _wheel(wheel)
            install_root = root / "environment"
            install_root.mkdir()
            (install_root / "old").write_text("old", encoding="utf-8")
            expected = "sha256:" + "1" * 64

            def create(environment: Path) -> None:
                (environment / "bin").mkdir(parents=True)
                (environment / "bin/python").write_bytes(b"python")

            with (
                patch(
                    "literate_ai.remote_worker_bootstrap.venv.EnvBuilder.create",
                    side_effect=create,
                ),
                patch("literate_ai.remote_worker_bootstrap._run_with_tree_kill") as run,
                patch(
                    "literate_ai.remote_worker_bootstrap.installed_distribution_identity",
                    side_effect=("sha256:" + "2" * 64, expected),
                ),
            ):
                launcher = bootstrap_worker_distribution(
                    wheel,
                    expected_wheel_sha256=digest,
                    expected_distribution_identity=expected,
                    install_root=install_root,
                    replace_existing=True,
                )
            expected_launcher = (
                install_root / "Scripts" / "litai.cmd"
                if launcher.name == "litai.cmd"
                else install_root / "bin" / "litai"
            )
            self.assertEqual(launcher, expected_launcher)
            self.assertFalse((install_root / "old").exists())
            self.assertTrue(expected_launcher.is_file())
            self.assertNotIn(
                ".bootstrap-",
                expected_launcher.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                tuple(root.glob(".environment.replaced-*")),
                (),
                "replaced environments must not remain after activation",
            )
            install_environment = run.call_args.kwargs["env"]
            self.assertEqual(install_environment["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertNotIn("PYTHONPYCACHEPREFIX", install_environment)
            self.assertFalse(
                any(name.startswith("PIP_") for name in install_environment)
            )

    def test_distribution_mismatch_removes_isolated_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "literate_ai.whl"
            digest = _wheel(wheel)
            install_root = root / "environment"

            def create(environment: Path) -> None:
                (environment / "bin").mkdir(parents=True)
                (environment / "bin/python").write_bytes(b"python")

            with (
                patch(
                    "literate_ai.remote_worker_bootstrap.venv.EnvBuilder.create",
                    side_effect=create,
                ),
                patch("literate_ai.remote_worker_bootstrap._run_with_tree_kill"),
                patch(
                    "literate_ai.remote_worker_bootstrap.installed_distribution_identity",
                    return_value="sha256:" + "2" * 64,
                ),
                self.assertRaises(WorkerBootstrapError) as raised,
            ):
                bootstrap_worker_distribution(
                    wheel,
                    expected_wheel_sha256=digest,
                    expected_distribution_identity="sha256:" + "1" * 64,
                    install_root=install_root,
                )
            self.assertEqual(
                raised.exception.code, "worker.bootstrap_distribution_mismatch"
            )
            self.assertFalse(install_root.exists())


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
