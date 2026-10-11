# Settle the Argument. Before CI.

Working script for the 1.2 film. Generated from `story.json`; edit that file, not this one.
62 turns, 1313 spoken words; target about 13 minutes including pauses and demo holds.
Terminal turns reuse the original recorded development-build sessions until fresh 1.2 captures exist.
Not a rendered film or an approved voice performance.

## The big picture

**01 · Litai** — *big-picture*

Picture your next release. Agents write code overnight. By morning, every change has a written promise, independent acceptance, builds on Mac, Linux and Windows, an installed package that actually ran, and a receipt naming the exact commit all of that proved. Before anyone opens CI.

**02 · Litai** — *big-picture*

You review promises, not diffs. Every claim carries the evidence to challenge it. Code becomes something you can regenerate; the specification is what you defend in review. That's agentic development done properly, and Literate AI is the harness that makes it real.

## The usual AI slop pitch

**03 · Sam** — *slop*

And there it is. The usual AI slop sales pitch. Agents overnight, promises instead of diffs. I've sat through four of these this quarter.

**04 · Sam** — *slop*

I'm Sam. C++, Make, tests. My company is in a civil war: our CTO wants agent-first everything by Q3, and I lead the holdouts. Thursday's architecture review settles it.

**05 · Litai** — *slop*

Then let's settle it with evidence instead of volume. You bring the beliefs; I'll bring a terminal. One at a time.

**06 · Sam** — *slop*

Start here. Agent code is slop. Nobody can review ten thousand generated lines, so nobody does.

## Belief one: nobody can review it

**07 · Litai** — *terminal / specification*

Agreed. So don't start with the lines; review the promise. A Component states observable behavior: a greeting card takes a name and messages, returns a greeting, an identifier and word counts. Unicode whitespace included. No implementation in it.

**08 · Sam** — *contract*

A requirements doc. Those rot the day after the kickoff meeting.

**09 · Litai** — *flavors*

Not this one; every build is pinned to it. Flavors say how: Python and Make here, C++ and CMake next door. Change the promise and every lock pinned to it goes stale until it's rebuilt and verified. Change a Flavor and you still need a real toolchain and new tests. No magic porting.

**10 · Sam** — *clone*

Fine. How would I even get it?

**11 · Litai** — *clone*

Clone the repository, make install, then litai doctor checks the result. Sign in to your coding CLI separately. The install panel is a walkthrough; the terminal sessions are recorded runs. Anything without a terminal is a walkthrough card.

**12 · Litai** — *terminal / create-plan*

Nothing changes without a plan. This is the real read-only create plan, pinned to a public parent commit.

**13 · Sam** — *terminal / create-apply*

And I acknowledge it, and it writes files. Every tool writes files.

**14 · Litai** — *terminal / create-apply*

Every tool writes files. Few tell you exactly which ones before they do.

## Belief two: it grades its own homework

**15 · Sam** — *terminal / generation*

Next. The agent writes the code and the tests, then congratulates itself. Homework graded by the student.

**16 · Litai** — *terminal / generation*

Rebuild calls your chosen coding agent, with explicit permission to run generated code in this disposable project. Edited replay; the waiting is compressed.

**17 · Litai** — *terminal / source*

The generated code: normalize the recipient, count the words.

**18 · Litai** — *terminal / tests*

And its own tests. Useful, and exactly the homework you're worried about. So they aren't the judge.

**19 · Sam** — *terminal / rebuild*

Green on the first try. Which is exactly what slop looks like.

**20 · Litai** — *terminal / oracle*

Green by whose cases? These acceptance cases come from the promise, inherited and unmodified: two messages, six words, this identifier. They run separately from the agent's own tests.

**21 · Sam** — *terminal / oracle*

And if your judge were too lenient, you'd be green and smiling.

**22 · Litai** — *terminal / verify*

Yes. A lenient oracle or a wrong specification still passes; no harness catches that alone. This one puts them where your reviewers can. Skips are shown as skips, not passes.

**23 · Litai** — *repair*

And when the agent is the one that's wrong, 1.2 hands it the redacted compiler error and asks for a fresh candidate, at most twice more. Then it stops and tells you why. Not in this recording; this run didn't need it.

**24 · Sam** — *terminal / rebuild-refresh*

And every edit means paying for another agent run.

**25 · Litai** — *terminal / rebuild-refresh*

Only for what changed. Here the source came back from the cache and the rebuild took under half a minute.

## Belief three: CI is the safety net

**26 · Sam** — *cicd*

Now the big one. We have CI. Forty jobs, a matrix, a dashboard. That's the safety net.

**27 · Litai** — *cicd*

For most teams, CI is where they first learn the truth. Push and pray, queue for an hour, find Windows red, push a fix you never ran. The green run tested a merge commit, not the one you tagged. And the pipeline file is untested code holding production keys.

**28 · Sam** — *cicd*

Rude. Accurate, but rude.

