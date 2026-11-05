"""Real public installed-CLI tree publication controls; no synthetic authority.

Invoked by the existing wheel qualification driver with its once-installed wheel
interpreter. Fixtures are small real files, aliases and directories. This journey
qualifies initial metadata publication and read-only freshness through actual
standalone and project CLIs, not browser behavior, rerender, recovery, native scale
or per-unit admission.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def snapshot(root):
    """Observe original physical paths without following directory aliases."""
    node = root.lstat()
    rows = {
        ".": (
            "root",
            node.st_dev,
            node.st_ino,
            node.st_mode,
            node.st_mtime_ns,
            node.st_ctime_ns,
        )
    }
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            relative = path.relative_to(root).as_posix()
            node = path.lstat()
            if stat.S_ISDIR(node.st_mode):
                rows[relative] = (
                    "directory",
                    node.st_dev,
                    node.st_ino,
                    node.st_mode,
                    node.st_uid,
                    node.st_gid,
                )
                pending.append(path)
            elif stat.S_ISLNK(node.st_mode):
                rows[relative] = ("symlink", os.readlink(path))
            else:
                require(stat.S_ISREG(node.st_mode), "fixture contains unexpected node")
                rows[relative] = (
                    "file",
                    node.st_dev,
                    node.st_ino,
                    node.st_mode,
                    node.st_nlink,
                    node.st_size,
                    node.st_mtime_ns,
                    node.st_ctime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
    return rows


class OfflinePage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        require(
            tag not in {"script", "iframe", "object", "embed", "img", "link"},
            "tree page requires an active or fetched resource",
        )
        for name, value in attrs:
            require(
                name not in {"src", "srcset"} and not name.startswith("on"),
                "tree page contains active resource attributes",
            )
            if name == "href":
                require(tag == "a", "unexpected navigation attribute")
                self.links.append(value)

    def handle_data(self, value):
        self.text.append(value)


def crawl(source, generation):
    initial = source / "index.html"
    pending = [initial]
    visited = set()
    text = []
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        parser = OfflinePage()
        parser.feed(path.read_text(encoding="utf-8"))
        parser.close()
        text.extend(parser.text)
        for href in parser.links:
            parsed = urlsplit(href)
            require(
                not any((parsed.scheme, parsed.netloc, parsed.query, parsed.fragment)),
                "tree navigation is not fully offline",
            )
            require(not Path(parsed.path).is_absolute(), "absolute tree navigation")
            candidate = path.parent / parsed.path
            target = candidate.resolve(strict=True)
            require(candidate == target, "navigation follows an indirect path")
            require(target.is_relative_to(generation), "navigation leaves generation")
            require(
                target.is_file() and not target.is_symlink(), "indirect page target"
            )
            pending.append(target)
    pages = set(generation.glob("*.html"))
    require(visited == pages | {initial}, "offline crawl missed a generated page")
    return len(pages), " ".join(text)


def captured_inputs(generation, publication, original):
    """Join public retained descriptors to every actual original fixture node."""
    manifest_bytes = (generation / "manifest.json").read_bytes()
    manifest_ref = publication["generation_manifest"]
    require(
        len(manifest_bytes) == manifest_ref["size"]
        and hashlib.sha256(manifest_bytes).hexdigest() == manifest_ref["digest"],
        "physical manifest differs from public publication",
    )
    manifest = json.loads(manifest_bytes)

    def document(reference):
        raw = (generation / "metadata" / (reference["digest"] + ".json")).read_bytes()
        require(
            len(raw) == reference["size"]
            and hashlib.sha256(raw).hexdigest() == reference["digest"],
            "retained source descriptor byte binding differs",
        )
        return json.loads(raw)

    source_ref = manifest["source_index"]
    require(
        "sha256:" + source_ref["digest"] == publication["source_identity"],
        "public source identity differs from retained complete index",
    )
    index = document(source_ref)
    seen = set()
    for reference in index["shards"]:
        shard = document(reference)
        for coordinate in shard["directories"]:
            require(original[coordinate][0] == "directory", "source directory changed")
            seen.add(coordinate)
        for row in shard["files"]:
            value = original[row["path"]]
            require(
                value[0] == "file" and value[-1] == row["blob"]["digest"],
                "captured file digest does not bind actual payload",
            )
            seen.add(row["path"])
        for row in shard["aliases"]:
            value = original[row["path"]]
            if row["kind"] == "symlink":
                require(
                    value == ("symlink", row["target"]), "symbolic topology differs"
                )
            else:
                target = original[row["target"]]
                require(
                    value[0] == target[0] == "file" and value[1:3] == target[1:3],
                    "hardlink topology differs from actual native inode",
                )
            seen.add(row["path"])
    require(
        seen == set(original) - {"."},
        "retained source membership is incomplete or foreign",
    )


def main():
    require(sys.flags.isolated, "journey requires isolated installed interpreter")
    import literate_ai
    from literate_ai.adapters.builders._process import run_bounded_process
    from literate_ai.adapters.html_framework import observe_html_framework
    from literate_ai.contracts.html_observability import HtmlRenderRefusal
    from literate_ai.source_to_specification.wire_migrations import (
        _validate_wire_document,
    )

    require(
        Path(literate_ai.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),
        "journey imported framework outside installed prefix",
    )
    framework = observe_html_framework()
    require(
        not isinstance(framework, HtmlRenderRefusal), "noneditable wheel unavailable"
    )
    require(
        framework.framework_distribution_identity.uri == sys.argv[2],
        "journey imported a different wheel distribution",
    )
    workspace = Path(sys.argv[1]).absolute()
    workspace.mkdir(mode=0o700)
    workspace = workspace.resolve(strict=True)
    seed = workspace / "seed"
    seed.mkdir()
    (seed / "empty").mkdir()
    (seed / "branch").mkdir()
    (seed / "branch" / "note.txt").write_bytes(b"nested fixture\n")
    (seed / "leaf&one.txt").write_bytes(b"original payload\n")
    for ordinal in range(9):
        (seed / f"fanout-{ordinal}.txt").write_bytes(f"child {ordinal}\n".encode())
    if os.name == "posix":
        (seed / "branch" / "up-link").symlink_to("../leaf&one.txt")
        (seed / "directory-alias").symlink_to("branch", target_is_directory=True)
    limits = {
        "schema": "literate-ai/retained-tree-html-limits@1",
        "source": {
            "max_entries": 128,
            "max_file_bytes": 1048576,
            "max_total_bytes": 16777216,
        },
        "max_descriptor_bytes": 32768,
        "max_descriptor_total_bytes": 524288,
        "max_index_bytes": 65536,
        "max_metadata_memory_bytes": 67108864,
        "max_depth": 16,
        "max_children_per_group": 2,
        "max_groups_per_page": 1,
        "max_pages": 128,
        "max_page_bytes": 32768,
        "max_total_generated_bytes": 4194304,
        "max_retained_generation_bytes": 8388608,
    }

    def fixture(name):
        case = workspace / name
        case.mkdir()
        source = case / "source"
        shutil.copytree(seed, source, symlinks=True)
        if os.name == "posix":
            os.link(source / "leaf&one.txt", source / "hardlink.txt")
        declaration = case / "declaration.json"
        document = {
            "schema": "literate-ai/retained-tree-html-declaration@1",
            "request_id": "installed-tree",
            "source_root": ".",
            "output_root": ".",
            "limits": json.loads(json.dumps(limits)),
            "case_sensitive": True,
            "renderer": "renderer://literate-ai/information-cards/1.0.0",
        }
        declaration.write_text(json.dumps(document), encoding="utf-8")
        state, config = case / "state", case / "config"
        environment = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "PYTHONPATH",
                "PYTHONHOME",
                "LITAI_DEBUG",
                "LITAI_DEBUG_LOG",
                "LITAI_DEBUG_JSON",
            }
        }
        environment.update(
            LITAI_STATE_DIR=str(state),
            LITAI_CONFIG_DIR=str(config),
            OBJ_DIR=str(source / "unexpected-objects"),
        )
        return source, declaration, document, state, config, environment

    cli_evidence = workspace / "cli-evidence"
    cli_evidence.mkdir(mode=0o700)
    cli_call_count = 0

    def invoke(environment, command, arguments, *, expected=0):
        nonlocal cli_call_count
        cli_call_count += 1
        require(cli_call_count <= 64, "installed tree CLI call bound exceeded")
        argv = (
            sys.executable,
            "-I",
            "-B",
            "-m",
            "literate_ai.cli",
            "--json",
            *arguments,
        )
        completed = run_bounded_process(
            argv,
            cwd=workspace,
            environment=environment,
            timeout_seconds=180,
            stdout_limit_bytes=131072,
            stderr_limit_bytes=131072,
            error_prefix="installed-tree-html",
            terminate_descendants=True,
        )
        stem = cli_evidence / f"call-{cli_call_count:02d}"
        for suffix, content in (
            ("stdout", completed.stdout),
            ("stderr", completed.stderr),
        ):
            with stem.with_suffix("." + suffix).open("xb") as stream:
                stream.write(content)
        with stem.with_suffix(".json").open("x") as stream:
            json.dump(
                {
                    "argv": list(argv),
                    "actual_exit_code": completed.returncode,
                    "expected_exit_code": expected,
                    "framework_distribution_identity": (
                        framework.framework_distribution_identity.uri
                    ),
                },
                stream,
                indent=2,
            )
        require(
            completed.returncode
            in (expected if isinstance(expected, tuple) else (expected,)),
            f"public tree CLI exit differs; retained {stem.name} raw evidence",
        )
        raw = completed.stdout if completed.stdout else completed.stderr
        envelope = json.loads(raw)
        require(envelope["command"] == command, "incorrect public command label")
        if expected == 2:
            require(
                envelope["schema"] == "literate-ai/cli-error@1", "missing CLI refusal"
            )
            return envelope["error"]
        require(envelope["schema"] == "literate-ai/cli-result@1", "missing CLI result")
        result = envelope["result"]
        if command != "onboard":
            _validate_wire_document(
                result, result["schema"], code="installed-tree.schema"
            )
        return result

    def cli(case, *options, expected=0):
        source, declaration, _, _, _, environment = case
        return invoke(
            environment,
            "render.tree",
            (
                "render",
                "tree",
                "--root",
                str(source),
                "--declaration",
                str(declaration),
                *options,
            ),
            expected=expected,
        )

    def untouched(case, before):
        source, _, _, state, config, _ = case
        require(snapshot(source) == before, "read-only/refused CLI changed source")
        require(not state.exists() and not config.exists(), "unexpected operator state")

    case = fixture("initial")
    before = snapshot(case[0])
    planned = cli(case, expected=0 if os.name == "posix" else 1)
    if os.name != "posix":
        require(
            planned["status"] == "refused", "unsupported native output was admitted"
        )
        require(
            planned["refusal"]["code"]
            == "html.tree.native-output-initialization-unsupported",
            "wrong refusal",
        )
        untouched(case, before)
        print(
            json.dumps(
                {
                    "qualified": False,
                    "native_output": "unsupported",
                    "readonly_refusal_preserved": True,
                },
                sort_keys=True,
            )
        )
        return
    require(planned["status"] == "planned", "initial plan was not produced")
    require(
        planned["plan"]["framework_distribution_identity"]
        == framework.framework_distribution_identity.to_dict(),
        "public plan did not bind the actual installed distribution",
    )
    untouched(case, before)
    for option in ("--discover-mcps", "--debug=" + str(workspace / "forbidden.log")):
        error = cli(case, option, expected=2)
        require(error["code"] == "render.tree.scoped_options", "wrong scoped refusal")
        untouched(case, before)
    require(not (workspace / "forbidden.log").exists(), "scoped debug file was written")
    for options, code in (
        (("--apply",), "html.tree.explicit-plan-acknowledgement-required"),
        (
            ("--apply", "--ack-plan", "sha256:" + "0" * 64),
            "html.tree.full-plan-acknowledgement-mismatch",
        ),
    ):
        refused = cli(case, *options, expected=1)
        require(refused["status"] == "refused", "unacknowledged publication admitted")
        require(refused["refusal"]["code"] == code, "wrong acknowledgement refusal")
        untouched(case, before)
    published = cli(case, "--apply", "--ack-plan", planned["plan_identity"])
    require(
        published["status"] == "published", "acknowledged initial publication failed"
    )
    publication = published["publication"]
    namespace = case[0] / ".litai-tree-installed-tree"
    generation = namespace / ("g-" + publication["generation_manifest"]["digest"])
    captured_inputs(generation, publication, before)
    page_count, text = crawl(case[0], generation)
    require(
        page_count == publication["page_count"] and page_count > 3,
        "real fanout did not exercise paginated closure",
    )
    require(
        "leaf&one.txt" in text
        and "../leaf&one.txt" in text
        and "hardlink" in text
        and "directory-alias" in text
        and "No children" in text,
        "cards omitted escaped labels, alias targets, topology or empty directory",
    )
    after = snapshot(case[0])
    require(
        {p: after[p] for p in before if p != "."}
        == {p: value for p, value in before.items() if p != "."},
        "publication altered original inputs",
    )
    require(
        set(after) - set(before)
        == {"index.html", namespace.name}
        | {p for p in after if p.startswith(namespace.name + "/")},
        "publication created undeclared source paths",
    )
    require(not case[4].exists(), "publication initialized unrelated operator config")
    require(
        hashlib.sha256((case[0] / "index.html").read_bytes()).hexdigest()
        == publication["index_identity"].split(":")[1],
        "public index digest differs",
    )

    def private_snapshot(environment):
        return {
            key: snapshot(Path(environment[key]))
            if Path(environment[key]).exists()
            else None
            for key in ("LITAI_STATE_DIR", "LITAI_CONFIG_DIR", "OBJ_DIR")
        }

    def check_unchanged(case, *, expected=0):
        private_before = private_snapshot(case[5])
        before = snapshot(case[0])
        checked = cli(case, "--check", expected=expected)
        require(snapshot(case[0]) == before, "freshness check wrote source/output")
        require(
            private_snapshot(case[5]) == private_before,
            "freshness check wrote private state",
        )
        return checked

    checked = check_unchanged(case)
    require(checked["status"] == "current", "installed published tree not current")
    require(checked["freshness"]["writes_authorized"] is False, "check grants writes")
    # Actual mutations of independent source and published page bytes must refuse;
    # restoring their original bytes does not restore a publication owner.
    for path, code in (
        (case[0] / "leaf&one.txt", "html.tree.source-stale"),
        (next(generation.glob("*.html")), None),
    ):
        original = path.read_bytes()
        try:
            path.write_bytes(original + b"changed\n")
            refused = check_unchanged(case, expected=1)
            require(refused["status"] == "stale", "changed complete tree admitted")
            if code is not None:
                require(
                    refused["refusal"]["code"] == code, "wrong stale source refusal"
                )
        finally:
            path.write_bytes(original)
    require(check_unchanged(case)["status"] == "current", "restored tree bytes differ")

    # A separate real project is created through the same installed wheel and the
    # wheel driver's already-local, stripped, exact-revision parent. Never mutate
    # another golden-path fixture or synthesize framework/project authority.
    require(len(sys.argv) == 4, "local exact parent source argument required")
    project = workspace / "project"
    project_environment = dict(case[5])
    project_environment.update(
        LITAI_STATE_DIR=str(workspace / "project-state"),
        LITAI_CONFIG_DIR=str(workspace / "project-config"),
        OBJ_DIR=str(workspace / "project-objects"),
    )
    create = ("onboard", "create", str(project), "--empty", "--from", sys.argv[3])
    project_plan = invoke(project_environment, "onboard", create)
    require(
        project_plan["writes"] is False and not project.exists(),
        "onboard plan mutated new project",
    )
    created = invoke(
        project_environment,
        "onboard",
        (
            *create,
            "--apply",
            "--acknowledge",
            "--expect-plan",
            project_plan["plan_identity"],
        ),
    )
    require(
        created["applied"] is True and (project / "literate.project.json").is_file(),
        "real installed onboard did not initialize project",
    )
    source, output = project / "source", project / "view"
    source.mkdir()
    output.mkdir()
    payload = source / "payload.txt"
    payload.write_bytes(b"installed project tree\n")
    declaration = project / "view.json"
    declaration_document = json.loads(json.dumps(case[2]))
    declaration_document.update(source_root="source", output_root="view")
    declaration.write_text(json.dumps(declaration_document), encoding="utf-8")
    project_case = (
        project,
        declaration,
        declaration_document,
        workspace / "project-state",
        workspace / "project-config",
        project_environment,
    )
    registry = project / "literate.tree-html.json"

    def verify_tree(*, expected=0, gate="tree-html-observability"):
        private_before = private_snapshot(project_environment)
        before = snapshot(project)
        result = invoke(
            project_environment,
            "verify",
            ("verify", str(project), "--gate", gate),
            expected=expected,
        )
        require(snapshot(project) == before, "project verification wrote project")
        require(
            private_snapshot(project_environment) == private_before,
            "verification wrote private state",
        )
        return result

    legacy = verify_tree(gate="html-observability")
    require(legacy["schema"] == "literate-ai/project-verify@1", "legacy schema changed")
    absent = verify_tree()
    require(
        absent["schema"] == "literate-ai/project-verify@2"
        and absent["gates"][0]["state"] == "skipped",
        "absent registry was adopted",
    )
    registry_document = {
        "schema": "literate-ai/project-tree-html@1",
        "declarations": ["view.json"],
    }
    registry.write_text(json.dumps(registry_document), encoding="utf-8")
    project_publication_plan = cli(project_case)
    cli(
        project_case, "--apply", "--ack-plan", project_publication_plan["plan_identity"]
    )
    current = verify_tree()
    require(
        current["gates"][0]["state"] == "pass"
        and len(current["gates"][0]["artifacts"]) == 1,
        "declared view not verified",
    )
    for artifact in current["gates"][0]["artifacts"]:
        _validate_wire_document(
            artifact, artifact["schema"], code="installed-tree.artifact"
        )
    original = payload.read_bytes()
    try:
        payload.write_bytes(b"changed installed project source\n")
        stale = verify_tree(expected=1)
        require(
            "html.tree.source-stale" in stale["gates"][0]["detail"],
            "project gate failed to reject changed source",
        )
    finally:
        payload.write_bytes(original)
    require(
        verify_tree()["gates"][0]["state"] == "pass", "restored project view differs"
    )
    for invalid in ("../outside.json", "missing.json"):
        registry.write_text(
            json.dumps({**registry_document, "declarations": [invalid]})
        )
        require(
            verify_tree(expected=1)["gates"][0]["state"] == "fail",
            "invalid declared project input admitted",
        )
    registry.unlink()
    registry.symlink_to("missing-registry.json")
    require(
        verify_tree(expected=1)["gates"][0]["state"] == "fail",
        "dangling registry was silently skipped",
    )

    for kind in ("index", "namespace"):
        foreign = fixture("foreign-" + kind)
        path = foreign[0] / ("index.html" if kind == "index" else namespace.name)
        if kind == "namespace":
            path.mkdir()
            path = path / "foreign.txt"
        path.write_bytes(b"operator owned\n")
        original = snapshot(foreign[0])
        refused = cli(foreign, expected=1)
        require(refused["status"] == "refused", "foreign output admitted")
        require(
            refused["refusal"]["code"] == "html.tree.initial-output-already-exists",
            "wrong foreign output refusal",
        )
        untouched(foreign, original)

    for kind in ("declaration", "physical-root"):
        stale = fixture("stale-" + kind)
        acknowledgement = cli(stale)["plan_identity"]
        if kind == "declaration":
            stale[2]["limits"]["max_pages"] -= 1
            stale[1].write_text(json.dumps(stale[2]), encoding="utf-8")
        else:
            old = stale[0].with_name("retained-original-source")
            stale[0].rename(old)
            shutil.copytree(old, stale[0], symlinks=True)
        original = snapshot(stale[0])
        result = cli(stale, "--apply", "--ack-plan", acknowledgement, expected=1)
        require(
            result["refusal"]["code"] == "html.tree.full-plan-acknowledgement-mismatch",
            "stale complete plan was not refused",
        )
        untouched(stale, original)

    bounded = fixture("bounded-interruption")
    bounded[2]["limits"]["source"]["max_entries"] = 1
    bounded[1].write_text(json.dumps(bounded[2]), encoding="utf-8")
    acknowledgement = cli(bounded)["plan_identity"]
    original = snapshot(bounded[0])
    require(
        cli(bounded, "--apply", "--ack-plan", acknowledgement, expected=1)["status"]
        == "refused",
        "over-limit source was published",
    )
    require(
        not (bounded[0] / "index.html").exists(), "partial generation became visible"
    )
    retained = bounded[0] / namespace.name
    require(
        (retained / "owner.json").is_file(), "interruption lost actual owner marker"
    )
    after = snapshot(bounded[0])
    require(
        {p: after[p] for p in original if p != "."}
        == {p: value for p, value in original.items() if p != "."},
        "budget refusal altered source",
    )
    require(cli(bounded, expected=1)["status"] == "refused", "interruption was adopted")
    require(snapshot(bounded[0]) == after, "refused recovery changed retained evidence")
    print(
        json.dumps(
            {
                "qualified": True,
                "scope": "initial-directory-metadata-and-readonly-freshness",
                "standalone_freshness": True,
                "project_tree_gate": True,
                "framework_distribution": framework.framework_distribution_identity.uri,
                "page_count": page_count,
                "source_identity": publication["source_identity"],
                "generation_manifest_digest": publication["generation_manifest"][
                    "digest"
                ],
                "index_identity": publication["index_identity"],
                "offline_complete": True,
                "original_payload_topology_preserved": True,
                "foreign_output_preserved": True,
                "stale_ack_refused": True,
                "budget_interruption_preserved": True,
                "runtime_source_mutation_race": "not-qualified",
                "transient_index_replacement": "source-review-only",
                "rerender_recovery_units_browser_native_scale": "not-qualified",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
