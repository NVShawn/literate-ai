# FreeBSD host Flavor

### Requirement: Native FreeBSD execution

The application SHALL use portable filesystem, temporary-storage and UTF-8 APIs
on FreeBSD unless its own specification requires native interfaces. Linux procfs,
GNU utilities and Linux ABI compatibility SHALL NOT be presumed available.
Native extensions SHALL target the selected FreeBSD architecture and userland.
GNU Make requirements SHALL resolve to `gmake`, distinct from the base BSD make.
Native installation SHALL read the FreeBSD `pkg` realization for the exact supported
architecture. Package provenance and behavioral probes must both pass. Coding-agent
providers without a native realization remain unavailable; Linux agent binaries
SHALL NOT substitute for native support. A missing or older package remains missing
until an authorized declared installation provides the required version.

#### Scenario: Physical or virtual FreeBSD worker

- **WHEN** `platform.os=freebsd` is selected for a physical machine or a guest
- **THEN** build and acceptance execute in that FreeBSD environment
- **AND** the virtualization host's OS does not substitute for guest identity

### Requirement: Truthful native capability evidence

Worker observations SHALL use named bounded FreeBSD queries. An unsupported GPU,
quota, paging or pressure observation SHALL remain explicitly unsupported or
unavailable and SHALL NOT become proof of capacity or successful acceptance.
Hardware acceptance SHALL require the actual target hardware where specified.

Native ELF dependency evidence SHALL bind the FreeBSD inspector, hints, library
maps and selected library bytes. Inspection SHALL NOT execute target binaries.
Unsupported loader controls and ambiguous mappings SHALL refuse qualification;
Linux loader-cache semantics SHALL NOT substitute for FreeBSD loader evidence.

#### Scenario: Unsupported capability

- **WHEN** a required native observation cannot be measured
- **THEN** its status states that limitation and the requiring gate cannot claim
  qualification from a fabricated successful measurement
