"""Public operator-adoption contracts and fail-closed authority behavior."""

from __future__ import annotations

import io
import json
import shlex
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.conversion_authority import (
    ConversionAuthorityError,
    require_current_qualified_conversion_authority,
)
from literate_ai.adapters.models import CodingCliSelection
from literate_ai.adapters.models.coding_cli import inspect_coding_cli_authentication
from literate_ai.adapters.operator_status import (
    OperatorStatusError,
    _next_verb,
    _standard_binding_distribution_mismatch,
    inspect_operator_status,
)
from literate_ai.adapters.standard_lifecycle_binding import (
    StandardLifecycleBindingError,
)
from literate_ai.application.operator_adoption import (
    OperatorAdoptionError,
    OperatorAdoptionService,
)
from literate_ai.cli import main
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.rebuild import _rebuild_from_args_body
from literate_ai.contracts import StandardProjectLifecycleDriver, canonical_identity
from literate_ai.contracts.operator_adoption import (
    ConversionAuthorityStage,
    ConversionAuthorityState,
    advance_conversion_authority,
)


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    document = output.getvalue() if status == 0 else errors.getvalue()
    return status, json.loads(document)


def status_fixture() -> dict[str, object]:
    return {
        "schema": "literate-ai/operator-status@1",
        "view": "status",
        "project": {
            "root": "/project",
            "project_id": "example",
            "kind": "adopted",
            "authority": {"state": "current"},
            "conversion_authority": {"stage": "wrapped"},
            "locks": {"state": "current"},
            "test_receipt": {"state": "missing"},
        },
        "host": {
            "host_tools": {"ready": True},
            "coding_cli": {"name": "codex", "state": "authenticated"},
        },
        "next_verb": "litai project test-receipt run-retained CANDIDATE",
    }


class ConversionAuthorityContractTests(unittest.TestCase):
    def test_transitions_are_adjacent_and_keep_original_source_authority(self) -> None:
        first_evidence = canonical_identity({"evidence": "wrapped"})
        wrapped = ConversionAuthorityState(
            project_id="example",
            stage=ConversionAuthorityStage.WRAPPED,
            evidence_identities=(first_evidence,),
        )

        self.assertEqual(wrapped.release_authority, "original-source")
        with self.assertRaisesRegex(ValueError, "cannot advance"):
            advance_conversion_authority(
                wrapped,
                ConversionAuthorityStage.DRAFTED,
                evidence_identities=(canonical_identity({"evidence": "draft"}),),
            )

        retained = advance_conversion_authority(
            wrapped,
            ConversionAuthorityStage.RETAINED,
            evidence_identities=(canonical_identity({"evidence": "receipt"}),),
        )
        drafted = advance_conversion_authority(
            retained,
            ConversionAuthorityStage.DRAFTED,
            evidence_identities=(canonical_identity({"evidence": "projection"}),),
        )
        qualified = advance_conversion_authority(
            drafted,
            ConversionAuthorityStage.QUALIFIED,
            evidence_identities=(canonical_identity({"evidence": "qualified"}),),
        )

        self.assertEqual(qualified.release_authority, "specification")
        self.assertEqual(retained.prior_state_identity, wrapped.identity)

    def test_release_authority_claim_cannot_be_forged(self) -> None:
        state = ConversionAuthorityState(
            project_id="example",
            stage=ConversionAuthorityStage.WRAPPED,
            evidence_identities=(canonical_identity({"evidence": "wrapped"}),),
        )
        document = state.to_dict()
        document["release_authority"] = "specification"

        with self.assertRaisesRegex(ValueError, "claim is invalid"):
            ConversionAuthorityState.from_dict(document)

    def test_evidence_order_cannot_be_normalized_during_read(self) -> None:
        state = ConversionAuthorityState(
            project_id="example",
            stage=ConversionAuthorityStage.WRAPPED,
            evidence_identities=tuple(
                sorted(
                    (
                        canonical_identity({"evidence": "a"}),
                        canonical_identity({"evidence": "b"}),
                    ),
                    key=lambda item: item.uri,
                )
            ),
        )
        document = state.to_dict()
        document["evidence_identities"] = list(
            reversed(document["evidence_identities"])
        )

        with self.assertRaisesRegex(ValueError, "must be sorted"):
            ConversionAuthorityState.from_dict(document)


