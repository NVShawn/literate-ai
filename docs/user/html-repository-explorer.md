# Explore a repository in generated HTML

Literate AI includes a browser-facing repository explorer for its effective authority
graph. It turns the same canonical project/repository DAG returned by `litai graph` into
one inspectable `graph.html` file. The HTML is a derived view: JSON and project files
remain authority, and `litai verify` can detect when a declared view is stale.

The explorer is useful for a symposium, design review, or inheritance audit because it
shows Components, Flavors, skills, repositories, provenance and their canonical
relationships together. It supports catalog-type filtering, node selection, keyboard
navigation, focus, pan and zoom. Selecting a node shows its exact graph-bound Markdown
or JSON source excerpt when one exists.

## Render the explorer

Use an installed, non-editable Literate AI wheel. From the project root:

```console
litai render html \
  --project . \
  --surface authority-graph \
  --view dag \
  --view-version 1.0.0 \
  --output graph.html \
  --cache-mode read-write \
  --external-asset-policy pinned-cdn
```

Open `graph.html` directly in a browser. The command does not start a server or open a
browser. Interactive graph layout uses one exact SRI-pinned Cytoscape.js CDN asset;
the artifact retains a readable native catalog and source disclosures if JavaScript or
that asset is unavailable. `inline-only` intentionally refuses the interactive DAG
because it would forbid its declared graph library.

To make freshness part of ordinary project verification, declare the artifact in
`literate.project.json`:

```json
{
  "html_render_requests": [
    {
      "schema": "urn:literate-ai:schema:v1:html-observability-render-request",
      "surface_id": "authority-graph",
      "view": {
        "schema": "urn:literate-ai:schema:v1:html-observability-view",
        "view_id": "dag",
        "view_version": "1.0.0",
        "scope_kind": "project",
        "scope_identifier": "my-project"
      },
      "output_path": "graph.html",
      "cache_mode": "read-write",
      "external_asset_policy": "pinned-cdn"
    }
  ]
}
```

Replace `my-project` with the project's declared ID, render the file, then check it:

```console
litai verify --gate html-observability .
```

Verification does not regenerate or repair anything. It reopens the artifact, its
embedded provenance, current source bindings and installed renderer and refuses missing,
edited, unpinned or stale output.

## What the metadata proves

Every generated view binds:

- the named surface, view version and project or Component scope;
- ordered canonical input identities;
- the exact framework distribution, renderer and template;
- the observation time and declared external assets; and
- the completed file identity in the enclosing artifact record.

The completed file digest cannot be embedded inside the bytes it hashes, so it lives in
the enclosing artifact record. The embedded provenance instead binds the render-input
identity. This is intentional and is checked during staleness verification.

## Repository exploration is not arbitrary code browsing

The built-in explorer follows the Literate AI authority graph; it is not an IDE or a
general filesystem browser. Source previews are available only for identity-bound local
`.md` and `.json` files named by graph nodes. The renderer verifies the complete file
before displaying a bounded excerpt and refuses unsafe paths, links, changed bytes,
oversized files and invalid UTF-8. It does not expose arbitrary source files, symbol
search, go-to-definition or reference navigation.

Literate AI's specification-to-source map is a separate debugging surface. Running
`litai --debug generate ...` or `litai --debug rebuild ...` asks generated code to carry
`litai:spec PATH:LINE` anchors, writes `source/.literate/spec-map.json`, and reports map
events on stderr. Those maps are not currently projected into `graph.html`; describing
the HTML explorer as a source-code browser would therefore overstate the implementation.

## Other HTML views and multi-project composition

The same renderer supports `version-check/health`, `lock-health/health`,
`verification-health/health`, `performance-history/history`, and
`workflow-routing/catalog`. The health views and authority graph have retained installed
qualification. Performance and workflow views are implemented, but their dedicated
installed/browser acceptance remains incomplete.

Already-rendered, declared artifacts from several projects can be composed into an
offline shell:

```console
litai render dashboard --root /work \
  --project /work/service-a \
  --project /work/service-b \
  --output observability.html
```

The shell verifies and binds every pane. Cross-project selection and filtering controls
are not implemented yet, so the current dashboard is a static pane composition rather
than a complete fleet explorer.

For the wire and trust boundaries, see
[HTML observability: derived views over canonical JSON](../architecture/html-observability.md).
For every CLI flag and manifest rule, see
[Configuration and CLI](configuration-and-cli.md#three-verbs-three-levels-of-intrusiveness).
