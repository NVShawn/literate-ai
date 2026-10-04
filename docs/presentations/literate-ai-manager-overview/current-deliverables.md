# Current deliverables

The checked-in members below are the published **1.1.0 throughput and native-acceptance
edition**, the first edition from the public repository. Their stable Google Workspace IDs and links did not change. Both resources
were refreshed in place and export-back verified against these accepted local members.

- Native Google Slides (1.1.0 edition): [Literate-AI — Application Foundry Vision](https://docs.google.com/presentation/d/1zGugAIHdxXNSDKJia9jak55_0J0LnpSq2dFt2F5OZpE/edit?usp=drivesdk)
  — stable ID; 26 slides / 26 notes pages required on export-back.
- Native Google Doc (1.1.0 edition): [Literate-AI — Application Foundry Narrative](https://docs.google.com/document/d/1C6jtFrm9oAj6dg4CuLimylzu5HdaP6CovP2KY8U1HRA/edit?usp=drivesdk)
  — stable ID; 72 headings with no skipped level required on export-back.
- Generated PowerPoint:
  [literate-ai-manager-and-engineering-overview.pptx](literate-ai-manager-and-engineering-overview.pptx)
- Generated Word:
  [literate-ai-manager-and-engineering-overview.docx](literate-ai-manager-and-engineering-overview.docx)
- Related roadmap document: [HGX and Knowledge Framework roadmap](https://docs.google.com/document/d/1iinPBrxuP8YtGYsdGCwZ0vlQRgIzU_fCl-CcqnvGnPE/edit)

The roadmap document’s Executive Summary contains links to both the HGX executive decision
deck and this Literate-AI overview. The Slides file was updated in place, so that link
resolves to the current revision without change.

## Realized document-pair member

`python-pptx`'s ZIP container embeds a per-entry write timestamp, so re-running
`build_deck.py` reproduces byte-identical slide content but not necessarily the same
SHA-256. Trust the acceptance report for the digest of the exact committed artifact.

| Property | Value |
| --- | --- |
| Component | `component://literate-ai/literate-ai-overview` (terminal) |
| Capability | `literate-ai.document-pair` |
| Ecosystem | `google-workspace` |
| Edition | 1.1.0 (published in place and export-back verified, 2026-10-04) |
| Members | `presentation`, `narrative` |
| Local presentation | `docs/presentations/literate-ai-manager-overview/literate-ai-manager-and-engineering-overview.pptx` |
| Local narrative | `docs/presentations/literate-ai-manager-overview/literate-ai-manager-and-engineering-overview.docx` |
| Published presentation | Slides file `1zGugAIHdxXNSDKJia9jak55_0J0LnpSq2dFt2F5OZpE` |
| Published narrative | Doc file `1C6jtFrm9oAj6dg4CuLimylzu5HdaP6CovP2KY8U1HRA` |
| Slides | 26 local; 26 on export-back |
| Notes slides | 26 local; 26 on export-back |
| Narrative headings | 72 local, no skipped level; 72 required on export-back |
| Local presentation size / SHA-256 | 1,375,154 bytes / `33a0c0782858e7bd7691107c4fef945fdc65d2b7cd469e69fec2a3c6604dfdad` |
| Local narrative size / SHA-256 | 48,870 bytes / `a44535da53757a7ae1dc309f70cdaee30c8033a9a4b39d0c246b60bafaacb04f` |
| Geometry scan | 0 elements outside 1280 × 720; 0 overlapping text-bearing frames. Geometry-escape is not the overflow gate — `scripts/verify_document_pair.py` now checks both; `render_slides.py` prints an AABB overlap report for inspection. |

## Access record

| Field | Value |
| --- | --- |
| `audience` | `organization` |
| `principals` | none; granted by domain |
| `permission` | `view` |
| `link_sharing` | organization-restricted |

Realized as a Drive permission of `type=domain`, `domain=nvidia.com`, `role=reader`,
`allowFileDiscovery=false`, alongside the existing owner grant, on both members. This
matches the audience declared by the Component. No credential material is recorded here
or anywhere in the repository.
