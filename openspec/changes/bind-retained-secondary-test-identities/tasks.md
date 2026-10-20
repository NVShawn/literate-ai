## Implementation

- [x] Add strict numeric secondary identity and read-only POSIX currentness checks.
- [x] Add optional worker/account/test-action/view/fixture policy to the exact
  profile identity, preserving absent-field serialization. Validate manifest alias
  closure and refuse execution without an enforcing provisioning owner.
- [ ] Bind reviewed scope to worker/profile/action and actual provisioning authority.
- [x] Add the descriptor ACL backend, mask-safe projection and exact restoration;
  13 focused native checks pass with one unsupported-platform skip.
- [ ] Bind declared readonly input/export views, internal aliases and fresh fixtures through the workspace owner.
- [x] Implement the separate FreeBSD view owner: private reservation, bounded
  no-follow path custody, effective mount flags, eligible test grants, fresh
  fixtures and private-root restoration before unmounting. Ten source-level
  native checks pass with no skips, including real busy mounts, writable-view
  refusal, source/root replacement and exact normal/exception cleanup. Actual
  provisioning and installed workspace integration remain open.
- [ ] Integrate actual private provisioning authority and current instance checks; reject configuration-only claims.
- [x] Add a separate optional private inspection command and a non-deserializable
  owner with fresh challenges, instance/prefix/lifetime currentness and permanent
  revocation. Real child-process fixtures cover protocol refusals; actual native
  allocation and workspace integration remain open.
- [x] Integrate lower-level workspace command scoping: typed allocation and
  allocated-parent checks before capture, explicit reviewed argument bindings,
  sealed post-build exports, eligible command activation/revocation and failed-run
  retention. Completion/receipt integration and operator-private orchestration
  are now implemented. The source-level native command case passes; installed
  qualification is tracked separately.
- [ ] Bind source launcher view paths and actual root/unprivileged dependency observations.
- [x] Integrate cleanup-bound completion/receipt evidence and operator-private
  local orchestration without changing absent-policy defaults. Installed
  qualification, actual provisioner inspection and original-suite coverage remain
  separate open gates.

## Verification

- [x] Cover invalid/root IDs, group drift, reverse aliases, changing observations,
  missing accounts, unsupported platforms and an actual local account lookup.
- [x] Run actual UID/GID 2001 observations on the disposable guest without
  changing its account or the system tests account. The source-level probe
  reports eight passes and one root-account skip; installed integration is open.
- [ ] Execute complete privileged and unprivileged original cases through the
  installed framework, including denial, drift and cleanup failures.
- [x] Complete identity-prerequisite broad regression: 1,874 passed, 42 skipped,
  no failures across 1,916 checks in 1,039.060 seconds.
- [x] Complete descriptor-backend full regression: 1,879 passed, 51 skipped,
  no failures across 1,930 selected checks in 1,015.463 seconds.
- [x] Complete policy-stage retained regression: 277 passed, 11 skipped, no
  failures across 288 selected checks in 219.171 seconds. This predates the
  inspection owner and does not qualify runtime integration.
- [x] Complete policy/inspection retained regression: 288 passed, 11 skipped,
  no failures across 299 selected checks in 228.360 seconds. Three existing
  provisioning smoke checks also pass. Actual allocation and view execution are
  still unqualified.
- [x] Complete view-owner retained regression: 290 passed, 19 skipped, no failures
  across 309 selected checks in 301.541 seconds. This checks the source backend;
  actual provisioning and installed execution remain open.
- [x] Complete command-workspace retained regression: 297 passed, 20 skipped,
  no failures across 317 selected checks in 263.107 seconds. This includes unknown
  paths, allocation mismatch, command exceptions, constructor failure retention and
  revocation failure after a successful command. It does not qualify installed
  allocation, original native suites or receipts.
- [x] Run the real native command-workspace case: one pass, no skips,
  224.210 seconds. Actual UID/GID 2001 reads the views, writes/removes its fixture
  and cannot read private inputs; root writes to the view fail. Final audit finds
  zero nullfs mounts and unchanged account IDs. The inspector is a protocol fixture.
  Preserve the earlier 11-case run with ten passes and one command error; the native
  fixture had created its delegated export directory as private mode 0700. The
  corrected fixture explicitly creates that export directory with mode 0755.
- [x] Pass 15 receipt checks, six evidence-contract checks and 21 local
  orchestration checks. Cover failed retention/cleanup, changed plans, corrupt
  evidence, exact event history, private inspector selection and input overlap.
- [x] Pass combined retained regression: 311 passed, 21 skipped, no failures
  across 332 selected in 271.326 seconds. This is not the complete framework suite.
- [x] Pass the real native receipt fixture without skips in 760.742 seconds:
  secondary operations, independent acceptance, cleanup, retained admission and
  materialized payload after run retirement. Audit confirms zero nullfs mounts
  and unchanged account IDs. Inspector is a protocol fixture; installed CLI and
  original source-suite qualification remain open.
- [x] Declare and repair the missing FreeBSD SQLite capability through existing
  Flavor host-bootstrap/package owners; twelve host-install checks pass. Retain
  the original native import failure and failed catalog validations.
- [x] Recheck account identity after private content/dependency verification,
  including after cleanup. A late account change refuses admission. The final
  retained regression passes 312 checks with 21 skips across 333 selected in
  275.180 seconds. Native renewal exercises local orchestration: eleven passes,
  no skips in 797.989 seconds. It still uses a protocol-peer allocation inspector.
- [ ] Complete integrated feature regression, installed qualification and review gates.
