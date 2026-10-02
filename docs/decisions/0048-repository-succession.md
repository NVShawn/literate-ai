# ADR 0048: Continue installations across a declared repository move

- Status: Accepted
- Date: 2026-10-02
- Accepted: 2026-10-02 (repository owner direction)
- Decision owners: Literate AI maintainers
- Roadmap: REPO-MOVE-001 in `docs/roadmap/active-work.md`
- Extends: ADR 0037 prefix-installed CLI self-update

## Context

Literate AI moved from the internal `NVIDIA-dev/literate-ai` repository, now archived,
to the public `jordanhubbard/literate-ai` repository. The public repository starts from
a sanitized single-root snapshot; no earlier history or tags were copied.

Existing users carry the old location in three places:

- Projects record their initializing framework origin in
  `.literate/initialization-origin.json`. Update planning refuses an installed framework
  whose origin is a different repository (`project.update_origin_changed`).
- Self-update derives its release endpoint from the installed wheel's embedded origin.
  Releases 1.0.x instead hard-code
  `https://api.github.com/repos/NVIDIA-dev/literate-ai/releases/latest`.
- Contributor clones point at the old remote and hold the old tags.

Treating any two repositories as equivalent would weaken the origin check. Rewriting
recorded origins would destroy provenance.

## Decision

Declare repository moves explicitly in `literate_ai.repository_urls`. A move is a
predecessor-to-successor mapping between exact GitHub coordinates. The only declaration
is `github.com/NVIDIA-dev/literate-ai` to `github.com/jordanhubbard/literate-ai`.

- `repository_origin_continues(previous, upstream)` accepts an upstream that is the same
  repository, by existing transport-alias rules, or that resolves to the same current
  repository through declared moves. Project update planning uses it in place of plain
  equivalence. Different hosts, owners, repositories, ports and credentials stay
  distinct, and `repository_urls_equivalent` is unchanged.
- `current_repository_origin(url)` follows declared moves. Self-update resolves its
  release endpoint through it, so a build whose embedded origin is the predecessor
  checks the successor's releases.
- Recorded origins, initialization baselines and Standard pins are never rewritten.
  The project keeps its historical provenance; Standard rebind remains the reviewed
  path for any changed installed payload, as for every upgrade.

Releases 1.0.x cannot learn the successor. For enrolled prefix installs, the 1.1.0
release publishes one bridge: the identical 1.1.0 wheel is also attached to a `v1.1.0`
release on the predecessor repository, which is unarchived only for that publication.
1.0.x installs then self-update once to 1.1.0, whose embedded public origin directs
every later check to the successor. Pip and editable installs upgrade manually from
the successor's releases.

Historical tags `v0.1.0a1` through `v1.0.1` exist in the successor as annotated tags on
parentless marker commits. Each contains a README naming the release, its date and
original commit, plus its changelog section. They keep version history and changelog
links coherent without publishing pre-export source.

## Consequences

- Projects initialized from the predecessor plan updates with a successor-built CLI
  without manual edits.
- Self-update reaches the successor from either recorded origin.
- The bridge is a one-time release task for 1.1.0; later releases publish only to the
  successor.
- Adding another move requires an explicit declaration and review, never inference
  from names.
