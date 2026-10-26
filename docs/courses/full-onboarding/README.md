# Eight Hours to Make Coffee

A full Sam / LitAI episode about doing serious engineering before waiting for CI,
with a skeptical Sam, a sharper LitAI and a growing pile of domestic excuses.
The [full narrative and animation annotations](NARRATIVE.md) pair every spoken turn
with acting, listener response, focus and a hold. The
[research ledger](RESEARCH.md) traces five real project integrations to exact public
GitHub commits; [snapshot data](research-snapshot.json) records the inspected files.

The user approved the [directed pilot](../animated-onboarding/README.md)'s visual
treatment and previously approved its voices. The full replacement is an explicit
1.3.1 release goal. Its chapter compiler, measured-audio preparation, offline Motion
player and fixed-time capture path are authored. All 86 turns have now been recorded
with the approved NVIDIA Magpie voices, rendered into a 12:04.896 local candidate and
independently media-verified. The user approved the exact 12:04.896 candidate after
complete normal-speed playback; the review-bound rebuild preserves byte-identical
video and captions and independently verifies again. [Approval evidence](../../../verification/onboarding-1.3.1-approval.json)
binds both source identities and every published media hash. The [production plan](PRODUCTION.md)
records the completed gates. Fresh [host and read-only adoption evidence](../../../verification/onboarding-1.3.1-fresh-evidence.json)
is retained against the exact candidate source revision without claiming apply or
build. The prior film remains available; the approved replacement is published as an
[MP4](../../../media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.mp4)
with [captions](../../../media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.vtt),
[receipt](../../../media/courses/eight-hours-to-make-coffee/video/video-result.json),
[poster](../../../media/courses/eight-hours-to-make-coffee/poster.png) and retained
provenance in the same media bundle.

The argument preserves required CI, independent review and native/release gates.
It challenges waiting for CI to provide the first useful feedback. Sam's eight-hour
pipeline is a fictional anecdote, not a measured performance claim. Case studies
show different kinds of real adoption and scoped results, including unfinished
native work; they are not five identical, fully qualified success stories.

The development-only FreeBSD profile is distinguished from the observed v1.2.0
published wheel. No example-project execution was performed to produce the narrative.
The final film uses the explicitly approved NVIDIA Magpie recordings and publishes
their retained provenance without credentials.

The film has 12 chapters, 86 spoken turns, 1,732 spoken words and a measured duration
of 724.896322 seconds.

The rebuildable sources are the [story compiler](compile_story.py),
[batch synthesizer](synthesize.py), [measured-input preparer](prepare.py),
[offline player](player.mjs), [stage template](stage.html),
[browser build](build.mjs) and [fixed-time capture](capture.mjs). Focused
[compiler](../../../tests/critical/test_full_onboarding_compile.py) and
[preparation](../../../tests/critical/test_full_onboarding_prepare.py) tests protect
the narrative and retained-audio boundaries.

## Build the 1.3.1 candidate

All generated speech and media stay under ignored `_build/` custody. The compiler
extracts only the quoted dialogue and binds every turn to its Play, Listen and
Focus/hold direction. It refuses any missing chapter, turn, annotation or scene that
exceeds three minutes.

```sh
FULL=docs/courses/full-onboarding
PILOT=docs/courses/animated-onboarding
OBJ_DIR=_build/av/full-1.3.1
TOOLCHAIN=_build/av/toolchain

python3 "$FULL/compile_story.py" --output "$OBJ_DIR/story.json"
python3 "$PILOT/speech.py" voices \
  --api-key-file /private/path/to/nvidia-key \
  --output "$OBJ_DIR/voice-catalog.json"
python3 "$FULL/synthesize.py" --story "$OBJ_DIR/story.json" \
  --catalog "$OBJ_DIR/voice-catalog.json" --output "$OBJ_DIR/voices" \
  --api-key-file /private/path/to/nvidia-key \
  --minimum-request-interval-seconds 7
python3 "$FULL/prepare.py" --story "$OBJ_DIR/story.json" \
  --audio-dir "$OBJ_DIR/voices" --output "$OBJ_DIR/package"

mkdir -p "$TOOLCHAIN"
cp "$PILOT/package.json" "$PILOT/package-lock.json" "$TOOLCHAIN/"
npm ci --prefix "$TOOLCHAIN" --ignore-scripts
node "$FULL/build.mjs" "$OBJ_DIR/package" "$TOOLCHAIN"
node "$FULL/capture.mjs" "$OBJ_DIR/package" "$TOOLCHAIN" \
  /path/to/chrome
litai video plan "$OBJ_DIR/package/course.json"
litai video build "$OBJ_DIR/package/course.json" \
  --output "$OBJ_DIR/final" --font /path/to/font.ttf --narration recorded
litai video verify /path/to/video-result.json \
  --manifest "$OBJ_DIR/package/course.json"
```

The batch synthesizer performs no retry or fallback and paces new provider requests.
It validates and preserves any already completed take before continuing, so a provider
interruption does not replace earlier audio. Every new utterance and the assembled film
were reviewed at normal speed, with factual, accessibility, listening and visual
approval recorded against exact media identities.
