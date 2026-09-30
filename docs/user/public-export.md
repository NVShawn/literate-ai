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
