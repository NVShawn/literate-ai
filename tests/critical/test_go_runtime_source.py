"""Real Go compiler controls for the generated native runtime-test package."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.adapters.builders.go import (
    GuardedGoBuilder,
    _go_compiled_source_files,
    discover_go_toolchain,
)
from literate_ai.adapters.builders.python import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    BuildError,
    canonical_tree_digest,
)
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildRequest,
    OriginAttestation,
    SecurityPolicy,
)

_MAIN = """package main
import (
    "./tests"
    "encoding/json"
    "fmt"
    "os"
)
func calculate(value int) int { return value + 1 }
func main() {
    if len(os.Args) > 1 && os.Args[1] == "--litai-test" {
        if err := tests.Run(calculate); err != nil {
            fmt.Fprintln(os.Stderr, err)
            os.Exit(1)
        }
        fmt.Println(`{"cases":[{"case_id":"one","outcome":"passed"},{"case_id":"two","outcome":"passed"}]}`)
        return
    }
    if len(os.Args) > 1 && os.Args[1] == "--litai-smoke" {
        fmt.Println(`{"status":"ok"}`)
        return
    }
    var args []int
    if len(os.Args) != 2 ||
        json.Unmarshal([]byte(os.Args[1]), &args) != nil || len(args) != 1 {
        os.Exit(2)
    }
    fmt.Printf("%d\\n", calculate(args[0]))
}
"""

_TESTS = """package tests
import "fmt"
func Run(calculate func(int) int) error {
    if got := calculate(1); got != 2 { return fmt.Errorf("case one: got %d", got) }
    if got := calculate(2); got != 3 { return fmt.Errorf("case two: got %d", got) }
    return nil
}
"""


@unittest.skipUnless(shutil.which("go"), "Go toolchain unavailable")
class GoRuntimeSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "generated"
        (self.source / "source/tests").mkdir(parents=True)
        (self.source / "source/main.go").write_text(_MAIN, encoding="utf-8")
        self.runtime = self.source / "source/tests/litai_runtime.go"
        self.runtime.write_text(_TESTS, encoding="utf-8")
        self.toolchain = discover_go_toolchain()
        self.environment = {
            **os.environ,
            "GO111MODULE": "off",
            "GOFLAGS": "-mod=mod",
            "GOCACHE": str(self.root / "cache"),
            "GOPATH": str(self.root / "gopath"),
            "GOMODCACHE": str(self.root / "module-cache"),
            "GOWORK": "off",
            "GOENV": "off",
            "GOTOOLCHAIN": "local",
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "CGO_ENABLED": "0",
        }

    def _build(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                *self.toolchain.command,
                "build",
                "-o",
                str(self.root / "artifact"),
                "source/main.go",
            ],
            cwd=self.source,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def _inventory(self) -> tuple[str, ...]:
        return _go_compiled_source_files(
            self.toolchain, self.source, self.environment, timeout_seconds=120
        )

    def test_compiled_native_cases_and_app_work_without_source(self) -> None:
        self.assertEqual(
            self._inventory(),
            ("source/main.go", "source/tests/litai_runtime.go"),
        )
        built = self._build()
        self.assertEqual(built.returncode, 0, built.stderr)
        shutil.rmtree(self.source)
        artifact = self.root / "artifact"
        for argument, expected in (
            ("--litai-test", '"outcome":"passed"'),
            ("--litai-smoke", '"status":"ok"'),
            ("[4]", "5\n"),
        ):
            observed = subprocess.run(
                [str(artifact), argument], capture_output=True, text=True, check=False
            )
            self.assertEqual(observed.returncode, 0, observed.stderr)
            self.assertIn(expected, observed.stdout)

    def test_guarded_builder_records_real_compiler_consumption(self) -> None:
        (self.source / "source/tests/manifest.json").write_text(
            '{"cases":[]}', encoding="utf-8"
        )
        source_digest = canonical_tree_digest(self.source)
        policy = SecurityPolicy(policy_digest="sha256:" + "c" * 64)
        classification = policy.classify(
            effective_revision_digest="sha256:" + "b" * 64,
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
            effective_revision_digest="sha256:" + "b" * 64,
            source_bundle_digest=source_digest,
            builder_id=GuardedGoBuilder.builder_id,
            toolchain_digest=self.toolchain.identity,
            sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
            requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
            allowed_outputs=("native-executable",),
        )
        now = datetime(2026, 11, 5, tzinfo=UTC)
        authorization = policy.authorize_build(
            classification,
            request,
            actor="test",
            reason="Go native test binding",
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
            yolo_acknowledged=True,
        )
        artifact = GuardedGoBuilder(self.toolchain, AuthorizationRevocationSet()).build(
            request,
            authorization,
            source_root=self.source,
            artifact_store=self.root / "artifacts",
            now=now + timedelta(minutes=1),
            lifecycle_consumed_files=("source/tests/manifest.json",),
        )
        self.assertEqual(
            artifact.consumed_source_files,
            ("source/main.go", "source/tests/litai_runtime.go"),
        )
        shutil.rmtree(self.source)
        observed = subprocess.run(
            [str(artifact.artifact_path / artifact.executable_file), "--litai-test"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertIn('"outcome":"passed"', observed.stdout)

    def test_old_test_filename_fails_real_compiler(self) -> None:
        self.runtime.rename(self.runtime.with_name("litai_test.go"))
        built = self._build()
        self.assertNotEqual(built.returncode, 0)
        self.assertIn("no non-test Go files", built.stderr)
        with self.assertRaises(BuildError):
            self._inventory()

    def test_missing_or_hidden_runtime_source_refuses(self) -> None:
        self.runtime.unlink()
        with self.assertRaises(BuildError):
            self._inventory()
        self.runtime.write_text("//go:build omitted\n" + _TESTS, encoding="utf-8")
        with self.assertRaises(BuildError):
            self._inventory()

    def test_changed_application_or_assertion_fails_native_cases(self) -> None:
        main = self.source / "source/main.go"
        main.write_text(_MAIN.replace("value + 1", "value + 2"), encoding="utf-8")
        self.assertEqual(self._build().returncode, 0)
        self.assertNotEqual(
            subprocess.run(
                [str(self.root / "artifact"), "--litai-test"], check=False
            ).returncode,
            0,
        )
        main.write_text(_MAIN, encoding="utf-8")
        self.runtime.write_text(
            _TESTS.replace("got != 3", "got != 99"), encoding="utf-8"
        )
        self.assertEqual(self._build().returncode, 0)
        self.assertNotEqual(
            subprocess.run(
                [str(self.root / "artifact"), "--litai-test"], check=False
            ).returncode,
            0,
        )

    def test_external_package_is_refused_offline(self) -> None:
        main = self.source / "source/main.go"
        main.write_text(
            _MAIN.replace('"fmt"', '"fmt"\n"example.com/undeclared/module"'),
            encoding="utf-8",
        )
        with self.assertRaises(BuildError):
            self._inventory()

    def test_unsafe_import_is_refused_even_when_go_can_resolve_it(self) -> None:
        main = self.source / "source/main.go"
        main.write_text(_MAIN.replace('"fmt"', '"fmt"\n_ "unsafe"'), encoding="utf-8")
        with self.assertRaises(BuildError) as refusal:
            self._inventory()
        self.assertEqual(refusal.exception.code, "builder.go_source_inventory_invalid")


if __name__ == "__main__":
    unittest.main()
