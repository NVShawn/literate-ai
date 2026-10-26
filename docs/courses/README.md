# Literate AI instructional courses

## Start here: the continuous video play

**[Watch or download: Eight Hours to Make Coffee.](https://github.com/jordanhubbard/literate-ai/raw/main/media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.mp4)**

Sam, a skeptical C++/Make engineer, defends an eight-hour CI loop while LitAI moves
useful feedback earlier without weakening required CI, independent review, native
qualification or release gates. Twelve animated chapters use two approved NVIDIA
Magpie voices, 86 visible captions and five source-backed project case studies with
explicit limits. Running time: **12:04.896**.

[SRT](../../media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.srt) · [WebVTT](../../media/courses/eight-hours-to-make-coffee/video/eight-hours-to-make-coffee.vtt) ·
[Production and limitations](full-onboarding/PRODUCTION.md) · [Script and annotations](full-onboarding/NARRATIVE.md) ·
[Authoring sources](full-onboarding/README.md) ·
[Media receipt](../../media/courses/eight-hours-to-make-coffee/video/video-result.json) ·
[Approval and hashes](../../verification/onboarding-1.3.1-approval.json)

The film is an illustrated walkthrough, not a recording of the five projects running.
Its retained fresh evidence is a read-only adoption plan over an unchanged fixture;
it does not claim apply, build, native execution or an eight-hour-to-minutes benchmark.
Report timestamped feedback in the public repository's issue tracker.

## Earlier cuts, retained for comparison

The [animated onboarding authoring pilot](animated-onboarding/README.md)
adds articulated Sam/LitAI characters, an offline Motion/SVG companion and recorded
NVIDIA Magpie voice auditions. The user approved the pilot voices and directed moving
cut. That approval established the treatment; the separate full-film approval is
bound to the published replacement's exact media hashes.

The superseded 1.2 film, [Settle the Argument. Before CI.](https://github.com/jordanhubbard/literate-ai/raw/v1.2.0/media/courses/sam-meets-literate-ai/video/sam-meets-literate-ai-settle-the-argument.mp4), remains available with its [chapter timestamps](play/CHAPTERS.md).

The cuts below are superseded creative drafts, not the recommended viewing path.
The [creative brief](creative-brief.md) and [accepted audition package](preview/README.md)
record how the project-owned story evolved without prescribing a cast to children.

Three short lessons pair LitAI, a female guide, with Sam, a skeptical C++/Make
engineer. Each includes two-voice narration, embedded English subtitles, and
downloadable SRT/WebVTT captions.
The original [character artwork prompts](assets/PROMPTS.md) are retained with
the owned PNG assets: [LitAI](assets/litai-guide.png) and [Sam](assets/sam-engineer.png).

| Course | Video | Captions | Editable source |
| --- | --- | --- | --- |
| Your first greenfield project | [MP4](01-greenfield-first-project/01-greenfield-first-project.mp4) | [SRT](01-greenfield-first-project/01-greenfield-first-project.srt) · [VTT](01-greenfield-first-project/01-greenfield-first-project.vtt) | [Manifest](01-greenfield-first-project.json) |
| Use machines you already have | [MP4](02-use-your-existing-machines/02-use-your-existing-machines.mp4) | [SRT](02-use-your-existing-machines/02-use-your-existing-machines.srt) · [VTT](02-use-your-existing-machines/02-use-your-existing-machines.vtt) | [Manifest](02-use-your-existing-machines.json) |
| Adopt an existing project | [MP4](03-adopt-an-existing-project/03-adopt-an-existing-project.mp4) | [SRT](03-adopt-an-existing-project/03-adopt-an-existing-project.srt) · [VTT](03-adopt-an-existing-project/03-adopt-an-existing-project.vtt) | [Manifest](03-adopt-an-existing-project.json) |

These are edited transcript replays from real sessions, **not raw screen
recordings**. The worker lesson proves local execution and explains SSH setup;
it does not claim a remote-worker run. The brownfield example actually adopted
TinyXML2 and reached the retained stage after fixing a CTest parser defect.
Read [evidence and limitations](EVIDENCE.md) before treating the examples as
release qualification.

## Rebuild or refresh

Media integrity receipts: [greenfield](01-greenfield-first-project/video-result.json),
[workers](02-use-your-existing-machines/video-result.json), and
[brownfield](03-adopt-an-existing-project/video-result.json).

Use the Literate AI revision containing this package (the new commands are not in
older wheels). On macOS with ImageMagick, ffmpeg/ffprobe, and the two installed
voices, run each manifest through the standard CLI:

```sh
litai video plan docs/courses/01-greenfield-first-project.json
litai video build docs/courses/01-greenfield-first-project.json \
  --output _build/courses --font /path/to/Arial.ttf --narration say
litai video verify _build/courses/01-greenfield-first-project-UNIQUE/video-result.json \
  --manifest docs/courses/01-greenfield-first-project.json
```

Repeat for the other two manifests. After verification and human visual/audio
review, copy the MP4, SRT, VTT and receipt into the corresponding course directory.
Builds preserve previous outputs. Change the dialogue and evidence to refresh a
lesson; supply recorded audio for portable narration or screen recordings for
new coding demonstrations. See [video authoring](../user/instructional-videos.md)
for supported backends, manifest fields, tools, integrity and publication boundaries.

## 1.3.1 animated replacement

[Eight Hours to Make Coffee](full-onboarding/README.md) expands the approved
Sam/LitAI treatment into a skeptical, source-backed pre-CI argument with five
project case studies. The 12-chapter script, animation annotations and offline
authoring/capture system are authored. All 86 approved-voice turns have been rendered
into the published 12:04.896 film, which passes deterministic chapter QA and independent
continuous-media verification. Fresh scoped evidence and complete listening, factual,
accessibility and normal-speed playback approval are recorded against exact identities.
The previous 11:52 film remains available as historical course evidence.
