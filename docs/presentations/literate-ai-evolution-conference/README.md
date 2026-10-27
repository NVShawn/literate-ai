# Literate AI: from readable intent to executable evidence

This browser-native conference deck tells the product and engineering story of Literate AI:
why readable authority matters, how the evidence model evolved, what is present on the
1.4.0 development line, and which boundaries remain deliberately open.

- [Open the standalone HTML5 deck](index.html) — the primary conference artifact
- [PowerPoint compatibility export](literate-ai-evolution-conference.pptx)
- [Deck specification](deck-specification.md)
- [Factual source ledger](source-notes.md)
- [Deterministic HTML5 builder](build_html_deck.py)
- [HTML5 structural verifier](verify_html_deck.py)
- [HTML5 validation record](html-validation.json)
- [PowerPoint builder](build_deck.py) / [verifier](verify_deck.py) / [validation](validation.json)

The primary deck is one self-contained HTML file. Open it directly in any modern
browser: it makes no network requests and needs no Microsoft or Google ecosystem.
Use arrow keys, Page Up/Down, or Space to navigate; `N` toggles speaker notes, `F`
toggles fullscreen, touch gestures work on mobile, and browser printing produces one
16:9 slide per page. The URL hash preserves the current slide.

Rebuild and structurally verify the HTML5 artifact with the system Python:

```bash
python3 docs/presentations/literate-ai-evolution-conference/build_html_deck.py
python3 docs/presentations/literate-ai-evolution-conference/verify_html_deck.py
```

[verify_html_browser.mjs](verify_html_browser.mjs) is the real-browser acceptance
driver. It accepts explicit
Puppeteer and Chrome paths so repository verification does not silently provision a
browser or depend on a workstation-global Node installation.

The optional editable PowerPoint compatibility export still uses the document toolchain:

```bash
_build/doc-toolchain/bin/python \
  docs/presentations/literate-ai-evolution-conference/build_deck.py
```

After rendering all slides, record the structural and visual review:

```bash
_build/doc-toolchain/bin/python \
  docs/presentations/literate-ai-evolution-conference/verify_deck.py \
  --render-dir _build/conference-deck/rendered --visual-review pass
```

The checked-in PNGs are browser renders of three labs and are the only raster assets;
the builder embeds them as data URIs. Explanatory diagrams are native HTML and CSS.
This concrete deck is usable now. General-purpose Rich HTML5 presentation authoring,
routing, and publication remain a distinct 1.4 roadmap feature.

The visual evidence is retained as the [first lab](assets/00-grumpy-counter.png),
[3D lab](assets/03-impossible-furniture.png), and
[component cathedral](assets/05-component-cathedral.png) renders.
