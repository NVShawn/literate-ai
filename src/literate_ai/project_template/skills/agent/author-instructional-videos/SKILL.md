---
name: author-instructional-videos
description: Create or refresh narrated instructional videos with captions and real demo evidence using the packaged litai video commands. Use for project tutorial courses, not slide decks or speculative product commercials.
metadata:
  author: Literate AI maintainers <literate-ai-maintainers@users.noreply.github.com>
---

# Author instructional videos

Use `litai video init` to start an editable course package, `plan` to validate
its manifest and assets, `build` to render, and `verify --manifest` to detect
stale inputs or damaged output. Run `litai help video` for the installed syntax.
Keep manifests, owned assets, and sanitized factual evidence under the project's
documentation root; render intermediates under ignored build custody.

Agree the audience and instructional outcome with the request. Demonstrate real
commands, code changes, and observable results in disposable demo projects;
capture failures as well as successes. Identify the tested framework revision.
Never turn a hypothetical result into a transcript. Label transcript replays,
edited recordings, and walkthrough-only steps in the video itself. A media
verification receipt proves media integrity, not that demo claims are true.

Use one or two speakers as requested. Supply recorded narration for portable
builds, or explicitly select an available local speech backend. Do not silently
install tools, impersonate a real speaker, or send narration to a cloud service.

Inspect rendered scenes, listen to both voices, and review caption timing before
delivery. Rebuild after changing evidence, dialogue, recordings, or assets;
retain prior outputs until their replacement passes verification. Scan captions,
recordings, and evidence for credentials and private endpoints before publishing
only to the destination the user authorized. Rendering does not grant publication
or execution authority.
