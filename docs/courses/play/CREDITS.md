# Credits and asset sources

Every asset in *Settle the Argument. Before CI.* is listed here with its source and
license. `production.json` in each authoring package repeats the exact fonts,
tool versions and scene-script hashes used for that render.

## Original to this repository

| Asset | Source | Notes |
| --- | --- | --- |
| LitAI and Sam portraits | [`../assets`](../assets/PROMPTS.md) | Fictional characters; no real likeness |
| 3D scenes | [`scenes/blender_scenes.py`](scenes/blender_scenes.py) | Generic shapes, no logos; labeled ILLUSTRATION in the film |
| Cards, captions, close-ups | [`scenes/cards.py`](scenes/cards.py) | Composed from the portraits and text |
| Sound bed and UI cues | [`scenes/sound.py`](scenes/sound.py) | Synthesized by ffmpeg formulas; no recordings or samples |
| Terminal and file pages | [`collect.py`](collect.py) evidence | Excerpts of recorded runs and repository files |

## Third-party inputs

| Asset | Version | License | Use |
| --- | --- | --- | --- |
| [Inter](https://github.com/rsms/inter/releases/tag/v4.1) | 4.1 (`Inter-4.1.zip` sha256 `9883fdd4…b11e`) | SIL OFL 1.1 | Captions, labels; Inter Display Bold for titles |
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono/releases/tag/v2.304) | 2.304 (`JetBrainsMono-2.304.zip` sha256 `6f6376c6…7bbf`) | SIL OFL 1.1 | Terminal pages |
| [Qwen3-TTS CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice), [MLX 4-bit conversion](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit) | snapshot `f35faf19b0cc` | Apache-2.0 (model card) | Local narration with built-in presets Serena and Ryan; no voice cloning, no cloud speech |
| [TinyXML2](https://github.com/leethomason/tinyxml2/tree/8224e427b655b83dae5e2298f1e6919523a78737) | `8224e427` | zlib | Filenames and harness evidence only; implementation not shown |
| Cytoscape.js (inside recorded `litai render html` output) | 3.34.3 | MIT | Appears in screenshots of the rendered graph page |

Fonts are downloaded into the private build directory, not committed; the film
embeds rendered pixels, which the OFL permits. Model weights are never
redistributed: only generated narration ships.

## Tools (not redistributed)

Blender 5.2.2 LTS (3D rendering), ImageMagick 7.1.2, ffmpeg 9.0.2, mlx-audio 0.5.7,
Playwright with the locally installed Chrome (screenshots of local HTML files).
Speech-recognition checks ([`listen.py`](listen.py)) use a local Whisper large-v3
turbo snapshot; its transcripts are QA notes, never part of the film.
