# Two-hour symposium running order

Use the [browser deck](../presentations/literate-ai-evolution-conference/index.html)
as the main narrative. Keep the [attendee hub](README.md) open in a second tab and
download the offline kit before presenting. The deck and individual labs open from
disk, so a lost network connection need not interrupt the session.

| Minutes | Segment | Material and aim |
| --- | --- | --- |
| 0–15 | Motivation and onboarding | Establish the problem, then show the 12:04 film. Frame its waiting anecdote as fiction rather than a measured benchmark. |
| 15–35 | Core concepts | Components, Flavors, skills, workflow, locks and the distinction between generated tests and independent acceptance. Use the deck's diagrams and the framework-flow guide. |
| 35–55 | Lessons across releases | Explain how readable intent grew into explicit lifecycle and evidence boundaries. Separate the published 1.3.0 distribution, unfinished 1.3.1 patch and 1.4 development work. |
| 55–80 | Interactive examples | Start with Grumpy Counter. Inspect its specification, sources and surrounding harness, then choose two richer labs. Clearly identify the current examples as authored demonstrations. |
| 80–105 | Adoption, navigation and evidence | Walk through the HTML repository explorer documentation, onboarding plan, selected targets, worker alignment, receipts and the limits of reusing earlier results. |
| 105–120 | Questions and next steps | Return to the attendee hub. Point to installation, getting started, the user guide, source and issue tracker. |

## Presenter controls

- Deck: arrow keys, Page Up/Down or Space advance; **N** toggles notes, **F** toggles fullscreen. The URL hash preserves the current slide.
- Film: enable English captions through the player controls. The MP4 and both subtitle formats are in the download kit.
- Labs: open each `index.html` directly. Audio examples require an explicit click to start sound. The source inspector and harness panel explain what the example actually uses.
- Handout: print the HTML deck from the browser with background graphics enabled. The compatibility PowerPoint is available for editing.

## Claims to keep precise

The film, deck and browser demonstrations are usable conference material. They do
not establish that 1.3.1 has shipped, that every example is a generated canonical
Component graph, or that an earlier receipt qualifies changed inputs. Show the
current-feature guide and recorded release goals when discussing availability.

If an optional live command or network service fails, use the retained source and
documented boundary. Explain the observed failure instead of substituting an
illustration for a successful live run.
