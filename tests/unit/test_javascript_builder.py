"""Exact Node.js discovery and authorization-gated JavaScript builds."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.builders import javascript as javascript_builder_module
from literate_ai.adapters.builders.javascript import (
    GuardedJavaScriptBuilder,
    NodeToolchain,
    NpmToolchain,
    controlled_node_environment,
    discover_node_toolchain,
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
)
from literate_ai.contracts.flavors import ToolchainConstraint
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
    SecurityProfile,
)

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

    def test_npm_distribution_rejects_package_escapes(self) -> None:
        escape = {"name": "escape", "version": "1.0.0"}
        subpath = "import '../../../outside.mjs'\nexport default true\n"
        cases = (
            (
                "dev-only-dynamic-import",
                "module.exports = true\n",
                {"devDependencies": {"external": "1.0.0"}},
                None,
                {},
                "dependencies.npm-literal-import-unbound",
            ),
            (
                "esm-package-subpath",
                "import('escape/subpath').then(() => console.log('escaped'))\n",
                {"dependencies": {"escape": "1.0.0"}},
                {
                    **escape,
                    "exports": {".": "./index.js", "./subpath": "./subpath.mjs"},
                },
                {"index.js": "module.exports = true\n", "subpath.mjs": subpath},
                "dependencies.npm-esm-subpath-unsupported",
            ),
            (
                "esm-main",
                "import('escape')\n",
                {"dependencies": {"escape": "1.0.0"}},
                {**escape, "type": "module", "main": "../../../outside.mjs"},
                {},
                "dependencies.npm-entrypoint-path-unsafe",
            ),
            (
                "external-import-alias",
                "import('#escape')\n",
                {
                    "dependencies": {"escape": "1.0.0"},
                    "imports": {"#escape": "escape/subpath"},
                },
                {**escape, "exports": {"./subpath": "./subpath.mjs"}},
                {"subpath.mjs": subpath},
                "dependencies.npm-esm-subpath-unsupported",
            ),
        )
        for name, cli_text, manifest, dependency_manifest, files, code in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve(strict=True)
                package = root / "npm"
                cli = package / "bin" / "npm-cli.js"
                cli.parent.mkdir(parents=True)
                cli.write_text(cli_text, encoding="utf-8")
                (package / "package.json").write_text(
                    json.dumps(
                        {
                            "name": "npm",
                            "version": "10.0.0",
                            "bin": {"npm": "bin/npm-cli.js"},
                            **manifest,
                        }
                    ),
                    encoding="utf-8",
                )
                (root / "outside.mjs").write_text(
                    "console.log('outside executed')\n", encoding="utf-8"
                )
                options = {}
                if dependency_manifest is None:
                    # A dev-only dependency outside the package must stay unbound.
                    external = root / "external"
                    external.mkdir()
                    (external / "package.json").write_text(
                        json.dumps({"name": "external", "version": "1.0.0"}),
                        encoding="utf-8",
                    )
                    (external / "index.js").write_text(
                        "module.exports = true\n", encoding="utf-8"
                    )
                    (package / "dev.config.mjs").write_text(
                        "export default import('external')\n", encoding="utf-8"
                    )
                    options["dependency_resolver"] = lambda _p, _n, root=external: root
                else:
                    dependency = package / "node_modules" / "escape"
                    dependency.mkdir(parents=True)
                    (dependency / "package.json").write_text(
                        json.dumps(dependency_manifest), encoding="utf-8"
                    )
                    for relative, content in files.items():
                        (dependency / relative).write_text(content, encoding="utf-8")
                with self.assertRaises(DependencyObservationError) as raised:
                    javascript_builder_module.observe_npm_distribution(cli, **options)
                self.assertEqual(raised.exception.code, code)

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


if __name__ == "__main__":
    unittest.main()
