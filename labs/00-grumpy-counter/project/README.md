# Grumpy Counter — canonical lab project

Welcome aboard, Unprompted AI. This project inherits its local catalog from
[Literate AI 1.3.1](https://github.com/jordanhubbard/literate-ai/releases/tag/v1.3.1),
source revision `87506c804210c46835723e11d1ca1c6cae0f496c`.
All selected authority is included here; no earlier lab or sibling repository is
needed to resolve it. Use the installed 1.3.1 wheel and browser extra from the
conference attendee setup guide.

## Build and run

From this directory:

```sh
litai doctor
litai status
litai build --update-receipt
litai run components/frontend-application/grumpy-counter -- --litai-serve 8877
```

Open `http://127.0.0.1:8877/`; Ctrl+C stops the server. The first build can invoke
your configured assistant. For exact already-admitted source, use
`litai build --from-accepted-source`; it refuses missing/stale membership instead
of generating replacement code. The explicit server arguments are passed to the
lab executable by the harness.

## Inspect and change authority

Read [the Component](components/frontend-application/grumpy-counter/component.md),
[its lock](components/frontend-application/grumpy-counter/component.lock.json), and
[the project manifest](literate.project.json). The initial selection is JavaScript,
GNU Make and macOS. The local catalog also contains Linux and Windows Flavors.
On another host, replace the OS selector in `default_flavor_selectors` in the
project manifest with `+flavor://literate-ai/os-linux` or
`+flavor://literate-ai/os-windows`, then run:

```sh
litai lock components/frontend-application/grumpy-counter
litai plan components/frontend-application/grumpy-counter
litai build --update-receipt
```

Review the changed lock and plan. Changing a Flavor changes authority and requires
a new native build and acceptance; the old host receipt does not qualify the new
host. WSL uses the Linux Flavor. Changing a spec similarly requires relocking,
planning, rebuilding and checking the resulting receipt.

## Initial authored source

`retained-input/` contains the explicitly authored initial implementation, current
source SBOM and test declarations. These are **untrusted inputs**, not a fabricated
generator run. To qualify this exact seed on its matching locked host without a
model call, inspect the public plan:

```sh
litai rebuild components/frontend-application/grumpy-counter --retained-source retained-input --retained-source-plan
```

Review its identities and execution scope. Substitute its actual
`authorization_identity` below after review:

```sh
litai rebuild components/frontend-application/grumpy-counter --retained-source retained-input --authorize-retained-source sha256:REPLACE_WITH_REVIEWED_IDENTITY --allow-host-execution --update-receipt
litai build --from-accepted-source
litai run components/frontend-application/grumpy-counter -- --litai-serve 8877
```

This executes generated/authored host code. The acknowledgement is explicit;
writing a source manifest, test declaration or SBOM alone grants no acceptance.
After a spec or Flavor change, the old retained seed metadata is stale. Use the
normal assistant-backed `litai build`, or re-author and review the seed rather
than copying an old receipt. Only the harness writes `verification/current.json`.
The independent browser contract is verifier-owned under `verification/acceptance/`.

This project currently has one application Component with three selected Flavors.
It does not claim that its internal drawing/audio roles are separate Components.
Progressive multi-Component curriculum work remains tracked by LAB-AUTHORITY-001.

<!-- literate-ai:parent-repositories -->
- Parent repository: [github.com/jordanhubbard/literate-ai](<https://github.com/jordanhubbard/literate-ai>)
<!-- /literate-ai:parent-repositories -->
