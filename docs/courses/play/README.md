# It Builds. Can We Ship It?

## Revision in progress: Settle the Argument. Before CI.

The existing 13:38 public film below is preserved. `story.json` now holds the 1.2
narrative: LitAI opens with the big picture of agentic development, Sam rejects it
as the usual AI slop pitch, and seven of his beliefs are settled one by one with
evidence that doubles as the tutorial. Its terminal turns use fresh recordings made
against the 1.2 integration build (a clean clone at `63358492`, reporting version
1.1.0 before the release bump): greenfield create, a real Codex rebuild that passed
independent acceptance on its first candidate (no repair was needed, so bounded
repair appears only as a disclosed card), a cached rebuild, verification with and
without declared HTML views, `litai render html`, a stale-page qualification refusal,
`litai release qualify` on this Mac, TinyXML2 adoption to `retained`, a read-only
update plan, the local wheel, and `make release-check` stopping at a real unreviewed
driver change before resuming and passing from gate one. The full-gate excerpt from
`76c8c339` and the Windows wheel timing are labelled as recorded runs. Remote workers
remain a walkthrough. Raw captures stay in ignored build custody.

The previous ten-minute draft and its Opus critique remain in Git history and in
[OPUS-REVIEW.md](OPUS-REVIEW.md); that critique reviewed the earlier draft, not
this one.

Read the [1.2 script](SCRIPT.md).
The [preserved voice audition](https://github.com/jordanhubbard/literate-ai/tree/docs/publish-video-courses/media/courses/ten-minute-audition)
includes its recorded audio, visuals and integrity receipt for migration.
The 1.2 draft has 1,313 spoken words in 62 turns; measured delivery, pauses
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
it rejects failed commands except the failures the film shows on purpose (listed
in `SHOWN_FAILURES`), and omits optional captures that a recording did not produce.
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

### Studio rebuild for the 1.2 film

The 1.2 film adds motion, 3D illustration and sound through small scripts beside
the producer; heavy outputs (Blender frames, clips, sound, fonts, screenshots) stay
in a private build directory such as `_build/film-1.2/`:

- [`scenes/blender_scenes.py`](scenes/blender_scenes.py) renders three illustrative
  3D scenes (overnight pipeline, CI queue, one gate on three machines). The film
  labels them ILLUSTRATION • NOT A RECORDING.
- [`scenes/cards.py`](scenes/cards.py) composes chapter cards, speaker close-ups,
  recorded-output browser frames, provenance tags and caption bands.
- [`scenes/motion.py`](scenes/motion.py) adds Ken Burns moves, fits 3D clips to each
  line and refuses any clip without real frames.
- [`scenes/sound.py`](scenes/sound.py) synthesizes an original ambient bed and UI
  cues and mixes them about 23 dB below speech (bed about -40 LUFS, speech -16).
- [`shoot.py`](shoot.py) screenshots the recorded `litai render html` pages.
- [`listen.py`](listen.py) compares local speech recognition with the script.
- [`check_render.py`](check_render.py) refuses empty or truncated rendered clips.

Terminal pages say what they are: a recorded execution replay, a recorded gate log
excerpt (with its revision), a repository file excerpt, or recorded harness evidence.
Tested with Blender 5.2.2 LTS, ImageMagick 7.1.2-33, ffmpeg 9.0.2, mlx-audio 0.5.7,
Playwright 1.63 with local Chrome, and the fonts listed in [CREDITS](CREDITS.md).

```sh
B=_build/film-1.2
python docs/courses/play/shoot.py "$B/play-hello/views" "$B/screens"  # Playwright env
python docs/courses/play/produce.py --output "$B/package" \
  --model "$B/models/Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit@f35faf19b0cc" \
  --font "$B/fonts/Inter-Regular.ttf" --label-font "$B/fonts/Inter-SemiBold.ttf" \
  --display-font "$B/fonts/InterDisplay-Bold.ttf" \
  --mono-font "$B/fonts/JetBrainsMono-Regular.ttf" \
  --repository-image "$B/screens/public-repository.png" \
  --evidence "$B/evidence/sessions.json" --scenes "$B/scenes" \
  --blender /Applications/Blender.app/Contents/MacOS/Blender \
  --screens "$B/screens" --sound-bed
python docs/courses/play/listen.py "$B/models/whisper-large-v3-turbo" "$B/package"
litai video build "$B/package/course.json" --output "$B/renders" \
  --font "$B/fonts/Inter-Regular.ttf" --narration recorded
litai video verify "$B/renders/<run>/video-result.json" --manifest "$B/package/course.json"
python docs/courses/play/check_render.py "$B/renders/<run>/video-result.json"
```

Without `--scenes`, the producer keeps the original still-frame rendering.

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

The 1.2 film uses the local Qwen3-TTS CustomVoice presets Serena (LitAI) and Ryan
(Sam), again with no cloning and no cloud speech. Its fonts, sounds and 3D scenes
are listed with sources and licenses in [CREDITS](CREDITS.md); the sound bed and
cues are synthesized, not sampled.

The short voice/style audition was accepted by the project owner. The continuous
edition is deliberately being published for further audience review; no complete
human audio-review signoff is claimed.
