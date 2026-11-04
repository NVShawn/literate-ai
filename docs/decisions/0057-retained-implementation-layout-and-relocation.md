# ADR 0057: Select and relocate retained implementation layouts transactionally

- Status: Accepted
- Date: 2026-11-03
- Acceptance: The directing user explicitly accepted ADRs 0056–0059 for 1.4 on 2026-11-03 ("Accept all four ADRs").
- Decision owners: directing user and literate-ai maintainers
- Release target: 1.4.0
- Roadmap: [ADOPT-RELOCATE-001](../roadmap/active-work.md#adopt-relocate-001-support-selected-and-relocated-retained-implementation-layouts)
- Issue: [#265](https://github.com/jordanhubbard/literate-ai/issues/265)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)

## Context

Adoption moves retained source below a Component named `legacy-project-wrapper`.
Some readers use the recorded implementation directory, while inventory, updates,
readmission and wrapper-lock discovery still reconstruct the legacy path. Editing
one evidence field or leaving a symlink cannot safely migrate that authority.
Users need a plain subdirectory, repository-root placement and a chosen wrapper
Component name. All three remain required for 1.4.

## Decision

### One current layout authority

Introduce an opt-in versioned retained-layout declaration that names the wrapper
Component coordinate and implementation coordinate. Use `.` explicitly for root
placement; all other coordinates are canonical portable relative paths. Do not
weaken the shared relative-path contract to accept `.` everywhere. The chosen
wrapper remains a normal Component with a retained-harness role; qualified use
requires its current lock. Its name alone never proves that role.

One resolver joins the declaration with the actual project, conversion authority,
wrapper Component, lift-and-shift record, source scope and current physical
ancestry. All consumers use that resolver, including adoption, inventory, update,
retained-scope refresh, receipts, readmission, monorepo discovery and retained
Cargo. Disagreement, ambiguous wrappers or a redirected physical directory refuse.
The resolver does not repair evidence or infer relocation from a missing old path.

Distinguish structurally current layout from qualified retained execution. A
completed relocation commits a structurally resolved but unqualified layout,
with explicit lineage to the prior layout and invalidated lock/receipt evidence.
The ordinary lock, inventory, baseline, parity and readmission owners must be
able to locate and renew this exact layout without first satisfying the stale
evidence they replace. Their existing authorizations and source-custody checks
still apply. Receipt creation and lifecycle/package admission require the full
renewed current authority. Do not expose a general caller-controlled switch that
treats an unqualified layout as qualified, or accept a historical lock as current.

Existing projects without the new declaration keep their exact legacy behavior
and evidence identities. A legacy lift-and-shift record remains readable through
the same resolver. Selecting a new layout requires a supported transition; merely
editing a historic path cannot qualify a layout or preserve a receipt.

### Initial placement and root ownership

Expose implementation-directory and wrapper-name choices through adoption's
existing read-only plan and acknowledged apply flow. Preserve the default layout.
The plan inventories original retained source before reserving new framework
authority paths. Refuse an original-source collision with a new Component,
metadata file, entry point or other framework-owned coordinate; do not overwrite
or silently exclude it.

For root placement, retain source at the repository root and keep the wrapper
Component's specifications separate. Bind a finite explicit ownership partition
between original retained inputs and current framework-owned files. Capture the
complete retained source namespace, including newly added inputs; the partition
must not become an allowlist that hides future source. Neither all ignored files,
all Component directories nor the entire root may be excluded by convention.
An ancestor directory shared by the two partitions is not itself exclusive
ownership. Nested independent projects keep their own declared authority.

### Reviewed relocation transaction

Add public `litai project relocate-implementation` plan and apply operations.
The read-only plan binds the current layout, source bytes/modes/link topology,
physical ancestry, project/conversion authority, exact affected authored files,
current Component locks, destination occupancy and the proposed ownership partition.
Application requires that exact plan identity and current host-execution authority.
No new approval is required for installing an already-authorized prerequisite.

Hold the canonical project lifecycle lock and descriptor-backed reservations
through revalidation, source movement and metadata publication. Move only the
retained ownership partition and explicitly selected wrapper authority. Root
placement never renames the repository or moves its Git administration. Preserve
relative internal aliases, complete hardlink groups, executable modes and exact
bytes. An alias or hardlink crossing the proposed ownership boundary refuses.
Require one filesystem and available exclusive rename primitives; a cross-device
move refuses before mutation instead of silently copying or losing custody.

Publish updated layout, lift-and-shift lineage, wrapper configuration and affected
manifests together as a recoverable transaction. A multi-path root transition is
not claimed to be one atomic filesystem rename. Persist bounded private recovery
state before the first mutation, then publish only exact expected old/new states.
On failure, roll back only still-owned nodes and exact metadata bytes. Preserve
concurrent changes and report incomplete recovery; never remove foreign files or
turn a saved success flag into ownership. A crash-retained reservation requires
explicit supported recovery before another apply.

Every successful relocation invalidates previous test receipts and retained
admission. Do not rewrite old receipts or manufacture a new positive receipt.
Resolve fresh locks through the ordinary reviewed lock operation, then require
the genuine inventory/baseline/parity/readmission and acceptance owners to renew
the affected evidence. Report requalification required until those gates pass.

### Acceptance

Run non-editable installed public journeys covering initial placement and
relocation into a plain directory, into the project root, out of the project root,
and with a non-default wrapper name. Exercise update, retained-scope refresh,
readmission, lock renewal and fresh receipt creation on the resulting layouts.
Prove unchanged legacy behavior and rejection of an old receipt after movement.

Retain actual native source files, executable modes, internal file/directory
aliases and complete hardlinks across the moves. Exercise stale plans, changed
source or authority, portable path aliases, foreign destinations, escaping links,
cross-boundary hardlinks, cross-device refusal, interrupted metadata publication
and interrupted root movement. Recovery must restore original bytes and custody
or report precisely retained unresolved state. Include concurrent-owner protection
and missing recovery authorization. Complete independent source review, native
platform evidence and the integrated release gates before closing #265.

## Consequences

Retained source can use ordinary repository layouts while one current authority
owns path selection. Root placement requires explicit source/metadata ownership
and transactional recovery, rather than a path substitution. This changes public
adoption and relocation behavior. The directing user accepted it under ADR 0006
on 2026-11-03; implementation and qualification remain open. It does not reduce the
remaining Cargo, FreeBSD SDK or 1.4 qualification scope.
