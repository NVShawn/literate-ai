# Literate AI: from readable intent to executable evidence

This editable conference deck tells the product and engineering story of Literate AI:
why readable authority matters, how the evidence model evolved, what is present on the
1.4.0 development line, and which boundaries remain deliberately open.

- [PowerPoint deck](literate-ai-evolution-conference.pptx)
- [Deck specification](deck-specification.md)
- [Factual source ledger](source-notes.md)
- [Deterministic builder](build_deck.py)
- [Structural and render verifier](verify_deck.py)
- [Validation record](validation.json)

Rebuild from the repository root after `make doc-toolchain-bootstrap`:

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

The checked-in PNGs are browser renders of three labs and are the only raster assets.
All explanatory diagrams remain editable PowerPoint shapes. The deck is local
conference collateral; it has not been published to an external presentation service.

The visual evidence is retained as the [first lab](assets/00-grumpy-counter.png),
[3D lab](assets/03-impossible-furniture.png), and
[component cathedral](assets/05-component-cathedral.png) renders.
