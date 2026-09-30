# Capability-based provider resolution

Literate AI resolves interchangeable implementation providers without owning a
project's mission policy. A project supplies stable provider IDs, each provider's exact
capability set, required capabilities, one preferred provider, and an ordered fallback
list. The canonical resolver owns only the deterministic selection semantics and
identity evidence.

## Selection contract

`ProviderResolutionDeclaration`, `ProviderResolutionRequest`, and
`ProviderCapabilitySet` are provider-neutral public contracts. A Component authors each
named declaration in `component.md`; a selected Flavor may author one exact
`provider_overrides` entry for that declaration. Required capabilities and each
capability set use unique lexical order. Fallback order is explicit and must not repeat
the preferred provider. Resolution IDs are unique across the locked Component closure.

```yaml
provider_resolutions:
  - resolution_id: storage
    preferred_provider: native
    required_capabilities:
      - transactions
    fallback_order:
      - portable
    capability_sets:
      - provider_id: native
        capabilities:
          - snapshots
          - transactions
      - provider_id: portable
        capabilities:
          - transactions
```

A Flavor override is deliberately smaller and cannot replace the catalog or weaken the
requirements:

```yaml
provider_overrides:
  - resolution_id: storage
    provider_id: portable
```

Resolution follows this fixed policy:

1. An explicit Flavor override is evaluated alone. It succeeds only when that provider
   satisfies every requirement; otherwise resolution fails closed.
2. Without an override, the preferred provider is selected whenever sufficient.
3. Fallback candidates are considered in declared order only after the preferred
   provider is insufficient. Input catalog traversal order has no effect.
4. If no candidate satisfies every requirement, resolution fails without dropping,
   weakening, or partitioning requirements.

The result records the selected provider, exact request, evaluated capability-set
identities, exact catalog identity, canonical resolver-policy identity, fallback reason,
and—only for an override—the exact Flavor declaration and provenance identities.

## Plan and lock authority

`ComponentLock.provider_resolutions` stores complete canonical results. A provider
capability change therefore changes the catalog and resolution identities, making an
old lock stale by identity rather than by an ambient timestamp. Every
`ComponentGenerationKey` repeats the selected resolution identities, so execution-plan
and generated-source cache identities cannot silently retain an old provider choice.

Re-resolving after catalog evolution starts from the request again. If the preferred
provider gains the missing capability, it is selected immediately even when the prior
lock selected a fallback.

`litai lock COMPONENT` authors the exact result into `component.lock.json` and reports
the resolution ID, selected provider, fallback reason, result identity, and exact
override declaration/provenance identities. `litai lock --check` validates the same
inputs without writing. `litai plan COMPONENT` projects every complete provider result
from the admitted lock under `resolution.provider_resolutions`.

The Python API exposes both authored and already-projected integration surfaces:

```python
from literate_ai.contracts import (
    ProviderCapabilitySet,
    ProviderOverrideDeclaration,
    ProviderResolutionDeclaration,
    ProviderResolutionRequest,
    resolve_provider,
    resolve_provider_declaration,
)
```

Derived products should normally author `component.md` and `flavor.md` and consume the
selected result from the lock or plan. Product APIs may project mission declarations
into these contracts directly, but must not reimplement ordering, sufficiency, override
validation, or provenance rules.

## Physics Workbench sample migration

Physics Workbench sample keeps its physics policy downstream:

- provider IDs and capability declarations for Newton and PhysX;
- Newton as the preferred provider and PhysX in the fallback order;
- mission-required physics capabilities and mission-specific acceptance;
- any Flavor declaration that explicitly overrides the selected physics provider.

After consuming a Literate AI revision containing this contract, Physics Workbench sample should:

1. add one `physics` `provider_resolutions` declaration to the owning `component.md`,
   using Physics Workbench sample's Newton/PhysX capability catalog, required capabilities, Newton
   preference, and PhysX fallback order;
2. project any mission-selected physics Flavor override as that Flavor's
   `provider_overrides` entry rather than computing override provenance downstream;
3. run `litai lock ...`, review the selected provider/fallback or exact override
   provenance in its report, and commit the resulting current lock;
4. run `litai plan ...` and consume the `physics` result's `selected_provider` in
   downstream physics planning; and
5. delete generic candidate sorting, fallback traversal, capability-sufficiency,
   override-validation, and selection-provenance code from
   `physics_provider_resolution.py`.

Until a release contains the change, pin Literate AI to the exact merge commit of the
upstream pull request. After release, replace that commit pin with the first released
version containing `PROVIDER-001`; do not use an unpinned branch. Newton-first/PhysX
fallback remains Physics Workbench sample mission authority and must not move into this framework.
