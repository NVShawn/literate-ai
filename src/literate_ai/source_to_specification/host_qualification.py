"""Concrete local-host providers for measured regenerative qualification.

These adapters execute commands and derive evidence from their observed results. They
never deserialize caller-supplied qualification evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters._processes import run_with_tree_kill

from .contracts import canonical_digest, canonical_value
from .errors import SourceToSpecificationError
from .inventory import inventory_source
from .qualification_runner import (
    ParityOutcome,
    ParityVerificationRequest,
    QualificationRunCheckpoint,
    RegenerationOutcome,
    RegenerationRunPlan,
)

LOCAL_QUALIFICATION_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:local-regenerative-qualification-profile"
)
LOCAL_QUALIFICATION_SIGNATURE_SCHEMA = (
    "urn:literate-ai:schema:v2:local-qualification-signature"
)
LOCAL_QUALIFICATION_CHECKPOINT_DIRECTORY = "qualification-checkpoints"
_PLACEHOLDER = re.compile(r"\{[^{}]+\}")
_CONTENT_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}")
_ALLOWED_PLACEHOLDERS = frozenset({"{workspace}", "{build_root}"})


def _canonical_json(value: object) -> str:
    return json.dumps(
        canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _content_identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _CONTENT_IDENTITY.fullmatch(value) is None:
        raise SourceToSpecificationError(
            "qualification_host.identity_invalid",
            f"{label} must be an exact sha256 content identity",
        )
    return value


def _command(value: Sequence[str], *, label: str) -> tuple[str, ...]:
    command = tuple(value)
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise SourceToSpecificationError(
            "qualification_host.command_invalid",
            f"{label} must be a non-empty argument vector",
        )
    unknown = {
        match.group(0)
        for item in command
        for match in _PLACEHOLDER.finditer(item)
        if match.group(0) not in _ALLOWED_PLACEHOLDERS
    }
    if unknown:
        raise SourceToSpecificationError(
            "qualification_host.placeholder_invalid",
            f"{label} contains unsupported placeholders",
        )
    return command


def _expand(
    command: tuple[str, ...], *, workspace: Path, build_root: Path
) -> tuple[str, ...]:
    replacements = {
        "{workspace}": str(workspace),
        "{build_root}": str(build_root),
    }
    return tuple(
        argument.replace("{workspace}", replacements["{workspace}"]).replace(
            "{build_root}", replacements["{build_root}"]
        )
        for argument in command
    )


def _process_identity(
    *, command: tuple[str, ...], returncode: int, stdout: bytes, stderr: bytes
) -> str:
    return canonical_digest(
        {
            "command": command,
            "returncode": returncode,
            "stdout": f"sha256:{hashlib.sha256(stdout).hexdigest()}",
            "stderr": f"sha256:{hashlib.sha256(stderr).hexdigest()}",
        }
    )


def _observe(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
    maximum_output_bytes: int,
) -> tuple[bytes, bytes, str, int]:
    try:
        completed = run_with_tree_kill(
            command,
            cwd=cwd,
            env=dict(environment),
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceToSpecificationError(
            "qualification_host.command_timeout",
            "qualification command exceeded its bounded timeout",
        ) from exc
    except OSError as exc:
        raise SourceToSpecificationError(
            "qualification_host.command_unavailable",
            "qualification command could not be started",
        ) from exc
    if (
        len(completed.stdout) > maximum_output_bytes
        or len(completed.stderr) > maximum_output_bytes
    ):
        raise SourceToSpecificationError(
            "qualification_host.output_limit",
            "qualification command exceeded its bounded output limit",
        )
    identity = _process_identity(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    return completed.stdout, completed.stderr, identity, completed.returncode


def _run(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
    maximum_output_bytes: int,
) -> tuple[bytes, str]:
    stdout, stderr, identity, returncode = _observe(
        command,
        cwd=cwd,
        environment=environment,
        timeout_seconds=timeout_seconds,
        maximum_output_bytes=maximum_output_bytes,
    )
    if returncode != 0:
        diagnostic = ""
        try:
            error_envelope = json.loads(stderr.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            error_envelope = None
        if (
            isinstance(error_envelope, dict)
            and error_envelope.get("schema") == "literate-ai/cli-error@1"
            and error_envelope.get("ok") is False
            and isinstance(error_envelope.get("error"), dict)
        ):
            error = error_envelope["error"]
            code = error.get("code")
            message = error.get("message")
            if isinstance(code, str) and isinstance(message, str):
                diagnostic = f"; {code}: {message[:512]}"
        raise SourceToSpecificationError(
            "qualification_host.command_failed",
            f"qualification command failed with exit status {returncode} "
            f"(result {identity}){diagnostic}",
        )
    return stdout, identity


def _strict_json(content: bytes) -> object:
    try:
        text = content.decode("utf-8")
        return json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceToSpecificationError(
            "qualification_host.application_output_invalid",
            "qualified applications must emit exactly one UTF-8 JSON value",
        ) from exc


_NATIVE_ROOT_LOCATOR = ".litai-native-root"


def _generated_native_root(
    *,
    workspace: Path,
    build_root: Path,
    generated_root: str,
    generation_stdout: bytes | None = None,
) -> Path:
    """Resolve the root Component's native tree without assuming flat output."""

    direct = workspace.joinpath(*Path(generated_root).parts)
    marker = build_root / _NATIVE_ROOT_LOCATOR
    candidate: Path | None = (
        direct if direct.is_dir() and not direct.is_symlink() else None
    )
    if candidate is None and generation_stdout is not None:
        envelope = _strict_json(generation_stdout)
        result = envelope.get("result") if isinstance(envelope, dict) else None
        custody = (
            result.get("standard_source_generation")
            if isinstance(result, dict)
            else None
        )
        root_revision = (
            custody.get("root_revision") if isinstance(custody, dict) else None
        )
        components = custody.get("components") if isinstance(custody, dict) else None
        matches = (
            [
                item
                for item in components
                if isinstance(item, dict)
                and item.get("component_revision") == root_revision
            ]
            if isinstance(components, list)
            else []
        )
        if len(matches) == 1 and isinstance(matches[0].get("workspace_locator"), str):
            component_workspace = Path(matches[0]["workspace_locator"]).resolve()
            try:
                component_workspace.relative_to(workspace)
            except ValueError:
                component_workspace = Path()
            if component_workspace != Path():
                nested = component_workspace.joinpath(*Path(generated_root).parts)
                if nested.is_dir() and not nested.is_symlink():
                    candidate = nested
    if candidate is None and generation_stdout is None and marker.is_file():
        relative = marker.read_text(encoding="utf-8").strip()
        if (
            relative
            and not Path(relative).is_absolute()
            and ".." not in Path(relative).parts
        ):
            recorded = workspace.joinpath(*Path(relative).parts).resolve()
            try:
                recorded.relative_to(workspace)
            except ValueError:
                recorded = Path()
            if recorded != Path() and recorded.is_dir() and not recorded.is_symlink():
                candidate = recorded
    if candidate is None:
        raise SourceToSpecificationError(
            "qualification_host.generated_root_missing",
            "generation did not produce the profile's native source root",
        )
    if generation_stdout is not None:
        marker.write_text(
            candidate.relative_to(workspace).as_posix(), encoding="utf-8", newline="\n"
        )
    return candidate