class OperatorStatusNextVerbTests(unittest.TestCase):
    """A pending Standard distribution-binding mismatch must block a rebuild
    recommendation, since `litai rebuild` requires a resolved binding first."""

    def test_rebuild_recommendation_redirects_to_rebind_on_distribution_mismatch(
        self,
    ) -> None:
        verb = _next_verb(
            None,
            {"state": "current"},
            standard_binding_distribution_mismatch=True,
        )

        self.assertEqual(
            verb, "litai project lifecycle rebind-standard --project . --output OUTPUT"
        )

    def test_qualified_stage_rebuild_recommendation_also_redirects_to_rebind(
        self,
    ) -> None:
        verb = _next_verb(
            "qualified",
            {"state": "current"},
            standard_binding_distribution_mismatch=True,
        )

        self.assertEqual(
            verb, "litai project lifecycle rebind-standard --project . --output OUTPUT"
        )

    def test_rebuild_recommendation_unaffected_when_binding_matches(self) -> None:
        verb = _next_verb(
            None,
            {"state": "current"},
            standard_binding_distribution_mismatch=False,
        )

        self.assertEqual(verb, "litai rebuild --allow-host-execution")

    def test_earlier_stages_are_not_overridden_by_a_pending_mismatch(self) -> None:
        verb = _next_verb(
            "wrapped",
            {"state": "current"},
            standard_binding_distribution_mismatch=True,
        )

        self.assertEqual(
            verb, "litai project test-receipt run-retained CANDIDATE --project ."
        )

    def test_non_standard_driver_is_never_a_mismatch(self) -> None:
        self.assertFalse(_standard_binding_distribution_mismatch(None))
        self.assertFalse(_standard_binding_distribution_mismatch(object()))

    def test_distribution_mismatch_error_code_is_detected(self) -> None:
        driver = mock.MagicMock(spec=StandardProjectLifecycleDriver)

        with mock.patch(
            "literate_ai.adapters.operator_status."
            "resolve_standard_project_lifecycle_driver",
            side_effect=StandardLifecycleBindingError(
                "standard_binding.distribution_mismatch",
                "project-pinned framework distribution differs from installed "
                "wheel bytes",
            ),
        ):
            self.assertTrue(_standard_binding_distribution_mismatch(driver))

    def test_other_binding_errors_are_not_treated_as_a_distribution_mismatch(
        self,
    ) -> None:
        driver = mock.MagicMock(spec=StandardProjectLifecycleDriver)

        with mock.patch(
            "literate_ai.adapters.operator_status."
            "resolve_standard_project_lifecycle_driver",
            side_effect=StandardLifecycleBindingError(
                "standard_binding.policy_mismatch",
                "project-pinned Standard policy differs from installed policy",
            ),
        ):
            self.assertFalse(_standard_binding_distribution_mismatch(driver))


