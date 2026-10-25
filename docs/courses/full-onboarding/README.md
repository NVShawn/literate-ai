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
player and fixed-time capture path are now authored, but **the new script has not been
voiced or rendered** because the retained NVIDIA credential returned HTTP 401 on
2026-10-25. The [production plan](PRODUCTION.md) names the remaining credential,
new-audio, rendering and complete-playback review gates. The pilot and published film
remain intact until the replacement passes all of them.

The argument preserves required CI, independent review and native/release gates.
It challenges waiting for CI to provide the first useful feedback. Sam's eight-hour
pipeline is a fictional anecdote, not a measured performance claim. Case studies
show different kinds of real adoption and scoped results, including unfinished
native work; they are not five identical, fully qualified success stories.

The development-only FreeBSD profile is distinguished from the observed v1.2.0
published wheel. No new release, cloud speech request, example-project execution or media
publication was performed to produce this narrative.

The draft has 12 chapters, 86 spoken turns and 1,732 spoken words.
Allow roughly 13–16 minutes with source-reading and acting holds; this is an
authoring estimate, not measured narration or a completed film duration.

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
  --api-key-file /private/path/to/nvidia-key
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

The batch synthesizer performs no retry or fallback. It validates and preserves any
already completed take before continuing, so a provider interruption does not replace
earlier audio. Release approval still requires listening to every new utterance and
watching the assembled film at normal speed, with a separate factual, accessibility
and visual verdict recorded against exact media identities.
