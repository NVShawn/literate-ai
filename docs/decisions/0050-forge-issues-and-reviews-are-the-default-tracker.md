# ADR 0050: Forge issues and reviews are the default tracker and coordination channel

- Status: Accepted
- Date: 2026-10-09
- Accepted: 2026-10-09 (repository owner direction)
- Decision owners: Literate AI maintainers
- Roadmap: TRACKER-DEFAULT-001 (1.2) and TRACKER-DEFAULT-002 (1.3) in
  `docs/roadmap/active-work.md`
- Relates to: ADR 0034 continuous evidence-bound release closure, which already lands
  through "the forge selected by repository tracker inspection"

## Context

`litai project tracker inspect` already detects the forge from the repository's remote
(GitHub or GitLab) and projects `gh` or `glab` commands for issues, reviews, CI and
landing. Agent guidance already files parent-owned defects as upstream issues and lands
through PRs/MRs.

Other parts of the framework still described an external agent ledger, MAC, as the
owner of tasks and agent coordination: the agent-ledger architecture, the
`prompt-master` routing boundary (`LITAI_MAC_TASK_ID`, `--mac-envelope`), and the
project `SKILL.md`. Every initialized project also received the MAC runner-contract
skill. Agents coordinate through a Markdown queue (`docs/roadmap/active-work.md`), with
issues reconciled into it only during planning passes. Landing named no target branch,
so a PR/MR silently targeted the forge's repository default even when the project's
policy named another branch.

## Decision

1. **Defaults.** The forge that hosts the repository receiving issues and pull requests
   is a Literate AI project's task and project-management system. It is GitHub or
   GitLab, as tracker inspection detects it from the repository's remote.
   - Problems are reported as issues.
   - Changes are submitted as PRs/MRs against an explicit target branch, the project's
     `repository_policy.default_branch`. `litai project guidance --operation land`
     passes it as `gh pr create --base` or `glab mr create --target-branch`.
   - Agents coordinate with each other through issues.
2. **Overrides.** A user or project preference may select another task tracker, an
   external agent ledger, or another review system such as Gerrit. These are explicit
   overrides and never the default. No framework code, template, or skill names a
   specific external ledger as the default coordination mechanism.
3. **External task envelopes are generic.** A task that an external system already
   translated is identified by `LITAI_EXTERNAL_TASK_ID` or
   `litai prompt translate --external-envelope`, and fails closed as
   `prompt_routing.external_envelope_bypass`. The 1.1 spellings `LITAI_MAC_TASK_ID` and
   `--mac-envelope` stay as deprecated aliases so an existing integration is never
   translated twice. A forge issue is not such an envelope; it is a direct request.
   The `prompt-task` envelope schema moves to `@2`, replacing `mac_bypass` with
   `external_envelope_bypass`.
4. **MAC is an opt-in runner contract.** `skills/agent/write-mac-project-contract/` and
   `litai project mac-contract` remain for projects built by MAC runners. `litai init`
   no longer installs that skill, and it covers build/test runner metadata only, never
   issues, reviews or coordination.

## 1.2 scope and follow-up

1.2 delivers decisions 2–4, the target branch from decision 1, and the agent-facing
statement of the defaults in `SKILL.md` (TRACKER-DEFAULT-001). These remain for 1.3
under TRACKER-DEFAULT-002:

- Make issues the resumable coordination record. The Markdown queue
  (`work_queue`, `litai work record`) becomes a local mirror or an override.
- GitLab parity for peer-work review and issue status, and forge-neutral peer-work
  classification and garbage collection.
- A project (`repository_policy`) and user preference that selects another tracker or
  review system, revising TRACKER-HOST-001's "no third tracker client" boundary into
  "no third tracker by default".

## Consequences

- A project with no extra configuration reports, submits, and coordinates on the forge
  that already hosts it; no other service is assumed.
- Landing targets the project's declared branch even when the forge's repository default
  differs.
- Integrations that set `LITAI_MAC_TASK_ID` keep working; they should move to
  `LITAI_EXTERNAL_TASK_ID`. Consumers of `prompt-task@1` must read `@2`.
- Newly initialized projects no longer carry the MAC skill as a framework file. A
  project that inherits the framework catalog still receives it through that catalog,
  and `litai update` leaves an existing copy in place; a project whose parent does not
  ship the skill and never edited it loses it on `update --apply` and copies it from
  the framework if it uses MAC runners.
- Landing now targets `default_branch`, which defaults to `main`. A project on a forge
  whose default branch differs must declare it.
