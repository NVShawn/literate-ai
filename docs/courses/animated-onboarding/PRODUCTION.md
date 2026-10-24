# Animated onboarding production brief

## Story and visual direction

Audience: an engineer with an existing codebase who wants to try Literate AI
without surrendering source ownership. Sam challenges a concrete consequence;
Literate AI answers it, and the explanation changes what appears on screen.
Their dialogue is collaborative, with dry skepticism rather than alternating
sales narration. The useful outcome appears first: understand an onboarding plan
before acknowledging a mutation.

Preserve the established project identities from `../assets/PROMPTS.md`: Sam's
dark hair, beard, navy/amber engineering clothes, and LitAI's teal hair and jacket.
The new vector puppets have separate head, arm, eyes, eyebrows and mouth groups.
Nods, listening posture, blinks, gaze and short hand gestures are deterministic;
the listener rests between reactions. No portrait is edited, and no third-party
character artwork or voice likeness is used.

The browser companion and film share the same scene and recording clock. A
separate animated clip for each spoken turn prevents clip-duration rounding from
accumulating into lip/caption drift. The film's visible captions survive forwarding;
the browser offers a static-character mode, normal controls and full transcript.
No autoplay, unbounded loops or global keyboard interception are used.

## Source ledger

| Claim or choice | Authority and limit |
| --- | --- |
| Exact release wheel; Python 3.11+, Git and an authenticated coding CLI | `docs/user/getting-started.md`, preparation section; this audition is not the complete installation walkthrough. |
| Planning is read-only; apply and acknowledgement are separate | `src/literate_ai/cli/operator.py` and `litai onboard adopt --help`; the retained source-run observation in `evidence.json` proves only its explicit scope. |
| Original source remains owned by the project | Existing onboarding/adoption authority and the unchanged two-file fixture. No build, baseline execution or acceptance is claimed. |
| Plan readiness does not establish release qualification | `docs/user/framework-flow.md` and the existing lifecycle/release gates. The video explicitly refuses that inference. |
| Hybrid Motion supports controlled object/SVG animations | [Motion animate documentation](https://motion.dev/docs/animate); the optional package lock pins the actual runtime. |
| Magpie voice discovery and recorded synthesis | [NVIDIA API example](https://build.nvidia.com/nvidia/magpie-tts-multilingual/api); exact voice IDs come from the observed catalog, not a guessed list. |
| Expressive stock voice styles | [NVIDIA speech NIM documentation](https://docs.nvidia.com/nim/speech/26.05.0/tts/index.html); voice/style availability must still be observed for the endpoint being used. |

`codex-ios` was inspected locally at revision
`c3ad16c7059a882aa49adfeb162db17e5b72c118`. Its `host/voice.mjs`
normalizes the live Codex `thread/realtime/listVoices` catalog and treats requested
versus confirmed voice/model as separate observations. It supplies a live
Codex/WebRTC voice integration, not bundled downloadable TTS weights or a generic
offline narration endpoint. No private code, pairing state, live sessions or
credentials were copied, and building this package does not depend on that repo.
The applicable design lesson is live discovery, explicit speaker selection and
honest model provenance. OpenAI voice names are not sent to NVIDIA's API.

## Audio and rights

Initial cast: `Magpie-Multilingual.EN-US.Jason.Neutral` (Sam) and
`Magpie-Multilingual.EN-US.Aria` (Literate AI), observed from NVIDIA's live catalog.
Use the native sample rate and retain stock synthetic recordings with their
provider/model/voice, text/story/catalog hashes and audio identity. No real person
is cloned. [NVIDIA's governing-terms notice](https://build.nvidia.com/nvidia/magpie-tts-multilingual)
identifies the API Trial Service Terms. The package records rights context without inventing an
independent license grant for a model or a person's likeness.

Voice IDs record the requested selection from the observed catalog. The audio
response does not separately report a confirmed effective voice or model-weight
revision; neither is claimed. The named model is the API model family.

Plex fonts come from the pinned `@fontsource/ibm-plex-sans` package; its SIL Open
Font License is copied beside each generated companion. The Motion runtime's MIT
license and dependency licenses must remain with a distributed companion.

## From audition to the full episode

1. Listen to both cast samples and watch the entire short cut. Record actual
   feedback on delivery, pronunciation, pacing, expressiveness and character fit.
2. Expand into installation/acquisition, doctor, an observed adopt plan, explicit
   acknowledgement, and the first real verification/failure-recovery path. Capture
   fresh disposable-project evidence and bind the actual tested revision.
3. Carry prerequisite and authentication explanations on screen. Mark walkthroughs,
   edited replays and unexecuted steps; never present generated terminal text as
   a successful execution transcript.
4. Use the existing video CLI for final assembly/integrity, inspect every shot and
   listen to the complete soundtrack, then publish only to an authorized destination.

The existing accepted 11:52 film and its media remain intact. The native Windows
qualification timeout is unrelated to this authoring pilot and is not resolved or
relabelled by a successful animation or speech check.
