"""Explicit coding-CLI and model pins for live qualification (ADR 0017)."""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from literate_ai.adapters.models.coding_cli import CODING_CLIS, CodingCliError
from literate_ai.adapters.user_assets import (
    LEGACY_TEST_CONFIG,
    UserAssetPathError,
    resolve_test_config_path,
)
from literate_ai.adapters.user_assets import (
    TEST_CONFIG_ENVIRONMENT as USER_TEST_CONFIG_ENVIRONMENT,
)

LIVE_MODEL_ENVIRONMENT = "LITAI_LIVE_MODEL"
REMOTE_LIVE_GATE_ENVIRONMENT = "LITAI_REMOTE_LIVE_GATE"
TEST_CONFIG_FILENAME = LEGACY_TEST_CONFIG
TEST_CONFIG_ENVIRONMENT = USER_TEST_CONFIG_ENVIRONMENT
PROVENANCE_CLI = "cli-flag"
PROVENANCE_ENVIRONMENT = "environment"
PROVENANCE_TEST_CONFIG = "test-config"
_REMOTE_FLAG_VALUES = frozenset({"1", "true", "yes"})
_Provenance = Literal["cli-flag", "environment", "test-config"]


@dataclass(frozen=True, slots=True)
class LiveTestSelection:
    """One explicit live-qualification launcher and model."""

    coding_cli: str
    model: str
    coding_cli_provenance: _Provenance
    model_provenance: _Provenance
    user_coding_cli: str | None = None
    user_model: str | None = None

    def environment_overlay(self) -> dict[str, str]:
        return {
            "CODING_CLI": self.coding_cli,
            LIVE_MODEL_ENVIRONMENT: self.model,
        }

    def cli_flags_override_user_default(self) -> bool:
        return (
            self.coding_cli_provenance == PROVENANCE_CLI
            or self.model_provenance == PROVENANCE_CLI
        )


def live_gate_is_remote(environment: Mapping[str, str] | None = None) -> bool:
    configured = os.environ if environment is None else environment
    raw = str(configured.get(REMOTE_LIVE_GATE_ENVIRONMENT, "")).strip().casefold()
    return raw in _REMOTE_FLAG_VALUES


