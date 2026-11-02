"""Join retained SDK historical facts without creating producer/runtime authority.

Producer build expiry/revocation checks were not recorded in old build results.
These assertions never replace the authenticated worker's genuine execution and
live SDK/LINK checks, nor grant a controller permission to run anything.
"""

from collections import defaultdict
from pathlib import PurePosixPath, PureWindowsPath

from literate_ai.adapters.builders.python import UNSANDBOXED_HOST_BUILD_PRIVILEGES
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthoritySnapshot,
)
from literate_ai.adapters.native_sdk_dependencies import (
    project_native_sdk_dependency_assertions,
)
from literate_ai.adapters.native_sdk_qualification import (
    _dependencies,
    verify_historical_completed_sdk_check,
    verify_historical_sdk_authority_checks,
)
from literate_ai.adapters.native_sdk_recipes import select_native_sdk_recipes
from literate_ai.adapters.retained_project_execution import (
    RetainedProjectCommandObservation,
)
from literate_ai.adapters.retained_project_sdks import (
    read_retained_sdk_policy,
    require_retained_sdk_product_placement,
)
from literate_ai.adapters.retained_sdk_command_context import (
    EXECUTION_SCHEMA,
    verify_retained_sdk_command_context,
)
from literate_ai.adapters.retained_sdk_runtime_context import (
    verify_historical_retained_sdk_runtime_context,
)
from literate_ai.application.repository_sources import RepositoryBuildApproval
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.native_sdk_tree_products import (
    NativeSdkTreeBuildRecipe,
    NativeSdkTreeSnapshot,
)
from literate_ai.contracts.native_sdks import NativeSdkBuildRecipe, NativeSdkSnapshot
from literate_ai.contracts.repositories import (
    RepositoryBuildPlan,
    RepositorySourceAdmission,
)
from literate_ai.contracts.retained_project_sdks import (
    RetainedProjectSdkSelectionPolicy,
)
from literate_ai.contracts.sbom import (
    ManagedComponentKind,
    project_component_lock_managed_graph,
)
from literate_ai.security import BuildAuthorization, BuildRequest, SecurityProfile


def _verify_runtime_assertions(runtime, snapshot, layout, require, step):
    """Reopen the recorded native graph; paths remain historical observations."""
    components = runtime["components"]
    edges = runtime["edges"]
    libraries = runtime["native_libraries"]
    require(
        type(components) is list and type(edges) is list and type(libraries) is list
    )
    by_ref = {}
    for component in components:
        step()
        require(type(component) is dict and type(component.get("bom-ref")) is str)
        key = component["bom-ref"]
        require(bool(key) and key not in by_ref and key != snapshot.identity.uri)
        by_ref[key] = component
    graph = {key: set() for key in (*by_ref, snapshot.identity.uri)}
    for edge in edges:
        step()
        require(
            type(edge) is list
            and len(edge) == 2
            and all(type(key) is str for key in edge)
            and edge[0] in graph
            and edge[1] in by_ref
            and edge[1] not in graph[edge[0]]
        )
        graph[edge[0]].add(edge[1])
    pending, reached = [snapshot.identity.uri], set()
    while pending:
        step()
        current = pending.pop()
        if current not in reached:
            reached.add(current)
            pending.extend(graph[current] - reached)
    require(reached == set(graph) and len(libraries) == len(snapshot.native_libraries))
    physical = (
        snapshot.tree.resolve_all(snapshot.native_libraries)
        if isinstance(snapshot, NativeSdkTreeSnapshot)
        else snapshot.native_libraries
    )
    files = {item.path: item.blob for item in snapshot.files}
    path_type = (
        PureWindowsPath if layout.operating_system == "windows" else PurePosixPath
    )
    path_property = {
        "windows": "literate-ai:pe-path",
        "macos": "literate-ai:macho-path",
        "linux": "literate-ai:elf-path",
        "freebsd": "literate-ai:elf-path",
    }[layout.operating_system]
    roots = set()
    for logical, relative, observed in zip(
        snapshot.native_libraries, physical, libraries, strict=True
    ):
        step()
        require(
            type(observed) is dict
            and set(observed)
            == (
                {"path", "component", "physical_path"}
                if isinstance(snapshot, NativeSdkTreeSnapshot)
                else {"path", "component"}
            )
        )
        require(observed["path"] == logical and observed["component"] in by_ref)
        if isinstance(snapshot, NativeSdkTreeSnapshot):
            require(observed["physical_path"] == relative)
        component = by_ref[observed["component"]]
        properties = component.get("properties", [])
        require(type(properties) is list)
        for prop in properties:
            require(
                type(prop) is dict
                and set(prop) == {"name", "value"}
                and type(prop["name"]) is str
                and type(prop["value"]) is str
            )
        paths = [prop["value"] for prop in properties if prop["name"] == path_property]
        require(len(paths) == 1)
        path, suffix = path_type(paths[0]), PurePosixPath(relative)
        require(
            path.is_absolute()
            and ".." not in path.parts
            and tuple(path.parts[-len(suffix.parts) :]) == suffix.parts
            and {"alg": "SHA-256", "content": files[relative].digest}
            in component.get("hashes", [])
        )
        roots.add(path_type(*path.parts[: -len(suffix.parts)]))
    require(len(roots) <= 1)


