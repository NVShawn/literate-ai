# Independent narrative review

Requested and provider-reported model: `claude-opus-5-5` (Opus 5.5).
Claude Code 2.1.280, isolated installation; tool-free review of the supplied script
and factual constraints. Reviewed draft SHA-256: `91e2a902538bb089518ab98be6ab2aac0ac0cc6a8a27f451be1723332c230b26`.

Verdict: **revise**, not approval. The current script incorporates the first
critique; it has not received a second Opus signoff or human viewing approval.

## Disposition

- Moved the skills explanation to the consequential maintenance/inheritance objection.
- Cut the PATH exchange that made an experienced engineer sound like a novice.
- Made Sam explicitly challenge the Python demo in a C++ pitch.
- Conceded that a lenient independent oracle can pass bad code; removed the spin
  that the framework's own faulty oracle was evidence of success.
- Let Sam count both exposed framework bugs and demand a bounded trial.
- Separated agent-work orchestration from repository dependency orchestration.
- Aligned terminal evidence with LitAI's explanation, splitting source from tests.
- Replaced the conversion speech with a trial, not a convert.
- Retained setup, remote, inheritance and deployment limitations. Corrected the
  reviewer's 'inherited, not copied' wording: inheritance materializes files but
  records provenance; it does not make local copies or maintenance disappear.

## Original critique

# Critique: "Ten Minutes. Keep Your Makefile."

Turn indices below are zero-based and refer to the **original** script. Word counts are by hand, accurate to about ±3%.

## Verdict: **REVISE**

The core is worth keeping:
- Sam spotting the red failure (24) is the best beat in the film.
- The brownfield-to-retained sequence (32–39) is real evidence.
- The trial terms in 60 are the right shape.

It doesn't ship as written, for four reasons:
- It runs about 45–90 seconds over.
- Nothing executes until about 3:00.
- The failure scene dodges its own sharpest objection.
- In the back half, Sam turns into a question-feeder and the film sags into diagrams.

## Ratings

| Dimension | Score | Why |
|---|---|---|
| Dramatic causality | **6/10** | Turns 22–31 form a real causal chain: claim, failure, challenge, recovery, limits. Elsewhere Sam just asks for the next agenda item. Eight Sam turns open with "And…" (10, 16, 18, 30, 38, 48, 52, 58). That is a checklist interview, not an argument. |
| Skepticism | **6/10** | Good jabs, but Sam feeds lines (30, 38, 40). In 58 he states the distinction Litai is supposed to make. He also lets the biggest hits go: the *independent* oracle was the broken part, the tool showed two of its own bugs, Python is pitched to a C++ engineer, and the skips are never explained. |
| Clarity | **5/10** | Components and Flavors land. After that come undefined terms: "content identities," "resolver," "recipe," "routing," "catalog coordinate," "Standard-bound rebind," "fix the owner," "lifecycle." "Orchestration" means two different things (53 and 59), and nobody flags it. Agentis is never introduced. |
| Factual restraint | **8/10** | Unusually honest. Gaps against your constraints: the wrong oracle is never said to be tracked for an upstream fix; lineage is never labeled a walkthrough. Some lines assert behavior that isn't shown: "C++ plus Make is a valid combination" (7), "resolver rejects…" (13), "peer-parent definitions are rejected" (47). "The gate stopped the run" (25) dresses a false failure caused by your own defect as a gate success. About nine serial disclaimers make Litai sound defensive. |
| Visuals | **5/10** | Captures are systematically one turn early: Sam's lines play over the evidence Litai then describes (18/19, 22/23, 32/33, 34/35). Install/PATH/doctor panels waste screen time. Turns 36–56 are about 3.5 minutes of talking heads over diagrams. |
| Pacing | **4/10** | About 1,520 words. Roughly 330 words of taxonomy and setup come before the first capture. Grab-bag and updates are each covered twice. After TinyXML2, the film runs downhill. |

## Runtime estimate (as written)

About **1,520 spoken words in 63 turns**.