def default_test_config_path(
    *,
    project_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    configured = os.environ if environment is None else environment
    try:
        return resolve_test_config_path(
            project_root=project_root,
            environment=configured,
        )
    except UserAssetPathError as exc:
        raise CodingCliError(exc.code, exc.message) from exc


def posix_export_prefix(values: Mapping[str, str]) -> str:
    """Render `export NAME=value ...` for a POSIX login-shell SSH command."""

    if not values:
        raise ValueError("POSIX export prefix requires at least one binding")
    return "export " + " ".join(
        f"{name}={shlex.quote(value)}" for name, value in values.items()
    )


def remote_live_gate_overlay(
    selection: LiveTestSelection | None = None,
) -> dict[str, str]:
    overlay = {REMOTE_LIVE_GATE_ENVIRONMENT: "1"}
    if selection is not None:
        overlay.update(selection.environment_overlay())
    return overlay


def try_resolve_live_test_selection(
    *,
    coding_cli: str | None = None,
    model: str | None = None,
    environment: Mapping[str, str] | None = None,
    test_config_path: Path | None = None,
    project_root: Path | None = None,
    require_opencode: bool = False,
    ignore_environment_pins: bool = False,
) -> LiveTestSelection | None:
    """Return a pin when configured; ``None`` when live selection is absent."""

    try:
        return resolve_live_test_selection(
            coding_cli=coding_cli,
            model=model,
            environment=environment,
            test_config_path=test_config_path,
            project_root=project_root,
            require_opencode=require_opencode,
            ignore_environment_pins=ignore_environment_pins,
        )
    except CodingCliError as exc:
        if exc.code in {
            "coding_cli.test_selection_unconfigured",
            "user_assets.project_invalid",
            "user_assets.project_required",
        }:
            return None
        raise


def configured_test_coding_cli(
    *, environment: Mapping[str, str], project_root: Path | None
) -> str | None:
    """Return the test configuration's coding CLI, or None when none is configured.

    Unlike a full selection this applies no environment pins or remote-gate rules;
    a malformed configuration still refuses with its own code.
    """

    try:
        path = default_test_config_path(
            project_root=project_root, environment=environment
        )
        coding_cli, _ = _test_config_pin(path)
    except CodingCliError as exc:
        if exc.code in {
            "coding_cli.test_selection_unconfigured",
            "user_assets.project_invalid",
            "user_assets.project_required",
        }:
            return None
        raise
    if coding_cli is not None and coding_cli not in CODING_CLIS:
        raise CodingCliError(
            "coding_cli.unsupported",
            "CODING_CLI must be codex, claude, cursor-agent, or opencode",
        )
    return coding_cli


def resolve_live_test_selection(
    *,
    coding_cli: str | None = None,
    model: str | None = None,
    environment: Mapping[str, str] | None = None,
    test_config_path: Path | None = None,
    project_root: Path | None = None,
    require_opencode: bool = False,
    ignore_environment_pins: bool = False,
) -> LiveTestSelection:
    """Bind one coding CLI and model for live qualification.

    Precedence is ``--coding-cli``/``--model``, then ``CODING_CLI``/
    ``LITAI_LIVE_MODEL``, then the project-scoped user test config. PATH
    first-available search is not a live-test default.
    """

    configured = dict(os.environ if environment is None else environment)
    environment_cli = None if ignore_environment_pins else configured.get("CODING_CLI")
    environment_model = (
        None if ignore_environment_pins else configured.get(LIVE_MODEL_ENVIRONMENT)
    )
    higher_cli, _ = _pick(
        ("cli-flag", coding_cli),
        ("environment", environment_cli),
    )
    higher_model, _ = _pick(
        ("cli-flag", model),
        ("environment", environment_model),
    )
    try:
        config_path = (
            test_config_path
            if test_config_path is not None
            else default_test_config_path(
                project_root=project_root, environment=configured
            )
        )
    except CodingCliError as exc:
        if (
            exc.code != "user_assets.project_required"
            or higher_cli is None
            or higher_model is None
        ):
            raise
        file_cli, file_model = None, None
    else:
        file_cli, file_model = _test_config_pin(config_path)
    selected_cli, cli_provenance = _pick(
        ("cli-flag", coding_cli),
        ("environment", environment_cli),
        ("test-config", file_cli),
    )
    selected_model, model_provenance = _pick(
        ("cli-flag", model),
        ("environment", environment_model),
        ("test-config", file_model),
    )
    if selected_cli is None or selected_model is None:
        raise CodingCliError(
            "coding_cli.test_selection_unconfigured",
            "live qualification requires an explicit coding CLI and model; set "
            "--coding-cli and --model, or CODING_CLI and LITAI_LIVE_MODEL, or "
            "coding_cli and model in the project-scoped user test configuration",
        )
    if selected_cli not in CODING_CLIS:
        raise CodingCliError(
            "coding_cli.unsupported",
            "CODING_CLI must be codex, claude, cursor-agent, or opencode",
        )
    # A model names a model for one coding CLI. A higher-precedence CLI pin
    # (often an ambient CODING_CLI) never borrows the test config's model when
    # the config pairs that model with a different CLI.
    if (
        model_provenance == PROVENANCE_TEST_CONFIG
        and cli_provenance != PROVENANCE_TEST_CONFIG
        and file_cli is not None
        and file_cli != selected_cli
    ):
        raise CodingCliError(
            "coding_cli.test_selection_mismatch",
            f"{'--coding-cli' if cli_provenance == PROVENANCE_CLI else 'CODING_CLI'}"
            f" selects {selected_cli}, but the test configuration's model "
            f"{selected_model!r} is for {file_cli}; also pin the model (--model "
            f"or {LIVE_MODEL_ENVIRONMENT})"
            + (
                ", or unset CODING_CLI to use the configured pair"
                if cli_provenance == PROVENANCE_ENVIRONMENT
                else ""
            ),
        )
    remote = live_gate_is_remote(configured)
    enforce_opencode = require_opencode or (remote and cli_provenance != PROVENANCE_CLI)
    if enforce_opencode and selected_cli != "opencode":
        raise CodingCliError(
            "coding_cli.remote_prerequisite",
            "remote live qualification requires coding CLI opencode; pass "
            "--coding-cli with --model for a one-shot override of that default",
        )
    # No provider credential is required here: opencode resolves credentials
    # for the pinned model's provider from its own configuration and auth
    # store, which the controller cannot observe.
    return LiveTestSelection(
        selected_cli,
        selected_model,
        cli_provenance,
        model_provenance,
        user_coding_cli=file_cli,
        user_model=file_model,
    )


#: A trivial, bounded, non-source-writing task the selected coding CLI + model
#: must be able to answer. If the model id does not resolve (e.g. a mis-qualified
#: ``router/...`` instead of ``custom-provider/router/...``,
#: or any string the user set by mistake), the CLI fails here with the real
#: underlying cause instead of deep inside a generation run masked as a generic
#: server error.
_MODEL_PREFLIGHT_PROMPT = (
    "Respond with only this exact JSON object and nothing else: "
    '{"schema":"literate-ai/live-model-preflight@1","ok":true}'
)
_MODEL_PREFLIGHT_SCHEMA = "literate-ai/live-model-preflight@1"
_MODEL_PREFLIGHT_TIMEOUT_SECONDS = 180


def verify_live_model_resolves(
    selection: LiveTestSelection,
    *,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: int = _MODEL_PREFLIGHT_TIMEOUT_SECONDS,
) -> None:
    """Fail closed unless the selected coding CLI + model actually resolves.

    The pin in project-scoped user config (or ``--model`` / ``LITAI_LIVE_MODEL``) is
    an unvalidated string until now: nothing proves the model id exists for the
    chosen coding CLI before an expensive live run trusts it. This runs one
    minimal bounded task through the selected CLI and model; a model that does
    not resolve raises ``coding_cli.model_unavailable`` naming the model and the
    underlying CLI cause, rather than surfacing mid-generation as an opaque
    "Unexpected server error".

    It is the caller's responsibility to run this once, before the first
    generation of a live run — not inside :func:`resolve_live_test_selection`,
    which stays a pure, network-free string resolution.
    """

    # Imported lazily: resolving/serializing a selection must not require the
    # heavyweight coding-CLI transport, only verification does.
    from literate_ai.adapters.models.coding_cli import (
        CodingCliError,
        CodingCliTaskRunner,
    )

    configured = dict(os.environ if environment is None else environment)
    configured = apply_live_test_selection(configured, selection)
    try:
        runner = CodingCliTaskRunner(
            environment=configured, timeout_seconds=timeout_seconds
        )
        result = runner.run_json_task(_MODEL_PREFLIGHT_PROMPT, model=selection.model)
    except CodingCliError as exc:
        raise CodingCliError(
            "coding_cli.model_unavailable",
            f"live-qualification model {selection.model!r} did not resolve for "
            f"coding CLI {selection.coding_cli!r} ({exc.code}): {exc}. Set a model "
            f"the CLI can run in user test config (or --model / LITAI_LIVE_MODEL); "
            f"list available ids with the coding CLI (for opencode: `opencode "
            f"models`).",
        ) from exc
    if not isinstance(result.response, dict) or (
        result.response.get("schema") != _MODEL_PREFLIGHT_SCHEMA
    ):
        raise CodingCliError(
            "coding_cli.model_unavailable",
            f"live-qualification model {selection.model!r} responded but did not "
            f"return the expected preflight JSON for coding CLI "
            f"{selection.coding_cli!r}; the model may be reachable but unusable for "
            f"generation. Choose a different model in user test config.",
        )


def apply_live_test_selection(
    environment: dict[str, str],
    selection: LiveTestSelection,
) -> dict[str, str]:
    """Write the pin into a mutable process environment."""

    environment.update(selection.environment_overlay())
    return environment


def log_live_session_models(selection: LiveTestSelection) -> None:
    """Log the user-default tuple and a distinct CLI-override event when flags win."""

    from literate_ai.diagnostics import emit_log_event

    emit_log_event(
        "model.session.user_default",
        coding_cli=selection.user_coding_cli or "",
        model=selection.user_model or "",
    )
    if selection.cli_flags_override_user_default():
        emit_log_event(
            "model.session.cli_override",
            coding_cli=selection.coding_cli,
            model=selection.model,
            coding_cli_provenance=selection.coding_cli_provenance,
            model_provenance=selection.model_provenance,
            user_coding_cli=selection.user_coding_cli or "",
            user_model=selection.user_model or "",
        )


def _pick(
    *candidates: tuple[_Provenance, object],
) -> tuple[str | None, _Provenance]:
    for provenance, raw in candidates:
        text = _explicit_text(raw)
        if text is not None:
            return text, provenance
    return None, PROVENANCE_TEST_CONFIG


def _explicit_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _test_config_pin(path: Path) -> tuple[str | None, str | None]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise CodingCliError(
            "coding_cli.test_selection_unconfigured",
            "live-test configuration could not be read",
        ) from exc
    if not path.is_file() or metadata.st_size < 2:
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodingCliError(
            "coding_cli.test_selection_invalid",
            "live-test configuration must be UTF-8 JSON",
        ) from exc
    if not isinstance(value, dict):
        raise CodingCliError(
            "coding_cli.test_selection_invalid",
            "live-test configuration must be one JSON object",
        )
    return (
        _required_optional_string(value, "coding_cli"),
        _required_optional_string(value, "model"),
    )


def _required_optional_string(value: Mapping[str, object], field: str) -> str | None:
    if field not in value:
        return None
    raw = value[field]
    if not isinstance(raw, str) or not raw.strip():
        raise CodingCliError(
            "coding_cli.test_selection_invalid",
            f"live-test configuration field {field!r} must be a non-empty string",
        )
    return raw.strip()