class OperatorStatusEndToEndNextVerbTests(unittest.TestCase):
    """`litai status` must not recommend a rebuild that rebuild's own front door
    (`resolve_standard_project_lifecycle_driver`) is guaranteed to reject."""

    def test_status_recommends_rebind_before_rebuild_on_distribution_mismatch(
        self,
    ) -> None:
        driver = mock.MagicMock(spec=StandardProjectLifecycleDriver)
        project = SimpleNamespace(
            root=Path("/project"),
            definition=SimpleNamespace(
                project_id="example",
                lifecycle_driver=driver,
                default_flavor_selectors=(),
                test_receipt=None,
            ),
        )
        store = mock.Mock()
        store.load_optional.return_value = None

        with (
            mock.patch(
                "literate_ai.adapters.operator_status.discover_project",
                return_value=project,
            ),
            mock.patch(
                "literate_ai.adapters.operator_status.inspect_project_tracker",
                return_value={"state": "unavailable"},
            ),
            mock.patch(
                "literate_ai.adapters.operator_status."
                "FilesystemConversionAuthorityStore",
                return_value=store,
            ),
            mock.patch(
                "literate_ai.adapters.operator_status."
                "validated_project_authority_identity",
                return_value=canonical_identity({"authority": "current"}),
            ),
            mock.patch(
                "literate_ai.adapters.operator_status."
                "current_project_component_lock_identities",
                return_value=(),
            ),
            mock.patch(
                "literate_ai.adapters.operator_status.inspect_project_test_receipt",
                return_value={"state": "missing"},
            ),
            mock.patch(
                "literate_ai.adapters.operator_status.inspect_host_preflight",
                return_value={"host_tools": {"ready": True}},
            ),
            mock.patch(
                "literate_ai.adapters.operator_status."
                "resolve_standard_project_lifecycle_driver",
                side_effect=StandardLifecycleBindingError(
                    "standard_binding.distribution_mismatch",
                    "project-pinned framework distribution differs from "
                    "installed wheel bytes",
                ),
            ),
        ):
            result = inspect_operator_status(Path("/project"))

        self.assertEqual(
            result["next_verb"],
            "litai project lifecycle rebind-standard --project . --output OUTPUT",
        )


