import assert from "node:assert/strict";
import test from "node:test";

import { auditViolations } from "../scripts/check-npm-audit.mjs";

const BRACES = "GHSA-vfj7-8cjw-p6xm";

function report(vias) {
  return {
    metadata: { vulnerabilities: { high: 1 } },
    vulnerabilities: {
      braces: { via: vias },
      micromatch: { via: ["braces"] },
    },
  };
}

function advisory(severity, id = BRACES) {
  return {
    source: 1,
    name: "braces",
    severity,
    title: "stack exhaustion",
    url: `https://github.com/advisories/${id}`,
  };
}

const exception = {
  advisory: BRACES,
  package: "braces",
  reason: "no patched release",
  issue: "https://github.com/jordanhubbard/literate-ai/issues/24",
  expires: "2026-11-15",
};

test("an unexcepted high advisory fails", () => {
  assert.deepEqual(auditViolations(report([advisory("high")]), [], "2026-10-03"), [
    `high ${BRACES} in braces: stack exhaustion`,
  ]);
});

test("a matching unexpired exception passes, including derived packages", () => {
  assert.deepEqual(auditViolations(report([advisory("high")]), [exception], "2026-10-03"), []);
});

test("an exception does not cover a different advisory or a critical finding elsewhere", () => {
  const other = "GHSA-aaaa-bbbb-cccc";
  assert.deepEqual(
    auditViolations(report([advisory("high"), advisory("critical", other)]), [exception], "2026-10-03"),
    [`critical ${other} in braces: stack exhaustion`],
  );
});

test("an expired exception fails", () => {
  assert.deepEqual(auditViolations(report([advisory("high")]), [exception], "2026-11-16"), [
    `exception for ${BRACES} (braces) expired on 2026-11-15`,
    `high ${BRACES} in braces: stack exhaustion`,
  ]);
});

test("an exception that no longer matches a finding fails", () => {
  assert.deepEqual(auditViolations(report([]), [exception], "2026-10-03"), [
    `exception for ${BRACES} (braces) no longer matches a finding; remove it`,
  ]);
});

test("low and moderate advisories do not gate", () => {
  assert.deepEqual(
    auditViolations(report([advisory("low"), advisory("moderate")]), [], "2026-10-03"),
    [],
  );
});

test("an incomplete exception is rejected", () => {
  assert.deepEqual(auditViolations(report([]), [{ advisory: BRACES }], "2026-10-03"), [
    "exception 0 is invalid (package, reason, issue, expires)",
  ]);
});

test("an unavailable audit is an error, not a pass", () => {
  assert.throws(
    () => auditViolations({ error: { code: "ENOTFOUND" } }, [], "2026-10-03"),
    /npm audit unavailable/,
  );
  assert.throws(() => auditViolations({}, [], "2026-10-03"), /lacks vulnerability evidence/);
});
