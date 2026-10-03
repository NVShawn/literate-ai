# Ten-minute pitch: voice and dialogue audition

This is a 75.76-second **work-in-progress audition**, not the completed ten-minute
film and not a human-approved replacement for the earlier public feedback edition.

- [Watch the audition](video/sam-meets-literate-ai-ten-minutes-audition.mp4)
- [Subtitles](video/sam-meets-literate-ai-ten-minutes-audition.srt)
- [Revised full narrative](../../../docs/courses/play/SCRIPT.md)
- [Opus 5.5 critique and dispositions](../../../docs/courses/play/OPUS-REVIEW.md)
- [Production and limitations](../../../docs/courses/play/README.md)

The package retains narration, images, provenance and its verified media receipt.
It can be rebuilt without speech-model access:

```sh
litai video build media/courses/ten-minute-audition/package/course.json \
  --output _build/audition-renders --font /path/to/Arial.ttf --narration recorded
```

Voices are synthetic Qwen3-TTS presets Serena and Ryan, not impersonations.
Human listening approval and full-film production remain outstanding. The older
continuous film is retained separately; this audition does not replace it.
