"""#332: JavaScript generation must reject a broken single-file bundle early."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.models import CodingCliError, CodingCliSourceGenerator
from literate_ai.adapters.models.generated_source_validation import (
    GeneratedSourceValidationError,
    validate_javascript_generation_handoff,
)
from tests.support.fixtures_test_coding_cli_generation import (
    flavor,
    recipe,
    write_generated_test_suite,
)


def _naive_bundle(*, modules: dict[str, str], entry: str, footer: str) -> str:
    """Build a single-file registry whose keys include explicit .js suffixes."""

    del entry

    parts = ["#!/usr/bin/env node\n", "const __litaiModules = {\n"]
    for module_id, body in modules.items():
        parts.append(
            f"  {json.dumps(module_id)}: function (module, exports, require) {{\n"
        )
        for line in body.splitlines():
            parts.append(f"    {line}\n")
        parts.append("  },\n")
    parts.append("};\n")
    parts.append(
        "function __litaiRequire(id) {\n"
        "  const load = __litaiModules[id];\n"
        "  if (typeof load !== 'function') {\n"
        "    throw new TypeError('__litaiModules[id] is not a function');\n"
        "  }\n"
        "  const module = {exports: {}};\n"
        "  load(module, module.exports, __litaiRequire);\n"
        "  return module.exports;\n"
        "}\n"
    )
    parts.append(footer)
    return "".join(parts)


def _passing_application() -> dict[str, str]:
    util = (
        "function twice(value) { return value + value; }\nmodule.exports = {twice};\n"
    )
    tests = (
        "function runGeneratedTests() {\n"
        "  return {\n"
        "    schema: 'literate-ai/generated-test-results@1',\n"
        "    cases: [{name: 'twice', passed: true}],\n"
        "  };\n"
        "}\n"
        "module.exports = {runGeneratedTests};\n"
    )
    main = (
        "const {twice} = require('./lib/util.js');\n"
        "const {runGeneratedTests} = require('./tests/litai_test.js');\n"
        "function dispatch(argv) {\n"
        "  const mode = argv[2];\n"
        "  if (mode === '--litai-test') {\n"
        "    process.stdout.write(JSON.stringify(runGeneratedTests()) + '\\n');\n"
        "    return;\n"
        "  }\n"
        "  if (mode === '--litai-smoke') {\n"
        "    process.stdout.write("
        "JSON.stringify({ok: true, twice: twice('ab')}) + '\\n');\n"
        "    return;\n"
        "  }\n"
        "}\n"
        "module.exports = {dispatch};\n"
        "if (require.main === module) {\n"
        "  dispatch(process.argv);\n"
        "}\n"
    )
    bundle = _naive_bundle(
        modules={
            "./lib/util.js": util,
            "./tests/litai_test.js": tests,
            "./main.js": main.replace("#!/usr/bin/env node\n", ""),
        },
        entry="./main.js",
        footer="__litaiRequire('./main.js').dispatch(process.argv);\n",
    )
    return {
        "source/main.js": "#!/usr/bin/env node\n" + main,
        "source/lib/util.js": util,
        "source/tests/litai_test.js": tests,
        "source/tools/bundle.js": (
            "const fs = require('fs');\n"
            "const collected = [];\n"
            "function collect(file) {\n"
            "  const source = fs.readFileSync(file, 'utf8');\n"
            "  const pattern = /require\\(['\"](\\.\\/[^'\"]+)['\"]\\)/g;\n"
            "  const matches = Array.from("
            "source.matchAll(pattern), (item) => item[1]);\n"
            "  collected.push(file);\n"
            "  for (const specifier of matches) collect(specifier);\n"
            "}\n"
            "collect('./main.js');\n"
        ),
        "source/build/run.js": bundle,
    }


class JavaScriptGenerationHandoffTests(unittest.TestCase):
    def test_extensionless_local_require_with_naive_module_map_is_rejected(
        self,
    ) -> None:
        files = {
            "source/main.js": (
                "const util = require('./lib/util');\nmodule.exports = util;\n"
            ),
            "source/lib/util.js": "module.exports = {ok: true};\n",
            "source/build/run.js": _naive_bundle(
                modules={
                    "./lib/util.js": "module.exports = {ok: true};\n",
                    "./main.js": (
                        "const util = require('./lib/util');\nmodule.exports = util;\n"
                    ),
                },
                entry="./main.js",
                footer="__litaiRequire('./main.js');\n",
            ),
        }

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_javascript_generation_handoff(files)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_javascript_local_specifier_extensionless",
        )
        self.assertIn("./lib/util", raised.exception.message)

    def test_explicit_js_imports_with_complete_bundle_and_dispatch_pass(self) -> None:
        validate_javascript_generation_handoff(_passing_application())

    def test_shared_global_regexp_missing_transitive_module_is_rejected(self) -> None:
        files = {
            "source/main.js": "require('./lib/mid.js');\n",
            "source/lib/mid.js": "require('./leaf.js');\n",
            "source/lib/leaf.js": "module.exports = 1;\n",
            "source/build/run.js": _naive_bundle(
                modules={
                    "./main.js": "require('./lib/mid.js');\n",
                    "./lib/mid.js": "require('./leaf.js');\n",
                },
                entry="./main.js",
                footer="__litaiRequire('./main.js');\n",
            ),
        }

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_javascript_generation_handoff(files)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_javascript_bundle_module_missing",
        )
        self.assertIn("./lib/leaf.js", raised.exception.message)

    def test_literal_module_path_loop_is_accepted_as_a_complete_registry(self) -> None:
        files = _passing_application()
        files.pop("source/build/run.js")
        files["source/tools/bundle.js"] = (
            "const modulePaths = ['main.js', 'lib/util.js', "
            "'tests/litai_test.js'];\n"
            "const __litaiModules = Object.create(null);\n"
            "for (const id of modulePaths) {\n"
            "  output += `__litaiModules[${JSON.stringify(id)}] = value`;\n"
            "}\n"
            "function __litaiRequire(id) { return __litaiModules[id](); }\n"
            "__litaiRequire('main.js').dispatch(process.argv);\n"
        )

        validate_javascript_generation_handoff(files)

    def test_literal_module_path_loop_still_rejects_an_incomplete_registry(
        self,
    ) -> None:
        files = _passing_application()
        files.pop("source/build/run.js")
        files["source/tools/bundle.js"] = (
            'const modulePaths = ["tests/litai_test.js"];\n'
            "const __litaiModules = Object.create(null);\n"
            "for (const id of modulePaths) {\n"
            "  __litaiModules[id] = function () {};\n"
            "}\n"
            "function __litaiRequire(id) { return __litaiModules[id](); }\n"
            "__litaiRequire('main.js').dispatch(process.argv);\n"
        )

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_javascript_generation_handoff(files)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_javascript_bundle_module_missing",
        )
        self.assertIn("./lib/util.js", raised.exception.message)

    def test_embedded_shebang_in_bundled_module_body_is_rejected(self) -> None:
        files = _passing_application()
        files["source/build/run.js"] = files["source/build/run.js"].replace(
            "function __litaiRequire(id) {",
            "#!/usr/bin/env node\nfunction __litaiRequire(id) {",
            1,
        )

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_javascript_generation_handoff(files)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_javascript_bundle_shebang",
        )

    def test_registry_loaded_require_main_guard_is_rejected(self) -> None:
        files = _passing_application()
        files["source/build/run.js"] = _naive_bundle(
            modules={
                "./lib/util.js": files["source/lib/util.js"],
                "./tests/litai_test.js": files["source/tests/litai_test.js"],
                "./main.js": files["source/main.js"].replace(
                    "#!/usr/bin/env node\n", ""
                ),
            },
            entry="./main.js",
            footer="__litaiRequire('./main.js');\n",
        )

        with self.assertRaises(GeneratedSourceValidationError) as raised:
            validate_javascript_generation_handoff(files)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_javascript_bundle_entrypoint_undispatched",
        )

    def test_generation_rejects_extensionless_bundle_before_index_or_cache(
        self,
    ) -> None:
        value = replace_javascript_recipe()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated"

            def execute(command, **kwargs):
                del command
                workspace = Path(kwargs["cwd"])
                source = workspace / "source"
                (source / "lib").mkdir(parents=True)
                (source / "build").mkdir()
                (source / "main.js").write_text(
                    "const util = require('./lib/util');\nmodule.exports = util;\n",
                    encoding="utf-8",
                )
                (source / "lib" / "util.js").write_text(
                    "module.exports = {ok: true};\n", encoding="utf-8"
                )
                (source / "build" / "run.js").write_text(
                    _naive_bundle(
                        modules={
                            "./lib/util.js": "module.exports = {ok: true};\n",
                            "./main.js": (
                                "const util = require('./lib/util');\n"
                                "module.exports = util;\n"
                            ),
                        },
                        entry="./main.js",
                        footer="__litaiRequire('./main.js');\n",
                    ),
                    encoding="utf-8",
                )
                write_generated_test_suite(workspace, value)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with (
                mock.patch(
                    "literate_ai.adapters.models.coding_cli.shutil.which",
                    return_value=sys.executable,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli._run_bounded",
                    side_effect=execute,
                ),
                mock.patch(
                    "literate_ai.adapters.models.coding_cli."
                    "_require_codex_linux_workspace_write_prerequisite"
                ),
            ):
                generator = CodingCliSourceGenerator(
                    environment={"CODING_CLI": "codex", "PATH": "/tools"},
                    source_intelligence_mode="off",
                )
                with self.assertRaises(CodingCliError) as raised:
                    generator.generate(value, output_root=output)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generated_javascript_local_specifier_extensionless",
        )


def replace_javascript_recipe():
    from dataclasses import replace

    return replace(recipe(flavor("javascript")), required_entrypoint="source/main.js")


class JavaScriptBundleExecutionTests(unittest.TestCase):
    def test_bundled_modes_emit_required_payloads(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required to execute the bundled self-check fixture")
        files = _passing_application()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "run.js"
            artifact.write_text(files["source/build/run.js"], encoding="utf-8")
            test_run = subprocess.run(
                (node, str(artifact), "--litai-test"),
                capture_output=True,
                check=False,
            )
            smoke_run = subprocess.run(
                (node, str(artifact), "--litai-smoke"),
                capture_output=True,
                check=False,
            )
        self.assertEqual(test_run.returncode, 0, test_run.stderr)
        self.assertEqual(smoke_run.returncode, 0, smoke_run.stderr)
        test_payload = json.loads(test_run.stdout.decode("utf-8"))
        smoke_payload = json.loads(smoke_run.stdout.decode("utf-8"))
        self.assertEqual(test_payload["schema"], "literate-ai/generated-test-results@1")
        self.assertGreaterEqual(len(test_payload["cases"]), 1)
        self.assertTrue(smoke_payload)


if __name__ == "__main__":
    unittest.main()
