# Greenfield creative review cut

This is the repository-owned replacement opening, not a reusable cast template.
The [storyboard](storyboard.json) contains the dialogue and voice choices.
[Production source](prepare.py) composes illustrated shots, a real public-repository
screenshot, and local neural audio into a neutral `litai video` manifest.
The [creative brief](../creative-brief.md) owns the full-course direction.

The cut uses [Kokoro through MLX-Audio](https://github.com/Blaizzy/mlx-audio/blob/main/docs/models/tts/kokoro.md)
on Apple Silicon, with original synthetic voice presets, not a cloned speaker.
The course-local [dependency file](requirements-neural.txt) is not inherited and
does not add speech libraries to the framework's runtime dependencies. Use an
isolated environment beneath `_build` after reviewing those dependencies. Download
the public `mlx-community/Kokoro-82M-bf16` model once and retain its exact snapshot.
Neither project narration nor credentials are submitted to a speech service.

```sh
python docs/courses/preview/prepare.py \
  --output _build/course-review-v2 \
  --font /path/to/Arial.ttf \
  --model /path/to/downloaded/kokoro/snapshot \
  --repository-image /path/to/public-repository-screenshot.png
litai video build _build/course-review-v2/course.json \
  --output _build/course-review-renders \
  --font /path/to/Arial.ttf --narration recorded
```

The first command uses the isolated neural-production Python; the second uses the
framework CLI. Generated audio and visuals remain in the authoring package so a
subsequent media rebuild needs neither MLX nor model access. `production.json`
records model snapshot, voices, tool version and storyboard identity.

The installation shots are labeled walkthroughs, not recordings of successful
installation. The full replacement still needs captured setup and coding sessions.
This review cut is for judging voice quality, dialogue, visual direction and pace;
it is not creative approval, a completed course, or release qualification.
