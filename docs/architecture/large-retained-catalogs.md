# Explicit scale limits for retained catalogs

Issue [#234](https://github.com/jordanhubbard/literate-ai/issues/234) requires
complete retained src and ports trees to fit canonical catalogs. Raising global
limits would expand resource exposure for every project. Instead the current v2
project definition optionally declares an exact, versioned `scale_limits` object:

```json
"scale_limits": {
  "schema": "literate-ai/project-scale-limits@1",
  "pinned_input_files": 500000,
  "pinned_input_total_bytes": 1073741824,
  "catalog_files": 200000,
  "catalog_entries": 1000000,
  "catalog_total_bytes": 1073741824
}
```

This is project authority, included in the project identity and captured manifest.
It is not a command-line exemption or a worker environment override. Absent policy
preserves existing serialization and limits: 1,024 pinned files, 4,096 catalog
files, 16,384 traversed entries and 64 MiB aggregate bytes. A closure still reads
at most 2 MiB per file. Documentation, path, depth, symlink, retained ownership,
asset and provider bounds remain independent. Catalog Component and Flavor
traversals each retain their own budget as before.

All five limits are required integers; booleans and unknown fields are rejected.
File ceilings cannot exceed 1,000,000, entries cannot exceed 4,000,000, and either
aggregate-byte ceiling cannot exceed 4 GiB or be less than 2 MiB. Catalog entries
must cover the declared catalog file count. Small positive file/entry limits may
also tighten a project. Reaching a limit is valid; exceeding it refuses with a
named failure and observed/allowed values. Neither an enlarged file count nor an
enlarged byte budget disables the other bounds.

Catalog load, membership revalidation, lock planning, lock input capture and
project validation consume the same declared policy. Pinned-input forks retain
their budgets. Exact byte custody, changed-manifest refusal and independent
acceptance remain required. Running byte accounting avoids a quadratic rescan
for each admitted file and preserves duplicate-file accounting.

The legacy v1 reader and frozen v1 schema do not admit this authority. Current v2
projects opt in through an authored manifest change; older runtimes that reject
unknown fields require a framework upgrade before consuming it. No framework-wide
increase or silent downstream manifest migration is performed.

Synthetic large-catalog tests prove bounds and code paths, not native FreeBSD
qualification. Issue #234 remains open until a real retained FreeBSD tree has
measured load time, peak memory, lock resolution, project validation and
`litai verify`, with exact source and runtime evidence. Input ownership and test
receipt failures remain failures during that measurement.
