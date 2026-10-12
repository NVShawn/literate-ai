# HTML observability: derived views over canonical JSON

The accepted [HTML observability contract](../../schemas/v2/html-observability.schema.json)
describes single-file, regenerable views over existing JSON. It does not introduce
a planner, dashboard daemon, server, or new source of project authority. The
[archived Phase 0/1 plan](../history/roadmap/observe-html-phase-0-1-plan.md) records
the graph implementation and acceptance. The [current scope audit](../roadmap/html-observability-completion.md)
owns remaining umbrella obligations. `litai render html` and its staleness gate now share source loading and
exact output reconstruction. The [active roadmap](../roadmap/active-work.md) records
qualification by source revision; implemented views are not automatically release-qualified.
Typed core records live in `literate_ai.contracts.html_observability`.
`adapters/html_surfaces.py` registers these project-scoped views at version `1.0.0`:

| Surface | View | Canonical producer |
| --- | --- | --- |
| `authority-graph` | `dag` | Effective project authority graph |
| `version-check` | `health` | Existing version-check report |
| `lock-health` | `health` | Shared current Component/repository lock observations |
| `verification-health` | `health` | Selected canonical project-verification gates |
| `performance-history` | `history` | Bounded diagnostic spans from the project build root |
| `workflow-routing` | `catalog` | Exact declared workflow and routing catalog bytes |

`adapters/html_emitter.py` now emits inspected single-file bytes for that source.
Its complete DAG view uses one SRI-pinned Cytoscape.js library, fixed inline controls
and the same artifact's native catalog/relationship/source disclosures as a readable
fallback. The emitter itself does not publish files or exercise a cache policy;
`adapters/html_render.py` now owns that service and `cli/render.py` wraps it.
`adapters/html_source_excerpts.py` supplies bounded, hash-checked local Markdown/JSON
previews using the existing graph's path/identity pairs. The emitter embeds these
observations as JSON and escaped native previews. Missing pairs are explicitly
unavailable; declared sources that are
unsafe, changed, oversized or not UTF-8 refuse the observation. Display truncation
occurs only after verifying the complete source bytes, without changing graph JSON.

## Contract responsibilities

All names below share the prefix `urn:literate-ai:schema:v1:html-observability-`.
The linked schema, not this table, owns fields and validation rules.

| Contract suffix | Responsibility |
| --- | --- |
| `view` | Versioned view and its project or Component scope |
| `source-binding` | Identity of one canonical input surface |
| `external-asset` | HTTPS reference pinned by Subresource Integrity |
| `renderer-binding` | Exact renderer, framework and template provenance |
| `provenance` | Embedded input and renderer evidence |
| `artifact` | Completed file identity and embedding attestation |
| `staleness-report` | Current-state comparison against retained provenance |
| `surface` | Registered JSON producer and supported views |
| `render-request` | Surface/view selection, destination and cache/asset policy |
| `render-refusal` | Closed-set reason why no acceptable artifact was produced |
| `render-result` | Exactly one artifact or typed refusal |

## Identity without self-reference

An HTML file cannot contain its own final digest: inserting that digest changes the
file. The embedded `litai-provenance` JSON therefore carries provenance and render-input
identities; the enclosing artifact record carries the completed file identity.
Staleness compares the current input/view/renderer identity with the retained one.
Task IDs and wall-clock observations must not manufacture new render-input identities.
The renderer still has to prove deterministic output; a schema-valid record alone
does not prove any of these content relationships.

The core's `render_inputs_identity` hashes exactly the ordered source bindings,
view and renderer through the existing canonical JSON identity helper. It does
not accept task/correlation metadata or timestamps. `HtmlProvenance.create`
adds the explicitly supplied UTC observation and declared assets, then hashes
the resulting provenance excluding its own identity. Reading provenance checks
both identities instead of trusting a caller-supplied digest. Source labels and
asset identifiers must be unambiguous within the record.

`HtmlArtifact.from_bytes` binds the completed byte sequence and its size; it does
not attest that arbitrary bytes are safe or self-contained HTML. The emitter
owns that inspection. Artifact construction also checks the declared
external-reference count against the declared asset list. Typed records reject
unsafe portable path representations, malformed calendar instants and invalid
SHA-256/384/512 SRI encodings; filesystem containment and browser enforcement
remain separate runtime checks.

