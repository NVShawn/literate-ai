#!/usr/bin/env node

import { mkdtemp, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";
import { TextDecoder } from "node:util";
import { pathToFileURL } from "node:url";

import { renderMermaid } from "@mermaid-js/mermaid-cli";
import puppeteer from "puppeteer";

import {
  conciseMermaidError,
  extractMermaidFences,
  sourceLineForMermaidError,
} from "./mermaid-fences.mjs";

const SKIPPED_DIRECTORIES = new Set([
  ".codegraph",
  ".git",
  ".mypy_cache",
  ".nox",
  ".pytest_cache",
  ".ruff_cache",
  ".tox",
  ".venv",
  "__pycache__",
  "_build",
  "dist",
  "node_modules",
  "venv",
]);
const UTF8 = new TextDecoder("utf-8", { fatal: true });

async function markdownFiles(root) {
  const files = [];
  async function visit(directory) {
    const entries = await readdir(directory, { withFileTypes: true });
    entries.sort((left, right) => left.name.localeCompare(right.name, "en"));
    for (const entry of entries) {
      const candidate = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        continue;
      }
      if (entry.isDirectory()) {
        if (!SKIPPED_DIRECTORIES.has(entry.name)) {
          await visit(candidate);
        }
        continue;
      }
      if (
        entry.isFile() &&
        [".md", ".markdown"].includes(path.extname(entry.name).toLowerCase())
      ) {
        files.push(candidate);
      }
    }
  }
  await visit(root);
  return files;
}

function sourceLabel(root, filename) {
  return path.relative(root, filename).split(path.sep).join("/");
}

function isSvg(content) {
  const text = content.toString("utf-8");
  return /<svg(?:\s|>)/u.test(text) && /<\/svg>\s*$/u.test(text);
}

export function hostedUbuntuSandboxUnavailable(
  errors,
  { platform = process.platform, environment = process.env } = {}
) {
  return (
    platform === "linux" &&
    environment.GITHUB_ACTIONS === "true" &&
    environment.RUNNER_ENVIRONMENT === "github-hosted" &&
    errors.some((error) =>
      conciseMermaidError(error).includes("No usable sandbox")
    )
  );
}

async function launchBrowser() {
  try {
    return await puppeteer.launch({ headless: true });
  } catch (defaultError) {
    // A system Chrome is a portable fallback when Puppeteer's downloaded browser is
    // unavailable or has been evicted. `channel` performs Puppeteer's OS-specific
    // discovery and retains the same sandbox defaults.
    try {
      return await puppeteer.launch({ channel: "chrome", headless: true });
    } catch (channelError) {
      // GitHub's hosted Ubuntu image may disable unprivileged user namespaces and
      // provide no setuid Chromium sandbox. Retry without that unavailable layer
      // only in the explicitly identified ephemeral hosted runner; local and
      // self-hosted environments retain Puppeteer's sandbox defaults.
      const hostedUbuntuWithoutSandbox = hostedUbuntuSandboxUnavailable([
        defaultError,
        channelError,
      ]);
      if (hostedUbuntuWithoutSandbox) {
        return await puppeteer.launch({
          headless: true,
          args: ["--no-sandbox", "--disable-setuid-sandbox"],
        });
      }
      throw new Error(
        "Puppeteer could not launch its managed browser or the system Chrome channel: " +
          `${conciseMermaidError(defaultError)}; ` +
          conciseMermaidError(channelError),
        { cause: channelError }
      );
    }
  }
}

async function main() {
  const configuredRoot = process.argv[2] ?? path.join(import.meta.dirname, "../../..");
  const repositoryRoot = path.resolve(configuredRoot);
  const rootMetadata = await stat(repositoryRoot);
  if (!rootMetadata.isDirectory()) {
    throw new Error(`documentation root is not a directory: ${repositoryRoot}`);
  }

  const files = await markdownFiles(repositoryRoot);
  const diagrams = [];
  for (const filename of files) {
    const label = sourceLabel(repositoryRoot, filename);
    let markdown;
    try {
      markdown = UTF8.decode(await readFile(filename));
    } catch (error) {
      throw new Error(`${label}:1: Markdown is not valid UTF-8`, { cause: error });
    }
    diagrams.push(...extractMermaidFences(markdown, label));
  }
  if (diagrams.length === 0) {
    throw new Error("documentation contains no Mermaid fenced diagrams");
  }

  const temporaryRoot = await mkdtemp(
    path.join(tmpdir(), "literate-ai-mermaid-")
  );
  let browser;
  const failures = [];
  try {
    // Intentionally keep Puppeteer's sandbox defaults. One browser is shared by all
    // renders; mermaid-cli opens and closes a page for each isolated diagram.
    browser = await launchBrowser();
    for (const [index, diagram] of diagrams.entries()) {
      try {
        const rendered = await renderMermaid(
          browser,
          diagram.definition,
          "svg",
          {
            backgroundColor: "white",
            mermaidConfig: {
              deterministicIds: true,
              deterministicIDSeed: `literate-ai-${index + 1}`,
              securityLevel: "strict",
              startOnLoad: false,
            },
            svgId: `literate-ai-diagram-${index + 1}`,
          }
        );
        const svg = Buffer.from(rendered.data);
        const output = path.join(temporaryRoot, `diagram-${index + 1}.svg`);
        await writeFile(output, svg);
        const persisted = await readFile(output);
        if (persisted.length === 0 || !isSvg(persisted)) {
          throw new Error("renderer did not produce a complete SVG document");
        }
      } catch (error) {
        failures.push(
          `${diagram.sourcePath}:${sourceLineForMermaidError(diagram, error)}: ` +
            `error: Mermaid render failed: ${conciseMermaidError(error)}`
        );
      }
    }
  } finally {
    try {
      await browser?.close();
    } finally {
      await rm(temporaryRoot, { force: true, recursive: true });
    }
  }

  if (failures.length > 0) {
    throw new Error(failures.join("\n"));
  }
  console.log(
    `Rendered ${diagrams.length} Mermaid diagram(s) from ${files.length} Markdown file(s).`
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    await main();
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
