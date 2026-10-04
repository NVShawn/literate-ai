"""Pure generation transition helpers retained beside canonical lock CLI tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import literate_ai.cli.generation as generation_cli
from literate_ai.adapters.legacy_generation_catalog import (
    model_mapping,
)
from literate_ai.application.generation_preparation import GenerationPreparationError
from literate_ai.contracts import CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR


def bind_tree(generated, root, **candidate_updates):
    from literate_ai.adapters.lifecycle import local_generated_source_tree_identity

    candidate = replace(
        generated.output.candidate,
        tree_identity=local_generated_source_tree_identity(root),
        **candidate_updates,
    )
    provenance = replace(
        generated.output.provenance, candidate_identity=candidate.identity
    )
    output = replace(
        generated.output,
        candidate=candidate,
        candidate_identity=candidate.identity,
        provenance=provenance,
        provenance_identity=provenance.identity,
    )
    return replace(generated, output=output, output_identity=output.identity)


class GenerationTransitionHelperTests(unittest.TestCase):
    def test_generation_stops_before_coding_cli_when_authority_is_unreviewed(
        self,
    ) -> None:
        prepared = object()
        failure = generation_cli.CliFailure(
            "project.documentation_authority_review_stale",
            "authority review is stale",
        )
        with (
            mock.patch.object(
                generation_cli, "_prepare_generation", return_value=prepared
            ),
            mock.patch.object(
                generation_cli,
                "_require_reviewed_component_authority",
                side_effect=failure,
            ) as authority_gate,
            mock.patch.object(
                generation_cli.FilesystemStandardSourceGenerationAdapter,
                "from_environment",
            ) as generator,
        ):
            with self.assertRaises(generation_cli.CliFailure) as raised:
                generation_cli.generate_from_args(object())

        self.assertEqual(
            raised.exception.code, "project.documentation_authority_review_stale"
        )
        authority_gate.assert_called_once_with(prepared)
        generator.assert_not_called()

    def test_an_invalid_coding_cli_timeout_configuration_fails_closed(self) -> None:
        from literate_ai.adapters.models import CodingCliError

        prepared = mock.Mock()
        with (
            mock.patch.object(
                generation_cli, "_prepare_generation", return_value=prepared
            ),
            mock.patch.object(generation_cli, "_require_reviewed_component_authority"),
            mock.patch.object(generation_cli, "discover_project", return_value=None),
            mock.patch.object(
                generation_cli, "resolve_cache_directories"
            ) as directories,
            mock.patch.object(generation_cli, "_require_disposable_output"),
            mock.patch.object(generation_cli, "_require_generation_inputs_unchanged"),
            mock.patch.object(
                generation_cli.FilesystemStandardSourceGenerationAdapter,
                "from_environment",
                side_effect=CodingCliError(
                    "coding_cli.generation_timeout_configuration_invalid",
                    "LITERATE_AI_CODING_CLI_TIMEOUT_SECONDS must be a positive integer",
                ),
            ),
        ):
            directories.return_value = mock.Mock(build_dir=Path("/tmp/build"))
            args = mock.Mock(output="/tmp/out", model=None)
            with self.assertRaises(generation_cli.CliFailure) as raised:
                generation_cli.generate_from_args(args)

        self.assertEqual(
            raised.exception.code,
            "coding_cli.generation_timeout_configuration_invalid",
        )

    def test_configured_model_cannot_use_the_omission_sentinel(self) -> None:
        with self.assertRaises(GenerationPreparationError) as raised:
            model_mapping(
                {"codex": CLI_CONFIGURED_DEFAULT_MODEL_SELECTOR},
                source="test Component",
            )
        self.assertEqual(raised.exception.code, "generate.invalid_model_selection")

    def test_source_admission_verifier_executes_tests_and_records_results(self) -> None:
        from tests.support.fixtures_test_standard_source_admission import generation

        generated = generation()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            suite = root / "source" / "tests"
            suite.mkdir(parents=True)
            (suite / "manifest.json").write_text(
                json.dumps({"cases": [{}, {}, {}]}), encoding="utf-8"
            )
            generated = bind_tree(generated, root)
            verification = generation_cli._CommandSourceVerifier(
                root,
                generated.output.candidate.source_manifest_identity,
                ((sys.executable, "-c", "raise SystemExit(0)"),),
            ).verify(generated)

        self.assertEqual(
            verification.source_manifest_identity,
            generated.output.candidate.source_manifest_identity,
        )
        self.assertEqual(verification.test_results[0].case_count, 3)
        self.assertEqual(verification.test_results[0].passed_count, 3)

    def test_source_admission_verifier_canonicalizes_multiple_results(self) -> None:
        from tests.support.fixtures_test_standard_source_admission import generation

        generated = generation()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            suite = root / "source" / "tests"
            suite.mkdir(parents=True)
            (suite / "manifest.json").write_text(
                json.dumps({"cases": [{}, {}]}), encoding="utf-8"
            )
            commands = (
                (sys.executable, "-c", "print('first')"),
                (sys.executable, "-c", "print('second')"),
            )
            generated = bind_tree(generated, root)
            verification = generation_cli._CommandSourceVerifier(
                root,
                generated.output.candidate.source_manifest_identity,
                commands,
            ).verify(generated)

        identities = tuple(item.identity.uri for item in verification.test_results)
        self.assertEqual(identities, tuple(sorted(identities)))
        self.assertEqual(len(identities), 2)

    def test_source_admission_rejects_duplicate_test_commands(self) -> None:
        command = json.dumps([sys.executable, "-c", "raise SystemExit(0)"])
        with self.assertRaises(generation_cli.CliFailure) as raised:
            generation_cli._source_test_commands(
                type("Args", (), {"source_test_command": (command, command)})()
            )

        self.assertEqual(
            raised.exception.code, "source_admission.test_command_duplicate"
        )

    def test_source_admission_verifier_failure_names_revision_status_and_output(
        self,
    ) -> None:
        # source_admission.tests_failed used to say only "source verifier command
        # failed: python3" -- no failing Component revision, exit status, or
        # bounded output to distinguish a generated-test defect from an
        # invocation/cwd defect (see issue #65).
        from tests.support.fixtures_test_standard_source_admission import generation

        generated = generation()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            suite = root / "source" / "tests"
            suite.mkdir(parents=True)
            (suite / "manifest.json").write_text(
                json.dumps({"cases": [{}]}), encoding="utf-8"
            )
            generated = bind_tree(generated, root)
            with self.assertRaises(generation_cli.CliFailure) as raised:
                generation_cli._CommandSourceVerifier(
                    root,
                    generated.output.candidate.source_manifest_identity,
                    (
                        (
                            sys.executable,
                            "-c",
                            "import sys; sys.stderr.write('boom'); sys.exit(3)",
                        ),
                    ),
                ).verify(generated)

        self.assertEqual(raised.exception.code, "source_admission.tests_failed")
        self.assertIn(
            generated.output.candidate.component_revision.uri,
            raised.exception.message,
        )
        self.assertIn("exited 3", raised.exception.message)
        self.assertIn("boom", raised.exception.message)

    def test_source_admission_requires_verifier_owned_command(self) -> None:
        with self.assertRaises(generation_cli.CliFailure) as raised:
            generation_cli._source_test_commands(
                type("Args", (), {"source_test_command": ()})()
            )
        self.assertEqual(raised.exception.code, "source_admission.test_command_missing")


if __name__ == "__main__":
    unittest.main()
