# FreeBSD pkg provider

### Requirement: Bind native package intent

The provider SHALL bind the exact accepted package plan, selected FreeBSD ABI,
native pkg tool, complete installed-file projection, explicit dependency
coordinates and finite archive limits. It SHALL preserve authored specifications
and both source and resolved BOMs. Missing accepted input bytes or dependency
coordinates, unsupported targets and source-authority substitution SHALL refuse.

#### Scenario: A target dependency cannot be resolved

- **WHEN** an external runtime requirement lacks one accepted native package coordinate
- **THEN** planning refuses without consulting or installing ambient packages

### Requirement: Construct without modifying the host

Construction SHALL require a matching FreeBSD worker and captured native tool,
use isolated staging and fixed timestamps/compression, and preserve planned file
paths, root ownership and executable modes. It SHALL disable native tool plugins,
use explicit configuration, bound process output/time and execute no install
hooks, package installation or publication.
For the native pkg 2.8 encoding, both header owner names SHALL be explicitly
root/wheel. Manifest entries SHALL use numeric IDs zero and zero timestamps;
payload entries SHALL use reproducible numeric IDs 65534 and the planned fixed
timestamp. Numeric IDs SHALL NOT replace installation owner-name authority.

#### Scenario: The native tool changes

- **WHEN** the bound executable or ABI changes after planning
- **THEN** construction refuses and produces no accepted result

### Requirement: Independently inspect native bytes

Verification SHALL inspect native manifests and the complete archive without
extracting or executing it. It SHALL check exact dependencies, provenance,
canonical file ownership/modes/timestamps, bounded member sizes, native file
checksums and accepted input digests. Unsafe paths, duplicate or extra members,
hooks, links, special files and rehashed payload/manifest tampering SHALL refuse.
Extended tar metadata SHALL NOT add ACLs, file flags, extended attributes or
other unplanned installation semantics. Standard PAX encodings SHALL reproduce
the exact accepted member path, size, timestamp and root ownership.
Native required/provided shared-library metadata SHALL match an independent,
bounded inspection of DT_NEEDED and DT_SONAME in the accepted ELF payloads.
Libraries provided inside the package SHALL NOT become external requirements.
Malformed or unsupported ELF targets, extra/missing library names and duplicate
native library metadata SHALL refuse without extracting or executing target code.

#### Scenario: The archive is changed and rehashed

- **WHEN** native metadata or a payload member differs despite a matching new outer digest
- **THEN** independent verification rejects the package

#### Scenario: A rehashed archive adds file flags through PAX

- **WHEN** an archive retains accepted payload bytes and normal ownership/modes but adds extended file flags, ACLs or attributes
- **THEN** independent verification rejects the unplanned metadata

#### Scenario: A package substitutes its native shared-library requirements

- **WHEN** the native manifests change required or provided library names while retaining accepted ELF bytes and a new matching outer digest
- **THEN** independent verification rejects the metadata mismatch
