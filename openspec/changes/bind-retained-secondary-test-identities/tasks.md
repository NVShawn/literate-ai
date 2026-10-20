## Implementation

- [x] Add strict numeric secondary identity and read-only POSIX currentness checks.
- [ ] Bind reviewed scope to worker/profile/action and actual provisioning authority.
- [ ] Implement descriptor-owned native permissions and exact cleanup.
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
- [ ] Complete integrated feature regression, installed qualification and review gates.
