"""Real FreeBSD command/cleanup admission; inspector is a protocol fixture.

This does not qualify an installed distribution, actual provisioning, an SDK or
the original source suite. It preserves failed native runs for diagnosis.
"""

import hashlib
import json
import os
import subprocess
import sys
import unittest
from dataclasses import replace

from literate_ai.adapters.retained_allocation import (
    RESPONSE_SCHEMA,
)
from literate_ai.adapters.retained_project_acceptance import ACCEPTANCE_POLICY_PATH
from literate_ai.adapters.retained_project_inputs import _remove_tree
from literate_ai.adapters.retained_project_planning import RetainedProjectPlanningResult
from literate_ai.adapters.retained_project_runtime import execute_local_retained_project
from literate_ai.adapters.worker_provisioning import configure
from literate_ai.cache_directories import CacheDirectories
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptancePolicy,
)
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.contracts.retained_secondary_access import (
    RetainedReadOnlyView,
    RetainedSecondaryAccessPolicy,
)
from literate_ai.contracts.retained_secondary_evidence import (
    RetainedSecondaryAccessEvidence,
)
from literate_ai.contracts.retained_test_identity import RetainedSecondaryTestIdentity
from literate_ai.contracts.worker_provisioning import WorkerProvisioner
from tests.critical import test_retained_project_receipt as receipts

_REQUIRED = os.environ.get("LITAI_REQUIRE_FREEBSD_READONLY_VIEWS") == "1"
_NATIVE = sys.platform.startswith("freebsd") and os.geteuid() == 0


