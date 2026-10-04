"""Public Cargo provisioning uses real TLS delivery and the archive verifier.

Provider discovery uses the existing qualification fixture; TLS, archive bytes,
reviewed files, validation and package publication run through real adapters.
"""

import json
import unittest
from unittest.mock import patch

from tests.support import fixtures_test_cli_retained_cargo as cli_fixture
from tests.support import fixtures_test_evidence_storage as tls_fixture


class RetainedCargoHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tls_fixture.HttpsEvidenceStoreTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        tls_fixture.HttpsEvidenceStoreTests.tearDownClass()

    def setUp(self):
        self.tls = tls_fixture.HttpsEvidenceStoreTests()
        self.tls.setUp()
        self.cli = cli_fixture.RetainedCargoCliTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        self.enterContext(
            patch(
                "literate_ai.adapters.evidence_storage.ssl.create_default_context",
                return_value=self.tls.client_context,
            )
        )
        self.arguments = list(self.cli.arguments)
        index = self.arguments.index("--archive")
        self.arguments[index : index + 2] = ["--archive-https", self.tls.endpoint]
        self.reference = self.cli.fixture.fixture.binding.qualification_archive
        self.path = (
            f"/evidence/blobs/sha256/{self.reference.digest[:2]}/"
            f"{self.reference.digest}"
        )
        self.state = self.tls.server.state
        self.state["objects"][self.path] = (
            self.cli.archive.read_bytes(),
            self.reference.media_type,
        )
        # A remote failure must not use the otherwise valid local artifact.
        self.before = self.cli.snapshot()

    def invoke(self, operation="check", extra=()):
        return self.cli.invoke(operation, [*self.arguments, *extra])

    def assert_refused(self, response):
        status, output, error = response
        self.assertEqual(status, 2, error)
        self.assertIn("retained.cargo.refused", error + output)
        for private in (self.tls.endpoint, str(self.cli.root), "test-secret-canary"):
            self.assertNotIn(private, error + output)

        # Materialization reserves its normal project lock before delivery. Only
        # that operational metadata may be created by a refused invocation.
        def authored(files):
            return {
                name: content
                for name, content in files.items()
                if not name.startswith(".litai-locks/")
            }

        self.assertEqual(authored(self.before), authored(self.cli.snapshot()))
        self.assertTrue(all(not p.exists() for p in self.cli.fixture.destinations))

    def test_real_tls_check_then_materialize_and_reuse(self):
        with patch.dict(
            "os.environ", {"LITAI_TEST_ARTIFACT_TOKEN": "test-secret-canary"}
        ):
            for operation in ("check", "materialize", "check", "materialize"):
                response = self.invoke(
                    operation, ("--archive-token-env", "LITAI_TEST_ARTIFACT_TOKEN")
                )
                status, output, error = response
                self.assertEqual((status, error), (0, ""), error)
                result = json.loads(output)["result"]
                self.assertFalse(result["importer_admission"])
                self.assertFalse(result["consumer_gates_executed"])
                self.assertFalse(result["source_retirement"])
                if len(self.state["requests"]) == 1:
                    self.assertEqual(self.before, self.cli.snapshot())
                    self.assertGreater(result["missing_packages"], 0)
                else:
                    self.assertEqual(result["missing_packages"], 0)
        self.assertEqual(len(self.state["requests"]), 4)
        for method, path, headers in self.state["requests"]:
            self.assertEqual((method, path), ("GET", self.path))
            self.assertEqual(headers["Authorization"], "Bearer test-secret-canary")

    def test_local_file_rejects_https_credentials(self):
        self.assert_refused(
            self.cli.invoke(
                "check", [*self.cli.arguments, "--archive-token-env", "ANY_TOKEN"]
            )
        )
        self.cli.provider_reader.assert_not_called()
        self.assertEqual(self.state["requests"], [])

    def test_redirect_and_wrong_bytes_never_publish_or_fall_back(self):
        for mode in ("redirect", "corrupt", "truncated", "size", "media", "encoding"):
            with self.subTest(mode=mode):
                self.state["mode"] = mode
                self.assert_refused(self.invoke("materialize"))
        self.assertEqual(len(self.state["requests"]), 6)
        self.assertTrue(
            all(request[1] == self.path for request in self.state["requests"])
        )
