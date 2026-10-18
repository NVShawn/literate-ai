import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { markdownFiles } from "../scripts/markdown-files.mjs";

test("Git documentation ignores build output, untracked Markdown and nested worktrees", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "litai-markdown-"));
  try {
    const git = (...args) => execFileSync("git", ["-C", root, ...args]);
    git("init", "--quiet");
    await writeFile(path.join(root, "tracked file.md"), "```mermaid\ngraph LR; A-->B\n```\n");
    await writeFile(path.join(root, "UPPER.MD"), "# Upper\n");
    await writeFile(path.join(root, ".gitignore"), "generated/\n.worktrees/\n");
    git("add", ".");
    const expected = ["tracked file.md", "UPPER.MD"].map(name => path.join(root, name)).sort((a,b) => a.localeCompare(b,"en"));
    assert.deepEqual(await markdownFiles(root), expected);
    for (const name of ["generated", ".worktrees"]) {
      await mkdir(path.join(root, name));
      await writeFile(path.join(root, name, "unreviewed.md"), "```mermaid\ninvalid ignored diagram\n```\n");
    }
    await writeFile(path.join(root, "untracked.md"), "# Untracked\n");
    assert.deepEqual(await markdownFiles(root), expected);
    // Tracked local edits are still inspected, and deleted files are absent.
    await writeFile(expected[0], "# Changed\n");
    await rm(expected[1]);
    assert.deepEqual(await markdownFiles(root), [expected[0]]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("exported trees retain the directory-walk fallback", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "litai-export-docs-"));
  try {
    await writeFile(path.join(root, "README.markdown"), "# Export\n");
    await mkdir(path.join(root, "_build"));
    await writeFile(path.join(root, "_build", "ignored.md"), "# Build\n");
    assert.deepEqual(await markdownFiles(root), [path.join(root, "README.markdown")]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
