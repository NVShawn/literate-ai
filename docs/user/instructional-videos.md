# Rebuildable instructional videos

For visuals that already contain visible captions, set the manifest's optional
`embed_subtitles` to `false` to omit the duplicate selectable track.
SRT/VTT sidecars are always retained; ordinary courses embed subtitles by default.
Optional `audio_bitrate_kbps` selects 64–320 kbps AAC (default 160); a mono speech
course can choose a smaller delivery file while retaining its original WAV inputs.

`litai video` turns an editable JSON course into an MP4 with narration and
embedded English subtitles, SRT and WebVTT captions, and a verification receipt.
It supports project-selected presenters, authored visual shots, and screen recordings.
It never executes displayed demo commands, installs tools, uploads content, or
submits text to a hosted speech service. See the [introductory courses](../courses/README.md).

## Create, build, refresh

```sh
litai video init docs/tutorial/course.json
litai video plan docs/tutorial/course.json
litai video build docs/tutorial/course.json --output _build/videos \
  --font /path/to/font.ttf --narration recorded
litai video verify _build/videos/first-project-UNIQUE/video-result.json \
  --manifest docs/tutorial/course.json
```

Edit the starter manifest before building. The default `recorded` backend requires
an `audio` file on every dialogue turn. Alternatively, macOS `--narration say`
uses each speaker's explicitly selected installed voice;
`--narration espeak` uses installed `espeak-ng` voice IDs. Select voices appropriate
to that backend. Both synthesize locally; neither requires a paid API. Recorded
audio can override individual turns even when a speech backend is selected.
The starter is narrative-neutral: one unnamed-role narrator, no prescribed voice,
characters, gender, jokes, or framework-specific dialogue. Its structure is an
editable starting point, not a requirement to use a solo presenter. Projects may
declare up to sixteen speakers. Natural neural or human narration can be supplied
through recorded audio; audition it before producing a complete course. The system
speech backends are useful scratch tracks, not publication-quality guarantees.

Rendering requires `ffmpeg`, `ffprobe`, and ImageMagick's `magick` on PATH,
with H.264/AAC encoding and SVG rendering support. Supply an installed font file
explicitly. The core Python package and read-only plan command need none of these
optional host tools. Missing prerequisites fail without installation or mutation
of previous results. Install tools through your platform's package manager.

Refresh by updating the manifest or its evidence and running `build` again.
Every build writes a unique new output directory; no accepted output is replaced.
Keep the final MP4, SRT, VTT, and `video-result.json` together. Intermediate scenes,
audio, and clips are disposable. Receipts bind the manifest, referenced assets,
font, final file hashes, renderer versions, and durations. Identical source does
not promise byte-identical speech across host voice or renderer upgrades.

## Manifest

The schema is `literate-ai/video-course@1`. `init` creates the minimal example.
Each speaker has a display `name`, optional backend-specific `voice`, and optional
`portrait` image. Each scene has a `title`, up to three short `points`, up to four
`terminal` lines, and ordered `dialogue` turns (`speaker`, `text`, optional
`audio`). Use multiple short turns for accurate recorded-narration captions.
Synthesized speech is timed sentence by sentence, not estimated by word counts.
`voice` is required only when synthesizing that speaker's audio. A scene or dialogue
turn may supply a `visual` image instead of a generated text slide, allowing
storyboarded shots, screenshots, diagrams and close-ups to follow narration. Visual
assets are hashed like recordings; a scene recording cannot also select still shots.

A scene's optional `recording` replaces its slide with video; its original audio
is replaced by the dialogue. Playback continues across turns and loops if shorter
than the narration. Edit recordings to appropriate lengths, label any replay or
speed changes, and check the final timing. Displayed terminal lines are inert
text, not a recording or proof of execution. Put sanitized transcripts, source
revision records, and verification results in the `evidence` file list.
All referenced paths must stay inside the manifest's directory, including through
symlinks. Do not include private addresses, tokens, or personal data.

## Acceptance and publication

`verify` checks final hashes, picture/audio/subtitle streams, media duration,
caption ordering and bounds, and a full decoder pass. Add `--manifest` to reject
stale evidence or changed narration. This is an integrity check, not a signed
attestation: a person must check claims, listen to speech, and inspect visual
layout. A receipt does not prove a demo was actually executed and does not
authorize publishing anything.

Keep editable course sources and owned artwork in Git. For small courses, final
MP4s can live alongside the manifest; larger collections should use a separately
authorized media destination with stable links from the course index. Review
OSS attribution and media privacy before publishing. Ordinary framework release
gates still apply to any bugs discovered while making a course.
