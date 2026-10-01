"""Request a tool-free narrative critique through the operator's Claude CLI."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    story = (base / "story.json").read_text()
    prompt = (
        "Independently critique this video script. Do not flatter the author. "
        "The user rejected an alternating lecture. Sam is a competent skeptical "
        "C++/make/tests engineer defending established practice, NOT the co-presenter. "
        "Litai is the female presenter selling the harness in TEN MINUTES, responding "
        "to tough objections by demonstrating progressively useful capabilities. "
        "Audience: skeptical software engineers, not novices. Keep dry humor, never "
        "condescend. Must explain Components, Flavors, scoped skills, repository "
        "inheritance, real greenfield code, brownfield adoption, updates, worker use, "
        "and distinction from orchestration. An explicit small next-step trial should "
        "be earned, not instant conversion. Rate dramatic causality, skepticism, "
        "clarity, factual restraint, visuals and pacing. Identify the five biggest "
        "problems and provide exact replacement turns with indices. Estimate runtime "
        "at 145-155 spoken words/minute plus pauses; suggest cuts to fit ten minutes. "
        "Assess whether the component/flavor/skill taxonomy is comprehensible to a "
        "first-time viewer. This is a sales demo, not a taxonomy lecture. Give a "
        "go/revise verdict. Do not invent demonstrated capabilities.\n\n"
        "FACTUAL CONSTRAINTS: recordings are existing pinned development-build "
        "sessions, not current-release proof. Setup/SSH/lineage/orchestration are "
        "walkthroughs, not fresh execution. The greeting starter exposed a wrong "
        "inherited oracle, corrected only in the demo and still tracked for upstream "
        "repair. TinyXML2 reaches retained; original source remains release authority. "
        "No deployed Agentis integration, cloud publishing, automatic arbitrary "
        "conflict merging, or guaranteed correct requirements is demonstrated. "
        "Review only the supplied text. No tools or repository changes.\n\nSCRIPT:\n"
        + story
    )
    argv = [
        args.claude,
        "-p",
        "--model",
        args.model,
        "--effort",
        "high",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--no-session-persistence",
        "--output-format",
        "json",
        "--system-prompt",
        "You are an independent skeptical technical script editor.",
    ]
    result = subprocess.run(
        argv, input=prompt, text=True, capture_output=True, timeout=900
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "review.json").write_text(result.stdout)
    (args.output / "stderr.txt").write_text(result.stderr)
    (args.output / "request.json").write_text(
        json.dumps(
            {
                "requested_model": args.model,
                "story_sha256": hashlib.sha256(story.encode()).hexdigest(),
                "prompt": prompt,
                "argv": argv,
                "exit_code": result.returncode,
            },
            indent=2,
        )
        + "\n"
    )
    print(result.stdout or result.stderr)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
