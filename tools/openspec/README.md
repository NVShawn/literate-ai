# Repository documentation and OpenSpec tooling

This directory isolates the repository-pinned OpenSpec and Mermaid CLIs, Puppeteer, and
their npm dependency graph. The tools validate behavioral specifications and render every
Mermaid fence through one sandboxed browser session; they are not part of the Python
framework package and are not required to install, import, or run the kernel.

Contributor tooling requires Node.js 22.12 or newer (CI uses Node 24). Puppeteer
25 removes the vulnerable `extract-zip` and js-yaml dependency chains from the
lock. These are documentation-tool requirements, not changes
to generated applications' selected runtimes. Keep browser sandbox defaults enabled.

`make documentation-check` first audits the staged locked dependency graph. High or
critical findings, or an unavailable audit service, fail the gate; do not substitute
`--omit=dev`, because these development dependencies are the tooling being qualified.
The only allowance is `audit-exceptions.json`: each entry names one advisory and
package, with a reason, tracking issue and expiry date. Expired entries and entries that
no longer match a finding also fail the gate.
The gate needs access to the configured npm registry's audit endpoint. Parser and
rendering checks still run after a passing audit.

Install and run it from the repository root:

```bash
make tools-install
make openspec-check
make documentation-check
```
