"""Plan finalization has portable inputs and no controller filesystem dependency."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.application.standard_plan_finalization import (
    finalize_standard_component_plan,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardBuildAuthorization,
    StandardComponentBuildIntent,
    StandardProjectLifecycleError,
)
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    ComponentCommandContract,
)
from tests.unit.test_standard_local_command_adapter import (
    _identity,
    _provider_export,
    _python_copy_lifecycle,
)


class StandardPlanFinalizationTests(unittest.TestCase):
    def test_public_inputs_reconstruct_plan_after_controller_custody_is_gone(self):
        for mode in ("none", "npm", "python"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as scratch:
                    ports, _, candidate, intent = _python_copy_lifecycle(Path(scratch))
                    provider = _provider_export("provider")
                    package = _provider_export("package")
                    intent = replace(
                        intent,
                        provider_artifact_identities=(provider.identity,),
                        package_artifact_identities=(package.identity,),
                    )
                    ports._intent_artifacts[intent.identity.uri] = (provider,)
                    ports._intent_package_artifacts[intent.identity.uri] = (package,)
                    if mode == "npm":
                        intent = replace(
                            intent,
                            build_request=replace(
                                intent.build_request,
                                requested_privileges=(
                                    "execute-build-tools",
                                    "network-access",
                                ),
                            ),
                        )
                        ports._intent_artifacts[intent.identity.uri] = (provider,)
                        ports._intent_package_artifacts[intent.identity.uri] = (
                            package,
                        )
                        ports.npm_targets[candidate.component_revision.uri] = object()
                    elif mode == "python":
                        ports.python_targets[candidate.component_revision.uri] = (
                            object()
                        )
                    authorization = ports.authorize(intent, _identity("index"))
                    expected = ports.finalize(intent, authorization)
                    contract = ports.contracts[candidate.component_revision.uri]
                    wire = json.loads(
                        json.dumps(
                            {
                                "intent": intent.to_dict(),
                                "authorization": authorization.to_dict(),
                                "contract": contract.to_dict(),
                                "providers": [provider.to_dict()],
                                "packages": [package.to_dict()],
                            }
                        )
                    )
                # The input records are sufficient after the originating source tree,
                # object root, and in-memory intent maps have all been discarded.
                del ports
                result = finalize_standard_component_plan(
                    StandardComponentBuildIntent.from_dict(wire["intent"]),
                    StandardBuildAuthorization.from_dict(wire["authorization"]),
                    ComponentCommandContract.from_dict(wire["contract"]),
                    tuple(
                        ArtifactExport.from_dict(value) for value in wire["providers"]
                    ),
                    tuple(
                        ArtifactExport.from_dict(value) for value in wire["packages"]
                    ),
                    dependency_resolution=mode,
                )
                self.assertEqual(result.identity, expected.identity)
                self.assertEqual(result.to_dict(), expected.to_dict())

    def test_substituted_phase_inputs_cannot_produce_an_accepted_plan(self):
        with tempfile.TemporaryDirectory() as scratch:
            ports, _, candidate, intent = _python_copy_lifecycle(Path(scratch))
            contract = ports.contracts[candidate.component_revision.uri]
            authorization = ports.authorize(intent, _identity("index"))
            arguments = dict(
                intent=intent,
                authorization=authorization,
                contract=contract,
                providers=(),
                package_artifacts=(),
                dependency_resolution="none",
            )
            substitutions = (
                {"providers": (_provider_export("unbound"),)},
                {"package_artifacts": (_provider_export("unbound"),)},
                {
                    "authorization": replace(
                        authorization, build_intent_identity=_identity("other")
                    )
                },
                {
                    "authorization": replace(
                        authorization, build_request_identity=_identity("other")
                    )
                },
                {"dependency_resolution": "unknown"},
                {"dependency_resolution": "npm"},
                {
                    "contract": replace(
                        contract, locked_build_authority_identity=_identity("other")
                    )
                },
                {"contract": replace(contract, component_revision=_identity("other"))},
            )
            for change in substitutions:
                with (
                    self.subTest(change=tuple(change)),
                    self.assertRaises(StandardProjectLifecycleError),
                ):
                    finalize_standard_component_plan(**(arguments | change))
