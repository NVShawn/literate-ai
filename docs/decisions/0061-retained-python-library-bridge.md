# ADR 0061: Bind Qualified Python Libraries Into Retained Test Workspaces

- Status: Proposed
- Date: 2026-11-04
- Decision owners: literate-ai maintainers
- Release target: 1.4.0
- Roadmap: [RETAINED-PYTHON-BRIDGE-001](../roadmap/active-work.md#retained-python-bridge-001-consume-qualified-native-library-children-from-retained-python-projects)
- GitHub issue: [#307](https://github.com/jordanhubbard/literate-ai/issues/307)
- Approval: pending explicit human acceptance of this design

## Context

A retained Python project can qualify a provider as a native library Component,
yet its retained test workspace contains only the implementation-relative harness
source inventory. Qualified children and their committed caches are outside that
inventory. Looking up the original project or an ambient cache from the disposable
workspace cannot provide a reproducible sealed dependency. Copying generated source
into authored implementation directories would create a second source authority.

[ADR 0040](0040-retained-cargo-library-bridge.md) establishes reviewed importer
bindings, current provider qualification and transactional materialization for
Cargo. It explicitly leaves Python consumers to another decision. Existing library
products already describe Python import surfaces and exact sealed exports. The
missing behavior is their integration into retained Python execution and receipts.

## Decision

### Bind the provider and consumer explicitly

Add a versioned Python workspace plan using the existing retained-library binding
and closed artifact/evidence graph wherever their semantics already apply. Keep
existing Cargo schemas, defaults and admission unchanged. A plan binds:

- The importing project and current retained wrapper, harness inventory, complete
  test inventory and source scope, including the prospective source-retirement delta.
- Every provider's Component/interface/import-surface identities, exact qualified
  Python export, target/interpreter requirements, transitive package closure,
  qualification evidence, current policy/verifier and reviewed importer trust.
- Package/module names, exact import roots within the disposable workspace,
  consumer packaging metadata and dependency lock identities where applicable,
  and the full admitted build/test commands and runtime environment projection.
- Explicit artifact sources and finite byte, entry, package and path budgets.

A committed-cache directory is an artifact location, not qualification evidence.
Reopen the existing qualification and source-promotion evidence under current
custody before using its export. A digest or decoded success field alone cannot
establish current admission. No host path, environment variable, package name or
mutable version range may silently select a provider.

### Materialize inside the execution seal

Compose a complete disposable execution workspace from two separately attributed
input classes: the retained authored source inventory and verified library exports.
Library inputs live under short declared private package roots owned by that
attempt, outside the authored implementation tree. Their identities and import
mapping participate in the workspace, execution request and receipt identities.
They must remain included when the receipt is reopened or promoted.

Materialize the exact export bytes through existing bounded archive and filesystem
custody owners. Preserve modes; reject traversal, links or reparse points not
supported by the export contract, duplicate/case-colliding destinations, namespace
overlap and conflicting package roots. Reserve and publish only attempt-owned
destinations. Recheck provider and consumer inputs before execution and before
admission; preserve foreign files and concurrent changes on failure.

Use the selected Python runtime and the framework-owned isolated launcher to add
only these verified roots and the declared retained execution roots. Retain the
standard library. Do not enable ambient site packages, user-site initialization,
unreviewed `.pth` execution, an inherited `PYTHONPATH`, editable installs or an
escape to the original project/cache. Validate actual imported module origins
against the selected export roots during the real consumer journey. Reject
ambiguous namespace-package contributions and module shadowing unless the exact
combined namespace is explicitly bound and verified.

The adapter must consume both existing supported Python library export forms and
the selected packaging profile honestly. If a wheel is required, verify its exact
qualified bytes and complete installed file projection in a private environment;
do not rebuild an accepted wheel or run a backend during read-only inspection.
If an admitted source export requires a build, bind the exact backend and offline
build closure and execute it only as an acknowledged lifecycle stage. Neither
pip resolution nor setuptools discovery may fetch or substitute a provider.
Ordinary external dependencies retain their reviewed lock/provisioning policy.
Missing dependencies refuse instead of falling back to ambient packages or a
network index. Unsupported export or native-extension/ABI forms refuse explicitly;
they are not reported as qualified Python consumption.

### Integrate with retained commands and evidence

Expose Python check, materialize and admit operations through the retained-library
front door with an explicit Python adapter. Read-only check and plan perform no
materialization, import, test, tool installation, backend execution, model call or
network retrieval. An acknowledged, identity-bound materialization/execution plan
owns those effects. Use existing model-free exact artifact retrieval where
supported; credentials remain operator-local and offline mode performs no network.

Integrate the bound library inputs into retained harness execution and readmission,
including the sealed receipt workspace that currently copies only source scope.
Do not enlarge the authored source scope to include arbitrary project ancestors.
Keep existing no-bridge receipts and Cargo behavior unchanged. Use a versioned
receipt/inventory extension when new fields change their closed contracts; old
readers must refuse unfamiliar authority rather than silently dropping it.

All previously admitted full consumer gates and their complete test identities
remain required. Missing imports, zero executed tests, changed test membership,
policy drift or an unqualified provider cannot yield a successful receipt. Reuse
the current independent acceptance and receipt-promotion rules without exemptions.
Remote execution requires complete transfer and revalidation of the same bound
package/evidence inputs; a remote path or local workspace link is not equivalent.
Until that transport is supported, refuse the requested remote operation before
dispatch and report the unsupported capability explicitly.

### Retire source only after prospective qualification

First apply the proposed import/packaging changes and source removal to the
disposable complete workspace. Require the exact prospective consumer to pass
its full original gates against the independently qualified providers. Preserve
shared ownership and unaffected boundaries. Only then may a separately
acknowledged integration plan retire the original source and publish the reviewed
binding/packaging delta. Never delete source merely to discover whether a package
works. Repeating the workflow with an unchanged valid provider is model-free.

## Validation

1. Exercise the public conversion, provider qualification, committed-cache
   publication, Python bridge, prospective retirement, retained full workflow and
   receipt reopening with a real multi-module Python library and consumer.
2. Repeat from a fresh checkout with empty ambient caches and model access
   disabled, supplying the exact artifact closure offline. Verify imports resolve
   to the sealed package roots, with standard-library imports working.
3. Exercise supported source and wheel packaging profiles and actual locked
   dependencies. Refuse absent artifacts, unbound dependencies, index fallback,
   unsupported ABI/export forms and package/module/namespace collisions.
4. Refuse provider/source/cache/evidence/policy/verifier/lock/interpreter drift,
   stale consumer receipts, changed inventories and missing or zero test execution.
   Exercise actual wrong-result and foreign-origin failures through public commands.
5. Verify read-only commands cause no execution or writes; exercise concurrent
   replacement, foreign-file preservation, interruption and bounded inputs.
6. Run unchanged Cargo and no-bridge Python regressions, independent source review,
   complete source gates, fresh noneditable installed qualification and the exact
   integrated platform/release gates.

These are required outcomes, not claims established by this proposal. Approval
authorizes implementation; source retirement and lifecycle execution retain their
existing separate acknowledgements.
