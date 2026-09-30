"""Cursor hook bridge tests over the real authenticated directory protocol."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from literate_ai.adapters.models import (
    DirectoryInheritedSessionTransport,
    InheritedSessionError,
    InheritedSessionProviderAdapter,
    InheritedSessionProviderConfig,
)
from literate_ai.contracts import InheritedSessionContextBundle, canonical_identity
from literate_ai.integrations.cursor_inherited_session import (
    cursor_provider_identity,
    cursor_session_identity,
    handle_stop,
    require_live_cursor_hook,
)

SECRET = b"c" * 32


def identity(label: str):
    return canonical_identity({"cursor-inherited-session-test": label})


def hook(workspace: Path, transcript: Path, *, model: str = "test-model"):
    return {
        "conversation_id": "conversation-1",
        "generation_id": "generation-1",
        "model": model,
        "model_id": model,
        "model_params": [],
        "cursor_version": "test",
        "workspace_roots": [str(workspace)],
        "user_email": "test@example.invalid",
        "transcript_path": str(transcript),
        "status": "completed",
        "loop_count": 0,
    }


class CursorInheritedSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text('{"role":"user"}\n', encoding="utf-8")
        self.hook = hook(self.root, self.transcript)
        self.config = InheritedSessionProviderConfig(
            cursor_provider_identity(self.hook),
            cursor_session_identity(self.hook),
            "cursor-test-key",
            5,
        )
        self.environment = {
            "LITAI_CODING_PROVIDER": "inherited-session",
            "LITAI_INHERITED_SESSION_PROVIDER_IDENTITY": (
                self.config.provider_identity.uri
            ),
            "LITAI_INHERITED_SESSION_IDENTITY": self.config.session_identity.uri,
            "LITAI_INHERITED_SESSION_AUTH_KEY_ID": "cursor-test-key",
            "LITAI_INHERITED_SESSION_CHANNEL": str(self.root),
            "LITAI_INHERITED_SESSION_AUTH_KEY": SECRET.hex(),
        }

    def tearDown(self):
        self.temporary.cleanup()

    def bundle(self):
        prompt = b"Create source/main.py."
        import hashlib

        from literate_ai.contracts import ContentIdentity

        return InheritedSessionContextBundle(
            bounded_prompt=prompt,
            workspace_locator=str(self.workspace),
            context_manifest_identity=identity("context"),
            prompt_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(prompt).hexdigest()
            ),
            writable_boundary_identity=identity("workspace"),
            component_lock_identity=identity("lock"),
            recipe_identity=identity("recipe"),
            model_name="test-model",
            model_binding_identity=identity("model"),
            output_contract_identity=identity("output"),
        )

    def start_exchange(self):
        transport = DirectoryInheritedSessionTransport(self.root)
        adapter = InheritedSessionProviderAdapter(
            self.config,
            transport,
            secret_provider=lambda key_id: SECRET,
        )
        result = []
        failure = []

        def execute():
            try:
                result.append(
                    adapter.execute(
                        generation_request_identity=identity("request"),
                        generation_plan_identity=identity("plan"),
                        context_bundle=self.bundle(),
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted by callers
                failure.append(exc)

        thread = threading.Thread(target=execute)
        thread.start()
        request = transport.directory / transport.REQUEST_NAME
        for _ in range(500):
            if request.exists():
                break
            thread.join(0.002)
        self.assertTrue(request.exists())
        return transport, thread, result, failure

    def test_current_session_receives_exact_context_and_returns_bound_source(self):
        transport, thread, result, failure = self.start_exchange()
        first = handle_stop(self.hook, environment=self.environment)
        self.assertIn(self.bundle().bounded_prompt.decode(), first["followup_message"])
        self.assertIn(str(self.workspace), first["followup_message"])

        source = self.workspace / "source"
        source.mkdir()
        (source / "main.py").write_text("print('cursor')\n", encoding="utf-8")
        self.transcript.write_text(
            '{"role":"user"}\n{"role":"assistant"}\n', encoding="utf-8"
        )
        self.hook["generation_id"] = "generation-2"
        self.assertEqual(handle_stop(self.hook, environment=self.environment), {})
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failure, [])
        self.assertIsNotNone(result[0].evidence.response.transcript_identity)
        self.assertIsNotNone(result[0].evidence.response.provider_evidence_identity)
        transport.cleanup()
        self.assertFalse(transport.directory.exists())

    def test_model_workspace_and_output_scope_mismatch_fail_closed(self):
        for mutation, code in (
            (
                lambda event: event.update(model="other", model_id="other"),
                "cursor_inherited_session.model_binding_mismatch",
            ),
            (
                lambda event: event.update(workspace_roots=[str(self.root / "other")]),
                "cursor_inherited_session.workspace_mismatch",
            ),
        ):
            with self.subTest(code=code):
                transport, thread, _, _ = self.start_exchange()
                event = dict(self.hook)
                mutation(event)
                if code.endswith("workspace_mismatch"):
                    (self.root / "other").mkdir(exist_ok=True)
                with self.assertRaises(InheritedSessionError) as raised:
                    handle_stop(event, environment=self.environment)
                self.assertEqual(raised.exception.code, code)
                transport.cleanup()
                thread.join(5)

    def test_symlink_output_is_rejected(self):
        transport, thread, _, _ = self.start_exchange()
        handle_stop(self.hook, environment=self.environment)
        source = self.workspace / "source"
        source.mkdir()
        (source / "escape").symlink_to(self.transcript)
        with self.assertRaises(InheritedSessionError) as raised:
            handle_stop(self.hook, environment=self.environment)
        self.assertEqual(
            raised.exception.code,
            "cursor_inherited_session.source_link_rejected",
        )
        transport.cleanup()
        thread.join(5)

    def test_missing_cursor_stop_hook_is_current_session_unavailable(self):
        with self.assertRaises(InheritedSessionError) as raised:
            require_live_cursor_hook(environment={})
        self.assertEqual(
            raised.exception.code,
            "inherited_session.current_session_unavailable",
        )
        admitted = require_live_cursor_hook(self.hook, environment={})
        self.assertEqual(admitted["conversation_id"], "conversation-1")


if __name__ == "__main__":
    unittest.main()
