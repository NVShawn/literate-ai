"""Exercise the inspection protocol with real bounded child processes.

The fixture is a protocol peer, not proof of a real disposable allocation. Native
provisioner qualification must independently inspect the actual instance.
"""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator

from literate_ai.adapters.retained_allocation import (
    RESPONSE_SCHEMA,
    RetainedAllocationOwner,
)
from literate_ai.adapters.worker_provisioning import configure
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.retained_secondary_access import (
    RetainedReadOnlyView,
    RetainedSecondaryAccessPolicy,
)
from literate_ai.contracts.retained_test_identity import RetainedSecondaryTestIdentity
from literate_ai.contracts.worker_provisioning import WorkerProvisioner
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture


@unittest.skipUnless(os.name == "posix", "requires private POSIX configuration")
class RetainedAllocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "provider.json"
        self.mode = self.root / "mode"
        self.calls = self.root / "requests.jsonl"
        self.allocated = self.root / "allocated"
        self.cached = self.root / "response.json"
        self.script = self.root / "inspect.py"
        allocation = self.root / "allocate.py"
        allocation.write_text(
            "from pathlib import Path\n"
            f"Path({str(self.allocated)!r}).touch()\n"
            "raise SystemExit(99)\n"
        )
        self.script.write_text(
            "import json,sys,hashlib,os,time\nfrom pathlib import Path\n"
            f"mode=Path({str(self.mode)!r})\n"
            "mode=mode.read_text() if mode.exists() else 'ok'\n"
            "request=json.load(sys.stdin)\n"
            f"with Path({str(self.calls)!r}).open('a') as log:\n"
            " log.write(json.dumps(request)+'\\n')\n"
            "assert 'UNBOUND_SECRET' not in os.environ\n"
            "if mode=='nonzero': sys.exit(9)\n"
            "if mode=='overflow': print('x'*100000); sys.exit(0)\n"
            "if mode=='timeout': time.sleep(10)\n"
            f"cache=Path({str(self.cached)!r})\n"
            "if mode=='replay': print(cache.read_text()); sys.exit(0)\n"
            "digest='sha256:'+hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()\n"
            f"response={{'schema':{RESPONSE_SCHEMA!r},\n"
            "'request_identity':digest,\n"
            f"'allocation_identity':{canonical_identity('instance-one').uri!r},\n"
            f"'workspace_prefix':{str(self.root / 'workspace')!r},\n"
            "'remaining_seconds':3600,'disposable':True}\n"
            "if mode=='instance': response['allocation_identity']='sha256:'+'0'*64\n"
            "if mode=='prefix': response['workspace_prefix']+='/changed'\n"
            "if mode=='root': response['workspace_prefix']='/'\n"
            "if mode=='escape': response['workspace_prefix']='/work/../elsewhere'\n"
            "if mode=='expired': response['remaining_seconds']=1\n"
            "if mode=='bool': response['remaining_seconds']=True\n"
            "if mode=='physical': response['disposable']=False\n"
            "if mode=='identity': response['request_identity']='sha256:'+'0'*64\n"
            "if mode=='unknown': response['endpoint']='unreviewed'\n"
            "if mode=='disable':\n"
            f" path=Path({str(self.config)!r})\n"
            " config=json.loads(path.read_text()); config['enabled']=False\n"
            " path.write_text(json.dumps(config))\n"
            "text=json.dumps(response)\n"
            "if mode=='duplicate': text=text[:-1]+',\"disposable\":true}'\n"
            "cache.write_text(text)\nprint(text)\n"
        )
        self.settings = WorkerProvisioner(
            (sys.executable, str(allocation)),
            enabled=True,
            inspection_command=(sys.executable, str(self.script)),
        )
        configure(self.config, self.settings)
        plan, _ = retained_project_sbom_fixture()
        policy = RetainedSecondaryAccessPolicy(
            plan.profile.worker_identity,
            RetainedSecondaryTestIdentity("litaitest", 2001, 2001, (2001,)),
            ("test",),
            (RetainedReadOnlyView("runtime", "inputs", "sdk"),),
            ("work",),
        )
        self.plan = replace(
            plan, profile=replace(plan.profile, secondary_access=policy)
        )

    def owner(self):
        return RetainedAllocationOwner(
            self.config,
            plan=self.plan,
            environment=dict(os.environ, UNBOUND_SECRET="never-forward"),
            minimum_remaining_seconds=60,
        )

    def test_real_inspector_is_fresh_bound_and_never_calls_allocator(self):
        owner = self.owner()
        first = owner.evidence()
        owner.require_plan(self.plan, minimum_remaining_seconds=60)
        requests = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0]["nonce"], requests[1]["nonce"])
        for request in requests:
            self.assertEqual(request["plan_identity"], self.plan.identity.uri)
            self.assertEqual(
                request["worker_identity"], self.plan.profile.worker_identity.uri
            )
            self.assertEqual(
                request["policy_identity"],
                self.plan.profile.secondary_access.identity.uri,
            )
        self.assertEqual(owner.workspace_prefix, self.root / "workspace")
        self.assertFalse(self.allocated.exists())
        self.assertEqual(len(owner.evidence()), 2)
        first[0]["acceptance"] = True
        self.assertFalse(owner.evidence()[0]["acceptance"])

    def test_cached_response_and_instance_or_namespace_drift_revoke_owner(self):
        for mode in ("replay", "instance", "prefix"):
            self.mode.unlink(missing_ok=True)
            owner = self.owner()
            self.mode.write_text(mode)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                owner.require_current(minimum_remaining_seconds=60)
            self.mode.unlink()
            with self.assertRaisesRegex(ValueError, "owner-revoked"):
                owner.require_current(minimum_remaining_seconds=60)
            with self.assertRaisesRegex(ValueError, "owner-revoked"):
                _ = owner.workspace_prefix

    def test_malformed_expired_or_non_disposable_inspection_refuses(self):
        for mode in (
            "root",
            "escape",
            "expired",
            "bool",
            "physical",
            "identity",
            "unknown",
            "duplicate",
        ):
            self.mode.write_text(mode)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.owner()
        self.assertFalse(self.allocated.exists())

    def test_disabled_or_missing_inspector_never_invokes_allocation_command(self):
        for settings in (
            replace(self.settings, enabled=False),
            replace(self.settings, inspection_command=()),
        ):
            configure(self.config, settings)
            with self.assertRaisesRegex(ValueError, "inspector-not-enabled"):
                self.owner()
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.allocated.exists())

    def test_config_drift_and_changed_plan_permanently_revoke_owner(self):
        owner = self.owner()
        configure(self.config, replace(self.settings, timeout_seconds=2))
        with self.assertRaisesRegex(ValueError, "config-changed"):
            owner.require_current(minimum_remaining_seconds=60)
        configure(self.config, self.settings)
        with self.assertRaisesRegex(ValueError, "owner-revoked"):
            owner.require_current(minimum_remaining_seconds=60)
        other = self.owner()
        with self.assertRaisesRegex(ValueError, "plan-changed"):
            other.require_plan(
                replace(self.plan, project_identity=canonical_identity("changed")),
                minimum_remaining_seconds=60,
            )

    def test_config_change_during_inspection_refuses(self):
        self.mode.write_text("disable")
        with self.assertRaisesRegex(ValueError, "inspector-not-enabled"):
            self.owner()

    def test_inspector_process_failure_timeout_and_output_overflow_refuse(self):
        configure(self.config, replace(self.settings, timeout_seconds=1))
        for mode in ("nonzero", "overflow", "timeout"):
            self.mode.write_text(mode)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.owner()
        self.assertFalse(self.allocated.exists())

    def test_private_file_parent_symlink_and_hardlink_guards(self):
        self.config.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private-config-file"):
            self.owner()
        self.config.chmod(0o600)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private-config-parent"):
            self.owner()
        self.root.chmod(0o700)
        other = self.root / "copy.json"
        self.config.rename(other)
        self.config.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "canonical-config-path"):
            self.owner()
        self.config.unlink()
        os.link(other, self.config)
        with self.assertRaisesRegex(ValueError, "private-config-file"):
            self.owner()
        self.assertFalse(self.calls.exists())

    def test_untyped_plan_or_missing_policy_and_invalid_budget_refuse(self):
        plan, _ = retained_project_sbom_fixture()
        for invalid in (plan, self.plan.to_dict()):
            with self.assertRaises((TypeError, ValueError)):
                RetainedAllocationOwner(
                    self.config,
                    plan=invalid,
                    environment={},
                    minimum_remaining_seconds=60,
                )
        for seconds in (0, True, 86401):
            with self.assertRaisesRegex(ValueError, "bounded-lifetime"):
                RetainedAllocationOwner(
                    self.config,
                    plan=self.plan,
                    environment={},
                    minimum_remaining_seconds=seconds,
                )
        self.assertFalse(self.calls.exists())


