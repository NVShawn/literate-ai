# A plan before surprises

Project-owned Sam / Literate AI onboarding audition using articulated SVG characters,
Motion and recorded NVIDIA Magpie narration. This is a short review cut before a
longer onboarding episode, not a replacement for the accepted film.

The next episode's communication job is to help an engineer with existing working
code understand onboarding: install the exact release wheel, inspect the host,
review an adopt plan, acknowledge changes separately, and then qualify the actual
build and independent acceptance. The audition isolates the central conversation
and visual treatment. It does not demonstrate installation or an apply.

The user has **validated the voices and rejected the first animation**. The
[performance score](STORYBOARD.md) now pairs every approved line with purposeful
acting and listener direction. [Review decision](review-decision.json) identifies
the audition and recordings. The directed renderer now implements that score with purposeful poses, listener
reactions and phrase-cued visual emphasis. The rebuild commands below produce the
new treatment. The user will perform visual validation; technical checks do not
approve its creative quality. Earlier outputs remain retained as the rejected baseline.

## Source and output custody

- [Spoken story](story.json), [performance score](STORYBOARD.md),
  [executable performance](performance.json), [cue map](cue-map.json),
  [production brief and sources](PRODUCTION.md),
  [read-only demo evidence](evidence.json), [QA and review state](QA.md),
  and [character rig](characters.mjs)
  remain authored documentation.
- The package lock pins the optional authoring toolchain. Install only beneath
  ignored `OBJ_DIR`; the framework Python package does not acquire Motion or
  a speech-service dependency.
- Retain each WAV with its `.voice.json` provenance and the observed voice
  catalog. Rendering an accepted recording is offline and needs no API key.
- Each build uses a new output directory. It contains a self-contained browser
  companion, static poster, complete transcript, per-turn animations, recordings,
  measured timings and QA inputs. The standard video CLI produces the final MP4,
  SRT, VTT and integrity receipt. Keep the font's OFL license with the companion.
- Generated media remains local review material until its destination is
  explicitly authorized. The current course index and accepted film are preserved.

Authoring entry points: [recording preparation](prepare.py),
[explicit speech generation](speech.py), [offline builder](build.mjs),
[frame QA and export](capture.mjs), [player](player.mjs),
[stage template](stage.html), [unit guards](test_authoring.py), and the
[toolchain manifest](package.json) / [lock](package-lock.json).
The [score admission guards](test-performance.mjs) reject unsupported or stale
performance inputs. The [input-drift integration tests](test-inputs.mjs) exercise the actual build and
capture boundaries in disposable copies. Each pilot timeline is bounded to three
minutes; compose longer episodes from separately reviewed scenes.

## Rebuild the local review cut

Use Node 22.12+ supported by the pinned browser client, Python 3.11+, ffmpeg,
ffprobe, ImageMagick, a Chromium/Chrome executable and an installed TTF/OTF font
for the standard video CLI. The companion embeds its pinned OFL-licensed Plex
fonts and does not fetch them from a CDN. These commands do not install host tools.

```sh
AUTHOR=docs/courses/animated-onboarding
OBJ_DIR=_build/onboarding-review
export PYTHONPYCACHEPREFIX="$OBJ_DIR/pycache"
mkdir -p "$OBJ_DIR/toolchain"
cp "$AUTHOR/package.json" "$AUTHOR/package-lock.json" "$OBJ_DIR/toolchain/"
npm ci --prefix "$OBJ_DIR/toolchain" --ignore-scripts

# With retained recordings and their provenance in $OBJ_DIR/voices:
python3 "$AUTHOR/prepare.py" --audio-dir "$OBJ_DIR/voices" --output "$OBJ_DIR/cut-1"
node "$AUTHOR/test-performance.mjs" "$OBJ_DIR/cut-1"
node "$AUTHOR/test-inputs.mjs" "$OBJ_DIR/cut-1" "$OBJ_DIR/toolchain"
node "$AUTHOR/build.mjs" "$OBJ_DIR/cut-1" "$OBJ_DIR/toolchain"
node "$AUTHOR/capture.mjs" "$OBJ_DIR/cut-1" "$OBJ_DIR/toolchain" /path/to/chrome

litai video plan "$OBJ_DIR/cut-1/course.json"
litai video build "$OBJ_DIR/cut-1/course.json" --output "$OBJ_DIR/final" \
  --font /path/to/font.ttf --narration recorded
# Verify the returned unique output's video-result.json:
litai video verify /path/to/video-result.json --manifest "$OBJ_DIR/cut-1/course.json"
```