@unittest.skipUnless(_NATIVE or _REQUIRED, "requires native FreeBSD root")
class NativeSecondaryReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _NATIVE:
            raise AssertionError("required native receipt platform is unavailable")
        receipts.RetainedProjectReceiptTests.setUpClass()
        cls.addClassCleanup(receipts.RetainedProjectReceiptTests.doClassCleanups)

    def setUp(self):
        import pwd

        name = os.environ.get("LITAI_SECONDARY_TEST_ACCOUNT", "litaitest")
        try:
            account = pwd.getpwnam(name)
        except KeyError:
            if _REQUIRED:
                self.fail("required existing secondary account is unavailable")
            self.skipTest("existing secondary account unavailable")
        self.account = RetainedSecondaryTestIdentity(
            name,
            account.pw_uid,
            account.pw_gid,
            tuple(sorted(set(os.getgrouplist(name, account.pw_gid)))),
        )
        self.assertEqual(
            self.account.groups,
            (account.pw_gid,),
            "fixture requires its sole reviewed group",
        )
        self.fixture = receipts.RetainedProjectReceiptTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_real_secondary_execution_acceptance_cleanup_and_retained_admission(self):
        completion = self.fixture.fixture
        f = completion.fixture
        f._retain_native_fixture = True
        f.base.chmod(0o755)
        worker = ExecutionWorker("native-secondary-fixture", ExecutionWorkerKind.LOCAL)
        f.plan = replace(
            f.plan, profile=replace(f.plan.profile, worker_identity=worker.identity)
        )
        policy = RetainedSecondaryAccessPolicy(
            f.plan.profile.worker_identity,
            self.account,
            ("tests",),
            (
                RetainedReadOnlyView("runtime", "inputs", "tools"),
                RetainedReadOnlyView("products", "exports", "products"),
            ),
            ("work",),
        )
        actions = list(f.plan.profile.actions)
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
        acceptance_path = f.source / ACCEPTANCE_POLICY_PATH
        acceptance = RetainedProjectAcceptancePolicy.from_dict(
            json.loads(acceptance_path.read_bytes())
        )
        acceptance = replace(
            acceptance,
            cases=tuple(
                replace(c, executable="products/app.bin") for c in acceptance.cases
            ),
        )
        content = canonical_json_bytes(acceptance.to_dict())
        acceptance_path.write_bytes(content)
        f.plan = replace(
            f.plan,
            profile=replace(
                f.plan.profile,
                actions=tuple(actions),
                package_inventory=("products", "docs.txt"),
                secondary_access=policy,
                acceptance_policy_identity=ContentIdentity.parse_uri(
                    "sha256:" + hashlib.sha256(content).hexdigest()
                ),
            ),
        )
        f.current = f.plan
        completion.graph = project_retained_project_managed_graph(
            f.plan, completion.locks, version="1.0.0"
        )
        private = f.base / "provider"
        private.mkdir(mode=0o700)
        inspector = private / "inspect.py"
        instance = canonical_identity("native receipt protocol fixture").uri
        inspector.write_text(
            "import json,sys,hashlib\nr=json.load(sys.stdin)\n"
            "d='sha256:'+hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()\n"
            f"print(json.dumps({{'schema':{RESPONSE_SCHEMA!r},'request_identity':d,"
            f"'allocation_identity':{instance!r},'workspace_prefix':{str(f.base)!r},"
            "'remaining_seconds':3600,'disposable':True}))\n"
        )
        config = private / "worker-provisioner.json"
        configure(
            config,
            WorkerProvisioner(
                ("never-allocate",),
                enabled=True,
                inspection_command=(sys.executable, str(inspector)),
            ),
        )
        workspace = None
        try:
            admission, workspace = execute_local_retained_project(
                f.source,
                f.manifest,
                RetainedProjectPlanningResult(f.plan, completion.graph),
                component_locks=completion.locks,
                worker=worker,
                current_plan=lambda: f.current,
                test_receipt_policy=f.configured_test_policy,
                cache_directories=CacheDirectories(
                    f.source, f.base / "generated", f.base / "objects"
                ),
                authorization=f.plan.identity.uri,
                allow_host_execution=True,
                operator_environment={**os.environ, "LITAI_CONFIG_DIR": str(private)},
            )
            self.addCleanup(workspace.close_secondary_access)
            paths = workspace._secondary_views._paths
            self.assertTrue(workspace.root.is_relative_to(f.base))
            self.assertEqual(workspace.root.parent.stat().st_mode & 0o777, 0o700)
            identity = admission.run.secondary_access_identity
            ref = next(
                v for v in admission.receipt.records if v.identity == identity.uri
            )
            evidence = RetainedSecondaryAccessEvidence.from_dict(
                admission.store.get_manifest(ref)
            )
            evidence.require_context(
                f.plan,
                export_manifest_identity=admission.run.resolved_bom.output_manifest.identity,
                command_observations=tuple(
                    v.identity for v in admission.run.observations
                ),
            )
            self.assertTrue(evidence.to_dict()["cleanup_ok"])
            self.assertEqual(admission.receipt.passed, 1)
            self.assertEqual(admission.receipt.skipped, 0)
            for name in ("runtime", "products"):
                self.assertEqual(
                    paths[name].stat().st_dev, paths[name].parent.stat().st_dev
                )
                self.assertEqual(paths[name].parent.stat().st_mode & 0o777, 0o700)
            _remove_tree(workspace.root)
            admission.require_current()
            destination = f.base / "native-payload"
            admission.materialize_payload(destination)
            result = subprocess.run(
                (str(destination / "products/app.bin"), "echo"),
                input=b"native\x00product",
                capture_output=True,
                timeout=10,
                check=True,
            )
            self.assertEqual(result.stdout, b"native\x00product")
            f._retain_native_fixture = False
        except BaseException as exc:
            diagnostic = {
                "error": str(exc),
                "commands": [
                    {
                        "action": v.action.action_id,
                        "returncode": v.result.returncode,
                        "stdout": v.result.stdout.decode("utf-8", "replace"),
                        "stderr": v.result.stderr.decode("utf-8", "replace"),
                    }
                    for v in (workspace.observations if workspace is not None else ())
                ],
                "views": workspace.secondary_access_evidence()
                if workspace is not None
                else [],
            }
            (f.base / "native-receipt-diagnostics.json").write_text(
                json.dumps(diagnostic)
            )
            print(json.dumps(diagnostic), file=sys.stderr, flush=True)
            raise