| Rate | Speech only | + pauses and dwell |
|---|---|---|
| 145 wpm | 10:29 | **~11:30** |
| 150 wpm | 10:08 | **~11:10** |
| 155 wpm | 9:48 | **~10:50** |

The pause allowance assumes:
- 62 speaker handoffs at 0.4–0.7 s each, about 25–43 s.
- 7 chapter transitions, about 7–14 s.
- Letting the failure and CTest results sit on screen, about 15–30 s.

TTS will also expand "C++," "TinyXML2," and "Python three eleven." **Target about 1,250–1,300 words.** The five replacements below land at about **1,255 words in 53 turns**, roughly **9:10–9:45** with pauses.

---

## The five biggest problems and their fixes

### 1. A lecture before any demo (turns 5–19)

**The problem.** Sam opens with "show me." The script answers with about two minutes of Components, Flavors, two skill lanes, resolvers, nested skills, install steps, PATH, and doctor. The script admits it in turn 14: *"That's a plausible diagram. Show me something executing."*

The setup section also damages Sam's character. A competent C++ engineer does not ask what to do when a command isn't on his PATH (16). That line condescends to him and to the audience.

The skill-lane material belongs where the grab-bag objection actually bites, in the inheritance section (see problem 4). There is also a real objection the script never uses: the demo is Python, and the skeptic is a C++ engineer.

**Replace original 5–19 (15 turns, about 375 words) with these 9 turns (about 200 words):**

```json
{"speaker":"litai","shot":"contract","chapter":"One promise, two layers","text":"One promise. A greeting card: a name and messages in; a greeting, a recipient identifier and counts out. That promise is the Component, Unicode whitespace included. It says nothing about how it's built."},
{"speaker":"sam","shot":"contract","text":"So, a requirements file. We've had those since before the coffee machine had firmware."},
{"speaker":"litai","shot":"flavors","text":"Yes, except this one is bound to the work. Flavors are the build choices: language, build system, platform, packaging. Swap a Flavor and you still need a real toolchain and new tests. No magic porting. The promise stays put."},
{"speaker":"sam","shot":"flavors","text":"And your recorded example is Python. You brought Python to a C++ interview."},
{"speaker":"litai","shot":"flavors","text":"Guilty. The smallest recorded run is Python plus Make. Your kind of code shows up in a few minutes: C++, CMake, CTest."},
{"speaker":"sam","shot":"clone","chapter":"Recorded greenfield session","text":"Then stop drawing and run something. Starting with where I get it."},
{"speaker":"litai","shot":"clone","text":"Repository and commands are on screen. On macOS: Python three eleven or newer, Git and Make, then make install. This panel is a walkthrough. The terminal sessions are a recorded development build, revisions beside the film."},
{"speaker":"sam","shot":"terminal","capture":"create-plan","text":"A development build. So, tomorrow's features on loan."},
{"speaker":"litai","shot":"terminal","capture":"create-plan","text":"Possibly, which is why it's labeled. This is its real read-only create plan, pinned to a public parent commit. Destination and choices are reviewable; nothing applies until you acknowledge it."}
```

**Also change turn 20:** set `"capture"` to `"create-apply"`.

**What this does:**
- The first capture now lands at about 1:30 instead of about 3:00.
- The unverified "C++ plus Make is valid" claim is gone.
- "No fictional release tag" is gone; it implies other demos have fake tags.
- The "few minutes" promise is paid off by TinyXML2.

### 2. The failure scene dodges its own sharpest objection (turns 25–29)

**What happened.** The *independent* acceptance oracle was the broken component. That is the strongest thing a skeptic could say. Sam never says it. Instead, turn 28 shifts the blame to "the specification," which was actually correct.

**Litai's framing has three problems:**
- *"Correct objection"* responds to a joke as if it were an objection.
- *"The gate stopped the run"* spins a false failure, caused by your own defect, as a gate win.
- *"We should fix the owner"* is vague, and "owner" is unexplained jargon. The required fact, that the defect is tracked for an upstream repair and still unfixed, is never stated.

