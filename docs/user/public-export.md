# Public repository export

Run `make public-export-check` before copying this repository to a public location.
The check reads every tracked file and the embedded members of tracked Office
documents. It rejects non-public repository names, internal forge and tracker hosts,
employee addresses, private downstream project names, and organization-private model
or MCP provider identifiers. References to this repository itself and public NVIDIA
OSS, CUDA, documentation, and API endpoints remain allowed.

## Publish a clean snapshot, not the private object database

The existing Git object database contains removed internal hostnames and repository
paths on historical and non-default refs. Do not mirror it, push all refs, copy the
`.git` directory, or publish existing tags. A public repository starts from an export
of the audited tracked tree:

```sh
make public-export-check
git archive --format=tar HEAD > literate-ai-public.tar
mkdir literate-ai-public
tar -xf literate-ai-public.tar -C literate-ai-public
git -C literate-ai-public init -b main
git -C literate-ai-public add .
git -C literate-ai-public commit -m "Initial public release"
```

Create the destination repository, set its new public `origin`, and update
`literate.release.json` plus the generated Homebrew formula to that owner/repository
before publishing a release. Re-run `make public-export-check` in the exported tree.
The old private repository remains the historical authority; do not rewrite it merely
to create the public snapshot.

## Historical tags and existing installations

Earlier release tags are not published. Instead, each `v*` tag in the public repository is
an annotated tag on a parentless marker commit. The commit contains a README naming the
release, its date and original commit, plus its changelog section. Keep the original
tags locally outside `refs/tags` (for example `refs/archive/<old-remote>/tags/`) so
`git push --tags` cannot publish old history.

Declare the move in `literate_ai.repository_urls` so existing projects and self-update
follow it ([ADR 0048](../decisions/0048-repository-succession.md)). Existing users follow
[Moving from NVIDIA-dev/literate-ai](repository-migration.md).
