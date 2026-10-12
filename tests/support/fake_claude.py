"""Deterministic stand-in for the `claude` coding CLI at the model-provider boundary.

The source generator runs this in an empty workspace containing the generation
request. It writes a self-contained starter implementation, its generated test
suite and the prompt's minimal SBOM under `source/`, exactly as a model would.
Everything after generation (admission, build, test, execution, independent
acceptance and receipts) stays real. It imports nothing from the framework.
"""

import json
import re
import sys
from pathlib import Path

PROGRAM = '''"""Starter greeting implementation written by the fake coding CLI."""

import json
import sys

CARDS = {cards!r}
CASES = {cases!r}


def main(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
        raise ValueError("payload requires a name")
    name = payload["name"]
    if not CARDS:
        return {{"greeting": f"Hello, {{name}}!", "name": name}}
    messages = payload.get("messages")
    if not isinstance(messages, list) or not all(
        isinstance(item, str) for item in messages
    ):
        raise ValueError("payload requires messages")
    return {{
        "greeting": f"Hello, {{name}}!",
        "recipient_id": "-".join(name.casefold().split()),
        "message_count": len(messages),
        "word_count": sum(len(message.split()) for message in messages),
    }}


def _passed(case):
    return main(*case["arguments"]) == case["expected_result"]


if __name__ == "__main__":
    if sys.argv[1:] == ["--litai-test"]:
        print(
            json.dumps(
                {{
                    "schema": "literate-ai/generated-test-results@1",
                    "cases": [
                        {{
                            "case_id": case["case_id"],
                            "outcome": "passed" if _passed(case) else "failed",
                        }}
                        for case in CASES
                    ],
                }}
            )
        )
        raise SystemExit(0)
    if sys.argv[1:] == ["--litai-smoke"]:
        print(json.dumps(main(*CASES[0]["arguments"])))
        raise SystemExit(0 if _passed(CASES[0]) else 1)
    try:
        print(json.dumps(main(*json.loads(sys.argv[1]))))
    except (IndexError, TypeError, ValueError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(2)
'''

COPY = "import shutil,sys;shutil.copyfile(sys.argv[1],sys.argv[2])"
MAKEFILE = f""".PHONY: all test clean
all:
\t"$(LITAI_LANGUAGE_TOOL)" -B -c "{COPY}" main.py "$(EXPORT_PATH)"
test:
\t@true
clean:
\t@true
"""


def _expected(name, messages, cards):
    if not cards:
        return {"greeting": f"Hello, {name}!", "name": name}
    return {
        "greeting": f"Hello, {name}!",
        "recipient_id": "-".join(name.casefold().split()),
        "message_count": len(messages),
        "word_count": sum(len(message.split()) for message in messages),
    }


def _citable_paths(request):
    lines = request.splitlines()
    start = next(i for i, line in enumerate(lines) if "chosen only from:" in line)
    paths = []
    for line in lines[start + 1 :]:
        match = re.match(r"\s*- `([^`]+)`", line)
        if match is None:
            if paths:
                break
            continue
        paths.append(match.group(1))
    return paths


def generate(workspace):
    request = (workspace / ".literate-ai-generation-request.md").read_text("utf-8")
    recipe = re.search(r"Recipe identity: `([^`]+)`", request).group(1)
    entrypoint = re.search(
        r"The required generated entrypoint is\s+`([^`]+)`", request
    ).group(1)
    marker = request.index(
        '"$schema": "http://cyclonedx.org/schema/bom-1.7.schema.json"'
    )
    document, _end = json.JSONDecoder().raw_decode(
        request, request.rindex("{", 0, marker)
    )
    sbom = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    cards = "| `messages` |" in request
    references = _citable_paths(request)[:1]
    rows = (
        ("example", "Ada Lovelace", ["Build portable software"]),
        ("boundary", "X", []),
        ("invariant", "Grace Hopper", ["Debug boldly", "Run everywhere safely"]),
    )
    cases = [
        {
            "case_id": f"{category}-greeting",
            "category": category,
            "specification_refs": references,
            "arguments": [
                {"name": name, "messages": messages} if cards else {"name": name}
            ],
            "expected_result": _expected(name, messages, cards),
        }
        for category, name, messages in rows
    ]
    source = workspace / "source"
    (source / "tests").mkdir(parents=True)
    (source / ".literate").mkdir()
    (workspace / entrypoint).write_text(
        PROGRAM.format(cards=cards, cases=cases), "utf-8"
    )
    if "build.system=make" in request:
        (source / "Makefile").write_text(MAKEFILE, "utf-8")
    (source / "tests" / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "urn:literate-ai:schema:v1:generated-test-suite",
                "recipe_identity": recipe,
                "generation_mode": "major-rebuild",
                "cases": cases,
            },
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    (source / ".literate" / "sbom.cdx.json").write_text(sbom + "\n", "utf-8")


if __name__ == "__main__":
    try:
        generate(Path.cwd())
    except Exception:
        # The generator scrubs the environment; leave diagnostics beside the fake.
        import traceback

        failure = Path(__file__).with_name("last-failure.txt")
        request = Path.cwd() / ".literate-ai-generation-request.md"
        failure.write_text(
            traceback.format_exc()
            + "\n--- request ---\n"
            + (request.read_text("utf-8") if request.exists() else "(missing)"),
            "utf-8",
        )
        raise
    print("done")
    sys.exit(0)
