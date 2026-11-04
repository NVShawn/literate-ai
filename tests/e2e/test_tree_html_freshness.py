"""Public tree checks over actual source/output; framework observation is external.

The sole test seam supplies an explicit framework distribution observation, not
source/output owners or case results. Installed non-editable qualification stays
a separate gate. All publication, link topology, bytes and currentness are real.
"""

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.html_framework import HtmlFrameworkObservation
from literate_ai.adapters.html_tree_published_output import (
    _readonly_html_tree_capture_exclusions,
    observe_published_html_tree_output,
)
from literate_ai.adapters.html_tree_render import _framework, _renderer_identity
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.html_trees import HtmlTreeViewDeclaration
from literate_ai.schema_catalog import schema_catalog_root
from tests.support.fixtures_test_cli_component_locks import _run
from tests.support.fixtures_test_html_render import _project


def _declaration_document(source_root=".", output_root="."):
    document = {
        "schema": "literate-ai/retained-tree-html-declaration@1",
        "request_id": "freshness",
        "source_root": ".",
        "output_root": ".",
        "renderer": "renderer://literate-ai/information-cards/1.0.0",
        "case_sensitive": True,
        "limits": {
            "schema": "literate-ai/retained-tree-html-limits@1",
            "source": {
                "max_entries": 256,
                "max_file_bytes": 1048576,
                "max_total_bytes": 4194304,
            },
            "max_descriptor_bytes": 32768,
            "max_descriptor_total_bytes": 1048576,
            "max_index_bytes": 65536,
            "max_metadata_memory_bytes": 134217728,
            "max_depth": 16,
            "max_children_per_group": 2,
            "max_groups_per_page": 1,
            "max_pages": 128,
            "max_page_bytes": 32768,
            "max_total_generated_bytes": 4194304,
            "max_retained_generation_bytes": 8388608,
        },
    }
    document["source_root"] = source_root
    document["output_root"] = output_root
    return document