class OperatorCliTests(unittest.TestCase):
    def test_status_json_uses_the_common_cli_envelope(self) -> None:
        with mock.patch(
            "literate_ai.cli.operator.inspect_operator_status",
            return_value=status_fixture(),
        ):
            status, envelope = invoke("--json", "status", "--project", ".")

        self.assertEqual(status, 0, envelope)
        self.assertEqual(envelope["schema"], "literate-ai/cli-result@1")
        self.assertEqual(envelope["command"], "status")
        self.assertEqual(
            envelope["result"]["project"]["conversion_authority"]["stage"],
            "wrapped",
        )

    def test_status_tty_renders_the_same_contract(self) -> None:
        output = TtyStringIO()
        errors = TtyStringIO()
        with mock.patch(
            "literate_ai.cli.operator.inspect_operator_status",
            return_value=status_fixture(),
        ):
            status = main(("status",), stdout=output, stderr=errors)

        self.assertEqual(status, 0, errors.getvalue())
        self.assertIn("Conversion stage: wrapped", output.getvalue())
        self.assertIn("Next:", output.getvalue())

    def test_status_fails_closed_when_project_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status, envelope = invoke("--json", "status", "--project", directory)

        self.assertEqual(status, 2, envelope)
        self.assertEqual(envelope["error"]["code"], "project.not_found")

    def test_doctor_reports_host_preflight_without_requiring_a_project(self) -> None:
        with mock.patch(
            "literate_ai.cli.operator.inspect_host_preflight",
            return_value={"schema": "literate-ai/operator-host-preflight@1"},
        ):
            status, envelope = invoke("--json", "doctor")

        self.assertEqual(status, 0, envelope)
        self.assertEqual(envelope["result"]["view"], "doctor")
        self.assertIsNone(envelope["result"]["project"])

    def test_doctor_tty_does_not_render_a_fake_project(self) -> None:
        output = TtyStringIO()
        errors = TtyStringIO()
        with mock.patch(
            "literate_ai.cli.operator.inspect_host_preflight",
            return_value={
                "paths": {"state": "resolved"},
                "host_tools": {"ready": True},
                "coding_cli": {"name": "codex", "state": "authenticated"},
            },
        ):
            status = main(("doctor",), stdout=output, stderr=errors)

        self.assertEqual(status, 0, errors.getvalue())
        self.assertIn("Literate AI doctor", output.getvalue())
        self.assertNotIn("Project:", output.getvalue())

    def test_doctor_rejects_project_selection_because_it_is_host_only(self) -> None:
        status, envelope = invoke("--json", "doctor", "--project", ".")

        self.assertEqual(status, 2, envelope)
        self.assertEqual(envelope["error"]["code"], "cli.usage")

    def test_codex_unauthenticated_status_does_not_make_a_model_request(self) -> None:
        executable = str(Path(tempfile.gettempdir()).resolve() / "tools" / "codex")
        selection = CodingCliSelection("codex", executable, "sha256:" + "a" * 64)
        completed = subprocess.CompletedProcess(
            (executable, "login", "status"), 1, "", "not logged in"
        )
        runner = mock.Mock(return_value=completed)
        with mock.patch(
            "literate_ai.adapters.models.coding_cli.select_coding_cli",
            return_value=selection,
        ):
            report = inspect_coding_cli_authentication({}, runner=runner)

        self.assertEqual(report["state"], "unauthenticated")
        self.assertEqual(runner.call_args.args[0], (executable, "login", "status"))

    def test_onboard_apply_refuses_without_acknowledgement(self) -> None:
        with (
            mock.patch(
                "literate_ai.cli.operator._create_plan",
                return_value={
                    "path": "/new",
                    "writes": False,
                    "apply_supported": True,
                },
            ),
            mock.patch("literate_ai.cli.operator.init_project_from_args") as initialize,
        ):
            status, envelope = invoke("--json", "onboard", "create", "/new", "--apply")

        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"],
            "operator.onboard_acknowledgement_required",
        )
        initialize.assert_not_called()

    def test_onboard_refuses_apply_only_flags_on_a_read_only_plan(self) -> None:
        status, envelope = invoke(
            "--json", "onboard", "create", "/new", "--acknowledge"
        )

        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"], "operator.onboard_apply_flag_required"
        )

    def test_onboard_create_passes_the_reviewed_parent_to_init(self) -> None:
        parent = "https://example.test/literate-ai.git#" + "a" * 40
        initialized = {"schema": "literate-ai/project-initialization@6"}
        with (
            mock.patch(
                "literate_ai.cli.operator._create_plan",
                return_value={
                    "path": "/new",
                    "writes": False,
                    "apply_supported": True,
                },
            ),
            mock.patch(
                "literate_ai.cli.operator.init_project_from_args",
                return_value=initialized,
            ) as initialize,
            mock.patch(
                "literate_ai.cli.operator.inspect_operator_status",
                return_value=status_fixture(),
            ),
        ):
            status, envelope = invoke(
                "--json",
                "onboard",
                "create",
                "/new",
                "--from",
                parent,
                "--apply",
                "--acknowledge",
            )

        self.assertEqual(status, 0, envelope)
        self.assertEqual(envelope["result"]["initialization"], initialized)
        self.assertEqual(initialize.call_args.args[0].repository_from, parent)

    def test_onboard_preserves_success_when_post_apply_status_fails(self) -> None:
        with (
            mock.patch(
                "literate_ai.cli.operator._create_plan",
                return_value={
                    "path": "/new",
                    "writes": False,
                    "apply_supported": True,
                },
            ),
            mock.patch(
                "literate_ai.cli.operator.init_project_from_args",
                return_value={"schema": "literate-ai/project-initialization@6"},
            ),
            mock.patch(
                "literate_ai.cli.operator.inspect_operator_status",
                side_effect=OperatorStatusError(
                    "project.status_unavailable", "status fixture failed"
                ),
            ),
        ):
            status, envelope = invoke(
                "--json",
                "onboard",
                "create",
                "/new",
                "--apply",
                "--acknowledge",
            )

        self.assertEqual(status, 0, envelope)
        self.assertTrue(envelope["result"]["applied"])
        self.assertEqual(
            envelope["result"]["status"]["state"], "unavailable-after-apply"
        )

    def test_onboard_adopt_plan_names_wrapped_landing_stage(self) -> None:
        conversion = {
            "schema": "literate-ai/convert-readiness@1",
            "readiness": "ready",
            "writes": False,
            "landing_stage": "wrapped",
        }
        with tempfile.TemporaryDirectory(prefix="litai adopt space ") as directory:
            target = str(Path(directory).resolve())
            with (
                mock.patch(
                    "literate_ai.cli.operator.plan_convert", return_value=conversion
                ),
                mock.patch(
                    "literate_ai.cli.operator.detect_repo_flavors", return_value=[]
                ),
                mock.patch(
                    "literate_ai.cli.operator.inspect_host_preflight",
                    return_value={"schema": "host"},
                ),
            ):
                status, envelope = invoke("--json", "onboard", "adopt", directory)

        self.assertEqual(status, 0, envelope)
        self.assertEqual(envelope["result"]["landing_stage"], "wrapped")
        self.assertFalse(envelope["result"]["writes"])
        for command in envelope["result"]["next_commands"]:
            argv = shlex.split(command)
            self.assertEqual(argv[argv.index("--project") + 1], target)


