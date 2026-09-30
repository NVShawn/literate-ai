import assert from "node:assert/strict";
import test from "node:test";

import { hostedUbuntuSandboxUnavailable } from "../scripts/check-mermaid.mjs";

const sandboxError = new Error("Chromium failed: No usable sandbox!");

test("allows the no-sandbox retry only on GitHub-hosted Linux", () => {
  assert.equal(
    hostedUbuntuSandboxUnavailable([sandboxError], {
      platform: "linux",
      environment: {
        GITHUB_ACTIONS: "true",
        RUNNER_ENVIRONMENT: "github-hosted",
      },
    }),
    true
  );
  for (const [platform, environment] of [
    ["darwin", { GITHUB_ACTIONS: "true", RUNNER_ENVIRONMENT: "github-hosted" }],
    ["linux", { GITHUB_ACTIONS: "true", RUNNER_ENVIRONMENT: "self-hosted" }],
    ["linux", { GITHUB_ACTIONS: "false", RUNNER_ENVIRONMENT: "github-hosted" }],
  ]) {
    assert.equal(
      hostedUbuntuSandboxUnavailable([sandboxError], { platform, environment }),
      false
    );
  }
});

test("does not weaken the hosted runner when the sandbox failed for another reason", () => {
  assert.equal(
    hostedUbuntuSandboxUnavailable([new Error("browser missing")], {
      platform: "linux",
      environment: {
        GITHUB_ACTIONS: "true",
        RUNNER_ENVIRONMENT: "github-hosted",
      },
    }),
    false
  );
});
