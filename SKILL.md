---
name: literate-ai
description: Use Literate AI to create, change, generate, validate, build, test, review, or explain specification-led Components, Flavors, skills, workflows, routing policies, and project layouts. Use whenever a repository contains literate.project.json or the user asks to work with the Literate AI framework or one of its projects.
metadata:
  author: Literate AI maintainers <literate-ai-maintainers@users.noreply.github.com>
---

# Literate AI

Treat specifications as application authority and generated source as disposable output.
The operator front door is `litai doctor`, then `litai onboard create` for a new
tree or `litai onboard adopt` for an existing tree. Inspect the read-only plan,
apply it only with acknowledgement, then use `litai status`, `verify`, `lock`,
`plan`, `rebuild`, and versioned `release`. The lower-level `litai init` and
`litai init --convert` mutators remain available. Run `litai help` for flags,
errors, and identity rules. Do not restate those in this file.

## Keep Windows-portable paths

Use short, shallow, conservative names. Windows `MAX_PATH` is 260 characters for the
full absolute path. Applications can declare long-path awareness, and a machine's
registry can be changed to match, but do not assume the target host has done either.
Avoid reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`,
`LPT1`–`LPT9`), characters `<>:"/\|?*`, and trailing dots or spaces in any segment.

## Follow agent skills by path

Do not copy `skills/agent/` into this file. `skills/agent/SKILL.md` is the parent
catalog: wrap `litai`, keep nested skills as deltas, and open **only** the nested
`SKILL.md` that matches the current task. The directories under `skills/agent/`
are the inventory; [skill architecture](docs/architecture/skills.md) names them.
There is no persistent Literate AI daemon. Follow
`skills/agent/configure-operator-mcp/SKILL.md` for the operator catalog;
`skills/agent/associate-release-jira/SKILL.md` and
`skills/agent/ingest-channel-work/SKILL.md` when those MCPs apply. Mutagenic posts
are `litai`'s job after journal success; do not re-post envelopes the CLI already
sent.

## Follow the project flow

1. Follow `skills/agent/track-project-requirements/SKILL.md` then
   `skills/agent/record-user-directed-work/SKILL.md`. Conversation history is not
   project authority. Run `litai project tracker inspect` before reconciling issues
   or review requests (`gh` on GitHub remotes, `glab` on GitLab). When a derived
   project discovers reusable parent-owned work, follow the record skill\'s issue-first
   rule: search or file the sanitized upstream issue before implementation, while
   retaining every explicit external-write authorization gate. At the start and
   end of a `dev` cycle, follow
   `skills/agent/develop-in-production-workflow/staging/dev/survey-peer-work/SKILL.md`.
   When a user works directly through Literate AI or a supported coding agent, use
   `skills/agent/prompt-master/SKILL.md` or `litai prompt translate`. Do not apply
   that translation to a task envelope an external task system already translated
   (`LITAI_EXTERNAL_TASK_ID`); that system owns its task-to-provider translation.
   By default the forge hosting the repository that receives issues and pull
   requests (GitHub or GitLab) is the project's tracker: report problems as issues,
   submit changes as PRs/MRs against the target branch that
   `litai project guidance --operation land` names, and coordinate with other agents
   through issues. Another tracker or review system (for example Gerrit) is a project
   or user override, never the default (ADR 0050).
2. Run `litai verify` for every declared gate in one call. It writes nothing and
   builds nothing. `litai rebuild` runs the project's build harness. `litai update`
   reconciles inherited or framework-owned files; `litai reparent` changes parent
   authority. After an intentional non-editable framework-wheel upgrade, use the
   reviewed `litai project lifecycle rebind-standard` plan/apply flow before rebuild;
   never hand-edit or silently advance a Standard distribution pin during update.
   `litai project validate` is the authority gate alone. `litai onboard` calls the
   existing `litai init` / `litai init --convert` mutators after an acknowledged,
   revalidated plan; those mutators and `litai catalog copy` continue to own their
   selectors, deadlines, and fail-closed collisions. Follow
   `skills/agent/convert-project/SKILL.md` after adoption.
3. Read the selected `component.md`, its local behavioral documents, direct public
   interface contracts, and acceptance contract. Do not flatten transitive internals.
   `litai lock` and `litai plan` own resolution, assets, and identities.
4. Change specifications, Flavors, or pinned skills to change intent. Generated
   source lives under advisory `BUILD_DIR`/`OBJ_DIR`; see
   `skills/specification-to-source/repository-layout/SKILL.md`.
5. Obtain acknowledgement before compiling or running generated host code. Use
   `litai rebuild` for the authorized lifecycle and `litai generate` only to stop at
   source. Cache hits stay untrusted until current indexing, validation, build,
   tests, execution, and independent acceptance pass.
6. Inverse work is `litai spec …`. Representative proof is `make roundtrip-host`;
   fan-out is `make samples-platform-regression`. Follow
   `skills/agent/configure-test-workers/SKILL.md`, then align every worker with
   `skills/agent/align-workers/SKILL.md` (`litai worker align`) before dispatch.
   Never commit hostnames, credentials, or private routing skills. For complex
   projects where minimum turnaround matters, prefer this host, then aligned
   `workers.json` workers, over hosted CI/CD; use CI/CD only for platform and
   action cells those cannot cover or when CI is marked mandatory.
7. Long ladders checkpoint under ignored `OBJ_DIR` via the project's Make/`litai`
   runner. A resumed repair is not release attestation.
8. For a versioned release, follow `skills/agent/release-project/SKILL.md` and
   `literate.release.json`. Land through
   `skills/agent/develop-in-production-workflow/staging/dev/land/SKILL.md`.
   Packaging is `skills/agent/package-artifacts/SKILL.md`. A project built by an
   external MAC runner opts into `skills/agent/write-mac-project-contract/SKILL.md`;
   `litai init` does not install it. Host tools are
   `skills/agent/preflight-host-toolchains/SKILL.md`. Lifecycle identities are
    `skills/agent/bind-lifecycle-evidence/SKILL.md`.
    Rendered frontend inspection is
    `skills/agent/verify-frontend-browser/SKILL.md` after execution authorization.

When verifying this repository's Python package, use `make python-check` (session
venv under `$(OBJ_DIR)/python-envs/$(LITAI_SESSION_KEY)`), not an ad hoc venv.

## Author specifications without boilerplate

Keep each Component focused on observable product intent. Start with one
`component.md`. Exact selection belongs in generated `component.lock.json`. Put OS,
language, toolchain, packaging, and build-system choices in Flavors; conversion
practice in specification-to-source skills; cross-Component relationships in
capability requirements and public contracts. See
[Writing readable specifications](docs/user/specifications.md).

## Preserve authority boundaries

- A weaker model gets no relaxed constraints. Fix defects where authority lives.
- Specifications decide observable behavior; Flavors decide target requirements;
  pinned skills guide technique; workflows and routing own stage order and models.
- Never let a skill, model, or Flavor weaken validation, security, or execution
  authorization. Fail closed on missing content, digest drift, dependency mismatch,
  ambiguous Flavors, or generated application output outside `source/`.

## Read only the relevant detail

Use Markdown under declared `documentation_roots`. Start at
[the documentation index](docs/README.md), then
[getting started](docs/user/getting-started.md) and
[the framework flow](docs/user/framework-flow.md). Run `make skills-check` before
committing a changed skill. Do not copy detailed protocols into this entry point.
