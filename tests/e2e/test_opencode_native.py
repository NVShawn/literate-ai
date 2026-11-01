"""Opt-in native CLI qualification against a loopback model, without paid inference."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from literate_ai.adapters.models import coding_cli as cli


class ModelEndpoint(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        tools = request.get("tools", [])
        blocked_path = getattr(self.server, "blocked_path", None)
        tool_name = "read" if blocked_path else "write"
        tool = next(
            (tool for tool in tools if tool["function"]["name"] == tool_name), None
        )
        results = [
            message.get("content")
            for message in request["messages"]
            if message.get("role") == "tool"
        ]
        if results:
            self.server.tool_results.extend(results)
        if tool and not results:
            properties = tool["function"]["parameters"].get("properties", {})
            path_key = "path" if "path" in properties else "filePath"
            arguments = {path_key: blocked_path or ".literate-ai-task-response.json"}
            if not blocked_path:
                arguments["content"] = '{"ok":true}'
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_fixture",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "Done"}
            finish = "stop"
        base = {"id": "chatcmpl-local", "created": 1, "model": request["model"]}
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        self.send_response(200)
        if request.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for choice, extra in (
                ({"delta": delta, "finish_reason": None}, {}),
                ({"delta": {}, "finish_reason": finish}, {"usage": usage}),
            ):
                chunk = {
                    **base,
                    "object": "chat.completion.chunk",
                    **extra,
                    "choices": [{"index": 0, **choice}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        **base,
                        "object": "chat.completion",
                        "usage": usage,
                        "choices": [
                            {"index": 0, "message": delta, "finish_reason": finish}
                        ],
                    }
                ).encode()
            )


class OpenCodeNativeTests(unittest.TestCase):
    def qualify(self, interface: str, variable: str):
        configured = os.environ.get(variable)
        if not configured:
            self.skipTest(f"set {variable} to an exact native OpenCode binary")
        executable = str(Path(configured).resolve(strict=True))
        server = ThreadingHTTPServer(("127.0.0.1", 0), ModelEndpoint)
        server.tool_results = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                endpoint = f"http://127.0.0.1:{server.server_port}/v1"
                model = {"name": "Test", "limit": {"context": 32000, "output": 2048}}
                provider = {"name": "Local qualification"}
                if interface == "v1":
                    constant = "_OPENCODE_CONFIG_CONTENT"
                    config = json.loads(cli._OPENCODE_CONFIG_CONTENT)
                    config["provider"] = {
                        "litai-local": {
                            **provider,
                            "npm": "@ai-sdk/openai-compatible",
                            "options": {"baseURL": endpoint, "apiKey": "local-test"},
                            "models": {"test-model": {**model, "tool_call": True}},
                        }
                    }
                else:
                    constant = "_OPENCODE_V2_CONFIG_CONTENT"
                    config = json.loads(cli._OPENCODE_V2_CONFIG_CONTENT)
                    config["providers"] = {
                        "litai-local": {
                            **provider,
                            "package": "aisdk:@ai-sdk/openai-compatible",
                            "settings": {"baseURL": endpoint, "apiKey": "local-test"},
                            "models": {
                                "test-model": {**model, "capabilities": {"tools": True}}
                            },
                        }
                    }
                environment = {
                    "CODING_CLI": "opencode",
                    "PATH": os.environ["PATH"],
                    "HOME": str(root),
                    "USERPROFILE": str(root),
                    "TMPDIR": str(root),
                    # Keep Windows data and credentials private as well.
                    "LOCALAPPDATA": str(root / "local"),
                    **{
                        key: os.environ[key]
                        for key in ("SystemRoot", "ProgramData", "TEMP", "TMP")
                        if key in os.environ
                    },
                }
                selection = cli._pin_coding_cli("opencode", executable)
                with cli._coding_cli_process_environment(
                    environment, selection, workspace=root
                ) as probe:
                    self.assertEqual(
                        cli._require_coding_cli_compatible(
                            selection, workspace=root, environment=probe
                        ),
                        interface,
                    )
                    if interface == "v2":
                        # First qualify the unmodified production isolation document.
                        cli._configure_opencode_interface(
                            probe, interface, selection, root
                        )
                        # Then normalize this test-only provider declaration so the
                        # exact-source check remains active during the real task.
                        probe["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
                        normalized = cli._run_bounded(
                            [executable, "debug", "config", "--standalone"],
                            cwd=root,
                            environment=probe,
                            timeout_seconds=30,
                            maximum_stdout_bytes=262144,
                            maximum_stderr_bytes=262144,
                        )
                        self.assertEqual(normalized.returncode, 0)
                        entries = json.loads(normalized.stdout)
                        self.assertEqual(len(entries), 2)
                        config = entries[1]["info"]
                        config["mcp"] = {"servers": {}}
                with (
                    mock.patch.object(cli.shutil, "which", return_value=executable),
                    mock.patch.object(cli, constant, json.dumps(config)),
                ):
                    actual = cli.CodingCliTaskRunner(
                        environment=environment, timeout_seconds=60
                    ).run_json_task(
                        'Write {"ok":true} to the requested JSON response file.',
                        model="litai-local/test-model",
                    )
                self.assertEqual(actual.response, {"ok": True})
                self.assertEqual("--standalone" in actual.command, interface == "v2")
                # The model is untrusted: explicitly attempt an out-of-workspace read.
                outside = root / "outside.txt"
                outside.write_text("EXTERNAL-SENTINEL-MUST-NOT-REACH-MODEL")
                server.blocked_path = str(outside)
                server.tool_results.clear()
                with (
                    mock.patch.object(cli.shutil, "which", return_value=executable),
                    mock.patch.object(cli, constant, json.dumps(config)),
                ):
                    with self.assertRaises(cli.CodingCliError) as refused:
                        cli.CodingCliTaskRunner(
                            environment=environment, timeout_seconds=60
                        ).run_json_task(
                            "Try the requested read, then stop.",
                            model="litai-local/test-model",
                        )
                self.assertEqual(
                    refused.exception.code, "coding_cli.task_response_missing"
                )
                observed = json.dumps(server.tool_results)
                self.assertTrue(server.tool_results)
                self.assertNotIn("EXTERNAL-SENTINEL-MUST-NOT-REACH-MODEL", observed)
                self.assertRegex(observed.casefold(), "denied|permission|rejected")

        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_v1_native_file_writing(self):
        self.qualify("v1", "LITAI_OPENCODE_V1_BINARY")

    def test_v2_native_file_writing(self):
        self.qualify("v2", "LITAI_OPENCODE_V2_BINARY")
