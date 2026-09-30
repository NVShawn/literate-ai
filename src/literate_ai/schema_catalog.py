"""Locate and verify the installed language-neutral JSON Schema catalog."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sysconfig
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import CannotDetermineSpecification, Unresolvable

from .version import SCHEMA_CATALOG_RELEASES

SUPPORTED_SCHEMA_CATALOGS = ("v1", "v2")
SCHEMA_CATALOG_ROOT_ENVIRONMENT = "LITERATE_AI_SCHEMA_CATALOG_ROOT"
PUBLISHED_SCHEMA_MANIFEST = "published-v0.1.1.json"
_PUBLISHED_SCHEMA_MANIFEST_SCHEMA = "literate-ai/published-schema-digests@1"
_PUBLISHED_SCHEMA_RELEASE = SCHEMA_CATALOG_RELEASES["v1"]
_PUBLISHED_SCHEMA_SOURCE_TAG = "v0.1.1"
_PUBLISHED_SCHEMA_SOURCE_COMMIT = "465260cb450dc1c386e257f93ff0277a28f8666b"
_PUBLISHED_SCHEMA_FILE_COUNT = 17
_PUBLISHED_SCHEMA_MANIFEST_SHA256 = (
    "21cc0cd2705f4d636b6dbeebee52f53fccd902d8947215a4e87668cc29f78d9a"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAXIMUM_MANIFEST_BYTES = 1024 * 1024
_COMPATIBILITY_READ_PATH_KEYS = frozenset({"input", "status", "adapter", "output"})
_COMPATIBILITY_WRITE_CONTRACT_ALLOWLIST = frozenset(
    {
        "urn:literate-ai:schema:v2:evidence-signer-rule",
        "urn:literate-ai:schema:v2:evidence-revocations",
        "urn:literate-ai:schema:v2:evidence-trust-policy",
        "urn:literate-ai:schema:v2:evidence-verification-plan",
        "urn:literate-ai:schema:v2:current-evidence-map",
        "literate-ai/evidence-verification-result@1",
        "literate-ai/evidence-publication-result@1",
        "literate-ai/retained-library-export-set@1",
        "literate-ai/retained-library-binding@1",
        "literate-ai/retained-cargo-workspace-plan@1",
        "literate-ai/version-check@1",
        "literate-ai/project-verify@1",
        "urn:literate-ai:schema:v2:project-lock-health",
        "urn:literate-ai:schema:v2:run-evidence-expectation",
        "urn:literate-ai:schema:v2:evidence-derivation-run",
        "urn:literate-ai:schema:v2:evidence-platform-run",
        "urn:literate-ai:schema:v2:evidence-matrix",
        "urn:literate-ai:schema:v2:evidence-locator",
        "literate-ai/isolation-policy@1",
        "literate-ai/isolation-request@1",
        "literate-ai/isolation-observation@1",
        "literate-ai/isolation-decision@1",
        "urn:literate-ai:schema:v1:html-observability-artifact",
        "urn:literate-ai:schema:v1:html-observability-external-asset",
        "urn:literate-ai:schema:v1:html-observability-provenance",
        "urn:literate-ai:schema:v1:html-observability-render-request",
        "urn:literate-ai:schema:v1:html-observability-render-refusal",
        "urn:literate-ai:schema:v1:html-observability-render-result",
        "urn:literate-ai:schema:v1:html-observability-renderer-binding",
        "urn:literate-ai:schema:v1:html-observability-source-binding",
        "urn:literate-ai:schema:v1:html-observability-staleness-report",
        "urn:literate-ai:schema:v1:html-observability-view",
        "urn:literate-ai:schema:v1:inherited-session-context-bundle",
        "urn:literate-ai:schema:v1:inherited-session-request",
        "urn:literate-ai:schema:v1:inherited-session-response",
        "urn:literate-ai:schema:v1:inherited-session-handoff-evidence",
        "urn:literate-ai:schema:v2:component-authority-projection",
        "urn:literate-ai:schema:v2:component-content-selector",
        "urn:literate-ai:schema:v2:component-asset-selector",
        "urn:literate-ai:schema:v2:resolved-component-asset",
        "urn:literate-ai:schema:v2:locked-flavor-requirement",
        "urn:literate-ai:schema:v2:component-authoring",
        "urn:literate-ai:schema:v2:locked-component-revision",
        "urn:literate-ai:schema:v2:component-lock-node",
        "urn:literate-ai:schema:v2:component-lock",
        "urn:literate-ai:schema:v2:component-resolution-audit",
        "urn:literate-ai:schema:v2:provider-capability-set",
        "urn:literate-ai:schema:v2:provider-override-declaration",
        "urn:literate-ai:schema:v2:provider-resolution-request",
        "urn:literate-ai:schema:v2:provider-resolution",
        "urn:literate-ai:schema:v2:provider-resolution-declaration",
        "urn:literate-ai:schema:v2:public-interface-contract",
        "urn:literate-ai:schema:v2:component-interface-binding",
        "urn:literate-ai:schema:v2:executable-component-edge",
        "urn:literate-ai:schema:v2:node-target-flavor-selection",
        "urn:literate-ai:schema:v2:authored-binary-asset",
        "urn:literate-ai:schema:v2:candidate-replacement-policy",
        "urn:literate-ai:schema:v2:lifecycle-driver-trust-binding",
        "urn:literate-ai:schema:v2:component-invalidation-decision",
        "urn:literate-ai:schema:v2:component-invalidation-table",
        "urn:literate-ai:schema:v2:candidate-repair-diagnostic",
        "urn:literate-ai:schema:v2:candidate-repair-request",
        "urn:literate-ai:schema:v2:candidate-attempt",
        "urn:literate-ai:schema:v2:candidate-attempt-chain",
        "urn:literate-ai:schema:v2:learning-evidence-binding",
        "urn:literate-ai:schema:v2:learning-signal-evidence",
        "urn:literate-ai:schema:v2:learning-observation",
        "urn:literate-ai:schema:v2:learning-classification",
        "urn:literate-ai:schema:v2:learning-plan-input",
        "urn:literate-ai:schema:v2:learning-proposal",
        "urn:literate-ai:schema:v2:component-generation-context-manifest",
        "urn:literate-ai:schema:v2:generation-complexity-budget",
        "urn:literate-ai:schema:v2:generation-complexity-decision",
        "urn:literate-ai:schema:v2:bounded-component-generation-request",
        "urn:literate-ai:schema:v2:forward-generation-context-segment-binding",
        "urn:literate-ai:schema:v2:direct-interface-context-binding",
        "urn:literate-ai:schema:v2:forward-generation-prompt-journal",
        "urn:literate-ai:schema:v2:component-context-benchmark-record",
        "urn:literate-ai:schema:v2:forward-generation-context-cache-entry",
        "urn:literate-ai:schema:v2:forward-generation-context-cache-report",
        "urn:literate-ai:schema:v2:component-generation-runtime-observation",
        "urn:literate-ai:schema:v2:component-generation-resume-candidate",
        "urn:literate-ai:schema:v2:component-generation-run-output",
        "urn:literate-ai:schema:v2:component-generation-node-result",
        "urn:literate-ai:schema:v2:component-generation-schedule-result",
        "urn:literate-ai:schema:v2:component-definition",
        "urn:literate-ai:schema:v2:component-revision",
        "urn:literate-ai:schema:v2:effective-revision",
        "urn:literate-ai:schema:v2:effective-specification-set",
        "urn:literate-ai:schema:v2:flavor-definition",
        "urn:literate-ai:schema:v2:flavor-revision",
        "urn:literate-ai:schema:v2:flavor-set-lock",
        "urn:literate-ai:schema:v2:model-endpoint",
        "urn:literate-ai:schema:v2:model-group",
        "urn:literate-ai:schema:v2:stage-model-policy",
        "urn:literate-ai:schema:v2:model-route-decision",
        "urn:literate-ai:schema:v2:publication-request",
        "urn:literate-ai:schema:v2:publication-authorization",
        "urn:literate-ai:schema:v2:publication-manifest",
        "urn:literate-ai:schema:v2:import-request",
        "urn:literate-ai:schema:v2:import-authorization",
        "urn:literate-ai:schema:v2:transfer-receipt",
        "urn:literate-ai:schema:v2:security-origin-attestation",
        "urn:literate-ai:schema:v2:security-finding",
        "urn:literate-ai:schema:v2:security-classification",
        "urn:literate-ai:schema:v2:build-request-declaration",
        "urn:literate-ai:schema:v2:build-request",
        "urn:literate-ai:schema:v2:observation-request",
        "urn:literate-ai:schema:v2:build-authorization",
        "urn:literate-ai:schema:v2:observation-execution-authorization",
        "urn:literate-ai:schema:v2:security-scan-report",
        "urn:literate-ai:schema:v2:authorization-revocation-set",
        "urn:literate-ai:schema:v3:source-to-specification-intelligence",
        "urn:literate-ai:schema:v3:source-to-specification-translation-run",
        "urn:literate-ai:schema:v2:source-to-specification-model-output",
        "urn:literate-ai:schema:v3:source-to-specification-model-call-journal",
        "urn:literate-ai:schema:v5:source-to-specification-translation-run",
        "urn:literate-ai:schema:v2:source-to-specification-result",
        "urn:literate-ai:schema:v2:component-graph-draft",
        "urn:literate-ai:schema:v3:source-to-specification-result",
        "urn:literate-ai:schema:v3:component-graph-draft",
        "urn:literate-ai:schema:v2:regenerative-qualification-policy",
        "urn:literate-ai:schema:v2:clean-regeneration-evidence",
        "urn:literate-ai:schema:v2:regenerative-qualification-decision",
        "urn:literate-ai:schema:v2:operational-qualification-attestation",
        "urn:literate-ai:schema:v1:qualification-run-checkpoint",
        "urn:literate-ai:schema:v1:qualification-run-checkpoint-key",
        "urn:literate-ai:schema:v2:local-regenerative-qualification-profile",
        "urn:literate-ai:schema:v2:local-qualification-signature",
        "urn:literate-ai:schema:v3:regenerative-qualification-evidence",
        "urn:literate-ai:schema:v2:generation-input-audit-entry",
        "urn:literate-ai:schema:v2:generation-input-audit",
        "urn:literate-ai:schema:v2:source-promotion-provenance",
        "urn:literate-ai:schema:v3:source-promotion-provenance",
        "urn:literate-ai:schema:v1:inverse-evidence-custody",
        "urn:literate-ai:schema:v1:semantic-requirement-graph",
        "urn:literate-ai:schema:v1:semantic-comparison-model-output",
        "urn:literate-ai:schema:v1:semantic-comparison-result",
        "urn:literate-ai:schema:v2:legacy-source-promotion-journal-migration",
        "urn:literate-ai:schema:v2:specification-node-authoring",
        "urn:literate-ai:schema:v2:specification-node",
        "urn:literate-ai:schema:v2:effective-specification-context",
        "urn:literate-ai:schema:v2:target-profile",
        "urn:literate-ai:schema:v2:toolchain-constraint",
        "urn:literate-ai:schema:v2:source-cache-model-binding",
        "urn:literate-ai:schema:v1:accepted-source-lookup-key",
        "urn:literate-ai:schema:v2:source-derivation-cache-key",
        "urn:literate-ai:schema:v2:accepted-source-derivation",
        "urn:literate-ai:schema:v2:source-cache-target",
        "urn:literate-ai:schema:v2:source-cache-configuration",
        "urn:literate-ai:schema:v2:cached-source-file",
        "urn:literate-ai:schema:v2:accepted-source-cache-entry",
        "urn:literate-ai:schema:v2:standard-accepted-source-cache-entry",
        "urn:literate-ai:schema:v1:source-intelligence-artifact",
        "urn:literate-ai:schema:v1:source-intelligence-attachment",
        "urn:literate-ai:schema:v2:project-definition",
        "urn:literate-ai:schema:v1:project-initialization-origin",
        "urn:literate-ai:schema:v1:project-initialization-baseline-file",
        "urn:literate-ai:schema:v1:project-initialization-baseline",
        "urn:literate-ai:schema:v1:repository-fetch-deadline-policy",
        "urn:literate-ai:schema:v1:repository-parent-reference",
        "urn:literate-ai:schema:v1:repository-parent-selection",
        "urn:literate-ai:schema:v1:repository-lineage-node",
        "urn:literate-ai:schema:v1:repository-lineage",
        "urn:literate-ai:schema:v1:repository-reparent-change",
        "urn:literate-ai:schema:v1:repository-reparent-plan",
        "urn:literate-ai:schema:v1:repository-lineage-update-plan",
        "urn:literate-ai:schema:v1:repository-lineage-update-apply",
        "urn:literate-ai:schema:v2:repository-lineage-update-apply",
        "urn:literate-ai:schema:v2:project-update-file",
        "urn:literate-ai:schema:v2:project-update-plan",
        "urn:literate-ai:schema:v2:project-lifecycle-driver",
        "urn:literate-ai:schema:v2:component-lifecycle-command",
        "urn:literate-ai:schema:v2:component-command-contract",
        "urn:literate-ai:schema:v2:multi-entrypoint-component-semantics@1",
        "urn:literate-ai:schema:v2:library-capability-import@1",
        "urn:literate-ai:schema:v2:library-import-surface@1",
        "urn:literate-ai:schema:v2:native-sdk-snapshot",
        "urn:literate-ai:schema:v2:native-sdk-build-layout",
        "urn:literate-ai:schema:v2:native-sdk-build-recipe",
        "urn:literate-ai:schema:v2:library-consumer-binding@1",
        "urn:literate-ai:schema:v1:project-source-intelligence-policy",
        "literate-ai/source-intelligence-stage-status@1",
        "urn:literate-ai:schema:v2:rebuild-source-cache-operator-root",
        "urn:literate-ai:schema:v2:rebuild-source-cache-derivation-manifest",
        "urn:literate-ai:schema:v2:rebuild-source-cache-control",
        "urn:literate-ai:schema:v2:rebuild-source-cache-decision-item",
        "urn:literate-ai:schema:v2:rebuild-source-cache-decision",
        "urn:literate-ai:schema:v2:rebuild-source-cache-lifecycle-member",
        "urn:literate-ai:schema:v2:rebuild-source-cache-lifecycle-binding",
        "urn:literate-ai:schema:v2:rebuild-source-cache-publication-member",
        "urn:literate-ai:schema:v2:rebuild-source-cache-publication-offer",
        "urn:literate-ai:schema:v2:rebuild-source-cache-publication-result",
        "urn:literate-ai:schema:v2:standard-component-build-intent",
        "urn:literate-ai:schema:v2:standard-build-authorization",
        "urn:literate-ai:schema:v2:standard-component-build-plan",
        "urn:literate-ai:schema:v2:standard-source-cache-membership",
        "urn:literate-ai:schema:v2:standard-planned-lifecycle-node",
        "urn:literate-ai:schema:v2:standard-node-cache-decision",
        "urn:literate-ai:schema:v2:standard-node-failure-evidence",
        "urn:literate-ai:schema:v2:standard-project-lifecycle-membership",
        "urn:literate-ai:schema:v2:standard-aggregate-receipt",
        "urn:literate-ai:schema:v2:standard-component-build-evidence",
        "urn:literate-ai:schema:v2:standard-generated-test-case-evidence",
        "urn:literate-ai:schema:v2:known-test-failure-annotation",
        "urn:literate-ai:schema:v2:known-test-failure-outcome",
        "urn:literate-ai:schema:v2:known-test-failure-report",
        "urn:literate-ai:schema:v2:standard-entrypoint-generated-test-evidence",
        "urn:literate-ai:schema:v2:standard-generated-test-execution-evidence",
        "urn:literate-ai:schema:v2:standard-entrypoint-execution-evidence",
        "urn:literate-ai:schema:v2:standard-component-execution-evidence",
        "urn:literate-ai:schema:v2:standard-component-acceptance-evidence",
        "urn:literate-ai:schema:v1:standard-root-integration-evidence",
        "urn:literate-ai:schema:v1:component-source-workspace-custody",
        "urn:literate-ai:schema:v1:project-source-generation-custody",
        "urn:literate-ai:schema:v2:qualification-case-surface-binding",
        "urn:literate-ai:schema:v2:qualification-verifier-case-map",
        "urn:literate-ai:schema:v2:qualification-parity-case-evidence",
        "urn:literate-ai:schema:v2:qualification-parity-evidence",
        "urn:literate-ai:schema:v2:qualification-lifecycle-run-evidence",
        "urn:literate-ai:schema:v2:qualification-lifecycle-result",
        "literate-ai/evidence-ledger@1",
        "literate-ai/evidence-ledger-node@1",
    }
)
_COMPATIBILITY_READ_PATH_ALLOWLIST = frozenset(
    {
        (
            "urn:literate-ai:schema:v2:source-to-specification-intelligence",
            "frozen-v2-adapter",
            "source_intelligence_from_dict",
            "urn:literate-ai:schema:v3:source-to-specification-intelligence",
        ),
        (
            "urn:literate-ai:schema:v2:source-to-specification-translation-run",
            "frozen-v2-adapter",
            "validate_model_translation_record",
            "urn:literate-ai:schema:v3:source-to-specification-translation-run",
        ),
        (
            "literate-ai/prepared-release@1",
            "unreleased-development-compatibility",
            "_load_record(..., (prepared-release@2, prepared-release@1))",
            "literate-ai/prepared-release@2",
        ),
        (
            "urn:literate-ai:schema:v1:generated-source-index",
            "unreleased-development-compatibility",
            "SourceIntelligenceArtifact.from_dict(...).to_dict",
            "urn:literate-ai:schema:v1:source-intelligence-artifact",
        ),
        *(
            (
                f"urn:literate-ai:schema:v1:{name}",
                "unreleased-development-compatibility",
                f"{adapter}.from_dict(...).to_dict",
                f"urn:literate-ai:schema:v2:{name}",
            )
            for name, adapter in (
                ("source-cache-model-binding", "SourceCacheModelBinding"),
                ("source-derivation-cache-key", "SourceDerivationCacheKey"),
                ("accepted-source-derivation", "AcceptedSourceDerivation"),
                ("source-cache-target", "SourceCacheTarget"),
                ("source-cache-configuration", "SourceCacheConfiguration"),
                ("cached-source-file", "CachedSourceFile"),
                ("accepted-source-cache-entry", "AcceptedSourceCacheEntry"),
                ("project-definition", "ProjectDefinition"),
                ("project-lifecycle-driver", "ProjectLifecycleDriver"),
                (
                    "rebuild-source-cache-operator-root",
                    "RebuildSourceCacheOperatorRoot",
                ),
                (
                    "rebuild-source-cache-derivation-manifest",
                    "RebuildSourceCacheDerivationManifest",
                ),
                ("rebuild-source-cache-control", "RebuildSourceCacheControl"),
                (
                    "rebuild-source-cache-decision-item",
                    "RebuildSourceCacheDecisionItem",
                ),
                ("rebuild-source-cache-decision", "RebuildSourceCacheDecision"),
                (
                    "rebuild-source-cache-lifecycle-member",
                    "RebuildSourceCacheLifecycleMember",
                ),
                (
                    "rebuild-source-cache-lifecycle-binding",
                    "RebuildSourceCacheLifecycleBinding",
                ),
                (
                    "rebuild-source-cache-publication-member",
                    "RebuildSourceCachePublicationMember",
                ),
                (
                    "rebuild-source-cache-publication-offer",
                    "RebuildSourceCachePublicationOffer",
                ),
            )
        ),
        (
            "urn:literate-ai:schema:v1:component-definition",
            "published-plus-known-unreleased-extension",
            "ComponentDefinition.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:component-definition",
        ),
        (
            "urn:literate-ai:schema:v1:component-revision",
            "published-envelope",
            "ComponentRevision.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:component-revision",
        ),
        (
            "urn:literate-ai:schema:v1:flavor-definition",
            "published-plus-known-unreleased-extension",
            "FlavorDefinition.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:flavor-definition",
        ),
        (
            "urn:literate-ai:schema:v1:flavor-revision",
            "published-envelope",
            "FlavorRevision.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:flavor-revision",
        ),
        (
            "urn:literate-ai:schema:v1:target-profile",
            "published-plus-known-unreleased-extension",
            "TargetProfile.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:target-profile",
        ),
        (
            "urn:literate-ai:schema:v1:flavor-set-lock",
            "published-envelope",
            "FlavorSetLock.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:flavor-set-lock",
        ),
        (
            "urn:literate-ai:schema:v1:effective-specification-set",
            "published-envelope",
            "EffectiveSpecificationSet.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:effective-specification-set",
        ),
        (
            "urn:literate-ai:schema:v1:effective-revision",
            "published-envelope",
            "EffectiveRevision.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:effective-revision",
        ),
        (
            "urn:literate-ai:schema:v1:toolchain-constraint",
            "unreleased-development-compatibility",
            "ToolchainConstraint.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:toolchain-constraint",
        ),
        (
            "urn:literate-ai:schema:v1:model-endpoint",
            "published-plus-known-unreleased-extension",
            "ModelEndpoint.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:model-endpoint",
        ),
        (
            "urn:literate-ai:schema:v1:model-group",
            "published",
            "ModelGroup.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:model-group",
        ),
        (
            "urn:literate-ai:schema:v1:stage-model-policy",
            "published",
            "StageModelPolicy.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:stage-model-policy",
        ),
        (
            "urn:literate-ai:schema:v1:model-route-decision",
            "published",
            "ModelRouteDecision.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:model-route-decision",
        ),
        (
            "urn:literate-ai:schema:v1:publication-request",
            "unreleased-development-compatibility",
            "PublicationRequest.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:publication-request",
        ),
        (
            "urn:literate-ai:schema:v1:publication-authorization",
            "unreleased-development-compatibility",
            "PublicationAuthorization.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:publication-authorization",
        ),
        (
            "urn:literate-ai:schema:v1:publication-manifest",
            "published-context-required",
            "PublicationManifest.from_dict(..., legacy_request=..., "
            "legacy_authorization=...).to_dict",
            "urn:literate-ai:schema:v2:publication-manifest",
        ),
        (
            "literate-ai/unreleased-post-v0.1.1-publication-manifest@3",
            "unreleased-development-compatibility",
            "PublicationManifest.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:publication-manifest",
        ),
        (
            "urn:literate-ai:schema:v1:import-request",
            "unreleased-development-compatibility",
            "ImportRequest.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:import-request",
        ),
        (
            "urn:literate-ai:schema:v1:import-authorization",
            "unreleased-development-compatibility",
            "ImportAuthorization.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:import-authorization",
        ),
        (
            "urn:literate-ai:schema:v1:transfer-receipt",
            "published-context-required",
            "TransferReceipt.from_dict(..., legacy_manifest=...).to_dict",
            "urn:literate-ai:schema:v2:transfer-receipt",
        ),
        (
            "literate-ai/unreleased-post-v0.1.1-transfer-receipt@3",
            "unreleased-development-compatibility",
            "TransferReceipt.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:transfer-receipt",
        ),
        (
            "urn:literate-ai:schema:v1:security-origin-attestation",
            "published",
            "OriginAttestation.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:security-origin-attestation",
        ),
        (
            "urn:literate-ai:schema:v1:security-finding",
            "published",
            "SecurityFinding.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:security-finding",
        ),
        (
            "urn:literate-ai:schema:v1:security-classification",
            "published-supported-subset",
            "SecurityClassification.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:security-classification",
        ),
        (
            "urn:literate-ai:schema:v1:build-request",
            "published",
            "BuildRequest.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:build-request",
        ),
        (
            "urn:literate-ai:schema:v1:observation-request",
            "published-supported-subset",
            "ObservationRequest.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:observation-request",
        ),
        (
            "urn:literate-ai:schema:v1:build-authorization",
            "published",
            "BuildAuthorization.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:build-authorization",
        ),
        (
            "literate-ai/unreleased-post-v0.1.1-observation-execution-authorization",
            "unreleased-development-compatibility",
            "ObservationExecutionAuthorization.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:observation-execution-authorization",
        ),
        (
            "urn:literate-ai:schema:v1:security-scan-report",
            "published",
            "SecurityScanReport.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:security-scan-report",
        ),
        (
            "urn:literate-ai:schema:v1:authorization-revocation-set",
            "published",
            "AuthorizationRevocationSet.from_dict(...).to_dict",
            "urn:literate-ai:schema:v2:authorization-revocation-set",
        ),
        (
            "urn:literate-ai:schema:v1:source-to-specification-result",
            "published",
            "migrate_v1_source_to_specification_result",
            "urn:literate-ai:schema:v2:source-to-specification-result",
        ),
        (
            "literate-ai/unreleased-post-v0.1.1-source-to-specification-result",
            "unreleased-development-compatibility",
            "adapt_unreleased_post_v011_source_to_specification_result",
            "urn:literate-ai:schema:v2:source-to-specification-result",
        ),
        *(
            (
                f"urn:literate-ai:schema:v1:{name}",
                "unreleased-development-compatibility",
                "adapt_unreleased_post_v011_qualification_document",
                f"urn:literate-ai:schema:v2:{name}",
            )
            for name in (
                "regenerative-qualification-policy",
                "clean-regeneration-evidence",
                "regenerative-qualification-decision",
                "operational-qualification-attestation",
                "local-qualification-signature",
            )
        ),
    }
)
_COMPATIBILITY_READ_PATH_METADATA = {
    "urn:literate-ai:schema:v1:publication-manifest": {
        "required_context": [
            "verified-v2-publication-request",
            "verified-v2-publication-authorization",
        ]
    },
    "urn:literate-ai:schema:v1:transfer-receipt": {
        "required_context": ["verified-v2-publication-manifest-bytes"]
    },
    "urn:literate-ai:schema:v1:security-classification": {
        "condition": "source-digests-and-origin-attestation-digests-nonempty"
    },
    "urn:literate-ai:schema:v1:observation-request": {
        "condition": "source-digests-and-allowed-outputs-nonempty"
    },
    "urn:literate-ai:schema:v1:source-to-specification-result": {
        "missing_graph": "blocking-uncertainty"
    },
    "literate-ai/unreleased-post-v0.1.1-source-to-specification-result": {
        "missing_graph": "blocking-uncertainty"
    },
    "urn:literate-ai:schema:v1:regenerative-qualification-decision": {
        "authority_semantics": "historical-claim-effective-source-baseline"
    },
}
_COMPATIBILITY_INCOMPATIBLE_ALLOWLIST = frozenset(
    {
        (
            "literate-ai/accepted-specification-set@1",
            None,
            "legacy acceptance asserted intent authority but did not bind the "
            "evidence closure required by a ComponentAuthorityProjection",
            "fail-closed-until-AUTH-100-evidence-is-supplied",
        ),
        (
            "urn:literate-ai:schema:v1:security-classification",
            "source_digests or origin_attestation_digests is empty",
            "the published record lacks the source and origin evidence required by "
            "the v2 security decision",
            "fail-closed-missing-required-security-evidence",
        ),
        (
            "urn:literate-ai:schema:v1:observation-request",
            "source_digests or allowed_outputs is empty",
            "the published record does not identify a non-empty observed source and "
            "output scope",
            "fail-closed-missing-observation-scope",
        ),
        (
            "urn:literate-ai:schema:v1:observation-execution-authorization",
            None,
            "the published record has neither effective_revision_digest nor security "
            "profile and no local field can reconstruct them",
            "fail-closed-missing-effective-revision-and-security-profile",
        ),
    }
)


class PublishedSchemaError(RuntimeError):
    """Published schema bytes or their immutable digest catalog are invalid."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SchemaCatalogError(RuntimeError):
    """A requested versioned schema catalog is unsupported or malformed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _catalog_version(value: str) -> str:
    if value not in SUPPORTED_SCHEMA_CATALOGS:
        raise SchemaCatalogError(
            "schema.catalog_version_unsupported",
            f"unsupported schema catalog version: {value!r}",
        )
    return value


def _catalog_roots_from_base(base: Path, *, origin: str) -> dict[str, Path]:
    if base.is_symlink() or not base.is_dir():
        raise SchemaCatalogError(
            "schema.catalog_root_invalid",
            f"{origin} schema catalog root must be a directory, not a symbolic link",
        )
    try:
        resolved_base = base.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SchemaCatalogError(
            "schema.catalog_root_invalid",
            f"{origin} schema catalog root must be an available directory",
        ) from exc

    roots: dict[str, Path] = {}
    for version in SUPPORTED_SCHEMA_CATALOGS:
        unresolved = base / version
        if unresolved.is_symlink() or not unresolved.is_dir():
            raise SchemaCatalogError(
                "schema.catalog_version_missing",
                f"{origin} schema catalog root does not contain regular {version}",
            )
        try:
            root = unresolved.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SchemaCatalogError(
                "schema.catalog_root_invalid",
                f"{origin} {version} schema catalog is unavailable",
            ) from exc
        if root.parent != resolved_base:
            raise SchemaCatalogError(
                "schema.catalog_root_invalid",
                f"{origin} {version} schema catalog escapes its configured root",
            )
        index = root / "index.json"
        if index.is_symlink() or not index.is_file():
            raise SchemaCatalogError(
                "schema.catalog_index_missing",
                f"{origin} {version} schema catalog index is unavailable",
            )
        roots[version] = root
    return roots


def _schema_catalog_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    try:
        for path in sorted(root.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise OSError
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError as exc:
        raise SchemaCatalogError(
            "schema.catalog_file_invalid",
            "schema catalog entries must be available regular files",
        ) from exc
    return digest.hexdigest()


@lru_cache(maxsize=16)
def _validated_catalog_root(version: str, root_text: str, fingerprint: str) -> Path:
    root = Path(root_text)
    verify_schema_catalog(version, root)
    if version == "v1":
        verify_published_schemas(root)
    if _schema_catalog_fingerprint(root) != fingerprint:
        raise SchemaCatalogError(
            "schema.catalog_changed",
            "schema catalog changed while it was being validated",
        )
    return root


def _validated_catalog_roots(roots: dict[str, Path]) -> dict[str, Path]:
    return {
        version: _validated_catalog_root(
            version,
            str(root),
            _schema_catalog_fingerprint(root),
        )
        for version, root in roots.items()
    }


def schema_catalog_root(catalog_version: str = "v1") -> Path:
    """Resolve one version from a complete source, embedded, or installed catalog."""

    version = _catalog_version(catalog_version)
    configured = os.environ.get(SCHEMA_CATALOG_ROOT_ENVIRONMENT)
    if configured is not None:
        if not configured or "\x00" in configured or not Path(configured).is_absolute():
            raise SchemaCatalogError(
                "schema.catalog_root_configuration_invalid",
                f"{SCHEMA_CATALOG_ROOT_ENVIRONMENT} must name one absolute directory",
            )
        roots = _catalog_roots_from_base(
            Path(configured), origin=SCHEMA_CATALOG_ROOT_ENVIRONMENT
        )
        return _validated_catalog_roots(roots)[version]

    candidates = (
        (Path(__file__).resolve().parents[2] / "schemas", "source"),
        (
            Path(sysconfig.get_path("data")) / "share" / "literate-ai" / "schemas",
            "installed",
        ),
    )
    for base, origin in candidates:
        if any(
            (candidate := base / supported).exists() or candidate.is_symlink()
            for supported in SUPPORTED_SCHEMA_CATALOGS
        ):
            return _validated_catalog_roots(
                _catalog_roots_from_base(base, origin=origin)
            )[version]
    raise FileNotFoundError(
        f"Literate AI {version} schema catalog is unavailable; reinstall the wheel or "
        f"set {SCHEMA_CATALOG_ROOT_ENVIRONMENT} to a complete catalog root"
    )


def schema_path(name: str, *, catalog_version: str = "v1") -> Path:
    """Resolve one normalized schema filename inside the installed catalog."""

    if not name or Path(name).name != name or not name.endswith(".json"):
        raise ValueError("schema name must be one JSON filename")
    path = schema_catalog_root(catalog_version) / name
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"installed schema is unavailable: {name}")
    return path


def _schema_resources(value: object) -> set[str]:
    resources: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            identifier = item.get("$id")
            if isinstance(identifier, str):
                if identifier in resources:
                    raise SchemaCatalogError(
                        "schema.catalog_resource_duplicate",
                        f"schema resource is duplicated: {identifier}",
                    )
                resources.add(identifier)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return resources


def _schema_references(value: object, *, base_uri: str) -> tuple[tuple[str, str], ...]:
    references: list[tuple[str, str]] = []

    def visit(item: object, current_base: str) -> None:
        if isinstance(item, dict):
            identifier = item.get("$id")
            if isinstance(identifier, str):
                current_base = urljoin(current_base, identifier)
            for keyword in ("$ref", "$dynamicRef"):
                reference = item.get(keyword)
                if isinstance(reference, str):
                    references.append((current_base, reference))
            for child in item.values():
                visit(child, current_base)
        elif isinstance(item, list):
            for child in item:
                visit(child, current_base)

    visit(value, base_uri)
    return tuple(references)


def _v1_support_root(catalog_root: Path) -> Path:
    candidates = (
        catalog_root.parent / "v1",
        Path(__file__).resolve().parents[2] / "schemas" / "v1",
        Path(sysconfig.get_path("data")) / "share" / "literate-ai" / "schemas" / "v1",
    )
    for candidate in candidates:
        if not candidate.is_symlink() and candidate.is_dir():
            try:
                return candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
    raise SchemaCatalogError(
        "schema.catalog_reference_support_missing",
        "v2 schema validation requires the frozen v1 reference catalog",
    )


def _verify_schema_documents(
    *,
    catalog_version: str,
    catalog_root: Path,
    documents: tuple[tuple[Path, dict[str, object]], ...],
) -> None:
    supporting = documents
    if catalog_version == "v2":
        v1_root = _v1_support_root(catalog_root)
        loaded: list[tuple[Path, dict[str, object]]] = []
        for path in sorted(v1_root.glob("*.schema.json")):
            if path.is_symlink() or not path.is_file():
                raise SchemaCatalogError(
                    "schema.catalog_reference_support_invalid",
                    "frozen v1 reference schemas must be regular files",
                )
            try:
                value = json.loads(path.read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SchemaCatalogError(
                    "schema.catalog_reference_support_invalid",
                    f"frozen v1 reference schema is invalid: {path.name}",
                ) from exc
            if not isinstance(value, dict):
                raise SchemaCatalogError(
                    "schema.catalog_reference_support_invalid",
                    f"frozen v1 reference schema is invalid: {path.name}",
                )
            loaded.append((path, value))
        supporting = (*documents, *loaded)

    registry = Registry()
    for path, document in supporting:
        identifier = document.get("$id")
        if not isinstance(identifier, str) or not identifier:
            raise SchemaCatalogError(
                "schema.catalog_file_invalid",
                f"schema lacks a root identity: {path.name}",
            )
        try:
            Draft202012Validator.check_schema(document)
            registry = registry.with_resource(
                identifier, Resource.from_contents(document)
            )
        except (CannotDetermineSpecification, SchemaError) as exc:
            raise SchemaCatalogError(
                "schema.catalog_file_invalid",
                f"schema violates Draft 2020-12: {path.name}",
            ) from exc
    registry = registry.crawl()
    for path, document in documents:
        identifier = document["$id"]
        assert isinstance(identifier, str)
        for base_uri, reference in _schema_references(document, base_uri=identifier):
            try:
                registry.resolver(base_uri=base_uri).lookup(reference)
            except Unresolvable as exc:
                raise SchemaCatalogError(
                    "schema.catalog_reference_unresolved",
                    f"schema reference is unresolved in {path.name}: {reference}",
                ) from exc


def verify_schema_catalog(
    catalog_version: str, root: Path | None = None
) -> dict[str, object]:
    """Verify one complete packaged catalog and its exact resource index."""

    version = _catalog_version(catalog_version)
    unresolved = schema_catalog_root(version) if root is None else Path(root)
    if unresolved.is_symlink() or not unresolved.is_dir():
        raise SchemaCatalogError(
            "schema.catalog_root_invalid", "schema catalog root must be a directory"
        )
    try:
        catalog_root = unresolved.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SchemaCatalogError(
            "schema.catalog_root_invalid", "schema catalog root must be a directory"
        ) from exc
    index_path = catalog_root / "index.json"
    if index_path.is_symlink() or not index_path.is_file():
        raise SchemaCatalogError(
            "schema.catalog_index_missing", "schema catalog index is unavailable"
        )
    try:
        index = json.loads(index_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaCatalogError(
            "schema.catalog_index_invalid", "schema catalog index is invalid"
        ) from exc
    expected_id = f"urn:literate-ai:schema-catalog:{version}"
    if (
        not isinstance(index, dict)
        or index.get("schema_version") != int(version[1:])
        or index.get("catalog_id") != expected_id
        or index.get("dialect") != "https://json-schema.org/draft/2020-12/schema"
        or not isinstance(index.get("schemas"), list)
    ):
        raise SchemaCatalogError(
            "schema.catalog_index_invalid", "schema catalog metadata is invalid"
        )
    indexed_files: set[str] = set()
    indexed_resources: set[str] = set()
    actual_resources: set[str] = set()
    schema_documents: list[tuple[Path, dict[str, object]]] = []
    for position, raw in enumerate(index["schemas"]):
        if (
            not isinstance(raw, dict)
            or set(raw) != {"file", "root_id", "public_ids"}
            or not isinstance(raw["file"], str)
            or Path(raw["file"]).name != raw["file"]
            or not raw["file"].endswith(".schema.json")
            or not isinstance(raw["root_id"], str)
            or not isinstance(raw["public_ids"], list)
            or not all(isinstance(item, str) for item in raw["public_ids"])
        ):
            raise SchemaCatalogError(
                "schema.catalog_index_invalid",
                f"schema catalog entry {position} is invalid",
            )
        filename = raw["file"]
        if filename in indexed_files:
            raise SchemaCatalogError(
                "schema.catalog_index_invalid", "schema files must be unique"
            )
        indexed_files.add(filename)
        path = catalog_root / filename
        if path.is_symlink() or not path.is_file():
            raise SchemaCatalogError(
                "schema.catalog_file_missing", f"schema is unavailable: {filename}"
            )
        try:
            document = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaCatalogError(
                "schema.catalog_file_invalid", f"schema is invalid: {filename}"
            ) from exc
        if not isinstance(document, dict) or document.get("$id") != raw["root_id"]:
            raise SchemaCatalogError(
                "schema.catalog_file_invalid",
                f"schema root identity differs from its index: {filename}",
            )
        schema_documents.append((path, document))
        resources = _schema_resources(document)
        expected_resources = {raw["root_id"], *raw["public_ids"]}
        if resources != expected_resources or not actual_resources.isdisjoint(
            resources
        ):
            raise SchemaCatalogError(
                "schema.catalog_resource_mismatch",
                f"schema resources differ from their index: {filename}",
            )
        actual_resources.update(resources)
        indexed_resources.update(expected_resources)
    actual_files = {path.name for path in catalog_root.glob("*.schema.json")}
    if indexed_files != actual_files or indexed_resources != actual_resources:
        raise SchemaCatalogError(
            "schema.catalog_index_incomplete",
            "schema catalog index does not cover every schema resource",
        )
    _verify_schema_documents(
        catalog_version=version,
        catalog_root=catalog_root,
        documents=tuple(schema_documents),
    )
    if version == "v2":
        if (
            index.get("release") != SCHEMA_CATALOG_RELEASES["v2"]
            or index.get("compatibility") != "compatibility.json"
        ):
            raise SchemaCatalogError(
                "schema.catalog_index_invalid", "v2 catalog release metadata is invalid"
            )
        compatibility = catalog_root / "compatibility.json"
        if compatibility.is_symlink() or not compatibility.is_file():
            raise SchemaCatalogError(
                "schema.catalog_compatibility_missing",
                "v2 compatibility matrix is unavailable",
            )
        try:
            matrix = json.loads(compatibility.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaCatalogError(
                "schema.catalog_compatibility_invalid",
                "v2 compatibility matrix is invalid",
            ) from exc
        if (
            not isinstance(matrix, dict)
            or set(matrix)
            != {
                "schema",
                "distribution",
                "current_catalog",
                "write_contracts",
                "read_paths",
                "incompatible_inputs",
                "unknown_versions",
            }
            or matrix["schema"] != "literate-ai/wire-compatibility-matrix@1"
            or matrix["distribution"] != SCHEMA_CATALOG_RELEASES["v2"]
            or matrix["current_catalog"] != "v2"
            or matrix["unknown_versions"] != "fail-closed"
            or not isinstance(matrix["write_contracts"], list)
            or not all(isinstance(item, str) for item in matrix["write_contracts"])
            or len(matrix["write_contracts"]) != len(set(matrix["write_contracts"]))
            or set(matrix["write_contracts"]) != _COMPATIBILITY_WRITE_CONTRACT_ALLOWLIST
            or not _COMPATIBILITY_WRITE_CONTRACT_ALLOWLIST.issubset(actual_resources)
            or not isinstance(matrix["read_paths"], list)
            or not isinstance(matrix["incompatible_inputs"], list)
        ):
            raise SchemaCatalogError(
                "schema.catalog_compatibility_invalid",
                "v2 compatibility matrix metadata is invalid",
            )
        read_path_claims: set[tuple[str, str, str, str]] = set()
        for position, path in enumerate(matrix["read_paths"]):
            metadata = (
                _COMPATIBILITY_READ_PATH_METADATA.get(path.get("input"), {})
                if isinstance(path, dict)
                else {}
            )
            if (
                not isinstance(path, dict)
                or set(path) != _COMPATIBILITY_READ_PATH_KEYS | set(metadata)
                or not all(
                    isinstance(path[key], str) and path[key]
                    for key in ("input", "status", "adapter", "output")
                )
                or path["output"] not in actual_resources
                or any(path.get(key) != value for key, value in metadata.items())
            ):
                raise SchemaCatalogError(
                    "schema.catalog_compatibility_invalid",
                    f"v2 compatibility read path {position} is invalid",
                )
            claim = (
                path["input"],
                path["status"],
                path["adapter"],
                path["output"],
            )
            if (
                claim not in _COMPATIBILITY_READ_PATH_ALLOWLIST
                or claim in read_path_claims
            ):
                raise SchemaCatalogError(
                    "schema.catalog_compatibility_invalid",
                    f"v2 compatibility read path {position} is not allowlisted",
                )
            read_path_claims.add(claim)
        if read_path_claims != _COMPATIBILITY_READ_PATH_ALLOWLIST:
            raise SchemaCatalogError(
                "schema.catalog_compatibility_invalid",
                "v2 compatibility matrix omits an allowlisted read path",
            )
        incompatible_claims: set[tuple[str, str | None, str, str]] = set()
        for position, claim in enumerate(matrix["incompatible_inputs"]):
            condition = claim.get("condition") if isinstance(claim, dict) else None
            expected_keys = {"input", "reason", "migration"}
            if condition is not None:
                expected_keys.add("condition")
            if (
                not isinstance(claim, dict)
                or set(claim) != expected_keys
                or not all(
                    isinstance(claim[key], str) and claim[key] for key in expected_keys
                )
            ):
                raise SchemaCatalogError(
                    "schema.catalog_compatibility_invalid",
                    f"v2 incompatible input {position} is invalid",
                )
            normalized = (
                claim["input"],
                condition,
                claim["reason"],
                claim["migration"],
            )
            if (
                normalized not in _COMPATIBILITY_INCOMPATIBLE_ALLOWLIST
                or normalized in incompatible_claims
            ):
                raise SchemaCatalogError(
                    "schema.catalog_compatibility_invalid",
                    f"v2 incompatible input {position} is not allowlisted",
                )
            incompatible_claims.add(normalized)
        if incompatible_claims != _COMPATIBILITY_INCOMPATIBLE_ALLOWLIST:
            raise SchemaCatalogError(
                "schema.catalog_compatibility_invalid",
                "v2 compatibility matrix omits an allowlisted incompatible input",
            )
    return {
        "catalog_version": version,
        "catalog_id": expected_id,
        "schema_file_count": len(indexed_files),
        "resource_count": len(actual_resources),
        "release": index.get("release"),
    }


def _published_manifest(root: Path) -> dict[str, object]:
    path = root / PUBLISHED_SCHEMA_MANIFEST
    if path.is_symlink() or not path.is_file():
        raise PublishedSchemaError(
            "schema.published_manifest_missing",
            "published schema digest catalog is unavailable",
        )
    content = path.read_bytes()
    if len(content) > _MAXIMUM_MANIFEST_BYTES:
        raise PublishedSchemaError(
            "schema.published_manifest_invalid",
            "published schema digest catalog exceeds its size limit",
        )
    if hashlib.sha256(content).hexdigest() != _PUBLISHED_SCHEMA_MANIFEST_SHA256:
        raise PublishedSchemaError(
            "schema.published_manifest_drift",
            "published schema digest catalog differs from its frozen release",
        )
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishedSchemaError(
            "schema.published_manifest_invalid",
            "published schema digest catalog is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "release",
        "source_tag",
        "source_commit",
        "algorithm",
        "catalog_index",
        "files",
    }:
        raise PublishedSchemaError(
            "schema.published_manifest_invalid",
            "published schema digest catalog has an invalid record shape",
        )
    if (
        value["schema"] != _PUBLISHED_SCHEMA_MANIFEST_SCHEMA
        or value["release"] != _PUBLISHED_SCHEMA_RELEASE
        or value["source_tag"] != _PUBLISHED_SCHEMA_SOURCE_TAG
        or value["source_commit"] != _PUBLISHED_SCHEMA_SOURCE_COMMIT
        or value["algorithm"] != "sha256"
        or not isinstance(value["release"], str)
        or not value["release"]
        or not isinstance(value["source_tag"], str)
        or not value["source_tag"]
        or not isinstance(value["source_commit"], str)
        or not isinstance(value["catalog_index"], dict)
        or not isinstance(value["files"], list)
        or len(value["files"]) != _PUBLISHED_SCHEMA_FILE_COUNT
    ):
        raise PublishedSchemaError(
            "schema.published_manifest_invalid",
            "published schema digest catalog metadata is invalid",
        )
    return value


def verify_published_schemas(root: Path | None = None) -> dict[str, object]:
    """Require every released v1 schema to match its first-published bytes."""

    unresolved_root = schema_catalog_root("v1") if root is None else Path(root)
    if unresolved_root.is_symlink() or not unresolved_root.is_dir():
        raise PublishedSchemaError(
            "schema.catalog_root_invalid", "schema catalog root must be a directory"
        )
    try:
        catalog_root = unresolved_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PublishedSchemaError(
            "schema.catalog_root_invalid", "schema catalog root must be a directory"
        ) from exc
    manifest = _published_manifest(catalog_root)
    index_entry = manifest["catalog_index"]
    if (
        not isinstance(index_entry, dict)
        or set(index_entry) != {"path", "bytes", "digest"}
        or index_entry.get("path") != "index.json"
        or not isinstance(index_entry.get("bytes"), int)
        or isinstance(index_entry.get("bytes"), bool)
        or index_entry["bytes"] < 1
        or not isinstance(index_entry.get("digest"), str)
        or _SHA256.fullmatch(index_entry["digest"]) is None
    ):
        raise PublishedSchemaError(
            "schema.published_manifest_invalid",
            "published catalog index entry is invalid",
        )
    index_path = catalog_root / "index.json"
    if index_path.is_symlink() or not index_path.is_file():
        raise PublishedSchemaError(
            "schema.published_index_missing", "published catalog index is unavailable"
        )
    index_content = index_path.read_bytes()
    if (
        len(index_content) != index_entry["bytes"]
        or hashlib.sha256(index_content).hexdigest() != index_entry["digest"]
    ):
        raise PublishedSchemaError(
            "schema.published_index_drift",
            "published catalog index changed under the same catalog identity",
        )
    raw_files = manifest["files"]
    assert isinstance(raw_files, list)
    paths: list[str] = []
    for index, raw in enumerate(raw_files):
        if not isinstance(raw, dict) or set(raw) != {"path", "bytes", "digest"}:
            raise PublishedSchemaError(
                "schema.published_manifest_invalid",
                f"published schema entry {index} has an invalid record shape",
            )
        name = raw["path"]
        size = raw["bytes"]
        digest = raw["digest"]
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".schema.json")
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise PublishedSchemaError(
                "schema.published_manifest_invalid",
                f"published schema entry {index} is invalid",
            )
        paths.append(name)
        path = catalog_root / name
        if path.is_symlink() or not path.is_file():
            raise PublishedSchemaError(
                "schema.published_file_missing",
                f"published schema is unavailable: {name}",
            )
        content = path.read_bytes()
        actual_digest = hashlib.sha256(content).hexdigest()
        if len(content) != size or actual_digest != digest:
            raise PublishedSchemaError(
                "schema.published_file_drift",
                f"published schema bytes changed under the same URI: {name}",
            )
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise PublishedSchemaError(
            "schema.published_manifest_invalid",
            "published schema paths must be sorted and unique",
        )
    return {
        "release": manifest["release"],
        "source_tag": manifest["source_tag"],
        "source_commit": manifest["source_commit"],
        "algorithm": manifest["algorithm"],
        "file_count": len(paths),
    }


__all__ = [
    "PUBLISHED_SCHEMA_MANIFEST",
    "PublishedSchemaError",
    "SCHEMA_CATALOG_ROOT_ENVIRONMENT",
    "SUPPORTED_SCHEMA_CATALOGS",
    "SchemaCatalogError",
    "schema_catalog_root",
    "schema_path",
    "verify_schema_catalog",
    "verify_published_schemas",
]
