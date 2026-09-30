---
name: report-format
summary: Wire contract for the access-log tally report consumed by every deployment target
kind: interface
status: approved
---
# Report Format Boundary 1.0

This document is the named public boundary of the Access-Log Tally Component. It may
change under its own review without touching the behavioral requirements in
`component.md`; consumers depend on exactly these fields and this ordering.

The application writes exactly one JSON object with exactly these top-level fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `total_lines` | integer ≥ 0 | Input lines received |
| `parsed_lines` | integer ≥ 0 | Lines matching Common Log Format |
| `malformed_lines` | integer ≥ 0 | Lines rejected by the parser |
| `status_2xx`, `status_3xx`, `status_4xx`, `status_5xx` | integer ≥ 0 | Per-class status counts; classes absent from the log are `0` |
| `bytes_total` | integer ≥ 0 | Sum of byte fields; `-` counts as zero |
| `top_paths` | array of `{path: string, hits: integer}` | At most `top_count` entries, hits descending then ASCII path ascending |
| `report_version` | string `"1.0"` | Contract version marker |

Field order is significant for canonical comparison. No additional fields are
permitted; consumers reject reports with unknown members.
