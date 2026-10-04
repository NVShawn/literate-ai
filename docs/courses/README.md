# Literate AI instructional courses

## Start here: the continuous video play

**[Watch or download: It Builds. Can We Ship It?](https://github.com/jordanhubbard/literate-ai/raw/main/media/courses/sam-meets-literate-ai/video/sam-meets-literate-ai.mp4)**

Sam and LitAI introduce themselves, argue about agents and engineering habits,
work through real greenfield and TinyXML2 sessions, and finish with updates,
repository inheritance and orchestration. One continuous film, two neural voices,
visible captions, and SRT/VTT sidecars. This is the **public feedback edition**.
Running time: **13:38**. [Chapter timestamps](play/CHAPTERS.md).

[SRT](../../media/courses/sam-meets-literate-ai/video/sam-meets-literate-ai.srt) · [WebVTT](../../media/courses/sam-meets-literate-ai/video/sam-meets-literate-ai.vtt) ·
[Production and limitations](play/README.md) · [Script](play/story.json) ·
[Rebuildable manifest](../../media/courses/sam-meets-literate-ai/package/course.json) · [Session excerpts](../../media/courses/sam-meets-literate-ai/package/sessions.json) ·
[Media receipt](../../media/courses/sam-meets-literate-ai/video/video-result.json)

The coding sections are edited terminal replays of actual execution, not raw screen
recordings. Installation and remote-worker setup are labeled walkthroughs. An
inherited-starter acceptance defect is shown honestly, including its demo-local
correction; the framework-level repair is still open. Report timestamped feedback
in the public repository's issue tracker.

## Earlier cuts, retained for comparison

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
