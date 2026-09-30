---
name: regenerate-literate-ai-manager-overview
description: Regenerate and refresh the Literate-AI manager and engineering overview document pair (presentation and narrative) when the framework, CLI, qualification model, or portfolio evidence changes.
---

# Literate-AI manager overview regeneration

This directory is the durable authoring package for the `literate-ai.document-pair`
realized by `component://literate-ai/literate-ai-overview`: the generated presentation at
`docs/presentations/literate-ai-manager-overview/literate-ai-manager-and-engineering-overview.pptx`
and its native Google Slides import (the `presentation` member), and a comprehensive
Google Doc narrative built from `narrative-specification.md` (the `narrative` member).

Before changing or regenerating the deck:

1. Read the repository-root `AGENTS.md` and `SKILL.md` completely.
2. Use CodeGraph first when `.codegraph/` exists and code behavior must be located or verified.
3. Use `build_deck.py` (`python-pptx`) to construct/render the PPTX. This is the primary
   build path; it has no LLM calls and no Codex plugin dependency. Bootstrap the pinned
   packages with `make doc-toolchain-bootstrap` into ignored `OBJ_DIR` (see
   `tools/doc-toolchain/authoring-toolchain.json`). `regenerate.sh` prefers that portable
   path and refuses to pip-install without Makefile authorization. Codex
   `build_deck.mjs` / `@oai/artifact-tool` discovery remains a fallback only — see
   `source-notes.md`'s "Codex-independent rebuild" entry. Do not remove plugin discovery
   until three-OS visual parity closes.
4. Use the installed `imagegen` skill only when an existing image asset must be replaced. Preserve the current palette, composition intent, and negative-space requirements in `prompts/image-prompts.md`.
5. Treat `deck-specification.md` as the presentation’s narrative authority and `source-notes.md` as its factual-claim ledger.

## When to regenerate

Full regeneration of both members belongs on every **major or minor** distribution cut
(`0.8.0`, `0.9.0`, `1.0.0`, `1.1.0`, …). **Patch** cuts keep the authoring-package and
repository-root `README.md` files pointing at the last major/minor *edition* of the
published Slides and Google Doc. Do not retag a published patch or minor to carry a later
refresh. A skipped minor is recovered on the next published cut of that line (this
repository used `0.7.1` to catch up `0.7.0`). See
`docs/architecture/project-releases.md` and `skills/agent/release-project/SKILL.md`.

Release-policy source changes must update the deck and narrative specifications, factual
ledger, both builders, authoring prompt, and QA ledger together. Preserve the 26-slide
sequence by revising the existing release slide. Unless the request explicitly authorizes
artifact regeneration and Google publication, stop at source changes and record both as
pending; do not rewrite prior QA entries.

## Refresh procedure

1. Revalidate every implementation claim in `source-notes.md` against current specifications and current code. Do not promote roadmap language into implemented claims.
2. Update `deck-specification.md` first. The presentation source is derived from that specification.
3. If a visual must change, regenerate only that asset using its recorded prompt and replace the corresponding file in `assets/`. Keep visuals text-free.
4. Update [build_deck.py](build_deck.py)'s corresponding slide function(s) to match. It ports
   `build_deck.mjs`'s primitive shape vocabulary (`shape`, `text`, `pill`, `line`,
   `arrow`, `footer`, `title`, `wash`, cover-fit image placement) 1:1; add native-shape
   diagrams rather than raster images for anything that shows a real mechanism.
5. Run [regenerate.sh](regenerate.sh). It prefers the pinned OBJ_DIR toolchain from
   [regenerate_python.sh](regenerate_python.sh) and
   `make doc-toolchain-bootstrap` (it will not pip-install on its own), builds the PPTX
   and the narrative `.docx`, rasterizes slides for inspection, then runs
   `scripts/verify_document_pair.py` against both members and writes its report to
   `$OBJ_DIR/literate-ai-manager-overview/acceptance.json`. Fix any failing scenario
   before continuing — it checks slide-surface geometry, non-overlapping text-bearing
   frames, non-empty speaker notes on every page, narrative heading levels, no credential
   material, and no unresolved placeholder tokens.
6. Render every slide, inspect a contact sheet and full-size slides, and fix all
   unintended clipping, overflow, overlap, broken connectors, and unreadable type.
   Geometry-escape (elements inside 1280 × 720) is not sufficient: two-line titles
   that paint into subtitles, section tags that collide with the brand pill, and
   bottom captions that collide with the footer all fail this pass. After the
   native Google Slides update, read the published copy as well — font substitution
   can introduce overlaps the local PPTX did not show. Workflow slides must show
   the flow as a picture or diagram; a paragraph that restates the workflow is the
   defect, not a caption. Native-shape diagrams still count as pictures; do not
   replace the specification excerpt or control-model diagrams with decorative
   raster that hides the mechanism.
