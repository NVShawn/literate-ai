"""Select factual excerpts from local captures; never fabricate execution results."""

import argparse
import hashlib
import json
import re
import subprocess
import textwrap
from pathlib import Path

# Failures the film shows on purpose. Any other nonzero exit refuses collection.
SHOWN_FAILURES = {
    "rebuild-failed",
    "release-check",
    "release-check-resume-a",
    # By design: a repaired run is not release evidence; it demands a clean rerun.
    "release-check-resume",
    "qualify-stale",
}
# Captures that only some recordings produce; absent means "did not happen".
OPTIONAL = {
    "rebuild-failed",
    "rebuild-refresh",
    "release-check",
    "release-check-resume-a",
    "release-check-resume",
    "release-check-clean",
    "render-html",
    "render-html-verification-health",
    "verify-html",
    "qualify-stale",
    "qualify",
}


def documents(text):
    """Every JSON document in a stream; gate runners may print several."""
    decoder, index, found = json.JSONDecoder(), 0, []
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        value, index = decoder.raw_decode(text, index)
        found.append(value)
    return found


def make_lines(record):
    """Checkpoint narration and verdict lines from a Make gate run."""
    keep = re.compile(
        r"^(checkpoint: |lifecycle driver|state +:|changed since pin|"
        r"    [\w./-]+\.\w+$|"
        r"Totals: |ℹ (pass|fail) |make: \*\*\*|RuntimeError: |stderr: )"
    )
    text = record["stdout"] + "\n" + record["stderr"]
    lines = [line for line in text.splitlines() if keep.match(line)]
    # The node test reporter's info glyph is absent from replay fonts.
    lines = [line.replace("ℹ ", "node test: ", 1) for line in lines]
    return list(dict.fromkeys(lines))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--captures", type=Path, required=True)
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--retained-evidence", type=Path, required=True)
    p.add_argument("--tinyxml2", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--build-root", type=Path, help="private build custody root")
    p.add_argument("--release-clone", type=Path, help="disposable gate clone")
    p.add_argument("--gate-log", type=Path, help="recorded release-check log")
    p.add_argument("--gate-revision", help="revision the recorded gate log proved")
    p.add_argument("--windows-line", help="reported Windows gate line, verbatim")
    p.add_argument("--framework-version", default="1.1.0 development wheel")
    p.add_argument("--framework-source", default="98042090")
    p.add_argument("--coding-cli", default="codex")
    p.add_argument("--model-selector", default="gpt-5.6-sol")
    p.add_argument(
        "--parent",
        default="https://github.com/jordanhubbard/literate-ai#fcc40bc617a2bc2455627db7396a1e016ebfbab6",
    )
    args = p.parse_args()
    excerpts = {}
    selected = {
        "create-plan": [
            "writes",
            "mode",
            "blockers",
            "initialization.project_type",
            "initialization.flavor_selectors",
        ],
        "create-apply": ["applied", "mode", "status.project.project_id"],
        "adopt-plan": ["writes", "mode", "blockers", "landing_stage"],
        "adopt-apply": ["applied", "mode"],
        "rebuild": [
            "passed",
            "receipt_committed",
            "coding_cli",
            "test_summary",
            "repair",
            "source_generation",
            "tests",
            "source",
            "test_count",
        ],
        "rebuild-refresh": [
            "passed",
            "receipt_committed",
            "source_generation",
            "build_cache.hits",
            "repair",
        ],
        "rebuild-failed": [],
        "verify": ["counts", "ok"],
        "verify-html": ["counts", "ok"],
        "package": ["publication_authorized", "packages", "execution_worker.kind"],
        "package-verify": [
            "publication_authorized",
            "verified",
            "execution_worker.kind",
        ],
        "retained": [
            "classification",
            "tests",
            "native_component_generation",
            "native_component_acceptance",
        ],
        "retained-publish": ["updated", "state", "receipt_identity"],
        "retained-stage": ["stage", "release_authority", "advanced", "authority"],
        "update": ["mode", "changed", "framework.counts", "repository_lineage.counts"],
        "render-html": [
            "status",
            "artifact.artifact_path",
            "artifact.embedding.single_file",
            "artifact.embedding.external_reference_count",
            "artifact.provenance.view.view_id",
        ],
        "render-html-verification-health": [
            "status",
            "artifact.artifact_path",
            "artifact.embedding.external_reference_count",
        ],
        "qualify-stale": ["counts", "ok"],
        "qualify": ["host_platform", "coverage", "ci_required", "ci"],
        "release-check": [],
        "release-check-resume-a": [],
        "release-check-resume": [],
        "release-check-clean": [],
    }
    titles = {
        "create-plan": "Inspect before creating",
        "create-apply": "Apply the reviewed create plan",
        "adopt-plan": "TinyXML2: inspect the existing project",
        "adopt-apply": "TinyXML2: wrap and preserve",
        "rebuild": "Real agent generation and lifecycle result",
        "rebuild-refresh": "Rebuild after a project change: source reused",
        "rebuild-failed": "A real refusal: inherited oracle mismatch",
        "verify": "Current verification, including skips",
        "verify-html": "Verification with declared HTML views",
        "package": "Build the local application wheel",
        "package-verify": "Verify the wheel",
        "retained": "Retained C++ build and test evidence",
        "retained-publish": "Commit the local finalized receipt",
        "retained-stage": "Retained does not mean rewritten",
        "update": "Read-only project update plan",
        "render-html": "litai render html: the authority graph",
        "render-html-verification-health": "litai render html: a health view",
        "qualify-stale": "Qualification refused: a declared page went stale",
        "qualify": "litai release qualify: this host first",
        "release-check": "make release-check stops at an unreviewed change",
        "release-check-resume-a": "Resume after review: passed gates are reused",
        "release-check-resume": "Repair run completes; evidence restarts at gate one",
        "release-check-clean": "Clean release-check from gate one",
    }
    roots = [
        (args.release_clone, "<release-clone>"),
        (args.project, "<project>"),
        (args.source, "<generated-source>"),
        (args.tinyxml2, "<tinyxml2>"),
        (args.build_root, "<build>"),
        (Path.cwd(), "<workspace>"),
        (Path.home(), "<user>"),
    ]
    paths = sorted(
        ((str(path.resolve()), label) for path, label in roots if path),
        key=lambda item: -len(item[0]),
    )
    private = re.compile(
        r"/(?:private/)?var/folders/[^\s\"']+|/(?:private/)?tmp/claude-[^\s\"']+"
        r"|/Users/[^/\s\"']+|[A-Za-z]:\\Users\\[^\\\s\"']+"
    )

    def clean(value):
        for path, label in paths:
            value = value.replace(path, label)
        value = private.sub("<host-path>", value)
        if any(
            s in value.lower()
            for s in ("nvidia-dev", "gitlab-master", "bearer ", "api_key=", "token=")
        ):
            raise ValueError("Private-looking excerpt: manual review required")
        if re.search(r"\b(jkh|jorda)\b", value):
            raise ValueError("Username in excerpt: manual review required")
        return value

    def wrap(lines):
        return [
            wrapped
            for line in lines
            for wrapped in (
                textwrap.wrap(line, 94, replace_whitespace=False, drop_whitespace=False)
                or [""]
            )
        ]

    for name, fields in selected.items():
        file = args.captures / f"{name}.json"
        if name in OPTIONAL and not file.exists():
            continue
        record = json.loads(file.read_text())
        if record["exit_code"] != 0 and name not in SHOWN_FAILURES:
            raise ValueError(
                f"Cannot imply success for {name}: exit {record['exit_code']}"
            )
        executable = Path(record["argv"][0]).name
        command = [executable, *record["argv"][1:]]
        lines = textwrap.wrap("$ " + " ".join(command), 94, subsequent_indent="  ")
        lines += [
            f"exit_code: {record['exit_code']}",
            f"recorded duration: {record['elapsed_seconds']} seconds",
        ]
        if executable == "make":
            lines += make_lines(record)
        else:
            document = documents(record["stdout"] or record["stderr"])[-1]
            result = document.get("result", {})
            lines.append(f"ok: {str(document.get('ok')).lower()}")
            if name == "rebuild-failed":
                lines += [
                    "error.code: " + document["error"]["code"],
                    *document["error"]["message"].splitlines()[-3:],
                ]
            for field in fields:
                value = result
                for part in field.split("."):
                    value = value.get(part) if isinstance(value, dict) else None
                if value is None:
                    continue
                # Complex plans get a truthful key inventory, not invented results.
                if isinstance(value, dict) and len(json.dumps(value)) > 800:
                    value = {
                        "section_keys": list(value),
                        "detail": "see full local capture; excerpt only",
                    }
                rendered = (
                    json.dumps(value, indent=2, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else json.dumps(value)
                )
                lines += [f"{field}:"] + rendered.splitlines()
            if name in {"verify", "verify-html", "qualify-stale"}:
                # Show why a gate skipped or failed, never a bare state.
                lines += [
                    f"{gate['gate']}: {gate['state']}"
                    + ("" if gate["state"] == "pass" else f" ({gate.get('detail')})")
                    for gate in result["gates"]
                ]
        lines = wrap([clean(line) for line in lines])
        # Keep individual terminal passages readable; further fields remain in evidence.
        excerpts[name] = {
            "title": titles[name],
            "lines": lines[:28],
            "raw_capture_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
            "excerpted": True,
            "elapsed_seconds": record["elapsed_seconds"],
            "exit_code": record["exit_code"],
        }

    def source_excerpt(key, title, path, start, end):
        content = path.read_text()
        excerpts[key] = {
            "title": title,
            "lines": [clean(line) for line in content.splitlines()[start:end]],
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_lines": [start + 1, end],
            "excerpted": True,
        }

    component = args.project / "samples/hello-component/component.md"
    content = component.read_text().splitlines()
    start = next(
        i for i, line in enumerate(content) if line == "### Public invocation contract"
    )
    source_excerpt(
        "specification",
        "The actual authored greeting-card contract",
        component,
        start,
        start + 28,
    )
    source_excerpt(
        "source", "Generated Python: main(request)", args.source / "main.py", 0, 28
    )
    source_excerpt(
        "tests",
        "Agent-generated native behavior tests",
        args.source / "tests/litai_test.py",
        0,
        23,
    )
    oracle = "verification/acceptance/hello-component.json"
    # The title states whether this recording changed the inherited oracle.
    history = subprocess.run(
        ["git", "log", "--format=%H", "--", oracle],
        cwd=args.project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    source_excerpt(
        "oracle",
        "Demo-local acceptance correction (not framework fix)"
        if len(history) > 1
        else "Independent acceptance cases, as inherited (unmodified)",
        args.project / oracle,
        5,
        28,
    )
    attempt = excerpts.get("rebuild-failed") or excerpts["rebuild"]
    excerpts["generation"] = {
        "title": "Recorded agent generation: waiting compressed",
        "lines": [
            *attempt["lines"][:2],
            "",
            f"Coding CLI: {args.coding_cli}",
            f"Requested model: {args.model_selector}",
            "The first lifecycle result is shown after code inspection.",
            "Waiting compressed; no fabricated live typing.",
        ],
        "raw_capture_sha256": attempt["raw_capture_sha256"],
    }
    # Show structure without redistributing the library's implementation.
    impl = args.tinyxml2 / "components/legacy-project-wrapper/implementation"
    excerpts["cpp-source"] = {
        "title": "Preserved TinyXML2 implementation",
        "lines": [
            "$ ls components/legacy-project-wrapper/implementation",
            *sorted(
                x.name
                for x in impl.iterdir()
                if x.name
                in {
                    "tinyxml2.cpp",
                    "tinyxml2.h",
                    "xmltest.cpp",
                    "CMakeLists.txt",
                    "LICENSE.txt",
                    "readme.md",
                }
            ),
            "",
            "Original source remains release authority.",
        ],
        "observed": True,
    }
    retained = json.loads(args.retained_evidence.read_text())
    lines = []
    for phase in retained["phases"]:
        lines += [
            "$ " + phase["command"],
            "exit_code: " + str(phase["exit_code"]),
            "test_collection:",
        ]
        lines += json.dumps(phase["test_collection"], indent=2).splitlines()
    excerpts["ctest"] = {
        "title": "Actual retained-harness phase results",
        "lines": lines,
        "evidence_sha256": hashlib.sha256(
            args.retained_evidence.read_bytes()
        ).hexdigest(),
    }
    if args.gate_log:
        # A recorded full gate run; labeled with the revision it proved.
        log = args.gate_log.read_text()
        # Top-level gates only, in order, from the runner's own argument list.
        gates = re.search(r"run_checkpointed_gates\.py .*?--make \S+ (.+)$", log, re.M)
        names = set(gates.group(1).split()) if gates else set()
        lines = [
            f"Recorded release-check, macOS, revision {args.gate_revision}",
            "$ make release-check",
        ]
        for line in log.splitlines():
            if line.startswith("checkpoint: run ") and line[16:] in names:
                lines.append(line)
            elif re.match(r"^(Ran \d+ tests|OK \(|FAILED)", line):
                lines.append(line)
        if log.rstrip().endswith(
            "checkpoint: complete; reset to the beginning for the next run"
        ):
            lines.append("checkpoint: complete")
        samples = set(re.findall(r"^sample: (\S+) passed$", log, re.MULTILINE))
        lines.append(f"samples passed: {len(samples)}")
        receipts = re.findall(
            r'"summary":(\{"failed":\d+,"passed":\d+,"skipped":\d+,"total":\d+\})',
            log,
        )
        if receipts:
            lines.append("test receipt summary: " + receipts[-1])
        if args.windows_line:
            lines += [
                "",
                "Windows worker, same revision (reported):",
                args.windows_line,
            ]
        excerpts["release-check-recorded"] = {
            "title": f"Recorded full gate run at {args.gate_revision}",
            "lines": [clean(line) for line in lines],
            "log_sha256": hashlib.sha256(args.gate_log.read_bytes()).hexdigest(),
            "excerpted": True,
            "recorded_revision": args.gate_revision,
        }
    for entry in excerpts.values():
        entry["lines"] = wrap(entry["lines"])
    doc = {
        "schema": "literate-ai/course-session-excerpts@1",
        "framework_version": args.framework_version,
        "framework_source": args.framework_source,
        "coding_cli": args.coding_cli,
        "model_selector": args.model_selector,
        "model_resolution": (
            "selector requested; no separate provider-resolved model identity claimed"
        ),
        "parent": args.parent,
        "tinyxml2": "https://github.com/leethomason/tinyxml2#8224e427b655b83dae5e2298f1e6919523a78737",
        "recording": (
            "edited terminal replays of actual command captures; "
            "not unedited screen recordings"
        ),
        "excerpts": excerpts,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(doc, indent=2) + "\n")


if __name__ == "__main__":
    main()
