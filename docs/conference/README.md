# Literate AI symposium

Readable specifications are the starting point. Reviewable plans, selected targets,
independent acceptance and retained evidence make the work accountable.
These materials cover the core concepts, the lessons from the first releases, and
the current framework without treating unfinished release work as shipped.

## Slides and downloads

- **[Open the conference slides](../presentations/literate-ai-evolution-conference/index.html)** in your browser. Arrow keys navigate, **N** opens speaker notes, and your browser's Print command creates a handout.
- **[Download the offline conference kit](https://jordanhubbard.github.io/literate-ai/downloads/literate-ai-conference-kit.zip)** with the standalone slides, PowerPoint export, onboarding film, captions and six interactive labs. Unzip it and read `START-HERE.txt`.
- [Download the editable PowerPoint](../presentations/literate-ai-evolution-conference/literate-ai-evolution-conference.pptx).
- [Presenter guide and two-hour running order](presenter-guide.md).
- [Slide sources and factual ledger](../presentations/literate-ai-evolution-conference/README.md).

## The ideas behind the framework

1. **[Components and Flavors](../user/concepts.md):** observable intent belongs in readable authority; target choices remain explicit.
2. **[The framework flow](../user/framework-flow.md):** onboarding, locks, plans, generation, build and independent acceptance have distinct jobs.
3. **[Evidence and project receipts](../user/configuration-and-cli.md#project-test-receipts):** a result applies to its recorded inputs, environment and scope.
4. **[The HTML repository explorer](../user/html-repository-explorer.md):** inspect documented metadata, Components, relationships and source navigation.
5. **[Current features and release boundaries](../user/current-features.md):** distinguish the published distribution, the patch program and development work.

## Watch the onboarding film

**Eight Hours to Make Coffee** follows Sam and LitAI through the engineering and the
excuses. The approved 12:04 film uses new recorded dialogue and animation. English
captions are available in the player and as downloads.

<!-- CONFERENCE_VIDEO -->

[Download the MP4](../../media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.mp4) ·
[WebVTT captions](../../media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.vtt) ·
[SRT captions](../../media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.srt) ·
[Read the dialogue and annotations](../courses/full-onboarding/NARRATIVE.md)

The eight-hour anecdote is fictional, not a performance benchmark. Project examples
retain their stated native-platform and qualification limits.

## Explore the examples

The [six visual labs](https://github.com/jordanhubbard/literate-ai/blob/main/labs/README.md) span arithmetic, orbital motion, audio,
3D, music and video. Each opens independently and includes its specification and
exact authored sources. Each includes an independent 1.3.1 project with a real
application Component, selected Flavors and a lock-derived authority graph. Use
`litai build` and `litai run` from its project directory. Progressive multi-Component
composition remains in progress; internal drawing/audio roles are not extra Components.

Start with [Grumpy Counter](https://github.com/jordanhubbard/literate-ai/blob/main/labs/00-grumpy-counter/index.html), then explore
[Orbital Teapot](https://github.com/jordanhubbard/literate-ai/blob/main/labs/01-orbital-teapot/index.html),
[Unlicensed Birdsong](https://github.com/jordanhubbard/literate-ai/blob/main/labs/02-unlicensed-birdsong/index.html),
[Impossible Furniture](https://github.com/jordanhubbard/literate-ai/blob/main/labs/03-impossible-furniture/index.html),
[Glitch Opera](https://github.com/jordanhubbard/literate-ai/blob/main/labs/04-glitch-opera/index.html), and
[Component Cathedral](https://github.com/jordanhubbard/literate-ai/blob/main/labs/05-component-cathedral/index.html).

## Try Literate AI after the session

[Installation](../user/installation.md) and [Getting started](../user/getting-started.md)
provide the runnable entry points. Use the [user guide](../user/README.md) for the
task map and the [documentation index](../README.md) for deeper architecture.

**Welcome, Unprompted AI attendees!** Start with the
[from-zero setup guide](attendee-setup.md): machines and tools, your emailed
OpenRouter key, assistant choices, and the first lab.

**Release status:** [1.3.1 is published](https://github.com/jordanhubbard/literate-ai/releases/tag/v1.3.1).
Linux and macOS passed their complete native gates. The Windows test matrix is
temporarily suspended for this release because of resource availability and
must return in the next release. The approved film, caption files, browser deck
and editable briefing are attached to the release. Version 1.4 remains the
development line. The decks retain dated readiness snapshots; see the
[verified publication record](../../verification/1.3.1-release-publication.json)
and [current README](https://github.com/jordanhubbard/literate-ai#readme) for the published state.

The six browser previews and their complete project inputs are included in the
offline kit. The remaining progressive composition work is tracked under
[LAB-AUTHORITY-001](../roadmap/active-work.md).

[Source repository and questions](https://github.com/jordanhubbard/literate-ai)

The website is built from repository sources by the [site builder](../../scripts/build_conference_site.mjs)
and the [dedicated publication workflow](../../.github/workflows/conference-pages.yml).
Its deployment runs independently of product CI. The [publication check](../../scripts/verify_conference_site.py)
checks local navigation, required attendee artifacts and offline archive integrity.
The [browser check](../../scripts/verify_conference_browser.mjs) exercises attendee
navigation, search, media metadata and desktop/mobile layout using the existing
documentation browser toolchain.
The [publication record](publication.json) identifies the first verified deployment,
its public asset hashes and the scope of the checks.

