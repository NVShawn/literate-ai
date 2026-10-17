"""Invariants: a reference receiver never emits a result after its tools changed,
attests only the exact capability it measured around an action, and its cached
dependency facts never live where an action can write and are reused only for
the same inspector and image bytes.

Reads between boundaries check only executable metadata, so the receiver must
fully re-measure its tools before any response leaves the process. Cached facts
decide what a tool's dependency closure contains.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests.support.fixtures_test_action_generate_execution as generate_fixture
from literate_ai import action_worker, standard_receiver
from literate_ai.adapters.action_capabilities import (
    CAPABILITY_REQUEST_SCHEMA,
    decode_capability_response,
    receiver_code_identity,
)
from literate_ai.adapters.action_dispatch_wire import (
    decode_attested_action_response,
    encode_action_request,
)
from literate_ai.adapters.dependencies import observation
from literate_ai.adapters.standard_receiver_config import (
    StandardReceiverConfigError,
    load_standard_receiver_config,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from tests.support.fixtures_test_action_generate_action import make_generate_request


@unittest.skipIf(os.name == "nt" or shutil.which("make") is None, "POSIX make")
class StandardReceiverCustodyTests(unittest.TestCase):
    def test_response_is_withheld_when_a_tool_changes_during_the_action(self):
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                make = root / "make"
                make.write_text(f'#!/bin/sh\nexec {shutil.which("make")} "$@"\n')
                make.chmod(0o755)
                config = root / "receiver.json"
                config.write_text(
                    json.dumps(
                        {
                            "schema": "literate-ai/standard-receiver@1",
                            "cas": str(root / "cas"),
                            "workspace": str(root / "workspace"),
                            "phases": ["BUILD"],
                            "tools": {"python": [sys.executable], "make": [str(make)]},
                            "child_environment": {"PATH": os.environ["PATH"]},
                            "contract_policy": "portable-starter@1",
                        }
                    )
                )

                def action(_argv, make=make, changed=changed, **_workers):
                    sys.stdout.buffer.write(b"result")
                    if changed:
                        make.write_text(make.read_text() + "# replaced\n")
                    return 0

                stdout = io.TextIOWrapper(io.BytesIO())
                with (
                    patch("literate_ai.action_worker.main", action),
                    patch.object(sys, "stdout", stdout),
                    patch.object(sys, "stderr", io.StringIO()),
                ):
                    status = standard_receiver.main(["--config", str(config)])
                stdout.flush()
                self.assertEqual(status, 2 if changed else 0)
                self.assertEqual(
                    stdout.buffer.getvalue(), b"" if changed else b"result"
                )

    def test_dependency_cache_must_be_outside_action_storage(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for cache, refused in (
                (root / "workspace" / "facts.json", True),
                (root / "cas" / "facts.json", True),
                (root / "private" / "facts.json", False),
            ):
                with self.subTest(cache=cache.parent.name):
                    config = root / "receiver.json"
                    config.write_text(
                        json.dumps(
                            {
                                "schema": "literate-ai/standard-receiver@1",
                                "cas": str(root / "cas"),
                                "workspace": str(root / "workspace"),
                                "phases": ["PACKAGE"],
                                "tools": {
                                    "python": [sys.executable],
                                    "make": ["/usr/bin/make"],
                                },
                                "child_environment": {},
                                "contract_policy": "portable-starter@1",
                                "dependency_cache": str(cache),
                            }
                        )
                    )
                    if refused:
                        with self.assertRaises(StandardReceiverConfigError):
                            load_standard_receiver_config(config)
                    else:
                        loaded = load_standard_receiver_config(config)
                        self.assertEqual(loaded.dependency_cache, cache)

    def test_private_inputs_stay_outside_action_storage(self):
        """The oracle and caches cannot sit in, or link into, action storage."""

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root / "workspace").mkdir()
            (root / "private").mkdir()
            linked = root / "linked"
            try:
                linked.symlink_to(root / "workspace", target_is_directory=True)
            except OSError:
                self.skipTest("symlinks are unavailable")
            base = {
                "schema": "literate-ai/standard-receiver@1",
                "cas": str(root / "cas"),
                "workspace": str(root / "workspace"),
                "phases": ["PACKAGE"],
                "tools": {"python": [sys.executable], "make": ["/usr/bin/make"]},
                "child_environment": {},
                "contract_policy": "portable-starter@1",
            }
            private = str(root / "private" / "file.json")
            config = root / "receiver.json"
            for name, extra in (
                ("linked-cache", {"dependency_cache": str(linked / "facts.json")}),
                (
                    "oracle",
                    {
                        "finalize": {
                            "grant_path": private,
                            "oracle": {
                                "component": "root",
                                "path": str(root / "workspace" / "cases.json"),
                            },
                        }
                    },
                ),
                (
                    "linked-grant",
                    {
                        "finalize": {
                            "grant_path": str(linked / "grant.json"),
                            "oracle": {"component": "root", "path": private},
                        }
                    },
                ),
            ):
                with self.subTest(name), self.assertRaises(StandardReceiverConfigError):
                    config.write_text(json.dumps({**base, **extra}))
                    load_standard_receiver_config(config)
            # Storage options come only from the config, even abbreviated.
            config.write_text(json.dumps(base))
            for flag in ("--workspace", "--works", f"--cas={root}", "--allow-http"):
                with self.subTest(flag=flag):
                    self.assertEqual(
                        standard_receiver.main(["--config", str(config), flag]), 2
                    )


class CapabilityAttestationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = generate_fixture.GenerateExecutionTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.request, self.records = make_generate_request(f.value, f.deadline)

    def receive(self, wire, *, describe=False):
        f = self.fixture
        output = io.BytesIO()
        with (
            patch(
                "literate_ai.action_worker.sys.stdin",
                SimpleNamespace(buffer=io.BytesIO(wire)),
            ),
            patch(
                "literate_ai.action_worker.sys.stdout", SimpleNamespace(buffer=output)
            ),
            patch.dict(
                os.environ,
                {
                    "LITAI_ACTION_WORKER_IDENTITY": (
                        self.request.worker.worker_identity.uri
                    )
                },
            ),
        ):
            arguments = ["--cas", str(f.cas.root), "--workspace", str(f.jobs)]
            status = action_worker.main(
                [*arguments, "--describe"] if describe else arguments
            )
        self.assertEqual(status, 0)
        return output.getvalue()

    def test_receiver_attests_only_the_capability_it_measured(self):
        probe = canonical_json_bytes(
            {
                "schema": CAPABILITY_REQUEST_SCHEMA,
                "worker_identity": self.request.worker.worker_identity.uri,
                "nonce": secrets.token_hex(16),
                "deadline": self.fixture.deadline.to_dict(),
            }
        )
        measured = decode_capability_response(
            self.receive(probe, describe=True),
            probe,
            receiver_code_identity(deadline=self.fixture.deadline),
        ).capability_identity
        for expected, attested, failure in (
            (measured, measured, "action_generate.not_configured"),
            (canonical_identity("admitted elsewhere"), None, "runtime_changed"),
        ):
            with self.subTest(attested=attested is not None):
                wire = encode_action_request(
                    self.request,
                    self.fixture.deadline,
                    self.records,
                    capability=expected,
                )
                outcome, _, capability = decode_attested_action_response(
                    self.receive(wire), self.request
                )
                self.assertEqual(capability, attested)
                self.assertIn(failure, outcome.failure_code)


class CustodyFailureCodeTests(unittest.TestCase):
    def test_refusal_names_the_failure_class_but_never_its_message(self):
        from literate_ai.adapters.action_observation_failure import (
            encode_observation_failure,
            observation_failure_message,
        )
        from literate_ai.storage.cas import StorageError

        secret = "/private/secret-path"
        wrapped = StorageError(secret)
        wrapped.__cause__ = PermissionError(13, secret)
        for exc, suffix in (
            (StorageError(secret), "storage"),
            (wrapped, "storage.os.eacces"),
            (FileNotFoundError(2, secret), "os.enoent"),
            (OSError(secret), "os"),
            (ValueError(secret), "value"),
            (RuntimeError(secret), "runtime"),
        ):
            with self.subTest(suffix=suffix):
                code = action_worker._custody_failure_code(exc)
                self.assertEqual(
                    code, f"action_capability.custody_unavailable.{suffix}"
                )
                message = observation_failure_message(encode_observation_failure(code))
                self.assertIn(code, message)
                self.assertNotIn(secret, message)


class DependencyFactCacheTests(unittest.TestCase):
    def setUp(self):
        for table in (observation._INSPECTION_FACTS, observation._PERSISTED):
            patcher = patch.dict(table, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cache = self.root / "private" / "facts.json"
        self.cache.parent.mkdir()

    def reload(self):
        observation._save_persistent_dependency_facts()
        observation._INSPECTION_FACTS.clear()
        observation.use_persistent_dependency_facts(self.cache)

    def test_elf_facts_follow_inspector_and_image_bytes_across_processes(self):
        readelf = self.root / "readelf"
        readelf.write_bytes(b"readelf one")
        image = self.root / "libexample.so"
        image.write_bytes(b"\x7fELF one")
        outputs = {
            "-h": "  Class: ELF64\n  Machine: AArch64\n",
            "-d": " 0x1 (NEEDED) Shared library: [libc.so.6]\n",
            "-l": "[Requesting program interpreter: /lib/ld-linux-aarch64.so.1]\n",
            "-n": "    Build ID: ABC123\n",
        }
        runs = []

        def run(_tool, arguments, *, code):
            runs.append(arguments[0])
            return outputs[arguments[0]]

        def inspect(tool=readelf):
            return observation._inspect_elf(
                tool, image, readelf_digest=observation._file_digest(tool)
            )

        with patch.object(observation, "_run_bounded_tool", run):
            observation.use_persistent_dependency_facts(self.cache)
            first = inspect()
            self.assertEqual(len(runs), 4)
            self.assertEqual(first.exact_identity, "build-id:abc123")
            self.reload()
            self.assertEqual(inspect(), first)
            self.assertEqual(len(runs), 4)
            image.write_bytes(b"\x7fELF two")
            inspect()
            self.assertEqual(len(runs), 8)
            readelf.write_bytes(b"readelf two")
            inspect()
            self.assertEqual(len(runs), 12)

    def test_pe_imports_follow_inspector_and_image_bytes_across_processes(self):
        tool = self.root / "llvm-readobj"
        tool.write_bytes(b"readobj")
        image = self.root / "example.dll"
        image.write_bytes(
            b"MZ" + bytes(0x3A) + (0x40).to_bytes(4, "little") + b"PE\0\0"
        )
        inspector = observation._PeInspector(
            tool, "llvm-readobj", "1", "sha256:0", observation._file_digest(tool)
        )
        runs = []

        def imports(_inspector, _path):
            runs.append(_path)
            return ()

        def closure():
            return observation._pe_closure(
                inspector, {str(image): {"runtime"}}, (), api_set_schema=None
            )

        with patch.object(observation, "_inspect_pe_imports", imports):
            observation.use_persistent_dependency_facts(self.cache)
            first = closure()
            self.reload()
            self.assertEqual(closure(), first)
            self.assertEqual(len(runs), 1)
            image.write_bytes(image.read_bytes() + b"changed")
            closure()
            self.assertEqual(len(runs), 2)


if __name__ == "__main__":
    unittest.main()
