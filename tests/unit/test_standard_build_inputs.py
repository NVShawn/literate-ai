"""BUILD preflight validates portable records before any host execution."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.application.standard_build_inputs import (
    StandardBuildInputError,
    validate_standard_build_inputs,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardComponentBuildPlan,
)
from literate_ai.contracts.executable_components import ComponentCommandContract
from tests.support.fixtures_test_standard_local_command_adapter import (
    _identity,
    _provider_export,
    _python_copy_lifecycle,
)


class StandardBuildInputsTests(unittest.TestCase):
    def test_serialized_inputs_validate_after_local_source_is_gone(self):
        with tempfile.TemporaryDirectory() as scratch:
            ports, plan, candidate, _ = _python_copy_lifecycle(Path(scratch))
            wire = json.loads(
                json.dumps(
                    {
                        "plan": plan.to_dict(),
                        "contract": ports.contracts[
                            candidate.component_revision.uri
                        ].to_dict(),
                    }
                )
            )
        del ports
        validate_standard_build_inputs(
            StandardComponentBuildPlan.from_dict(wire["plan"]),
            (),
            ComponentCommandContract.from_dict(wire["contract"]),
        )

    def test_changed_export_shape_and_compiler_refuse_before_command(self):
        with tempfile.TemporaryDirectory() as scratch:
            ports, plan, candidate, _ = _python_copy_lifecycle(Path(scratch))
            contract = ports.contracts[candidate.component_revision.uri]
            for field, value in (
                ("export_id", "other"),
                ("role", "data"),
                ("abi_identity", _identity("foreign-abi")),
                ("target_identity", _identity("foreign-target")),
                ("media_type", "application/octet-stream"),
                ("producer_identity", _identity("foreign-producer")),
            ):
                with self.subTest(field=field):
                    changed = replace(
                        contract,
                        artifact_export=replace(
                            contract.artifact_export, **{field: value}
                        ),
                    )
                    with self.assertRaises(StandardBuildInputError):
                        validate_standard_build_inputs(plan, (), changed)
                    ports.contracts[candidate.component_revision.uri] = changed
                    with patch.object(ports, "_run_locked") as execute:
                        with self.assertRaisesRegex(
                            LocalStandardLifecycleError,
                            "exact locked command export shape",
                        ):
                            ports.build(plan, ())
                        execute.assert_not_called()
            with self.assertRaises(StandardBuildInputError):
                validate_standard_build_inputs(
                    plan,
                    (),
                    replace(
                        contract,
                        language_compiler_identity=_identity("foreign-compiler"),
                    ),
                )

    def test_extra_provider_is_refused_before_command(self):
        with tempfile.TemporaryDirectory() as scratch:
            ports, plan, candidate, _ = _python_copy_lifecycle(Path(scratch))
            providers = (_provider_export("unaccepted"),)
            contract = ports.contracts[candidate.component_revision.uri]
            with self.assertRaisesRegex(
                StandardBuildInputError, "different provider artifacts"
            ):
                validate_standard_build_inputs(plan, providers, contract)
            with patch.object(ports, "_run_locked") as execute:
                with self.assertRaisesRegex(
                    LocalStandardLifecycleError, "different provider artifacts"
                ):
                    ports.build(plan, providers)
                execute.assert_not_called()
