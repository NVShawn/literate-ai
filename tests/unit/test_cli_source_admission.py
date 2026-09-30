"""Public admission keeps test output out of accepted source-cache membership.

The generation and installed-distribution ports supply reviewed fixture evidence;
CLI parsing, source commands, admission, cache publication and restore are real.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import literate_ai.cli.generation as generation_cli
from literate_ai.adapters.cache import (
    FileSystemSourceCache,
    SourceCacheCandidate,
    SourceCacheMaterializer,
)
from literate_ai.adapters.standard_project import (
    GeneratedStandardProject,
    GeneratedStandardSourceAdmissionCandidate,
)
from literate_ai.cli.dispatch import main
from literate_ai.contracts import ContentIdentity, StandardProjectLifecycleDriver
from literate_ai.generated_tests import GENERATED_TEST_SUITE_PATH
from scripts import installed_source_admission_smoke as smoke
from tests.unit.test_cli_generation import bind_tree
from tests.unit.test_cli_locked_generation import _generation_fixture, _write_lock
from tests.unit.test_source_generation_custody_contracts import _generated_custody


class SourceAdmissionCliTests(unittest.TestCase):
    def exercise(self, *, use_make=False, change_source=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            component, flavors = _generation_fixture(root)
            _write_lock(component, flavors)
            build = root / "_build"
            output = build / "generated"
            cas = smoke.FileSystemCAS(build / "candidate-cas")
            generated, key, _ = smoke.create_generation(
                0,
                session="public-cli",
                orchestration=smoke.identity("orchestration"),
                cas=cas,
                source_root=output,
            )
            suite_path = output / GENERATED_TEST_SUITE_PATH
            suite = json.loads(suite_path.read_bytes())
            suite["cases"] = [{"name": "fixture"}]
            suite_path.write_bytes(smoke.canonical_json_bytes(suite))
            script = (
                "from pathlib import Path\n"
                "import sys\n"
                "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
                "import module\n"
                "assert module.VALUE == 0\n"
                "Path('test-output').mkdir(exist_ok=True)\n"
                "Path('test-output/result.txt').write_text('passed')\n"
            )
            if change_source:
                script += "Path(__file__).write_text('changed')\n"
            (output / "source" / "verify.py").write_text(script)
            (output / "source" / "Makefile").write_text(
                '.PHONY: test\ntest:\n\t"$(PYTHON)" verify.py\n'
            )
            generated = bind_tree(
                generated,
                output,
                generated_test_suite_identity=ContentIdentity.parse_uri(
                    cas.put_bytes(suite_path.read_bytes()).identity
                ),
            )
            item = GeneratedStandardSourceAdmissionCandidate(
                generated,
                key,
                output,
                smoke.identity("flavors"),
                smoke.identity("skills"),
                smoke.identity("coding-cli-transcript-0"),
            )
            selection = SimpleNamespace(
                name="codex",
                executable="fixture-codex",
                executable_identity=smoke.identity("executable").uri,
                identity=smoke.identity("selection").uri,
                tool_binding_identity=key.coding_cli_tool_binding_identity.uri,
            )
            generated_project = GeneratedStandardProject(
                selection, _generated_custody(), {}, admission_candidates=(item,)
            )
            distribution = smoke.identity("installed-distribution")
            project = SimpleNamespace(
                root=root,
                flavor_selectors_for=lambda _root, selectors: selectors,
                definition=SimpleNamespace(
                    lifecycle_driver=StandardProjectLifecycleDriver(
                        distribution, smoke.identity("policy")
                    ),
                    source_cache=None,
                ),
            )
            binding = SimpleNamespace(
                require_unchanged=lambda: None,
                distribution=SimpleNamespace(identity=distribution),
            )
            command = (
                [
                    shutil.which("make"),
                    "-C",
                    "source",
                    f"PYTHON={sys.executable}",
                    "test",
                ]
                if use_make
                else [sys.executable, "source/verify.py"]
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(
                    generation_cli, "discover_project", return_value=project
                ),
                mock.patch.object(
                    generation_cli,
                    "_require_reviewed_component_authority",
                    return_value=None,
                ),
                mock.patch.object(generation_cli, "_require_generation_project_index"),
                mock.patch.object(
                    generation_cli,
                    "resolve_cache_directories",
                    return_value=SimpleNamespace(build_dir=build),
                ),
                mock.patch.object(
                    generation_cli,
                    "resolve_standard_project_lifecycle_driver",
                    return_value=binding,
                ),
                mock.patch.object(
                    generation_cli.FilesystemStandardSourceGenerationAdapter,
                    "from_environment",
                ) as factory,
            ):
                factory.return_value.generate.return_value = generated_project
                status = main(
                    [
                        "generate",
                        str(component),
                        "--output",
                        str(output),
                        "--target",
                        "macos-host",
                        "--flavor",
                        "+macos",
                        "--flavor",
                        "+python",
                        "--flavor-root",
                        str(flavors),
                        "--admit",
                        "--source-test-command",
                        json.dumps(command),
                        "--json",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                )
            response = json.loads(stdout.getvalue() or stderr.getvalue())
            cache = FileSystemSourceCache(
                "standard-local", build / "accepted-source-cache"
            )
            entries = cache.published_entries()
            if change_source:
                self.assertNotEqual(status, 0)
                self.assertEqual(
                    response["error"]["code"], "source_admission.source_changed"
                )
                self.assertEqual(entries, ())
                return
            self.assertEqual(status, 0, response)
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(
                entry.source_tree_identity, generated.output.candidate.tree_identity
            )
            self.assertFalse(
                any(
                    "test-output" in f.path or "__pycache__" in f.path
                    for f in entry.source_files
                )
            )
            self.assertFalse((output / "test-output").exists())
            self.assertFalse((output / "source/test-output").exists())
            destination = root / "restored"
            SourceCacheMaterializer().materialize(
                SourceCacheCandidate(entry, ("standard-local",), cache), destination
            )
            self.assertEqual((destination / "source/verify.py").read_text(), script)
            self.assertFalse((destination / "test-output").exists())

    def test_public_admission_and_restore_exclude_test_outputs(self):
        self.exercise()

    @unittest.skipUnless(shutil.which("make"), "native make is unavailable")
    def test_build_make_command_admits_and_restores_original_source(self):
        self.exercise(use_make=True)

    def test_public_admission_refuses_source_edit_and_publishes_nothing(self):
        self.exercise(change_source=True)
