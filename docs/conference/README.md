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

The [six visual labs](../../labs/README.md) span arithmetic, orbital motion, audio,
3D, music and video. Each opens independently and includes its specification and
exact authored sources. Their displayed lesson roles are illustrative; conversion
to a complete canonical Component curriculum remains in progress.

Start with [Grumpy Counter](../../labs/00-grumpy-counter/index.html), then explore
[Orbital Teapot](../../labs/01-orbital-teapot/index.html),
[Unlicensed Birdsong](../../labs/02-unlicensed-birdsong/index.html),
[Impossible Furniture](../../labs/03-impossible-furniture/index.html),
[Glitch Opera](../../labs/04-glitch-opera/index.html), and
[Component Cathedral](../../labs/05-component-cathedral/index.html).

## Try Literate AI after the session

[Installation](../user/installation.md) and [Getting started](../user/getting-started.md)
provide the runnable entry points. Use the [user guide](../user/README.md) for the
task map and the [documentation index](../README.md) for deeper architecture.

**Release status:** 1.3.0 is the latest published software release. The approved
onboarding film is already available. Patch 1.3.1 still has open qualification goals;
1.4 is the development line. Publishing these conference materials does not publish
the patch or complete its milestone. See the [1.3.1 briefing](../presentations/literate-ai-1.3.1-release-briefing/README.md)
and [roadmap](../roadmap/active-work.md) for the recorded work.

[Source repository and questions](https://github.com/jordanhubbard/literate-ai)

The website is built from repository sources by the [site builder](../../scripts/build_conference_site.mjs)
and the [dedicated publication workflow](../../.github/workflows/conference-pages.yml).
Its deployment runs independently of product CI. The [publication check](../../scripts/verify_conference_site.py)
checks local navigation, required attendee artifacts and offline archive integrity.
The [browser check](../../scripts/verify_conference_browser.mjs) exercises attendee
navigation, search, media metadata and desktop/mobile layout using the existing
documentation browser toolchain.