@unittest.skipUnless(os.name == "posix", "native POSIX tree output boundary")
class TreeHtmlFreshnessTests(unittest.TestCase):
    def test_public_check_reopens_complete_data_without_writes_or_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            source = workspace / "source"
            source.mkdir()
            (source / "empty").mkdir()
            (source / "nested").mkdir()
            (source / "a&b.txt").write_bytes(b"original data\n")
            os.link(source / "a&b.txt", source / "hardlink.txt")
            (source / "alias").symlink_to("nested", target_is_directory=True)
            for ordinal in range(7):
                (source / "nested" / f"item-{ordinal}").write_text(str(ordinal))
            declaration = workspace / "view.json"
            declaration.write_text(json.dumps(_declaration_document()))
            arguments = [
                "render",
                "tree",
                "--root",
                str(source),
                "--declaration",
                str(declaration),
                "--json",
            ]

            def snapshot():
                rows = {}
                pending = [workspace]
                while pending:
                    directory = pending.pop()
                    for path in directory.iterdir():
                        node = path.lstat()
                        content = (
                            os.readlink(path)
                            if path.is_symlink()
                            else hashlib.sha256(path.read_bytes()).hexdigest()
                            if path.is_file()
                            else None
                        )
                        rows[path.relative_to(workspace).as_posix()] = (
                            node.st_dev,
                            node.st_ino,
                            node.st_mode,
                            node.st_size,
                            node.st_mtime_ns,
                            node.st_ctime_ns,
                            content,
                        )
                        if path.is_dir() and not path.is_symlink():
                            pending.append(path)
                return rows

            from jsonschema import Draft202012Validator, ValidationError
            from referencing import Registry, Resource

            documents = [
                json.loads(path.read_bytes())
                for version in ("v1", "v2")
                for path in schema_catalog_root(version).glob("*.schema.json")
            ]
            registry = Registry().with_resources(
                (document["$id"], Resource.from_contents(document))
                for document in documents
            )
            validator = Draft202012Validator(
                {"$ref": "literate-ai/retained-tree-html-check-result@1"},
                registry=registry,
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "LITAI_STATE_DIR": str(workspace / "state"),
                        "LITAI_CONFIG_DIR": str(workspace / "config"),
                    },
                ),
                patch(
                    "literate_ai.adapters.html_tree_render.observe_html_framework",
                    return_value=HtmlFrameworkObservation(
                        canonical_identity("explicit test framework boundary"), "1.4.0"
                    ),
                ),
            ):
                status, planned, error = _run(arguments)
                self.assertEqual(status, 0, (planned, error))
                acknowledgement = planned["result"]["plan_identity"]
                status, published, error = _run(
                    [*arguments, "--apply", "--ack-plan", acknowledgement]
                )
                self.assertEqual(status, 0, (published, error))
                before = snapshot()
                status, checked, error = _run([*arguments, "--check"])
                self.assertEqual(status, 0, (checked, error))
                self.assertEqual(checked["result"]["status"], "current")
                self.assertTrue(checked["result"]["freshness"]["current"])
                self.assertFalse(checked["result"]["freshness"]["writes_authorized"])
                validator.validate(checked["result"])
                forged = json.loads(json.dumps(checked["result"]))
                forged["freshness"]["writes_authorized"] = True
                with self.assertRaises(ValidationError):
                    validator.validate(forged)
                self.assertEqual(snapshot(), before)

                typed = HtmlTreeViewDeclaration.from_dict(
                    json.loads(declaration.read_bytes())
                )
                request = typed.resolve(_renderer_identity(_framework()))
                # Policies below fixed bookkeeping or the actual first control
                # charge refuse before output/request decoding can take over.
                for metadata_budget in (
                    1,
                    65536 + 16 * (source / "index.html").stat().st_size - 1,
                ):
                    tiny = json.loads(declaration.read_bytes())
                    tiny["limits"]["max_metadata_memory_bytes"] = metadata_budget
                    tiny_request = HtmlTreeViewDeclaration.from_dict(tiny).resolve(
                        _renderer_identity(_framework())
                    )
                    with self.assertRaisesRegex(
                        ValueError, "^html.tree.published-output-memory-bound-exceeded$"
                    ):
                        with observe_published_html_tree_output(source, tiny_request):
                            self.fail("unadmitted output controls were exposed")
                self.assertEqual(snapshot(), before)
                # This real read-only observation is already charged by the
                # canonical capture's aggregate reservation. At the exact policy
                # limit, its own charge must not be counted a second time.
                with observe_published_html_tree_output(source, request) as output:
                    retained = output.retained_memory_bytes
                    # Discover the real replay's additional-memory boundary;
                    # its scratch space also needs headroom beyond retained data.
                    accepted = 0
                    refused = request.limits.max_metadata_memory_bytes + 1
                    for _ in range(refused.bit_length()):
                        if refused - accepted <= 1:
                            break
                        candidate = (accepted + refused) // 2
                        try:
                            output.require_current(reserved_memory_bytes=candidate)
                        except ValueError as exc:
                            self.assertIn(
                                str(exc),
                                {
                                    "html.tree.published-output-memory-bound-exceeded",
                                    "html.tree.generation-file-memory-policy-exceeded",
                                    "html.tree.generation-metadata-memory-policy-exceeded",
                                    "html.tree.projection-metadata-policy-exceeded",
                                    "html.tree.invalid-memory-reservation",
                                },
                            )
                            refused = candidate
                        else:
                            accepted = candidate
                    self.assertEqual(refused, accepted + 1)
                    directories, files, current = (
                        _readonly_html_tree_capture_exclusions(
                            output, source, reserved_memory_bytes=retained + accepted
                        )
                    )
                    self.assertEqual(directories, frozenset({".litai-tree-freshness"}))
                    self.assertEqual(files, frozenset({"index.html"}))
                    current(reserved_memory_bytes=retained + accepted)
                    with self.assertRaises(ValueError):
                        current(reserved_memory_bytes=retained + refused)
                    with self.assertRaises(ValueError):
                        current(reserved_memory_bytes=retained - 1)
                with self.assertRaises(ValueError):
                    current(reserved_memory_bytes=retained)
                self.assertEqual(snapshot(), before)

                namespace = source / ".litai-tree-freshness"
                generation = next(namespace.glob("g-*"))
                page = next(generation.glob("*.html"))
                descriptor = next((generation / "metadata").glob("*.json"))
                mutations = (
                    (source / "a&b.txt", b"changed source\n"),
                    (source / "added.txt", b"new member\n"),
                    (page, b"changed page\n"),
                    (page, page.read_bytes() + b"changed\n"),
                    (descriptor, b"{}"),
                    (namespace / "foreign", b"foreign output\n"),
                )
                for path, changed in mutations:
                    with self.subTest(path=path.name):
                        original = path.read_bytes() if path.exists() else None
                        path.write_bytes(changed)
                        tampered = snapshot()
                        status, refused, error = _run([*arguments, "--check"])
                        self.assertEqual(status, 1, (refused, error))
                        self.assertEqual(refused["result"]["status"], "stale")
                        validator.validate(refused["result"])
                        self.assertEqual(snapshot(), tampered)
                        if original is None:
                            path.unlink()
                        else:
                            path.write_bytes(original)
                for topology in ("alias", "hardlink", "deletion"):
                    with self.subTest(topology=topology):
                        alias = source / "alias"
                        hardlink = source / "hardlink.txt"
                        removed = source / "nested" / "item-0"
                        if topology == "alias":
                            alias.unlink()
                            alias.symlink_to("empty", target_is_directory=True)
                        elif topology == "hardlink":
                            payload = hardlink.read_bytes()
                            hardlink.unlink()
                            hardlink.write_bytes(payload)
                        else:
                            payload = removed.read_bytes()
                            removed.unlink()
                        tampered = snapshot()
                        status, refused, error = _run([*arguments, "--check"])
                        self.assertEqual(status, 1, (refused, error))
                        self.assertNotEqual(refused["result"]["status"], "current")
                        validator.validate(refused["result"])
                        self.assertEqual(snapshot(), tampered)
                        if topology == "alias":
                            alias.unlink()
                            alias.symlink_to("nested", target_is_directory=True)
                        elif topology == "hardlink":
                            hardlink.unlink()
                            os.link(source / "a&b.txt", hardlink)
                        else:
                            removed.write_bytes(payload)
                status, checked, error = _run([*arguments, "--check"])
                self.assertEqual(status, 0, (checked, error))
                before = snapshot()
                status, refused, error = _run(
                    [*arguments, "--apply", "--ack-plan", acknowledgement]
                )
                self.assertEqual(status, 1, (refused, error))
                self.assertEqual(snapshot(), before)

    def test_declared_project_gate_is_opt_in_complete_and_read_only(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        from literate_ai.schema_catalog import verify_schema_catalog

        verify_schema_catalog("v2")
        documents = [
            json.loads(path.read_bytes())
            for version in ("v1", "v2")
            for path in schema_catalog_root(version).glob("*.schema.json")
        ]
        resources = Registry().with_resources(
            (document["$id"], Resource.from_contents(document))
            for document in documents
        )

        def validate(result):
            Draft202012Validator(
                {"$ref": result["schema"]}, registry=resources
            ).validate(result)

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            # Real private setgid parents are common on shared build volumes.
            # Only this newly allocated fixture is changed, never a host root.
            workspace.chmod(0o2700)
            state = workspace / "state"
            state.mkdir(mode=0o700)
            state.chmod(0o2700)
            project = workspace / "project"
            project.mkdir()
            _project(project)
            source = project / "source"
            source.mkdir()
            payload = source / "payload.txt"
            payload.write_bytes(b"real project tree source\n")
            (project / "view").mkdir()
            (project / "view").chmod(0o2700)
            declaration = project / "view.json"
            declaration.write_text(json.dumps(_declaration_document("source", "view")))
            registry = project / "literate.tree-html.json"
            registry_document = {
                "schema": "literate-ai/project-tree-html@1",
                "declarations": ["view.json"],
            }

            def snapshot():
                result = {}
                for path in workspace.rglob("*"):
                    node = path.lstat()
                    data = (
                        hashlib.sha256(path.read_bytes()).hexdigest()
                        if stat.S_ISREG(node.st_mode)
                        else os.readlink(path)
                        if stat.S_ISLNK(node.st_mode)
                        else None
                    )
                    result[path.relative_to(workspace).as_posix()] = (
                        node.st_dev,
                        node.st_ino,
                        node.st_mode,
                        node.st_nlink,
                        node.st_size,
                        node.st_mtime_ns,
                        node.st_ctime_ns,
                        data,
                    )
                return result

            def verify(*gates):
                arguments = ["verify", str(project), "--json"]
                for gate in gates:
                    arguments.extend(("--gate", gate))
                before = snapshot()
                status, envelope, error = _run(arguments)
                self.assertEqual(snapshot(), before)
                self.assertIn("result", envelope, (status, envelope, error))
                result = envelope["result"]
                validate(result)
                return status, result, error

            with (
                patch.dict(
                    os.environ,
                    {
                        "LITAI_STATE_DIR": str(workspace / "state"),
                        "LITAI_CONFIG_DIR": str(workspace / "config"),
                        "OBJ_DIR": str(workspace / "unrelated-diagnostics"),
                    },
                ),
                patch(
                    "literate_ai.adapters.html_tree_render.observe_html_framework",
                    return_value=HtmlFrameworkObservation(
                        canonical_identity("explicit project framework boundary"),
                        "1.4.0",
                    ),
                ),
            ):
                status, legacy, error = verify("html-observability")
                self.assertEqual(status, 0, (legacy, error))
                self.assertEqual(legacy["schema"], "literate-ai/project-verify@1")
                status, absent, error = verify("tree-html-observability")
                self.assertEqual(status, 0, (absent, error))
                self.assertEqual(absent["schema"], "literate-ai/project-verify@2")
                self.assertEqual(absent["gates"][0]["state"], "skipped")
                registry.write_text(json.dumps(registry_document))
                arguments = [
                    "render",
                    "tree",
                    "--root",
                    str(project),
                    "--declaration",
                    str(declaration),
                    "--json",
                ]
                status, planned, error = _run(arguments)
                self.assertEqual(status, 0, (planned, error))
                for unsafe_mode in (0o2750, 0o2707, 0o3700):
                    with self.subTest(refused_private_state_mode=oct(unsafe_mode)):
                        state.chmod(unsafe_mode)
                        before = snapshot()
                        status, refused, error = _run(
                            [
                                *arguments,
                                "--apply",
                                "--ack-plan",
                                planned["result"]["plan_identity"],
                            ]
                        )
                        self.assertEqual(status, 1, (refused, error))
                        self.assertEqual(
                            refused["result"]["refusal"]["code"],
                            "html.tree.private-state-directory-required",
                        )
                        self.assertEqual(snapshot(), before)
                state.chmod(0o2700)
                status, published, error = _run(
                    [
                        *arguments,
                        "--apply",
                        "--ack-plan",
                        planned["result"]["plan_identity"],
                    ]
                )
                self.assertEqual(status, 0, (published, error))
                self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o2700)
                if sys.platform.startswith("linux"):
                    # Linux actually inherits setgid on mkdir, exercising the
                    # independent newly allocated output namespace check too.
                    namespace = project / "view" / ".litai-tree-freshness"
                    self.assertEqual(stat.S_IMODE(namespace.stat().st_mode), 0o2700)
                status, current, error = verify("tree-html-observability")
                self.assertEqual(status, 0, (current, error))
                self.assertEqual(current["gates"][0]["state"], "pass")
                self.assertEqual(len(current["gates"][0]["artifacts"]), 1)
                validate(current["gates"][0]["artifacts"][0])
                _, complete, _ = verify()
                self.assertEqual(complete["schema"], "literate-ai/project-verify@2")
                tree_gate = next(
                    gate
                    for gate in complete["gates"]
                    if gate["gate"] == "tree-html-observability"
                )
                self.assertEqual(tree_gate["state"], "pass")
                # This fixture is deliberately not full project acceptance: the
                # inherited unrelated authority gates keep their own verdicts.
                from literate_ai.adapters.html_tree_project import (
                    observe_project_tree_views,
                )
                from literate_ai.projects import ProjectError, discover_project

                observed_project = discover_project(project)
                for changed in (registry, declaration):
                    with self.subTest(original_input_currentness=changed.name):
                        original = changed.read_bytes()
                        with self.assertRaises(ProjectError):
                            with observe_project_tree_views(observed_project) as (
                                _,
                                current_inputs,
                            ):
                                changed.write_bytes(original + b"\n")
                                current_inputs()
                        changed.write_bytes(original)
                manifest = project / "literate.project.json"
                original_manifest = manifest.read_bytes()
                constrained = json.loads(original_manifest)
                from literate_ai.contracts.projects import ProjectScaleLimits

                constrained["scale_limits"] = ProjectScaleLimits(
                    pinned_input_files=1,
                    pinned_input_total_bytes=2 * 1024**2,
                    catalog_files=1,
                    catalog_entries=1,
                    catalog_total_bytes=2 * 1024**2,
                ).to_dict()
                manifest.write_text(json.dumps(constrained))
                status, refused, error = verify("tree-html-observability")
                self.assertEqual(status, 1, (refused, error))
                self.assertIn(
                    "inputs.closure_file_limit", refused["gates"][0]["detail"]
                )
                manifest.write_bytes(original_manifest)
                large = project / "large.json"
                raw = declaration.read_bytes()
                large.write_bytes(raw + b" " * (65537 - len(raw)))
                registry.write_text(
                    json.dumps({**registry_document, "declarations": ["large.json"]})
                )
                status, refused, error = verify("tree-html-observability")
                self.assertEqual(status, 1, (refused, error))
                self.assertIn(
                    "inputs.closure_file_size_limit", refused["gates"][0]["detail"]
                )
                other = project / "other.json"
                other_document = _declaration_document("source", "view")
                other_document["request_id"] = "another"
                other.write_text(json.dumps(other_document))
                registry.write_text(
                    json.dumps(
                        {
                            **registry_document,
                            "declarations": ["view.json", "other.json"],
                        }
                    )
                )
                status, refused, error = verify("tree-html-observability")
                self.assertEqual(status, 1, (refused, error))
                self.assertIn(
                    "project-output-index-collision", refused["gates"][0]["detail"]
                )
                registry.write_text(json.dumps(registry_document))
                payload.write_bytes(b"changed project source\n")
                status, stale, error = verify("tree-html-observability")
                self.assertEqual(status, 1, (stale, error))
                self.assertIn("html.tree.source-stale", stale["gates"][0]["detail"])
                payload.write_bytes(b"real project tree source\n")
                for paths in (
                    ["../view.json"],
                    ["/view.json"],
                    ["view.json", "VIEW.json"],
                    ["view.json", "view.json/child.json"],
                    ["view.json"] * 129,
                ):
                    with self.subTest(paths=paths[:2], count=len(paths)):
                        registry.write_text(
                            json.dumps({**registry_document, "declarations": paths})
                        )
                        status, refused, error = verify("tree-html-observability")
                        self.assertEqual(status, 1, (refused, error))
                        self.assertEqual(refused["gates"][0]["state"], "fail")
                registry.write_text(
                    '{"schema":"literate-ai/project-tree-html@1",'
                    '"declarations":["view.json"],"declarations":[]}'
                )
                status, refused, error = verify("tree-html-observability")
                self.assertEqual(status, 1, (refused, error))
                self.assertIn("duplicate-json-field", refused["gates"][0]["detail"])
                registry.unlink()
                registry.symlink_to("missing-registry.json")
                _, refused, _ = verify()
                self.assertEqual(refused["schema"], "literate-ai/project-verify@2")
                self.assertEqual(
                    next(
                        gate
                        for gate in refused["gates"]
                        if gate["gate"] == "tree-html-observability"
                    )["state"],
                    "fail",
                )
                registry.unlink()
                _, legacy_default, _ = verify()
                self.assertEqual(
                    legacy_default["schema"], "literate-ai/project-verify@1"
                )
                self.assertNotIn(
                    "tree-html-observability",
                    [g["gate"] for g in legacy_default["gates"]],
                )