def verify_retained_sdk_run_assertions(
    reader,
    inputs,
    run,
    documents,
    *,
    maximum_steps,
    expected_platform=None,
    expected_architecture=None,
    current_sdk_snapshots=(),
):
    """Check exact locked SDK closure, every command and its final authority check."""
    if (
        type(maximum_steps) is not int
        or not 1 <= maximum_steps <= 16_000_000
        or expected_platform not in {None, "freebsd", "linux", "macos", "windows"}
    ):
        raise ValueError("retained.sdk.bounded-historical-inspection-required")
    remaining = maximum_steps

    def require(value):
        if not value:
            raise ValueError("retained.sdk.historical-run-scope-differs")

    def step():
        nonlocal remaining
        remaining -= 1
        require(remaining >= 0)
        reader.guard()

    def fields(value, names):
        require(type(value) is dict and set(value) == set(names.split()))

    def identity(value):
        return ContentIdentity.from_dict(value)

    def read(key):
        step()
        require(key in documents)
        return documents[key]

    def blob(value):
        reference = BlobRef.from_dict(value)
        require(reader.reference(reference.identity) == reference)
        return read(ContentIdentity.parse_uri(reference.identity))

    plan, manifest = inputs.plan, inputs.source_manifest
    step()
    if not any(member.role == "sdk-policy" for member in manifest.members):
        require(
            not documents
            and not run.resolved_bom.sdk_dependencies
            and run.native_sdk_final_check_identity is None
        )
        return
    require(
        type(current_sdk_snapshots) is tuple
        and len(current_sdk_snapshots) == len(inputs.component_locks)
        and all(
            isinstance(snapshot, LockedGenerationAuthoritySnapshot)
            for snapshot in current_sdk_snapshots
        )
    )
    authored = {}
    for snapshot, lock in zip(
        current_sdk_snapshots, inputs.component_locks, strict=True
    ):
        step()
        snapshot.require_unchanged()
        require(snapshot.authority.lock == lock)
        for node in lock.nodes:
            if node.revision.repository_sources:
                for selection in select_native_sdk_recipes(
                    snapshot, node.revision.identity
                ):
                    step()
                    authored[lock.identity, selection.identity] = selection
    policy = read_retained_sdk_policy(inputs.source, manifest, plan.component_locks)
    by_input = {
        key: value
        for key, value in documents.items()
        if value.get("schema") == "literate-ai/native-sdk-consumer-input@1"
    }
    by_selection = defaultdict(list)
    for key, value in by_input.items():
        step()
        by_selection[
            (
                identity(value["selection"]),
                BlobRef.from_dict(value["sdk_snapshot"]).identity,
            )
        ].append(key)
    locks = {lock.identity: lock for lock in inputs.component_locks}
    nodes = {
        lock.identity: {node.revision.identity: node for node in lock.nodes}
        for lock in inputs.component_locks
    }
    selected = {}
    for row in policy.bindings:
        step()
        require(row.component_lock_identity in locks)
        if isinstance(policy, RetainedProjectSdkSelectionPolicy):
            candidates = by_selection[
                (row.selection_identity, row.snapshot_identity.uri)
            ]
            filtered = []
            for key in candidates:
                step()
                if (
                    identity(by_input[key]["consumer_revision"])
                    in nodes[row.component_lock_identity]
                ):
                    filtered.append(key)
            candidates = filtered
            require(len(candidates) == 1)
            key = candidates[0]
        else:
            key = row.input_identity
            require(key in by_input)
        binding = by_input[key]
        fields(
            binding,
            "schema consumer_revision target_identity "
            "dependency_id selection source_admission sdk_snapshot "
            "runtime_observation build_result import_surface",
        )
        revision, target = (
            identity(binding["consumer_revision"]),
            identity(binding["target_identity"]),
        )
        require(revision in nodes[row.component_lock_identity])
        node = nodes[row.component_lock_identity][revision]
        require(target == node.target_flavor_selection.identity)
        sources = {
            source.dependency.dependency_id: source
            for source in node.revision.repository_sources
        }
        require(binding["dependency_id"] in sources)
        source_lock = sources[binding["dependency_id"]]
        snapshot_document = blob(binding["sdk_snapshot"])
        snapshot_type = (
            NativeSdkTreeSnapshot
            if snapshot_document.get("schema") == NativeSdkTreeSnapshot.SCHEMA
            else NativeSdkSnapshot
        )
        snapshot = snapshot_type.from_dict(snapshot_document)
        admission = RepositorySourceAdmission.from_dict(binding["source_admission"])
        require(
            admission.source_lock
            == source_lock.identity
            == snapshot.source_lock_identity
            and admission.source_snapshot == source_lock.source_snapshot
            and admission.source_tree == source_lock.source_tree
            and admission.effective_revision == revision
            and admission.flavor_set == target
            and admission.dependency_id == binding["dependency_id"]
            and snapshot.target_identity == target
            and snapshot.import_surface.to_dict() == binding["import_surface"]
            and admission.build_result.uri
            == BlobRef.from_dict(binding["build_result"]).identity
        )
        selection = read(identity(binding["selection"]))
        fields(
            selection,
            "schema component_revision target_identity "
            "flavor_revision content source_lock recipe",
        )
        require(
            selection["schema"] == "literate-ai/native-sdk-recipe-selection@1"
            and identity(selection["component_revision"]) == revision
            and identity(selection["target_identity"]) == target
            and identity(selection["source_lock"]) == source_lock.identity
            and identity(selection["flavor_revision"])
            in node.revision.selected_flavor_revisions
        )
        authored_selection = authored.get(
            (row.component_lock_identity, identity(binding["selection"]))
        )
        require(
            authored_selection is not None and selection == authored_selection.to_dict()
        )
        recipe_document = read(identity(selection["recipe"]))
        require(recipe_document == authored_selection.recipe.to_dict())
        recipe_type = (
            NativeSdkTreeBuildRecipe
            if isinstance(snapshot, NativeSdkTreeSnapshot)
            else NativeSdkBuildRecipe
        )
        recipe = recipe_type.from_dict(recipe_document)
        layout = recipe.layout
        require(
            recipe.dependency_id == binding["dependency_id"]
            and snapshot.license_identity == layout.license_identity
            and snapshot.import_surface == layout.import_surface
            and snapshot.import_root == layout.import_root
            and snapshot.native_libraries == layout.native_libraries
        )
        if expected_platform is not None:
            require(layout.operating_system == expected_platform)
        if expected_architecture is not None:
            require(layout.architecture == expected_architecture)
        source_plan = RepositoryBuildPlan.from_dict(read(admission.build_plan))
        require(
            source_plan.identity == snapshot.recipe_identity == admission.build_plan
            and source_plan.source_lock == source_lock.identity
            and source_plan.effective_revision == revision
            and source_plan.flavor_set == target
            and source_plan.commands == recipe.commands
            and source_plan.expected_outputs
            == admission.expected_outputs
            == (layout.sdk_root,)
            and source_plan.evidence
            == (
                identity(binding["selection"]),
                recipe.identity,
                layout.identity,
                admission.index_binding,
            )
            and tuple(
                (output.path, output.identity) for output in admission.build_outputs
            )
            == ((layout.sdk_root, snapshot.identity),)
        )
        require(read(layout.identity) == layout.to_dict())
        decision = read(source_plan.model_decision)
        require(
            decision
            == {
                "schema": "literate-ai/authored-native-sdk-build-decision@1",
                "selection": identity(binding["selection"]).to_dict(),
                "recipe": recipe.identity.to_dict(),
                "coding_provider_invoked": False,
            }
        )
        index = read(admission.index_binding)
        fields(index, "schema source_snapshot source_tree provider index")
        require(
            index["schema"]
            == "urn:literate-ai:schema:v1:repository-source-index-binding"
            and identity(index["source_snapshot"]) == source_lock.source_snapshot
            and identity(index["source_tree"]) == source_lock.source_tree
        )
        identity(index["provider"])
        identity(index["index"])
        source_grant = BuildAuthorization.from_dict(read(admission.authorization))
        require(
            source_grant.profile is not SecurityProfile.BLOCKED
            and source_grant.classification_digest == admission.classification.uri
            and source_grant.effective_revision_digest == revision.uri
            and source_grant.privileges == UNSANDBOXED_HOST_BUILD_PRIVILEGES
        )
        read(
            admission.classification
        )  # A terminal assertion, not underlying scan bytes.
        result = blob(binding["build_result"])
        fields(result, "schema plan approval snapshot runtime_observation commands")
        require(
            result["schema"] == "literate-ai/native-sdk-repository-build@1"
            and identity(result["plan"]) == source_plan.identity
            and identity(result["approval"])
            == RepositoryBuildApproval(
                admission.source_lock,
                admission.build_plan,
                admission.classification,
                admission.authorization,
            ).identity
            and result["snapshot"] == binding["sdk_snapshot"]
            and result["runtime_observation"] == binding["runtime_observation"]
            and type(result["commands"]) is list
            and len(result["commands"]) == len(source_plan.commands)
        )
        for command, observed in zip(
            source_plan.commands, result["commands"], strict=True
        ):
            step()
            fields(observed, "step_id returncode stdout stderr")
            require(
                observed["step_id"] == command.step_id
                and type(observed["returncode"]) is int
                and observed["returncode"] == 0
            )
            for stream in ("stdout", "stderr"):
                require(BlobRef.from_dict(observed[stream]).size <= 1024**2)
        runtime = blob(binding["runtime_observation"])
        fields(
            runtime,
            "schema snapshot_identity target_identity "
            "operating_system architecture native_libraries "
            "components edges",
        )
        require(
            runtime["schema"]
            == (
                "literate-ai/native-sdk-tree-runtime-observation@1"
                if isinstance(snapshot, NativeSdkTreeSnapshot)
                else "literate-ai/native-sdk-runtime-observation@1"
            )
            and identity(runtime["snapshot_identity"]) == snapshot.identity
            and identity(runtime["target_identity"]) == target
            and runtime["operating_system"] == layout.operating_system
            and runtime["architecture"] == layout.architecture
        )
        _verify_runtime_assertions(runtime, snapshot, layout, require, step)
        selected[row.component_lock_identity, key] = (
            row.prefix,
            snapshot,
            recipe,
            revision,
            target,
            binding,
        )
    require({key for _lock, key in selected} == set(by_input))
    # Share the exact file/alias/directory join with the live retained owner.
    require_retained_sdk_product_placement(
        manifest, tuple((value[0], value[1]) for value in selected.values())
    )
    dependencies = {
        value.input_identity: value for value in run.resolved_bom.sdk_dependencies
    }
    require(set(dependencies) == {key for _lock, key in selected})
    for (_lock, key), (
        _prefix,
        snapshot,
        _recipe,
        revision,
        _target,
        binding,
    ) in selected.items():
        step()
        _dependencies(dependencies[key].to_dict(), binding, key, snapshot, revision)
        cache = read(dependencies[key].repository_resolution.cache_record_identity)
        fields(
            cache,
            "schema admission admission_identity capture_manifest "
            "source_snapshot source_tree "
            "materialized_source_intelligence source_intelligence_status",
        )
        admission = RepositorySourceAdmission.from_dict(binding["source_admission"])
        require(
            cache["schema"]
            == "urn:literate-ai:schema:v2:repository-source-cache-record"
            and cache["admission"] == admission.to_dict()
            and identity(cache["admission_identity"]) == admission.identity
            and identity(cache["source_snapshot"]) == admission.source_snapshot
            and identity(cache["source_tree"]) == admission.source_tree
        )
        BlobRef.from_dict(cache["capture_manifest"])
    groups = []
    by_group = defaultdict(list)
    by_lock = defaultdict(set)
    group_keys = defaultdict(set)
    for (lock_identity, key), value in selected.items():
        step()
        by_group[lock_identity, value[3], value[4]].append((key, value))
        group_keys[lock_identity].add((value[3], value[4]))
        require((value[3], value[5]["dependency_id"]) not in by_lock[lock_identity])
        by_lock[lock_identity].add((value[3], value[5]["dependency_id"]))
    for lock in inputs.component_locks:
        step()
        graph = project_component_lock_managed_graph(lock, lock.root_revision)
        closure = {
            item.identity
            for item in graph.components
            if item.kind in (ManagedComponentKind.ROOT, ManagedComponentKind.COMPONENT)
        }
        expected = {
            (node.revision.identity, source.dependency.dependency_id)
            for node in lock.nodes
            if node.revision.identity in closure
            for source in node.revision.repository_sources
        }
        require(by_lock[lock.identity] == expected)
        identities = sorted(
            group_keys[lock.identity], key=lambda pair: (pair[0].uri, pair[1].uri)
        )
        for revision, target in identities:
            values = sorted(
                by_group[lock.identity, revision, target],
                key=lambda pair: identity(pair[1][5]["selection"]).uri,
            )
            namespaces = [
                (value[1].import_surface.language, value[1].import_surface.package)
                for _key, value in values
            ]
            require(len(set(namespaces)) == len(namespaces))
            ordered = tuple(key for key, _value in values)
            runtime_identity = canonical_identity(
                {
                    "consumer": revision.to_dict(),
                    "target": target.to_dict(),
                    "inputs": [key.to_dict() for key in ordered],
                    "dependencies": [
                        dependencies[key].identity.to_dict() for key in ordered
                    ],
                }
            )
            groups.append((revision, target, values, ordered, runtime_identity))
    require(
        tuple(
            dependencies[key]
            for _revision, _target, _values, ordered, _runtime in groups
            for key in ordered
        )
        == run.resolved_bom.sdk_dependencies
    )
    inputs_identity = canonical_identity(
        {
            "schema": "literate-ai/retained-project-sdk-inputs@1",
            "plan_identity": plan.identity.uri,
            "policy_identity": policy.identity.uri,
            "inputs": [
                runtime.uri for _revision, _target, _values, _ordered, runtime in groups
            ],
            "lifecycle_receipt": False,
        }
    )
    final_scopes = []
    anchor = None
    canonical_inputs_anchor = None
    tool_identities = {name: value for name, _path, value in plan.profile.tools}
    cases = {case.identity: case for case in run.acceptance_policy.cases}
    cases_by_action = defaultdict(list)
    accepted = {
        observation.case_identity: observation
        for observation in run.acceptance_observations
    }
    for case in run.acceptance_policy.cases:
        cases_by_action[case.action_id].append(case)
    stage_manifests = {
        observation.action.action_id: observation.output_manifest
        for observation in run.observations
    }

    def chronology():
        for observation in run.observations:
            yield observation
            if observation.action.stage == "accept":
                for case in cases_by_action[observation.action.action_id]:
                    yield accepted[case.identity]

    for observation in chronology():
        step()
        ordinary = isinstance(observation, RetainedProjectCommandObservation)
        if ordinary:
            require(observation.native_sdk_inputs_identity == inputs_identity)
            command_identity = canonical_identity(observation.action.to_dict())
            phase, expected_tool = (
                observation.action.stage,
                tool_identities[observation.action.tool],
            )
            stage_manifest = observation.output_manifest
        else:
            case = cases[observation.case_identity]
            command_identity, phase = observation.case_identity, "accept"
            stage_manifest = stage_manifests[case.action_id]
            require(stage_manifest is not None)
            executable = next(
                (
                    member
                    for member in stage_manifest.members
                    if member.path == case.executable
                ),
                None,
            )
            require(executable is not None)
            expected_tool = ContentIdentity.parse_uri("sha256:" + executable.sha256)
        require(len(observation.native_sdk_executions) == len(groups))
        scopes = []
        for execution_id, (revision, target, values, ordered, runtime_identity) in zip(
            observation.native_sdk_executions,
            groups,
            strict=True,
        ):
            step()
            execution = read(execution_id)
            captured = bool(plan.profile.runtime_library_paths)
            fields(
                execution,
                "schema phase command_binding request authorization "
                "runtime_identity manifest_identity dependencies "
                "checks retained_command_context retained_sdk_runtime_observations"
                + (" captured_runtime_context" if captured else ""),
            )
            require(
                execution["schema"] == EXECUTION_SCHEMA and execution["phase"] == phase
            )
            binding = execution["command_binding"]
            fields(
                binding,
                "phase command_identity command_contract_identity "
                "entrypoint_identity argv_identity cwd_identity "
                "environment_identity manifest runtime"
                + (" captured_runtime_identity" if captured else ""),
            )
            require(
                binding["entrypoint_identity"] is None
                and binding["manifest"] == execution["manifest_identity"]
                and binding["runtime"]
                == execution["runtime_identity"]
                == runtime_identity.to_dict()
            )
            context = execution["retained_command_context"]
            verify_retained_sdk_command_context(
                context,
                plan=plan,
                manifest=manifest,
                output_manifest=stage_manifest,
                acceptance_policy=run.acceptance_policy,
                command_identity=command_identity,
                phase=phase,
                command_binding=binding,
            )
            current_anchor = canonical_identity(
                {
                    "workspace_root": context["workspace_root"],
                    "platform": context["platform"],
                    "substitutions": context["substitutions"],
                    "system_root": context["environment"].get("SystemRoot"),
                }
            )
            if anchor is None:
                anchor = current_anchor
            require(anchor == current_anchor)
            if not ordinary:
                require(
                    observation.command_identity == canonical_identity(context["argv"])
                )
            require(
                all(
                    context["platform"] == value[2].layout.operating_system
                    for _key, value in values
                )
            )
            materialization = read(identity(execution["manifest_identity"]))
            require(
                materialization
                == {
                    "schema": "literate-ai/native-sdk-execution-inputs@1",
                    "consumer_revision": revision.to_dict(),
                    "target_identity": target.to_dict(),
                    "imports": [
                        {
                            "root": "snapshot/tree/" + value[0],
                            "input_identity": key.to_dict(),
                            "snapshot": value[1].to_dict(),
                        }
                        for key, value in values
                    ],
                }
            )
            require(
                execution["dependencies"]
                == [dependencies[key].to_dict() for key in ordered]
            )
            materialized = execution["retained_sdk_runtime_observations"]
            require(type(materialized) is list and len(materialized) == len(values))
            path_type = (
                PureWindowsPath if context["platform"] == "windows" else PurePosixPath
            )
            input_root = path_type(context["workspace_root"]) / "snapshot" / "tree"
            scope_canonical_input_root = None
            for (key, value), materialized_record in zip(
                values, materialized, strict=True
            ):
                step()
                fields(
                    materialized_record,
                    "input_identity logical_root observation_root observation",
                )
                require(
                    identity(materialized_record["input_identity"]) == key
                    and materialized_record["logical_root"]
                    == str(input_root / value[0])
                )
                root_value = materialized_record["observation_root"]
                require(type(root_value) is str and 0 < len(root_value) <= 65536)
                observed_root = path_type(root_value)
                require(observed_root.is_absolute() and ".." not in observed_root.parts)
                prefix_parts = PurePosixPath(value[0]).parts
                require(
                    tuple(observed_root.parts[-len(prefix_parts) :]) == prefix_parts
                )
                canonical_input_root = path_type(
                    *observed_root.parts[: -len(prefix_parts)]
                )
                require(
                    canonical_input_root.name == "tree"
                    and canonical_input_root.parent.name == "snapshot"
                    and canonical_input_root.parent.parent.name
                    == path_type(context["workspace_root"]).name
                )
                if canonical_inputs_anchor is None:
                    canonical_inputs_anchor = canonical_input_root
                require(canonical_inputs_anchor == canonical_input_root)
                scope_canonical_input_root = canonical_input_root
                fresh_runtime = blob(materialized_record["observation"])
                fields(
                    fresh_runtime,
                    "schema snapshot_identity target_identity operating_system "
                    "architecture native_libraries components edges",
                )
                require(
                    identity(fresh_runtime["snapshot_identity"]) == value[1].identity
                    and identity(fresh_runtime["target_identity"]) == target
                    and fresh_runtime["operating_system"]
                    == value[2].layout.operating_system
                    and fresh_runtime["architecture"] == value[2].layout.architecture
                )
                _verify_runtime_assertions(
                    fresh_runtime, value[1], value[2].layout, require, step
                )
                projected = project_native_sdk_dependency_assertions(
                    input_identity=key,
                    snapshot=value[1],
                    operating_system=value[2].layout.operating_system,
                    architecture=value[2].layout.architecture,
                    consumer_revision=revision,
                    source_resolution=dependencies[key].repository_resolution,
                    source_admission_identity=RepositorySourceAdmission.from_dict(
                        value[5]["source_admission"]
                    ).identity,
                    root=observed_root,
                    runtime=fresh_runtime,
                )
                require(projected.to_dict() == dependencies[key].to_dict())
            request = BuildRequest.from_dict(execution["request"])
            grant = BuildAuthorization.from_dict(execution["authorization"])
            require(
                request.effective_revision_digest == revision.uri
                and request.source_bundle_digest == plan.manifest_identity.uri
                and request.builder_id == canonical_identity(binding).uri
                and request.toolchain_digest == expected_tool.uri
                and request.sandbox_profile == "local-explicit-native-sdk"
                and request.requested_privileges
                == grant.privileges
                == ("execute-native-sdk",)
                and request.allowed_outputs == ("stdout", "stderr")
                and grant.profile is not SecurityProfile.BLOCKED
                and grant.classification_digest == runtime_identity.uri
            )
            after = verify_historical_sdk_authority_checks(
                execution["checks"], grant, request, ordered
            )
            if captured:
                verify_historical_retained_sdk_runtime_context(
                    execution["captured_runtime_context"],
                    plan=plan,
                    manifest=manifest,
                    command_context=context,
                    command_binding=binding,
                    tool_observation=run.resolved_bom.tool_observation,
                    sdk_values=tuple(
                        (value[0], dependencies[key]) for key, value in values
                    ),
                    guard=reader.guard,
                    canonical_input_root=scope_canonical_input_root,
                )
            scopes.append((grant, request, ordered, after))
        final_scopes = scopes
    require(run.native_sdk_final_check_identity is not None)
    final = reader.record(run.native_sdk_final_check_identity.uri)
    fields(final, "schema plan_identity inputs_identity checks")
    require(
        final["schema"]
        in {
            "literate-ai/retained-sdk-final-authority-check@1",
            "literate-ai/retained-sdk-final-authority-check@2",
        }
        and final["plan_identity"] == plan.identity.uri
        and final["inputs_identity"] == inputs_identity.uri
        and type(final["checks"]) is list
        and len(final["checks"]) == len(final_scopes)
    )
    for check, (grant, request, ordered, after) in zip(
        final["checks"], final_scopes, strict=True
    ):
        step()
        if final["schema"] == "literate-ai/retained-sdk-final-authority-check@2":
            verify_historical_completed_sdk_check(
                check, grant, request, ordered, after=after
            )
        else:
            verify_historical_sdk_authority_checks(
                [check], grant, request, ordered, expected_count=1, not_before=after
            )
    for snapshot in current_sdk_snapshots:
        snapshot.require_unchanged()
    step()