**29 · Litai** — *terminal / release-check-recorded*

So move the whole gate before CI. Here's how we ship Literate AI itself. One make release-check: lint, formatting, spec and docs checks, the full test suite, a wheel, a clean install, a real installed project, live samples, and a current test receipt. Your project declares its own gate; litai release check runs it on the exact prepared commit.

**30 · Sam** — *pre-ci*

That's not a checklist, it's a hostage situation. Nobody runs all that before every merge.

**31 · Litai** — *pre-ci*

No human does. That's the superhuman part, and it's stamina, not judgment. Plan, generate, build, test, accept, package, release: every stage, every time, with a record.

**32 · Litai** — *terminal / release-check*

It even caught us making this film. A test helper under the driver's reviewed code changed without a fresh review pin. Red at driver review, on a laptop, before CI ever saw it.

**33 · Sam** — *terminal / release-check-resume*

So you push a fix and pray, like the rest of us.

**34 · Litai** — *terminal / release-check-resume*

Review, re-pin, resume. Passed gates are reused while you repair, then release evidence starts clean from gate one.

**35 · Litai** — *terminal / qualify*

Then it repeats on machines you already own. That Linux box and a Windows desktop become workers through private configuration. When workers build, test and run everything, your laptop doesn't even need the toolchains. Release qualify covers what this machine can, then the workers, and uses CI only for what's left. The record names the exact commit.

**36 · Sam** — *workers*

So CI becomes...

**37 · Litai** — *workers*

A confirmation, not a discovery. This recording qualified this Mac; the remote workers are a walkthrough.

## Belief four: it's a black box

**38 · Sam** — *html*

Fine. Even when it passes, I can't see what an agent did. It's a black box with a commit message.

**39 · Litai** — *terminal / render-html*

litai render html. One file per view, no server. The authority graph shows every Component, Flavor and skill, with hash-checked previews of their sources. Health views cover locks and verification.

**40 · Sam** — *html*

Pretty. Somebody still has to babysit it.

**41 · Litai** — *terminal / qualify-stale*

It's rebuilt from the project's own records, and a declared page that goes stale blocks qualification, like this. Build-and-test run history isn't shipped yet; the graph and health views are.

## Belief five: adoption means a rewrite

**42 · Sam** — *adoption*

My real code is years of C++. Adoption means a rewrite and six months of regret.

**43 · Litai** — *terminal / adopt-plan*

TinyXML2, pinned to a public commit. The plan inspects before it touches anything: preserve the original, take a recovery point, wrap its real build and tests.

**44 · Sam** — *terminal / adopt-apply*

Which tests? Swap CTest for an agent's opinion and Thursday's over.

**45 · Litai** — *terminal / ctest*

Its own CMake build and its own CTest, on preserved source. Nothing replaced, nothing rewritten.

**46 · Litai** — *terminal / retained*

Retained: baseline evidence recorded, and the original source still governs releases. You can stop here for good. Later stages exist; none are shown complete here.

## Belief six: it never learns

**47 · Sam** — *learning*

And agents make the same mistake every Monday. They never learn.

**48 · Litai** — *learning*

Nothing retrains a model. A lesson becomes a reviewed change to its owner: the specification if behavior was missing, a Flavor if it's platform-specific, a skill if it's technique. litai learn proposes; your review admits.

**49 · Sam** — *learning*

So 'learning' means a pull request.

**50 · Litai** — *learning*

Exactly. Reviewable, revertible, and it outlives the model.

**51 · Sam** — *lineage*

And your framework's rules rewrite my release some Tuesday.

**52 · Litai** — *terminal / update*

They arrive as a plan. This recorded update compares what you inherited, what you changed and what's new upstream. Conflicts wait for you.

**53 · Sam** — *lineage*

And when the CTO's agents swarm the repo, who assigns the work?

**54 · Litai** — *lineage*

Your forge. GitHub or GitLab issues and pull requests, by default. Agents open reviewed pull requests like everyone else.

## Belief seven: releases are where it breaks

**55 · Sam** — *terminal / package*

Last one. Demos stop at green. Our actual release artifact is still called final-final-really-final dot zip.

**56 · Litai** — *terminal / package-verify*

A local wheel, built and verified; no registry upload here. A real release is plan, prepare, check, publish: a release branch, a SemVer tag, and a check of what was actually published. Tests of one commit never qualify another.

## Thursday

**57 · Sam** — *challenge*

So our CTO is wrong. Agents don't get to ship on vibes.

**58 · Litai** — *challenge*

And the holdouts?

**59 · Sam** — *challenge*

Also wrong. Hand-reading every generated line was never the safety net. Evidence is.

**60 · Litai** — *challenge*

Argument settled. Before Thursday: install it, write one Component, render the graph, qualify on two machines. Bring the receipts.

**61 · Sam** — *challenge*

And final-final-really-final dot zip retires.

**62 · Litai** — *challenge*

It had a good run. Unlike its tests.
