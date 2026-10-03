#!/usr/bin/env node
// Fail on high or critical npm advisories unless one reviewed, unexpired exception
// names that exact advisory and package. An unavailable audit always fails.
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const GATING = new Set(["high", "critical"]);
const EXCEPTION_FIELDS = ["advisory", "package", "reason", "issue", "expires"];
const DATE = /^\d{4}-\d{2}-\d{2}$/;

function advisoryId(url) {
  const match = /\/(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})$/i.exec(url ?? "");
  return match ? match[1] : null;
}

export function gatingAdvisories(report) {
  if (!report || typeof report !== "object" || report.error) {
    throw new Error(`npm audit unavailable: ${JSON.stringify(report?.error ?? report)}`);
  }
  if (!report.vulnerabilities || !report.metadata?.vulnerabilities) {
    throw new Error("npm audit report lacks vulnerability evidence");
  }
  const found = [];
  for (const [name, vulnerability] of Object.entries(report.vulnerabilities)) {
    for (const via of vulnerability.via ?? []) {
      if (typeof via !== "object" || !GATING.has(via.severity)) continue;
      found.push({
        advisory: advisoryId(via.url) ?? String(via.source),
        package: via.name ?? name,
        severity: via.severity,
        title: via.title ?? "",
      });
    }
  }
  return found;
}

export function auditViolations(report, exceptions, today) {
  const violations = [];
  const valid = new Map();
  for (const [index, item] of exceptions.entries()) {
    const missing = EXCEPTION_FIELDS.filter(
      (field) => typeof item?.[field] !== "string" || !item[field].trim(),
    );
    if (missing.length || !DATE.test(item.expires)) {
      violations.push(`exception ${index} is invalid (${missing.join(", ") || "expires"})`);
      continue;
    }
    if (item.expires < today) {
      violations.push(`exception for ${item.advisory} (${item.package}) expired on ${item.expires}`);
      continue;
    }
    valid.set(`${item.advisory}\u0000${item.package}`, item);
  }
  const used = new Set();
  for (const finding of gatingAdvisories(report)) {
    const key = `${finding.advisory}\u0000${finding.package}`;
    if (valid.has(key)) {
      used.add(key);
    } else {
      violations.push(
        `${finding.severity} ${finding.advisory} in ${finding.package}: ${finding.title}`,
      );
    }
  }
  for (const [key, item] of valid) {
    if (!used.has(key)) {
      violations.push(
        `exception for ${item.advisory} (${item.package}) no longer matches a finding; remove it`,
      );
    }
  }
  return violations;
}

function main() {
  const root = dirname(dirname(fileURLToPath(import.meta.url)));
  const exceptions = JSON.parse(
    readFileSync(join(root, "audit-exceptions.json"), "utf8"),
  ).exceptions;
  const npm = process.platform === "win32" ? "npm.cmd" : "npm";
  const audit = spawnSync(npm, ["audit", "--json"], {
    cwd: root,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
    shell: process.platform === "win32",
  });
  let report;
  try {
    report = JSON.parse(audit.stdout);
  } catch {
    console.error(`npm audit unavailable (exit ${audit.status}): ${audit.stderr || audit.error}`);
    process.exit(1);
  }
  let violations;
  try {
    violations = auditViolations(report, exceptions, new Date().toISOString().slice(0, 10));
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
  for (const item of exceptions) {
    console.log(`excepted ${item.advisory} (${item.package}) until ${item.expires}: ${item.issue}`);
  }
  if (violations.length) {
    for (const violation of violations) console.error(violation);
    process.exit(1);
  }
  console.log("npm audit: no unexcepted high or critical advisories");
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main();
