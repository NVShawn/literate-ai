"""Select factual excerpts from local captures; never fabricate execution results."""

import argparse
import hashlib
import json
import textwrap
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--captures", type=Path, required=True)
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--retained-evidence", type=Path, required=True)
    p.add_argument("--tinyxml2", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
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
            "tests",
            "coding_cli",
            "source",
            "test_count",
        ],
        "rebuild-failed": [],
        "verify": ["counts", "ok"],
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
    }
    titles = {
        "create-plan": "Inspect before creating",
        "create-apply": "Apply the reviewed create plan",
        "adopt-plan": "TinyXML2: inspect the existing project",
        "adopt-apply": "TinyXML2: wrap and preserve",
        "rebuild": "Real agent generation and lifecycle result",
        "rebuild-failed": "A real refusal: inherited oracle mismatch",
        "verify": "Current verification, including skips",
        "package": "Build the local application wheel",
        "package-verify": "Verify the wheel",
        "retained": "Retained C++ build and test evidence",
        "retained-publish": "Commit the local finalized receipt",
        "retained-stage": "Retained does not mean rewritten",
        "update": "Read-only project update plan",
    }
    paths = [
        str(args.project.resolve()),
        str(args.source.resolve()),
        str(args.tinyxml2.resolve()),
        str(Path.cwd()),
        str(Path.home()),
    ]

    def clean(value):
        for index, path in enumerate(paths):
            value = value.replace(
                path,
                [
                    "<project>",
                    "<generated-source>",
                    "<tinyxml2>",
                    "<workspace>",
                    "<user>",
                ][index],
            )
        if any(
            s in value.lower()
            for s in ("nvidia-dev", "gitlab-master", "bearer ", "api_key=", "token=")
        ):
            raise ValueError("Private-looking excerpt: manual review required")
        return value

    for name, fields in selected.items():
        file = args.captures / f"{name}.json"
        record = json.loads(file.read_text())
        if record["exit_code"] != 0 and name != "rebuild-failed":
            raise ValueError(
                f"Cannot imply success for {name}: exit {record['exit_code']}"
            )
        document = json.loads(record["stdout"] or record["stderr"])
        result = document.get("result", {})
        command = ["litai", *record["argv"][1:]]
        lines = textwrap.wrap("$ " + " ".join(command), 94, subsequent_indent="  ")
        lines += [
            f"exit_code: {record['exit_code']}",
            f"recorded duration: {record['elapsed_seconds']} seconds",
            f"ok: {str(document.get('ok')).lower()}",
        ]
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
        if name == "verify":
            lines += [f"{gate['gate']}: {gate['state']}" for gate in result["gates"]]
        lines = [clean(line) for line in lines]
        lines = [
            wrapped
            for line in lines
            for wrapped in (
                textwrap.wrap(line, 94, replace_whitespace=False, drop_whitespace=False)
                or [""]
            )
        ]
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
    source_excerpt(
        "oracle",
        "Demo-local acceptance correction (not framework fix)",
        args.project / "verification/acceptance/hello-component.json",
        5,
        28,
    )
    excerpts["generation"] = {
        "title": "Recorded agent generation: waiting compressed",
        "lines": [
            *excerpts["rebuild-failed"]["lines"][:2],
            "",
            "Coding CLI: Codex",
            "Requested model: gpt-5.6-sol",
            "The first lifecycle result is shown after code inspection.",
            "Waiting compressed; no fabricated live typing.",
        ],
        "raw_capture_sha256": excerpts["rebuild-failed"]["raw_capture_sha256"],
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
    for entry in excerpts.values():
        entry["lines"] = [
            part
            for line in entry["lines"]
            for part in (
                textwrap.wrap(line, 94, replace_whitespace=False, drop_whitespace=False)
                or [""]
            )
        ]
    doc = {
        "schema": "literate-ai/course-session-excerpts@1",
        "framework_version": "1.1.0 development wheel",
        "framework_source": "98042090",
        "coding_cli": "codex",
        "model_selector": "gpt-5.6-sol",
        "model_resolution": (
            "selector requested; no separate provider-resolved model identity claimed"
        ),
        "parent": "https://github.com/jordanhubbard/literate-ai#fcc40bc617a2bc2455627db7396a1e016ebfbab6",
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
