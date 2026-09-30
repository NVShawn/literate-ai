"""Exact Node.js discovery and authorization-gated JavaScript builds."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders import javascript as javascript_builder_module
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.builders.javascript import (
    DEFAULT_NODE_MINIMUM_VERSION,
    GuardedJavaScriptBuilder,
    GuardedJavaScriptRoleBuilder,
    NodeToolchain,
    NpmToolchain,
    controlled_node_environment,
    discover_node_toolchain,
    discover_npm_toolchain,
)
from literate_ai.adapters.builders.python import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    BuildError,
    canonical_tree_digest,
    executable_file_digest,
)
from literate_ai.adapters.dependencies import (
    DependencyObservationError,
    NpmDistributionAuthority,
)
from literate_ai.contracts import SemanticVersion
from literate_ai.contracts.flavors import ToolchainConstraint
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
    SecurityProfile,
)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 3, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]


def _package_npm_constraint() -> ToolchainConstraint:
    path = REPO / "flavors" / "package-npm" / "toolchain.json"
    return ToolchainConstraint.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _npm_constraint_fields() -> dict[str, str]:
    constraint = _package_npm_constraint()
    return {
        "version_constraint": constraint.version_range(),
        "constraint_identity": constraint.identity.uri,
    }


def _write_standalone_main(source_root: Path, content: str) -> Path:
    target = source_root / "source" / "main.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _install_node_alias(directory: Path, name: str, target: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    alias = directory / f"{name}{suffix}"
    try:
        alias.symlink_to(target)
    except OSError:
        shutil.copy2(target, alias)
        alias.chmod(alias.stat().st_mode | 0o111)
    return alias


def _node_probe_result(
    toolchain: NodeToolchain, *, version: str | None = None
) -> BoundedProcessResult:
    selected_version = toolchain.version if version is None else version
    payload = {
        "exec_path": toolchain.runtime_executable,
        "version": selected_version,
        "versions_node": selected_version.removeprefix("v"),
    }
    return BoundedProcessResult(
        0,
        (
            javascript_builder_module._NODE_PROBE_PREFIX
            + json.dumps(payload, sort_keys=True, separators=(",", ":"))
        ).encode("utf-8"),
        b"",
    )


def _write_fake_npm_distribution(
    root: Path, *, version: str
) -> tuple[Path, NpmDistributionAuthority]:
    package = root.resolve(strict=True) / "npm"
    cli = package / "bin" / "npm-cli.js"
    implementation = package / "lib" / "cli.js"
    dependency = package / "node_modules" / "fixture-dependency"
    cli.parent.mkdir(parents=True)
    implementation.parent.mkdir(parents=True)
    dependency.mkdir(parents=True)
    cli.write_text("require('../lib/cli.js')\n", encoding="utf-8")
    implementation.write_text("module.exports = 'v1'\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "npm",
                "version": version,
                "bin": {"npm": "bin/npm-cli.js"},
                "dependencies": {"fixture-dependency": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (dependency / "package.json").write_text(
        json.dumps({"name": "fixture-dependency", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (dependency / "index.js").write_text(
        "module.exports = 'dependency'\n", encoding="utf-8"
    )
    return cli, javascript_builder_module.observe_npm_distribution(cli)


def _write_fake_npm_with_external_dependency(
    root: Path, *, version: str
) -> tuple[Path, Path, NpmDistributionAuthority]:
    package = root.resolve(strict=True) / "npm"
    dependency = root.resolve(strict=True) / "external"
    cli = package / "bin" / "npm-cli.js"
    (package / "lib").mkdir(parents=True)
    dependency.mkdir()
    cli.parent.mkdir()
    cli.write_text("require('../lib/cli.js')\n", encoding="utf-8")
    (package / "lib" / "cli.js").write_text("module.exports = 'v1'\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "npm",
                "version": version,
                "bin": {"npm": "bin/npm-cli.js"},
                "dependencies": {"fixture-dependency": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (dependency / "package.json").write_text(
        json.dumps({"name": "fixture-dependency", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (dependency / "index.js").write_text(
        "module.exports = 'external-v1'\n", encoding="utf-8"
    )
    distribution = javascript_builder_module.observe_npm_distribution(
        cli,
        dependency_resolver=lambda _package, _name: dependency,
    )
    return cli, dependency, distribution


def _npm_resolution_probe_result(
    package_root: Path, dependency_root: Path
) -> BoundedProcessResult:
    evidence = {
        "builtin_modules": ["fs", "module", "path"],
        "records": [
            {
                "package_root": str(package_root.resolve(strict=True)),
                "dependency": "fixture-dependency",
                "resolved_root": str(dependency_root.resolve(strict=True)),
            }
        ],
    }
    return BoundedProcessResult(
        0,
        (
            javascript_builder_module._NPM_RESOLUTION_PROBE_PREFIX
            + json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        ).encode("utf-8"),
        b"",
    )


class GuardedJavaScriptBuilderTests(unittest.TestCase):
    @staticmethod
    def toolchain() -> NodeToolchain:
        try:
            return discover_node_toolchain()
        except BuildError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    @staticmethod
    def request_and_authorization(
        source: Path,
        toolchain: NodeToolchain,
        *,
        builder_id: str = GuardedJavaScriptBuilder.builder_id,
    ):
        source_digest = canonical_tree_digest(source)
        policy = SecurityPolicy(policy_digest=DIGEST_C)
        classification = policy.classify(
            effective_revision_digest=DIGEST_B,
            attestations=(
                OriginAttestation(
                    source_digest=source_digest,
                    signer="test",
                    trust_root="test",
                    signature_identity="test",
                    verified=True,
                ),
            ),
            findings=(),
        )
        request = BuildRequest(
            effective_revision_digest=DIGEST_B,
            source_bundle_digest=source_digest,
            builder_id=builder_id,
            toolchain_digest=toolchain.identity,
            sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
            requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
            allowed_outputs=("javascript-checked-bundle",),
        )
        authorization = policy.authorize_build(
            classification,
            request,
            actor="test",
            reason="JavaScript conformance build",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        return request, authorization

    def test_current_node_meets_default_minimum_and_revalidates(self) -> None:
        selected = self.toolchain()
        parsed = SemanticVersion.parse(selected.version.removeprefix("v"))

        self.assertFalse(parsed < SemanticVersion(*DEFAULT_NODE_MINIMUM_VERSION))
        self.assertTrue(Path(selected.command[0]).is_absolute())
        self.assertEqual(
            Path(selected.command[0]).resolve(strict=True),
            Path(selected.launcher_executable),
        )
        self.assertEqual(
            selected.launcher_digest,
            executable_file_digest(Path(selected.launcher_executable)),
        )
        self.assertEqual(
            selected.runtime_digest,
            executable_file_digest(Path(selected.runtime_executable)),
        )
        selected.require_unchanged()

    def test_npm_discovery_binds_the_selected_node_and_resolved_cli(self) -> None:
        node = self.toolchain()
        try:
            npm = discover_npm_toolchain(node, **_npm_constraint_fields())
        except BuildError as exc:
            raise unittest.SkipTest(str(exc)) from exc

        self.assertIs(npm.node, node)
        self.assertEqual(npm.command[0], node.runtime_executable)
        self.assertEqual(Path(npm.cli_path).name, "npm-cli.js")
        self.assertEqual(npm.cli_digest, executable_file_digest(Path(npm.cli_path)))
        npm.require_unchanged()

    def test_npm_toolchain_rejects_a_version_outside_the_host_sbom_range(
        self,
    ) -> None:
        node = self.toolchain()
        with tempfile.TemporaryDirectory() as temporary:
            cli, distribution = _write_fake_npm_distribution(
                Path(temporary), version="13.0.0"
            )
            with self.assertRaisesRegex(ValueError, ">=9,<13"):
                NpmToolchain(
                    node=node,
                    cli_path=str(cli.resolve(strict=True)),
                    cli_digest=executable_file_digest(cli),
                    package_root=distribution.package_root,
                    package_roots=distribution.package_roots,
                    distribution_identity=distribution.identity.uri,
                    version="13.0.0",
                    **_npm_constraint_fields(),
                )

    def test_package_npm_flavor_range_matches_host_sbom(self) -> None:
        constraint = _package_npm_constraint()
        sbom = json.loads(
            (REPO / "flavors" / "package-npm" / "host-toolchain.cdx.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(constraint.version_range(), sbom["components"][0]["version"])

    def test_npm_toolchain_identity_binds_the_flavor_constraint(self) -> None:
        node = self.toolchain()
        fields = _npm_constraint_fields()
        with tempfile.TemporaryDirectory() as temporary:
            cli, distribution = _write_fake_npm_distribution(
                Path(temporary), version="9.0.0"
            )
            npm = NpmToolchain(
                node=node,
                cli_path=str(cli.resolve(strict=True)),
                cli_digest=executable_file_digest(cli),
                package_root=distribution.package_root,
                package_roots=distribution.package_roots,
                distribution_identity=distribution.identity.uri,
                version="9.0.0",
                **fields,
            )
            other = NpmToolchain(
                node=node,
                cli_path=str(cli.resolve(strict=True)),
                cli_digest=executable_file_digest(cli),
                package_root=distribution.package_root,
                package_roots=distribution.package_roots,
                distribution_identity=distribution.identity.uri,
                version="9.0.0",
                version_constraint=fields["version_constraint"],
                constraint_identity="sha256:" + "b" * 64,
            )
            self.assertEqual(npm.version_constraint, ">=9,<13")
            self.assertEqual(npm.constraint_identity, fields["constraint_identity"])
            self.assertNotEqual(npm.identity, other.identity)

    def test_npm_discovery_requires_flavor_constraint_authority(self) -> None:
        node = self.toolchain()
        with self.assertRaises(TypeError):
            discover_npm_toolchain(node)

    def test_npm_toolchain_rejects_non_loader_distribution_mutation(self) -> None:
        node = self.toolchain()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cli, distribution = _write_fake_npm_distribution(root, version="10.0.0")
            loader_digest = executable_file_digest(cli)
            npm = NpmToolchain(
                node=node,
                cli_path=str(cli.resolve(strict=True)),
                cli_digest=loader_digest,
                package_root=distribution.package_root,
                package_roots=distribution.package_roots,
                distribution_identity=distribution.identity.uri,
                version="10.0.0",
                **_npm_constraint_fields(),
            )
            self.assertIn(
                "node_modules/fixture-dependency/index.js",
                {record[0] for record in distribution.files},
            )
            (root / "npm/lib/cli.js").write_text(
                "module.exports = 'mutated'\n", encoding="utf-8"
            )

            with (
                patch.object(NodeToolchain, "require_unchanged"),
                patch.object(
                    javascript_builder_module,
                    "_probe_npm_version",
                    return_value="10.0.0",
                ) as version_probe,
                self.assertRaises(BuildError) as raised,
            ):
                npm.require_unchanged({})

            self.assertEqual(raised.exception.code, "builder.npm_toolchain_changed")
            self.assertEqual(executable_file_digest(cli), loader_digest)
            version_probe.assert_not_called()

    def test_npm_distribution_rejects_native_and_wasm_payloads(self) -> None:
        for name, content in (
            ("native.node", b"not-even-a-valid-addon"),
            ("opaque.bin", b"\x00asm\x01\x00\x00\x00"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                cli, _distribution = _write_fake_npm_distribution(
                    root, version="10.0.0"
                )
                (root / "npm" / name).write_bytes(content)

                with self.assertRaises(DependencyObservationError) as raised:
                    javascript_builder_module.observe_npm_distribution(cli)

                self.assertEqual(
                    raised.exception.code,
                    "dependencies.npm-native-payload-unsupported",
                )

    def test_npm_toolchain_rejects_external_resolved_dependency_mutation(
        self,
    ) -> None:
        node = self.toolchain()
        with tempfile.TemporaryDirectory() as temporary:
            cli, dependency, distribution = _write_fake_npm_with_external_dependency(
                Path(temporary), version="10.0.0"
            )
            loader_digest = executable_file_digest(cli)
            npm = NpmToolchain(
                node=node,
                cli_path=str(cli.resolve(strict=True)),
                cli_digest=loader_digest,
                package_root=distribution.package_root,
                package_roots=distribution.package_roots,
                distribution_identity=distribution.identity.uri,
                version="10.0.0",
                **_npm_constraint_fields(),
            )
            self.assertNotIn(
                dependency.name,
                {Path(record[0]).parts[0] for record in distribution.files},
            )
            (dependency / "index.js").write_text(
                "module.exports = 'external-mutated'\n", encoding="utf-8"
            )

            with (
                patch.object(NodeToolchain, "require_unchanged"),
                patch.object(
                    javascript_builder_module,
                    "_run_bounded_process",
                    return_value=_npm_resolution_probe_result(
                        Path(distribution.package_root), dependency
                    ),
                ) as resolution_probe,
                patch.object(
                    javascript_builder_module,
                    "_probe_npm_version",
                    return_value="10.0.0",
                ) as version_probe,
                self.assertRaises(BuildError) as raised,
            ):
                npm.require_unchanged({})

            self.assertEqual(raised.exception.code, "builder.npm_toolchain_changed")
            self.assertEqual(executable_file_digest(cli), loader_digest)
            resolution_probe.assert_called_once()
            version_probe.assert_not_called()

    def test_npm_runtime_guard_rejects_undeclared_literal_package_load(self) -> None:
        node = self.toolchain()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            package = root / "npm"
            external = root / "node_modules" / "external"
            cli = package / "bin" / "npm-cli.js"
            cli.parent.mkdir(parents=True)
            external.mkdir(parents=True)
            cli.write_text("require('external')\n", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "npm",
                        "version": "10.0.0",
                        "bin": {"npm": "bin/npm-cli.js"},
                    }
                ),
                encoding="utf-8",
            )
            (external / "package.json").write_text(
                json.dumps({"name": "external", "version": "1.0.0"}),
                encoding="utf-8",
            )
            (external / "index.js").write_text(
                "module.exports = true\n", encoding="utf-8"
            )

            distribution = javascript_builder_module.observe_npm_distribution(cli)
            npm = NpmToolchain(
                node=node,
                cli_path=str(cli.resolve(strict=True)),
                cli_digest=executable_file_digest(cli),
                package_root=distribution.package_root,
                package_roots=distribution.package_roots,
                distribution_identity=distribution.identity.uri,
                version="10.0.0",
                **_npm_constraint_fields(),
            )
            completed = subprocess.run(
                npm.command,
                cwd=root,
                env=controlled_node_environment(dict(os.environ)),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(b"npm module resolution escaped authority", completed.stderr)

    def test_npm_runtime_guard_rejects_in_tree_orphan_package_load(self) -> None:
        node = self.toolchain()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            package = root / "npm"
            orphan = package / "node_modules" / "orphan"
            cli = package / "bin" / "npm-cli.js"
            cli.parent.mkdir(parents=True)
            orphan.mkdir(parents=True)
            cli.write_text("require('orphan')\n", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "npm",
                        "version": "10.0.0",
                        "bin": {"npm": "bin/npm-cli.js"},
                    }
                ),
                encoding="utf-8",
            )
            (orphan / "package.json").write_text(
                json.dumps({"name": "orphan", "version": "1.0.0"}),
                encoding="utf-8",
            )
            (orphan / "index.js").write_text(
                "module.exports = true\n", encoding="utf-8"
            )

            distribution = javascript_builder_module.observe_npm_distribution(cli)
            self.assertNotIn(
                str(orphan.resolve(strict=True)), distribution.package_roots
            )
            npm = NpmToolchain(
                node=node,
                cli_path=str(cli.resolve(strict=True)),
                cli_digest=executable_file_digest(cli),
                package_root=distribution.package_root,
                package_roots=distribution.package_roots,
                distribution_identity=distribution.identity.uri,
                version="10.0.0",
                **_npm_constraint_fields(),
            )
            completed = subprocess.run(
                npm.command,
                cwd=root,
                env=controlled_node_environment(dict(os.environ)),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(b"npm module resolution escaped authority", completed.stderr)

    def test_npm_distribution_rejects_dev_only_dynamic_import_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            package = root / "npm"
            external = root / "external"
            cli = package / "bin" / "npm-cli.js"
            cli.parent.mkdir(parents=True)
            external.mkdir()
            cli.write_text("module.exports = true\n", encoding="utf-8")
            (package / "dev.config.mjs").write_text(
                "export default import('external')\n", encoding="utf-8"
            )
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "npm",
                        "version": "10.0.0",
                        "bin": {"npm": "bin/npm-cli.js"},
                        "devDependencies": {"external": "1.0.0"},
                    }
                ),
                encoding="utf-8",
            )
            (external / "package.json").write_text(
                json.dumps({"name": "external", "version": "1.0.0"}),
                encoding="utf-8",
            )
            (external / "index.js").write_text(
                "module.exports = true\n", encoding="utf-8"
            )

            with self.assertRaises(DependencyObservationError) as raised:
                javascript_builder_module.observe_npm_distribution(
                    cli,
                    dependency_resolver=lambda _package, _name: external,
                )

            self.assertEqual(
                raised.exception.code,
                "dependencies.npm-literal-import-unbound",
            )

    def test_npm_distribution_rejects_esm_package_subpath_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            package = root / "npm"
            dependency = package / "node_modules" / "escape"
            cli = package / "bin" / "npm-cli.js"
            cli.parent.mkdir(parents=True)
            dependency.mkdir(parents=True)
            cli.write_text(
                "import('escape/subpath').then(() => console.log('escaped'))\n",
                encoding="utf-8",
            )
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "npm",
                        "version": "10.0.0",
                        "bin": {"npm": "bin/npm-cli.js"},
                        "dependencies": {"escape": "1.0.0"},
                    }
                ),
                encoding="utf-8",
            )
            (dependency / "package.json").write_text(
                json.dumps(
                    {
                        "name": "escape",
                        "version": "1.0.0",
                        "exports": {
                            ".": "./index.js",
                            "./subpath": "./subpath.mjs",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (dependency / "index.js").write_text(
                "module.exports = true\n", encoding="utf-8"
            )
            (dependency / "subpath.mjs").write_text(
                "import '../../../outside.mjs'\nexport default true\n",
                encoding="utf-8",
            )
            (root / "outside.mjs").write_text(
                "console.log('outside executed')\n", encoding="utf-8"
            )

            with self.assertRaises(DependencyObservationError) as raised:
                javascript_builder_module.observe_npm_distribution(cli)

            self.assertEqual(
                raised.exception.code,
                "dependencies.npm-esm-subpath-unsupported",
            )

    def test_npm_distribution_rejects_esm_main_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            package = root / "npm"
            dependency = package / "node_modules" / "escape"
            cli = package / "bin" / "npm-cli.js"
            cli.parent.mkdir(parents=True)
            dependency.mkdir(parents=True)
            cli.write_text("import('escape')\n", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "npm",
                        "version": "10.0.0",
                        "bin": {"npm": "bin/npm-cli.js"},
                        "dependencies": {"escape": "1.0.0"},
                    }
                ),
                encoding="utf-8",
            )
            (dependency / "package.json").write_text(
                json.dumps(
                    {
                        "name": "escape",
                        "version": "1.0.0",
                        "type": "module",
                        "main": "../../../outside.mjs",
                    }
                ),
                encoding="utf-8",
            )
            (root / "outside.mjs").write_text(
                "console.log('outside executed')\n", encoding="utf-8"
            )

            with self.assertRaises(DependencyObservationError) as raised:
                javascript_builder_module.observe_npm_distribution(cli)

            self.assertEqual(
                raised.exception.code,
                "dependencies.npm-entrypoint-path-unsafe",
            )

    def test_npm_distribution_rejects_external_package_import_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            package = root / "npm"
            dependency = package / "node_modules" / "escape"
            cli = package / "bin" / "npm-cli.js"
            cli.parent.mkdir(parents=True)
            dependency.mkdir(parents=True)
            cli.write_text("import('#escape')\n", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "npm",
                        "version": "10.0.0",
                        "bin": {"npm": "bin/npm-cli.js"},
                        "dependencies": {"escape": "1.0.0"},
                        "imports": {"#escape": "escape/subpath"},
                    }
                ),
                encoding="utf-8",
            )
            (dependency / "package.json").write_text(
                json.dumps(
                    {
                        "name": "escape",
                        "version": "1.0.0",
                        "exports": {"./subpath": "./subpath.mjs"},
                    }
                ),
                encoding="utf-8",
            )
            (dependency / "subpath.mjs").write_text(
                "import '../../../outside.mjs'\n", encoding="utf-8"
            )
            (root / "outside.mjs").write_text(
                "console.log('outside executed')\n", encoding="utf-8"
            )

            with self.assertRaises(DependencyObservationError) as raised:
                javascript_builder_module.observe_npm_distribution(cli)

            self.assertEqual(
                raised.exception.code,
                "dependencies.npm-esm-subpath-unsupported",
            )

    def test_explicit_node_19_pin_fails_closed_without_path_fallback(self) -> None:
        baseline = self.toolchain()
        environment = dict(os.environ)
        environment["NODE"] = (
            subprocess.list2cmdline(baseline.command)
            if os.name == "nt"
            else shlex.join(baseline.command)
        )
        with patch.object(
            javascript_builder_module,
            "_run_bounded_process",
            return_value=_node_probe_result(baseline, version="v19.99.0"),
        ) as probe:
            with self.assertRaises(BuildError) as error:
                discover_node_toolchain(environment)

        self.assertEqual(error.exception.code, "builder.node_version_unsupported")
        self.assertEqual(probe.call_count, 1)

    def test_unpinned_old_node_falls_back_in_ordered_path(self) -> None:
        baseline = self.toolchain()
        launcher = Path(baseline.launcher_executable)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            early = root / "early"
            late = root / "late"
            early.mkdir()
            late.mkdir()
            old_node = _install_node_alias(early, "node", launcher)
            selected_node = _install_node_alias(late, "node", launcher)

            def report_versions(command, **kwargs):
                del kwargs
                version = (
                    "v19.99.0" if Path(command[0]) == old_node else baseline.version
                )
                return _node_probe_result(baseline, version=version)

            with patch.object(
                javascript_builder_module,
                "_run_bounded_process",
                side_effect=report_versions,
            ) as probe:
                selected = discover_node_toolchain(
                    {"PATH": os.pathsep.join((str(early), str(late)))}
                )

            self.assertEqual(Path(selected.command[0]), selected_node)
            self.assertEqual(
                Path(selected.launcher_executable),
                selected_node.resolve(strict=True),
            )
            self.assertEqual(probe.call_count, 2)

    def test_malformed_node_semver_is_rejected(self) -> None:
        baseline = self.toolchain()
        malformed_outputs = (
            b"v20.1\n",
            b"v020.0.0\n",
            b"v20.0.0\nextra\n",
            b"20.0.0\n",
            b"v20.0.0\xff\n",
        )
        for output in malformed_outputs:
            with self.subTest(output=output):
                with patch.object(
                    javascript_builder_module,
                    "_run_bounded_process",
                    return_value=BoundedProcessResult(0, output, b""),
                ):
                    with self.assertRaises(BuildError) as error:
                        discover_node_toolchain(
                            dict(os.environ), pinned_command=baseline.command
                        )

                self.assertEqual(error.exception.code, "builder.node_version_failed")

    def test_prerelease_below_default_minimum_is_rejected(self) -> None:
        baseline = self.toolchain()
        with patch.object(
            javascript_builder_module,
            "_run_bounded_process",
            return_value=_node_probe_result(baseline, version="v20.0.0-rc.1"),
        ):
            with self.assertRaises(BuildError) as error:
                discover_node_toolchain(
                    dict(os.environ), pinned_command=baseline.command
                )

        self.assertEqual(error.exception.code, "builder.node_version_unsupported")

    def test_pinned_command_and_required_version_prefix_are_enforced(self) -> None:
        baseline = self.toolchain()
        parsed = SemanticVersion.parse(baseline.version.removeprefix("v"))

        selected = discover_node_toolchain(
            dict(os.environ),
            pinned_command=baseline.command,
            required_version=(parsed.major, parsed.minor),
        )

        self.assertEqual(selected.command, baseline.command)
        self.assertEqual(selected.version, baseline.version)

        with self.assertRaises(BuildError) as error:
            discover_node_toolchain(
                dict(os.environ),
                pinned_command=baseline.command,
                required_version=(parsed.major + 1,),
            )

        self.assertEqual(error.exception.code, "builder.node_version_unsupported")

    @unittest.skipUnless(os.name == "posix", "symlink retarget test is POSIX-specific")
    def test_symlink_invocation_is_retained_and_retargeting_is_rejected(self) -> None:
        baseline = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alias = root / "node"
            alias.symlink_to(Path(baseline.launcher_executable))
            selected = discover_node_toolchain({"PATH": str(root)})

            self.assertEqual(Path(selected.command[0]), alias)
            self.assertEqual(
                Path(selected.launcher_executable),
                Path(baseline.launcher_executable),
            )

            alias.unlink()
            alias.symlink_to(Path("/bin/false"))
            with self.assertRaises(BuildError) as error:
                selected.require_unchanged()

            self.assertEqual(error.exception.code, "builder.node_toolchain_changed")

    @unittest.skipUnless(os.name == "posix", "wrapper retarget test is POSIX-specific")
    def test_stable_wrapper_retargeting_its_runtime_is_rejected(self) -> None:
        baseline = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected_runtime = root / "selected-node"
            selected_runtime.symlink_to(Path(baseline.runtime_executable))
            wrapper = root / "node-wrapper"
            wrapper.write_text(
                "#!/bin/sh\nexec " + shlex.quote(str(selected_runtime)) + ' "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            selected = discover_node_toolchain(
                dict(os.environ), pinned_command=(str(wrapper),)
            )
            wrapper_digest = executable_file_digest(wrapper)

            self.assertEqual(selected.launcher_digest, wrapper_digest)
            self.assertEqual(
                Path(selected.runtime_executable),
                Path(baseline.runtime_executable),
            )
            selected_runtime.unlink()
            selected_runtime.symlink_to(Path("/bin/false"))

            with self.assertRaises(BuildError) as error:
                selected.require_unchanged()

            self.assertEqual(error.exception.code, "builder.node_toolchain_changed")
            self.assertEqual(executable_file_digest(wrapper), wrapper_digest)

    def test_file_loading_or_unknown_node_command_arguments_fail_closed(self) -> None:
        baseline = self.toolchain()
        unsafe_arguments = (
            ("--require", "mutable-hook.js"),
            ("--import=./mutable-hook.mjs",),
            ("--experimental-loader", "mutable-loader.mjs"),
            ("--eval", "process.exit(0)"),
            ("application.js",),
        )
        for arguments in unsafe_arguments:
            with self.subTest(arguments=arguments):
                with patch.object(
                    javascript_builder_module, "_run_bounded_process"
                ) as probe:
                    with self.assertRaises(BuildError) as error:
                        discover_node_toolchain(
                            dict(os.environ),
                            pinned_command=(baseline.command[0], *arguments),
                        )

                self.assertEqual(
                    error.exception.code,
                    "builder.node_command_arguments_unsafe",
                )
                probe.assert_not_called()

    def test_discovery_pins_configured_arguments_path_bytes_and_version(self) -> None:
        baseline = self.toolchain()
        environment = dict(os.environ)
        configured = [baseline.command[0], "--no-warnings"]
        environment["NODE"] = (
            subprocess.list2cmdline(configured)
            if os.name == "nt"
            else shlex.join(configured)
        )

        selected = discover_node_toolchain(environment)

        self.assertEqual(selected.command, tuple(configured))
        self.assertEqual(
            selected.executable_digest,
            executable_file_digest(Path(selected.command[0])),
        )
        self.assertTrue(selected.version.startswith("v"), selected.version)
        self.assertNotEqual(selected.identity, baseline.identity)

    def test_controlled_environment_removes_node_code_injection_hooks(self) -> None:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "NODE_OPTIONS": "--require=ambient.js",
            "node_path": "/ambient/modules",
            "Node_Compile_Cache": "/ambient/cache",
            "node_disable_compile_cache": "0",
            "SYSTEMROOT": "required-on-windows",
        }

        controlled = controlled_node_environment(environment)

        self.assertNotIn("NODE_OPTIONS", controlled)
        self.assertNotIn("node_path", controlled)
        self.assertNotIn("Node_Compile_Cache", controlled)
        self.assertNotIn("node_disable_compile_cache", controlled)
        self.assertEqual(controlled["NODE_DISABLE_COMPILE_CACHE"], "1")
        self.assertEqual(controlled["SYSTEMROOT"], "required-on-windows")

    def test_toolchain_discovery_bounds_version_output(self) -> None:
        baseline = self.toolchain()

        with patch.object(
            javascript_builder_module,
            "_NODE_PROBE_SCRIPT",
            "process.stdout.write('x'.repeat(4096))",
        ):
            with self.assertRaises(BuildError) as error:
                discover_node_toolchain(
                    dict(os.environ),
                    pinned_command=baseline.command,
                    stdout_limit_bytes=1024,
                    stderr_limit_bytes=1024,
                )

        self.assertEqual(error.exception.code, "builder.node_version_output_limit")

    def test_valid_authorization_checks_seals_and_runs_bundle(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            _write_standalone_main(
                source,
                "const values = JSON.parse(process.argv[2]);\n"
                "const result = values.reduce((sum, value) => sum + value, 0);\n"
                "process.stdout.write("
                "JSON.stringify({count: values.length, result}));\n",
            )
            (source / "source" / "helper.js").write_text(
                "'use strict';\nmodule.exports = Object.freeze({portable: true});\n",
                encoding="utf-8",
            )
            request, authorization = self.request_and_authorization(source, toolchain)

            artifact = GuardedJavaScriptBuilder(
                toolchain, AuthorizationRevocationSet()
            ).build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )

            self.assertEqual(
                artifact.checked_files, ("source/helper.js", "source/main.js")
            )
            self.assertEqual(artifact.runtime_command, toolchain.command)
            self.assertEqual(artifact.toolchain_identity, toolchain.identity)
            completed = subprocess.run(
                [
                    *artifact.runtime_command,
                    str(artifact.artifact_path / artifact.entrypoint_file),
                    "[7,11,13]",
                ],
                cwd=artifact.artifact_path,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
                env=controlled_node_environment(dict(os.environ)),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertEqual(json.loads(completed.stdout), {"count": 3, "result": 31})
            self.assertFalse((artifact.artifact_path / "not-generated.txt").exists())

    def test_role_builder_binds_a_distinct_role_scoped_request(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "frontend"
            frontend.mkdir()
            (frontend / "main.js").write_text("'use strict';\n", encoding="utf-8")
            request, authorization = self.request_and_authorization(
                frontend,
                toolchain,
                builder_id=GuardedJavaScriptRoleBuilder.builder_id,
            )

            artifact = GuardedJavaScriptRoleBuilder(
                toolchain, AuthorizationRevocationSet()
            ).build(
                request,
                authorization,
                source_root=frontend,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )

            self.assertEqual(artifact.entrypoint_file, "main.js")
            self.assertEqual(artifact.checked_files, ("main.js",))
            self.assertTrue((artifact.artifact_path / "main.js").is_file())

    def test_syntax_error_fails_without_publishing_an_artifact(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            _write_standalone_main(source, "const broken = ;\n")
            request, authorization = self.request_and_authorization(source, toolchain)

            with self.assertRaises(BuildError) as error:
                GuardedJavaScriptBuilder(toolchain, AuthorizationRevocationSet()).build(
                    request,
                    authorization,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )

            self.assertEqual(error.exception.code, "builder.javascript_check_failed")
            self.assertEqual(list((root / "artifacts").iterdir()), [])

    def test_builder_requires_explicit_unsandboxed_yolo_grant(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            _write_standalone_main(source, "'use strict';\n")
            request, authorization = self.request_and_authorization(source, toolchain)
            falsely_constrained = replace(
                authorization,
                profile=SecurityProfile.CONSTRAINED,
                warning=None,
            )

            with patch.object(
                javascript_builder_module, "_run_bounded_process"
            ) as node_call:
                with self.assertRaises(BuildError) as error:
                    GuardedJavaScriptBuilder(
                        toolchain, AuthorizationRevocationSet()
                    ).build(
                        request,
                        falsely_constrained,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(
                error.exception.code, "builder.yolo_authorization_required"
            )
            node_call.assert_not_called()

    def test_request_must_bind_builder_owned_exact_node_toolchain(self) -> None:
        toolchain = self.toolchain()
        selected_other_toolchain = replace(
            toolchain, version=toolchain.version + "-other"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            _write_standalone_main(source, "'use strict';\n")
            request, authorization = self.request_and_authorization(
                source, selected_other_toolchain
            )

            with patch.object(
                javascript_builder_module, "_run_bounded_process"
            ) as node_call:
                with self.assertRaises(BuildError) as error:
                    GuardedJavaScriptBuilder(
                        toolchain, AuthorizationRevocationSet()
                    ).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(error.exception.code, "builder.toolchain_mismatch")
            node_call.assert_not_called()

    def test_node_executable_is_checked_after_syntax_check(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            _write_standalone_main(source, "'use strict';\n")
            request, authorization = self.request_and_authorization(source, toolchain)

            with patch.object(
                javascript_builder_module,
                "executable_file_digest",
                side_effect=(
                    toolchain.executable_digest,
                    toolchain.executable_digest,
                    DIGEST_A,
                ),
            ):
                with self.assertRaises(BuildError) as error:
                    GuardedJavaScriptBuilder(
                        toolchain, AuthorizationRevocationSet()
                    ).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(error.exception.code, "builder.node_toolchain_changed")

    def test_source_drift_during_check_is_rejected(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = _write_standalone_main(source, "'use strict';\n")
            request, authorization = self.request_and_authorization(source, toolchain)
            real_run = javascript_builder_module._run_bounded_process

            def check_then_mutate(*args, **kwargs):
                result = real_run(*args, **kwargs)
                if "--check" in args[0]:
                    target.write_text(
                        "'use strict';\nconst changed = true;\n", encoding="utf-8"
                    )
                return result

            with patch.object(
                javascript_builder_module,
                "_run_bounded_process",
                side_effect=check_then_mutate,
            ):
                with self.assertRaises(BuildError) as error:
                    GuardedJavaScriptBuilder(
                        toolchain, AuthorizationRevocationSet()
                    ).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(
                error.exception.code, "builder.source_changed_during_build"
            )

    def test_check_output_is_bounded_and_process_is_terminated(self) -> None:
        toolchain = self.toolchain()
        script = (
            "import os\n"
            "chunk = b'x' * 4096\n"
            "while True:\n"
            "    os.write(1, chunk)\n"
            "    os.write(2, chunk)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            _write_standalone_main(source, "'use strict';\n")
            request, authorization = self.request_and_authorization(source, toolchain)

            real_run = javascript_builder_module._run_bounded_process

            def replace_check(command, **kwargs):
                if "--check" in command:
                    return real_run([sys.executable, "-c", script], **kwargs)
                return real_run(command, **kwargs)

            with patch.object(
                javascript_builder_module,
                "_run_bounded_process",
                side_effect=replace_check,
            ):
                with self.assertRaises(BuildError) as error:
                    GuardedJavaScriptBuilder(
                        toolchain,
                        AuthorizationRevocationSet(),
                        stdout_limit_bytes=1024,
                        stderr_limit_bytes=1024,
                    ).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(
                error.exception.code, "builder.javascript_check_output_limit"
            )

    def test_cached_bundle_is_verified_before_reuse(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            _write_standalone_main(source, "'use strict';\n")
            request, authorization = self.request_and_authorization(source, toolchain)
            builder = GuardedJavaScriptBuilder(toolchain, AuthorizationRevocationSet())
            artifact = builder.build(
                request,
                authorization,
                source_root=source,
                artifact_store=root / "artifacts",
                now=NOW + timedelta(minutes=1),
            )
            (artifact.artifact_path / "source" / "main.js").write_text(
                "throw new Error('tampered');\n", encoding="utf-8"
            )

            with self.assertRaises(BuildError) as error:
                builder.build(
                    request,
                    authorization,
                    source_root=source,
                    artifact_store=root / "artifacts",
                    now=NOW + timedelta(minutes=1),
                )

            self.assertEqual(error.exception.code, "builder.artifact_collision")

    def test_staged_bundle_rejects_undeclared_output_before_publish(self) -> None:
        toolchain = self.toolchain()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            _write_standalone_main(source, "'use strict';\n")
            request, authorization = self.request_and_authorization(source, toolchain)
            real_match = javascript_builder_module._cached_javascript_artifact_matches

            def inject_undeclared_output(staging, *args, **kwargs):
                (staging / "undeclared.txt").write_text("rogue\n", encoding="utf-8")
                return real_match(staging, *args, **kwargs)

            with patch.object(
                javascript_builder_module,
                "_cached_javascript_artifact_matches",
                side_effect=inject_undeclared_output,
            ):
                with self.assertRaises(BuildError) as error:
                    GuardedJavaScriptBuilder(
                        toolchain, AuthorizationRevocationSet()
                    ).build(
                        request,
                        authorization,
                        source_root=source,
                        artifact_store=root / "artifacts",
                        now=NOW + timedelta(minutes=1),
                    )

            self.assertEqual(error.exception.code, "builder.artifact_invalid")
            self.assertEqual(list((root / "artifacts").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
