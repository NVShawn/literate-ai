# Literate AI 0.11.0 (historical release marker)

Literate AI 0.11.0 was released on 2026-09-08 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `c41c2cbc2d88f93f715339dbb0361fd319e6de60`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.11.0 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.11.0 - 2026-09-07

- Treat generated `GET /health` HTTP 204 as ready for the durable split-service
  cross-process flow. Snapshot and page fetches still require HTTP 200. Live
  JavaScript servers commonly answer health with empty No Content.
- Bind LocalStandard cache hits to an external artifact checkpoint outside the
  candidate object so a coordinated rewrite of artifact bytes and the self-declared
  tree manifest cannot authenticate itself. Missing checkpoints rebuild in disposable
  custody; same-run and restarted hits remain observable when the sealed tree matches
  (#327).
- Make Standard npm discovery consume the selected `package-npm` Flavor toolchain
  constraint as the only version authority, matching the host SBOM `>=9,<13` range
  and binding that constraint identity into the npm toolchain. Missing or incomplete
  bounds fail closed instead of using a duplicated Python constant (#328).
- Reject a generated JavaScript single-file bundle at source handoff when local
  CommonJS specifiers omit their file extension, the `__litaiModules` map misses a
  reachable module, a wrapped module body embeds a shebang, or registry load never
  dispatches the exported CLI. Direct `node source/main.js --litai-test` is no longer
  treated as proof that the selected artifact works (#332).
- Map an independent persistent-service process that exits before readiness to
  `literate-ai/cli-error@1` with code `lifecycle.persistent-service.exited`, the
  acceptance phase, child status, contract identity, and bounded sanitized stderr
  instead of an unstructured traceback (#333).
- Prefix-installed `litai` (from `make install`) stages newer GitHub Release wheels in
  the background and, on the next invocation of that same prefix, installs the staged
  wheel and re-execs the original arguments (ADR 0037). Opt out with
  `LITAI_NO_SELF_UPDATE=1`. `litai update` remains project-file reconciliation.