class ProvisionerInspectionContractTests(unittest.TestCase):
    def test_published_schema_accepts_inspector_and_rejects_empty_command(self):
        document = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "schemas/v2/worker-provisioning.schema.json"
            ).read_text()
        )
        Draft202012Validator.check_schema(document)
        validator = Draft202012Validator(document["$defs"]["configuration"])
        settings = WorkerProvisioner(("allocate",), inspection_command=("inspect",))
        validator.validate(settings.to_dict())
        self.assertTrue(
            list(validator.iter_errors(settings.to_dict() | {"inspection_command": []}))
        )

    def test_optional_command_preserves_old_wire_and_refuses_explicit_invalid_values(
        self,
    ):
        legacy = WorkerProvisioner(("provision",))
        self.assertNotIn("inspection_command", legacy.to_dict())
        self.assertEqual(WorkerProvisioner.from_dict(legacy.to_dict()), legacy)
        current = replace(legacy, inspection_command=("inspect",))
        self.assertEqual(WorkerProvisioner.from_dict(current.to_dict()), current)
        for value in ([], None, False, ["bad\0argument"], ["x"] * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WorkerProvisioner.from_dict(
                    legacy.to_dict() | {"inspection_command": value}
                )
        with self.assertRaises(ValueError):
            replace(legacy, inspection_command=["inspect"])
