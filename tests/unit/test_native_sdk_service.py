"""A real packaged SDK service is accepted, stopped and revoked through Standard."""

import json
import textwrap
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai.adapters.component_acceptance import (
    SERVICE_SCHEMA,
    load_persistent_service_acceptance,
)
from literate_ai.adapters.lifecycle import LocalStandardLifecycleError, standard_local
from literate_ai.adapters.qualification_capture import QualificationEvidenceReader
from literate_ai.application.standard_project_lifecycle import StandardProjectBuildPlan
from literate_ai.contracts.identity import canonical_identity
from literate_ai.security import AuthorizationError, BuildAuthorization
from tests.unit import test_native_sdk_packaged_execution


def service_program(results):
    return f"results={results!r}\n" + textwrap.dedent("""\
        import json,os,signal,sys,vendor_math
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from pathlib import Path
        if sys.argv[1]=='--litai-test':
            assert vendor_math.scale(1.5,3)==4.5
            print(json.dumps(results))
            raise SystemExit(0)
        assert sys.argv[1]=='--litai-serve',sys.argv
        port,marker=int(sys.argv[2]),Path(sys.argv[3])
        assert os.environ['SERVICE_BASE_URL']==f'http://127.0.0.1:{port}'
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body=(b'ready' if self.path=='/ready' else
                      json.dumps({'value':vendor_math.scale(1.5,3)}).encode())
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            def log_message(self,*args): pass
        def stop(*args):
            marker.write_text(json.dumps({'pid':os.getpid(),'port':port,
                                         'sdk_exists':Path(vendor_math.__file__).is_file()}))
            raise SystemExit(0)
        signal.signal(signal.SIGTERM,stop)
        if hasattr(signal,'SIGBREAK'): signal.signal(signal.SIGBREAK,stop)
        with ThreadingHTTPServer(('127.0.0.1',port),Handler) as server:
            server.serve_forever()
    """)


class NativeSdkServiceTests(unittest.TestCase):
    def test_native_http_acceptance_stops_on_success_failure_and_revocation(self):
        helper = test_native_sdk_packaged_execution.NativeSdkPackagedExecutionTests()
        self.addCleanup(helper.doCleanups)
        fixture, plan, custody, resources, recorder, arguments = (
            helper.prepare_relocated_package(service_program, "persistent-service")
        )
        package_plan, package_result = arguments[-2:]
        self.assertEqual(package_plan.entrypoints[0].kind, "persistent-service")
        root = fixture.fixture.fixture.root
        marker = root / "service-stopped.json"
        oracle_path = root / "service-acceptance.json"
        lock = arguments[0]
        node = next(
            item for item in lock.nodes if item.revision.identity == lock.root_revision
        )
        oracle_path.write_text(
            json.dumps(
                {
                    "schema": SERVICE_SCHEMA,
                    "specification_set_identity": (
                        node.revision.specification_set_identity.uri
                    ),
                    "process": {
                        "arguments": ["{port}", str(marker)],
                        "environment": {"SERVICE_BASE_URL": "{base_url}"},
                        # The real SDK/package build can leave a hosted runner
                        # CPU-starved; keep the production refusal bounded while
                        # allowing the packaged interpreter time to begin serving.
                        # The process deadline must outlive startup plus requests.
                        "startup_timeout_seconds": 120,
                        "timeout_seconds": 180,
                    },
                    "readiness": {
                        "path": "/ready",
                        "expected_text_contains": ["ready"],
                    },
                    "requests": [{"path": "/value", "expected_json": {"value": 4.5}}],
                }
            )
        )
        oracle = load_persistent_service_acceptance(
            oracle_path, node.revision.coordinate.name
        )
        fixture.ports.independent_acceptance_oracle = oracle
        tested = fixture.ports.test_root_integration(*arguments)
        deferred = fixture.ports.execute_packaged_project(*arguments)
        grants = []

        def authorize(**kwargs):
            value = BuildAuthorization(**kwargs)
            grants.append(value)
            return value

        def accept():
            return fixture.ports.accept_project_independently(
                *arguments, tested, deferred
            )

        original_probe = fixture.ports._observe_service_request

        def revoke_after_request(probe, base_url, observations, **options):
            original_probe(probe, base_url, observations, **options)
            if probe.path == "/value":
                fixture.fixture.revocations = fixture.fixture.revocations.revoke(
                    grants[-1].authorization_id,
                    actor="fixture operator",
                    reason="stop SDK service",
                )

        with (
            patch.object(standard_local, "BuildAuthorization", side_effect=authorize),
            patch.object(
                fixture.ports,
                "_stop_service_process",
                wraps=fixture.ports._stop_service_process,
            ) as stop,
        ):
            accepted = accept()
            self.assertTrue(marker.is_file())
            self.assertTrue(json.loads(marker.read_text())["sdk_exists"])
            reader = QualificationEvidenceReader(
                recorder.entries, max_bytes=256 * 1024 * 1024, max_records=2000
            )
            record = reader.read_json(accepted)
            self.assertEqual(
                record["schema"], "literate-ai/local-persistent-service-acceptance@2"
            )
            self.assertEqual(len(record["observations"]), 2)
            project = StandardProjectBuildPlan(
                canonical_identity("fixture execution plan"), (plan,)
            )
            recorder.remember_json(project.identity_document())
            helper.check_retained(
                recorder, project, package_plan, package_result, "execute", accepted
            )
            invalid = replace(oracle.requests[0], expected_json={"value": float("nan")})
            fixture.ports.independent_acceptance_oracle = replace(
                oracle, requests=(invalid,)
            )
            issued = len(grants)
            with self.assertRaises(ValueError):
                accept()
            self.assertEqual(
                len(grants), issued, "invalid oracle obtained a launch grant"
            )
            self.assertEqual(stop.call_count, 1)
            marker.unlink()
            wrong = replace(oracle.requests[0], expected_json={"value": 9.25})
            fixture.ports.independent_acceptance_oracle = replace(
                oracle, requests=(wrong,)
            )
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "JSON response differed"
            ):
                accept()
            self.assertTrue(marker.is_file())
            self.assertTrue(json.loads(marker.read_text())["sdk_exists"])
            marker.unlink()
            fixture.ports.independent_acceptance_oracle = oracle
            with patch.object(
                fixture.ports,
                "_observe_service_request",
                side_effect=revoke_after_request,
            ):
                with self.assertRaisesRegex(
                    AuthorizationError, "authorization_revoked"
                ):
                    accept()
            self.assertTrue(marker.is_file())
            self.assertTrue(json.loads(marker.read_text())["sdk_exists"])
            self.assertEqual(stop.call_count, 3)
            for call in stop.call_args_list:
                self.assertIsNotNone(
                    call.args[0].poll(), "service process survived acceptance"
                )
        resources.verify_materialized(custody.root)
        self.assertEqual(
            list(fixture.ports.object_root.iterdir()), [custody.root.parent]
        )
