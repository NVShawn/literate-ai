#!/usr/bin/env python3
"""Build the Literate-AI manager/engineering overview narrative with python-docx.

Codex-independent realization of the narrative member. The Codex documents plugin
is not a requirement of this package. Claims trace to source-notes.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from docx import Document
from docx.enum.text import WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

HERE = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("LITERATE_AI_DECK_SOURCE", str(HERE)))
REPO = Path(os.environ.get("LITERATE_AI_REPO", str(SOURCE.parents[2])))


def _obj_dir() -> Path:
    return Path(os.environ.get("OBJ_DIR") or (REPO / "_build"))


OUT = Path(
    os.environ.get("LITERATE_AI_NARRATIVE_OUTPUT")
    or (SOURCE / "literate-ai-manager-and-engineering-overview.docx")
)
PPTX = Path(
    os.environ.get("LITERATE_AI_DECK_OUTPUT")
    or (SOURCE / "literate-ai-manager-and-engineering-overview.pptx")
)
INK = RGBColor(0x10, 0x13, 0x17)
STEEL = RGBColor(0x65, 0x70, 0x7C)


def _set_run_font(run, *, name: str, size: Pt, color=INK, bold=False, italic=False):
    run.font.name = name
    run.font.size = size
    run.font.color.rgb = color
    run.bold = bold
    run.italic = italic
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), name)
    rfonts.set(qn("w:hAnsi"), name)
    rfonts.set(qn("w:cs"), name)


def heading(doc: Document, level: int, text: str) -> None:
    paragraph = doc.add_heading(text, level=level)
    for run in paragraph.runs:
        _set_run_font(run, name="Calibri", size=Pt(22 - (level * 2)), color=INK, bold=True)


def body(doc: Document, text: str, *, italic: bool = False) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(10)
    paragraph.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
    run = paragraph.add_run(text)
    _set_run_font(run, name="Calibri", size=Pt(12), color=INK, italic=italic)


def code(doc: Document, text: str) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(6)
    paragraph.paragraph_format.space_after = Pt(10)
    paragraph.paragraph_format.left_indent = Inches(0.25)
    run = paragraph.add_run(text)
    _set_run_font(run, name="Courier New", size=Pt(9), color=STEEL)


def _access() -> dict[str, object]:
    return {
        "audience": "organization",
        "principals": [],
        "permission": "view",
        "link_sharing": "organization-restricted",
    }


def write_manifest(pptx_path: Path, docx_path: Path) -> Path:
    manifest = {
        "schema": "literate-ai/document-pair-manifest@1",
        "ecosystem": "google-workspace",
        "members": {
            "presentation": {
                "local_artifact": str(pptx_path),
                "published_location": None,
                "publication_authorized": False,
                "access": _access(),
            },
            "narrative": {
                "local_artifact": str(docx_path),
                "published_location": None,
                "publication_authorized": False,
                "access": _access(),
            },
        },
        "authoring_package": {
            "root": str(SOURCE),
            "elements": {
                "narrative_specification": "narrative-specification.md",
                "factual_ledger": "source-notes.md",
                "generation_prompts": "prompts",
                "build_source": "build_deck.py",
                "assets": "assets",
                "regeneration_entry_point": "regenerate.sh",
                "deliverable_links": "current-deliverables.md",
                "qa_record": "qa-ledger.md",
            },
        },
    }
    path = _obj_dir() / "literate-ai-manager-overview" / "capability-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def build() -> Path:
    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(1)
    core = doc.core_properties
    core.title = "Literate-AI: the application foundry"
    core.author = "Literate AI maintainers"
    core.subject = "1.2.0 edition narrative member of component://literate-ai/literate-ai-overview"

    heading(doc, 1, "Literate-AI: the application foundry")
    body(
        doc,
        "This is the 1.2.0 edition of the narrative member of "
        "component://literate-ai/literate-ai-overview. It is the comprehensive technical "
        "account that accompanies the 26-slide presentation built from deck-specification.md. "
        "Both members share the factual ledger in source-notes.md and must not diverge on a "
        "shared claim. This edition is the major/minor regeneration required before the "
        "1.2.0 cut. Tag v1.1.0 is already published; this document describes implemented "
        "candidate authority, does not claim that v1.2.0 is already published, and does not "
        "grant release authority.",
    )

    heading(doc, 2, "What problem this solves")
    body(
        doc,
        "Software still scales by copying implementation. Each product, platform, and "
        "repository inherits another version of the same knowledge, and another place for it "
        "to drift. Literate-AI inverts that: a Component specification is the durable, "
        "human-readable authority; generated source is an untrusted candidate until build, "
        "test, execution, independent acceptance, and receipt requirements pass. The goal is "
        "not better code generation. It is an application foundry — governed creation of "
        "complete applications, then a living portfolio of shared capabilities.",
    )
    heading(doc, 2, "Who this document is for")
    body(
        doc,
        "This narrative is for an engineer evaluating adoption, an architect reviewing the "
        "control model, or a manager who wants the deck's claims traced to their source. It "
        "is one cumulative account for product, program, engineering, and executive readers. "
        "It does not invent productivity or return-on-investment percentages. Forward-looking "
        "sections are labeled as a target horizon, not a measured delivery guarantee.",
    )

    heading(doc, 1, "The control model")
    heading(doc, 2, "Readable intent as the durable product")
    body(
        doc,
        "A Component specification is a readable file. Requirements, scenarios, and the "
        "public capability contract are the product. Language, build system, operating "
        "system, and the entire source tree sit below that line: they are Flavor choices and "
        "regeneration targets. Authority: docs/architecture/component-execution-plans.md and "
        "the sample at components/money-calculation/.",
    )
    heading(doc, 2, "Implementation as a renewable candidate")
    body(
        doc,
        "Generated source holds no build, acceptance, cache-membership, or publication "
        "authority until the current gates pass. An accepted-source cache hit still runs the "
        "complete current index, authorization, build, test, execution, and acceptance "
        "sequence, and is not republished. Cache identity is the narrow boundary the model "
        "rests on.",
    )
    heading(doc, 2, "The Component specification as durable authority")
    heading(doc, 3, "A real specification excerpt")
    body(
        doc,
        "The presentation member quotes components/money-calculation/component.md. The "
        "requirement and scenario below are taken from that file, not invented for this "
        "document:",
    )
    code(
        doc,
        "### Requirement: Exact integer discount calculation\n"
        "The Component's portable `run` entrypoint SHALL accept one UTF-8 JSON argument "
        "array whose two ordered values are a non-negative integer subtotal in cents and a "
        "discount in basis points from 0 through 10000, compute the discount as "
        "floor((subtotal_cents * discount_basis_points + 5000) / 10000), and return integer "
        "subtotal_cents, discount_cents, and total_cents fields.\n\n"
        "#### Scenario: Half-up discount rounding\n"
        "- WHEN a 999-cent subtotal receives a 1250-basis-point discount\n"
        "- THEN the discount is 125 cents and the total is 874 cents",
    )
    heading(doc, 3, "Its public capability contract")
    body(
        doc,
        "interfaces/money-calculation.md is the reuse boundary. Consumers bind to a public "
        "process contract, never to provider source layout:",
    )
    code(
        doc,
        "This contract is language-neutral. The realized provider artifact SHALL expose its\n"
        "portable `run` entrypoint to consumers through the\n"
        "LITAI_CAPABILITY_MONEY_CALCULATION environment variable.\n\n"
        "Consumers SHALL use this public process contract and SHALL NOT import, copy, or "
        "depend on private provider source layout.",
    )

    heading(doc, 1, "How generation works")
    heading(doc, 2, "Generation keys: what they bind, what they refuse to bind")
    body(
        doc,
        "A ComponentGenerationKey binds the ordered specification set and document "
        "identities, selected target and Flavors, specification-to-source skills, workflow, "
        "routing policy, model, authored assets, its own exported public-interface "
        "identities, and the public interfaces of its direct generation dependencies. It "
        "deliberately does not bind aggregate Component or authoring revisions, nor a "
        "dependency's revision, private specifications, source tree, build output, tests, "
        "routing policy, or skills. Authority: docs/architecture/component-execution-plans.md, "
        "Generation-key boundary.",
    )
    heading(doc, 3, "Invalidation and its blast radius")
    body(
        doc,
        "A private leaf change invalidates the leaf only. An exported interface change "
        "invalidates the leaf and its direct consumers. A diamond dependency appears once per "
        "provider-first layer. A deeper private graph leaves the consumer key reusable. "
        "These are stated rules, not inferences. Binding aggregate revisions would be the "
        "easy implementation and would destroy both reuse and the acceptance-oracle "
        "separation.",
    )
    heading(doc, 2, "Layered execution, bounded concurrency, exact budgets")
    body(
        doc,
        "Action plans schedule every locked revision in explicit zero-based layers with "
        "provider-before-consumer ordering. Independent nodes in a layer run concurrently up "
        "to an explicit bound. Results emit in canonical Component-revision order. Absent "
        "runtime measurements remain null rather than an invented zero, and a supplied "
        "measurement exceeding the exact budget fails the node while retaining observed "
        "values. A plan that cannot be scheduled is a stable planning error, not a failed "
        "generation.",
    )
    heading(doc, 2, "The failure path")
    heading(doc, 3, "What happens when a gate fails")
    body(
        doc,
        "A failed provider cancels dependent generation nodes before their adapters are "
        "called, while independent branches continue. A different key fails closed before "
        "reuse. Failure custody retains sample scratch, installed workspaces, transcripts, "
        "and diagnostics on failed steps and reports their absolute paths; successful steps "
        "still clean up. Sample scratch remains outside the checkout; evidence custody "
        "points to that external directory rather than relocating a host build under "
        "_build/evidence/. This specifically closes the v0.5.2 host-E2E failure mode in "
        "which the failing sample's scratch disappeared with the run that produced it. "
        "Authority: src/literate_ai/evidence_ledger.py and src/literate_ai/step_harness.py.",
    )
    heading(doc, 2, "The model-egress trust boundary")
    heading(doc, 3, "What never crosses it")
    body(
        doc,
        "A BuildRequestDeclaration names builder, toolchain, sandbox, privileges, and "
        "outputs before generated bytes exist, and deliberately cannot name a source bundle. "
        "Component hierarchy never authorizes context flattening: a consumer receives direct "
        "public contracts, not private transitive specifications, source, or tests.",
    )
    heading(doc, 3, "Current containment state")
    body(
        doc,
        "The ordered isolation levels are host-yolo, process-limited, and os-sandboxed. A "
        "local sufficiency decision is explicitly unauthenticated-local and is neither "
        "execution authorization nor release evidence. Production containment is an "
        "architecture contract in this repository, not a shipped backend. The live sample "
        "path runs with explicit YOLO authorization as the host user, and only the "
        "source-to-specification observation path has narrower macOS sandbox-exec and Linux "
        "Bubblewrap adapters. Authority: docs/architecture/production-containment-threat-model.md.",
    )

    heading(doc, 1, "Evidence: what exists today")
    heading(doc, 2, "The language, build-system, and operating-system Flavor matrix")
    body(
        doc,
        "These figures measure catalog surface area, not maturity or Cartesian "
        "qualification. The live language Flavor matrix is Python 3.11+, C++17, Rust 2021, "
        "JavaScript with Node.js 20+, Swift with explicit Apple/Linux/Windows toolchain "
        "realizations, and Go. The live build-system Flavor matrix is Bazel, GNU Make, "
        "CMake, and Cargo. Bazel is a removable preference, never hard-coded authority. The "
        "live operating-system Flavor matrix is macOS, Linux, and Windows. The packaging "
        "Flavor catalog contains pip wheels, Conan, apt, Homebrew, WinGet, and Chocolatey. "
        "Compatible providers compose as independent selections. The current CLI provides "
        "exact, read-only multi-provider package planning and Standard-lifecycle-backed "
        "native construction and independent verification for deterministic pip wheels and "
        "portable Conan cache archives. Apt, Homebrew, WinGet, and Chocolatey construction "
        "remains open.",
    )
    body(
        doc,
        "The current private user-owned inventory configures six workers spanning macOS "
        "ARM64, Windows 11 CPU/GPU, Ubuntu 24.04 CPU/GPU, and Ubuntu 26.04 CPU. That is "
        "configuration evidence, not a claim that the full Cartesian product has passed. "
        "Private destinations are not copied into this package.",
    )
    heading(doc, 2, "Sample applications and what they verify")
    body(
        doc,
        "The sample harness catalogs 26 behavior applications. Three are OS-pinned: Linux "
        "Cgroup Budget Interpreter (C++/Conan), Windows Path Auditor (C++/Conan), and macOS "
        "LaunchAgent Catalog (Swift/Apple toolchain/Homebrew). Host discovery excludes the "
        "other two before generation while catalog discovery retains all three for readers. "
        "The 0.9 portfolio also includes a first-class multi-entrypoint fixture and the "
        "durable split service: separately generated frontend, read-only API, single-writer "
        "collector, and SQLite snapshot cache Components connected only by public contracts. "
        "Their specifications and verifier oracles define deterministic acceptance behavior. "
        "Catalog authority: samples/README.md and "
        "samples/_harness/durable-split-service/portfolio.json.",
    )
    heading(doc, 3, "Verified wheel/Conan output")
    body(
        doc,
        "Greeting Card Starter and Exact Statistics Library select pip; Dependency Planner "
        "and the OS-specific native utilities select Conan. make samples-packages is the "
        "opt-in live proof that selected Standard samples can construct and independently "
        "verify native wheel/Conan bytes from their accepted artifacts without publication. "
        "Ordinary sample execution does not pay this package-construction cost.",
    )
    heading(doc, 3, "OS-pinned applications and cross-platform fan-out")
    body(
        doc,
        "scripts/fanout_samples.py runs user-selected samples across user-owned SSH targets "
        "in parallel, selects platform Flavors, binds an exact Git revision or working tree, "
        "fails fast, and checkpoints successful target IDs under OBJ_DIR for repair-cycle "
        "resume. A completed resumed repair cycle clears its checkpoint and requires a clean "
        "rerun before the result can serve as release evidence. Configured fan-out is real "
        "mechanism. Configured inventory remains distinct from exact passing receipts; a "
        "host that rejects unattended SSH has not begun command serialization or execution.",
    )
    heading(doc, 2, "Independent acceptance and provenance")
    heading(doc, 3, "Correctness ships with the artifact")
    body(
        doc,
        "The Standard core implements exact per-Component planning, bounded parallel "
        "generation, invalidation, accepted-source caching, restart custody, build "
        "authorization, artifact assembly, generated tests, independent acceptance, and "
        "project-admission seams. A multi-entrypoint Component now retains every deployment "
        "unit through one multi-output build, attributed TEST and EXECUTE evidence, package "
        "dispatch, independent acceptance, receipts, and CycloneDX evidence. Some reference "
        "samples still retain a content-pinned external driver; do not claim every sample "
        "already runs solely through the ordinary CLI.",
    )
    heading(doc, 3, "Pre-build and post-build CycloneDX evidence")
    body(
        doc,
        "Pre-build and post-build CycloneDX evidence are supported. Authority: "
        "docs/architecture/sbom-and-dependency-graph.md. A polished overview cannot grant "
        "build, execution, publication, or release authority.",
    )
    heading(doc, 2, "Process-tree ownership and release-line decisions")
    heading(doc, 3, "Win32 Job Objects at timeout spawn sites")
    body(
        doc,
        "Remaining in-package timeout spawn sites bind create_process_tree_ownership so "
        "late grandchildren die with the parent. Standalone worker scripts stay "
        "stdlib-only and keep validated taskkill. Authority: "
        "src/literate_ai/adapters/_processes.py callers; source-notes.md 0.9.0 edition.",
    )
    heading(doc, 3, "Who decides patch content")
    body(
        doc,
        "Humans own when the next minor or major is cut. Agents own patch content on the "
        "current release/<major>.<minor>.x line. Each published changelog section links "
        "to that version's README.md at the matching git tag. Authority: "
        "skills/agent/release-project/SKILL.md and docs/architecture/project-releases.md.",
    )

    heading(doc, 1, "From Components to applications")
    heading(doc, 2, "Applications as arbitrary Component DAGs")
    body(
        doc,
        "Component plans support arbitrary acyclic dependency graphs, exact public-interface "
        "bindings, provider-before-consumer layers, deterministic result ordering, and "
        "bounded concurrency among independent nodes. The durable split portfolio proves the "
        "model with two Standard roots: collector and cache own collection; cache, read-only "
        "API, and browser frontend own serving. The cache provider owns schema and migrations, "
        "the collector alone invokes upstream, and no private source or credentials flatten "
        "across an edge.",
    )
    heading(doc, 2, "One application intent, many products and runtimes")
    body(
        doc,
        "Specification, Flavors, Skills, Workflow, and Routing resolve into an exact, "
        "inspectable plan before model execution. One application intent can serve many "
        "products and runtimes, including packaging and documentation ecosystem axes, "
        "because those axes are Flavor selections rather than forks of the specification.",
    )
    heading(doc, 2, "Packaging and documentation ecosystem axes")
    body(
        doc,
        "The documentation.ecosystem Flavor axis has google-workspace and microsoft-365 "
        "choices. The first targets Google Slides and Google Docs; the second targets "
        "PowerPoint and Word with SharePoint or OneDrive collaboration when authorized. "
        "These choices govern artifact format and collaboration surface, not product claims, "
        "approval state, application generation, build execution, or release authority. "
        "This overview Component realizes literate-ai.document-pair for google-workspace.",
    )

    heading(doc, 1, "Operating the system")
    heading(doc, 2, "`litai`'s separation of concerns")
    body(
        doc,
        "Current litai help exposes help, version, design, release, package, flavor, perf, "
        "matrix, config, work, document, worker, init, cache, skills, prompt, catalog, graph, "
        "build, test, run, verify, update, reparent, learn, lock, component, clean, "
        "really-clean, project, plan, generate, rebuild, profile, and spec. Diagnostic and "
        "deterministic connector verbs do not authorize a release. Native build and package "
        "managers remain delegated execution engines, never specification authority. Parser "
        "authority: src/literate_ai/cli/source_to_specification.py.",
    )
    heading(doc, 3, "Package planning")
    body(
        doc,
        "litai package plan is read-only. It reports compatible native formats from one "
        "accepted closure. It does not construct bytes and does not publish.",
    )
    heading(doc, 3, "Native construction")
    body(
        doc,
        "litai package build requires explicit host-execution acknowledgement, writes "
        "content-addressed package batches only beneath OBJ_DIR, embeds component.md plus "
        "the resolved CycloneDX SBOM, and grants no external publication authority.",
    )
    heading(doc, 3, "Independent verification")
    body(
        doc,
        "litai package verify reopens retained inputs and native bytes. Conan is restored "
        "into a fresh isolated cache. None of plan, build, or verify publishes.",
    )
    body(
        doc,
        "Host installation is a separate authority. make install selects one strict "
        "CycloneDX 1.7 prerequisite SBOM by OS, CPU, and accelerator coordinate, verifies "
        "APT, Homebrew, or WinGet records, and asks once before installing missing native "
        "packages. make uninstall removes only manifest-owned Literate AI runtime paths and "
        "leaves native dependencies plus durable user configuration and state intact. "
        "Authority: docs/decisions/0032-tuple-specific-native-install-sboms.md and "
        "docs/user/installation.md.",
    )
    heading(doc, 3, "Release control")
    body(
        doc,
        "litai release plan, prepare, check, and publish are four deliberately separate "
        "commands. Plan is read-only. Prepare writes declared version fields and may create "
        "the release line; it does not commit, tag, or push. Check runs the exact policy "
        "gate on that line. Publish requires --authorize-external-write. Never retag a "
        "published distribution tag. Authority: docs/architecture/project-releases.md.",
    )
    heading(doc, 2, "One authority connecting roadmap to operations")
    body(
        doc,
        "Work is recorded in docs/roadmap/active-work.md before implementation. Conversation "
        "history is not project authority. Content-identified objects let roadmap, "
        "operations, and evidence views reference the same identities without a "
        "synchronization process.",
    )
    body(
        doc,
        "Deterministic operations belong in Python services rather than repeated model work. "
        "One executable skill catalog owns taxonomy and inheritance, while one injectable "
        "platform policy resolves durable private configuration to XDG-style literate-ai "
        "paths on Linux/macOS or Roaming AppData on Windows. litai config migrate performs "
        "the explicit dry-run-first 0.8.x transition. Authority: ADRs 0030 and 0031.",
    )
    heading(doc, 2, "Existing systems as starting knowledge")
    body(
        doc,
        "Source-to-specification can index an inert mirror, draft reviewable Component and "
        "Flavor material, and journal model calls without executing the mirrored source. "
        "Draft extraction is available. Effective authority remains source-baseline until "
        "trusted, current qualification evidence exists. Do not claim live repository "
        "acquisition as an ordinary generation behavior. Authority: "
        "docs/architecture/source-promotion.md.",
    )

    heading(doc, 1, "Release engineering: proving the system knows when it's valid")
    heading(doc, 2, "Release Policy")
    heading(doc, 3, "Writable main and exact-main release candidates")
    body(
        doc,
        "The default branch remains writable for ordinary new work in both Free and "
        "Pre-release states. Pre-release names one canonical major.minor target. A release "
        "candidate tag is permitted only from the exact main revision that passed the "
        "declared gate; it is not a tag placed on an approximate or release-line commit.",
    )
    heading(doc, 3, "Release-line lockdown and authority")
    body(
        doc,
        "Cutting release/<major>.<minor>.x locks that release line while main continues to "
        "accept ordinary work. Only exact login bullets under README.md's Release Engineers "
        "section may merge release-line pull requests or create and publish a major or minor "
        "release. Patch authority is project configuration: strict reserves patch operations "
        "to Release Engineers; loose permits a configured writer to use reason-bearing "
        "break-glass only for a commit already landed on main. Break-glass grants no RC or "
        "major/minor authority.",
    )
    body(
        doc,
        "LitAI release commands enforce state, actor, branch, and revision constraints. "
        "Forge branch protection and required-review settings are distinct controls that "
        "should mirror this policy. This overview does not claim those settings are live.",
    )
    heading(doc, 3, "Pull-request classification and safe branch collection")
    body(
        doc,
        "Every pull request carries exactly one Literate-AI-Release: major.minor line for "
        "the active target, or Literate-AI-Release: none. Missing, duplicate, malformed, "
        "stale, and other-target declarations remain unknown rather than inferred. Terminal "
        "topic branches receive exact-head merged or reason-bearing dead markers. Peer-work "
        "garbage collection inspects open pull requests and all worktrees, rejects stale "
        "markers, protected or unmerged branches, and dirty state, plans by default, and "
        "requires explicit apply and delete authorization.",
    )
    heading(doc, 3, "Continuous contribution disposition and document-pair evidence")
    body(
        doc,
        "Every major or minor cut runs a Python-owned contribution sweep over open issues, "
        "reviews, unmerged branches, and attached worktrees. The operator decides relevance; "
        "the sweep fails closed on unclassified work. Tracker dispositions and exact-head "
        "branch-lifecycle markers survive into the next cycle. Major and minor cuts also "
        "regenerate, independently verify, preflight, publish, and export back the terminal "
        "document pair using one explicit active gcloud account. Authority: "
        "docs/decisions/0034-continuous-evidence-bound-release-closure.md. This edition does "
        "not claim that tag v1.2.0 is already published.",
    )
    heading(doc, 2, "Gates that remember where they stopped")
    heading(doc, 3, "Fail-fast, checkpointed, content-fingerprinted resume")
    body(
        doc,
        "A checkpointed test or gate run records only tests or gates that actually executed "
        "and passed. A run interrupted partway resumes from that exact point on the next "
        "invocation rather than restarting. python-check's checkpoint is keyed by a content "
        "hash of each test module's own bytes, so it is always safe to trust a resume. The "
        "coarser release-check gate-level checkpoint has no such content fingerprint — it "
        "tracks only the ordered gate-name list — so it is reset explicitly before "
        "re-validating a checkout against a different commit than it last ran against. "
        "Authority: scripts/run_checkpointed_unittests.py, src/literate_ai/test_checkpointing.py, "
        "and docs/user/troubleshooting.md.",
    )
    body(
        doc,
        "The Makefile RELEASE_GATES list is fourteen names: repository-layout-check, "
        "python-check, lint, format-check, openspec-check, documentation-check, "
        "driver-review, skills-check, installed-e2e, wheel-check, install-check, "
        "installed-project-e2e, samples, test-receipt-current. There is no codegraph-ready "
        "gate.",
    )
    heading(doc, 3, "Why a full clean pass is still required before evidence counts")
    body(
        doc,
        "A checkpointed run that resumed any prior state deliberately reports a distinct "
        "exit status from a genuine, from-scratch clean pass, and asks for one more full run "
        "before its result counts as release evidence. A resumed pass is never silently "
        "promoted to the same evidentiary weight as a full one.",
    )
    heading(doc, 2, "One gate, your choice of target")
    body(
        doc,
        "litai release check --target local|github|gitlab accepts all three target names, "
        "but GitLab is recognized and fail-closed as unsupported rather than dispatched. "
        "Local and GitHub execution use the same declared gate command (make release-check). "
        "A GitHub Actions --target github pass is not a substitute claim that local fan-out "
        "ran. v0.7.1 was published from release/0.7.x via --target github.",
    )
    heading(doc, 3, "A private worker fleet")
    body(
        doc,
        "When a fleet is configured, --target local fans out in parallel across eligible "
        "workers, requires every dispatched worker to pass, and names the first failing "
        "worker deterministically. SSH-dispatched remote commands run under a forced "
        "login-and-interactive shell (bash -lic), so a worker's real toolchain — reached "
        "only through its own shell startup files — is reliably visible without any "
        "worker-side configuration change. Authority: src/literate_ai/adapters/ssh_transport.py. "
        "This overview does not claim that 0.7.0 or 0.7.1 ran that fleet.",
    )
    heading(doc, 3, "GitHub Actions")
    body(
        doc,
        "--target github polls GitHub Actions for the checked revision's run. The gate "
        "definition itself never changes for supported targets; only where it runs changes.",
    )
    heading(doc, 3, "Why the same gate runs unmodified either way")
    body(
        doc,
        "When no worker fleet is configured, legacy-local executes the gate on the invoking "
        "VM and records absent fan-out and provider paths as explicit skipped or unavailable "
        "nodes, with authoritative: false, rather than failing the release. When the policy "
        "names default_branch, prepare creates release/<major>.<minor>.x if the plan records "
        "create: true. check and publish refuse that default branch and any per-patch name "
        "such as release/0.7.0. Do not wrap a raw git branch or git checkout in place of "
        "prepare. Authority: src/literate_ai/project_releases.py.",
    )
    heading(doc, 2, "Timed evidence: `litai perf`")
    heading(doc, 3, "What gets recorded, and where")
    body(
        doc,
        "Every top-level litai command, and every worker dispatched during a --target local "
        "fan-out, records one structured JSON-Lines performance span: stage, target kind and "
        "id, coding CLI, model, start/end timestamps, duration, and pass/fail outcome. This "
        "telemetry is diagnostic, never authority — a read-only or missing build root never "
        "fails the command it observes. Authority: src/literate_ai/perf.py and "
        "src/literate_ai/cli/perf.py.",
    )
    heading(doc, 3, "Reading fleet-dispatch cost from real recorded spans")
    body(
        doc,
        "litai perf show and litai perf chart --dir PATH can report on an archived copy of "
        "that telemetry directly, so it survives past the disposable build directory being "
        "cleaned. Field values shown in the presentation are a representative shape, not a "
        "claim about any specific historical run's exact numbers.",
    )
    heading(doc, 2, "Provenance: what a release pin protects")
    heading(doc, 3, "The lifecycle-driver trust boundary and its exact digest")
    body(
        doc,
        "The lifecycle driver's implementation identity is a single content-addressed digest "
        "over the exact declared source-file closure that is trusted to execute the "
        "project's build, test, and acceptance lifecycle. Any change under that closure "
        "invalidates the pin; the project fails closed until the pin is explicitly "
        "re-recorded, which is itself a reviewable, single-line diff. Re-pin with make "
        "driver-review-record. Authority: src/literate_ai/adapters/project_lifecycle_driver.py "
        "and scripts/review_lifecycle_driver.py.",
    )
    heading(doc, 3, "The documentation-authority marker")
    body(
        doc,
        "docs/architecture/design-traceability.md carries a literate-ai:authority-reviewed "
        "sha256 marker. The same fail-closed pattern applies to the declared documentation "
        "surface: a stale marker blocks litai project validate until a human re-reviews and "
        "re-records it with make documentation-review-record.",
    )
    heading(doc, 3, "What happens when either goes stale")
    body(
        doc,
        "The project fails closed. Re-recording is never an implicit side effect of a normal "
        "run. The release evidence ledger indexes and points to evidence; it does not "
        "authorize a build or accept a release. Inspect it with litai release evidence "
        "explain, index, show, and prune.",
    )
    body(
        doc,
        "0.7.1 also ships two related mechanics that are not extra release gates: tests "
        "prefer supported public CLI and Python APIs (CONTRIBUTING.md, TEST-PUBLIC-001), and "
        "a genuine large Component-lock semantic diff is reviewed in bounded pages then "
        "replaced atomically (docs/decisions/0020-paginated-component-lock-review.md).",
    )

    heading(doc, 1, "Where this is going")
    heading(doc, 2, "The next hardening horizon")
    body(
        doc,
        "The 1.1 native-custody and 1.0 operator and release boundaries remain implemented: contribution sweeps, "
        "document-pair publication, Standard lifecycle rebind, doctor, status, acknowledged "
        "create/adopt plans, explicit conversion stages, and an installed-wheel golden path. "
        "The 1.2 candidate adds dependency-aware bounded action scheduling, provider-neutral "
        "cache authority, real local Debian packages, native CLI and gRPC acceptance, and "
        "repository-owned worktree placement. The next horizon remains investment framing rather than a "
        "measured delivery guarantee.",
    )
    heading(doc, 3, "Operator adoption is now an explicit front door")
    body(
        doc,
        "Installed 1.2 projects retain the project-independent "
        "doctor, status over existing authority, lock, receipt, tracker, and host services, "
        "acknowledged create/adopt plans, and explicit wrapped, retained, drafted, and qualified "
        "stages. Original source remains release authority until current regenerative "
        "qualification. A release-candidate-wheel gate proves create through a validated hello "
        "project and adopt through a current retained receipt. These are implemented candidate "
        "claims inherited from 1.0, not a claim that v1.2.0 is already published.",
    )
    heading(doc, 3, "Exact scheduling, cache, package, and native acceptance slices")
    body(
        doc,
        "The 1.2 candidate projects exact lifecycle actions into a deterministic dependency "
        "DAG and dispatches newly ready work through bounded capability-matched slots while "
        "isolating descendant cancellation and rejecting changed recovery routes. Local and "
        "HTTPS/WebDAV cache authority projects to Bazel and sccache, and immutable test, "
        "package, and container objects are rehashed before reuse. The package-apt path "
        "constructs and independently inspects real deterministic Debian archives on Linux. "
        "Native CLI and native gRPC acceptance retain exact executable, stream, filesystem, "
        "descriptor, transport, deadline, status, evidence, and shutdown boundaries.",
    )
    body(
        doc,
        "Those are bounded implemented slices, not a claim of complete production lifecycle "
        "composition. Production command and SSH dispatch, measured real LAN warm-cache "
        "hits, remote native-package custody, complete synthetic native-CLI lifecycle proof, "
        "and audited legacy-worktree retirement remain open under explicit 1.3 trackers.",
    )
    heading(doc, 3, "Production containment backends")
    body(
        doc,
        "Realize the existing production-containment contract as supported OS-enforced host "
        "backends. The current explicit host-execution path is not a security boundary.",
    )
    heading(doc, 3, "Multi-entrypoint deployment and rollback")
    body(
        doc,
        "Automate launch, health, upgrade, and rollback for the deployment units that 0.9 "
        "already builds and accepts, without collapsing their evidence or authority.",
    )
    heading(doc, 3, "Native application-package formats")
    body(
        doc,
        "Extend the qualified local Debian construction path to compatible remote workers "
        "with exact returned artifact and inspection-evidence custody, then address remaining "
        "provider formats without treating host prerequisite installation as package proof.",
    )
    heading(doc, 2, "The portfolio destination")
    body(
        doc,
        "The long-term destination is a living portfolio of intent: shared capabilities "
        "evolve once, products compose them independently, and every change carries its "
        "dependency path and proof forward. Portfolio-scale concurrency is design intent. "
        "No throughput figure is a measured result. Any product-studio implementation claim "
        "is intentionally absent: the inspected successor project is a specification "
        "scaffold without an indexed implementation surface.",
    )

    heading(doc, 1, "How to engage")
    heading(doc, 2, "Starting with one demanding application")
    body(
        doc,
        "Use the operator front door on one demanding downstream application and carry it "
        "through an explicit retained receipt. Use the next investment cycle for production "
        "containment and operational hardening while preserving the architecture needed for "
        "many. The requested decision is production-hardening investment plus selection of "
        "that first downstream 1.2 production adoption.",
    )
    heading(doc, 2, "Building for the portfolio")
    body(
        doc,
        "Build for the portfolio from the first application: current gates, no shortcuts, "
        "and a regeneratable knowledge system rather than another copied implementation. "
        "The published presentation and this narrative are the 1.2.0 edition of that "
        "argument. They do not grant release authority.",
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(OUT))
    return OUT


def main() -> None:
    out = build()
    print(f"built narrative -> {out}")
    if not PPTX.is_file():
        raise SystemExit(f"presentation artifact missing: {PPTX}")
    manifest = write_manifest(PPTX, out)
    print(f"capability manifest -> {manifest}")


if __name__ == "__main__":
    main()
