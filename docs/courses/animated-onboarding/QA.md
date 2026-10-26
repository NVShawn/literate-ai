# Directed onboarding cut QA

Status: **directed pilot visual inspection passed by the user**; see the
[separate visual decision](visual-review.json). The user
validated the voices and rejected the first cut's animation. The
[original review decision](review-decision.json) is preserved; the new MP4 has a
different identity in [machine-readable observations](qa-summary.json). No creative
approval or publication is inferred from technical verification.

## New cut

- The 38.437989-second H.264/AAC film has eight captions, visible captions in the
  picture, retained SRT/WebVTT and an offline browser companion. Independent
  `litai video verify --manifest` accepts the canonical output.
- All eight approved WAVs and their original generation records remain unchanged.
  Sidecar caption bytes also match the previous audition. There was no new speech
  synthesis, cast selection or voice substitution.
- The renderer realizes the [performance score](STORYBOARD.md) through 46 finite
  beats and 23 recording-bound phrase cues. Two-joint working arms, a source folder,
  gaze, brow, expression, listener reactions, deliberate holds and pending-gate
  emphasis replace the old arm/nod cycle. The composition remains a stable two-shot.
- Local Whisper base.en provided estimated word times. The cue map retains raw
  recognition and curated word spans, model and recording identities. Recognition
  errors are not promoted into dialogue. These are not verified phoneme timings.
- Nine Python guards and lint/format pass. Ten score-admission refusals cover changed
  recordings, unknown targets/poses, bad timing, omitted turns and off-story cues.
  Ten real builder/capture refusals cover drift in scene, combined/individual audio,
  story, dependency lock, narrative, score, cues, compiled score and HTML.
- Nine browser checks pass. Five requested times repeat in both pixels and DOM
  after different seek histories. Captions fit; keyboard controls, offline local-file
  playback, reduced motion on load/change, static rigs and no-JavaScript transcript
  are checked. The browser performs no external requests.
- Directed checks observe an intentional hand hold, a hand stopping short of Apply,
  unchanged folder contents and spoken-order emphasis on three still-pending gates.
  Every adjacent hand position across the full 24 fps timeline is checked; the
  maximum step is 29.190382 pixels against the retained 35-pixel limit. This bounds
  snapping, not expressiveness or acting quality.
- Decoded midpoint frames from all eight turns and their contact sheet were
  inspected. No clipped captions or unintended overlap were observed. An early
  hand/panel layering issue and an over-fast reach were corrected before this cut.
  Sampled frames do not constitute watching the complete moving performance.

## User review and remaining production

The user has passed visual inspection of this exact directed cut. That approves
this pilot's treatment, not new dialogue, a full episode or publication. The next
creative artifact is the [full annotated narrative](../full-onboarding/NARRATIVE.md).
For the future full episode, check whether
each action communicates a thought or learning step; whether eyelines, listening,
anticipation, holds and settles feel coherent; and whether captions, mouth cues and
spoken emphasis line up. Mouth cues remain audio-energy-driven, not viseme alignment.
Review the sequence with sound and then muted; compare with the static fallback.
Keep voice, motion, factual and accessibility verdicts separate.

The full episode still needs installation/acquisition coverage, fresh scoped
execution evidence and complete playback review. The retained source-CLI observation
in [evidence.json](evidence.json) proves only a read-only two-file adopt plan with no
writes; no apply, compilation, release or native qualification is claimed. The
accepted longer film, rejected audition and native Windows evidence remain intact.