**Something important is also missing.** A wrong oracle can also fail in the lenient direction, and then nothing turns red. Sam needs to say this, and Litai needs to concede it.

**Replace original 25–29 with:**

```json
{"speaker":"litai","shot":"terminal","capture":"rebuild-failed","text":"Fair. The Component requires messages; the inherited acceptance case got that wrong. That's a defect in our starter, not the agent's. It's tracked for an upstream fix, and it isn't fixed yet."},
{"speaker":"sam","shot":"terminal","capture":"oracle","text":"Hold on. Your independent check, the thing meant to catch the agent, was the broken part. And then you edited it until it went green."},
{"speaker":"litai","shot":"terminal","capture":"oracle","text":"Then judge my edit against the written contract, not the generated code: two messages, six words, this identifier. We corrected this disposable project's acceptance cases and reran. Local recovery, disclosed. Upstream is still wrong."},
{"speaker":"sam","shot":"terminal","capture":"rebuild","text":"And if it had been wrong the other way, too lenient, you'd be green and smiling."},
{"speaker":"litai","shot":"terminal","capture":"verify","text":"Yes. A lenient oracle passes bad code, and so does a wrong specification; the harness can't detect either on its own. It puts the contract, the acceptance cases and the results where a reviewer can challenge them. Recorded results, skips included."}
```

**Editor note on skips:** If the verify capture doesn't show *why* each test was skipped, add a lower-third that does. Otherwise a skeptical audience will assume the worst. Do not drop the word "skips."

### 3. Sam feeds lines, and the ending converts him too neatly (turns 30, 35–39, 62)

**Feeder lines.** These are the vendor's thesis or the vendor's setup in Sam's mouth:
- 30, "And done still isn't shipped"
- 38, "And the catch?"
- 40, "Good."

**Missed objection.** Turn 35 discloses a *second* framework bug, in the CTest-summary parser. A real skeptic would count the bugs aloud. Doing so also sets up the trial honestly.

**Ending.** Turn 62, *"You've sold me a way to argue with the machine. That I can use,"* is marketing copy delivered by the skeptic. The trial in 60 is earned; 62 gives it back.

**Replace 30:**
```json
{"speaker":"sam","shot":"terminal","capture":"package","text":"Green on your machine. My release checklist is longer than that."}
```
**Replace 31:**
```json
{"speaker":"litai","shot":"terminal","capture":"package-verify","text":"Agreed. Local wheel and package verification; no registry upload. Publishing is a separate decision. Tests of one revision don't qualify another, and a green build doesn't prove the installed package works."}
```
**Replace 35 through 39:**
```json
{"speaker":"litai","shot":"terminal","capture":"ctest","text":"Its own CMake targets and CTest, run on the preserved source. In rehearsal we also hit a bug in our CTest-summary parser; it was repaired and the harness run repeated."},
{"speaker":"sam","shot":"adoption","text":"That's two bugs in your own tool in one demo. Why does it go anywhere near my CI?"},
{"speaker":"litai","shot":"terminal","capture":"retained","text":"Fair count. It shouldn't, until a trial earns it. What a trial gets you: a repeatable baseline, recorded build and test evidence, and a written account of what's authoritative. Wrapped: the harness surrounds your code. Retained: that preserved code has its required baseline evidence."},
{"speaker":"sam","shot":"adoption","text":"And then comes the upsell to a rewrite."},
{"speaker":"litai","shot":"adoption","text":"There are later stages: draft specifications, regenerate, and qualify against independent evidence before authority moves. None of that is complete in this demo. You can stop at retained; your original source still governs releases."}
```
**Replace 62:**
```json
{"speaker":"sam","shot":"challenge","text":"Don't look pleased. You've earned a trial, not a convert."}
```

**Verify before recording turn 35:** Is the TinyXML2 capture from the build *after* the parser repair, and was the repair made in the framework or locally? Say whichever is true.

### 4. The back half sags: repetition, conflated "orchestration," stacked disclaimers (turns 40–59)

