import assert from "node:assert/strict";
import test from "node:test";

import {
  conciseMermaidError,
  extractMermaidFences,
  sourceLineForMermaidError,
} from "../scripts/mermaid-fences.mjs";

test("extracts a backtick Mermaid fence with one-based source lines", () => {
  const markdown = [
    "# Flow",
    "",
    "```mermaid",
    "flowchart LR",
    "  A --> B",
    "```",
    "",
  ].join("\n");

  assert.deepEqual(extractMermaidFences(markdown, "docs/flow.md"), [
    {
      sourcePath: "docs/flow.md",
      fenceLine: 3,
      contentStartLine: 4,
      closingLine: 6,
      definition: "flowchart LR\n  A --> B",
    },
  ]);
});

test("supports tilde fences, extra info, indentation, and a longer close", () => {
  const markdown = [
    "  ~~~~mermaid title=flow",
    "  sequenceDiagram",
    "    A->>B: hello",
    "  ~~~~~",
  ].join("\r\n");

  assert.deepEqual(extractMermaidFences(markdown, "windows.md"), [
    {
      sourcePath: "windows.md",
      fenceLine: 1,
      contentStartLine: 2,
      closingLine: 4,
      definition: "sequenceDiagram\n  A->>B: hello",
    },
  ]);
});

test("a shorter fence remains diagram content until a valid close", () => {
  const markdown = [
    "````mermaid",
    "flowchart TD",
    "```",
    "A --> B",
    "`````",
  ].join("\n");

  assert.equal(
    extractMermaidFences(markdown)[0].definition,
    "flowchart TD\n```\nA --> B"
  );
});

test("ignores Mermaid-looking fences inside another fenced block", () => {
  const markdown = [
    "~~~text",
    "```mermaid",
    "flowchart LR",
    "```",
    "~~~",
  ].join("\n");

  assert.deepEqual(extractMermaidFences(markdown), []);
});

test("rejects a backtick opener whose info string contains a backtick", () => {
  const markdown = [
    "```mermaid`invalid",
    "flowchart LR",
    "```",
  ].join("\n");

  assert.deepEqual(extractMermaidFences(markdown), []);
});

test("treats EOF as the close for an unclosed CommonMark fence", () => {
  const diagrams = extractMermaidFences(
    "before\n```mermaid\ngraph TD\nA-->B",
    "eof.md"
  );

  assert.equal(diagrams.length, 1);
  assert.equal(diagrams[0].closingLine, null);
  assert.equal(diagrams[0].definition, "graph TD\nA-->B");
});

test("only the exact lowercase Mermaid info token is selected", () => {
  const markdown = [
    "```Mermaid",
    "graph TD",
    "```",
    "```mermaid-example",
    "graph LR",
    "```",
  ].join("\n");

  assert.deepEqual(extractMermaidFences(markdown), []);
});

test("maps a Mermaid-local parse line to the Markdown source line", () => {
  const [diagram] = extractMermaidFences(
    "heading\n\n```mermaid\ngraph TD\nA -> B\n```",
    "docs/broken.md"
  );

  assert.equal(
    sourceLineForMermaidError(diagram, new Error("Parse error on line 2:")),
    5
  );
  assert.equal(
    sourceLineForMermaidError(diagram, new Error("unknown diagram type")),
    3
  );
});

test("normalizes and bounds renderer diagnostics", () => {
  assert.equal(conciseMermaidError(new Error("bad\n  diagram")), "bad diagram");
  assert.equal(conciseMermaidError("x".repeat(900)).length, 800);
  assert.match(conciseMermaidError("x".repeat(900)), /\.\.\.$/u);
});