The emitter binds its implementation, inline-view and excerpt-projection modules,
presentation profile and ordered asset declarations into the template identity.
An asset URL, kind, order or SRI change cannot retain the same render-input identity;
neither can the private static-assembly primitive masquerade as the full DAG. It
inspects the completed document's closed element and
attribute vocabulary, balanced structure, fragment links, exact inline stylesheet,
JSON blocks, exact inline application script and external references before computing
the final artifact record.
This is an assertion about its own template, not a sanitizer for arbitrary HTML or
CSS. Visible strings use HTML escaping; embedded JSON separately escapes HTML
delimiters and JavaScript line separators, preserving decoded canonical input.

This low-level byte API requires an explicitly supplied framework distribution
identity, catalog release and UTC observation. It does not discover or qualify an
installation. The P1.5 service resolves the actual installed distribution and owns
publication, cache custody and repeat-render observation reuse; a synthetic development-fixture
binding must never be substituted for installed-release evidence. Identical supplied
inputs produce identical bytes. Changing the observation changes provenance and
file bytes, but not the semantic render-input identity.

P1.5 groundwork in `adapters/html_framework.py` reuses the Standard installed-wheel
observer and additionally requires the imported package and loaded framework modules
to belong to that wheel's recorded payload. Both active schema catalogs must match
its exact member inventory and bytes, including when an operator selects a catalog
override. Source checkouts, editable or ambiguous installations, mixed import roots
and changed catalogs refuse with the existing `render.surface_unavailable` code.
The rendering service repeats this observation before publication. This is an
in-process installation check, not an OS isolation guarantee or installed-release proof.

The existing `storage.ReferenceIndex` now supports read-only snapshots without
creating directories or lock files. It retains CAS verification on resolution,
rejects writes, bounds index reads and refuses observed publication drift rather
than reporting it as a cache miss. Its writable default and reference wire format
are unchanged.

## CLI publication and cache behavior

`litai render html` wraps the typed result in the existing CLI envelope and returns
nonzero on a typed refusal. Installed help owns the flags. The service uses the
existing object-cache ownership marker, CAS and reference index beneath resolved
`OBJ_DIR`. A cache hit must reproduce the exact current artifact at its retained
observation time; correct CAS hashes alone do not make a hit valid. Reusing a hit
at another output path preserves its bytes and changes only the enclosing path field.
Off/write-only modes observe the current UTC clock; the accepted contract has whole
second precision, so independent observations in one second can have identical bytes.
Read-only mode may publish the requested HTML but creates no cache directories,
markers, blobs, aliases or lock files. The CLI suppresses its incidental performance
cache span for this operation. Output publication still uses the existing project
lifecycle lock, outside the object cache, in every mode.

Publication rejects repository metadata paths, indirect paths, non-regular files,
and output inside the HTML cache. A bounded existing file must reconstruct exactly
from the supported template and its embedded observations before it can be replaced.
Borrowed provenance does not admit edited visible content. This recognizes derived
output, not source currency: stale inputs may be regenerated, but foreign/edited
files and artifacts from an unrecognized template require a new destination.
The service rechecks current source bytes and the installed binding immediately
before publication. Temporary bytes are flushed before an atomic replacement;
new destinations use a no-clobber link. Detected destination changes refuse,
and cleanup preserves a foreign replacement at the temporary pathname. These
cooperative-lock and snapshot checks are not same-user OS isolation. Cache failure
does not publish an output file; failed later publication may retain immutable cache
objects for a verified observation, not a successful output claim.

Local CLI, cache and publication regressions exercise the real graph producer and
emitter with explicitly synthetic installation discovery. A separate real
non-editable-wheel run now qualifies the derived project's CLI/cache behavior.
The update/browser journey and hosted integration remain separate gates.

## Single-file and network boundary

Application JavaScript and CSS are inline, with no companion asset tree or bundler.
The accepted contract permits explicit HTTPS CDN library references with SRI instead
of vendoring libraries inline. This keeps library bytes independently pinned and
visible in provenance. It does not make a CDN-dependent interactive view offline;
the Phase 1 renderer must provide an honest readable fallback when the library is
unavailable. `inline-only` refuses the library-dependent DAG view. Loading the library
is deferred so native content can appear first. Local catalog filtering, native node
selection and source inspection continue after CDN failure; the pan/zoom/layout
controls stay disabled with an explicit unavailable message. With JavaScript disabled,
the native disclosures still expose verified source previews.

`adapters/html_dag_view.py` owns fixed inline CSS/JavaScript and the exact asset pin.
Graph identifiers used by the library are generated, not untrusted selector strings;
native options and inspector content use text values, not HTML insertion. The library
lays out the existing edges without deriving another graph: isolated projections use
a grid, connected projections seed that grid and use bounded built-in CoSE with
randomization disabled. Filter, focus, keyboard pan/zoom and node pointer interactions
are local transforms; source previews never trigger a browser file or network fetch.