@dataclass(frozen=True, slots=True)
class LocalQualificationCase:
    """One verifier-owned argument vector and exact expected application result."""

    case_id: str
    arguments: tuple[object, ...]
    expected_result: object

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "qualification case_id must not be empty",
            )
        try:
            canonical_value(list(self.arguments))
            canonical_value(self.expected_result)
        except (TypeError, ValueError) as exc:
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "qualification case values must be canonical JSON",
            ) from exc

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "arguments": canonical_value(self.arguments),
            "expected_result": canonical_value(self.expected_result),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> LocalQualificationCase:
        if not isinstance(value, Mapping) or set(value) != {
            "case_id",
            "arguments",
            "expected_result",
        }:
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "qualification case fields are invalid",
            )
        arguments = value["arguments"]
        if not isinstance(arguments, list):
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "qualification case arguments must be an array",
            )
        return cls(
            case_id=value["case_id"],  # type: ignore[arg-type]
            arguments=tuple(arguments),
            expected_result=value["expected_result"],
        )


@dataclass(frozen=True, slots=True)
class LocalQualificationProfile:
    """Content-pinned commands and probes; this is an execution plan, not evidence."""

    profile_id: str
    build_command: tuple[str, ...]
    test_commands: tuple[tuple[str, ...], ...]
    source_command: tuple[str, ...]
    generated_command: tuple[str, ...]
    cases: tuple[LocalQualificationCase, ...]
    covered_surface_ids: tuple[str, ...]
    generated_root: str = "source"
    minimum_clean_runs: int = 2
    timeout_seconds: int = 900
    maximum_output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid", "profile_id must not be empty"
            )
        object.__setattr__(
            self, "build_command", _command(self.build_command, label="build_command")
        )
        object.__setattr__(
            self,
            "test_commands",
            tuple(_command(item, label="test_commands") for item in self.test_commands),
        )
        object.__setattr__(
            self,
            "source_command",
            _command(self.source_command, label="source_command"),
        )
        object.__setattr__(
            self,
            "generated_command",
            _command(self.generated_command, label="generated_command"),
        )
        if not self.test_commands or not self.cases:
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "qualification requires build, generated tests, and parity cases",
            )
        if any(not isinstance(item, LocalQualificationCase) for item in self.cases):
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "qualification cases must be typed verifier-owned cases",
            )
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "qualification case IDs must be unique",
            )
        generated = Path(self.generated_root)
        if (
            not self.generated_root
            or generated.is_absolute()
            or any(part in {"", ".", ".."} for part in generated.parts)
        ):
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "generated_root must be a safe relative directory",
            )
        surfaces = tuple(sorted(set(self.covered_surface_ids)))
        if not surfaces or len(surfaces) != len(self.covered_surface_ids):
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "covered_surface_ids must contain unique non-empty surfaces",
            )
        object.__setattr__(self, "covered_surface_ids", surfaces)
        if self.minimum_clean_runs < 2:
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "minimum_clean_runs must be at least two",
            )
        if self.timeout_seconds < 1 or self.maximum_output_bytes < 1:
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "process bounds must be positive",
            )

    @property
    def identity(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": LOCAL_QUALIFICATION_PROFILE_SCHEMA,
            "profile_id": self.profile_id,
            "build_command": list(self.build_command),
            "test_commands": [list(item) for item in self.test_commands],
            "source_command": list(self.source_command),
            "generated_command": list(self.generated_command),
            "cases": [item.to_dict() for item in self.cases],
            "covered_surface_ids": list(self.covered_surface_ids),
            "generated_root": self.generated_root,
            "minimum_clean_runs": self.minimum_clean_runs,
            "timeout_seconds": self.timeout_seconds,
            "maximum_output_bytes": self.maximum_output_bytes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> LocalQualificationProfile:
        expected = {
            "schema",
            "profile_id",
            "build_command",
            "test_commands",
            "source_command",
            "generated_command",
            "cases",
            "covered_surface_ids",
            "generated_root",
            "minimum_clean_runs",
            "timeout_seconds",
            "maximum_output_bytes",
        }
        if (
            set(value) != expected
            or value.get("schema") != LOCAL_QUALIFICATION_PROFILE_SCHEMA
        ):
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "qualification profile schema or exact fields are invalid",
            )
        try:
            return cls(
                profile_id=str(value["profile_id"]),
                build_command=tuple(value["build_command"]),  # type: ignore[arg-type]
                test_commands=tuple(
                    tuple(item)
                    for item in value["test_commands"]  # type: ignore[union-attr]
                ),
                source_command=tuple(value["source_command"]),  # type: ignore[arg-type]
                generated_command=tuple(value["generated_command"]),  # type: ignore[arg-type]
                cases=tuple(
                    LocalQualificationCase.from_dict(item)
                    for item in value["cases"]  # type: ignore[union-attr]
                ),
                covered_surface_ids=tuple(value["covered_surface_ids"]),  # type: ignore[arg-type]
                generated_root=str(value["generated_root"]),
                minimum_clean_runs=value["minimum_clean_runs"],  # type: ignore[arg-type]
                timeout_seconds=value["timeout_seconds"],  # type: ignore[arg-type]
                maximum_output_bytes=value["maximum_output_bytes"],  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise SourceToSpecificationError(
                "qualification_host.profile_invalid",
                "qualification profile contains invalid typed values",
            ) from exc


class LocalHostSpecRegenerator:
    """Run exact forward generation, build, and tests from an empty workspace."""

    def __init__(
        self,
        *,
        generation_command: Sequence[str],
        generation_cwd: Path,
        profile: LocalQualificationProfile,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.generation_command = _command(
            generation_command, label="generation_command"
        )
        if not any("{workspace}" in item for item in self.generation_command):
            raise SourceToSpecificationError(
                "qualification_host.command_invalid",
                "generation_command must bind the clean workspace",
            )
        self.generation_cwd = generation_cwd.resolve(strict=True)
        self.profile = profile
        self.environment = dict(os.environ if environment is None else environment)
        self._provider_identity = canonical_digest(
            {
                "provider": "local-host-spec-regenerator@1",
                "generation_command": self.generation_command,
                "generation_cwd": str(self.generation_cwd),
                "profile_identity": profile.identity,
            }
        )

    @property
    def provider_identity(self) -> str:
        return self._provider_identity

    def regenerate(
        self, request: RegenerationRunPlan, workspace: Path
    ) -> RegenerationOutcome:
        if any(workspace.iterdir()):
            raise SourceToSpecificationError(
                "qualification_host.workspace_not_empty",
                "regeneration workspace must be empty",
            )
        build_root = workspace.parent / f"{workspace.name}-build"
        build_root.mkdir()
        generation = _expand(
            self.generation_command, workspace=workspace, build_root=build_root
        )
        generation_stdout, _generation_id = _run(
            generation,
            cwd=self.generation_cwd,
            environment=self.environment,
            timeout_seconds=self.profile.timeout_seconds,
            maximum_output_bytes=self.profile.maximum_output_bytes,
        )
        inventory = inventory_source(workspace)
        generated_tree_id = inventory.identity
        native_root = _generated_native_root(
            workspace=workspace,
            build_root=build_root,
            generated_root=self.profile.generated_root,
            generation_stdout=generation_stdout,
        )
        build = _expand(
            self.profile.build_command, workspace=workspace, build_root=build_root
        )
        _, build_id = _run(
            build,
            cwd=native_root,
            environment=self.environment,
            timeout_seconds=self.profile.timeout_seconds,
            maximum_output_bytes=self.profile.maximum_output_bytes,
        )
        test_ids: list[str] = []
        for command in self.profile.test_commands:
            _, result_id = _run(
                _expand(command, workspace=workspace, build_root=build_root),
                cwd=native_root,
                environment=self.environment,
                timeout_seconds=self.profile.timeout_seconds,
                maximum_output_bytes=self.profile.maximum_output_bytes,
            )
            test_ids.append(result_id)
        return RegenerationOutcome(
            generation_input_ids=request.declared_generation_inputs,
            generated_tree_id=generated_tree_id,
            build_result_id=build_id,
            generated_test_result_id=canonical_digest(test_ids),
            generated_source_cache_hit=False,
            build_passed=True,
            generated_tests_total=len(test_ids),
            generated_tests_succeeded=len(test_ids),
            generated_tests_failed=0,
            generated_tests_skipped=0,
        )


class LocalHostParityVerifier:
    """Independently execute baseline and generated apps for exact JSON probes."""

    def __init__(
        self,
        *,
        source_root: Path,
        source_snapshot_id: str,
        profile: LocalQualificationProfile,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.source_root = source_root.resolve(strict=True)
        self.source_snapshot_id = source_snapshot_id
        self.profile = profile
        self.environment = dict(os.environ if environment is None else environment)
        self._provider_identity = canonical_digest(
            {
                "provider": "local-host-independent-parity@1",
                "source_snapshot_id": source_snapshot_id,
                "profile_identity": profile.identity,
            }
        )

    @property
    def provider_identity(self) -> str:
        return self._provider_identity

    def verify(
        self, request: ParityVerificationRequest, generated_workspace: Path
    ) -> ParityOutcome:
        if request.source_snapshot_id != self.source_snapshot_id:
            raise SourceToSpecificationError(
                "qualification_host.source_mismatch",
                "parity request selected another source snapshot",
            )
        if inventory_source(self.source_root).identity != self.source_snapshot_id:
            raise SourceToSpecificationError(
                "qualification_host.source_drift",
                "source baseline changed before parity verification",
            )
        build_root = generated_workspace.parent / f"{generated_workspace.name}-build"
        native_root = _generated_native_root(
            workspace=generated_workspace,
            build_root=build_root,
            generated_root=self.profile.generated_root,
        )
        observations: list[dict[str, object]] = []
        passed = True
        for index, case in enumerate(self.profile.cases):
            argument = _canonical_json(case.arguments)
            source_stdout, source_result = _run(
                (*self.profile.source_command, argument),
                cwd=self.source_root,
                environment=self.environment,
                timeout_seconds=self.profile.timeout_seconds,
                maximum_output_bytes=self.profile.maximum_output_bytes,
            )
            (
                generated_stdout,
                _generated_stderr,
                generated_result,
                generated_returncode,
            ) = _observe(
                (
                    *_expand(
                        self.profile.generated_command,
                        workspace=generated_workspace,
                        build_root=build_root,
                    ),
                    argument,
                ),
                cwd=native_root,
                environment=self.environment,
                timeout_seconds=self.profile.timeout_seconds,
                maximum_output_bytes=self.profile.maximum_output_bytes,
            )
            source_value = _strict_json(source_stdout)
            generated_value: object | None = None
            generated_output_valid = False
            if generated_returncode == 0:
                try:
                    generated_value = _strict_json(generated_stdout)
                    generated_output_valid = True
                except SourceToSpecificationError as exc:
                    if exc.code != "qualification_host.application_output_invalid":
                        raise
            equal = generated_output_valid and source_value == generated_value
            passed = passed and equal
            observations.append(
                {
                    "case": index,
                    "argument": canonical_digest(case),
                    "source_result": source_result,
                    "generated_result": generated_result,
                    "generated_exit_status": generated_returncode,
                    "generated_output_valid": generated_output_valid,
                    "equal": equal,
                }
            )
        if inventory_source(self.source_root).identity != self.source_snapshot_id:
            raise SourceToSpecificationError(
                "qualification_host.source_drift",
                "source baseline changed during parity verification",
            )
        return ParityOutcome(
            result_id=canonical_digest(observations),
            passed=passed,
            covered_surface_ids=(
                request.covered_surface_ids
                if passed
                else self.profile.covered_surface_ids
            ),
        )


class LocalHmacQualificationAttestor:
    """Sign exact measured payloads using an operator-held local trust key."""

    def __init__(self, *, signer: str, key: bytes) -> None:
        if not signer.strip() or len(key) < 32:
            raise SourceToSpecificationError(
                "qualification_host.attestor_invalid",
                "attestor requires a signer and at least 32 bytes of key material",
            )
        self.signer = signer
        self._key = bytes(key)
        self.envelopes: list[dict[str, object]] = []
        self._provider_identity = canonical_digest(
            {
                "provider": "local-hmac-qualification-attestor@2",
                "signer": signer,
                "verification_key_identity": canonical_digest(self._key.hex()),
            }
        )

    @property
    def provider_identity(self) -> str:
        return self._provider_identity

    def attest(self, payload: Mapping[str, object]) -> str:
        payload_value = canonical_value(payload)
        payload_identity = canonical_digest(payload_value)
        signature = hmac.new(
            self._key, _canonical_json(payload_value).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        envelope = {
            "schema": LOCAL_QUALIFICATION_SIGNATURE_SCHEMA,
            "signer": self.signer,
            "payload_identity": payload_identity,
            "signature": f"hmac-sha256:{signature}",
        }
        identity = canonical_digest(envelope)
        self.envelopes.append({**envelope, "identity": identity})
        return identity

    def verify(self, payload: Mapping[str, object], attestation_id: str) -> bool:
        try:
            supplied = _content_identity(attestation_id, label="attestation_id")
        except SourceToSpecificationError:
            return False
        payload_value = canonical_value(payload)
        signature = hmac.new(
            self._key, _canonical_json(payload_value).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        expected = canonical_digest(
            {
                "schema": LOCAL_QUALIFICATION_SIGNATURE_SCHEMA,
                "signer": self.signer,
                "payload_identity": canonical_digest(payload_value),
                "signature": f"hmac-sha256:{signature}",
            }
        )
        return hmac.compare_digest(expected, supplied)


class LocalFilesystemQualificationCheckpointStore:
    """Compact, atomic storage for signed qualification-run evidence."""

    def __init__(self, root: Path) -> None:
        supplied = Path(root)
        if supplied.is_symlink():
            raise SourceToSpecificationError(
                "qualification_host.checkpoint_root_invalid",
                "qualification checkpoint root cannot be a symbolic link",
            )
        supplied.mkdir(parents=True, exist_ok=True)
        self.root = supplied.resolve(strict=True)
        if not self.root.is_dir():
            raise SourceToSpecificationError(
                "qualification_host.checkpoint_root_invalid",
                "qualification checkpoint root must be a directory",
            )

    def _path(self, checkpoint_key: str) -> Path:
        normalized = _content_identity(checkpoint_key, label="checkpoint_key")
        return self.root / f"{normalized.removeprefix('sha256:')}.json"

    def load(self, checkpoint_key: str) -> QualificationRunCheckpoint | None:
        path = self._path(checkpoint_key)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise SourceToSpecificationError(
                "qualification_host.checkpoint_invalid",
                "qualification checkpoint must be a regular file",
            )
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceToSpecificationError(
                "qualification_host.checkpoint_invalid",
                "qualification checkpoint is unreadable or malformed",
            ) from exc
        return QualificationRunCheckpoint.from_dict(value)

    def save(self, checkpoint: QualificationRunCheckpoint) -> None:
        path = self._path(checkpoint.checkpoint_key)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".qualification-checkpoint-", dir=self.root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(_canonical_json(checkpoint.to_dict()))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                existing = self.load(checkpoint.checkpoint_key)
                if existing != checkpoint:
                    raise SourceToSpecificationError(
                        "qualification_host.checkpoint_conflict",
                        "qualification checkpoint key already names different evidence",
                    ) from exc
        finally:
            temporary.unlink(missing_ok=True)


__all__ = [
    "LOCAL_QUALIFICATION_PROFILE_SCHEMA",
    "LOCAL_QUALIFICATION_SIGNATURE_SCHEMA",
    "LOCAL_QUALIFICATION_CHECKPOINT_DIRECTORY",
    "LocalFilesystemQualificationCheckpointStore",
    "LocalHmacQualificationAttestor",
    "LocalHostParityVerifier",
    "LocalHostSpecRegenerator",
    "LocalQualificationCase",
    "LocalQualificationProfile",
]
