import { execFile } from "node:child_process";
import { lstat, readdir } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

const execute = promisify(execFile);
const skipped = new Set([
  ".codegraph", ".git", ".mypy_cache", ".nox", ".pytest_cache",
  ".ruff_cache", ".tox", ".venv", ".worktrees", "__pycache__",
  "_build", "dist", "node_modules", "venv",
]);

export async function markdownFiles(root) {
  const options = { cwd: root, encoding: "utf8", timeout: 10000, maxBuffer: 16 * 1024 * 1024 };
  let tracked = false;
  try {
    await execute("git", ["rev-parse", "--show-toplevel"], options);
    tracked = true;
  } catch (error) {
    if (error.code !== "ENOENT" && !String(error.stderr).includes("not a git repository")) {
      throw error;
    }
  }
  const files = [];
  if (tracked) {
    const { stdout } = await execute("git", [
      "ls-files", "--cached", "-z", "--", ":(icase)*.md", ":(icase)*.markdown",
    ], options);
    for (const relative of new Set(stdout.split("\0").filter(Boolean))) {
      let current = root;
      let metadata;
      for (const segment of relative.split("/")) {
        current = path.join(current, segment);
        try {
          metadata = await lstat(current);
        } catch (error) {
          if (error.code !== "ENOENT") throw error;
          metadata = undefined;
        }
        if (!metadata || metadata.isSymbolicLink()) {
          metadata = undefined;
          break;
        }
      }
      if (metadata?.isFile()) files.push(current);
    }
  } else {
    async function visit(directory) {
      for (const entry of await readdir(directory, { withFileTypes: true })) {
        const candidate = path.join(directory, entry.name);
        if (entry.isDirectory() && !skipped.has(entry.name)) await visit(candidate);
        else if (entry.isFile() && [".md", ".markdown"].includes(path.extname(entry.name).toLowerCase())) {
          files.push(candidate);
        }
      }
    }
    await visit(root);
  }
  return files.sort((left, right) => left.localeCompare(right, "en"));
}
