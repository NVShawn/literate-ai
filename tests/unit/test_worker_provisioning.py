"""Real child-process and public CLI checks for optional dynamic workers."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import worker_provisioning as adapter
from literate_ai.adapters.execution_dispatch import ExecutionDispatchAdapterError
from literate_ai.adapters.user_paths import UserPaths
from literate_ai.adapters.worker_registry import read_registry
from literate_ai.cli.dispatch import main
from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.execution_dispatch import ExecutionWorkerEnvironment
from literate_ai.contracts.worker_provisioning import WorkerProvisioner


class WorkerProvisioningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "worker-provisioner.json"
        self.state = self.root / "state"
        self.catalog = self.root / "workers.json"
        self.marker = self.root / "calls"
        self.script = self.root / "provider.py"
        worker = ExecutionWorker(
            "test-worker",
            ExecutionWorkerKind.SSH,
            endpoint="user@example.invalid",
            workspace="~/litai",
        )
        self.script.write_text(
            "import sys,json,hashlib,os,time\n"
            "from pathlib import Path\n"
            f"marker=Path({str(self.marker)!r})\n"
            "if sys.argv[-1] in ('--help','help'):\n"
            " print('Provisioner help '+os.environ.get('BOUND_SECRET',''))\n"
            " sys.exit(0)\n"
            "request=json.load(sys.stdin)\n"
            "marker.write_text(marker.read_text()+'x' if marker.exists() else 'x')\n"
            "mode=request['parameters'].get('mode')\n"
            "if mode=='timeout': time.sleep(10)\n"
            "if mode=='overflow': print('x'*100000); sys.exit(0)\n"
            "if mode=='error':\n"
            " print(os.environ.get('BOUND_SECRET',''),file=sys.stderr)\n"
            " sys.exit(1)\n"
            "if mode=='invalid': print('{}'); sys.exit(0)\n"
            "if mode=='env':\n"
            " assert 'UNBOUND_SECRET' not in os.environ\n"
            " assert os.environ['BOUND_SECRET']=='secret-value'\n"
            f"worker={worker.to_dict()!r}\n"
            "worker['worker_id']=request['worker_id']\n"
            "worker['requirements']=request['requirements']\n"
            "worker['target_profile']=request['target_profile']\n"
            "if mode=='mismatch': worker['worker_id']='wrong-worker'\n"
            "identity='sha256:'+hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()\n"
            f"print(json.dumps({{'schema':{adapter.RESPONSE_SCHEMA!r},'request_identity':identity,'worker':worker,'lease_id':'opaque-lease'}}))\n",
            encoding="utf-8",
        )
        self.settings = WorkerProvisioner(
            (sys.executable, str(self.script)), enabled=True
        )
        adapter.configure(self.config, self.settings)

    def launch(self, mode=None, request_id="request-one"):
        return adapter.provision(
            self.config,
            self.state,
            self.catalog,
            "test-worker",
            request_id,
            ExecutionRequirements(),
            "host",
            {"mode": mode} if mode else {},
            dict(
                os.environ, SOURCE_SECRET="secret-value", UNBOUND_SECRET="never-forward"
            ),
        )

    def test_disabled_and_missing_credentials_do_not_invoke_provider(self):
        adapter.configure(self.config, enabled=False)
        with self.assertRaisesRegex(ExecutionDispatchAdapterError, "disabled"):
            self.launch()
        self.assertFalse(self.marker.exists())
        adapter.configure(
            self.config,
            replace(
                self.settings,
                environment=(
                    ExecutionWorkerEnvironment("BOUND_SECRET", "MISSING_SECRET"),
                ),
            ),
        )
        with self.assertRaisesRegex(ExecutionDispatchAdapterError, "credential"):
            self.launch()
        self.assertFalse(self.marker.exists())

    def test_register_and_replay_allocate_once(self):
        result = self.launch()
        self.assertEqual(result["status"], "registered")
        self.assertEqual(self.launch(), result)
        self.assertEqual(self.marker.read_text(), "x")
        self.assertEqual(len(read_registry(self.catalog).workers), 1)
        with self.assertRaisesRegex(ExecutionDispatchAdapterError, "already has"):
            self.launch(request_id="request-two")

    def test_concurrent_requests_allocate_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.launch(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.marker.read_text(), "x")

    def test_invalid_response_blocks_retry_and_registration(self):
        for mode in ("invalid", "mismatch", "error", "overflow"):
            with self.subTest(mode=mode):
                self.state = self.root / mode
                with self.assertRaises((ValueError, ExecutionDispatchAdapterError)):
                    self.launch(mode)
                self.assertFalse(self.catalog.exists())
                self.assertEqual(
                    adapter.read_private(
                        adapter.operation_path(self.state, "test-worker")
                    )["status"],
                    "uncertain",
                )
                with self.assertRaisesRegex(
                    ExecutionDispatchAdapterError, "already attempted"
                ):
                    self.launch(mode)
        self.assertEqual(self.marker.read_text(), "xxxx")

    def test_timeout_leaves_uncertain_request(self):
        adapter.configure(self.config, replace(self.settings, timeout_seconds=1))
        with self.assertRaisesRegex(ExecutionDispatchAdapterError, "process limits"):
            self.launch("timeout")
        with self.assertRaisesRegex(ExecutionDispatchAdapterError, "already attempted"):
            self.launch("timeout")
        self.assertEqual(self.marker.read_text(), "x")

    def test_registration_failure_recovers_without_allocation(self):
        with patch.object(
            adapter, "register", side_effect=OSError("catalog unavailable")
        ):
            with self.assertRaises(OSError):
                self.launch()
        self.assertEqual(
            adapter.recover(self.state, "test-worker")["status"], "registered"
        )
        self.assertEqual(self.marker.read_text(), "x")
        self.assertEqual(len(read_registry(self.catalog).workers), 1)

    def test_credentials_are_bound_and_help_is_redacted(self):
        adapter.configure(
            self.config,
            replace(
                self.settings,
                environment=(
                    ExecutionWorkerEnvironment("BOUND_SECRET", "SOURCE_SECRET"),
                ),
                help_argument="help",
            ),
        )
        help_text = adapter.discover_help(
            self.config, dict(os.environ, SOURCE_SECRET="secret-value")
        )
        self.assertNotIn("secret-value", help_text)
        self.launch("env")
        self.assertNotIn("secret-value", self.config.read_text())

    def test_static_registration_prevents_allocation(self):
        self.launch()
        self.state = self.root / "another-state"
        with self.assertRaisesRegex(
            ExecutionDispatchAdapterError, "already registered"
        ):
            self.launch()
        self.assertEqual(self.marker.read_text(), "x")

    def test_public_cli_configuration_is_disabled_until_enabled(self):
        from literate_ai.cli import worker_provisioning as cli

        paths = UserPaths(self.root / "config", self.root / "cli-state")

        def call(*args):
            output = io.StringIO()
            with patch.object(cli, "resolve_user_paths", return_value=paths):
                status = main(["worker", *args, "--json"], stdout=output, stderr=output)
            return status, json.loads(output.getvalue())

        status, value = call("provisioner", "configure", "--file", str(self.config))
        self.assertEqual(status, 0, value)
        self.assertFalse(value["result"]["configuration"]["enabled"])
        self.assertNotEqual(call("provisioner", "command-help")[0], 0)
        self.assertEqual(call("provisioner", "enable")[0], 0)
        self.assertEqual(call("provisioner", "command-help")[0], 0)
        status, value = call(
            "provision",
            "test-worker",
            "--request-id",
            "request-one",
            "--worker-config",
            str(self.catalog),
        )
        self.assertEqual(status, 0, value)
        self.assertEqual(call("provisioner", "status", "test-worker")[0], 0)
        self.assertEqual(call("provisioner", "disable")[0], 0)
        self.assertEqual(call("provisioner", "recover", "test-worker")[0], 0)

    def test_configuration_drift_retains_validated_response(self):
        original = adapter.invoke

        def drift(config, environment, **kwargs):
            result = original(config, environment, **kwargs)
            if not kwargs.get("help_mode"):
                adapter.configure(self.config, enabled=False)
            return result

        with patch.object(adapter, "invoke", side_effect=drift):
            with self.assertRaisesRegex(ExecutionDispatchAdapterError, "changed"):
                self.launch()
        self.assertFalse(self.catalog.exists())
        self.assertEqual(
            adapter.recover(self.state, "test-worker")["status"], "registered"
        )
        self.assertEqual(self.marker.read_text(), "x")

    def test_unknown_config_fields_and_invalid_command_are_rejected(self):
        from literate_ai.contracts import ContractValidationError

        data = self.settings.to_dict()
        data["provider"] = "vendor-specific"
        with self.assertRaises(ContractValidationError):
            WorkerProvisioner.from_dict(data)
        for command in ((), ("bad\0command",)):
            with self.assertRaises(ContractValidationError):
                WorkerProvisioner(command)

    def test_published_schemas_admit_real_wire_values(self):
        from jsonschema import Draft202012Validator, ValidationError
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        from tests.unit.test_schema_catalog import SchemaCatalog

        resources = SchemaCatalog().resources
        registry = Registry().with_resources(
            (uri, Resource.from_contents(value, default_specification=DRAFT202012))
            for uri, value in resources.items()
        )
        validator = Draft202012Validator(
            {"$ref": "urn:literate-ai:schema:v1:worker-provisioning-contracts"},
            registry=registry,
        )
        record = self.launch()
        for document in (
            self.settings.to_dict(),
            record["request"],
            record["response"],
        ):
            with self.subTest(schema=document["schema"]):
                validator.validate(document)
                with self.assertRaises(ValidationError):
                    validator.validate({**document, "unknown": True})
        invalid = self.settings.to_dict()
        invalid["command"] = ["bad\0command"]
        with self.assertRaises(ValidationError):
            validator.validate(invalid)

        invalid_response = {
            **record["response"],
            "worker": ExecutionWorker(
                "test-worker", ExecutionWorkerKind.LOCAL
            ).to_dict(),
        }
        with self.assertRaises(ExecutionDispatchAdapterError):
            adapter.validate_response(invalid_response, record["request"])
        with self.assertRaises(ValidationError):
            validator.validate(invalid_response)

    def test_removed_registration_requires_explicit_recovery(self):
        self.launch()
        self.catalog.unlink()
        with self.assertRaisesRegex(
            ExecutionDispatchAdapterError, "Registration changed"
        ):
            self.launch()
        self.assertEqual(
            adapter.recover(self.state, "test-worker")["status"], "registered"
        )
        self.assertEqual(self.marker.read_text(), "x")

    def test_disable_during_help_prevents_allocation(self):
        original = adapter.invoke

        def disable(config, environment, **kwargs):
            result = original(config, environment, **kwargs)
            if kwargs.get("help_mode"):
                adapter.configure(self.config, enabled=False)
            return result

        with patch.object(adapter, "invoke", side_effect=disable):
            with self.assertRaisesRegex(ExecutionDispatchAdapterError, "changed"):
                self.launch()
        self.assertFalse(self.marker.exists())
        self.assertFalse(adapter.operation_path(self.state, "test-worker").exists())

    def test_recovery_revalidates_saved_response(self):
        record = self.launch()
        record["response"]["worker"]["requirements"]["os_family"] = "windows"
        path = adapter.operation_path(self.state, "test-worker")
        adapter.write_private(path, record)
        self.catalog.unlink()
        with self.assertRaisesRegex(ExecutionDispatchAdapterError, "differs"):
            adapter.recover(self.state, "test-worker")
        self.assertFalse(self.catalog.exists())
        self.assertEqual(self.marker.read_text(), "x")

    def test_malformed_record_is_a_contract_error(self):
        from literate_ai.contracts import ContractValidationError

        self.launch()
        path = adapter.operation_path(self.state, "test-worker")
        adapter.write_private(path, {"status": "ready"})
        with self.assertRaises(ContractValidationError):
            adapter.recover(self.state, "test-worker")
