# Python wheel-lock admission draft

- **Status:** partial
- **Owning queue item:** [PYTHON-LOCK-001](active-work.md#python-lock-001-admit-complete-typed-python-dependency-locks)
- **Completion / archival evidence:** pending Standard lifecycle integration

Original issue: private downstream tracker item 1 (link intentionally omitted).
This is an unpublished implementation draft, not an enabled packaging Flavor.
Explicit wheel profiles now support an operator-provisioned offline Standard
build/test/execute path. Installed defaults remain unchanged; full framework and
native-platform qualification are not complete.

## Implemented review surface

`StandardPythonWheelCommandProfile` is a public typed/schema contract for an
explicit `pip` packaging selection using the `python` toolchain. It names one
requirements/static-pyproject manifest and adjacent `python-wheel-lock.json`.
The locked projector binds the packaging Flavor/profile, interpreter command and
identity, installer pin, and source paths in `StandardPythonTarget` and the
toolchain closure. It refuses competing build/packaging/compiler profiles,
libraries and multiple entrypoints before tool discovery. Dependency-free Python
and the shipped `package-pip` Flavor are unchanged; no wheel profile is enabled
in that Flavor or installed distribution by this patch.

Wheel-profile BUILD/TEST/EXECUTE commands use `-I -S -B`. The runtime driver adds
only the authored entrypoint directory and retained `python-runtime/site` to the
isolated interpreter's standard-library search paths. It does not process ambient
site-packages, startup hooks or `.pth` files. The same driver remains in-process
for service mode, preserving its package path and isolation. Lifecycle-owned
artifact verification is still mandatory before launch; the driver itself is not
an admission verifier. Runtime assembly requires an explicit provisioned wheel
directory and refuses to treat a wheel target as dependency-free Python.

`LocalStandardLifecyclePorts` now validates source authority before authorization,
requires offline resolve-before-compile actions, installs exact verified wheels,
retains the payload beside the export, and resolves the SBOM through a fresh
retained-payload observer. No network privilege is requested for this path.
Cache reuse first checks the complete artifact against its external publication
checkpoint, then revalidates dependency evidence and the selected interpreter.
TEST/EXECUTE and service command preparation recheck the sealed complete artifact
and retained dependencies before launch. A cache manifest cannot authorize itself.

The rebuild composition accepts an explicit `python_wheelhouse` argument or the
private operator setting `LITAI_PYTHON_WHEELHOUSE`, naming an absolute local
directory. Explicit arguments take precedence; non-wheel profiles ignore the
setting. The directory must contain each lock-named wheel and the pinned installer
wheel `pip-26.2.1-py3-none-any.whl`. It provides untrusted bytes, not package or
execution authority. Regular-file checks, exact hashes and owned staging still
apply. Provisioning, network acquisition and native runtime dependency validation
are separate from this offline path.

`adapters/dependencies/python_lock.py` parses a provisional
`literate-ai/python-wheel-lock@1` document. Its exact fields are `schema`,
`environment`, `tags`, `requirements`, and `packages`. The environment contains
all PEP 508 marker environment fields, and tags are the sorted expanded tags of
one independently observed target interpreter. Each package records `name`,
`version`, `filename`, `sha256`, `requires_python`, and `requires_dist`.

The parser rejects duplicate JSON fields, unknown fields, incompatible wheel
filenames, unsupported direct URLs, missing selected dependencies, version
conflicts, and unreachable packages. It computes parent edges from marker- and
extra-selected metadata, including cycles and newly activated extras.

The wheel verifier reads caller-owned streams without extraction or execution.
It verifies the archive SHA-256 and compares bounded, unambiguous METADATA with
the lock's package identity and dependency claims. `python_archive.py` additionally
checks portable paths, directory/file aliases, special files, archive expansion
bounds, WHEEL version/tags/build and complete RECORD payload hashes and sizes.
The current profile accepts wheel format 1.0, stored/deflated ZIP members and
SHA-256/384/512 RECORD digests. Native binary payloads are not prohibited. Signature
sidecars are covered by the outer digest but are not authenticated as signatures.
Each verified regular member produces its actual SHA-256 and byte count.
Full-closure verification also requires an exact wheel inventory and independent
target observations.
This is **not** a complete wheel installer or an installed-tree attestation.
The intended archive contract follows the
[PyPA wheel specification](https://packaging.python.org/en/latest/specifications/binary-distribution-format/).

`python_wheelhouse.py` copies supplied streams into an exclusively created private
temporary directory, verifies those copies, and removes them on normal exit or
failure. Its phase-boundary revalidation rejects changed bytes, extra/missing files,
symlinks/reparse points and hard-link aliases. Mutable source streams are not reused
as install inputs. This context owns staging only: it does not attest network
origin, choose an installer or authorize installation. A malicious same-user
process racing an external installer remains outside this helper's protection;
the lifecycle still owns process isolation and custody through installation.

`python_installed.py` observes a dedicated installation payload tree using an
explicit five-root installation scheme, rather than the framework interpreter's
ambient packages. It hashes every regular file, checks each installed RECORD,
compares unmodified members against verified wheel bytes, detects scheme/path
collisions and files outside the locked closure, and produces package import
aliases and graph edges. Native payloads and namespace imports are included.
Observation does not import installed code. A second tree snapshot and staged
wheel revalidation detect drift during observation.

Shared ordinary package files are permitted only when every locked wheel claims
the same unmodified source path, SHA-256 and size at the same installed path.
Each owner's installed RECORD must independently cover and hash that file. The
observed tree identity binds all package owners, and every package hash includes
its complete owned payload. Distribution metadata, relocated `.data` files,
within-wheel scheme aliases, transformed scripts and generated wrappers cannot
use this exception. Conflicting bytes still fail, even if installed RECORDs are
rewritten. This supports split namespace distributions without deleting files
from third-party archives or accepting last-writer-wins behavior.

Installer additions include regenerated RECORD, literal pip INSTALLER and empty
REQUESTED files. Wrappers and rewritten scripts now require identity-bound
projector output; extra files or wrapper bytes cannot authorize themselves through
an installed RECORD. An installation scheme argument alone is still not evidence
of which interpreter or installer process ran.

`python_target.py` now obtains platform markers and expanded wheel tags from a
selected `PythonToolchain` using a bounded `-I -S -B` probe. The existing toolchain
adapter revalidates launcher/runtime identity before and after execution. The
probe loads an owned copy of the framework's pinned packaging helper, not the
target's site-packages, and binds exact helper/driver bytes into its identity.
An empty target environment therefore needs no package installation to report
compatibility. The resulting target can require an exact lock match. It does not
by itself invoke an installer or capture native runtime dependencies.

`python_install.py` connects target observation, verified input staging and the
installed-tree observer through a fixed offline installer profile. It accepts
only the exact pip 26.2.1 wheel hash published in
[PyPI release metadata](https://pypi.org/pypi/pip/26.2.1/json). The private pip API
is deliberately bound to that version and digest, not treated as a stable API.
The framework's optional `python-wheel-installer` dependency declares the driver's
imports for source-BOM closure; ambient pip never becomes installer authority.

The owned `python_pip_driver.py` runs under the selected interpreter with isolated
startup and loads pip from verified wheel bytes. It invokes the wheel installer
directly: no resolver, network acquisition, source build, ambient configuration,
application import or bytecode generation. Per-package schemes remain inside a
private installation root. A separate process first generates the expected console
and GUI wrappers and script-header rewrites; those hashes are bound into projector
evidence and compared to actual installed bytes. Obsolete setuptools wrappers are
suppressed explicitly, and RECORD is normalized from the fixed ownership map.
The driver fixes the launcher ZIP timestamp for reproducibility. Revalidation
checks the selected target, wheel custody and installed payload, and context exit
cleans all private staging/install files. Network acquisition remains unimplemented;
Standard dispatch now invokes this helper after authorization without relaxing
the default source gate.

`lifecycle/standard_python.py` now retains a verified payload at the artifact
boundary together with build authorization, Component/source/command/profile,
target, installer, process and installed graph identities. Cache verification
requires the caller's independently sealed evidence identity; it never lets the
manifest authorize itself. It rehashes the complete file tree, checks POSIX
executable paths and binds the current source and reobserved target. Retention
refuses occupied reserved paths and cleans only newly created dependency paths
on failure. The enclosing Standard lifecycle authorizes, dispatches and seals
the offline build and passes the expected identity from trusted artifact custody.
`StandardPythonDependencyObserver` now re-observes the interpreter and verifies
retained evidence for the CycloneDX resolver on every call. The resolver accepts
explicit source authority for preflight only, and refuses post-build resolution
without this fresh lifecycle-owned evidence. It maps observed package coordinates
to source BOM references, verifies exact versions/edges and claimed import aliases,
and checks actual source imports against installed modules rather than distribution
names. Ambient Python observations cannot supplement the verified closure.
The public resolution port binds the installed-evidence identity; identity drift
or stripping fails validation. Standard build/cache dispatch constructs this
callback from independently sealed artifact custody.

`python_source.py` binds a profile-selected requirements or static pyproject
manifest to the lock's complete root requirements, including inactive marker
declarations. It retains both text digests and rejects duplicate or unselected
manifests, pip options/includes/editables, alternate package-manager authority,
and undeclared build/dynamic/optional/grouped dependencies. This binary-consumer
profile does not build the application as an sdist.

`_python_wheel_lock_projection` connects the parsed graph to the existing
source-BOM package and parent-edge coverage checker. It is inert intent, not
acquisition evidence. `_generated_lock_projection` now performs manifest/lock
preflight for an explicit wheel lock, but by default refuses a consistent pair with
`dependencies.python-acquisition-evidence-missing`. An explicit matching source
authority enables intent projection, not execution. Unlocked Python dependencies
retain the existing unsupported diagnostic. No caller can execute from a matching
manifest and lock alone.

## Required before enabling admission

1. Define a locked Python packaging command profile and target identity, binding
   the actual interpreter, installer, Flavor, platform, and source authority.
   Selected-interpreter observation and a fixed offline installer profile are
   implemented, and command-profile/target selection now binds them into the
   projected Standard toolchain closure. Connect this selection to authorized
   acquisition/build/cache dispatch and retained lifecycle process evidence.
   Decide whether the internal projection should ingest standard `pylock.toml`
   rather than expose the provisional JSON as a public lock format.
2. Extend the implemented single-root manifest reconciliation when the packaging
   profile needs additional dependency groups or build authority; do not silently
   accept them. The currently required binary-consumer roots are supported.
3. Integrate the owned wheel-staging context into controlled network acquisition,
   verify the same bytes
   subsequently installed, and prohibit ambient pip configuration or dependency
   resolution from changing the admitted graph. Archive-member, WHEEL and RECORD
   checks, installation-path mapping and collision rejection are implemented;
   controlled network acquisition, native runtime dependency observation and
   artifact-level credential/custody boundaries remain adapter work.
4. Install into a controlled environment using the selected interpreter. Observe
   the installed closure without importing third-party code, verify its graph,
   and derive distribution-to-import aliases and installed payload identities.
   Do not use the framework interpreter's site-packages as target evidence.
   The controlled installer and wrapper/script projection now pass real offline
   fixture installation; connect that evidence to Standard's build/cache lifecycle.
5. Retain source authority, archive identities, process observations, installed
   graph and tree identities with the artifact. Include all of them in cache
   keys, dependency resolution, packaging, and execution-admission revalidation.
   The retained-payload verifier is implemented; integrate it with Standard's
   sealing/checkpoint path rather than deriving trust from a cache manifest.
6. Exercise fresh build, cache hit, modified cached wheel/tree, target drift,
   undeclared imports, and execution admission using a portable two-wheel fixture
   on Linux and Windows before relaxing `dependencies.python-lock-unsupported`.

The offline dispatch/custody integration above now covers the local fixture
portion of items 1, 4 and 5. Next: qualify the production rebuild/source-cache/
receipt chain and actual native dependency closures on Linux and Windows. Network
acquisition is still separate adapter work; verified operator-provisioned wheels
do not require an automatic downloader. The shipped packaging Flavor and installed
framework remain unchanged pending review and qualification.

The unit tests deliberately assert that a valid parser document alone cannot
bypass the current gate. The implementation must not be described as resolving
the downstream Python application blocker until the above integration passes.

## Draft verification

- Windows portability repair: use full `lstat()` identity/link-count metadata
  rather than incomplete `DirEntry.stat()` fields, while retaining hardlink and
  reparse rejection. Wheel-profile authoring fixtures explicitly use LF. The
  patched Windows snapshot passes 48 affected tests with one POSIX FIFO skip;
  two independent metadata/hardlink regressions also pass. Integrated local
  tests pass all 50 cases without skips; changed-file lint/format checks pass.
- Broad gate at `4853ee67`: 5,007 tests in 5,312.101 seconds, one failure and
  29 skips. The sole observed failure was stale self-hosting driver identity.
  The reviewed identity was refreshed using the repository helper; all 12
  version-authority tests now pass. A fresh broad gate is still required.
- Current offline dispatch regression run: 374 tests, 372 passed and two existing
  platform skips. Nine new Standard lifecycle tests exercise real installation,
  build/test/execute, fresh-adapter cache reuse without reinstalling, missing
  checkpoint refusal, changed application/payload rejection, target drift, wrong
  wheel hashes, FIFO refusal and failed-build cleanup. A separate 50-test rebuild,
  CLI and qualification-adapter run passes, including operator configuration.
  Changed-file Ruff lint/format and diff checks pass. These local framework
  fixtures do not prove native GPU application acceptance or the full suite.

- The wheel-lock and existing dependency-lifecycle modules passed 82 unit tests
  in the Make-managed Python environment.
- The subsequent source-preflight integration passes 96 combined manifest,
  wheel-lock and dependency-lifecycle tests, plus changed-file Ruff checks. The
  broad gate below belongs to the earlier draft, not this new source revision.
- Archive and owned-staging coverage expands the focused suite to 118 tests,
  including payload drift with a matching outer digest, unsafe paths, malformed
  RECORD/WHEEL, native payload retention, mutable input isolation, changed staging
  files, symlink/hard-link rejection and cleanup on partial failure. Changed-file
  Ruff checks pass; full Standard installation and runtime admission remain open.
- Installed-tree verification expands the focused suite to 134 passing tests.
  This includes an actual offline pip install of two hashed fixture wheels into
  an isolated temporary payload directory, altered payload plus rewritten RECORD,
  extra/missing files, scheme collisions, escaping paths, symlinks, SHA-384/512
  records, native files and a Windows-style layout. The Windows-style fixture
  is not a Windows execution pass. Ruff lint/format and diff checks pass.
- Selected-target observation expands the focused suite to 141 passing tests.
  The probe passes against a clean environment without packaging installed,
  ignores injected Python startup hooks, rejects runtime/response drift, binds
  helper byte changes, and cleans its temporary helper on failures. These are
  local tests, not Windows or GPU application acceptance.
- Controlled installation expands the focused suite to 152 passing tests with no
  skips when `LITAI_TEST_PIP_WHEEL` names the exact pip 26.2.1 wheel. The integration
  class explicitly skips when that external fixture is absent; tests never download
  it themselves. Coverage includes console/GUI wrappers, script-header rewriting,
  obsolete wrapper removal, native payloads, independently repeated installation,
  wrapper tampering, unsafe entry points, collisions, installer hash mismatch,
  process failure and context cleanup. This is local framework evidence, not a
  Linux/Windows native dependency installation or desktop acceptance result.
- Retained artifact evidence expands the focused suite to 163 passing tests with
  the exact installer fixture supplied. It checks cache reuse after the installer
  workspace is gone, payload and executable-mode drift, changed authorization,
  source and target, injected files/symlinks, a rehashed malicious manifest,
  reserved-path preservation, bounded metadata and cleanup after a failed copy.
  Full Standard dispatch, SBOM resolution and execution-admission tests remain open.
- Source-to-resolved integration expands the focused suite to 175 passing tests
  with no skips, including real retained offline installation, arbitrary source
  BOM references, source/graph drift before acquisition, fresh verification on
  reuse, installed payload tampering, unsupported import aliases, distribution
  names that do not supply modules, ambient-observation refusal, and public port
  acceptance/identity-drift rejection. Another 22 application-port and CycloneDX
  tests pass. These are resolver-boundary results, not Standard dispatch or
  application execution acceptance.
- Command-profile/schema/runtime integration passes 148 tests with two existing
  platform skips (macOS HTTP/SSE service acceptance and Linux-only JavaScript
  service process ownership). Profile selection is exercised for Linux, Windows
  and macOS descriptions, not claimed as native execution on those platforms.
  Real local offline installation and isolated Python launch verify retained
  imports, ignored ambient startup paths, missing-payload refusal and in-process
  service-mode isolation. Dependency/public-port/SBOM regression rerun: 197 tests,
  no skips. Standard build/cache dispatch is still deliberately unavailable.
- Ruff lint and formatting checks passed for all eight changed Python files;
  `git diff --check` passed.
- `make python-check PYTHON_ENV=_build/python-envs/make-95155` passed compilation
  and host-path checks. The checkpoint recorded 3,291 completed cases, including
  the two-clean-state self-hosting replay and the new lock tests, before the agent
  interrupted the broad run during slow repository-ownership tests. This is an
  incomplete full gate, not a suite pass or release qualification.
- An earlier broad attempt was invalidated by concurrent source edits during
  self-hosting. The later run kept the source frozen and passed that replay.
