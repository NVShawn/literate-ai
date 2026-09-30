const OPENING_FENCE = /^( {0,3})(`{3,}|~{3,})(.*)$/;
const CLOSING_FENCE = /^( {0,3})(`+|~+)[\t ]*$/;

function linesOf(markdown) {
  return markdown.split(/\r\n|\n|\r/);
}

function openingFence(line) {
  const match = OPENING_FENCE.exec(line);
  if (match === null) {
    return null;
  }
  const marker = match[2];
  const info = match[3].trim();
  if (marker[0] === "`" && info.includes("`")) {
    return null;
  }
  return {
    character: marker[0],
    indent: match[1].length,
    length: marker.length,
    info,
  };
}

function isClosingFence(line, opening) {
  const match = CLOSING_FENCE.exec(line);
  return (
    match !== null &&
    match[2][0] === opening.character &&
    match[2].length >= opening.length
  );
}

function stripOpeningIndent(line, indent) {
  let removed = 0;
  while (removed < indent && line[removed] === " ") {
    removed += 1;
  }
  return line.slice(removed);
}

function languageOf(info) {
  return info.split(/[\t ]+/, 1)[0];
}

/**
 * Extract Mermaid fenced code blocks using the fenced-block rules relevant to
 * CommonMark: backtick or tilde markers, at least three markers, up to three spaces
 * of indentation, a same-character closing fence at least as long as the opener,
 * and EOF as an implicit close.
 *
 * Returned line numbers are one-based source lines. `contentStartLine` points to the
 * first diagram line even when the block is empty.
 */
export function extractMermaidFences(markdown, sourcePath = "<memory>") {
  if (typeof markdown !== "string") {
    throw new TypeError("Markdown content must be a string");
  }
  if (typeof sourcePath !== "string" || sourcePath.length === 0) {
    throw new TypeError("Markdown source path must be a non-empty string");
  }

  const lines = linesOf(markdown);
  const diagrams = [];
  let lineIndex = 0;
  while (lineIndex < lines.length) {
    const opening = openingFence(lines[lineIndex]);
    if (opening === null) {
      lineIndex += 1;
      continue;
    }

    const fenceLine = lineIndex + 1;
    const contentStartLine = fenceLine + 1;
    const content = [];
    lineIndex += 1;
    let closingLine = null;
    while (lineIndex < lines.length) {
      if (isClosingFence(lines[lineIndex], opening)) {
        closingLine = lineIndex + 1;
        lineIndex += 1;
        break;
      }
      content.push(stripOpeningIndent(lines[lineIndex], opening.indent));
      lineIndex += 1;
    }

    if (languageOf(opening.info) === "mermaid") {
      diagrams.push({
        sourcePath,
        fenceLine,
        contentStartLine,
        closingLine,
        definition: content.join("\n"),
      });
    }
  }
  return diagrams;
}

/** Map a Mermaid-local parser line back to its Markdown source line. */
export function sourceLineForMermaidError(diagram, error) {
  const message = error instanceof Error ? error.message : String(error);
  const match = /\bline\s+(\d+)\b/i.exec(message);
  if (match === null) {
    return diagram.fenceLine;
  }
  const localLine = Number.parseInt(match[1], 10);
  return Number.isSafeInteger(localLine) && localLine > 0
    ? diagram.contentStartLine + localLine - 1
    : diagram.fenceLine;
}

export function conciseMermaidError(error) {
  const message = error instanceof Error ? error.message : String(error);
  const normalized = message.replace(/\s+/g, " ").trim();
  if (normalized.length <= 800) {
    return normalized;
  }
  return `${normalized.slice(0, 797)}...`;
}
