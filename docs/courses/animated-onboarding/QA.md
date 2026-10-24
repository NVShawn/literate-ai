# Animated onboarding pilot QA

Status: **local audition ready for complete viewing/listening feedback**. This is
not production or publication approval. [Machine-readable observations](qa-summary.json)
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

## Review still required

Listen to the complete soundtrack and watch the full moving cut. Check delivery,
voice fit, pronunciation, perceived levels, pauses, motion restraint, mouth cues,
caption synchronization and whether the conversation changes the explanation.
Mouth cues are audio-energy-driven rather than phoneme-aligned.

The next action is timestamped feedback on this audition. The full episode still
needs complete installation/acquisition coverage and fresh, explicitly scoped
execution evidence. Generic skill promotion and media publication follow that
review rather than being inferred from these technical checks.
