"""Explicit SDK inspection metadata reachability, never executable authority.

Payload files are joined against the retained input tree by the inspector. Producer
stdout/stderr are diagnostics: their descriptors remain in the build record, but
this metadata walk neither fetches nor claims to verify those binary streams.
"""

from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.native_sdk_tree_products import NativeSdkTreeSnapshot
from literate_ai.contracts.native_sdks import NativeSdkSnapshot
from literate_ai.contracts.repositories import (
    RepositoryBuildPlan,
    RepositorySourceAdmission,
)

SDK_METADATA_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/vnd.literate-ai.manifest+json",
    }
)


def retained_sdk_metadata_closure(
    execution_identities, *, read_document, guard, maximum_records
):
    """Visit named metadata edges through a caller-owned bounded CAS reader.

    The returned documents are historical assertions. This traversal checks the
    explicit object roles, not live source admission or permission to execute.
    Callers must apply their exact current plan/selection/runtime joins afterward.
    """
    documents, manifests, bindings = {}, set(), set()
    if (
        type(maximum_records) is not int
        or not 1 <= maximum_records <= 1_000_000
        or type(execution_identities) is not tuple
        or len(execution_identities) > maximum_records
        or len(set(execution_identities)) != len(execution_identities)
    ):
        raise ValueError("retained.unit.bounded-sdk-metadata-required")

    def require(condition):
        if not condition:
            raise ValueError("retained.unit.sdk-metadata-closure-differs")

    def read(identity, *, reference=None):
        guard()
        if identity not in documents:
            require(len(documents) < maximum_records)
            value = read_document(identity, reference=reference)
            require(type(value) is dict and canonical_identity(value) == identity)
            documents[identity] = value
        elif reference is not None:
            # The bounded reader still checks every encountered reference's size,
            # MIME and custody, including duplicate references to a cached object.
            require(read_document(identity, reference=reference) == documents[identity])
        guard()
        return documents[identity]

    def embedded(value):
        require(read(canonical_identity(value)) == value)

    def blob(value):
        reference = BlobRef.from_dict(value)
        require(reference.media_type in SDK_METADATA_MEDIA_TYPES)
        return read(ContentIdentity.parse_uri(reference.identity), reference=reference)

    def identified(value):
        return read(ContentIdentity.from_dict(value))

    for identity in execution_identities:
        execution = read(identity)
        require(
            execution.get("schema")
            in {
                "literate-ai/native-sdk-command-execution@2",
                "literate-ai/native-sdk-command-execution@4",
                "literate-ai/native-sdk-command-execution@5",
            }
        )
        if execution["schema"] == "literate-ai/native-sdk-command-execution@5":
            observations = execution["retained_sdk_runtime_observations"]
            require(type(observations) is list and len(observations) <= maximum_records)
            for observation in observations:
                blob(observation["observation"])
        embedded(execution["request"])
        embedded(execution["authorization"])
        for dependency in execution["dependencies"]:
            embedded(dependency)
            identified(dependency["repository_resolution"]["cache_record_identity"])
        manifest = identified(execution["manifest_identity"])
        require(manifest.get("schema") == "literate-ai/native-sdk-execution-inputs@1")
        manifest_identity = canonical_identity(manifest)
        if manifest_identity in manifests:
            continue
        manifests.add(manifest_identity)
        require(
            type(manifest["imports"]) is list
            and len(manifest["imports"]) <= maximum_records
        )
        for item in manifest["imports"]:
            guard()
            binding = identified(item["input_identity"])
            input_identity = canonical_identity(binding)
            if input_identity in bindings:
                continue
            bindings.add(input_identity)
            require(binding.get("schema") == "literate-ai/native-sdk-consumer-input@1")
            snapshot_document = blob(binding["sdk_snapshot"])
            snapshot_type = (
                NativeSdkTreeSnapshot
                if snapshot_document.get("schema") == NativeSdkTreeSnapshot.SCHEMA
                else NativeSdkSnapshot
            )
            snapshot = snapshot_type.from_dict(snapshot_document)
            require(snapshot.to_dict() == item["snapshot"])
            runtime = blob(binding["runtime_observation"])
            require(
                runtime.get("schema")
                in {
                    "literate-ai/native-sdk-runtime-observation@1",
                    "literate-ai/native-sdk-tree-runtime-observation@1",
                }
            )
            result = blob(binding["build_result"])
            require(result.get("schema") == "literate-ai/native-sdk-repository-build@1")
            blob(result["snapshot"])
            blob(result["runtime_observation"])
            admission = RepositorySourceAdmission.from_dict(binding["source_admission"])
            build_plan = RepositoryBuildPlan.from_dict(read(admission.build_plan))
            selection = identified(binding["selection"])
            require(
                selection.get("schema") == "literate-ai/native-sdk-recipe-selection@1"
            )
            recipe = identified(selection["recipe"])
            require(
                recipe.get("schema")
                in {
                    "urn:literate-ai:schema:v2:native-sdk-build-recipe",
                    "urn:literate-ai:schema:v2:native-sdk-tree-build-recipe@1",
                }
            )
            layout = recipe["layout"]
            embedded(layout)
            # Only the authored planner's exact four evidence roles are followed;
            # source snapshot archives and arbitrary digest-looking values are not.
            require(
                build_plan.evidence
                == (
                    canonical_identity(selection),
                    canonical_identity(recipe),
                    canonical_identity(layout),
                    admission.index_binding,
                )
            )
            read(admission.index_binding)
            read(build_plan.model_decision)
            read(admission.classification)
            read(admission.authorization)
    return documents