Rendering must escape untrusted labels and source excerpts, including `</script>`
and HTML comment openers within embedded JSON. Code must validate cross-field counts and actual
HTML references as well as schema shape. An embedding attestation is a claim to
verify, not a sandbox or proof that the emitted HTML matches it.

## Failure and extension boundaries

Unknown surfaces/views, unavailable input, unsafe output and incompatible cache or
asset policies return typed refusals rather than partial success. A no-artifact
project skips the future staleness gate; missing or malformed provenance in a
declared artifact is a finding. Verification must not regenerate files.

Phase 2 can register more named JSON surfaces without replacing the graph derivation.
The [authority-graph schema](../../schemas/v2/authority-graph.schema.json) gives the
existing `literate-ai/authority-graph@2` document a catalog URN without changing its
discriminator or exporters. The source adapter calls the existing producer, validates
its actual JSON and binds its existing graph identity. It observes the graph itself,
not the CLI wrapper's absolute project/output paths. Schema shape checks do not
replace the producer's DAG integrity checks or identity calculation.

The adapter returns immutable bytes and fresh decoded values; changes to authored
source change the binding, while relocating an otherwise identical project does not.
Unsupported surface/view combinations and missing, malformed or invalid authority
return typed refusals. The emitter consumes this graph-to-source-snapshot handoff;
neither adapter changes an accepted HTML contract or existing graph exporter.

## Performance, workflow and portfolio views

`performance-history` renders the existing diagnostic `PerformanceSpan` records as
run, stage, duration and failure history. It reads at most 4,096 newest records and
states when older rows were omitted. An empty log is displayed as unavailable
history, not as evidence that builds or tests passed. The native duration bars are
scaled against the longest retained span and require no external chart library.

`workflow-routing` reads only the manifest's declared workflow and routing roots.
Workflow Markdown is normalized by the existing generation planner, routing JSON is
retained verbatim, and every catalog entry carries the SHA-256 identity of its exact
source bytes. Catalog traversal is bounded and rejects links or paths outside their
declared root. The view exposes stage dependencies and the complete routing policy;
it does not create another workflow executor or router.

`litai render dashboard --root ROOT --project PROJECT ...` implements the Phase 3
offline composition shell. Every selected project contributes its declared,
already-rendered `html_render_requests`; missing, edited, foreign or unrecognized
artifacts refuse the whole dashboard. The shell records each artifact digest and
embeds only relative iframe references, so projects under one selected common root
can be viewed side by side without a server. It contains no live refresh, discovery
daemon or correctness rollup: each pane retains its own exact provenance and verdict.
Existing foreign dashboard output is preserved rather than overwritten.


## Version agreement health view

The `version-check` surface supports the `health` view at version `1.0.0` for a
selected project. It calls the existing `check_versions(require_project=True)`
producer, validates its [published report schema](../../schemas/v2/version-check.schema.json),
and binds the canonical JSON bytes by content identity. The report retains its
`literate-ai/version-check@1` discriminator and every nested diagnostic. Rendering
does not recalculate the producer's overall `ok` or convert an unavailable release
tag into a failure when the command's policy permits an unreleased checkout.

The HTML displays the overall version result, distribution, release-tag and project
results, and expandable schema-catalog diagnostics. Schema-catalog reports do not
have an independent overall boolean; the view labels them as diagnostics rather
than inventing a passing verdict. All content is readable without JavaScript or
network access. This surface requires no external assets and supports `inline-only`.

The shared publication, cache and staleness machinery binds the exact report,
renderer and view. Changed reports are stale; edited visible bytes with borrowed
provenance remain unpinned. The generation timestamp describes an observation,
not a continuously monitored result. Version agreement does not run project builds,
tests or the `verify` command. A verification dashboard remains separate: directly
calling that producer during HTML source reobservation would recurse through its
HTML gate, so its report-snapshot and input-currency contract must be explicit.

Review screenshots from the installed wheel at `330dd21a` show the new view
before and after expanding its disclosures, with JavaScript disabled:

| Viewport | Initial view | Expanded diagnostics |
| --- | --- | --- |
| Desktop, 1280px | [Screenshot](assets/version-health/desktop-no-script-collapsed.png) | [Screenshot](assets/version-health/desktop-no-script-expanded.png) |
| Mobile, 375px | [Screenshot](assets/version-health/mobile-no-script-collapsed.png) | [Screenshot](assets/version-health/mobile-no-script-expanded.png) |

