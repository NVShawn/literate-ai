# Open issue resolution

- **Status:** active
- **Owning queue item:** [ISSUE-RESOLUTION-001](active-work.md#issue-resolution-001-resolve-all-open-issues-with-verified-closure)
- **Completion / archival evidence:** pending until every live open issue is resolved and verified

The requested outcome is resolution of **all open issues**, not only the easiest fixes,
current PRs, or controller tests. The initial tracker set contains 54 issues. Re-query
GitHub at each integration and during the final audit; newly filed or reopened issues
join this scope. Each linked issue retains its complete original acceptance contract.

## Execution order

1. Remove the current critical/high documentation dependency findings without exceptions.
2. Repair the shared Windows/native qualification path: #116, #121 and #123. Reuse
   reviewed peer implementations, preserve failed attempts, and measure actual operations.
3. Complete installed retained SDK composition and observation performance (#118/#117),
   then the dependent original-source, native tree, platform, package and worker issues.
4. Resolve the remaining correctness, onboarding, catalog, UI, diagnostics and workflow
   issues against their own contracts while independent CI or native jobs run.
5. Integrate fixes through exact-head CI and synchronize main. Publish a release only
   where an issue explicitly requires it, using the release workflow and exact-main gates.

## Evidence and closure rules

- Audit each criterion against source, meaningful tests, rendered/runtime behavior,
  native records and landed commits at the scope requested. A test name, passing narrow
  fixture, declaration plan or mock does not prove a broader lifecycle requirement.
- Reuse existing proof only when the relevant code/authority identities and scope match.
  Retain timeouts and refusals. Keep compiler SDK, packaging and release admission distinct.
- Close an issue only after its full contract is proven. Record the criterion results,
  exact landed source and qualification before posting the closure. Wont-fix or duplicate
  dispositions need evidence and a real owner; neither is a shortcut around requested work.
- Completion requires a fresh tracker query with no unresolved open issues and a
  requirement-by-requirement audit of the resolution evidence. Keep the goal active until then.

## Live issue matrix

| Issue | Owning authority | Next evidence or action |
| --- | --- | --- |
| [#123](https://github.com/jordanhubbard/literate-ai/issues/123) Retain wheel-operation timings through the native Windows release gate | `WIN-GATE-001 (peer PR #124)` | Reviewed candidate exists in #124; native timing and integration acceptance remain. |
| [#121](https://github.com/jordanhubbard/literate-ai/issues/121) Avoid eager MAC schema parser initialization in ordinary project commands | `CLI-IMPORT-001 (peer PR #122)` | Reviewed candidate exists in #122; exact integration/native acceptance remains. |
| [#120](https://github.com/jordanhubbard/literate-ai/issues/120) Refresh DOMPurify pin for critical and high documentation advisories | `DOC-DEPENDENCIES-002` | Patched pin merged through #125 after green exact-head CI; native gate renewal is running on the integrated source. |
| [#118](https://github.com/jordanhubbard/literate-ai/issues/118) Compose captured retained runtime paths with native SDK command scopes | `RETAINED-SDK-PUBLIC-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#117](https://github.com/jordanhubbard/literate-ai/issues/117) Bound repeated installed-distribution observation during retained SDK execution | `SDK-OBSERVATION-COST-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#116](https://github.com/jordanhubbard/literate-ai/issues/116) Repair exact-main Windows toolchain and platform test contracts | `WINDOWS-TEST-CONTRACT-001` | Candidate #119 exact-head CI is green after retrying two pre-test OTP setup failures; native integration renewal remains. |
| [#113](https://github.com/jordanhubbard/literate-ai/issues/113) Audit missing agent entry points on authored original-source projects | `ENTRYPOINT-AUDIT-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#107](https://github.com/jordanhubbard/literate-ai/issues/107) Plan reviewed Standard first binding without discarding retained receipt policy | `STANDARD-FIRST-BIND-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#104](https://github.com/jordanhubbard/literate-ai/issues/104) Align Swift generation with the native single-executable source contract | `SWIFT-ENTRYPOINT-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#103](https://github.com/jordanhubbard/literate-ai/issues/103) Capture executable names and Python DLLs in Windows retained fixtures | `RETAINED-WIN-FIXTURE-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#102](https://github.com/jordanhubbard/literate-ai/issues/102) Bind native Windows system context in retained command workspaces | `NATIVE-SDK-ALIAS-001, RETAINED-WIN-CONTEXT-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#100](https://github.com/jordanhubbard/literate-ai/issues/100) Preserve explicit cache retirement during verified membership enumeration | `CACHE-RETIREMENT-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#98](https://github.com/jordanhubbard/literate-ai/issues/98) Keep complete native packaging fixture within its test dispatch grant | `NATIVE-PACKAGE-BUDGET-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#97](https://github.com/jordanhubbard/literate-ai/issues/97) Accepted-source build rejects verifier-admitted retained-source cache entries | `ACCEPTED-RETAINED-EXPORT-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#96](https://github.com/jordanhubbard/literate-ai/issues/96) Distinguish executable JavaScript bundle registries from inert writer text | `JAVASCRIPT-REGISTRY-LEX-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#95](https://github.com/jordanhubbard/literate-ai/issues/95) Make process-group termination fixture wait for child readiness | `NATIVE-SUPERVISOR-READY-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#94](https://github.com/jordanhubbard/literate-ai/issues/94) Delegate private allocation-prefix search in retained secondary views | `RETAINED-USER-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#93](https://github.com/jordanhubbard/literate-ai/issues/93) Preserve internal aliases through native SDK product admission | `NATIVE-SDK-ALIAS-001, NATIVE-TREE-WIRE-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#92](https://github.com/jordanhubbard/literate-ai/issues/92) Preflight retained workspace POSIX ACL support before input capture | `RETAINED-ACL-PREFLIGHT-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#91](https://github.com/jordanhubbard/literate-ai/issues/91) Preserve POSIX base interpreter linkage when creating the private runtime | `HOST-RUNTIME-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#90](https://github.com/jordanhubbard/literate-ai/issues/90) Bound Windows PE tool-dependency observation within receiver deadline | `NATIVE-SDK-ALIAS-001, WINDOWS-PE-OBSERVE-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#88](https://github.com/jordanhubbard/literate-ai/issues/88) Update mixes installed scaffold additions with an older pinned parent catalog | `UPDATE-CLOSURE-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#86](https://github.com/jordanhubbard/literate-ai/issues/86) Bound ancestor traversal cost in retained input verification | `INPUT-WALK-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#85](https://github.com/jordanhubbard/literate-ai/issues/85) Bind secondary test identities and scoped private-workspace access | `RETAINED-USER-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#84](https://github.com/jordanhubbard/literate-ai/issues/84) Bound strict SBOM validation cost for large retained inventories | `SBOM-SCALE-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#83](https://github.com/jordanhubbard/literate-ai/issues/83) Copy pinned catalog snapshots without cloning full repository history | `CATALOG-FETCH-001` | Resolved on main; 11 real-Git tests and exact integration CI pass. |
| [#80](https://github.com/jordanhubbard/literate-ai/issues/80) Bind tracker CLI commands to the selected repository in fork checkouts | `TRACKER-REPO-001` | Acceptance verified on landed source: five tracker tests cover exact repository routing and credential refusal across all emitted commands. |
| [#79](https://github.com/jordanhubbard/literate-ai/issues/79) Verify committed Component locks using their declared target and Flavors | `LOCK-TARGET-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#76](https://github.com/jordanhubbard/literate-ai/issues/76) Validate Python library build and export profiles before generation | `LIBRARY-PROFILE-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#75](https://github.com/jordanhubbard/literate-ai/issues/75) Normalize or safely reject Component file entry points in Standard rebuild | `REBUILD-ENTRY-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#74](https://github.com/jordanhubbard/literate-ai/issues/74) Support complete retained native trees as canonical Component catalogs | `RETAINED-CATALOG-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#70](https://github.com/jordanhubbard/literate-ai/issues/70) Do not require a coding agent for exact retained-source lifecycle execution | `RETAINED-ADMISSION-002` | Acceptance audit pending; preserve the linked issue contract. |
| [#69](https://github.com/jordanhubbard/literate-ai/issues/69) Honor worker SSH transport in SCP source and evidence transfers | `WORKER-SCP-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#67](https://github.com/jordanhubbard/literate-ai/issues/67) Add first-class FreeBSD worker, target and native lifecycle support | `FREEBSD-001, WINDOWS-PE-OBSERVE-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#65](https://github.com/jordanhubbard/literate-ai/issues/65) Support target-specific Python backend imports during retained-source admission | `PYTHON-TARGET-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#53](https://github.com/jordanhubbard/literate-ai/issues/53) Mermaid documentation check scans gitignored build output (generated/) | `MERMAID-SCOPE-001` | Acceptance verified on landed source: real Git ignored-file fixture, 21 tooling tests and 121 rendered diagrams. |
| [#52](https://github.com/jordanhubbard/literate-ai/issues/52) Distinguish credential literals from Python assignment expressions | `SOURCE-CREDENTIAL-001` | Acceptance verified on landed source: five inert inventory/derivation tests including mixed-secret adversarial controls; full source suite and CI pass. |
| [#51](https://github.com/jordanhubbard/literate-ai/issues/51) litai test --target host: generation returns empty source when the coding CLI refuses shell use | `GENERATION-REFUSAL-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#50](https://github.com/jordanhubbard/literate-ai/issues/50) litai render html graph canvas does not grow with its row | `HTML-CANVAS-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#49](https://github.com/jordanhubbard/literate-ai/issues/49) version-check HTML view can never be current at a clean commit | `HTML-VERSION-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#48](https://github.com/jordanhubbard/literate-ai/issues/48) Support reviewed updates to registered native child projects | `NATIVE-CHILD-UPDATE-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#47](https://github.com/jordanhubbard/literate-ai/issues/47) Support verified Python wheel dependencies for library Components | `PYTHON-LIBRARY-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#46](https://github.com/jordanhubbard/literate-ai/issues/46) Ensure project README links to its actual parent repository through a reusable skill | `README-PARENT-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#44](https://github.com/jordanhubbard/literate-ai/issues/44) Preserve unavailable NVIDIA GPU memory in POSIX worker observations | `WORKER-GPU-MEMORY-001` | Acceptance verified on landed source: real POSIX NVIDIA serializer fixtures and inspected Windows null handling; no physical GPU qualification claim. |
| [#43](https://github.com/jordanhubbard/literate-ai/issues/43) Add native Windows ARM64 host-install realization | `HOST-WINARM-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#27](https://github.com/jordanhubbard/literate-ai/issues/27) Repository refresh input checks re-walk gitlink inventory with thousands of git launches | `REFRESH-PERFORMANCE-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#24](https://github.com/jordanhubbard/literate-ai/issues/24) Remove the temporary braces audit exception once a patched release exists | `BRACES-AUDIT-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#22](https://github.com/jordanhubbard/literate-ai/issues/22) Qualify retained adopted projects through Standard lifecycle without transferring source authority | `RETAINED-PROJECT-001, RETAINED-FS-001, RETAINED-LOADER-001, ORIGINAL-ADOPT-001, RETAINED-SDK-PUBLIC-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#7](https://github.com/jordanhubbard/literate-ai/issues/7) Own project worktree placement and audit/remove legacy peer-directory worktrees | `WORKTREE-002` | Acceptance audit pending; preserve the linked issue contract. |
| [#6](https://github.com/jordanhubbard/literate-ai/issues/6) litai 1.1.0 rejects native-cli-application Components during project validation | `NATIVE-CLI-001` | Acceptance audit pending; preserve the linked issue contract. |
| [#5](https://github.com/jordanhubbard/literate-ai/issues/5) Dispatch native package construction and verification on compatible remote workers | `PACKAGE-APT-002` | Acceptance audit pending; preserve the linked issue contract. |
| [#4](https://github.com/jordanhubbard/literate-ai/issues/4) Compose production action DAG dispatch and qualify shared LAN caches | `RELEASE-INTEGRATION-003` | Acceptance audit pending; preserve the linked issue contract. |
| [#3](https://github.com/jordanhubbard/literate-ai/issues/3) Preserve SSH stderr and worker identity when worker probes fail | `WORKER-PROBE-DIAGNOSTICS-001` | Acceptance verified on landed source: seven managed worker-capability tests and exact integration CI; closure evidence is recorded in the owner. |
| [#2](https://github.com/jordanhubbard/literate-ai/issues/2) Complete three-way project update merging and reviewed resolution application | `UPDATE-MERGE-002` | Acceptance audit pending; preserve the linked issue contract. |
