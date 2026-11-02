# Windows host Flavor

### Requirement: Exact Windows ARM64 host realization

Windows ARM64 host installation SHALL select its own authored realization and
SHALL NOT substitute the x86-64 realization. Acquisition SHALL explicitly request
ARM64 packages and bind managed artifacts to their reviewed ARM64 bytes. Existing
ready capabilities SHALL remain unchanged. Unsupported providers or required
capabilities SHALL refuse rather than select another architecture or provider.

#### Scenario: An ARM64 worker already has its required tools

- **WHEN** the exact Windows ARM64 realization observes every selected capability and its required package provenance
- **THEN** readiness succeeds without installation or provider replacement
- **AND** current bounded PE-machine observations cover the selected executable, loaded Python DLL and declared managed-agent companions
- **AND** native worker execution and complete loader/ABI qualification remain separate evidence

#### Scenario: A required ARM64 tool or installer is unavailable

- **WHEN** a selected capability is absent or its ARM64 installer is unavailable
- **THEN** preflight or the authorized acquisition reports the missing requirement
- **AND** readiness does not succeed through the x86-64 catalog or an emulated replacement

#### Scenario: The selected coding provider has no reviewed ARM64 artifact

- **WHEN** the operator explicitly selects an unrealized provider
- **THEN** preflight refuses before probing or acquiring a different provider

#### Scenario: An existing tool has an unverified native execution chain

- **WHEN** a selected image or runtime companion is foreign, missing or unverified, or only a script launcher is available without a reviewed runtime binding
- **THEN** readiness refuses and preserves the existing tool before acquisition
- **AND** version output alone never establishes native readiness

#### Scenario: Native ARM64 baseline runtime coexists with x64 companions

- **WHEN** the ARM64 runtime baseline is observed
- **THEN** `MSVCP140.dll` and `VCRUNTIME140.dll` must both exist and pass native PE custody checks
- **AND** a missing or foreign selected DLL refuses readiness
- **AND** an unrelated `VCRUNTIME140_1.dll` is outside this ARM64 baseline probe
- **AND** the x86-64 three-DLL baseline and application-specific dependency checks remain unchanged

#### Scenario: NPM is selected on ARM64

- **WHEN** a selected Flavor requires NPM
- **THEN** capability closure refuses its explicitly unavailable realization before acquisition
- **AND** selecting the Node-only JavaScript language Flavor remains supported

### Requirement: Portable Windows host execution

The generated application SHALL use the selected language's portable APIs for
drive-aware paths, temporary storage, UTF-8 text, and process-independent computation
on Windows. It SHALL NOT require POSIX-only paths, signals, shell commands, or a
Windows-only dependency unless the application specification explicitly requires one.

#### Scenario: Generated application runs on Windows

- **WHEN** the `platform.os=windows` Flavor and one implementation-language Flavor are selected and the application is compiled by that host toolchain
- **THEN** the compiled entrypoint completes its acceptance scenario with drive-aware paths and portable UTF-8 behavior

### Requirement: Remote-worker clock synchronization

When this Flavor is selected for a remote worker, host configuration SHALL attempt to
synchronize the host clock to the public NTP source `time.nist.gov`. The attempt is
best-effort: a missing client, missing noninteractive privilege, blocked UDP/123, or
nonzero client exit SHALL be recorded in host-bootstrap evidence and SHALL NOT fail
sample-worker capability readiness. Skewed clocks are a frequent cause of coding-agent
TLS and token failures.

#### Scenario: Host configuration attempts NTP synchronization

- **WHEN** the sample-worker host bootstrap runs on a Windows remote worker
- **THEN** it attempts an argv-bounded `w32tm /resync` and, if that fails, configures
  `time.nist.gov` as the manual peer list and resyncs again
- **AND** a failed or skipped attempt is present in the bootstrap report's `clock_sync`
  object without making `passed` false solely for that reason
