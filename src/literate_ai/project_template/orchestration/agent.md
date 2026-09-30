---
name: repository-orchestration-agent
description: Guide changes to root-owned repository pins while preserving child ownership and explicit execution authority.
metadata:
  author: Literate AI maintainers <literate-ai-maintainers@users.noreply.github.com>
---

# Root-owned changes

Inherit the root onboarding instructions. Use `litai onboard orchestrate plan` and
`litai onboard orchestrate check` with an explicit dependency declaration. A current
plan proves only that the observed inputs match the reviewed identity.

Root initialization uses a distinct plan with an explicit project ID and version.
Use `litai onboard orchestrate initialize` only with that exact reviewed plan identity
and explicit acknowledgement. Follow the root orchestration guide for the write set
and incomplete-rollback handling; initialization does not qualify child repositories.

Record intended root changes and their evidence before implementation. Preserve
Gitlinks, configured repository URLs, child paths and independent child authority.
Do not infer build order or compatibility from declared dependency relationships.
Child execution requires its own acknowledgement and independent acceptance.

Do not replace unavailable refresh commands with hand-written
manifest edits, flat conversion, or a script that changes Git metadata. Report the
missing capability and keep its release requirement open.
