"""Reopen SDK command evidence without admitting a producer or authorizing a host."""

from __future__ import annotations

from datetime import datetime

from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    verify_qualification_command_authority,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildPlan,
)
from literate_ai.contracts import BlobRef
from literate_ai.contracts.executable_components.commands import (
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandRole,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.native_sdks import NativeSdkSnapshot
from literate_ai.contracts.paths import canonical_relative_posix_path
from literate_ai.contracts.repositories import (
    RepositoryBuildPlan,
    RepositorySourceAdmission,
)
from literate_ai.contracts.sbom import (
    CycloneDxRepositorySourceResolution,
    component_bom_ref,
    repository_dependency_bom_ref,
)
from literate_ai.security import (
    AuthorizationRevocationSet,
    BuildAuthorization,
    BuildRequest,
    SecurityProfile,
)


def _require(value):
    if not value:
        raise QualificationCaptureError("qualification.capture.native-sdk-mismatch")


def _fields(value, names):
    _require(isinstance(value, dict) and set(value) == set(names.split()))


def read_sdk_build_plan(reader, identity):
    try:
        plan = StandardComponentBuildPlan.from_dict(reader.read_json(identity))
        _require(plan.identity == identity)
        return plan
    except QualificationCaptureError:
        raise
    except (TypeError, ValueError, RuntimeError) as exc:
        raise QualificationCaptureError(
            "qualification.capture.native-sdk-plan-invalid"
        ) from exc


def packaged_sdk_process_fields(
    reader,
    *,
    package_plan,
    package_result,
    project_build_plan_identity,
    process,
    phase,
    library_oracle=None,
):
    """Reopen a package-bound SDK process within its expected project build plan."""
    try:
        resources = tuple(
            item for item in package_plan.inputs if item.role == "native-sdk-file"
        )
        if not resources:
            _require("native_sdk_execution_identity" not in process)
            return {}
        execution = reader.read_json(
            ContentIdentity.parse_uri(process["native_sdk_execution_identity"])
        )
        identity = ContentIdentity.from_dict(
            execution["command_binding"]["package_identity"]
        )
        package = reader.read_json(identity)
        has_scope = (
            package.get("schema") == "literate-ai/native-sdk-package-execution@2"
        )
        _fields(
            package,
            "schema package_plan package_result component_build_plan tree_identity"
            + (" execution_scope" if has_scope else ""),
        )
        _require(
            package["schema"]
            in (
                "literate-ai/native-sdk-package-execution@1",
                "literate-ai/native-sdk-package-execution@2",
            )
            and package["package_plan"] == package_plan.identity.to_dict()
            and package["package_result"] == package_result.identity.to_dict()
            and package_result.package_plan_identity == package_plan.identity
        )
        build_identity = ContentIdentity.from_dict(package["component_build_plan"])
        project = reader.read_json(project_build_plan_identity)
        _fields(project, "schema execution_plan_identity components")
        _require(project["schema"] == "literate-ai/standard-project-build-plan@1")
        _require(
            isinstance(project["components"], list)
            and build_identity.uri in project["components"]
        )
        plan = read_sdk_build_plan(reader, build_identity)
        _require(plan.component_revision == package_plan.root_component_revision)
        execution_scope = None
        if has_scope:
            from literate_ai.adapters.native_sdk_package_scope import (
                NativeSdkPackageExecutionScope,
            )

            scope_identity = ContentIdentity.from_dict(package["execution_scope"])
            execution_scope = NativeSdkPackageExecutionScope.from_dict(
                reader.read_json(scope_identity)
            )
            _require(execution_scope.identity == scope_identity)
            execution_scope.require_package(package_plan)
            _require(execution_scope.plan_for(plan.component_revision) == plan)
            for owner_plan in execution_scope.plans:
                _require(owner_plan.identity.uri in project["components"])
                _require(read_sdk_build_plan(reader, owner_plan.identity) == owner_plan)
                verify_qualification_command_authority(
                    reader,
                    contract=execution_scope.contract_for(
                        owner_plan.component_revision
                    ),
                    authorization_identity=owner_plan.request.authorization_identity,
                    plan=owner_plan,
                )
        tree = reader.read_json(ContentIdentity.from_dict(package["tree_identity"]))
        _fields(tree, "schema files")
        _require(
            tree["schema"] == "literate-ai/local-source-tree@1"
            and isinstance(tree["files"], list)
        )
        files = {}
        for item in tree["files"]:
            _fields(item, "path sha256")
            canonical_relative_posix_path(item["path"], label="package file")
            _require(item["path"] not in files)
            files[item["path"]] = item["sha256"]
        _require(all(files.get(item.path) == item.blob.digest for item in resources))
        manifest = reader.read_json(
            ContentIdentity.from_dict(execution["manifest_identity"])
        )
        _require(
            {item.source_identity for item in resources}
            == {
                ContentIdentity.from_dict(item["input_identity"])
                for item in manifest["imports"]
            }
        )
        for item in manifest["imports"]:
            binding = ContentIdentity.from_dict(item["input_identity"])
            snapshot = NativeSdkSnapshot.from_dict(item["snapshot"])
            retained = {
                item.path: item for item in resources if item.source_identity == binding
            }
            prefix = f"native-sdks/{binding.digest}/sdk/"
            _require(set(retained) == {prefix + item.path for item in snapshot.files})
            for item in snapshot.files:
                resource = retained[prefix + item.path]
                _require(
                    resource.blob == item.blob
                    and resource.executable == item.executable
                )
                _require(
                    resource.target_identity
                    == snapshot.target_identity
                    == package_plan.target_identity
                )
        return sdk_process_fields(
            reader,
            plan=plan,
            process=process,
            phase=phase,
            package_identity=identity,
            library_oracle=library_oracle,
            execution_scope=execution_scope,
        )
    except QualificationCaptureError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise QualificationCaptureError(
            "qualification.capture.packaged-sdk-invalid"
        ) from exc


def _dependencies(document, binding, identity, snapshot, revision):
    _fields(document, "schema input_identity repository_resolution components edges")
    _require(document["schema"] == "literate-ai/native-sdk-dependency-evidence@1")
    _require(document["input_identity"] == identity.to_dict())
    resolution = CycloneDxRepositorySourceResolution.from_dict(
        document["repository_resolution"]
    )
    admission = RepositorySourceAdmission.from_dict(binding["source_admission"])
    _require(
        resolution.admission_identity == admission.identity
        and resolution.source_lock_identity
        == snapshot.source_lock_identity
        == admission.source_lock
        and resolution.source_snapshot_identity == admission.source_snapshot
        and resolution.source_tree_identity == admission.source_tree
        and resolution.index_identity == admission.index_binding
    )
    _require(
        isinstance(document["components"], list) and isinstance(document["edges"], list)
    )
    components = {}
    sdk_paths = set()
    files = {item.path: item for item in snapshot.files}
    for item in document["components"]:
        ref = item["bom-ref"]
        _require(isinstance(ref, str) and ref and ref not in components)
        components[ref] = item
        properties = item.get("properties", [])
        _require(isinstance(properties, list))
        for prop in properties:
            _fields(prop, "name value")
            _require(isinstance(prop["name"], str) and isinstance(prop["value"], str))
        paths = [
            prop["value"]
            for prop in properties
            if prop["name"] == "literate-ai:native-sdk-relative-path"
        ]
        _require(len(paths) <= 1)
        if paths:
            path = paths[0]
            _require(path in files and path not in sdk_paths)
            sdk_paths.add(path)
            image = canonical_identity(
                {
                    "sdk": snapshot.identity.to_dict(),
                    "path": path,
                    "blob": files[path].blob.to_dict(),
                }
            )
            _require(ref == f"urn:literate-ai:native-sdk-image:{image.digest}")
            _require(
                {"alg": "SHA-256", "content": files[path].blob.digest}
                in item.get("hashes", [])
            )
    package_ref = f"urn:literate-ai:native-sdk:{identity.digest}"
    package = components[package_ref]
    expected = {
        "input": identity,
        "snapshot": snapshot.identity,
        "recipe": snapshot.recipe_identity,
        "target": snapshot.target_identity,
        "license": snapshot.license_identity,
        "import-surface": snapshot.import_surface.identity,
        "source-lock": admission.source_lock,
        "source-admission": admission.identity,
        "source-resolution": resolution.identity,
    }
    _require(
        package["name"] == snapshot.import_surface.package
        and package["version"] == snapshot.identity.uri
    )
    for key, value in expected.items():
        _require(
            {"name": f"literate-ai:native-sdk-{key}", "value": value.uri}
            in package["properties"]
        )
    _require(
        package
        == {
            "type": "library",
            "bom-ref": package_ref,
            "name": snapshot.import_surface.package,
            "version": snapshot.identity.uri,
            "hashes": [{"alg": "SHA-256", "content": snapshot.identity.digest}],
            "properties": [
                {"name": "literate-ai:dependency-kind", "value": "runtime"},
                {"name": "literate-ai:dependency-scope", "value": "runtime"},
                *(
                    {"name": f"literate-ai:native-sdk-{key}", "value": value.uri}
                    for key, value in sorted(expected.items())
                ),
            ],
        }
    )
    _require(set(snapshot.native_libraries).issubset(sdk_paths))
    source_ref = repository_dependency_bom_ref(resolution.dependency_identity)
    consumer_ref = component_bom_ref(revision)
    refs = {*components, source_ref, consumer_ref}
    edges = set()
    for edge in document["edges"]:
        _require(
            isinstance(edge, list)
            and len(edge) == 2
            and all(isinstance(ref, str) and ref in refs for ref in edge)
        )
        _require(tuple(edge) not in edges)
        _require(edge[0] != source_ref and edge[1] != consumer_ref)
        _require(edge[1] != package_ref or edge[0] == consumer_ref)
        edges.add(tuple(edge))
    _require(
        (consumer_ref, package_ref) in edges and (package_ref, source_ref) in edges
    )
    reached, pending = set(), [consumer_ref]
    adjacency = {ref: set() for ref in refs}
    for source, target in edges:
        adjacency[source].add(target)
    while pending:
        current = pending.pop()
        if current not in reached:
            reached.add(current)
            pending.extend(adjacency[current] - reached)
    _require(reached == refs)


def sdk_process_fields(
    reader,
    *,
    plan,
    process,
    phase,
    entrypoint_identity=None,
    command_identity=None,
    package_identity=None,
    library_oracle=None,
    execution_scope=None,
):
    """Validate an SDK extension and return its exact additional process fields.

    Historical state consistency does not authenticate a worker's observations or
    substitute for importer admission and fresh execution-time host policy.
    """
    try:
        compile_only = "native_sdk_build_identity" in process
        field = (
            "native_sdk_build_identity"
            if compile_only
            else "native_sdk_execution_identity"
        )
        _require(
            not compile_only
            or (phase == "build" and "native_sdk_execution_identity" not in process)
        )
        if field not in process:
            _require(not plan.materialization.native_sdk_input_identities)
            _require(execution_scope is None or not execution_scope.input_identities)
            return {}
        identity = ContentIdentity.parse_uri(process[field])
        value = reader.read_json(identity)
        _require(isinstance(value, dict))
        _require(
            (value.get("schema") == "literate-ai/native-sdk-consumer-build@1")
            == compile_only
        )
        linked = value.get("schema") == "literate-ai/native-sdk-command-execution@3"
        if linked:
            from literate_ai.adapters.native_sdk_package_scope import (
                NativeSdkPackageExecutionScope,
            )

            _require(execution_scope is None and package_identity is None)
            execution_scope = NativeSdkPackageExecutionScope.from_dict(
                value["execution_scope"]
            )
            _require(execution_scope.execution_revision == plan.component_revision)
        inputs = (
            plan.materialization.native_sdk_input_identities
            if execution_scope is None
            else execution_scope.input_identities
        )
        _require(bool(inputs))
        if execution_scope is not None:
            _require(linked or package_identity is not None)
            _require(execution_scope.plan_for(plan.component_revision) == plan)
        _fields(
            value,
            "schema phase command_binding request authorization runtime_identity "
            "manifest_identity dependencies checks"
            + (" execution_scope" if linked else ""),
        )
        _require(
            value["schema"]
            in (
                "literate-ai/native-sdk-command-execution@2",
                "literate-ai/native-sdk-command-execution@3",
                "literate-ai/native-sdk-consumer-build@1",
            )
            and value["phase"] == phase
        )
        scope = value["command_binding"]
        _fields(
            scope,
            "phase command_identity command_contract_identity entrypoint_identity "
            "manifest runtime"
            + (
                ""
                if compile_only
                else " argv_identity cwd_identity environment_identity"
            )
            + (" package_identity" if package_identity is not None else "")
            + (" execution_scope_identity" if linked else ""),
        )
        if linked:
            _require(
                scope["execution_scope_identity"] == execution_scope.identity.to_dict()
            )
            for owner_plan, owner_contract in zip(
                execution_scope.plans, execution_scope.contracts, strict=True
            ):
                verify_qualification_command_authority(
                    reader,
                    contract=owner_contract,
                    authorization_identity=owner_plan.request.authorization_identity,
                    plan=owner_plan,
                )
        if package_identity is not None:
            _require(scope["package_identity"] == package_identity.to_dict())
        _require(
            scope["phase"] == phase
            and scope["manifest"] == value["manifest_identity"]
            and scope["runtime"] == value["runtime_identity"]
        )
        if not compile_only:
            for key in ("argv_identity", "cwd_identity", "environment_identity"):
                ContentIdentity.from_dict(scope[key])
        contract = ComponentCommandContract.from_dict(
            reader.read_json(
                ContentIdentity.from_dict(scope["command_contract_identity"])
            )
        )
        verify_qualification_command_authority(
            reader,
            contract=contract,
            authorization_identity=plan.request.authorization_identity,
            plan=plan,
        )
        if linked:
            _require(execution_scope.contract_for(plan.component_revision) == contract)
        expected_entrypoint = (
            None if entrypoint_identity is None else entrypoint_identity.to_dict()
        )
        _require(scope["entrypoint_identity"] == expected_entrypoint)
        selected = contract
        if entrypoint_identity is not None:
            selected = next(
                item
                for item in contract.entrypoint_command_contracts()
                if item.entrypoint_identity == entrypoint_identity
            )
        else:
            _require(compile_only or not contract.is_multi_entrypoint)
        if library_oracle is not None:
            from literate_ai.adapters.native_sdk_library import (
                library_acceptance_command,
            )

            _require(phase == "library-acceptance" and package_identity is not None)
            _require(entrypoint_identity is None and command_identity is None)
            command_document = library_acceptance_command(contract, library_oracle)
            command_id = canonical_identity(command_document)
            _require(scope["command_identity"] == command_id.to_dict())
            _require(reader.read_json(command_id) == command_document)
            expected_toolchain = contract.library_acceptance_toolchain_identity
        else:
            stage = ComponentCommandPhase(phase)
            command = selected.command(stage)
            _require(scope["command_identity"] == command.identity.to_dict())
            _require(command_identity is None or command_identity == command.identity)
            _require(
                (ComponentCommandRole.NATIVE_SDK_INPUTS in command.roles)
                != compile_only
            )
            expected_toolchain = selected.tool_binding(stage).toolchain_identity
        manifest = reader.read_json(
            ContentIdentity.from_dict(value["manifest_identity"])
        )
        _fields(manifest, "schema consumer_revision target_identity imports")
        _require(manifest["schema"] == "literate-ai/native-sdk-execution-inputs@1")
        _require(manifest["consumer_revision"] == plan.component_revision.to_dict())
        target = contract.artifact_export.target_identity
        _require(
            manifest["target_identity"] == target.to_dict()
            and isinstance(manifest["imports"], list)
        )
        _require(
            isinstance(value["dependencies"], list)
            and len(value["dependencies"]) == len(inputs)
        )
        dependency_map = {
            ContentIdentity.from_dict(item["input_identity"]): item
            for item in value["dependencies"]
        }
        _require(
            set(dependency_map) == set(inputs)
            and len(manifest["imports"]) == len(inputs)
        )
        ordered, roots, namespaces = [], set(), set()
        for item in manifest["imports"]:
            _fields(item, "root input_identity snapshot")
            root = canonical_relative_posix_path(
                item["root"], label="SDK execution root"
            )
            _require(
                root not in roots
                and not any(
                    root in other.parents or other in root.parents for other in roots
                )
            )
            roots.add(root)
            input_identity = ContentIdentity.from_dict(item["input_identity"])
            _require(input_identity in inputs and input_identity not in ordered)
            ordered.append(input_identity)
            binding = reader.read_json(input_identity)
            _fields(
                binding,
                "schema consumer_revision target_identity dependency_id selection "
                "source_admission sdk_snapshot runtime_observation "
                "build_result import_surface",
            )
            _require(binding["schema"] == "literate-ai/native-sdk-consumer-input@1")
            owner_revision = ContentIdentity.from_dict(binding["consumer_revision"])
            owner_plan = (
                plan
                if execution_scope is None
                else execution_scope.plan_for(owner_revision)
            )
            _require(
                owner_plan.component_revision == owner_revision
                and input_identity
                in owner_plan.materialization.native_sdk_input_identities
                and binding["target_identity"] == manifest["target_identity"]
            )
            snapshot = NativeSdkSnapshot.from_dict(item["snapshot"])
            snapshot_ref = BlobRef.from_dict(binding["sdk_snapshot"])
            payload = canonical_json_bytes(snapshot.to_dict())
            _require(
                snapshot_ref.size == len(payload)
                and snapshot_ref.identity == canonical_identity(snapshot.to_dict()).uri
            )
            _require(
                snapshot.target_identity == target
                and snapshot.import_surface.to_dict() == binding["import_surface"]
            )
            namespace = (
                snapshot.import_surface.language,
                snapshot.import_surface.package,
            )
            _require(namespace not in namespaces)
            namespaces.add(namespace)
            admission = RepositorySourceAdmission.from_dict(binding["source_admission"])
            _require(
                admission.source_lock == snapshot.source_lock_identity
                and admission.dependency_id == binding["dependency_id"]
            )
            _require(
                admission.build_result.uri
                == BlobRef.from_dict(binding["build_result"]).identity
            )
            _require(
                snapshot.identity
                in tuple(item.identity for item in admission.build_outputs)
            )
            BlobRef.from_dict(binding["runtime_observation"])
            selection = reader.read_json(
                ContentIdentity.from_dict(binding["selection"])
            )
            _fields(
                selection,
                "schema component_revision target_identity flavor_revision "
                "content source_lock recipe",
            )
            _require(selection["schema"] == "literate-ai/native-sdk-recipe-selection@1")
            _require(
                selection["component_revision"] == owner_revision.to_dict()
                and selection["target_identity"] == target.to_dict()
                and selection["source_lock"] == snapshot.source_lock_identity.to_dict()
            )
            source_plan = RepositoryBuildPlan.from_dict(
                reader.read_json(admission.build_plan)
            )
            _require(
                source_plan.identity == snapshot.recipe_identity == admission.build_plan
                and source_plan.source_lock == admission.source_lock
                and source_plan.effective_revision == owner_revision
                and source_plan.flavor_set == target
                and ContentIdentity.from_dict(binding["selection"])
                in source_plan.evidence
                and ContentIdentity.from_dict(selection["recipe"])
                in source_plan.evidence
            )
            dependency = dependency_map[input_identity]
            _require(reader.read_json(canonical_identity(dependency)) == dependency)
            _dependencies(dependency, binding, input_identity, snapshot, owner_revision)
        runtime = canonical_identity(
            {
                "consumer": plan.component_revision.to_dict(),
                "target": target.to_dict(),
                "inputs": [item.to_dict() for item in ordered],
                "dependencies": [
                    canonical_identity(dependency_map[item]).to_dict()
                    for item in ordered
                ],
            }
        )
        _require(runtime.to_dict() == value["runtime_identity"])
        request = BuildRequest.from_dict(value["request"])
        grant = BuildAuthorization.from_dict(value["authorization"])
        if compile_only:
            _require(package_identity is None and execution_scope is None)
            _require(
                value["authorization"]
                == reader.read_json(plan.request.authorization_identity)
                and request.effective_revision_digest == plan.component_revision.uri
                and request.toolchain_digest
                == plan.request.language_compiler_identity.uri
                and request.sandbox_profile == "local-explicit-host-process"
                and request.requested_privileges
                == grant.privileges
                == ("execute-build-tools",)
                and set(request.allowed_outputs)
                == {item.export_id for item in plan.manifest.export_declarations}
                and grant.profile is not SecurityProfile.BLOCKED
            )
        else:
            _require(request.builder_id == canonical_identity(scope).uri)
            _require(
                request.effective_revision_digest == plan.component_revision.uri
                and request.source_bundle_digest
                == plan.request.source_tree_identity.uri
            )
            _require(request.toolchain_digest == expected_toolchain.uri)
            _require(
                request.sandbox_profile == "local-explicit-native-sdk"
                and request.allowed_outputs == ("stdout", "stderr")
            )
            _require(
                request.requested_privileges
                == grant.privileges
                == ("execute-native-sdk",)
                and grant.profile is not SecurityProfile.BLOCKED
            )
            _require(grant.classification_digest == runtime.uri)
        _require(
            reader.read_json(canonical_identity(value["request"])) == value["request"]
        )
        _require(
            reader.read_json(canonical_identity(value["authorization"]))
            == value["authorization"]
        )
        checks = value["checks"]
        _require(isinstance(checks, list) and len(checks) == 2)
        previous = None
        for check in checks:
            _fields(check, "checked_at revocations")
            at = datetime.fromisoformat(check["checked_at"])
            _require(at.tzinfo is not None and (previous is None or previous <= at))
            previous = at
            _require(
                isinstance(check["revocations"], list)
                and len(check["revocations"]) == len(inputs)
            )
            checked_inputs = []
            for revocation in check["revocations"]:
                _fields(revocation, "input_identity state")
                checked_inputs.append(
                    ContentIdentity.from_dict(revocation["input_identity"])
                )
                AuthorizationRevocationSet.from_dict(
                    revocation["state"]
                ).require_build_valid(grant, request, now=at)
            _require(checked_inputs == ordered)
        return {field: identity.uri}
    except QualificationCaptureError:
        raise
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        RuntimeError,
        StopIteration,
    ) as exc:
        raise QualificationCaptureError(
            "qualification.capture.native-sdk-invalid"
        ) from exc
