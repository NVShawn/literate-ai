---
name: literate-ai
description: Maintain this root's explicit repository orchestration authority without adopting child catalogs or changing independent child histories.
metadata:
  author: Literate AI maintainers <literate-ai-maintainers@users.noreply.github.com>
---

# Repository orchestration

Read the root PROJECT.md before changing this project. Follow the nested agent
instructions at `.literate/orchestration/skills/agent/SKILL.md` and the
[orchestration guide](.literate/orchestration/docs/overview.md).

The root owns exact child repository pins and explicitly declared dependencies.
Each child owns its source, optional Literate AI project, tests and release history.
Do not run conversion over this tree, copy child catalogs into root authority,
initialize children, fetch, re-pin or execute child code as an implicit side effect.

Review the exact read-only orchestration plan before acknowledging root changes.
Use the installed `litai` CLI; do not assume a framework contributor Makefile.
Project validation is not evidence that child revisions are published, compatible,
tested or release-ready. Keep missing qualification explicit.