7. Run the presentation skill’s overflow test when its managed Python environment is
   available and the Codex `presentations` plugin path is in use. For the `python-pptx`
   path, `scripts/verify_document_pair.py` is the mechanical geometry gate: it fails
   elements outside the declared surface **and** overlapping text-bearing frames.
   Geometry-escape alone is not an overflow test. `render_slides.py` also prints an AABB
   overlap report for inspection. Record any fallback in the QA ledger.
8. Run `publish_google_workspace.py --preflight-only --release VERSION
   --expected-account ACCOUNT` before any provider mutation. It must confirm the explicitly
   selected active `gcloud` account and both stable resource IDs, MIME types, edit/export/
   sharing capabilities, and access mapping. If no account is active, tell the operator to
   run `gcloud auth login ACCOUNT` and `gcloud config set account ACCOUNT`; do not choose an
   account for them. Only after the complete pair passes may an authorized publication
   update the exact Slides resource in place and export it for read-back verification.
   Never persist a token or include it in logs. Record the resulting Slides URL in
   `README.md`, near the top, alongside the narrative's URL.

## Narrative member refresh procedure

1. Treat `narrative-specification.md` as the narrative's structural authority (heading
   hierarchy, section scope) and `source-notes.md` as its factual-claim ledger — the same
   ledger the presentation member cites. Revalidate every claim before drafting or
   redrafting.
2. Use `build_narrative.py` (`python-docx`) to construct the local `.docx`. This is the
   primary narrative path; it has no LLM calls and no Codex plugin dependency. The Codex
   `documents` plugin remains an optional environment where that plugin happens to be
   installed and funded — it is not a hidden requirement of this package.
3. `./regenerate.sh` builds both members through the portable python-pptx path, writes
   the capability manifest covering both, runs `scripts/verify_document_pair.py`, and
   rasterizes slides with [render_slides.py](render_slides.py) into
   `$OBJ_DIR/literate-ai-manager-overview/rendered-slides/` for visual inspection. Fix any
   failing oracle scenario before continuing. Headings must map to Word `Heading 1`
   through `Heading 6` without skips.
4. Inspect the contact sheet and full-size slides. For the narrative, confirm the heading
   tree matches `narrative-specification.md` and that no placeholder or credential
   material ships.
5. Publish the verified PPTX and `.docx` only through the pair-wide preflight above and
   only with explicit external-write authorization. Update both exact stable resources in
   place; do not create a new Doc merely because the configured one is inaccessible. The
   publisher writes a release-bound, resumable receipt after each update and exports both
   native resources for structural and hash verification. On partial failure, retain the
   receipt, repeat the complete preflight, and resume only the missing member. Record both
   stable URLs in `README.md`, near the top, as the current major/minor *edition*. Drive
   `files.export` rejects this deck (`exportSizeLimitExceeded`); read-back uses the authenticated
   `https://docs.google.com/presentation/d/{id}/export/pptx` URL.
   [`publish_google_workspace.py`](publish_google_workspace.py) in this package performs
   the authorized update, access mapping, resumable receipt, and read-back.
6. Both members share one publication-authorization gate (see
   `components/literate-ai-overview/component.md`'s "Requirement: Publication is
   authorized per revision"): regenerating one member does not authorize publishing
   either. Asking for this overview to be regenerated and published for a named edition
   is authorization for that revision only.

The Codex `presentations`/`documents` plugins remain blocked by an organizational spend
cap in this environment; a spend-cap rejection is an external constraint, not a defect
in this package. Do not treat that path as required.

## Guardrails

- Do not invent productivity or ROI percentages.
- Keep one cumulative narrative for product, program, engineering, and executive audiences. Do not reintroduce audience-specific sections that repeat the same claims.
- Present the deck as an application-foundry investment story: current framework signal,
  real end-to-end samples, the Component DAG and cross-platform operating model, a
  two-week horizon, and the portfolio-scale destination.
- Preserve the distinction between implemented Standard-core behavior and remaining reference-sample or CLI integration work.
- Preserve the source-to-specification boundary: draft extraction is available; effective authority transfers only with trusted, current qualification evidence.
- Generated source is an untrusted candidate until build, test, execution, independent acceptance, and receipt requirements pass.
- Keep the deck visual, but carry mechanism alongside claim. Every headline assertion in
  the main sequence needs a slide, a diagram, or a shown artifact that explains how the
  system produces it. Prefer one precise diagram or real artifact excerpt to a hero image
  with three sentences of consequence.
- Image-led slides carrying only a headline and a paragraph must stay a minority of the
  deck. They punctuate the argument; they cannot be the argument.
- Show the durable authority at least once. A deck asserting that a readable
  specification is the product must display a real specification excerpt, plan, or
  receipt.
- Keep the technical mechanism slides grouped as a labeled sequence so the engineering
  audience can be pointed at them and the decision audience can move past them.
- Author speaker notes for every slide carrying its supporting authority and the limits
  recorded in `source-notes.md`. The deck is forwarded without narration.
- Answer cost, blast radius, failure handling, and the model-egress trust boundary. Those
  are the first questions this audience asks.
- Keep a clearly titled `Release Policy` section in both members. Distinguish LitAI CLI
  enforcement from forge branch protection and never imply live protection from policy
  text alone.
