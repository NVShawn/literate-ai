## Implementation

- [x] Add strict numeric secondary identity and read-only POSIX currentness checks.
- [ ] Bind reviewed scope to worker/profile/action and actual provisioning authority.
- [x] Add the descriptor ACL backend, mask-safe projection and exact restoration;
  13 focused native checks pass with one unsupported-platform skip.
- [ ] Bind complete anchored path/resource scope and cleanup through the workspace owner.
- [ ] Integrate command/completion/receipt evidence without changing legacy defaults.

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
- [ ] Complete integrated feature regression, installed qualification and review gates.
