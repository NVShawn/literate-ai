# Native FreeBSD installed SDK qualification

Use the **Native FreeBSD SDK qualification** workflow to renew the 1.3.1 patch
program's installed public retained-SDK acceptance on an actual FreeBSD 14.4
guest. It builds a wheel from one exact commit, installs it non-editably and runs
the consumer as an unprivileged account in a disposable VM on hosted Linux.
No configured FreeBSD worker is currently available; this is the platform cell
that the aligned worker fleet cannot cover.

The original 14.3 guest could not install current packages: that release
[reached end of life on June 30, 2026](https://lists.freebsd.org/archives/freebsd-announce/2026-June/000277.html),
and its userland is older than the package repository requires. The workflow uses
14.4 with normal package OS-version checks. Results from it provide 14.4 evidence;
the failed 14.3 bootstrap attempts provide no SDK acceptance evidence.

## Run and inspect

Push the candidate first, then supply its complete 40-character commit ID:

```bash
gh workflow run native-sdk-qualification.yml --ref main \
  -f candidate_revision=FULL_40_CHARACTER_COMMIT_ID
```

The workflow validates the selector before building. Its wheel metadata binds the
candidate revision, wheel hash, test-source archive hash and qualification-harness
revision separately. The installed framework must report that same candidate.

Inspect the run and download its `freebsd-native-sdk-qualification` artifact:

```bash
gh run view RUN_ID
gh run download RUN_ID --name freebsd-native-sdk-qualification
```

`result.json` is the final scoped verdict; stage stdout, stderr and terminal
records explain failures. A timeout retains partial streams and a failed terminal
record. An unfinished or failed run provides diagnostic evidence only.

## What a passing result proves

- The installed public CLI initializes an owned consumer using the published
  `v1.3.0` parent, without source imports or tool-bootstrap side effects.
- Fresh loopback SSH credentials authenticate acquisition of the exact Git SDK
  fixture. A real CMake producer builds its C ABI and Python binding; source,
  license, selection and snapshot identities remain bound to the consumer.
- Public retained execution refuses both missing authorizations, then completes
  authorized build, three test cases, documentation and independent acceptance
  with withheld product operands and invalid operands.
- The accepted executable needs the reviewed SDK loader context: running without
  it fails rather than silently using an ambient library.
- Forty installed owner controls also pass without skips. Those controls may use
  fixture injection; the separate canonical public-CLI scenario uses no discovery,
  transport, producer, command or acceptance mocks. Every imported production
  module must remain inside the non-editable installed prefix.

The host compiler and linker are observed bootstrap tools. This microfixture does
not admit a source-built compiler SDK, a complete operating system, a product
release or a software release. The consumer's original authority and full receipt
policy remain intact; the retained receipt is not release authorization.

The guest checks storage before package installation and execution. Fresh SSH
private keys stay in the disposable guest's private home; artifact upload copies
only the explicit report/log whitelist. It does not alter a host's SSH settings.

Development source acceptance can close its narrowly evidenced roadmap work.
Final prepared-candidate lifecycle, package and native release qualification
remain mandatory in the [1.3.1 execution checklist](../roadmap/1.3.1-release-execution.md).
