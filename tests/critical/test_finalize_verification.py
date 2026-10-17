"""Controller verifier custody is cleaned on refused proof or revoked configuration."""

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.action_finalize_verification import (
    PortableFinalizeVerification,
)
from literate_ai.contracts import canonical_identity


class FinalizeVerificationTests(unittest.TestCase):
    def test_refusal_or_configuration_change_removes_only_owned_workspace(self):
        for revoke in (False, True):
            with self.subTest(revoke=revoke), tempfile.TemporaryDirectory() as root:
                workspace = Path(root).resolve()
                preserved = workspace / "keep"
                preserved.write_text("other work")
                guard = Mock()
                verification = PortableFinalizeVerification(
                    oracle=SimpleNamespace(
                        identity=canonical_identity("oracle"), cases=Mock()
                    ),
                    workspace_root=workspace,
                    require_configuration=guard,
                )

                @contextmanager
                def materialize(**kwargs):
                    (kwargs["workspace_root"] / "proof").write_text("owned")
                    yield object()

                with (
                    patch(
                        "literate_ai.adapters.action_finalize_verification.materialize_finalize_inputs",
                        materialize,
                    ),
                    patch(
                        "literate_ai.adapters.action_finalize_verification.verify_portable_finalize_stages",
                        side_effect=ValueError("proof refused"),
                    ) as verify,
                    self.assertRaisesRegex(
                        ValueError,
                        "configuration changed" if revoke else "proof refused",
                    ),
                ):
                    with verification(admission_guard=Mock()) as check:
                        if revoke:
                            guard.side_effect = ValueError("configuration changed")
                        check(object(), object(), {})
                self.assertEqual(list(workspace.iterdir()), [preserved])
                self.assertEqual(preserved.read_text(), "other work")
                self.assertEqual(verify.call_count, 0 if revoke else 1)
