"""Real child-process and public CLI checks for optional dynamic workers."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
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

    def test_register_and_replay_allocate_once(self):
        result = self.launch()
        self.assertEqual(result["status"], "registered")
        self.assertEqual(self.launch(), result)
        self.assertEqual(self.marker.read_text(), "x")
        self.assertEqual(len(read_registry(self.catalog).workers), 1)
        with self.assertRaisesRegex(ExecutionDispatchAdapterError, "already has"):
            self.launch(request_id="request-two")

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
