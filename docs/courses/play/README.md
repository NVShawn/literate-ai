# It Builds. Can We Ship It?

## Revision in progress: Ten Minutes. Keep Your Makefile.

The existing 13:38 public film below is preserved, but its dialogue and voices
have not met the owner's latest review. `story.json` now contains the replacement
ten-minute pitch. Sam defends working C++/Make/tests and presses consequential
objections; LitAI presents and answers with inspectable evidence and explicit
limits. Its recorded execution references still identify the original session,
not a fresh run against today's release. Do not publish this script as a verified
new film merely because the older media receipt passes.

Read the [revised script](SCRIPT.md) and [Opus 5.5 critique with dispositions](OPUS-REVIEW.md).
The [preserved voice audition](https://github.com/jordanhubbard/literate-ai/tree/docs/publish-video-courses/media/courses/ten-minute-audition)
includes its recorded audio, visuals and integrity receipt for migration.
The post-review draft has 1,247 spoken words in 54 turns; measured delivery, pauses
and readable evidence holds still determine the final running time. The two skipped
gates in the recorded verification are source intelligence (provider `none`) and
HTML observability (no declared HTML artifacts), not hidden passing tests. Those
reasons come from the retained raw verification record and must be displayed when
that result appears in the full replacement film.

The replacement producer separates spoken pronunciations from captions and accepts
directed local Qwen3-TTS voices. `--limit-turns 6` builds the opening audition;
omit the limit only after the new voices and dialogue have passed human review.
The original Kokoro approval does not transfer to these voices. The public weights
are [Qwen3-TTS CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice),
using the [MLX conversion](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit).
No real person's voice is cloned and no narration text goes to a cloud TTS service.
The independent script review uses the explicitly requested Opus model through
[review.py](review.py); only the supplied narrative and factual constraints are sent for that
authorized review. The reviewer has no tools. Private output stays under `_build`.

## Existing feedback edition

One continuous video play, published as a **feedback edition**, not a framework
release or a claim of production qualification. Sam is an experienced C++/Make
developer; LitAI personifies the harness. Their argument becomes a practical
adventure through setup, generation, adoption, and keeping projects current.

The finished media and editable inputs are linked from the [course index](../README.md).
The [script](story.json) is repository-owned narrative, not an inherited cast template.
Use the public repository's issue tracker for comments: include a timestamp, what
you expected to understand or do, and what actually happened. Voice, pacing,
code readability, missing prerequisites and misleading claims are all useful feedback.

## What is real, and what is illustrative?

The middle contains edited terminal replays of fresh, actual command execution,
real generated Python and its tests, an independent-acceptance refusal, a local
oracle correction, a passing rerun, project verification, and local wheel work.
Waiting is compressed and excerpts are selected; this is not a continuous screen
capture, and no simulated agent typing is presented as live execution.

The first run exposed an **unfixed framework inheritance defect**: the public
parent's greeting-card Component was paired with the simple scaffold's oracle.
The demo correction changes only the disposable project's independent acceptance
cases to match the written contract. It does not weaken the gate, change generated
source, or establish a framework repair. COURSE-003 tracks the missing upstream
ownership/contract regression. Do not expect this exact inherited-starter path to
work without the disclosed correction yet.

TinyXML2 was freshly cloned at
[`8224e427`](https://github.com/leethomason/tinyxml2/tree/8224e427b655b83dae5e2298f1e6919523a78737),
wrapped, built/tested, receipted and advanced to **retained**, with **original-source**
release authority. Its later drafted/qualified stages are explanations, not completed
demonstrations. No source rewrite, upstream change, or maintainer endorsement is claimed.

Installation panels and remote-worker setup are explicitly walkthroughs. The real
session uses an isolated development wheel, an already authenticated Codex CLI,
and requested model selector `gpt-5.6-sol`; no separate provider-resolved model
identity is claimed. Model calls may cost money and generated host code executes
only with explicit acknowledgement. This session proves local execution, not SSH
dispatch or a deployed Agentis integration. No package registry upload occurs.

The closing update command is a real read-only plan. The three-way comparison
discussion does not promise automatic merging of arbitrary conflicting edits.
Repository lineage and orchestration diagrams explain distinct supported concepts;
the film does not execute a multi-repository deployment or framework upgrade.

## Rebuild and refresh

[Capture commands](capture.py) runs only a command explicitly passed after `--`. Keep its raw output
under ignored build custody; it may include local paths. Run demos in disposable
projects. [Collect excerpts](collect.py) selects and sanitizes factual excerpts for manual review;
it rejects failed commands except the expressly shown acceptance failure.
Neither collector nor renderer grants demo-execution or publication permission.
The [producer](produce.py) prepares narration and visuals; the [staging helper](stage.py)
copies only verified media and referenced inputs to an explicitly selected local destination.

Delivery settings are editable separately from speech generation. This edition
uses 720p H.264 and 80 kbps mono AAC to fit the repository's documentation-file
budget; the original 24 kHz WAV narration remains in the authoring package.
The large bound media bundle lives at `media/courses/sam-meets-literate-ai/`,
outside the 64 MiB documentation-authority inventory. The course index links to
its public repository location; no documentation limits were relaxed. Its manifest,
assets and final media remain covered by the separate video integrity receipt.

For a provider-free rebuild after cloning this course branch:

```sh
litai video build media/courses/sam-meets-literate-ai/package/course.json \
  --output _build/play-renders --font /path/to/Arial.ttf --narration recorded
```

The actual command argv, selected outputs, source-file hashes and raw-capture hashes
are retained in the published authoring package's `sessions.json`. Redacted local
paths are placeholders, not missing install instructions. The generated-source
excerpt and independently authored acceptance cases remain different authorities.

Neural production uses the optional isolated environment described in the
[audition package](../preview/README.md), with its pinned dependency file. On Apple
Silicon, with a downloaded model snapshot and reviewed session excerpts:

```sh
python docs/courses/play/produce.py \
  --output _build/play-package \
  --model /path/to/kokoro/snapshot \
  --font /path/to/Arial.ttf \
  --repository-image /path/to/public-repository.png \
  --evidence /path/to/reviewed-sessions.json
litai video build _build/play-package/course.json \
  --output _build/play-renders --font /path/to/Arial.ttf --narration recorded
litai video verify /path/to/video-result.json --manifest _build/play-package/course.json
```

The producer caches narration by text and voice content. The standard video CLI
consumes retained narration and visuals without MLX or a model download. Rendering
never executes terminal commands. Visible captions are part of the composed picture;
SRT/VTT sidecars provide separate accessible text. This edition deliberately omits
the extra embedded subtitle track to prevent duplicate captions in players.

To refresh factual content, rerun the demos, review the excerpts and narrative,
then rebuild. Do not simply reuse old passing results beside a new command.
Retain prior cuts until the new audience review is accepted. Hash verification
proves integrity, not instructional quality or a human listening pass.

## Credits and rights

LitAI and Sam are fictional original character illustrations retained in
[the artwork package](../assets/PROMPTS.md). Voices are local synthetic Kokoro
presets `af_heart` and `am_fenrir`, not a clone of a requested real person.
[Kokoro's model card](https://huggingface.co/hexgrad/Kokoro-82M) identifies the weights
as Apache-2.0; this package redistributes generated narration, not model weights.
The exact MLX model snapshot and production inputs are recorded in `production.json`.
No narration text is sent to a cloud speech provider.
TinyXML2 retains its [original license](https://github.com/leethomason/tinyxml2/blob/8224e427b655b83dae5e2298f1e6919523a78737/LICENSE.txt);
the film shows its filenames and harness evidence rather than redistributing its implementation.

The short voice/style audition was accepted by the project owner. The continuous
edition is deliberately being published for further audience review; no complete
human audio-review signoff is claimed.
