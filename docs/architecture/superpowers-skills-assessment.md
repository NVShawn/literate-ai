# Superpowers skills assessment

This assessment evaluates [obra/superpowers](https://github.com/obra/superpowers) as
an input to Literate AI's specification-to-source method. It is intentionally not an
endorsement of importing a popular suite wholesale. The reviewed upstream snapshot is
commit `44c9b2d6e889982ac18c27d05a19fefe335194e1` from 2026-07-28. Upstream is MIT
licensed. No upstream source, scripts, or skill prose are vendored here.

## Decision

Do not make Superpowers a Literate AI dependency and do not inject its complete skill
set into generation prompts. Its strongest ideas are sound, but most govern an
interactive coding session, git branch lifecycle, or subagent choreography. Literate
AI instead needs a bounded, non-interactive conversion whose authorities, inputs,
outputs, and independent evidence can be replayed. A second mandatory workflow would
create conflicting gates and make exact recipe identity harder to explain.

Three ideas are useful as design checks:

1. diagnose a failed generated candidate from exact evidence before changing it;
2. verify the current candidate before making a success claim;
3. test skill behavior, not merely its Markdown shape.

The first two already exist in the Standard lifecycle: generated implementation tests,
the independent acceptance oracle, build and execution receipts, regenerative
qualification, and fail-closed promotion. Language skills may add a generation-time
self-check, but it never replaces those independent phases. The third identifies a
real next step for skill admission: NVIDIA SkillEvaluator Tier 1 remains the required
change-triggered static gate, while major or behavior-shaping skills should also carry
fresh-context pressure/application scenarios before maintainers classify them as
reviewed. This is an authoring practice, not another source-generation authority.

## Skill-by-skill disposition

| Upstream skill | Useful idea | Fit for specification-to-source | Decision |
| --- | --- | --- | --- |
| `brainstorming` | Clarify intent, compare designs, split oversized systems | Poor as a generation input. Accepted specifications already own intent, and mandatory user approval makes unattended regeneration impossible. Its boundary advice is already expressed through nested Components and public contracts. | Do not import. Keep interactive discovery outside the deterministic recipe. |
| `dispatching-parallel-agents` | Parallelize genuinely independent work | Conditional. Component-level fan-out is useful; several agents editing one generated tree is nondeterministic and raises merge and cost risk. | Do not inject. Let the lifecycle parallelize independent Components and host tests explicitly. |
| `executing-plans` | Execute a reviewed plan with checkpoints | Mostly redundant. Literate AI's content-pinned recipe and lifecycle phases are the executable plan and retain stronger machine evidence. | Do not import. |
| `finishing-a-development-branch` | Verify before merge and preserve user control | Git-branch hygiene is outside conversion and conflicts with disposable generated-source custody. | Reject for generation. |
| `receiving-code-review` | Verify feedback instead of accepting it performatively | Sensible agent etiquette, but not a reproducible conversion rule. Literate AI review and promotion operate on typed evidence. | Do not import. |
| `requesting-code-review` | Independent review before integration | Conceptually aligned, but a generic reviewer cannot replace build, oracle, SBOM, and qualification gates. Mandatory subagent review would add variable cost and identity. | Do not import; retain typed independent gates. |
| `subagent-driven-development` | Fresh context per task, scoped reviews, a recovery ledger | The context-isolation insight supports Component boundaries. The prescribed per-task agents, commits, five-round loop, and scratch ledger conflict with source fungibility and deterministic orchestration. | Do not import. Apply isolation at the Component contract boundary instead. |
| `systematic-debugging` | Trace evidence, form one hypothesis, make a minimal correction, rerun | High conceptual value when generation or qualification fails. Literate AI already preserves exact prompts, diagnostics, receipts, and test history needed for this discipline. | Use as an optional maintainer reference; do not copy its generic prose into every recipe. Add targeted rules only where a recurring generator failure proves a language/build skill needs them. |
| `test-driven-development` | Tests state desired behavior and must be capable of failing | The principle fits, the literal workflow does not. Literate AI regenerates source and tests together from current specs, then challenges both with an independent oracle. Deleting generated code because a test was not authored first adds no trust. | Do not import. Preserve current-spec tests plus independent acceptance and regenerative parity. |
| `using-git-worktrees` | Isolate mutable feature work | Generated source already has explicit source/object caches and custody rules; forcing worktrees changes repository state and is irrelevant on remote workers. | Reject for generation. |
| `using-superpowers` | Discover applicable skills before acting | Direct conflict. Literate AI has one provider-neutral top-level onboarding skill and exact content-pinned recipe skills; a one-percent mandatory trigger rule would over-load context and weaken explicit selection. | Reject. Never make it the onboarding authority. |
| `verification-before-completion` | Fresh evidence before success claims | Strongly aligned and already implemented more rigorously by lifecycle receipts and qualification. | Keep the principle; do not add a redundant prompt layer. |
| `writing-plans` | Small testable tasks, explicit interfaces, no placeholders | Strong overlap with `portable-specification-planning`. Literate AI improves on it by bounding context to a Component and direct dependency contracts rather than files and task prose. | Do not import. Continue enforcing component composition and interface contracts. |
| `writing-skills` | Concise discovery metadata and behavior tests for skills | Valuable for framework maintainers. Its test-the-guidance idea complements, but does not replace, the pinned SkillEvaluator gate. Much of its long discipline prose would be costly in generation context. | Adopt the evaluation principle in project documentation; retain Literate AI's typed manifests, content pins, and change-triggered gate. |

## Why a reference is better than a merge

The suites optimize different layers. Superpowers is a general agent-development
methodology. Literate AI is an artifact and authority pipeline that happens to use a
coding agent for bounded transformations. The useful overlap is process wisdom, not a
shared runtime contract.

If a recurring failure later demonstrates that a Superpowers technique changes
generation outcomes, maintainers should write the smallest Literate AI-native skill
rule that addresses that failure, record upstream inspiration in the change, bump and
repin the skill identity, run `make skills-check`, and prove the affected E2E sample.
That keeps the idea while avoiding prompt bloat, hidden network dependencies, competing
onboarding rules, and ambiguous authority.

## Revisit criteria

Reconsider a narrow import only when all of these are true:

- a failing Literate AI E2E or skill-evaluation scenario demonstrates the gap;
- the proposed skill has one bounded conversion responsibility;
- it can run without interactive approval or unrecorded agent state;
- its exact bytes, license provenance, dependencies, and model instructions are pinned;
- it does not duplicate lifecycle validation or expose acceptance-oracle data;
- it passes SkillEvaluator and a fresh-context behavioral evaluation; and
- it measurably improves success rate, cost, or diagnosis on the affected samples.
