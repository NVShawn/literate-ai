# Animated onboarding pilot QA

Status: **voices validated by the user; animation rejected**. The
[review decision](review-decision.json) binds those separate verdicts to the original
audition and eight retained recordings. This is not production or publication approval. [Machine-readable observations](qa-summary.json)
record the exact media identity and tested scope.

- Nine authoring unit checks passed; Python lint passed.
- Six integration refusals passed against changed scenes, combined audio,
  individual recordings, story, staged dependency lock and built HTML. Invalid
  prepared inputs fail before browser launch; copied fixture inputs preserve the
  accepted outputs. The pilot timeline also has a three-minute duration bound.
- Seven browser checks passed, including repeated same-time DOM/pixel captures,
  every caption's bounds, keyboard Play/Pause, reduced-motion on load and while
  playing, a static no-JavaScript transcript/character fallback, finite seek
  refusal and actual offline local-file launch with external requests blocked.
- Five timeline positions repeat after seeking from different prior positions.
  Runtime lock, fonts, browser/platform and viewport are bound in local frame QA.
- The standard video builder and independent `litai video verify --manifest`
  passed for the 38.437989-second, eight-caption H.264/AAC film. Visible captions
  are rendered in the picture; SRT/WebVTT sidecars are retained.
- Decoded midpoint frames from all eight final-film turns and their contact sheet
  were inspected. Character silhouettes, panel copy, speaker identification and
  captions remain readable; no unintended overlap or clipped text was observed.
  This is sampled frame inspection, not a claim of watching the complete film.
- All eight source recordings contain signal with no clipped PCM samples. RMS
  levels range from -31.70 to -27.68 dBFS. These are signal observations, not
  an assessment of naturalness, pronunciation or perceived loudness.
- Stock NVIDIA Magpie requests used Jason Neutral for Sam and Aria for LitAI.
  Raw recordings, per-turn provenance and the observed catalog are retained.
  Backend checkpoint and separate effective-voice confirmation were not exposed.
- The actual read-only source-CLI adopt observation in [evidence.json](evidence.json)
  reports `writes: false`, no blockers and unchanged original source for its
  authored two-file C++/Make fixture. Nothing was applied or compiled.

## Animation correction and remaining review

The user found the short segment's arm flapping unhelpful. This exposed three gaps:
`story.json` supplied generic gesture labels; `player.mjs` mapped most of them to
one arm/nod cycle; the checks above evaluated timing, repeatability and layout,
not communicative performance. Sampled frames could not expose repetitive motion.
The existing video skill required storyboarding and complete playback review;
that requirement was not fulfilled by the technical pass.

The [performance score](STORYBOARD.md) now pairs exact dialogue with phrase cues,
meaningful speaker actions, listening, focus, props and stillness. The reusable
skills now require that score and a separate motion-quality review. This is an
authored correction, not a rendered fix: the existing renderer does not consume it.
Approved narration and original generation receipts remain unchanged. Revision
checks confirmed all eight score lines exactly match the spoken story, all eight
retained WAV hashes match the review decision, and the reviewed MP4 identity is
unchanged. Canonical and initialized video/presentation skills are byte-identical
and pass the required SkillEvaluator checks. These checks validate source coverage
and integrity, not the unimplemented performance.

Next: implement the score, measure cues from approved audio, bind all visual inputs,
and review a new complete moving cut at playback speed with sound and then muted.
Every movement must contribute to the explanation or an earned reaction. Check
mouth/caption timing, readable holds, eyelines, focus and static/reduced-motion
meaning separately. Mouth cues are currently audio-energy-driven, not phoneme-aligned.

The full episode still needs complete installation/acquisition coverage, fresh
execution evidence and complete playback review. The user's approval of these
voices does not approve new utterances, this animation, or publication.