**Repetition:**
- The grab-bag objection is answered at 10–13 and again at 42–43.
- Updates are covered at 44–45 and again at 56–57.

**Undefined or unlabeled:**
- "Standard-bound projects may need a reviewed rebind" (57) uses a term the film never defines.
- Agentis appears with no introduction.
- Lineage is never labeled a walkthrough.

**Conflated "orchestration":** It names Agentis (53) and multi-repository composition (59). A first-time viewer will merge the two, and that is precisely the distinction the brief requires.

**Visuals:** Turns 36–56 are about 3.5 minutes with no terminal capture.

**Replace original 40–59 (20 turns, about 490 words) with these 16 turns (about 375 words).** The skill lanes move here from turns 10–13, and the update capture moves in to break the diagram run.

```json
{"speaker":"sam","shot":"lineage","chapter":"Shared practice, workers, and two kinds of orchestration","text":"Fine. Now there's a framework-shaped pile of files beside my code, times every repository. Does my agent read that whole grab bag to fix a typo?"},
{"speaker":"litai","shot":"lineage","text":"No. Agent skills guide tasks like adoption or release; generation skills guide building one Component with its Flavors. Only the selected ones are active. And the pile is inherited, not copied: framework, your team's conventions, then each product. This part is a diagram, not a run."},
{"speaker":"sam","shot":"lineage","text":"Inherited means Tuesday's upstream cleverness rewrites Monday's release."},
{"speaker":"litai","shot":"terminal","capture":"update","text":"Not silently. A recorded revision isn't a live subscription. An update plan, like this recorded one, compares what you inherited, what you changed and what's new upstream. Conflicts wait for a decision. Upgrading the tool itself is separate, and an old receipt isn't fresh proof."},
{"speaker":"sam","shot":"lineage","text":"And when two parents disagree?"},
{"speaker":"litai","shot":"lineage","text":"Ambiguous definitions are rejected. A product can override deliberately, with provenance. Prefer a narrow project rule to copying a whole policy. Inheritance shares practice; it doesn't remove review."},
{"speaker":"sam","shot":"skills","text":"Including this video? Every project inherits two talking heads?"},
{"speaker":"litai","shot":"skills","text":"No. The video-production skill is reusable; this cast belongs to this repository. Your payment library is not contractually obliged to include a sarcastic man."},
{"speaker":"sam","shot":"workers","text":"I supply that service free. Now the bill: a cluster, a platform team, another subscription?"},
{"speaker":"litai","shot":"workers","text":"One machine, as this recording used. Compatible machines you already have can become workers: addresses and credentials in private configuration, then probe toolchains and agent access. Walkthrough, not an SSH run."},
{"speaker":"sam","shot":"pipeline","text":"And Agentis, the orchestrator? Two systems supervising one agent sounds like two managers in one meeting."},
{"speaker":"litai","shot":"pipeline","text":"Different jobs. The orchestrator assigns work. Literate AI structures specification, generation, checks and release evidence. A prompt saying be careful isn't a gate. No deployed Agentis integration is shown here."},
{"speaker":"sam","shot":"orchestration","text":"My product is three repositories that depend on each other. Is that inheritance, or more orchestration?"},
{"speaker":"litai","shot":"orchestration","text":"Repository orchestration, a different one from Agentis; sorry about the word. It records child pins and dependency order. Inheritance shares practice; Agentis assigns agent work. Three graphs. Each child still needs its own evidence. Diagram, not a deployment."},
{"speaker":"sam","shot":"pipeline","text":"AI slop with a receipt is still AI slop."},
{"speaker":"litai","shot":"pipeline","text":"Yes. A receipt records checks; it doesn't certify taste, usefulness or requirements. Independent acceptance is how you challenge the output. Humans need that too. We've just been better at declining supervision."}
```

Turns 60 and 61 stay as written. Ending on the slop objection leads straight into Sam's "Time."

**Verify before recording:**
- Is Agentis accurately described as "the orchestrator"?
- Is the `update` capture a recorded session rather than a mock?
- Do the pinned builds actually implement ambiguous-parent rejection and provenance? If not, say "designed to."