`prepare.py --animatic` explicitly makes a silent timing animatic instead. It
cannot be mistaken for synthesized narration: the film and companion label the
missing voice audition. Never silently substitute system speech for a failed
neural voice request.

## Regenerate voices, explicitly

The renderer never invokes a service. The separate authoring command sends only
the selected story turn to NVIDIA's documented speech endpoint. Supply
`NVIDIA_API_KEY` or `--api-key-file /private/path`; no credential belongs in the
story, command-line value, provenance or generated HTML.

```sh
python3 "$AUTHOR/speech.py" voices --output "$OBJ_DIR/voice-catalog.json"
python3 "$AUTHOR/speech.py" synthesize --catalog "$OBJ_DIR/voice-catalog.json" \
  --story "$AUTHOR/story.json" --turn existing \
  --voice Magpie-Multilingual.EN-US.Jason.Neutral \
  --output "$OBJ_DIR/voices/existing.wav"
```

Use only a voice returned by the live catalog. The audition uses Jason Neutral
for Sam and Aria for Literate AI. The user validated these voices in the retained audition. New spoken material
still needs pronunciation and delivery review; receiving WAV bytes alone cannot
establish suitability. All eight turns must be recorded with
matching story/text identities. A failed or uncertain request is not retried
automatically. Retain previous takes when trying another voice.

## Validation and review

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$AUTHOR" -p 'test_*.py' -v
# Same browser QA, without encoding a film:
node "$AUTHOR/capture.mjs" "$OBJ_DIR/cut-1" "$OBJ_DIR/toolchain" /path/to/chrome --qa-only
```

Capture checks repeat frames after seeks from different positions, keyboard
controls, caption bounds, reduced-motion on load and during playback, a no-JavaScript
poster/transcript, finite time validation and actual local-file playback with
external requests blocked. Directed checks also verify resting holds, stopping
short of Apply, unchanged source-folder contents, spoken pending-gate emphasis and
continuous hand trajectories across the complete frame grid. Score admission rejects
unknown targets/poses, non-finite timing, changed recordings and missing turns.
Frame identities bind input hashes, lock, fonts,
browser/platform, viewport, motion mode and requested time. Pixel identity across
different operating systems/renderers is not promised.

Watch every shot and listen to the complete soundtrack before approval. Check
pronunciation, natural turn-taking, levels, silent tails, mouth/caption timing,
gesture restraint, eye lines, original-source claims and command readability.
Automated media verification, frame inspection and human creative approval remain
separate outcomes. Mouth cues are audio-energy-driven, not phoneme/viseme alignment.
This local cut does not grant release or publication authority.

## Timing and regeneration

The renderer uses the committed cue map offline; Whisper and model weights are not
runtime dependencies. This pass used the already installed `openai-whisper` CLI,
`base.en`, CPU execution and word timestamps, retaining raw JSON in ignored build
custody. The source cue map records the model hash, raw word spans, estimated times
and approved WAV hashes. Recognition errors are retained rather than promoted into
the script. A newly synthesized take requires a new alignment and review; the old
map refuses its changed audio identity. Silent animatics scale the recorded score
to authored placeholder durations and are explicitly labelled as estimates.

The score interpreter samples absolute time using the pinned Motion easing curve;
it has no recurring arm, breathing or camera loop. Both the human-readable narrative
and executable score/cues are bound at preparation, build and capture. Unknown
capabilities fail before browser rendering. Technical admission and frame checks
remain separate from the user's visual verdict.
