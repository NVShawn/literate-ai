# Research ledger for Eight Hours to Make Coffee

Research date: **2026-10-24**. Public GitHub default-branch trees and file contents
were retrieved through the GitHub API, then pinned to commits below. Root project
definitions were cross-checked against actual contracts, Make entrypoints, verifier
code/tests and committed records. No external project was built or executed for
this research. A committed receipt is a historical repository record; this ledger
does not claim it is current for every subsequent HEAD or reproduces its result.

[Machine-readable snapshot](research-snapshot.json) records file hashes and public
URLs. The [narrative](NARRATIVE.md) cites the claim IDs below. There is no adoption
rate, market-share, revenue or controlled before/after performance study here.
The five projects are not five uniformly finished products or fully admitted trees.

## Framework authority

The framework sources below are pinned to main `c345e652b91863e72526f25f8e31a03ec34bec01`.
The published acquisition baseline observed in GitHub Releases is **v1.2.0**.
The v1.2.0 tree contains all nine language Flavor files named in the script, but
**not `os-freebsd`**. The FreeBSD Flavor is a current development-source feature;
native framework/project admission remains explicitly scoped/open. The film must
identify that distinction rather than imply the latest release supplies it.

- **F1** [docs/user/getting-started.md](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/docs/user/getting-started.md): five inputs; installed-wheel preparation; read-only plan and original-source boundary.
- **F2** [docs/user/framework-flow.md](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/docs/user/framework-flow.md): full lifecycle, target/worker distinction, custody and bounded HTTP/library acceptance.
- **F3** [skills/agent/develop-in-production-workflow/staging/dev/SKILL.md](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/skills/agent/develop-in-production-workflow/staging/dev/SKILL.md): local inner loop and asynchronous CI confirmation, without weakening outer requirements.
- **F4** [skills/agent/develop-in-production-workflow/staging/dev/land/SKILL.md](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/skills/agent/develop-in-production-workflow/staging/dev/land/SKILL.md): required CI before landing.
- **F4** [skills/agent/align-workers/SKILL.md](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/skills/agent/align-workers/SKILL.md): live alignment and capable user-supplied workers first.
- **F5** [src/literate_ai/cli/operator.py](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/src/literate_ai/cli/operator.py): operator/onboarding implementation; no run is claimed.
- **F6** [docs/user/test-matrix.md](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/docs/user/test-matrix.md): native worker matrix and FreeBSD qualification limitations.
- **F6** [flavors/os-freebsd/flavor.md](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/flavors/os-freebsd/flavor.md): current development FreeBSD profile; native identity separate from host.
- **F2** [samples/full-stack-rust-js/component.md](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/samples/full-stack-rust-js/component.md): Rust/Node host-observed two-stage JSON contract; not a deployed browser UI.
- **F2** [samples/_harness/full-stack-rust-js/acceptance/oracle.json](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/samples/_harness/full-stack-rust-js/acceptance/oracle.json): explicit expected results for the full-stack sample.