### 5. Captures run one turn ahead of the narration

**The pattern.** The evidence appears under Sam's skeptical line, then disappears while Litai describes it. In a sales demo built on "inspect the evidence," this costs credibility. These are field-level fixes; turns 18/19 are already handled in problem 1, and 25–29 and 30–37 already carry correct captures in problems 2 and 3.

| Original turn | Current capture | Change to |
|---|---|---|
| 20 (Sam) | generation | `create-apply` |
| 22 (Sam) | source | `generation`, held as the tail of the replay |
| 23 (Litai) | tests | split into two turns, below |
| 24 (Sam) | rebuild-failed | keep; hold the red for at least 1.5 s *before* "There. Red." so Sam is visibly finding it |
| 32 (Sam) | adopt-plan | no capture; shot `adoption` |
| 33 (Litai) | adopt-apply | `adopt-plan` |
| 34 (Sam) | ctest | `adopt-apply` |

**Split turn 23:**
```json
{"speaker":"litai","shot":"terminal","capture":"source","text":"It doesn't. It makes the mechanism inspectable before we touch your parser. This generated code normalizes the recipient and counts words."},
{"speaker":"litai","shot":"terminal","capture":"tests","text":"These generated tests are useful, but they're not our independent definition of correct."}
```
If the renderer requires speakers to alternate, keep turn 23 whole on `source` and put `tests` in a split screen.

**Recommended on-screen badges.** Put a persistent badge on every terminal shot: **"RECORDED DEV BUILD · rev ‹hash›"**. Put one on every diagram: **"WALKTHROUGH, NOT EXECUTED"**. Keep the short spoken tags already in the replacements. The badges let those tags stay brief without weakening the disclosure.

---

## Optional trims if a slow TTS voice pushes past 9:50 (about 25 words)

- **1:** "I'm Litai, from Literate AI. Keep your tools; I'm selling a harness, not a religion. The interesting part is between an agent saying done and a product being ready." (saves about 5)
- **21:** "Here. Rebuild invokes the selected coding agent, with explicit permission to run generated code in this disposable project. Edited replay; the waiting is compressed." (saves about 8)
- **3:** Cut "Let's inspect one promise before generating anything." Sam's next line already narrows to one example. (saves about 7)
- **55 replacement:** Cut "Humans need that too." The joke still lands without it. (saves about 4)

## Is the taxonomy comprehensible to a first-time viewer?

**As written: partly.**

| Term | Verdict | Why |
|---|---|---|
| **Component** | Lands | The greeting card makes it concrete. |
| **Flavor** | Mostly lands | "Target rules and selected technique" (9) is opaque. |
| **Skills** | Does not land | "Two lanes," "pinned through its Component and Flavors," "content identities," "resolver," "nested skills," and "recipe" all arrive before the viewer has a reason to care. |
| **Inheritance** | Lands as an idea | The mechanics ("catalog coordinate," "routing," "workflows," "Standard-bound rebind") are lecture material. |
| **Orchestration** | Actively confusing | Two meanings, no flag. |

A sales demo only needs the viewer to leave with four sentences. Put them on screen as one-line lower-thirds the first time each term is spoken:

- **Component:** what it must do.
- **Flavor:** what it's built with.
- **Skill:** instructions loaded only for the job at hand.
- **Parent repository:** where shared skills and conventions live.

The revised turns teach exactly that much and nothing more.

## Smaller items not covered above

- **31 (original):** "a successful compile" is odd for a Python wheel. The replacement says "a green build."
- **1:** "I represent Literate AI" sounds like an announcer. The trim above fixes it.
- **2:** Sam's "YAML" jab is never answered. That's fine; leave it as his assumption.
- **The video-production joke (48–49 in the original)** is the most concrete demonstration of skill scoping in the film. Keep it, shortened as in the replacement.
- **Turn 60** is right as written: one library, retained, measure the overhead, keep the Makefile, an exit if it's busywork. With the new 62, Sam ends committed to a trial but unconverted, which is what the brief asks for.
