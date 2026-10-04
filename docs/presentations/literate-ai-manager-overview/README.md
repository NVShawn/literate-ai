# Manager and engineering overview

This is the reproducible authoring package for the current manager and engineering
document pair: a `presentation` member (Google Slides deck) and a `narrative` member
(Google Doc), per `component://literate-ai/literate-ai-overview`'s declared realization.

**1.1.0 edition.** The stable Google Workspace pair now contains this accepted
scheduling/cache/package/native-acceptance edition; both members were refreshed in
place and export-back verified. Later patch cuts on the `1.1.x` line keep these same
published locations.

Published presentation: [Literate-AI — Application Foundry Vision](https://docs.google.com/presentation/d/1zGugAIHdxXNSDKJia9jak55_0J0LnpSq2dFt2F5OZpE/edit?usp=drivesdk)

Published narrative: [Literate-AI — Application Foundry Narrative](https://docs.google.com/document/d/1C6jtFrm9oAj6dg4CuLimylzu5HdaP6CovP2KY8U1HRA/edit?usp=drivesdk)

Start with the [current deliverables](current-deliverables.md), then read
the [deck specification](deck-specification.md),
[narrative specification](narrative-specification.md), [source notes](source-notes.md),
and [QA ledger](qa-ledger.md). The local [regeneration skill](SKILL.md) describes the
update workflow. Its model-facing inputs are the
[deck-authoring prompt](prompts/deck-authoring-prompt.md) and
[image prompts](prompts/image-prompts.md).

The [deck builder](build_deck.py), [narrative builder](build_narrative.py),
[slide rasterizer](render_slides.py), [Google Workspace publisher](publish_google_workspace.py),
and [regeneration launcher](regenerate.sh) derive the PowerPoint and Word outputs
from this reviewed package using `python-pptx` and `python-docx`. Image assets are
retained because they are authored presentation inputs, not generated application source.
`regenerate.sh` prefers the pinned OBJ_DIR toolchain from `make doc-toolchain-bootstrap`
so a vanilla worker with no Codex plugin can rebuild. The prior
[`build_deck.mjs`](build_deck.mjs) path remains a discovered fallback only (see
[source notes](source-notes.md)'s "Codex-independent rebuild" entry). That backend choice
was always project-local, never a dependency of the generic Literate-AI authoring skill,
and was never copied into initialized projects.

## Release Policy source status

The source package and both regenerated members now document writable Free/Pre-release
`main`, exact-main RC tags, release-line lockdown and README Release Engineer authority,
per-project strict/loose patch authority, mandatory `Literate-AI-Release` pull-request
classification, safe marker-based branch collection, continuous contribution
disposition, document-pair preflight, and the implemented 0.9 multi-entrypoint,
durable-service, host-path, and native-install boundaries plus release closure. The
current edition also carries the implemented doctor/status/onboard operator front door,
explicit brownfield authority stages, native SDK and compiled-library custody, retained
source lifecycle progress, exact worker-routing/remote-receipt slices, bounded action-DAG
scheduling, shared-cache authority, real local Debian packages, native CLI and gRPC
acceptance, and repository-owned worktree placement. Local acceptance and the state of
the 1.1.0 publication receipt are recorded in
[the QA ledger](qa-ledger.md).

Visual inputs: [cover](assets/cover-hero.png),
[manager section](assets/manager-section.png),
[engineering section](assets/engineering-section.png),
[one specification to many implementations](assets/one-many.png),
[legacy transformation](assets/legacy-transform.png),
[lifecycle gates](assets/lifecycle-gates.png),
[generation key](assets/generation-key.png),
[gate failure](assets/gate-failure.png),
[isolation containment](assets/isolation-containment.png), and
[evidence artifact](assets/evidence-artifact.png). The application-scale sequence uses the
[application foundry](assets/application-foundry.png),
[living portfolio](assets/living-portfolio.png) visual.
