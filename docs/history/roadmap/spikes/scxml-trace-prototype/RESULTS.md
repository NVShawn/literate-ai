# Declared-trace sidecar format: design pass and prototype results

- **Status:** historical
- **Owning queue item:** [SPEC-HIERARCHY-SCXML-001](../../../../roadmap/active-work.md#x-spec-hierarchy-scxml-001-design-and-implement-an-scxml-specification_provider)
- **Completion / archival evidence:** [`trace_sidecar_validator.py`](trace_sidecar_validator.py), proving the schema and closing the "one narrower design pass" SPEC-HIERARCHY-SCXML-001's Next action called for.
- **Parent design doc:** `docs/history/roadmap/0.6.0-scxml-provider-design.md` ("Open
  questions... answered", question 2, and the "Maturity / next-action
  assessment" section).

## Artifacts in this spike

- [`connection_handshake.scxml`](connection_handshake.scxml) — the sample chart
- [`connection_handshake.trace-straight-line.json`](connection_handshake.trace-straight-line.json)
- [`connection_handshake.trace-parallel.json`](connection_handshake.trace-parallel.json)
- [`connection_handshake.trace-history.json`](connection_handshake.trace-history.json)
- [`trace_sidecar_validator.py`](trace_sidecar_validator.py) — the static structural validator

## Relationship to the sibling `0.6.0-scxml-prototype/` spike

The design doc already recorded, on 2026-08-19, that the *placement* decision
for a trace sidecar was made and prototyped once already
(`docs/roadmap/0.6.0-scxml-prototype/media_player.trace.json` +
`trace_conformance.py`): a separate file next to the `.scxml` document,
JSON (not inline authoring Markdown, not an XML processing instruction
inside the `.scxml` itself), with a full *stateful* replay checker that
tracks `<history>` across steps. That prior spike answered **where the
sidecar lives** and proved a stateful interpreter can validate it end to
end.

This spike answers a narrower, still-open question that prior spike did not
attempt: **what the sidecar's own schema should look like as a reusable
format** (field names, how it expresses simultaneity under `<parallel>`,
how it annotates history re-entry, how it handles ordering/timing), and
whether a **lighter-weight, purely static structural check** — the kind a
`litai spec scxml-review`-shaped verb could run cheaply, without needing a
full interpreter — is sufficient to catch the most common authoring
mistakes in a hand-written sidecar document. It is deliberately validated
against a **second, independently-authored** `.scxml` sample
(`connection_handshake.scxml`, a different domain than `media_player.scxml`)
so the format is shown to generalize, not merely fit the one chart it was
designed against.

## The SCXML sample

`connection_handshake.scxml` — a connection-handshake protocol. `connect`
enters a `<parallel id="negotiating">` state with two independent regions
(`auth`, `caps`); a `drop` event suspends the connection mid-negotiation;
reconnecting resumes via `<history id="conn_history" type="deep">`
instead of restarting both regions. 14 declared
state/parallel/final/history nodes: `disconnected`, `connecting`,
`conn_history`, `negotiating`, `auth`, `auth_pending`, `auth_failed`,
`auth_done`, `caps`, `caps_pending`, `caps_done`, `suspended`,
`established`, `closed`. Uses SCXML's standard auto-raised
`done.state.negotiating` event (fired once every region of a `<parallel>`
reaches its own `<final>`) as the ancestor-declared transition off the
parallel state, the same "event bubbling" pattern the sibling
`media_player.scxml` sample already validated.

## The proposed sidecar format

One sidecar file per set of related traces, JSON, conventionally named
`<component>.trace-<purpose>.json` next to (or, for a single combined file,
matching) the `.scxml` document — consistent with the placement the parent
design doc already decided (separate file, not inline Markdown, not an XML
processing instruction).

```jsonc
{
  "$schema": "litai-scxml-trace-sidecar/v1",
  "scxml": "connection_handshake.scxml",
  "traces": [
    {
      "id": "unique-within-file",
      "description": "prose, why this trace exists",
      "steps": [
        {
          "event": "connect",              // required; the triggering event
          "auto": false,                    // optional; true = SCXML-auto-raised
                                             //   (e.g. done.state.<id>), not sent
                                             //   by an external caller
          "expect_active": ["auth_pending", "caps_pending"],
                                             // required; the FULL set of atomic
                                             //   leaf state ids active after this
                                             //   event -- one entry per <parallel>
                                             //   region currently active, plus
                                             //   whatever's active outside any
                                             //   <parallel>
          "expect_regions": {                // optional; human-readable annotation
            "auth": "auth_pending",           //   mapping a <parallel> region's
            "caps": "caps_pending"            //   root id -> its active leaf;
          },                                 //   cross-checked against
                                             //   expect_active, not authoritative
                                             //   on its own
          "via_history": "conn_history",     // optional; names the <history>
                                             //   node this transition is expected
                                             //   to resolve through (documents
                                             //   intent; validator confirms the
                                             //   id names a real <history> node)
          "note": "free text"                // optional; documents WHY this
                                             //   step matters (e.g. "this is the
                                             //   assertion that distinguishes
                                             //   default-entry from recorded
                                             //   history-entry")
        }
      ]
    }
  ]
}
```

### (a) Expressing simultaneous activity under `<parallel>`

`expect_active` is a flat set of every atomic leaf state id active at once
— the natural representation of an SCXML "configuration," which is
literally defined by the spec as a set of simultaneously-active atomic
states. This is the *authoritative* field. `expect_regions` is an optional,
purely diagnostic sibling: a map from a `<parallel>` region's root id to
its currently-active leaf, useful for readability when a chart has several
regions and a reviewer wants to see "which region is in which state" at a
glance without mentally re-deriving it from the flat leaf set. The
validator cross-checks `expect_regions` against `expect_active` and against
the chart's actual parallel/region structure, so the two can never silently
drift apart; if only one is worth authoring, `expect_active` is the one
that matters. Static structural well-formedness of a `<parallel>`
configuration means: every region of every implied `<parallel>` ancestor
has *exactly one* active leaf represented in the set (see
`_validate_parallel_region_coverage` in `trace_sidecar_validator.py`) — no
region left uncovered, no region double-covered, no leaf listed together
with its own ancestor.

### (b) Expressing history re-entry

No new step *type* is needed — a history-driven transition is authored
exactly like any other step (an `event`, an `expect_active`). The one
addition is the optional `via_history` annotation, which names the
`<history>` pseudostate the author expects this transition to route
through. It is intentionally documentation, not the primary assertion: the
primary assertion is still `expect_active` declaring the *exact*
post-re-entry configuration, which is what actually distinguishes correct
history behavior (restoring the last-active sub-configuration) from a
history-blind implementation (falling back to the chart's default-entry
configuration). `connection_handshake.trace-history.json` is built
specifically to make that distinction observable: after `caps` finishes
(`caps_done`) but `auth` is still pending, a `drop` followed by `connect`
must restore `{auth_pending, caps_done}`, not the default entry
configuration `{auth_pending, caps_pending}` a naive re-implementation
would produce. `via_history` lets a static structural check confirm the
referenced id is genuinely a `<history>` node (catching a typo'd or
wrong-id reference); confirming the *value* asserted after it is correct
requires the stateful interpreter the sibling `0.6.0-scxml-prototype/
trace_conformance.py` spike already prototyped — see "Known limitations"
below for why that split is deliberate, not an oversight.

### (c) Ordering and timing

Steps are an ordered list; sequence in the array *is* the declared event
order — no separate ordering field is needed for the common case. The
`auto` flag documents which steps are SCXML-internal auto-raised events
(most importantly `done.state.<id>` on `<parallel>` completion) versus
events an external caller/environment actually sends, which matters for a
future test-generation consumer of this format (an `auto: true` step
should become an assertion in generated test code, not a call the test
harness makes). Genuine wall-clock timing (e.g. `<send delay="...">`) is
deliberately **not** modeled as a first-class field in this version of the
format: SCXML's own timing model is already fully delegated to generated
code per the parent design doc's "minimum viable adapter" answer (litai
is a parser/static-analyzer, not an interpreter), and a delay assertion
would require exactly the stateful, time-aware interpretation this format
is designed to avoid needing. The `note` free-text field is sufficient for
documenting timing-sensitive intent in this version; a `min_delay_ms`/
`max_delay_ms` per-step field is a plausible v2 addition if a real
Component ever needs it (see Known limitations).

## Prototyped trace documents

Three sidecar documents against `connection_handshake.scxml`, each proven
structurally valid by `trace_sidecar_validator.py`:

- **`connection_handshake.trace-straight-line.json`** — no drop, no
  history: `connect` → `auth_ok` → `caps_ok` → auto `done.state.negotiating`
  → `disconnect`. The baseline case.
- **`connection_handshake.trace-parallel.json`** — proves the two
  `<parallel>` regions progress independently: `caps` reaches its `<final>`
  while `auth` is still pending, then `auth` fails and retries without
  disturbing `caps`'s already-final leaf, before both are simultaneously
  final. Uses `expect_regions` throughout for readability.
- **`connection_handshake.trace-history.json`** — the history-re-entry
  case described in (b) above: partial progress, a `drop`, then `connect`
  again, asserting the exact partial sub-configuration is restored via
  `conn_history` rather than the chart's default entry.

## The prototype validator

`trace_sidecar_validator.py` — stdlib-only (`xml.etree.ElementTree` +
`json`), standalone (does not import from the sibling
`0.6.0-scxml-prototype/` spike, per the task's instruction to keep this
spike self-contained). It parses the `.scxml` chart's state/parallel/
final/history structure and validates a sidecar document **structurally**:

- every `expect_active`/`expect_regions`/`via_history` id resolves to a
  declared node in the chart;
- every id in `expect_active` is an atomic leaf (`<state>`/`<final>` with
  no children), not a compound/parallel container;
- no id in `expect_active` is an ancestor of another id in the same set;
- for every `<parallel>` node implied by the configuration, every region
  (direct child) is covered by *exactly one* active leaf — the structural
  definition of a valid simultaneously-active configuration;
- `expect_regions` entries are cross-checked against `expect_active` and
  against the chart's actual region structure;
- `via_history` names an actual `<history>` node;
- every `event` is at least declared somewhere in the chart (a warning,
  not a hard failure, since this is a coarse sanity check, not full
  reachability-from-current-state analysis).

Confirmed by direct testing: all three prototyped trace documents pass
against `connection_handshake.scxml`. Deliberately broken variants (an
unknown state id, an incomplete `<parallel>` region set, a `via_history`
pointing at a non-`<history>` node, and an `expect_regions` entry
inconsistent with the chart's actual structure) were each constructed and
confirmed to fail with a specific, actionable error message rather than a
crash or a silent pass — proving the checks are load-bearing, not
vacuous.

Run it yourself:

```
python3 trace_sidecar_validator.py connection_handshake.scxml \
  connection_handshake.trace-straight-line.json \
  connection_handshake.trace-parallel.json \
  connection_handshake.trace-history.json
```

## Known limitations

- **This is a structural checker, not a conformance checker.** It confirms
  a trace document *could* refer to a real, coherently-shaped
  configuration of the chart — it does not confirm that firing `event` N
  from the configuration after step N-1 actually *produces* the
  configuration declared in step N. That is full interpretation
  (event-driven, stateful, `<history>`-tracking replay), which the sibling
  `0.6.0-scxml-prototype/trace_conformance.py` spike already prototyped
  successfully against `media_player.scxml`/`media_player.trace.json`. The
  two are complementary, not competing: a real `SCXMLProvider` implementation
  should run the cheap structural check first (catches typos and malformed
  configurations fast, with no interpreter needed) and the stateful replay
  check second (catches genuine behavioral mismatches, including exactly
  the default-entry-vs-history-entry class of bug the parent design doc's
  Finding 4 identified as the one place the two oracles must not share an
  implementation).
- **`via_history` is documentation, not a hard constraint on the previous
  step.** The validator confirms the referenced id is a real `<history>`
  node; it does not (and structurally cannot, without replay) confirm that
  the transition actually taken between the previous and current step was
  the one declared on that `<history>` node. A future stateful checker
  built on this schema could tighten this by also asserting `via_history`
  against replay state.
- **No timing/delay field yet** (see (c) above) — deliberately deferred
  until a real Component's needs justify it, consistent with the parent
  design doc's stance that litai should not take on interpreter-shaped
  responsibility it doesn't have a concrete need for.
- **`expect_regions` duplicates information already in `expect_active`.**
  This is an intentional readability trade-off, not an oversight — the
  validator enforces they cannot silently disagree, so the duplication
  cannot become a source of hidden bugs. A future iteration could instead
  make `expect_regions` the sole authoring surface and *derive*
  `expect_active` from it mechanically; that was not done here to keep
  `expect_active` — the SCXML-native "configuration" concept — as the one
  required, unambiguous field, with `expect_regions` staying strictly
  optional.
- **Event-declared-somewhere check is coarse.** It confirms an event
  string appears on *some* transition in the chart, not that it's a valid
  event from the *specific* configuration the trace is currently in. This
  is intentionally a warning, not an error, precisely because getting it
  right requires the same stateful reasoning the structural checker is
  designed to avoid needing.

## Recommendation

**Ready for the eventual production `SCXMLProvider`, not needing a further
speculative design iteration before implementation.** The schema is small,
each field earns its place against a concrete need surfaced by one of the
three prototyped traces (not speculatively added), and the two-tier
oracle split (cheap static structural check here; stateful replay check
already proven in the sibling spike) matches the parent design doc's
existing "two complementary oracles, not a single either/or" conclusion
exactly — this spike fills in the previously-unspecified schema for the
static side of that split with something now validated against a second,
independently-authored chart. What remains is ordinary implementation
work, already named in the parent design doc's "Maturity / next-action
assessment": porting this shape (plus the sibling spike's stateful
replay logic) from throwaway `ElementTree` scripts into the real
`SCXMLProvider`, the registry refactor shared with DMN, and the new
`litai spec scxml-review`-shaped verb that would actually invoke both
checks. No further open *design* questions were found for the sidecar
format itself.
