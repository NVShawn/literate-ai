"""Retained commands preserve original authority and fail closed at each boundary."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from literate_ai.adapters import retained_project_dependencies as dependencies
from literate_ai.adapters import retained_project_execution as execution
from literate_ai.adapters.builders import BuildError
from literate_ai.adapters.retained_project_acceptance import ACCEPTANCE_POLICY_PATH
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectTestReceiptPolicy,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptanceCase,
    RetainedProjectAcceptancePolicy,
)
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectPlan,
    RetainedProjectProfile,
)
from literate_ai.contracts.retained_project_tests import (
    RetainedProjectExpectedCase,
    RetainedProjectTestPolicy,
)
from literate_ai.contracts.retained_secondary_access import (
    RetainedReadOnlyView,
    RetainedSecondaryAccessPolicy,
)
from literate_ai.contracts.retained_test_identity import RetainedSecondaryTestIdentity


class RetainedProjectExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = shutil.which("cc")
        cls.fixture_binary = Path(sys.executable).resolve()
        if cls.compiler is None or os.name == "nt":
            return
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "fixture.c"
        report = json.dumps(
            {
                "schema": "literate-ai/retained-project-test-report@1",
                "action": "tests",
                "cases": [
                    {"id": "original-suite", "outcome": "passed", "reason": None}
                ],
            }
        )
        source.write_text(
            "#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n"
            "#include <errno.h>\n#include <grp.h>\n#include <pwd.h>\n"
            "#include <sys/wait.h>\n"
            "#include <sys/stat.h>\n"
            "#include <unistd.h>\n"
            "#include <dlfcn.h>\n"
            "int main(int n, char **v) {\n"
            ' if (getenv("LITAI_FIXTURE_SECRET")) return 99;\n'
            " if (n < 2) return 2;\n"
            ' if (!strcmp(v[1], "via-path") && n > 2) {\n'
            "  execvp(v[2], v+2); return 96;\n"
            " }\n"
            ' if (!strcmp(v[1], "echo")) {\n'
            "  int c; while ((c = getchar()) != EOF) if (putchar(c) == EOF) return 7;\n"
            "  return ferror(stdin) ? 8 : 0;\n"
            " }\n"
            ' if (!strcmp(v[1], "empty")) return 0;\n'
            ' if (!strcmp(v[1], "secondary") && n == 9) {\n'
            "  struct passwd *p = getpwnam(v[8]); if (!p || !p->pw_uid) return 60;\n"
            '  FILE *f = fopen(v[5], "ab");\n'
            "  if (f || errno != EROFS) { if (f) fclose(f); return 61; }\n"
            "  pid_t child = fork(); if (child < 0) return 62;\n"
            "  if (!child) {\n"
            "   for (int fd=3; fd<65536; ++fd) close(fd);\n"
            "   gid_t group = p->pw_gid;\n"
            "   if (setgroups(1,&group) || setgid(group) || setuid(p->pw_uid))\n"
            "    _exit(63);\n"
            '   f = fopen(v[4], "rb"); if (!f) _exit(64); fclose(f);\n'
            '   f = fopen(v[5], "rb"); if (!f) _exit(65); fclose(f);\n'
            '   f = fopen(v[7], "rb");\n'
            "   if (f || errno != EACCES) _exit(66);\n"
            '   f = fopen(v[6], "wb"); if (!f) _exit(67);\n'
            "   if (fclose(f) || unlink(v[6])) _exit(68); _exit(0);\n"
            "  }\n"
            "  int status;\n"
            "  if (waitpid(child,&status,0) != child || status) {\n"
            '   fprintf(stderr,"secondary child status=%d\\n",status); return 69; }\n'
            '  f = fopen(v[3], "wb"); if (!f) return 70;\n'
            "  fputs(" + json.dumps(report) + ", f); return fclose(f) ? 71 : 0;\n"
            " }\n"
            ' if (!strcmp(v[1], "directory") && n == 3) return mkdir(v[2], 0755);\n'
            ' if (!strcmp(v[1], "fail")) {fputs("failed", stderr); return 9;}\n'
            ' if (!strcmp(v[1], "scale") && n == 4) {\n'
            "  void *lib = dlopen(v[2], RTLD_NOW | RTLD_LOCAL);\n"
            "  if (!lib) { fputs(dlerror(), stderr); return 17; }\n"
            '  double (*scale)(double,double) = dlsym(lib, "vendor_math_scale");\n'
            "  if (!scale || scale(1.5, 3) != 4.5) return 18;\n"
            '  FILE *f = fopen(v[3], "wb"); if (!f) return 19;\n'
            '  fprintf(f, "%.1f", scale(1.5, 3));\n'
            "  if (fclose(f) || dlclose(lib)) return 20; return 0;\n"
            " }\n"
            " if (n < 3) return 2;\n"
            ' if (!strcmp(v[1], "export") || !strcmp(v[1], "export-nested")\n'
            '     || !strcmp(v[1], "export-visible")) {\n'
            '  if (strcmp(v[1], "export")) {\n'
            "   char *dir = strdup(v[2]); if (!dir) return 14;\n"
            "   char *slash = strrchr(dir, '/'); if (!slash) return 15;\n"
            '   *slash = 0; int mode = !strcmp(v[1], "export-visible") ? 0755 : 0700;\n'
            "   int rc = mkdir(dir, mode); free(dir); if (rc) return 16;\n"
            "  }\n"
            '  FILE *in = fopen(v[0], "rb"), *out = fopen(v[2], "wb");\n'
            "  if (!in || !out) return 10;\n"
            "  int c; while ((c = fgetc(in)) != EOF)\n"
            "    if (fputc(c, out) == EOF) return 11;\n"
            "  if (ferror(in) || fclose(out) || fclose(in)) return 12;\n"
            "  return chmod(v[2], 0755) ? 13 : 0;\n"
            " }\n"
            ' if (!strcmp(v[1], "extra") && n == 4) {\n'
            '  FILE *f = fopen(v[3], "wb"); if (!f) return 5;\n'
            '  fputs("extra", f); fclose(f);\n'
            " }\n"
            ' if (!strcmp(v[1], "spam")) {\n'
            "  for (int i=0; i<2048; ++i) fputc(65, stdout); return 0;\n"
            " }\n"
            ' if (!strcmp(v[1], "check")) {\n'
            '  FILE *f = fopen(v[2], "rb"); if (!f) return 3;\n'
            "  int c = fgetc(f); fclose(f); return c == EOF ? 4 : 0;\n"
            " }\n"
            ' if ((!strcmp(v[1], "test") || !strcmp(v[1], "report")) && n == 4) {\n'
            '  if (!strcmp(v[1], "test")) {\n'
            '   FILE *app = fopen(v[2], "rb"); if (!app) return 3;\n'
            "   int c = fgetc(app); fclose(app); if (c != 'b') return 4;\n"
            '   FILE *f = fopen(v[3], "wb"); if (!f) return 5;\n'
            f"   fputs({json.dumps(report)}, f); return fclose(f) ? 6 : 0;\n"
            "  }\n"
            '  FILE *f = fopen(v[2], "wb"); if (!f) return 5;\n'
            "  fputs(v[3], f); return fclose(f) ? 6 : 0;\n"
            " }\n"
            ' FILE *f = fopen(v[2], "wb"); if (!f) return 5;\n'
            " fputs(v[1], f); return fclose(f) ? 6 : 0;\n"
            "}\n",
            encoding="utf-8",
        )
        cls.fixture_binary = root / "fixture"
        subprocess.run(
            (cls.compiler, str(source), "-o", str(cls.fixture_binary)),
            capture_output=True,
            check=True,
            timeout=30,
        )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()

        def cleanup():
            if getattr(self, "_retain_native_fixture", False):
                # Do not recurse through a failed mount cleanup or discard the
                # streams that explain a failed native command.
                self.base.chmod(0o700)
                temporary._finalizer.detach()
            else:
                temporary.cleanup()

        self.addCleanup(cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "original"
        for directory in ("src", "tools"):
            (self.source / directory).mkdir(parents=True)
        (self.source / "src/main.txt").write_bytes(b"original\x00asset")
        self.runner_path = "tools/runner.exe" if os.name == "nt" else "tools/runner"
        shutil.copyfile(self.fixture_binary, self.source / self.runner_path)
        (self.source / self.runner_path).chmod(0o755)
        if os.name == "nt":
            # Windows guards use a native PE stand-in; C action execution is
            # POSIX-only. Base Python needs adjacent native DLLs, unlike a venv
            # redirector. Keep those imports captured rather than adding the
            # ambient Python installation to the command's search path.
            for pattern in ("python*.dll", "vcruntime*.dll"):
                for library in Path(sys.base_prefix).glob(pattern):
                    shutil.copyfile(library, self.source / "tools" / library.name)
        self.configured_test_policy = ProjectTestReceiptPolicy(
            "retained-fixture",
            "1.0.0",
            canonical_identity("runner"),
            ("test-report", "test-runner"),
            1,
        )
        self.test_policy = RetainedProjectTestPolicy(
            self.configured_test_policy.identity,
            (RetainedProjectExpectedCase("original-suite", "tests"),),
            (("tests", "test-result.json"),),
        )
        policy_bytes = canonical_json_bytes(self.test_policy.to_dict())
        (self.source / "test-policy.json").write_bytes(policy_bytes)
        self.manifest = discover_retained_project_inputs(
            self.source,
            {"src": "source", "tools": "toolchain", "test-policy.json": "test-policy"},
            RetainedProjectLimits(100, 64 * 1024 * 1024, 128 * 1024 * 1024),
            executable_paths=(self.runner_path,),
        )
        self.actions = (
            RetainedProjectAction(
                "build",
                "build",
                "shell",
                ("build", "{output_root}/app.bin"),
                (),
                ("build-app",),
                ("app.bin",),
                10,
            ),
            RetainedProjectAction(
                "tests",
                "test",
                "shell",
                ("test", "{output_root}/app.bin", "{output_root}/test-result.json"),
                ("build",),
                ("original-suite",),
                ("test-result.json",),
                10,
            ),
            RetainedProjectAction(
                "docs",
                "docs",
                "shell",
                ("docs", "{output_root}/docs.txt"),
                ("build",),
                ("original-docs",),
                ("docs.txt",),
                10,
            ),
            RetainedProjectAction(
                "accept",
                "accept",
                "shell",
                ("check", "{output_root}/docs.txt"),
                ("tests", "docs"),
                ("oracle-command",),
                (),
                10,
            ),
        )
        self.profile = RetainedProjectProfile(
            "host",
            *(canonical_identity(n) for n in range(3)),
            ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(policy_bytes).hexdigest()
            ),
            canonical_identity(4),
            (
                (
                    "shell",
                    self.runner_path,
                    ContentIdentity.parse_uri(
                        "sha256:"
                        + next(
                            m.sha256
                            for m in self.manifest.members
                            if m.path == self.runner_path
                        )
                    ),
                ),
            ),
            self.actions,
            ("app.bin", "docs.txt"),
        )
        self.plan = RetainedProjectPlan(
            "fixture",
            canonical_identity("authority"),
            (canonical_identity("lock"),),
            canonical_identity("conversion-original"),
            canonical_identity("standard"),
            self.manifest.identity,
            self.profile,
        )
        self.current = self.plan

    def workspace(self, **changes):
        return execution.RetainedProjectExecutionWorkspace(
            self.source,
            self.manifest,
            self.plan,
            **{
                "parent": self.base,
                "current_plan": lambda: self.current,
                "authorization": self.plan.identity.uri,
                "allow_host_execution": True,
                "test_receipt_policy": self.configured_test_policy,
                **changes,
            },
        )

    def alter_action(self, index, **changes):
        actions = list(self.actions)
        actions[index] = replace(actions[index], **changes)
        self.plan = replace(
            self.plan, profile=replace(self.profile, actions=tuple(actions))
        )
        self.current = self.plan

    def secondary_workspace(self, *, parent=None, preflight_effect=None):
        policy = RetainedSecondaryAccessPolicy(
            self.plan.profile.worker_identity,
            RetainedSecondaryTestIdentity("litaitest", 2001, 2001, (2001,)),
            ("tests",),
            (RetainedReadOnlyView("runtime", "inputs", "tools"),),
            ("work",),
        )
        self.plan = replace(
            self.plan, profile=replace(self.plan.profile, secondary_access=policy)
        )
        self.current = self.plan
        allocation = Mock(spec=execution.RetainedAllocationOwner)
        allocation.workspace_prefix = self.base
        views = Mock()
        views.paths = {"runtime": self.base / "runtime", "work": self.base / "work"}
        views.evidence.return_value = ({"event": "reserved"},)
        with (
            patch.object(
                execution, "_requires_freebsd_acl_preflight", return_value=True
            ),
            patch.object(
                execution,
                "preflight_freebsd_posix_acl",
                side_effect=preflight_effect,
            ) as preflight,
            patch.object(execution, "FreeBsdRetainedViewOwner", return_value=views),
        ):
            workspace = self.workspace(
                allocation_owner=allocation, parent=parent or self.base
            )
        preflight.assert_called_once_with(
            parent or self.base, self.plan.profile.secondary_access.account.uid
        )
        self.addCleanup(workspace.close_secondary_access)
        return workspace, allocation, views

    def test_secondary_acl_preflight_refuses_before_input_capture(self):
        with (
            patch.object(execution, "capture_retained_project_inputs") as capture,
            self.assertRaisesRegex(ValueError, "filesystem-unsupported"),
        ):
            self.secondary_workspace(
                preflight_effect=ValueError("retained.acl.filesystem-unsupported")
            )
        capture.assert_not_called()
        self.assertEqual(list(self.base.glob("retained-run-*")), [])

    @unittest.skipIf(os.name == "nt", "native fixture executable requires POSIX")
    def test_secondary_paths_build_binding_and_test_scope_are_explicit(self):
        workspace, allocation, views = self.secondary_workspace()
        self.assertEqual(workspace.inputs.stat().st_mode & 0o777, 0o700)
        replacements = workspace._command_replacements()
        self.assertEqual(replacements["{secondary_account}"], "litaitest")
        self.assertEqual(
            replacements["{secondary_view:runtime}"], str(views.paths["runtime"])
        )
        self.assertEqual(
            replacements["{secondary_fixture:work}"], str(views.paths["work"])
        )
        workspace.execute_next()
        views.bind_outputs.assert_called_once()
        views.activate.assert_not_called()
        with patch.object(
            execution, "run_bounded_process", wraps=execution.run_bounded_process
        ) as command:
            original = command._mock_wraps

            def run(*args, **kwargs):
                views.activate.assert_called_once_with("tests")
                views.deactivate.assert_not_called()
                return original(*args, **kwargs)

            command.side_effect = run
            workspace.execute_next()
        views.deactivate.assert_called_once()
        self.assertEqual(
            workspace.secondary_access_evidence(), ({"event": "reserved"},)
        )
        allocation.require_plan.assert_called_with(
            self.plan, minimum_remaining_seconds=60
        )
        with self.assertRaisesRegex(ValueError, "run-incomplete"):
            workspace.completed_run()

    @unittest.skipIf(os.name == "nt", "native fixture executable requires POSIX")
    def test_secondary_command_exception_revokes_and_prevents_resume(self):
        workspace, _, views = self.secondary_workspace()
        workspace.execute_next()
        with patch.object(
            execution,
            "run_bounded_process",
            side_effect=RuntimeError("command timeout"),
        ):
            with self.assertRaisesRegex(RuntimeError, "command timeout"):
                workspace.execute_next()
        views.activate.assert_called_once_with("tests")
        views.deactivate.assert_called_once()
        views.close.assert_called_once()
        with self.assertRaisesRegex(ValueError, "no-next-action"):
            workspace.execute_next()

    @unittest.skipIf(os.name == "nt", "native fixture executable requires POSIX")
    def test_secondary_unknown_binding_refuses_before_command(self):
        self.alter_action(0, argv=("{secondary_fixture:undeclared}",))
        workspace, _, views = self.secondary_workspace()
        with patch.object(execution, "run_bounded_process") as command:
            with self.assertRaisesRegex(ValueError, "unknown-command-binding"):
                workspace.execute_next()
        command.assert_not_called()
        views.close.assert_called_once()

    def test_allocation_without_policy_refuses_before_workspace_creation(self):
        allocation = Mock(spec=execution.RetainedAllocationOwner)
        with self.assertRaisesRegex(ValueError, "policy-required"):
            self.workspace(allocation_owner=allocation)
        allocation.require_plan.assert_not_called()
        self.assertEqual(list(self.base.glob("retained-run-*")), [])

    def test_secondary_parent_outside_allocation_refuses_before_capture(self):
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaisesRegex(ValueError, "allocated-parent-required"):
                self.secondary_workspace(parent=Path(outside).resolve())
        self.assertEqual(list(self.base.glob("retained-run-*")), [])

    @unittest.skipIf(os.name == "nt", "native fixture executable requires POSIX")
    def test_secondary_constructor_failure_preserves_private_backing_trees(self):
        _, allocation, _ = self.secondary_workspace()
        with (
            patch.object(
                execution,
                "FreeBsdRetainedViewOwner",
                side_effect=ValueError("partial mount"),
            ),
            patch.object(execution, "preflight_freebsd_posix_acl"),
            patch.object(execution, "_remove_tree") as remove,
        ):
            with self.assertRaisesRegex(ValueError, "partial mount"):
                self.workspace(allocation_owner=allocation)
        remove.assert_not_called()
        runs = list(self.base.glob("retained-run-*"))
        self.assertEqual(len(runs), 2)
        for root in runs:
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (root / "snapshot/tree/src/main.txt").read_bytes(), b"original\x00asset"
            )

    @unittest.skipIf(os.name == "nt", "native fixture executable requires POSIX")
    def test_secondary_revocation_failure_rejects_successful_command(self):
        workspace, _, views = self.secondary_workspace()
        workspace.execute_next()
        views.deactivate.side_effect = ValueError("revocation failed")
        with self.assertRaisesRegex(ValueError, "revocation failed"):
            workspace.execute_next()
        self.assertEqual(len(workspace.observations), 1)
        views.close.assert_called_once()
        with self.assertRaisesRegex(ValueError, "no-next-action"):
            workspace.execute_next()

    @unittest.skipUnless(
        (sys.platform.startswith("freebsd") and os.geteuid() == 0)
        or os.environ.get("LITAI_REQUIRE_FREEBSD_READONLY_VIEWS") == "1",
        "requires native FreeBSD root",
    )
    def test_native_workspace_owns_real_views_and_secondary_command_lifetime(self):
        import pwd

        from literate_ai.adapters.retained_allocation import RESPONSE_SCHEMA
        from literate_ai.adapters.worker_provisioning import configure
        from literate_ai.contracts.worker_provisioning import WorkerProvisioner

        self.assertEqual(os.geteuid(), 0, "native view integration requires root")
        self.assertIsNotNone(self.compiler, "native view integration requires cc")
        name = os.environ.get("LITAI_SECONDARY_TEST_ACCOUNT", "litaitest")
        try:
            actual = pwd.getpwnam(name)
        except KeyError:
            if os.environ.get("LITAI_REQUIRE_FREEBSD_READONLY_VIEWS") == "1":
                self.fail("required existing secondary account is unavailable")
            self.skipTest("existing secondary account unavailable")
        policy = RetainedSecondaryAccessPolicy(
            self.plan.profile.worker_identity,
            RetainedSecondaryTestIdentity(
                name,
                actual.pw_uid,
                actual.pw_gid,
                tuple(sorted(set(os.getgrouplist(name, actual.pw_gid)))),
            ),
            ("tests",),
            (
                RetainedReadOnlyView("runtime", "inputs", "tools"),
                RetainedReadOnlyView("products", "exports", "products"),
            ),
            ("work",),
        )
        actions = list(self.plan.profile.actions)
        actions[0] = replace(
            actions[0],
            argv=("export-visible", "{output_root}/products/app.bin"),
            outputs=("products",),
        )
        actions[1] = replace(
            actions[1],
            argv=(
                "secondary",
                "{output_root}/products/app.bin",
                "{output_root}/test-result.json",
                "{secondary_view:runtime}/runner",
                "{secondary_view:products}/app.bin",
                "{secondary_fixture:work}/scratch",
                "{input_root}/tools/runner",
                "{secondary_account}",
            ),
        )
        self.plan = replace(
            self.plan,
            profile=replace(
                self.profile,
                actions=tuple(actions),
                package_inventory=("products", "docs.txt"),
                secondary_access=policy,
            ),
        )
        self.current = self.plan
        self.base.chmod(0o755)
        self._retain_native_fixture = True
        private = self.base / "provider"
        private.mkdir(mode=0o700)
        inspector = private / "inspect.py"
        fixture_identity = canonical_identity("workspace protocol fixture").uri
        inspector.write_text(
            "import json,sys,hashlib\nr=json.load(sys.stdin)\n"
            "d='sha256:'+hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()\n"
            f"print(json.dumps({{'schema':{RESPONSE_SCHEMA!r},'request_identity':d,"
            f"'allocation_identity':{fixture_identity!r},"
            f"'workspace_prefix':{str(self.base)!r},'remaining_seconds':3600,'disposable':True}}))\n"
        )
        config = private / "provider.json"
        configure(
            config,
            WorkerProvisioner(
                ("never-allocate",),
                enabled=True,
                inspection_command=(sys.executable, str(inspector)),
            ),
        )
        allocation = execution.RetainedAllocationOwner(
            config,
            plan=self.plan,
            environment=os.environ,
            minimum_remaining_seconds=60,
        )
        workspace = self.workspace(allocation_owner=allocation)
        self.addCleanup(workspace.close_secondary_access)
        paths = workspace._secondary_views.paths
        try:
            workspace.execute_next()
            workspace.execute_next()
        except BaseException:
            diagnostic = [
                {
                    "action": v.action.action_id,
                    "returncode": v.result.returncode,
                    "stdout": v.result.stdout.decode("utf-8", "replace"),
                    "stderr": v.result.stderr.decode("utf-8", "replace"),
                }
                for v in workspace.observations
            ]
            (workspace.root / "native-command-diagnostics.json").write_text(
                json.dumps(diagnostic)
            )
            print(json.dumps(diagnostic), file=sys.stderr, flush=True)
            raise
        self.assertEqual(workspace.test_reports[0].report.passed, 1)
        events = workspace.secondary_access_evidence()
        self.assertEqual(
            [v["event"] for v in events],
            ["mounted", "mounted", "activated", "deactivated"],
        )
        self.assertEqual(list(paths["work"].iterdir()), [])
        workspace.close_secondary_access()
        self.assertTrue(workspace.secondary_access_evidence()[-1]["cleanup_ok"])
        for path in (paths["runtime"], paths["products"]):
            self.assertEqual(path.stat().st_dev, path.parent.stat().st_dev)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self._retain_native_fixture = False

    def test_authorization_and_current_plan_precede_workspace_creation(self):
        for changes in ({"allow_host_execution": False}, {"authorization": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.workspace(**changes)
        self.current = replace(self.plan, project_id="changed")
        with self.assertRaisesRegex(ValueError, "plan-changed"):
            self.workspace()
        self.assertEqual(list(self.base.glob("retained-run-*")), [])

    def test_manifest_and_tool_substitution_refuse(self):
        wrong = replace(
            self.manifest, limits=replace(self.manifest.limits, max_entries=101)
        )
        with self.assertRaisesRegex(ValueError, "manifest-mismatch"):
            execution.RetainedProjectExecutionWorkspace(
                self.source,
                wrong,
                self.plan,
                parent=self.base,
                current_plan=lambda: self.plan,
                authorization=self.plan.identity.uri,
                allow_host_execution=True,
                test_receipt_policy=self.configured_test_policy,
            )
        self.plan = replace(
            self.plan,
            profile=replace(
                self.profile,
                tools=(("shell", self.runner_path, canonical_identity("wrong")),),
            ),
        )
        self.current = self.plan
        with self.assertRaisesRegex(ValueError, "tool-mismatch"):
            self.workspace()
        self.assertEqual(list(self.base.glob("retained-run-*")), [])

    def test_output_ownership_cannot_alias_or_overlap_across_stages(self):
        for outputs in (("app.bin",), ("APP.BIN",), ("app.bin/child",)):
            with self.subTest(outputs=outputs), self.assertRaises(ValueError):
                replace(
                    self.profile,
                    actions=(
                        self.actions[0],
                        self.actions[1],
                        replace(self.actions[2], outputs=outputs),
                        self.actions[3],
                    ),
                )

    def test_output_capture_projects_reviewed_executable_intent(self):
        policy = RetainedProjectAcceptancePolicy(
            self.plan.component_locks,
            (
                RetainedProjectAcceptanceCase(
                    "oracle-command",
                    "accept",
                    "app.bin",
                    (),
                    b"",
                    0,
                    b"",
                    b"",
                    10,
                ),
            ),
        )
        content = canonical_json_bytes(policy.to_dict())
        policy_path = self.source / ACCEPTANCE_POLICY_PATH
        policy_path.parent.mkdir()
        policy_path.write_bytes(content)
        self.profile = replace(
            self.profile,
            acceptance_policy_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
            ),
        )
        self.plan = replace(self.plan, profile=self.profile)
        self.current = self.plan
        workspace = self.workspace()
        output = workspace.outputs / "app.bin"
        output.write_bytes(b"portable executable fixture")
        output.chmod(0o755)
        with patch.object(
            execution,
            "discover_retained_project_inputs",
            wraps=execution.discover_retained_project_inputs,
        ) as discover:
            manifest = workspace._capture_outputs(self.actions[0])
        self.assertEqual(discover.call_args.kwargs["executable_paths"], ("app.bin",))
        self.assertEqual(manifest.members[0].mode, 0o755)

    def test_stale_original_or_snapshot_refuses_before_process_launch(self):
        for selected in ("original", "snapshot"):
            with self.subTest(selected=selected):
                workspace = self.workspace()
                path = (
                    self.source if selected == "original" else workspace.inputs
                ) / "src/main.txt"
                path.chmod(0o644)
                path.write_bytes(b"changed")
                with patch.object(execution, "run_bounded_process") as process:
                    with self.assertRaises(ValueError):
                        workspace.execute_next()
                    process.assert_not_called()
                self.assertEqual(workspace.observations, ())
                path.write_bytes(b"original\x00asset")

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "C process fixture")
    def test_real_complete_graph_has_observations_without_acceptance_authority(self):
        with patch.dict(os.environ, {"LITAI_FIXTURE_SECRET": "must-not-leak"}):
            workspace = self.workspace()
            for action in self.actions:
                observed = workspace.execute_next()
                self.assertEqual(observed.action, action)
                self.assertEqual(observed.result.returncode, 0)
                self.assertEqual(observed.tool_identity, self.profile.tools[0][2])
                self.assertEqual(observed.plan_identity, self.plan.identity)
                self.assertEqual(
                    observed.dependency_identity, workspace.dependency_identity
                )
                self.assertTrue(workspace.dependency_observation.components)
                self.assertNotIn("LITAI_FIXTURE_SECRET", workspace._environment())
        self.assertEqual((workspace.outputs / "app.bin").read_bytes(), b"build")
        self.assertEqual(
            (self.source / "src/main.txt").read_bytes(), b"original\x00asset"
        )
        self.assertNotIn("conversion", {p.name for p in self.source.iterdir()})
        self.assertEqual(len({o.identity for o in workspace.observations}), 4)
        self.assertEqual(len(workspace.test_reports), 1)
        verified = workspace.test_reports[0]
        self.assertEqual(verified.report.passed, 1)
        self.assertEqual(verified.report.skipped, 0)
        self.assertEqual(
            verified.observation_identity, workspace.observations[1].identity
        )
        with self.assertRaisesRegex(ValueError, "no-next-action"):
            workspace.execute_next()

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "C process fixture")
    def test_native_dependency_drift_refuses_before_command(self):
        workspace = self.workspace()
        changed = workspace.dependency_observation
        changed.components[0]["name"] = "changed native dependency"
        with patch.object(dependencies, "PortableHostDependencyObserver") as factory:
            factory.return_value.observe.return_value = changed
            with patch.object(execution, "run_bounded_process") as process:
                with self.assertRaisesRegex(ValueError, "observation-changed"):
                    workspace.execute_next()
                process.assert_not_called()
        with self.assertRaisesRegex(ValueError, "no-next-action"):
            workspace.execute_next()

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "C process fixture")
    def test_native_dependency_drift_after_real_success_refuses_observation(self):
        workspace = self.workspace()
        before = workspace.dependency_observation
        changed = workspace.dependency_observation
        changed.components[0]["name"] = "changed native dependency"
        with patch.object(dependencies, "PortableHostDependencyObserver") as factory:
            factory.return_value.observe.side_effect = (before, changed)
            with self.assertRaisesRegex(ValueError, "observation-changed"):
                workspace.execute_next()
        self.assertEqual((workspace.outputs / "app.bin").read_bytes(), b"build")
        self.assertEqual(workspace.observations, ())
        with self.assertRaisesRegex(ValueError, "no-next-action"):
            workspace.execute_next()

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "C process fixture")
    def test_failed_test_retains_failure_and_prevents_docs_and_acceptance(self):
        self.alter_action(1, argv=("fail",))
        workspace = self.workspace()
        workspace.execute_next()
        with self.assertRaises(execution.RetainedProjectExecutionError) as raised:
            workspace.execute_next()
        self.assertEqual(len(raised.exception.observations), 2)
        self.assertEqual(raised.exception.observations[-1].result.returncode, 9)
        self.assertEqual(raised.exception.observations[-1].result.stderr, b"failed")
        self.assertFalse((workspace.outputs / "docs.txt").exists())
        with self.assertRaisesRegex(ValueError, "no-next-action"):
            workspace.execute_next()

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "C process fixture")
    def test_zero_exit_without_complete_passing_cases_stops_lifecycle(self):
        case = {"id": "original-suite", "outcome": "passed", "reason": None}
        report = {
            "schema": "literate-ai/retained-project-test-report@1",
            "action": "tests",
            "cases": [case],
        }
        invalid = (
            {**report, "cases": []},
            {**report, "cases": [{**case, "id": "other"}]},
            {**report, "cases": [case, {**case, "id": "other"}]},
            {**report, "cases": [case, case]},
            {**report, "cases": [{**case, "outcome": "failed"}]},
            {
                **report,
                "cases": [{**case, "outcome": "skipped", "reason": "exception"}],
            },
            {**report, "cases": [{**case, "outcome": "xfail"}]},
            {**report, "action": "another"},
            {**report, "passed": True},
        )
        for document in invalid:
            with self.subTest(document=document):
                self.alter_action(
                    1,
                    argv=(
                        "report",
                        "{output_root}/test-result.json",
                        json.dumps(document, separators=(",", ":")),
                    ),
                )
                workspace = self.workspace()
                workspace.execute_next()
                with self.assertRaisesRegex(
                    execution.RetainedProjectExecutionError, "test-report-rejected"
                ) as raised:
                    workspace.execute_next()
                self.assertEqual(len(raised.exception.observations), 2)
                self.assertEqual(raised.exception.observations[-1].result.returncode, 0)
                self.assertFalse((workspace.outputs / "docs.txt").exists())
                self.assertFalse(workspace.test_reports)
                with self.assertRaisesRegex(ValueError, "no-next-action"):
                    workspace.execute_next()

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "C process fixture")
    def test_all_permitted_skips_stop_before_docs_and_acceptance(self):
        policy = replace(
            self.test_policy,
            cases=(RetainedProjectExpectedCase("original-suite", "tests", "platform"),),
        )
        content = canonical_json_bytes(policy.to_dict())
        (self.source / "test-policy.json").write_bytes(content)
        self.manifest = discover_retained_project_inputs(
            self.source,
            {"src": "source", "tools": "toolchain", "test-policy.json": "test-policy"},
            self.manifest.limits,
        )
        self.plan = replace(
            self.plan,
            manifest_identity=self.manifest.identity,
            profile=replace(
                self.plan.profile,
                test_policy_identity=ContentIdentity.parse_uri(
                    "sha256:" + hashlib.sha256(content).hexdigest()
                ),
            ),
        )
        self.profile = self.plan.profile
        report = {
            "schema": "literate-ai/retained-project-test-report@1",
            "action": "tests",
            "cases": [
                {"id": "original-suite", "outcome": "skipped", "reason": "platform"}
            ],
        }
        self.alter_action(
            1, argv=("report", "{output_root}/test-result.json", json.dumps(report))
        )
        workspace = self.workspace()
        workspace.execute_next()
        with self.assertRaisesRegex(ValueError, "passes-below-project-minimum"):
            workspace.execute_next()
        self.assertEqual(workspace.test_reports[0].report.passed, 0)
        self.assertFalse((workspace.outputs / "docs.txt").exists())
        with self.assertRaisesRegex(ValueError, "no-next-action"):
            workspace.execute_next()

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "C process fixture")
    def test_missing_and_extra_exports_refuse(self):
        commands = (
            ("empty",),
            ("extra", "{output_root}/app.bin", "{output_root}/extra"),
        )
        for command in commands:
            with self.subTest(command=command):
                self.alter_action(0, argv=command)
                workspace = self.workspace()
                with self.assertRaises(ValueError):
                    workspace.execute_next()

    @unittest.skipIf(os.name == "nt", "POSIX symlink fixture")
    def test_redirected_output_and_mutated_snapshot_refuse_after_zero_exit(self):
        for mutation in ("output-link", "snapshot-write"):
            with self.subTest(mutation=mutation):
                workspace = self.workspace()

                def run(*args, mutation=mutation, workspace=workspace, **kwargs):
                    if mutation == "output-link":
                        (workspace.outputs / "app.bin").symlink_to(
                            self.source / "src/main.txt"
                        )
                    else:
                        path = workspace.inputs / "src/main.txt"
                        path.chmod(0o644)
                        path.write_bytes(b"substituted")
                    return execution.BoundedProcessResult(0, b"", b"")

                with patch.object(execution, "run_bounded_process", side_effect=run):
                    with self.assertRaises(ValueError):
                        workspace.execute_next()
                self.assertEqual(workspace.observations, ())

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "C process fixture")
    def test_changed_plan_after_zero_exit_is_not_a_successful_observation(self):
        workspace = self.workspace()
        original = execution.run_bounded_process

        def run(*args, **kwargs):
            result = original(*args, **kwargs)
            self.current = replace(self.plan, project_id="changed")
            return result

        with patch.object(execution, "run_bounded_process", side_effect=run):
            with self.assertRaisesRegex(ValueError, "plan-changed"):
                workspace.execute_next()
        self.assertEqual(workspace.observations, ())

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "C process fixture")
    def test_output_overflow_blocks_later_actions(self):
        self.alter_action(0, argv=("spam", "unused"))
        workspace = self.workspace()
        with patch.object(execution, "_PROCESS_OUTPUT_BYTES", 128):
            with self.assertRaisesRegex(BuildError, "stdout or stderr byte budget"):
                workspace.execute_next()
        with self.assertRaisesRegex(ValueError, "no-next-action"):
            workspace.execute_next()

    @unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "C process fixture")
    def test_sealed_export_mutation_is_rejected_before_next_action(self):
        workspace = self.workspace()
        workspace.execute_next()
        (workspace.outputs / "app.bin").write_bytes(b"substituted")
        with patch.object(execution, "run_bounded_process") as process:
            with self.assertRaises(ValueError):
                workspace.execute_next()
            process.assert_not_called()

    def test_missing_stages_and_incomplete_acceptance_edges_refuse(self):
        for actions in (
            self.actions[:-1],
            (*self.actions[:-1], replace(self.actions[-1], requires=("tests",))),
        ):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                replace(self.profile, actions=actions)

    def test_package_inventory_needs_declared_output_producers(self):
        with self.assertRaisesRegex(ValueError, "no declared output producer"):
            replace(self.profile, package_inventory=("unproduced.bin",))


if __name__ == "__main__":
    unittest.main()
