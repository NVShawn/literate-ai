"""Detect-first tests for LitAI-managed host tools."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.tool_bootstrap import (
    managed_npm_command,
    provision_node,
)


class ToolBootstrapTests(unittest.TestCase):
    def test_provisions_checksum_pinned_node_transport_without_system_npm(self) -> None:
        payload = b"#!/bin/sh\n"
        archive_stream = io.BytesIO()
        with tarfile.open(fileobj=archive_stream, mode="w:gz") as bundle:
            paths = (
                (
                    "node-test/node.exe",
                    "node-test/npm.cmd",
                    "node-test/node_modules/npm/bin/npm-cli.js",
                )
                if os.name == "nt"
                else (
                    "node-test/bin/node",
                    "node-test/bin/npm",
                    "node-test/lib/node_modules/npm/bin/npm-cli.js",
                )
            )
            for path in paths:
                member = tarfile.TarInfo(path)
                member.size = len(payload)
                member.mode = 0o755
                bundle.addfile(member, io.BytesIO(payload))
        archive = archive_stream.getvalue()
        digest = hashlib.sha256(archive).hexdigest()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            with (
                mock.patch(
                    "literate_ai.adapters.tool_bootstrap._host_node_distribution",
                    return_value=("node-test.tar.gz", digest, "node-test"),
                ),
                mock.patch(
                    "literate_ai.adapters.tool_bootstrap.urllib.request.urlopen",
                    return_value=io.BytesIO(archive),
                ) as download,
                mock.patch(
                    "literate_ai.adapters.tool_bootstrap.ssl.create_default_context",
                    return_value="pinned-ca-context",
                ) as tls_context,
                mock.patch(
                    "literate_ai.adapters.tool_bootstrap.certifi.where",
                    return_value="/verified/cacert.pem",
                ),
            ):
                result = provision_node(root=root)
                command = managed_npm_command(root)

            tls_context.assert_called_once_with(cafile="/verified/cacert.pem")
            self.assertEqual(download.call_args.kwargs["context"], "pinned-ca-context")
            self.assertEqual(result["state"], "installed-managed")
            self.assertEqual(result["command"], str(command))
            self.assertEqual(command.read_bytes(), payload)
            self.assertTrue(os.access(command, os.X_OK))