class OperatorApplicationServiceTests(unittest.TestCase):
    def test_apply_rejects_a_changed_reviewed_plan(self) -> None:
        service = OperatorAdoptionService()
        with self.assertRaises(OperatorAdoptionError) as raised:
            service.apply(
                "create",
                builder=lambda: {
                    "path": "/new",
                    "writes": False,
                    "apply_supported": True,
                },
                mutator=lambda: {"unexpected": True},
                acknowledged=True,
                expected_plan_identity="sha256:" + "0" * 64,
            )

        self.assertEqual(raised.exception.code, "operator.onboard_plan_changed")

    def test_plan_rejects_a_builder_that_does_not_prove_read_only(self) -> None:
        service = OperatorAdoptionService()

        with self.assertRaises(OperatorAdoptionError) as raised:
            service.plan("create", lambda: {"path": "/new"})

        self.assertEqual(raised.exception.code, "operator.onboard_plan_invalid")

    def test_plan_rejects_reserved_identity_fields(self) -> None:
        service = OperatorAdoptionService()

        with self.assertRaises(OperatorAdoptionError) as raised:
            service.plan(
                "create",
                lambda: {
                    "schema": "forged",
                    "path": "/new",
                    "writes": False,
                    "apply_supported": True,
                },
            )

        self.assertEqual(raised.exception.code, "operator.onboard_plan_invalid")

    def test_rebuild_refuses_unqualified_adopted_project(self) -> None:
        conversion = ConversionAuthorityState(
            project_id="example",
            stage=ConversionAuthorityStage.WRAPPED,
            evidence_identities=(canonical_identity({"evidence": "wrapped"}),),
        )
        project = SimpleNamespace(root=Path.cwd())
        store = mock.Mock()
        store.load_optional.return_value = conversion
        args = Namespace(allow_host_execution=True, project=".", specification=".")

        with (
            mock.patch("literate_ai.cli.rebuild._project", return_value=project),
            mock.patch(
                "literate_ai.adapters.conversion_authority."
                "FilesystemConversionAuthorityStore",
                return_value=store,
            ),
            self.assertRaises(CliFailure) as raised,
        ):
            _rebuild_from_args_body(args)

        self.assertEqual(
            raised.exception.code, "rebuild.conversion_authority_not_qualified"
        )

    def test_qualified_conversion_claim_rejects_changed_projection_set(self) -> None:
        recorded = canonical_identity({"projection": "recorded"})
        current = canonical_identity({"projection": "current"})
        state = ConversionAuthorityState(
            project_id="example",
            stage=ConversionAuthorityStage.QUALIFIED,
            evidence_identities=(recorded,),
            prior_state_identity=canonical_identity({"stage": "drafted"}),
        )
        project = SimpleNamespace(definition=SimpleNamespace(project_id="example"))

        with (
            mock.patch(
                "literate_ai.adapters.conversion_authority."
                "evidence_for_conversion_stage",
                return_value=(current,),
            ),
            self.assertRaises(ConversionAuthorityError) as raised,
        ):
            require_current_qualified_conversion_authority(project, state)

        self.assertEqual(
            raised.exception.code, "conversion_authority.qualification_stale"
        )


if __name__ == "__main__":
    unittest.main()