F6 also covers the source catalog: [`python`](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/flavors/lang-python/flavor.md), [`cpp`](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/flavors/lang-cpp/flavor.md), [`rust`](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/flavors/lang-rust/flavor.md), [`javascript`](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/flavors/lang-javascript/flavor.md), [`typescript`](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/flavors/lang-typescript/flavor.md), [`elixir`](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/flavors/lang-elixir/flavor.md), [`go`](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/flavors/lang-go/flavor.md), [`swift`](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/flavors/lang-swift/flavor.md), [`zig`](https://github.com/jordanhubbard/literate-ai/blob/c345e652b91863e72526f25f8e31a03ec34bec01/flavors/lang-zig/flavor.md). Presence of a Flavor is not proof of every language/OS combination.

## BookScan

Inspected commit: `cecfb84169e2166407219febe0cd66aa9901c69e`.

Python/PySide6 document-camera application, adopted with retained original source.
The project definition binds the standard lifecycle and a retained-parity receipt;
its wrapper preserves directly observed build/test stages and explicitly has no
proven package stage. The root Makefile forwards check/smoke to the implementation.
Tests actually name cancellation of a late camera callback, save rollback, durable
book/page order, original-resolution crop, selected-book PDF export and preservation
of an existing PDF on export failure. The committed retained receipt records **126**
tests. This is inspected repository evidence, not a fresh run or certification of
all cameras and OS combinations. Its PROJECT limits camera evidence to the tested
NETUM device and defers native regeneration. Do not call it spec-generated Python.

- **B1** [literate.project.json](https://github.com/jordanhubbard/bookscan/blob/cecfb84169e2166407219febe0cd66aa9901c69e/literate.project.json)
- **B1** [PROJECT.md](https://github.com/jordanhubbard/bookscan/blob/cecfb84169e2166407219febe0cd66aa9901c69e/PROJECT.md)
- **B2** [components/legacy-project-wrapper/component.md](https://github.com/jordanhubbard/bookscan/blob/cecfb84169e2166407219febe0cd66aa9901c69e/components/legacy-project-wrapper/component.md)
- **B2** [Makefile](https://github.com/jordanhubbard/bookscan/blob/cecfb84169e2166407219febe0cd66aa9901c69e/Makefile)
- **B3** [components/legacy-project-wrapper/implementation/tests/test_workflow.py](https://github.com/jordanhubbard/bookscan/blob/cecfb84169e2166407219febe0cd66aa9901c69e/components/legacy-project-wrapper/implementation/tests/test_workflow.py)
- **B3** [components/legacy-project-wrapper/implementation/tests/test_smoke.py](https://github.com/jordanhubbard/bookscan/blob/cecfb84169e2166407219febe0cd66aa9901c69e/components/legacy-project-wrapper/implementation/tests/test_smoke.py)
- **B4** [verification/current.json](https://github.com/jordanhubbard/bookscan/blob/cecfb84169e2166407219febe0cd66aa9901c69e/verification/current.json)
- **B4** [.literate/legacy-harness-baseline.json](https://github.com/jordanhubbard/bookscan/blob/cecfb84169e2166407219febe0cd66aa9901c69e/.literate/legacy-harness-baseline.json)

## Aviation

Inspected commit: `7b41ed2c6de8d348b13a9dfcb3d30a4ee7a9cda2`.

Retained monorepo with a real Literate AI definition, a retained wrapper and a
separate flight-track analysis contract. The root Makefile delegates to retained
commands. The implementation Makefile has Node, Python, Go and Clojure build/test
surfaces; this does not assert a canonical Clojure generation Flavor or that every
suite ran. Some optional test branches explicitly report unavailable suites as
skipped. The retained wrapper names proven build, test and package stages; the
committed receipt records **273** tests at its recorded scope. The flight analyzer
contract covers KML encodings, bounded/untrusted XML, aircraft association before
storage, track conditioning, uncertain units and browser presentation. These are
concrete requirements, not a claim this research executed or independently proved
every behavior. The project explicitly excludes real flight-operation approval.

- **A1** [literate.project.json](https://github.com/jordanhubbard/Aviation/blob/7b41ed2c6de8d348b13a9dfcb3d30a4ee7a9cda2/literate.project.json)
- **A1** [README.md](https://github.com/jordanhubbard/Aviation/blob/7b41ed2c6de8d348b13a9dfcb3d30a4ee7a9cda2/README.md)
- **A2** [components/legacy-project-wrapper/component.md](https://github.com/jordanhubbard/Aviation/blob/7b41ed2c6de8d348b13a9dfcb3d30a4ee7a9cda2/components/legacy-project-wrapper/component.md)
- **A2** [Makefile](https://github.com/jordanhubbard/Aviation/blob/7b41ed2c6de8d348b13a9dfcb3d30a4ee7a9cda2/Makefile)
- **A2** [components/legacy-project-wrapper/implementation/Makefile](https://github.com/jordanhubbard/Aviation/blob/7b41ed2c6de8d348b13a9dfcb3d30a4ee7a9cda2/components/legacy-project-wrapper/implementation/Makefile)
- **A3** [components/flight-analyzer/component.md](https://github.com/jordanhubbard/Aviation/blob/7b41ed2c6de8d348b13a9dfcb3d30a4ee7a9cda2/components/flight-analyzer/component.md)
- **A4** [verification/current.json](https://github.com/jordanhubbard/Aviation/blob/7b41ed2c6de8d348b13a9dfcb3d30a4ee7a9cda2/verification/current.json)

## ElixirSSI

Inspected commit: `49bf27f4d25fdaf5c148e79451c5ed0c6bafdb53`.

An Elixir/BEAM system with Linux as hardware abstraction and Pi 5/CM5 image targets.
The project definition's receipt policy requires four stages. The actual verifier
runs `build`, `test`, `test-emulator` and `test-cm5`; the last target invokes the
cluster acceptance tool on CM5 emulation. It invalidates the previous receipt
before execution, checks source/authority and artifact identities, and refuses a
new receipt on failed stages or input drift. The committed image report records
all four stage exits as zero and scopes acceptance to this host and **three emulated
CM5 boards; physical hardware is unqualified**. Four is a stage count, not the
number of individual assertions. Do not call emulator results physical-board proof.

- **E1** [literate.project.json](https://github.com/jordanhubbard/ElixirSSI/blob/49bf27f4d25fdaf5c148e79451c5ed0c6bafdb53/literate.project.json)
- **E1** [PROJECT.md](https://github.com/jordanhubbard/ElixirSSI/blob/49bf27f4d25fdaf5c148e79451c5ed0c6bafdb53/PROJECT.md)
- **E2** [Makefile](https://github.com/jordanhubbard/ElixirSSI/blob/49bf27f4d25fdaf5c148e79451c5ed0c6bafdb53/Makefile)
- **E2** [os/Makefile](https://github.com/jordanhubbard/ElixirSSI/blob/49bf27f4d25fdaf5c148e79451c5ed0c6bafdb53/os/Makefile)
- **E3** [scripts/verify-project.py](https://github.com/jordanhubbard/ElixirSSI/blob/49bf27f4d25fdaf5c148e79451c5ed0c6bafdb53/scripts/verify-project.py)
- **E3** [scripts/test-verification.py](https://github.com/jordanhubbard/ElixirSSI/blob/49bf27f4d25fdaf5c148e79451c5ed0c6bafdb53/scripts/test-verification.py)
- **E4** [verification/system-image.json](https://github.com/jordanhubbard/ElixirSSI/blob/49bf27f4d25fdaf5c148e79451c5ed0c6bafdb53/verification/system-image.json)
- **E4** [verification/current.json](https://github.com/jordanhubbard/ElixirSSI/blob/49bf27f4d25fdaf5c148e79451c5ed0c6bafdb53/verification/current.json)

## LeRTX

Inspected commit: `0ac4c58dfa07f2fdbe310e88c5fb2930706f97da`.

A hybrid specification/retained native digital-twin workspace with a real project
definition, desktop Component, language/OS slots and pinned native SDK inputs.
The desktop contract separates native NVIDIA OVRTX/OVStage work on Linux/Windows
from a Darwin-only Metal implementation and requires real rendering rather than
a mock success. Its prototype admission record reports **160** tests against the
admitted ARM64 artifact and sealed SDK closure, while preserving module/source
comparison and limitations. This is a recorded prototype milestone, not current
HEAD or every-platform release qualification. A separate 157-test regression record
retains three initial failures and a narrower passing rerun; that rerun does not
prove cold-start reliability or formal admission. The top-level standard receipt
records 30 tests and must not be conflated with the 160-test native milestone.
PROJECT calls for fresh qualification of the newly combined source/SDK release.
Do not claim present physical-arm qualification, generic GPU independence or an
eight-hour-to-108-second speedup from the scoped native duration.

- **L1** [literate.project.json](https://github.com/jordanhubbard/LeRTX/blob/0ac4c58dfa07f2fdbe310e88c5fb2930706f97da/literate.project.json)
- **L1** [PROJECT.md](https://github.com/jordanhubbard/LeRTX/blob/0ac4c58dfa07f2fdbe310e88c5fb2930706f97da/PROJECT.md)
- **L2** [components/desktop/component.md](https://github.com/jordanhubbard/LeRTX/blob/0ac4c58dfa07f2fdbe310e88c5fb2930706f97da/components/desktop/component.md)
- **L3** [verification/prototype-admission-review.json](https://github.com/jordanhubbard/LeRTX/blob/0ac4c58dfa07f2fdbe310e88c5fb2930706f97da/verification/prototype-admission-review.json)
- **L4** [verification/native/linux/retained-admission-regression.json](https://github.com/jordanhubbard/LeRTX/blob/0ac4c58dfa07f2fdbe310e88c5fb2930706f97da/verification/native/linux/retained-admission-regression.json)
- **L4** [verification/current.json](https://github.com/jordanhubbard/LeRTX/blob/0ac4c58dfa07f2fdbe310e88c5fb2930706f97da/verification/current.json)

## Daemonarchy

Inspected commit: `cebd8e7f5779513b64ee273f4fdfd02d28e7cc87`.

FreeBSD desktop/installer project with an in-place original-source authority layer
and eight retained product contracts: installation, packages, updates, desktop,
diagnostics, compatibility, release and verified boot. Installer requirements cover
explicit storage intent, verified completion and boot-environment isolation. This
is genuine Literate AI use, but a **partial adoption**, not a completed native
lifecycle or a regenerated FreeBSD tree. The adoption report says controller
contract locks pass while native execution and release enforcement are not yet
qualified; `verification/current.json` is absent from the inspected tree. Its
published 1.1.1 product record retains physical graphics/firmware and other limits.
Later framework development records are scoped framework work, not a product
receipt or a new released ISO. This case demonstrates honest boundaries; do not
present it as a fully qualified successful native adoption or a measured build speedup.

- **D1** [literate.project.json](https://github.com/jordanhubbard/daemonarchy/blob/cebd8e7f5779513b64ee273f4fdfd02d28e7cc87/literate.project.json)
- **D1** [PROJECT.md](https://github.com/jordanhubbard/daemonarchy/blob/cebd8e7f5779513b64ee273f4fdfd02d28e7cc87/PROJECT.md)
- **D2** [components/installer/component.md](https://github.com/jordanhubbard/daemonarchy/blob/cebd8e7f5779513b64ee273f4fdfd02d28e7cc87/components/installer/component.md)
- **D2** [components/installer/acceptance.md](https://github.com/jordanhubbard/daemonarchy/blob/cebd8e7f5779513b64ee273f4fdfd02d28e7cc87/components/installer/acceptance.md)
- **D2** [components/updates/component.md](https://github.com/jordanhubbard/daemonarchy/blob/cebd8e7f5779513b64ee273f4fdfd02d28e7cc87/components/updates/component.md)
- **D2** [components/verified-boot/component.md](https://github.com/jordanhubbard/daemonarchy/blob/cebd8e7f5779513b64ee273f4fdfd02d28e7cc87/components/verified-boot/component.md)
- **D3** [verification/adoption-checks.json](https://github.com/jordanhubbard/daemonarchy/blob/cebd8e7f5779513b64ee273f4fdfd02d28e7cc87/verification/adoption-checks.json)
- **D4** [docs/testing-1.1.1.md](https://github.com/jordanhubbard/daemonarchy/blob/cebd8e7f5779513b64ee273f4fdfd02d28e7cc87/docs/testing-1.1.1.md)
- **D4** [verification/composed-framework-qualification-development.json](https://github.com/jordanhubbard/daemonarchy/blob/cebd8e7f5779513b64ee273f4fdfd02d28e7cc87/verification/composed-framework-qualification-development.json)

## Screening and exclusions

Sixteen non-fork repository snapshots were inspected: the five above and WebMux,
NanoLang, CudaLab, shemacs, toolscheme, agentos, pythonos, Theseus, artlab, RubyOS and
RemoteOS-SDL. The latter eleven did not establish canonical Literate AI project
metadata/component integration in the inspected trees. They are **not used as
verified Literate AI success stories**. This is an evidence exclusion, not a claim
that they have never used a related workflow or have no unpushed adoption work.

## Claims the film must not make

- No measured universal “eight hours becomes minutes,” no side-by-side stopwatch,
  and no made-up current execution transcript. Sam's eight-hour wait is the story's
  anecdote. The defensible mechanism is earlier failure discovery and eligible
  parallel/worker execution while retaining required checks.
- No claim that everything can run on the author's host, every matrix cell passes,
  all skipped suites are covered, or emulator/mock evidence proves native hardware.
- No claim that metadata adoption regenerates legacy source, project users require
  an agent at runtime, or any of these projects is a safety certification.
- No growth-rate or “five completely finished projects” claim. Show concrete
  adoption paths, achieved scoped milestones and still-open work together.