These screenshots are retained review evidence for the fixture report, not a live
status display. The [roadmap evidence](../roadmap/active-work.md) records the exact
wheel, HTML, report and browser-result identities.

## Component lock and resolution-audit health

`litai render html --project . --surface lock-health --view health --output locks.html
--cache-mode off --external-asset-policy inline-only` renders the shared verifier
observation under the [v2 lock-health schema](../../schemas/v2/project-lock-health.schema.json).
The view retains the producer's gate state and detail. Each committed Component lock
has its project-relative path, current/not-current/error state, full lock-command
report and any structured refusal. Resolution identities, selected providers and
both artifact-check diagnostics remain visible in native expandable sections.
Repository failure leaves Component checks unobserved; Components without committed
locks are outside this verifier gate. Neither case is displayed as successful
Component qualification.

The observation calls shared lock orchestration in `adapters/component_lock_commands.py`
and `adapters/repository_lock_commands.py`. CLI wrappers translate refusals only.
HTML does not import the CLI or call full verification, so its source cannot recurse
through the HTML verification gate. It computes currentness from current inputs and
artifact checks, not from a stored audit alone. This surface has no external assets;
its details remain usable without JavaScript. Input drift changes the source identity,
and edited visible output fails exact artifact reconstruction. The wheel at `3b0f941d` passes installed qualification, including current,
missing-audit/stale and restored/current verification. Its retained 10,041-byte
artifact passes desktop/mobile browser inspection with scripts enabled and disabled,
without companion files, runtime errors or overflow. Full integration and hosted
qualification remain open in the active roadmap.

The screenshots below show that installed-wheel artifact before and after expanding
its Component and provenance disclosures. This new surface has no prior HTML version
to use as a pre-feature screenshot.

| Viewport | Initial view | Expanded disclosures |
| --- | --- | --- |
| Desktop | [Initial](assets/lock-health/desktop-no-script-collapsed.png) | [Expanded](assets/lock-health/desktop-no-script-expanded.png) |
| Mobile | [Initial](assets/lock-health/mobile-no-script-collapsed.png) | [Expanded](assets/lock-health/mobile-no-script-expanded.png) |

The installed qualification harness also retains separate not-current, malformed-lock
error and empty observations, each derived from actual fixture lock/audit changes
and compared with the public verifier. It restores the original fixture bytes even
when qualification fails. Wheel `64e6dbe0` passed this expanded qualification. All
16 isolated-file browser cases passed across the four states, desktop/mobile and
JavaScript enabled/disabled. The eight expanded screenshots below were also inspected
for readable diagnostics, correct state labels and overflow. Full-suite and hosted
qualification of the final branch remain required.

| Installed fixture state | Desktop expanded | Mobile expanded |
| --- | --- | --- |
| Current | [Screenshot](assets/lock-health-states/healthy-desktop.png) | [Screenshot](assets/lock-health-states/healthy-mobile.png) |
| Missing audit / not current | [Screenshot](assets/lock-health-states/not-current-desktop.png) | [Screenshot](assets/lock-health-states/not-current-mobile.png) |
| Malformed lock / error | [Screenshot](assets/lock-health-states/error-desktop.png) | [Screenshot](assets/lock-health-states/error-mobile.png) |
| No committed lock / skipped | [Screenshot](assets/lock-health-states/empty-desktop.png) | [Screenshot](assets/lock-health-states/empty-mobile.png) |


## Verification health

`litai render html --project . --surface verification-health --view health
--output verification.html --cache-mode off --external-asset-policy inline-only` renders the existing
`literate-ai/project-verify@1` report through a shared read-only adapter. The
[published schema](../../schemas/v2/project-verify.schema.json) preserves the CLI
wire discriminator, gate diagnostics, counts and verdict. The CLI retains its
existing gate selection, ordering, errors and exit codes.

The dashboard observes authority, locks, source intelligence and the test receipt.
It excludes the HTML-artifact gate: including that gate would make the report's
source identity depend on its own output. The page discloses this coverage and
labels its verdict “Selected gates.” Ordinary `litai verify` still evaluates all
five gates, including retained HTML freshness. A skipped check is shown separately
from a pass. Observation does not build Components or run their tests.

Each check has native expandable diagnostics that work without JavaScript. Counts,
source identity and provenance remain visible offline. The adapter refuses an
unavailable or malformed report, different project scope, unexpected gate coverage,
or inconsistent counts and verdict. Changed diagnostics invalidate the retained
source binding; edited visible output fails artifact verification.
