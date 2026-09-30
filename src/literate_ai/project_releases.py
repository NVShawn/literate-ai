"""Project-owned release policy, planning, preparation, and publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.execution_dispatch import (
    ExecutionDispatchAdapterError,
    load_execution_worker_catalog,
)
from literate_ai.adapters.live_test_selection import (
    posix_export_prefix,
    remote_live_gate_overlay,
    try_resolve_live_test_selection,
)
from literate_ai.adapters.ssh_transport import (
    BoundedSshProcessRunner,
    SshTransportError,
    ssh_arguments,
)
from literate_ai.adapters.user_assets import (
    UserAssetPathError,
    resolve_worker_config_path,
)
from literate_ai.contracts import (
    ExecutionWorker,
    ExecutionWorkerKind,
    SemanticVersion,
    canonical_identity,
)
from literate_ai.diagnostics import report_progress
from literate_ai.evidence_ledger import (
    EvidenceNode,
    EvidenceRun,
    attach_run,
    open_run,
    record_subprocess,
)
from literate_ai.perf import PerformanceRecorder
from literate_ai.projects import (
    ProjectConfigurationStore,
    ProjectError,
    discover_project,
)

RELEASE_TARGETS = frozenset({"local", "github", "gitlab"})
WORKERS_CATALOG_FILE = "workers.json"
_WORKER_PROBE_TIMEOUT_SECONDS = 60
_GITHUB_POLL_INTERVAL_SECONDS = 15

RELEASE_POLICY_FILE = "literate.release.json"
RELEASE_POLICY_SCHEMA = "literate-ai/release-policy@2"
LEGACY_RELEASE_POLICY_SCHEMA = "literate-ai/release-policy@1"
RELEASE_PLAN_SCHEMA = "literate-ai/release-plan@1"
PREPARED_RELEASE_SCHEMA = "literate-ai/prepared-release@2"
LEGACY_PREPARED_RELEASE_SCHEMA = "literate-ai/prepared-release@1"
PUBLICATION_RECEIPT_SCHEMA = "literate-ai/release-publication-receipt@1"
PUBLISHED_VERIFICATION_SCHEMA = "literate-ai/release-published-verification@1"
DOCUMENT_PAIR_PUBLICATION_SCHEMA = "literate-ai/release-document-pair-publication@1"
RELEASE_BACKPORT_SCHEMA = "literate-ai/release-backport@1"
RELEASE_BACKPORT_STATUS_SCHEMA = "literate-ai/release-backport-status@1"

_RELATIVE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+$")
_PYTHON_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_QUOTED_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CARGO_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAJOR_MINOR = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_RELEASE_LINE = re.compile(r"^release/(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.x$")
_RELEASE_ENGINEER = re.compile(r"^- `([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))`$")


class ProjectReleaseError(RuntimeError):
    """A release phase could not preserve its declared authority boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class VersionBinding:
    path: str
    format: str
    selectors: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object, *, path: str) -> VersionBinding:
        data = _mapping(value, path)
        _keys(data, path, {"path", "format", "selectors"})
        relative = _relative_path(data["path"], f"{path}.path")
        format_name = _string(data["format"], f"{path}.format")
        if format_name not in {
            "json-pointer",
            "python-constant",
            "quoted-constant",
            "cargo-lock-package",
        }:
            _fail(
                f"{path}.format",
                "must be json-pointer, python-constant, quoted-constant, "
                "or cargo-lock-package",
            )
        selectors = _strings(data["selectors"], f"{path}.selectors", minimum=1)
        if len(set(selectors)) != len(selectors):
            _fail(f"{path}.selectors", "selectors must be unique")
        if format_name == "json-pointer":
            if any(not item.startswith("/") or item == "/" for item in selectors):
                _fail(f"{path}.selectors", "JSON selectors must be non-root pointers")
        elif format_name == "cargo-lock-package":
            if any(not _CARGO_NAME.fullmatch(item) for item in selectors):
                _fail(f"{path}.selectors", "Cargo selectors must be package names")
        else:
            names = _QUOTED_NAME if format_name == "quoted-constant" else _PYTHON_NAME
            if any(not names.fullmatch(item) for item in selectors):
                _fail(f"{path}.selectors", "constant selectors must be names")
        return cls(relative, format_name, selectors)

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "format": self.format,
            "selectors": list(self.selectors),
        }


@dataclass(frozen=True, slots=True)
class ReleaseContributionsPolicy:
    current_milestone: str

    @classmethod
    def from_dict(cls, value: object, *, path: str) -> ReleaseContributionsPolicy:
        data = _mapping(value, path)
        _keys(data, path, {"current_milestone"})
        milestone = _string(data["current_milestone"], f"{path}.current_milestone")
        remainder = milestone.replace("{version}", "")
        if milestone.count("{version}") > 1 or "{" in remainder or "}" in remainder:
            _fail(
                f"{path}.current_milestone",
                "must be a literal milestone or contain one {version} placeholder",
            )
        return cls(milestone)

    def to_dict(self) -> dict[str, str]:
        return {"current_milestone": self.current_milestone}


@dataclass(frozen=True, slots=True)
class ReleaseCollateralPolicy:
    name: str
    ecosystem: str
    required_for: tuple[str, ...]
    presentation_path: str
    narrative_path: str
    verification_report_path: str
    publication_receipt_path: str
    presentation_resource_id: str
    narrative_resource_id: str

    @classmethod
    def from_dict(cls, value: object, *, path: str) -> ReleaseCollateralPolicy:
        data = _mapping(value, path)
        _keys(
            data,
            path,
            {
                "kind",
                "name",
                "ecosystem",
                "required_for",
                "artifacts",
                "verification_report",
                "publication_receipt",
                "resources",
            },
        )
        if data["kind"] != "document-pair":
            _fail(f"{path}.kind", "only document-pair is currently supported")
        required_for = _strings(data["required_for"], f"{path}.required_for")
        allowed_classes = {"initial", "minor", "major"}
        if (
            len(set(required_for)) != len(required_for)
            or not set(required_for) <= allowed_classes
        ):
            _fail(
                f"{path}.required_for",
                "must contain unique initial, minor, or major release classes",
            )
        artifacts = _mapping(data["artifacts"], f"{path}.artifacts")
        _keys(artifacts, f"{path}.artifacts", {"presentation", "narrative"})
        resources = _mapping(data["resources"], f"{path}.resources")
        _keys(resources, f"{path}.resources", {"presentation", "narrative"})
        resource_ids = tuple(
            _string(resources[member], f"{path}.resources.{member}")
            for member in ("presentation", "narrative")
        )
        if any(
            re.fullmatch(r"[A-Za-z0-9_-]{10,256}", resource_id) is None
            for resource_id in resource_ids
        ):
            _fail(f"{path}.resources", "resource IDs must be opaque safe identifiers")
        return cls(
            _string(data["name"], f"{path}.name"),
            _string(data["ecosystem"], f"{path}.ecosystem"),
            required_for,
            _relative_path(artifacts["presentation"], f"{path}.artifacts.presentation"),
            _relative_path(artifacts["narrative"], f"{path}.artifacts.narrative"),
            _relative_path(data["verification_report"], f"{path}.verification_report"),
            _relative_path(data["publication_receipt"], f"{path}.publication_receipt"),
            resource_ids[0],
            resource_ids[1],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "document-pair",
            "name": self.name,
            "ecosystem": self.ecosystem,
            "required_for": list(self.required_for),
            "artifacts": {
                "presentation": self.presentation_path,
                "narrative": self.narrative_path,
            },
            "verification_report": self.verification_report_path,
            "publication_receipt": self.publication_receipt_path,
            "resources": {
                "presentation": self.presentation_resource_id,
                "narrative": self.narrative_resource_id,
            },
        }


@dataclass(frozen=True, slots=True)
class ReleasePolicy:
    schema: str
    version_scheme: str
    version_authority: VersionBinding
    version_mirrors: tuple[VersionBinding, ...]
    allowed_transitions: tuple[str, ...]
    gate_argv: tuple[str, ...]
    gate_timeout_seconds: int
    changelog_path: str
    unreleased_heading: str
    tag_prefix: str
    signed_tag: bool
    remote: str
    provider_kind: str | None
    provider_repository: str | None
    documentation_authority_paths: tuple[str, ...]
    default_branch: str | None
    contributions: ReleaseContributionsPolicy | None
    collateral: tuple[ReleaseCollateralPolicy, ...]
    artifact_gate: dict[str, Any] | None = None
    authenticated_receipt: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, value: object) -> ReleasePolicy:
        data = _mapping(value, "release_policy")
        # Optional: absent in existing/downstream policies, which stay valid
        # unchanged. Present here so litai release prepare can atomically
        # refresh a stale documentation-authority marker in the same
        # declared-scope commit as the version bump (see #55).
        documentation_authority_raw = data.pop("documentation_authority", None)
        # Optional: absent in existing/downstream policies, which stay valid
        # unchanged (feature is opt-in). When present, publish_release()
        # automatically advances this branch's version past whichever
        # release it just published, whenever that release landed on a
        # different branch -- see advance_default_branch_version()'s
        # docstring for why this matters.
        default_branch_raw = data.pop("default_branch", None)
        contributions_raw = data.pop("contributions", None)
        collateral_raw = data.pop("collateral", None)
        artifact_gate = data.pop("artifact_gate", None)
        authenticated_receipt = data.pop("authenticated_receipt", None)
        if authenticated_receipt is not None:
            from literate_ai.adapters.release_evidence import (
                validate_release_evidence_policy,
            )

            try:
                authenticated_receipt = validate_release_evidence_policy(
                    authenticated_receipt
                )
            except (ValueError, TypeError) as exc:
                _fail("release_policy.authenticated_receipt", str(exc))
        if artifact_gate is not None:
            artifact_gate = _mapping(artifact_gate, "release_policy.artifact_gate")
            _keys(
                artifact_gate,
                "release_policy.artifact_gate",
                {
                    "argv",
                    "timeout_seconds",
                    "manifest",
                    "required_roles",
                },
            )
            _strings(artifact_gate["argv"], "artifact_gate.argv", minimum=1)
            roles = _strings(
                artifact_gate["required_roles"],
                "artifact_gate.required_roles",
                minimum=1,
            )
            if len(set(roles)) != len(roles):
                _fail("artifact_gate.required_roles", "roles must be unique")
            _relative_path(artifact_gate["manifest"], "artifact_gate.manifest")
            timeout = artifact_gate["timeout_seconds"]
            if type(timeout) is not int or not 1 <= timeout <= 604800:
                _fail(
                    "artifact_gate.timeout_seconds",
                    "must be an integer from 1 to 604800",
                )
        _keys(
            data,
            "release_policy",
            {
                "schema",
                "version_scheme",
                "version_authority",
                "version_mirrors",
                "allowed_transitions",
                "gate",
                "changelog",
                "tag",
                "remote",
                "provider",
            },
        )
        schema = data["schema"]
        if schema not in {RELEASE_POLICY_SCHEMA, LEGACY_RELEASE_POLICY_SCHEMA}:
            _fail(
                "release_policy.schema",
                f"must be {RELEASE_POLICY_SCHEMA} or {LEGACY_RELEASE_POLICY_SCHEMA}",
            )
        version_scheme = _string(
            data["version_scheme"], "release_policy.version_scheme"
        )
        if version_scheme not in {"semver", "pep440"}:
            _fail("release_policy.version_scheme", "must be semver or pep440")
        authority = VersionBinding.from_dict(
            data["version_authority"], path="release_policy.version_authority"
        )
        mirrors_raw = data["version_mirrors"]
        if not isinstance(mirrors_raw, list):
            _fail("release_policy.version_mirrors", "must be an array")
        mirrors = tuple(
            VersionBinding.from_dict(
                item, path=f"release_policy.version_mirrors[{index}]"
            )
            for index, item in enumerate(mirrors_raw)
        )
        bindings = (authority, *mirrors)
        paths = tuple(item.path for item in bindings)
        if len(set(paths)) != len(paths):
            _fail("release_policy.version_mirrors", "binding paths must be unique")
        transitions = _strings(
            data["allowed_transitions"],
            "release_policy.allowed_transitions",
            minimum=1,
        )
        allowed = {"patch", "minor", "major", "prerelease", "explicit"}
        if len(set(transitions)) != len(transitions) or not set(transitions) <= allowed:
            _fail(
                "release_policy.allowed_transitions",
                "must contain unique supported transitions",
            )
        gate = _mapping(data["gate"], "release_policy.gate")
        _keys(gate, "release_policy.gate", {"argv", "timeout_seconds"})
        gate_argv = _strings(gate["argv"], "release_policy.gate.argv", minimum=1)
        timeout = gate["timeout_seconds"]
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 604800
        ):
            _fail(
                "release_policy.gate.timeout_seconds",
                "must be an integer from 1 to 604800",
            )
        changelog = _mapping(data["changelog"], "release_policy.changelog")
        _keys(changelog, "release_policy.changelog", {"path", "unreleased_heading"})
        changelog_path = _relative_path(
            changelog["path"], "release_policy.changelog.path"
        )
        if changelog_path in paths:
            _fail(
                "release_policy.changelog.path",
                "must be distinct from every version binding path",
            )
        heading = _string(
            changelog["unreleased_heading"],
            "release_policy.changelog.unreleased_heading",
        )
        if not heading.startswith("## "):
            _fail(
                "release_policy.changelog.unreleased_heading",
                "must be a level-two Markdown heading",
            )
        tag = _mapping(data["tag"], "release_policy.tag")
        _keys(tag, "release_policy.tag", {"prefix", "signed"})
        prefix = _string(tag["prefix"], "release_policy.tag.prefix", allow_empty=True)
        if not re.fullmatch(r"[A-Za-z0-9._-]*", prefix):
            _fail("release_policy.tag.prefix", "contains unsupported characters")
        signed = tag["signed"]
        if not isinstance(signed, bool):
            _fail("release_policy.tag.signed", "must be boolean")
        remote = _string(data["remote"], "release_policy.remote")
        if not _REMOTE.fullmatch(remote):
            _fail("release_policy.remote", "must be a portable Git remote name")
        provider = data["provider"]
        provider_kind: str | None = None
        provider_repository: str | None = None
        if provider is not None:
            provider_data = _mapping(provider, "release_policy.provider")
            _keys(provider_data, "release_policy.provider", {"kind", "repository"})
            provider_kind = _string(
                provider_data["kind"], "release_policy.provider.kind"
            )
            if provider_kind != "github":
                _fail(
                    "release_policy.provider.kind", "only github is currently supported"
                )
            provider_repository = _string(
                provider_data["repository"], "release_policy.provider.repository"
            )
            if not _REPOSITORY.fullmatch(provider_repository):
                _fail(
                    "release_policy.provider.repository",
                    "must be an owner/repository name",
                )
        documentation_authority_paths: tuple[str, ...] = ()
        if documentation_authority_raw is not None:
            documentation_authority_paths = tuple(
                _relative_path(item, f"release_policy.documentation_authority[{index}]")
                for index, item in enumerate(
                    _strings(
                        documentation_authority_raw,
                        "release_policy.documentation_authority",
                    )
                )
            )
            if len(set(documentation_authority_paths)) != len(
                documentation_authority_paths
            ):
                _fail(
                    "release_policy.documentation_authority",
                    "paths must be unique",
                )
        default_branch: str | None = None
        if default_branch_raw is not None:
            default_branch = _string(
                default_branch_raw, "release_policy.default_branch"
            )
            if not _BRANCH_NAME.fullmatch(default_branch):
                _fail(
                    "release_policy.default_branch",
                    "branch name is not a safe identifier",
                )
        contributions = (
            None
            if contributions_raw is None
            else ReleaseContributionsPolicy.from_dict(
                contributions_raw, path="release_policy.contributions"
            )
        )
        collateral: tuple[ReleaseCollateralPolicy, ...] = ()
        if collateral_raw is not None:
            if not isinstance(collateral_raw, list):
                _fail("release_policy.collateral", "must be an array")
            collateral = tuple(
                ReleaseCollateralPolicy.from_dict(
                    item, path=f"release_policy.collateral[{index}]"
                )
                for index, item in enumerate(collateral_raw)
            )
            names = tuple(item.name for item in collateral)
            if len(names) != len(set(names)):
                _fail("release_policy.collateral", "deliverable names must be unique")
        return cls(
            schema,
            version_scheme,
            authority,
            mirrors,
            transitions,
            gate_argv,
            timeout,
            changelog_path,
            heading,
            prefix,
            signed,
            remote,
            provider_kind,
            provider_repository,
            documentation_authority_paths,
            default_branch,
            contributions,
            collateral,
            artifact_gate,
            authenticated_receipt,
        )

    @property
    def identity(self) -> str:
        return canonical_identity(self.to_dict()).uri

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version_scheme": self.version_scheme,
            "version_authority": self.version_authority.to_dict(),
            "version_mirrors": [item.to_dict() for item in self.version_mirrors],
            "allowed_transitions": list(self.allowed_transitions),
            "gate": {
                "argv": list(self.gate_argv),
                "timeout_seconds": self.gate_timeout_seconds,
            },
            "changelog": {
                "path": self.changelog_path,
                "unreleased_heading": self.unreleased_heading,
            },
            "tag": {"prefix": self.tag_prefix, "signed": self.signed_tag},
            "remote": self.remote,
            "provider": (
                None
                if self.provider_kind is None
                else {
                    "kind": self.provider_kind,
                    "repository": self.provider_repository,
                }
            ),
            "documentation_authority": list(self.documentation_authority_paths),
            **(
                {"artifact_gate": self.artifact_gate}
                if self.artifact_gate is not None
                else {}
            ),
            **(
                {"authenticated_receipt": self.authenticated_receipt}
                if self.authenticated_receipt is not None
                else {}
            ),
            **(
                {"default_branch": self.default_branch}
                if self.default_branch is not None
                else {}
            ),
            **(
                {"contributions": self.contributions.to_dict()}
                if self.contributions is not None
                else {}
            ),
            **(
                {"collateral": [item.to_dict() for item in self.collateral]}
                if self.collateral
                else {}
            ),
        }


def _fail(path: str, message: str) -> None:
    raise ProjectReleaseError("release.policy_invalid", f"{path}: {message}")


def _mapping(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _fail(path, "must be an object")
    return dict(value)


def _keys(data: dict[str, Any], path: str, expected: set[str]) -> None:
    missing = expected - data.keys()
    extra = data.keys() - expected
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unknown " + ", ".join(sorted(extra)))
        _fail(path, "; ".join(details))


def _string(value: object, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        _fail(path, "must be a string" if allow_empty else "must be a non-empty string")
    if len(value.encode("utf-8")) > 4096 or "\x00" in value:
        _fail(path, "is too large or contains NUL")
    return value


def _strings(value: object, path: str, *, minimum: int = 0) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) < minimum or len(value) > 128:
        _fail(path, f"must be an array with {minimum}..128 entries")
    result = tuple(
        _string(item, f"{path}[{index}]") for index, item in enumerate(value)
    )
    return result


def _relative_path(value: object, path: str) -> str:
    raw = _string(value, path)
    if not _RELATIVE_PATH.fullmatch(raw) or "//" in raw:
        _fail(path, "must be a safe project-relative portable path")
    return raw


def _project_root(selected: Path) -> Path:
    try:
        project = discover_project(selected.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ProjectReleaseError(
            "release.project_unavailable", "cannot resolve the selected project"
        ) from exc
    if project is None:
        raise ProjectReleaseError(
            "release.project_not_found", "no literate.project.json found"
        )
    return project.root


def _read_json(path: Path, *, code: str) -> object:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectReleaseError(code, f"cannot read canonical JSON: {path}") from exc


def load_release_policy(project: Path) -> tuple[Path, ReleasePolicy]:
    root = _project_root(project)
    policy_path = root / RELEASE_POLICY_FILE
    policy = ReleasePolicy.from_dict(
        _read_json(policy_path, code="release.policy_unavailable")
    )
    repository_policy = _repository_policy(root)
    repository_branch = getattr(repository_policy, "default_branch", None)
    if isinstance(repository_branch, str):
        if (
            policy.default_branch is not None
            and policy.default_branch != repository_branch
        ):
            raise ProjectReleaseError(
                "release.policy_conflict",
                "release policy default_branch differs from repository policy",
            )
        policy = replace(policy, default_branch=repository_branch)
    return root, policy


def _release_engineers(root: Path) -> tuple[str, ...]:
    repository_policy = _repository_policy(root)
    source = getattr(
        repository_policy, "release_engineer_source", "README.md#release-engineers"
    )
    path_text, separator, fragment = source.partition("#")
    if not separator or not fragment:
        raise ProjectReleaseError(
            "release.engineers_unavailable", "release engineer source is invalid"
        )
    document = root.joinpath(*Path(path_text).parts)
    try:
        text = document.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ProjectReleaseError(
            "release.engineers_unavailable", "release engineer source is unavailable"
        ) from exc
    lines = text.splitlines()
    headings = [
        (index, len(match.group(1)), match.group(2))
        for index, line in enumerate(lines)
        if (match := re.fullmatch(r"(#{1,6})\s+(.+?)\s*", line))
    ]
    selected = next(
        (
            item
            for item in headings
            if re.sub(r"[^a-z0-9 -]", "", item[2].casefold()).replace(" ", "-")
            == fragment.casefold()
        ),
        None,
    )
    if selected is None:
        raise ProjectReleaseError(
            "release.engineers_unavailable",
            f"release engineer section #{fragment} is unavailable",
        )
    start, level, _heading = selected
    engineers: list[str] = []
    for line in lines[start + 1 :]:
        heading = re.match(r"^(#{1,6})\s", line)
        if heading and len(heading.group(1)) <= level:
            break
        match = _RELEASE_ENGINEER.fullmatch(line)
        if match:
            engineers.append(match.group(1))
    if not engineers:
        raise ProjectReleaseError(
            "release.engineers_unavailable",
            "release engineer section must contain exact `- `github-login`` bullets",
        )
    return tuple(engineers)


def _resolve_actor(root: Path, policy: ReleasePolicy, actor: str | None) -> str:
    if policy.provider_kind != "github":
        if actor is not None and _RELEASE_ENGINEER.fullmatch(f"- `{actor}`"):
            return actor
        raise ProjectReleaseError(
            "release.actor_unavailable",
            "non-GitHub release operations require an explicit actor",
        )
    try:
        completed = subprocess.run(
            ("gh", "api", "user", "--jq", ".login"),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectReleaseError(
            "release.actor_unavailable", "cannot resolve the authenticated GitHub actor"
        ) from exc
    login = completed.stdout.strip()
    if completed.returncode or not _RELEASE_ENGINEER.fullmatch(f"- `{login}`"):
        raise ProjectReleaseError(
            "release.actor_unavailable", "cannot resolve the authenticated GitHub actor"
        )
    if actor is not None and actor != login:
        raise ProjectReleaseError(
            "release.actor_mismatch",
            f"asserted actor {actor!r} does not match authenticated GitHub actor",
        )
    return login


def _repository_policy(root: Path) -> object | None:
    try:
        project = discover_project(root)
    except (OSError, ValueError):
        return None
    definition = getattr(project, "definition", None)
    return getattr(definition, "repository_policy", None)


def _repository_state(root: Path) -> tuple[object, str, str | None, str]:
    policy = _repository_policy(root)
    if policy is None:
        raise ProjectReleaseError(
            "release.repository_policy_unavailable",
            "project repository policy is unavailable",
        )
    state = getattr(getattr(policy, "main_state", None), "value", None)
    target = getattr(policy, "pre_release_version", None)
    branch = getattr(policy, "default_branch", None)
    if state not in {"free", "pre-release"} or not isinstance(branch, str):
        raise ProjectReleaseError(
            "release.repository_policy_unavailable",
            "project repository policy release state is unavailable",
        )
    return policy, state, target, branch


def _authorization_mode(root: Path) -> str:
    repository_policy = _repository_policy(root)
    mode = getattr(repository_policy, "patch_authority", None)
    if mode is None:
        mode = getattr(repository_policy, "release_mode", None)
    if mode is None:
        mode = getattr(repository_policy, "mode", None)
    value = getattr(mode, "value", mode)
    return value if value in {"strict", "loose"} else "strict"


def _repository_writers(root: Path) -> tuple[str, ...]:
    repository_policy = _repository_policy(root)
    writers = getattr(repository_policy, "writers", None)
    if writers is None:
        writers = getattr(repository_policy, "write_identities", ())
    if not isinstance(writers, (tuple, list, frozenset)):
        return ()
    return tuple(item for item in writers if isinstance(item, str))


def _authorize(
    root: Path,
    policy: ReleasePolicy,
    *,
    actor: str | None,
    operation: str,
    allow_break_glass: bool = False,
    break_glass_reason: str | None = None,
) -> dict[str, str | None]:
    resolved = _resolve_actor(root, policy, actor)
    engineers = _release_engineers(root)
    mode = _authorization_mode(root)
    if resolved in engineers:
        return {"actor": resolved, "mode": mode, "reason": None}
    if (
        allow_break_glass
        and mode == "loose"
        and resolved in _repository_writers(root)
        and isinstance(break_glass_reason, str)
        and break_glass_reason.strip()
    ):
        return {
            "actor": resolved,
            "mode": "break-glass",
            "reason": break_glass_reason.strip(),
        }
    raise ProjectReleaseError(
        "release.actor_unauthorized",
        f"{operation} requires a release engineer"
        + (
            " or an authorized loose-mode break-glass writer with a reason"
            if allow_break_glass
            else ""
        ),
    )


def _version_major_minor(version: str, scheme: str) -> str:
    canonical = _canonical_version(version, scheme)
    if scheme == "semver":
        parsed = SemanticVersion.parse(canonical)
        return f"{parsed.major}.{parsed.minor}"
    release = (*Version(canonical).release, 0, 0)
    return f"{release[0]}.{release[1]}"


def _require_pre_release_target(
    root: Path, policy: ReleasePolicy, version: str
) -> None:
    _repository, state, configured_target, _branch = _repository_state(root)
    target = _version_major_minor(version, policy.version_scheme)
    if state != "pre-release" or configured_target != target:
        raise ProjectReleaseError(
            "release.state_mismatch",
            f"release {target} requires main_state pre-release targeting {target}",
        )


def release_state(project: Path) -> dict[str, object]:
    root, policy = load_release_policy(project)
    _repository, state, target, default_branch = _repository_state(root)
    snapshot = _git_snapshot(root)
    return {
        "schema": "literate-ai/release-state@1",
        "main_state": state,
        "pre_release_version": target,
        "default_branch": default_branch,
        "branch": snapshot["branch"],
        "writable": True,
        "lockdown": {
            "active": state == "pre-release",
            "release_line": (f"release/{target}.x" if target is not None else None),
            "authorization_mode": _authorization_mode(root),
        },
    }


def set_release_state(
    project: Path,
    *,
    mode: str,
    pre_release_version: str | None,
    actor: str | None,
) -> dict[str, object]:
    root, policy = load_release_policy(project)
    authorization = _authorize(
        root, policy, actor=actor, operation="release state mutation"
    )
    if mode not in {"free", "pre-release"}:
        raise ProjectReleaseError(
            "release.state_invalid", "mode must be free or pre-release"
        )
    if mode == "free":
        if pre_release_version is not None:
            raise ProjectReleaseError(
                "release.state_invalid", "free state requires a null target"
            )
    elif pre_release_version is None or not _MAJOR_MINOR.fullmatch(pre_release_version):
        raise ProjectReleaseError(
            "release.state_invalid", "pre-release state requires major.minor target"
        )
    store = ProjectConfigurationStore(root)
    try:
        snapshot = store.read()
        repository_policy = snapshot.definition.repository_policy
        state_type = type(repository_policy.main_state)
        updated_policy = replace(
            repository_policy,
            main_state=state_type(mode),
            pre_release_version=pre_release_version,
        )
        store.update(
            snapshot,
            replace(snapshot.definition, repository_policy=updated_policy),
        )
    except (ProjectError, TypeError, ValueError) as exc:
        raise ProjectReleaseError(
            "release.state_update_failed", "repository policy state update failed"
        ) from exc
    return {**release_state(root), "authorization": authorization}


def _binding_path(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*Path(relative).parts)
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ProjectReleaseError(
            "release.binding_unavailable", f"binding parent is unavailable: {relative}"
        ) from exc
    if (
        resolved_parent != resolved_root
        and resolved_root not in resolved_parent.parents
    ):
        raise ProjectReleaseError(
            "release.binding_unsafe", f"binding escapes project: {relative}"
        )
    if candidate.is_symlink() or not candidate.is_file():
        raise ProjectReleaseError(
            "release.binding_unavailable", f"binding is not a regular file: {relative}"
        )
    return candidate


def _pointer_parts(pointer: str) -> tuple[str, ...]:
    parts = pointer[1:].split("/")
    result = []
    for part in parts:
        result.append(part.replace("~1", "/").replace("~0", "~"))
    return tuple(result)


def _json_pointer_value(value: object, pointer: str, *, source: str) -> object:
    selected = value
    for part in _pointer_parts(pointer):
        if not isinstance(selected, dict) or part not in selected:
            raise ProjectReleaseError(
                "release.binding_invalid", f"missing JSON pointer {pointer} in {source}"
            )
        selected = selected[part]
    return selected


def _canonical_version(value: str, scheme: str) -> str:
    try:
        parsed = SemanticVersion.parse(value) if scheme == "semver" else Version(value)
    except (InvalidVersion, ValueError) as exc:
        raise ProjectReleaseError(
            "release.binding_invalid",
            f"invalid {scheme} version {value!r}",
        ) from exc
    canonical = str(parsed)
    if canonical != value:
        raise ProjectReleaseError(
            "release.binding_invalid",
            f"noncanonical {scheme} version {value!r}; use {canonical!r}",
        )
    return canonical


def _cargo_lock_versions(
    root: Path, binding: VersionBinding
) -> tuple[str, tuple[tuple[int, int, str], ...]]:
    """Locate unique source-free package versions without rewriting other TOML."""
    path = _binding_path(root, binding.path)
    try:
        text = path.read_bytes().decode("utf-8")
        parsed = tomllib.loads(text)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ProjectReleaseError(
            "release.binding_invalid", f"cannot parse Cargo lockfile {binding.path}"
        ) from exc
    packages = parsed.get("package")
    headers = list(
        re.finditer(r"(?m)^[ \t]*\[\[package\]\][ \t]*(?:#[^\r\n]*)?\r?$", text)
    )
    if (
        not isinstance(packages, list)
        or len(packages) != len(headers)
        or any(not isinstance(package, dict) for package in packages)
    ):
        raise ProjectReleaseError(
            "release.binding_invalid", "invalid Cargo package inventory"
        )
    if any(
        not isinstance(package.get("dependencies", []), list)
        or any(not isinstance(item, str) for item in package.get("dependencies", []))
        for package in packages
    ):
        raise ProjectReleaseError(
            "release.binding_invalid", "Cargo dependencies must be lists of strings"
        )
    spans = []
    for name in binding.selectors:
        matches = [
            (i, package)
            for i, package in enumerate(packages)
            if package.get("name") == name
        ]
        if len(matches) != 1 or matches[0][1].get("source") is not None:
            raise ProjectReleaseError(
                "release.binding_invalid",
                f"{binding.path} must contain one source-free package {name}",
            )
        if any(
            isinstance(dependency, str)
            and dependency.split(maxsplit=1)[:1] == [name]
            and len(dependency.split()) > 1
            for package in packages
            for dependency in package.get("dependencies", [])
        ):
            raise ProjectReleaseError(
                "release.binding_invalid",
                f"{binding.path} must use unqualified dependency references for {name}",
            )
        index, package = matches[0]
        start = headers[index].end()
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[start:end]
        versions = list(
            re.finditer(
                r"(?m)^[ \t]*version[ \t]*=[ \t]*([\"'])([^\"'\r\n]+)\1"
                r"[ \t]*(?:#[^\r\n]*)?\r?$",
                block,
            )
        )
        if len(versions) != 1 or versions[0].group(2) != package.get("version"):
            raise ProjectReleaseError(
                "release.binding_invalid",
                f"{binding.path} has an ambiguous version for {name}",
            )
        match = versions[0]
        _canonical_version(match.group(2), "semver")
        spans.append((start + match.start(2), start + match.end(2), match.group(2)))
    return text, tuple(spans)


def _binding_values(
    root: Path, binding: VersionBinding, *, version_scheme: str
) -> tuple[str, ...]:
    path = _binding_path(root, binding.path)
    if binding.format == "cargo-lock-package":
        _text, spans = _cargo_lock_versions(root, binding)
        raw = tuple(value for _start, _end, value in spans)
    elif binding.format == "json-pointer":
        value = _read_json(path, code="release.binding_invalid")
        raw = tuple(
            _json_pointer_value(value, pointer, source=binding.path)
            for pointer in binding.selectors
        )
    else:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ProjectReleaseError(
                "release.binding_invalid", f"cannot read {binding.path}"
            ) from exc
        raw_values = []
        for name in binding.selectors:
            indentation = r"[ \t]*" if binding.format == "quoted-constant" else ""
            pattern = re.compile(
                rf'(?m)^{indentation}{re.escape(name)} = ["\']([^"\']+)["\']$'
            )
            matches = pattern.findall(text)
            if len(matches) != 1:
                raise ProjectReleaseError(
                    "release.binding_invalid",
                    f"{binding.path} must define {name} exactly once",
                )
            raw_values.append(matches[0])
        raw = tuple(raw_values)
    result = []
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise ProjectReleaseError(
                "release.binding_invalid",
                f"{binding.path} selector {binding.selectors[index]} is not a string",
            )
        try:
            result.append(_canonical_version(item, version_scheme))
        except ProjectReleaseError as exc:
            raise ProjectReleaseError(
                exc.code, f"{binding.path} contains {exc.message}"
            ) from exc
    return tuple(result)


def current_release_version(root: Path, policy: ReleasePolicy) -> str:
    values = tuple(
        value
        for binding in (policy.version_authority, *policy.version_mirrors)
        for value in _binding_values(
            root, binding, version_scheme=policy.version_scheme
        )
    )
    if not values or len(set(values)) != 1:
        raise ProjectReleaseError(
            "release.version_mismatch",
            "declared release version authorities do not agree",
        )
    return values[0]


def _git(
    root: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        return run_with_tree_kill(
            ("git", "-C", str(root), *arguments),
            check=check,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectReleaseError(
            "release.git_failed", f"Git operation failed: {' '.join(arguments)}"
        ) from exc


def _git_snapshot(root: Path) -> dict[str, object]:
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    branch = _git(root, "symbolic-ref", "--short", "HEAD").stdout.strip()
    status = tuple(
        line
        for line in _git(
            root, "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout.splitlines()
        if line
    )
    return {
        "head": head,
        "branch": branch,
        "clean": not status,
        "changes": list(status),
    }


def release_line_for_version(version: str, *, scheme: str) -> str:
    """Return the ``release/<major>.<minor>.x`` line that must own ``version``."""

    canonical = _canonical_version(version, scheme)
    if scheme == "semver":
        parsed = SemanticVersion.parse(canonical)
        return f"release/{parsed.major}.{parsed.minor}.x"
    parsed = Version(canonical)
    parts = (*parsed.release, 0, 0)
    return f"release/{parts[0]}.{parts[1]}.x"


def _plan_cut_branch(plan: dict[str, Any]) -> str:
    """Branch that must own check and publish for ``plan``."""

    release_line = plan.get("release_line")
    if isinstance(release_line, dict):
        name = release_line.get("name")
        if isinstance(name, str) and name:
            return name
    return str(plan["source_branch"])


def _release_line_exists(root: Path, policy: ReleasePolicy, name: str) -> bool:
    if _branch_exists(root, name):
        return True
    tracking = _git(
        root,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/remotes/{policy.remote}/{name}",
        check=False,
    )
    if tracking.returncode == 0:
        return True
    return _remote_branch_revision(root, policy.remote, name) is not None


def _resolve_release_line_plan(
    root: Path, policy: ReleasePolicy, *, branch: str, version: str
) -> dict[str, object] | None:
    """Bind the cut line for a read-only plan when policy names ``default_branch``.

    A missing line is recorded with ``create: true`` so ``prepare`` can cut it.
    An existing line cannot be recut from the default branch.
    """

    repository_policy = _repository_policy(root)
    default_branch = getattr(repository_policy, "default_branch", policy.default_branch)
    if default_branch is None:
        return None
    expected = release_line_for_version(version, scheme=policy.version_scheme)
    if branch == expected:
        return {"name": expected, "create": False}
    if branch == default_branch:
        if _release_line_exists(root, policy, expected):
            raise ProjectReleaseError(
                "release.default_branch_cut_forbidden",
                f"{expected!r} already exists; check it out and plan there "
                "(backport onto that line first)",
            )
        return {"name": expected, "create": True}
    raise ProjectReleaseError(
        "release.branch_not_release_line",
        f"checked-out branch {branch!r} is not the release line {expected!r} "
        f"for version {version}",
    )


def _require_stable_cut_contains_default_branch(
    root: Path,
    policy: ReleasePolicy,
    *,
    release_class: str,
    candidate_revision: str,
) -> None:
    """Keep a first stable line cut from omitting current release-intent trunk work.

    Patch lines deliberately select bounded fixes and therefore need not contain
    unrelated newer default-branch work. A schema-v2 initial, major, or minor cut is
    different: while repository policy is in its pre-release state, the release line
    must contain the live remote default branch at every release phase.
    """

    if (
        policy.schema != RELEASE_POLICY_SCHEMA
        or release_class not in {"initial", "major", "minor"}
        or policy.default_branch is None
    ):
        return
    default_revision = _remote_branch_revision(
        root, policy.remote, policy.default_branch
    )
    if default_revision is None:
        raise ProjectReleaseError(
            "release.default_branch_unavailable",
            f"remote default branch {policy.default_branch!r} is unavailable",
        )
    available = _git(
        root,
        "cat-file",
        "-e",
        f"{default_revision}^{{commit}}",
        check=False,
    )
    if available.returncode:
        raise ProjectReleaseError(
            "release.default_branch_unavailable",
            f"remote default branch {policy.default_branch!r} advanced to an "
            "unfetched revision; fetch it and re-plan",
        )
    ancestor = _git(
        root,
        "merge-base",
        "--is-ancestor",
        default_revision,
        candidate_revision,
        check=False,
    )
    if ancestor.returncode:
        raise ProjectReleaseError(
            "release.release_line_behind_default",
            f"first stable {release_class} candidate does not contain current "
            f"{policy.default_branch!r}; backport or merge the approved release "
            "work and re-plan",
        )


def _require_release_line(policy: ReleasePolicy, *, branch: str, version: str) -> None:
    """Refuse prepare/check/publish on ``default_branch`` or any non-line name.

    Policies that omit ``default_branch`` keep checkout-agnostic planning so
    initialized projects without a trunk/release-line split still work.
    """

    if policy.default_branch is None:
        return
    expected = release_line_for_version(version, scheme=policy.version_scheme)
    if branch == policy.default_branch:
        raise ProjectReleaseError(
            "release.default_branch_cut_forbidden",
            f"cannot prepare, check, or publish on {policy.default_branch!r}; "
            f"the cut belongs on {expected!r}",
        )
    if branch != expected:
        raise ProjectReleaseError(
            "release.branch_not_release_line",
            f"checked-out branch {branch!r} is not the release line {expected!r} "
            f"for version {version}",
        )


def _ensure_release_line_checkout(
    root: Path,
    policy: ReleasePolicy,
    plan: dict[str, Any],
    *,
    branch: str,
) -> str | None:
    """Create and check out a planned new line. Return its name, or ``None``."""

    if policy.default_branch is None:
        return None
    expected = release_line_for_version(
        str(plan["next_version"]), scheme=policy.version_scheme
    )
    release_line = plan.get("release_line")
    if not isinstance(release_line, dict):
        _require_release_line(policy, branch=branch, version=str(plan["next_version"]))
        return None
    name = release_line.get("name")
    if name != expected:
        raise ProjectReleaseError(
            "release.plan_stale",
            "planned release line does not match the planned version",
        )
    if release_line.get("create") is True:
        if branch != policy.default_branch:
            raise ProjectReleaseError(
                "release.plan_stale",
                "creating a release line requires the default-branch checkout "
                "from the plan",
            )
        if _release_line_exists(root, policy, expected):
            raise ProjectReleaseError(
                "release.branch_exists",
                f"{expected!r} already exists; check it out and re-plan",
            )
        _git(root, "checkout", "-b", expected)
        return expected
    _require_release_line(policy, branch=branch, version=str(plan["next_version"]))
    return None


def _unwind_created_release_line(root: Path, *, default_branch: str, name: str) -> None:
    snapshot = _git_snapshot(root)
    if not snapshot["clean"] or snapshot["branch"] != name:
        return
    _git(root, "checkout", default_branch)
    _git(root, "branch", "-d", name)


def _is_prerelease(value: str, scheme: str) -> bool:
    if scheme == "semver":
        return bool(SemanticVersion.parse(value).prerelease)
    return Version(value).is_prerelease


def _next_version(
    current: str, transition: str, explicit: str | None, *, scheme: str
) -> str:
    """Compute the next release version.

    An explicit ``--version`` equal to the declared current version is allowed
    so an already-bumped, still-untagged line can be planned. A lower explicit
    version is not forward. Callers that plan a release still reject a version
    that already has a tag.
    """
    value = SemanticVersion.parse(current) if scheme == "semver" else Version(current)
    if transition in {"explicit", "prerelease"}:
        if explicit is None:
            raise ProjectReleaseError(
                "release.version_required",
                f"{transition} transition requires --version",
            )
        canonical = _canonical_version(explicit, scheme)
        candidate = (
            SemanticVersion.parse(canonical)
            if scheme == "semver"
            else Version(canonical)
        )
        if transition == "prerelease" and not _is_prerelease(canonical, scheme):
            raise ProjectReleaseError(
                "release.version_required",
                "prerelease transition requires a prerelease --version",
            )
    elif transition == "patch":
        if scheme == "semver":
            candidate = SemanticVersion(value.major, value.minor, value.patch + 1)
        else:
            release = (*value.release, 0, 0, 0)[:3]
            candidate = Version(f"{release[0]}.{release[1]}.{release[2] + 1}")
    elif transition == "minor":
        if scheme == "semver":
            candidate = SemanticVersion(value.major, value.minor + 1, 0)
        else:
            release = (*value.release, 0, 0)[:2]
            candidate = Version(f"{release[0]}.{release[1] + 1}.0")
    elif transition == "major":
        if scheme == "semver":
            candidate = SemanticVersion(value.major + 1, 0, 0)
        else:
            candidate = Version(f"{value.release[0] + 1}.0.0")
    else:
        raise ProjectReleaseError("release.version_required", "unsupported transition")
    if candidate < value:
        raise ProjectReleaseError(
            "release.version_not_forward", "release version must increase precedence"
        )
    return str(candidate)


def create_release_plan(
    project: Path,
    *,
    transition: str,
    explicit_version: str | None,
) -> dict[str, object]:
    root, policy = load_release_policy(project)
    current = current_release_version(root, policy)
    if (
        transition == "explicit"
        and explicit_version is not None
        and _is_prerelease(
            _canonical_version(explicit_version, policy.version_scheme),
            policy.version_scheme,
        )
    ):
        transition = "prerelease"
    if transition not in policy.allowed_transitions:
        raise ProjectReleaseError(
            "release.transition_forbidden",
            f"policy does not allow transition {transition!r}",
        )
    next_version = _next_version(
        current,
        transition,
        explicit_version,
        scheme=policy.version_scheme,
    )
    if policy.schema == RELEASE_POLICY_SCHEMA:
        _require_pre_release_target(root, policy, next_version)
    released = _released_tag_versions(root, policy)
    canonical_next = _canonical_version(next_version, policy.version_scheme)
    if canonical_next in released:
        raise ProjectReleaseError(
            "release.tag_exists",
            f"release tag already exists: {policy.tag_prefix}{canonical_next}",
        )
    snapshot = _git_snapshot(root)
    release_class, stable_predecessor = _derived_release_class(
        next_version, released, scheme=policy.version_scheme
    )
    _require_stable_cut_contains_default_branch(
        root,
        policy,
        release_class=release_class,
        candidate_revision=str(snapshot["head"]),
    )
    contributions = _release_contribution_summary(root, policy, version=next_version)
    collateral = _required_release_collateral(
        root,
        policy,
        version=next_version,
        release_class=release_class,
        revision=str(snapshot["head"]),
    )
    release_line = _resolve_release_line_plan(
        root,
        policy,
        branch=str(snapshot["branch"]),
        version=next_version,
    )
    bindings = (policy.version_authority, *policy.version_mirrors)
    result: dict[str, object] = {
        "schema": RELEASE_PLAN_SCHEMA,
        "policy_identity": policy.identity,
        "source_revision": snapshot["head"],
        "source_branch": snapshot["branch"],
        "source_clean": snapshot["clean"],
        "source_changes": snapshot["changes"],
        "transition": transition,
        "release_class": release_class,
        "stable_predecessor": stable_predecessor,
        "current_version": current,
        "next_version": next_version,
        "tag": f"{policy.tag_prefix}{next_version}",
        "version_bindings": [item.to_dict() for item in bindings],
        "gate": {
            "argv": list(policy.gate_argv),
            "timeout_seconds": policy.gate_timeout_seconds,
        },
        "changelog": {
            "path": policy.changelog_path,
            "unreleased_heading": policy.unreleased_heading,
        },
        "remote": policy.remote,
        "provider": (
            None
            if policy.provider_kind is None
            else {
                "kind": policy.provider_kind,
                "repository": policy.provider_repository,
            }
        ),
        "collateral": collateral,
    }
    if contributions is not None:
        result["contributions"] = contributions
    if release_line is not None:
        result["release_line"] = release_line
    result["identity"] = canonical_identity(result).uri
    return result


def _released_tag_versions(root: Path, policy: ReleasePolicy) -> tuple[str, ...]:
    """Every canonical version with a matching tag anywhere in the repository.

    Unlike ``current_release_version``, this deliberately does not require
    the tag to be an ancestor of any particular branch: a release cut on a
    release-line ``release/x.y.x`` branch, and never merged back,
    still counts as released.
    """

    completed = _git(root, "tag", "--list", f"{policy.tag_prefix}*")
    versions: list[str] = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line.startswith(policy.tag_prefix):
            continue
        candidate = line[len(policy.tag_prefix) :]
        try:
            versions.append(_canonical_version(candidate, policy.version_scheme))
        except ProjectReleaseError:
            continue
    return tuple(versions)


def _derived_release_class(
    next_version: str,
    released: tuple[str, ...],
    *,
    scheme: str,
) -> tuple[str, str | None]:
    """Classify against the highest stable tag, not the source version marker."""

    candidate = Version(_canonical_version(next_version, scheme))
    stable = sorted(
        (
            Version(_canonical_version(value, scheme))
            for value in released
            if not _is_prerelease(value, scheme)
            and Version(_canonical_version(value, scheme)) < candidate
        ),
    )
    if not stable:
        return "initial", None
    predecessor = stable[-1]
    previous_release = (*predecessor.release, 0, 0, 0)[:3]
    next_release = (*candidate.release, 0, 0, 0)[:3]
    if next_release[0] != previous_release[0]:
        release_class = "major"
    elif next_release[1] != previous_release[1]:
        release_class = "minor"
    else:
        release_class = "patch"
    return release_class, str(predecessor)


def _release_contribution_summary(
    root: Path,
    policy: ReleasePolicy,
    *,
    version: str,
) -> dict[str, object] | None:
    configured = policy.contributions
    if configured is None:
        return None
    from literate_ai.release_contributions import (
        ReleaseContributionsError,
        require_release_contributions_ready,
        sweep_release_contributions,
    )

    milestone = configured.current_milestone.replace("{version}", version)
    try:
        result = sweep_release_contributions(
            root,
            release=version,
            current_milestone=milestone,
            remote=policy.remote,
            default_branch=policy.default_branch or "main",
        )
        require_release_contributions_ready(result)
    except ReleaseContributionsError as exc:
        raise ProjectReleaseError(exc.code, exc.message) from exc
    return {
        "schema": result["schema"],
        "identity": result["identity"],
        "release": version,
        "current_milestone": milestone,
        "issue_count": len(result["issues"]),
        "review_count": len(result["reviews"]),
        "branch_count": len(result["branches"]),
        "worktree_count": len(result["worktrees"]),
        "ready": True,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _required_release_collateral(
    root: Path,
    policy: ReleasePolicy,
    *,
    version: str,
    release_class: str,
    revision: str,
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for deliverable in policy.collateral:
        if release_class not in deliverable.required_for:
            continue
        report_path = _binding_path(root, deliverable.verification_report_path)
        report = _read_json(
            report_path, code="release.collateral_verification_unavailable"
        )
        if not isinstance(report, dict) or report.get("accepted") is not True:
            raise ProjectReleaseError(
                "release.collateral_verification_failed",
                f"collateral {deliverable.name!r} lacks an accepted local report",
            )
        receipt_path = _binding_path(root, deliverable.publication_receipt_path)
        receipt = _load_record(receipt_path, DOCUMENT_PAIR_PUBLICATION_SCHEMA)
        if (
            receipt.get("release_version") != version
            or receipt.get("complete") is not True
        ):
            raise ProjectReleaseError(
                "release.collateral_publication_stale",
                f"collateral {deliverable.name!r} is not published for {version}",
            )
        source_revision = receipt.get("source_revision")
        if not isinstance(source_revision, str):
            raise ProjectReleaseError(
                "release.collateral_publication_invalid",
                "collateral receipt has no source revision",
            )
        ancestor = _git(
            root,
            "merge-base",
            "--is-ancestor",
            source_revision,
            revision,
            check=False,
        )
        if ancestor.returncode:
            raise ProjectReleaseError(
                "release.collateral_publication_stale",
                "collateral publication does not descend into the release candidate",
            )
        source = receipt.get("source")
        members = receipt.get("members")
        exports = receipt.get("exports")
        if not all(isinstance(value, dict) for value in (source, members, exports)):
            raise ProjectReleaseError(
                "release.collateral_publication_invalid",
                "collateral receipt omits source, member, or export evidence",
            )
        artifact_paths = {
            "presentation": deliverable.presentation_path,
            "narrative": deliverable.narrative_path,
        }
        resource_ids = {
            "presentation": deliverable.presentation_resource_id,
            "narrative": deliverable.narrative_resource_id,
        }
        artifact_summary: dict[str, object] = {}
        for member in ("presentation", "narrative"):
            artifact_path = _binding_path(root, artifact_paths[member])
            source_record = source.get(member)
            member_record = members.get(member)
            export_record = exports.get(member)
            digest = _sha256_file(artifact_path)
            if (
                not isinstance(source_record, dict)
                or source_record.get("path") != artifact_paths[member]
                or source_record.get("sha256") != digest
                or not isinstance(member_record, dict)
                or member_record.get("id") != resource_ids[member]
                or member_record.get("updated") is not True
                or not isinstance(export_record, dict)
                or not isinstance(export_record.get("sha256"), str)
            ):
                raise ProjectReleaseError(
                    "release.collateral_publication_mismatch",
                    f"collateral {deliverable.name!r} {member} evidence "
                    "mismatches policy",
                )
            artifact_summary[member] = {
                "path": artifact_paths[member],
                "bytes": artifact_path.stat().st_size,
                "sha256": digest,
                "resource_id": resource_ids[member],
                "export_sha256": export_record["sha256"],
            }
        account = receipt.get("account")
        if not isinstance(account, str) or not account:
            raise ProjectReleaseError(
                "release.collateral_publication_invalid",
                "collateral publication receipt omits the active account",
            )
        summaries.append(
            {
                "kind": "document-pair",
                "name": deliverable.name,
                "ecosystem": deliverable.ecosystem,
                "release_class": release_class,
                "account": account,
                "source_revision": source_revision,
                "verification_identity": canonical_identity(report).uri,
                "publication_identity": receipt["identity"],
                "artifacts": artifact_summary,
            }
        )
    return summaries


def _revalidate_release_closure(
    root: Path,
    policy: ReleasePolicy,
    record: dict[str, Any],
    *,
    revision: str,
    require_default_branch_ancestry: bool,
) -> dict[str, object] | None:
    version = str(record.get("next_version") or record.get("version") or "")
    release_class = record.get("release_class")
    if not isinstance(release_class, str):
        if policy.contributions is not None or policy.collateral:
            raise ProjectReleaseError(
                "release.plan_stale",
                "release closure policy requires a release-classified plan",
            )
        return None
    if require_default_branch_ancestry:
        _require_stable_cut_contains_default_branch(
            root,
            policy,
            release_class=release_class,
            candidate_revision=revision,
        )
    contributions = _release_contribution_summary(root, policy, version=version)
    collateral = _required_release_collateral(
        root,
        policy,
        version=version,
        release_class=release_class,
        revision=revision,
    )
    if record.get("collateral", []) != collateral:
        raise ProjectReleaseError(
            "release.collateral_stale",
            "release collateral differs from the planned verified publication",
        )
    return contributions


def _max_version(values: tuple[str, ...], *, scheme: str) -> str:
    def key(value: str) -> SemanticVersion | Version:
        return SemanticVersion.parse(value) if scheme == "semver" else Version(value)

    return max(values, key=key)


def advance_default_branch_version(
    project: Path,
    *,
    branch: str,
    transition: str,
    explicit_version: str | None,
) -> dict[str, object]:
    """Advance the checked-out branch's version past every released tag.

    A release is frequently cut on a release-line ``release/x.y.x`` branch
    and never merged back into the project's default branch, so that
    branch's own version authority/mirrors can silently lag behind every
    release that has actually shipped -- this repository's own ``main`` sat
    at ``0.4.1`` through the ``0.5.0`` and ``0.5.1`` releases before this
    command existed. This finds the highest version among every tag
    matching the policy's tag prefix (not required to be an ancestor of
    ``branch``) and the branch's own current version, then advances the
    version authority/mirrors past whichever is greater using the same
    transition semantics as ``litai release plan``.

    The caller must already have ``branch`` checked out clean; like
    ``prepare_release``, this only writes the declared files -- it never
    commits or pushes, so the caller reviews and commits the diff.
    """

    root, policy = load_release_policy(project)
    snapshot = _git_snapshot(root)
    if not snapshot["clean"]:
        raise ProjectReleaseError(
            "release.prepare_dirty",
            "advancing the default branch version requires a clean working tree",
        )
    if snapshot["branch"] != branch:
        raise ProjectReleaseError(
            "release.branch_mismatch",
            f"checked-out branch {snapshot['branch']!r} does not match "
            f"--branch {branch!r}; check out {branch!r} first",
        )
    current = current_release_version(root, policy)
    released = _released_tag_versions(root, policy)
    base = _max_version((current, *released), scheme=policy.version_scheme)
    if (
        transition == "explicit"
        and explicit_version is not None
        and _is_prerelease(
            _canonical_version(explicit_version, policy.version_scheme),
            policy.version_scheme,
        )
    ):
        transition = "prerelease"
    if transition not in policy.allowed_transitions:
        raise ProjectReleaseError(
            "release.transition_forbidden",
            f"policy does not allow transition {transition!r}",
        )
    next_version = _next_version(
        base, transition, explicit_version, scheme=policy.version_scheme
    )
    rendered = tuple(
        _render_binding(
            root,
            binding,
            current,
            next_version,
            version_scheme=policy.version_scheme,
        )
        for binding in (policy.version_authority, *policy.version_mirrors)
    )
    _write_transaction(rendered)
    refreshed_documentation = (
        _refresh_documentation_authority(root, policy)
        if policy.documentation_authority_paths
        else ()
    )
    return {
        "schema": "literate-ai/release-branch-advance@1",
        "branch": branch,
        "previous_version": current,
        "highest_released_version": _max_version(released, scheme=policy.version_scheme)
        if released
        else None,
        "next_version": next_version,
        "changed_paths": sorted(
            {
                policy.version_authority.path,
                *(item.path for item in policy.version_mirrors),
                *(str(path.relative_to(root)) for path in refreshed_documentation),
            }
        ),
        "next_action": (
            "review and commit the declared version authority; "
            "this command never commits or pushes"
        ),
    }


_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _branch_exists(root: Path, branch: str) -> bool:
    completed = _git(
        root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False
    )
    return completed.returncode == 0


def _resolve_revision(root: Path, ref: str) -> str:
    completed = _git(root, "rev-parse", "--verify", ref, check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ProjectReleaseError(
            "release.backport_ref_unknown", f"unknown revision: {ref}"
        )
    return completed.stdout.strip()


def _git_in(
    directory: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ("git", "-C", str(directory), *arguments),
            check=check,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectReleaseError(
            "release.git_failed", f"Git operation failed: {' '.join(arguments)}"
        ) from exc


def backport_commits(
    project: Path,
    *,
    commits: tuple[str, ...],
    to_branch: str,
    create_from: str | None,
    actor: str | None = None,
    break_glass_reason: str | None = None,
) -> dict[str, object]:
    """Cherry-pick already-landed commits onto a release branch.

    Implements the fix-forward-then-cherry-pick model from
    docs/history/roadmap/release-branching-model.md: the commit must already exist
    (typically on `main`); this only replays it onto `to_branch`, creating
    that branch from `create_from` first if it does not exist yet.
    """
    root = _project_root(project)
    policy: ReleasePolicy | None = None
    if (root / RELEASE_POLICY_FILE).is_file():
        _policy_root, policy = load_release_policy(root)
    authorization: dict[str, str | None] | None = None
    if policy is not None and policy.schema == RELEASE_POLICY_SCHEMA:
        _repository_state(root)
        authorization = _authorize(
            root,
            policy,
            actor=actor,
            operation="release backport",
            allow_break_glass=True,
            break_glass_reason=break_glass_reason,
        )
    if not commits:
        raise ProjectReleaseError(
            "release.backport_commits_required", "at least one commit is required"
        )
    if not _BRANCH_NAME.fullmatch(to_branch):
        raise ProjectReleaseError(
            "release.backport_branch_invalid", "branch name is not a safe identifier"
        )
    if policy is not None and not _RELEASE_LINE.fullmatch(to_branch):
        raise ProjectReleaseError(
            "release.backport_branch_invalid",
            "backports must target release/<major>.<minor>.x",
        )

    resolved_commits = tuple(_resolve_revision(root, commit) for commit in commits)
    if policy is not None and policy.default_branch is not None:
        default_revision = _resolve_revision(root, policy.default_branch)
        for commit in resolved_commits:
            if _git(
                root,
                "merge-base",
                "--is-ancestor",
                commit,
                default_revision,
                check=False,
            ).returncode:
                raise ProjectReleaseError(
                    "release.backport_not_on_default_branch",
                    f"backport source {commit} is not on {policy.default_branch}",
                )

    created = False
    if not _branch_exists(root, to_branch):
        if authorization is not None and authorization["mode"] == "break-glass":
            raise ProjectReleaseError(
                "release.backport_branch_missing",
                "break-glass backports require an existing release line",
            )
        if create_from is None:
            raise ProjectReleaseError(
                "release.backport_branch_missing",
                f"branch {to_branch!r} does not exist; pass create_from to create it",
            )
        from_revision = _resolve_revision(root, create_from)
        _git(root, "branch", to_branch, from_revision)
        created = True

    picked: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="litai-backport-") as tmp:
        worktree = Path(tmp) / "worktree"
        _git(root, "worktree", "add", str(worktree), to_branch)
        try:
            for original in resolved_commits:
                completed = _git_in(
                    worktree, "cherry-pick", "-x", original, check=False
                )
                if completed.returncode != 0:
                    _git_in(worktree, "cherry-pick", "--abort", check=False)
                    raise ProjectReleaseError(
                        "release.backport_conflict",
                        f"cherry-pick of {original} onto {to_branch} conflicts; "
                        "resolve manually with git",
                    )
                picked_sha = _git_in(worktree, "rev-parse", "HEAD").stdout.strip()
                picked.append(
                    {"source_commit": original, "backport_commit": picked_sha}
                )
        finally:
            _git(root, "worktree", "remove", "--force", str(worktree), check=False)

    branch_head = (
        picked[-1]["backport_commit"] if picked else _resolve_revision(root, to_branch)
    )
    result: dict[str, object] = {
        "schema": RELEASE_BACKPORT_SCHEMA,
        "branch": to_branch,
        "branch_created": created,
        "created_from": create_from if created else None,
        "commits": picked,
        "branch_head": branch_head,
    }
    if authorization is not None:
        result["authorization"] = authorization
    return result


def backport_status(project: Path, *, branch: str, against: str) -> dict[str, object]:
    """Report which commits on `against` are missing from `branch`.

    Uses `git cherry`'s patch-id equivalence (not plain reachability) so a
    commit already replayed by `backport_commits` (which cherry-picks with
    `-x`, producing a different SHA with the same diff) correctly drops off
    the pending list.
    """
    root = _project_root(project)
    branch_revision = _resolve_revision(root, branch)
    against_revision = _resolve_revision(root, against)
    completed = _git(root, "cherry", branch, against)
    pending: list[dict[str, object]] = []
    for line in completed.stdout.splitlines():
        if not line or not line.startswith("+"):
            continue
        sha = line[2:].strip()
        subject = _git(root, "log", "-1", "--format=%s", sha).stdout.strip()
        touched = _git(
            root, "diff", "--name-only", f"{sha}^!", "--", "src", check=False
        ).stdout.strip()
        pending.append(
            {"commit": sha, "subject": subject, "touches_src": bool(touched)}
        )
    return {
        "schema": RELEASE_BACKPORT_STATUS_SCHEMA,
        "branch": branch,
        "branch_revision": branch_revision,
        "against": against,
        "against_revision": against_revision,
        "pending_commit_count": len(pending),
        "pending_commits": pending,
    }


def _load_record(path: Path, schema: str | tuple[str, ...]) -> dict[str, Any]:
    raw = _read_json(path.resolve(strict=True), code="release.record_unavailable")
    if isinstance(raw, dict) and raw.get("schema") == "literate-ai/cli-result@1":
        raw = raw.get("result")
    data = _mapping(raw, "release_record")
    accepted = (schema,) if isinstance(schema, str) else schema
    if data.get("schema") not in accepted:
        expected = accepted[0] if len(accepted) == 1 else " or ".join(accepted)
        raise ProjectReleaseError(
            "release.record_invalid", f"record must use schema {expected}"
        )
    identity = data.get("identity")
    without = {key: value for key, value in data.items() if key != "identity"}
    if identity != canonical_identity(without).uri:
        raise ProjectReleaseError(
            "release.record_invalid", "record identity does not match its content"
        )
    return data


def _replace_json_pointer(value: object, pointer: str, replacement: str) -> None:
    parts = _pointer_parts(pointer)
    selected = value
    for part in parts[:-1]:
        if not isinstance(selected, dict) or part not in selected:
            raise ProjectReleaseError(
                "release.binding_invalid", f"missing JSON pointer {pointer}"
            )
        selected = selected[part]
    if not isinstance(selected, dict) or parts[-1] not in selected:
        raise ProjectReleaseError(
            "release.binding_invalid", f"missing JSON pointer {pointer}"
        )
    selected[parts[-1]] = replacement


def _render_binding(
    root: Path,
    binding: VersionBinding,
    current: str,
    replacement: str,
    *,
    version_scheme: str,
) -> tuple[Path, str]:
    path = _binding_path(root, binding.path)
    values = _binding_values(root, binding, version_scheme=version_scheme)
    if any(value != current for value in values):
        raise ProjectReleaseError(
            "release.version_drift", f"{binding.path} changed after planning"
        )
    if binding.format == "cargo-lock-package":
        _canonical_version(replacement, "semver")
        text, spans = _cargo_lock_versions(root, binding)
        for start, end, _value in sorted(spans, reverse=True):
            text = text[:start] + replacement + text[end:]
        return path, text
    if binding.format == "json-pointer":
        value = _read_json(path, code="release.binding_invalid")
        for pointer in binding.selectors:
            _replace_json_pointer(value, pointer, replacement)
        return path, json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    text = path.read_text(encoding="utf-8")
    for name in binding.selectors:
        indentation = r"[ \t]*" if binding.format == "quoted-constant" else ""
        pattern = re.compile(
            rf"(?m)^({indentation}{re.escape(name)} = )"
            rf'(["\']){re.escape(current)}\2$'
        )
        text, count = pattern.subn(rf'\g<1>"{replacement}"', text)
        if count != 1:
            raise ProjectReleaseError(
                "release.version_drift", f"{binding.path} changed after planning"
            )
    return path, text


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.release-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        with os.fdopen(descriptor, "wb") as output:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _write_transaction(changes: tuple[tuple[Path, str], ...]) -> None:
    originals = {path: path.read_bytes() for path, _text in changes}
    written: list[Path] = []
    try:
        for path, text in changes:
            _atomic_write_text(path, text)
            written.append(path)
    except Exception as exc:
        rollback_failures: list[str] = []
        for path in reversed(written):
            try:
                _atomic_write_bytes(path, originals[path])
            except Exception:
                rollback_failures.append(path.name)
        detail = (
            "; rollback also failed for " + ", ".join(rollback_failures)
            if rollback_failures
            else ""
        )
        raise ProjectReleaseError(
            "release.prepare_write_failed",
            "release preparation could not atomically replace declared authority"
            + detail,
        ) from exc


def _refresh_documentation_authority(
    root: Path, policy: ReleasePolicy
) -> tuple[Path, ...]:
    """Refresh a stale documentation-authority marker after a version bump.

    Runs against on-disk content, so callers must invoke this only after the
    version/changelog bindings are already written: bumping the version
    authority is exactly what makes the marker stale, and the refreshed
    marker must bind the post-bump identity. Only refreshes a marker already
    declared in ``policy.documentation_authority_paths``; a missing or
    duplicate marker, or one recorded outside the declared scope, fails
    closed without modifying anything (see issue #55).
    """

    from literate_ai.adapters.project_validation import (
        FilesystemProjectValidationAdapter,
    )
    from literate_ai.application.project_authority import AUTHORITY_REVIEW_MARKER

    review = FilesystemProjectValidationAdapter().documentation_review(root)
    state = str(review["state"])
    if state == "current":
        return ()
    document = review.get("document")
    if (
        state != "stale"
        or not isinstance(document, str)
        or document not in policy.documentation_authority_paths
    ):
        raise ProjectReleaseError(
            "release.documentation_authority_unrecordable",
            "documentation authority marker is "
            + state
            + "; resolve it outside release preparation before retrying",
        )
    path = _binding_path(root, document)
    raw = path.read_bytes()
    expected = str(review["expected_marker"]).encode("ascii")
    updated, count = AUTHORITY_REVIEW_MARKER.subn(expected, raw)
    if count != 1:
        raise ProjectReleaseError(
            "release.documentation_authority_unrecordable",
            f"expected exactly one authority marker in {document}",
        )
    _atomic_write_bytes(path, updated)
    return (path,)


def _reviewed_documentation_authority_path(root: Path) -> str | None:
    """Return the sole validated marker document for legacy release policies.

    Policies created before ``documentation_authority`` existed cannot authorize
    prepare to rewrite the marker automatically. A release operator may still record
    the exact post-prepare marker explicitly; admit only the document that project
    validation identifies as containing that sole current or stale marker. The release
    gate remains responsible for requiring the marker to be current.
    """

    from literate_ai.adapters.project_validation import (
        FilesystemProjectValidationAdapter,
        ProjectValidationError,
    )

    try:
        review = FilesystemProjectValidationAdapter().documentation_review(root)
    except ProjectValidationError:
        return None
    state = review.get("state")
    document = review.get("document")
    if state not in {"current", "stale"} or not isinstance(document, str):
        return None
    return document


def _changelog_version_heading_count(text: str, version: object) -> int:
    heading = re.compile(rf"(?m)^## {re.escape(str(version))}(?:\s|$)")
    return len(heading.findall(text))


def _changelog_readme_markdown(policy: ReleasePolicy, version: str) -> str | None:
    """Tag-pinned README.md blob link for one published changelog section.

    Kept as the first line *under* the version heading so
    ``_changelog_version_heading_count`` and ``_changelog_release_notes`` can
    still match ``^## {version}(?:\\s|$)``. Relative README links are wrong
    for historical sections: they would follow whatever revision the reader
    currently has checked out.
    """
    if policy.provider_kind != "github" or not policy.provider_repository:
        return None
    tag = f"{policy.tag_prefix}{version}"
    url = f"https://github.com/{policy.provider_repository}/blob/{tag}/README.md"
    return f"[README.md]({url})"


def prepare_release(
    project: Path,
    plan_path: Path,
    *,
    actor: str | None = None,
    break_glass_reason: str | None = None,
) -> dict[str, object]:
    plan = _load_record(plan_path, RELEASE_PLAN_SCHEMA)
    root, policy = load_release_policy(project)
    authorization: dict[str, str | None] | None = None
    if policy.schema == RELEASE_POLICY_SCHEMA:
        _require_pre_release_target(root, policy, str(plan["next_version"]))
        authorization = _authorize(
            root,
            policy,
            actor=actor,
            operation="release preparation",
            allow_break_glass=plan["transition"] == "patch",
            break_glass_reason=break_glass_reason,
        )
        release_line = plan.get("release_line")
        if (
            authorization["mode"] == "break-glass"
            and isinstance(release_line, dict)
            and release_line.get("create") is True
        ):
            raise ProjectReleaseError(
                "release.break_glass_line_missing",
                "break-glass patch preparation requires an existing release line",
            )
    snapshot = _git_snapshot(root)
    if not snapshot["clean"]:
        raise ProjectReleaseError(
            "release.prepare_dirty", "prepare requires a clean working tree"
        )
    if (
        snapshot["head"] != plan["source_revision"]
        or snapshot["branch"] != plan["source_branch"]
    ):
        raise ProjectReleaseError(
            "release.plan_stale", "Git revision or branch changed after planning"
        )
    if policy.identity != plan["policy_identity"]:
        raise ProjectReleaseError(
            "release.plan_stale", "release policy changed after planning"
        )
    current = current_release_version(root, policy)
    if current != plan["current_version"]:
        raise ProjectReleaseError(
            "release.plan_stale", "release version changed after planning"
        )
    contributions = _revalidate_release_closure(
        root,
        policy,
        plan,
        revision=str(snapshot["head"]),
        require_default_branch_ancestry=True,
    )
    rendered = tuple(
        _render_binding(
            root,
            binding,
            current,
            str(plan["next_version"]),
            version_scheme=policy.version_scheme,
        )
        for binding in (policy.version_authority, *policy.version_mirrors)
    )
    changelog = _binding_path(root, policy.changelog_path)
    text = changelog.read_text(encoding="utf-8")
    if text.count(policy.unreleased_heading) != 1:
        raise ProjectReleaseError(
            "release.changelog_invalid",
            "changelog must contain the unreleased heading exactly once",
        )
    heading_count = _changelog_version_heading_count(text, plan["next_version"])
    if heading_count > 1 or (
        heading_count == 1 and plan["current_version"] != plan["next_version"]
    ):
        raise ProjectReleaseError(
            "release.changelog_invalid",
            f"changelog already contains a heading for version {plan['next_version']}",
        )
    if heading_count == 0:
        dated = f"## {plan['next_version']} - {datetime.now(UTC).date().isoformat()}"
        inserted = f"{policy.unreleased_heading}\n\n{dated}"
        readme = _changelog_readme_markdown(policy, str(plan["next_version"]))
        if readme is not None:
            inserted = f"{inserted}\n\n{readme}"
        text = text.replace(policy.unreleased_heading, inserted, 1)
    changes = (*rendered, (changelog, text))
    created_line: str | None = None
    try:
        created_line = _ensure_release_line_checkout(
            root, policy, plan, branch=str(snapshot["branch"])
        )
        _write_transaction(
            tuple(
                (path, updated)
                for path, updated in changes
                if path.read_text(encoding="utf-8") != updated
            )
        )
        refreshed_documentation = (
            _refresh_documentation_authority(root, policy)
            if policy.documentation_authority_paths
            else ()
        )
    except Exception:
        if created_line is not None and policy.default_branch is not None:
            _unwind_created_release_line(
                root, default_branch=policy.default_branch, name=created_line
            )
        raise
    cut = _plan_cut_branch(plan)
    result: dict[str, object] = {
        "schema": "literate-ai/release-preparation@1",
        "plan_identity": plan["identity"],
        "next_version": plan["next_version"],
        "branch": created_line or str(snapshot["branch"]),
        "release_line_created": created_line is not None,
        "release_class": plan.get("release_class"),
        "stable_predecessor": plan.get("stable_predecessor"),
        "collateral": plan.get("collateral", []),
        "changed_paths": sorted(
            {
                policy.changelog_path,
                policy.version_authority.path,
                *(item.path for item in policy.version_mirrors),
                *(str(path.relative_to(root)) for path in refreshed_documentation),
            }
        ),
        "next_action": (
            f"review, validate, and commit the declared release authority on {cut}"
            if policy.default_branch is not None
            else "review, validate, and commit the declared release authority"
        ),
    }
    if authorization is not None:
        result["authorization"] = authorization
    if contributions is not None:
        result["contributions"] = contributions
    return result


def _digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _release_perf_build_root(root: Path) -> Path:
    configured = os.environ.get("OBJ_DIR", "").strip()
    if not configured:
        return root / "_build"
    configured_path = Path(configured)
    return configured_path if configured_path.is_absolute() else root / configured_path


_GATE_FAILURE_DETAIL_BYTES = 2000

# Interpreter-resolution variables must never cross into the gate subprocess: a
# caller invoking `litai release check` through a wrapper that sets PYTHONPATH
# (e.g. `make release`, which needs it to import literate_ai from the source
# tree for its own invocation) would otherwise leak that into every command the
# gate spawns beneath it, making those commands resolve literate_ai from both
# the source tree and the installed distribution simultaneously. The gate is
# supposed to reproduce the same verdict regardless of how it was invoked; an
# environment that changes the verdict is a defect in the gate's isolation, not
# in the revision under release (see issue #65 / RELEASE-ENV-001).
_GATE_ENVIRONMENT_DENYLIST = frozenset({"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"})


def _gate_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _GATE_ENVIRONMENT_DENYLIST
    }


def _run_release_gate(
    root: Path,
    policy: ReleasePolicy,
    *,
    run: EvidenceRun | None = None,
    parent: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], str | None]:
    try:
        context = (
            run.node(
                "release/gate",
                operation="release.gate",
                parent=parent,
                host={"kind": "legacy-local", "platform": sys.platform},
            )
            if run is not None
            else nullcontext()
        )
        with context as evidence_node:
            completed = record_subprocess(
                policy.gate_argv,
                cwd=root,
                run=run,
                parent=parent,
                path="release/gate",
                operation="release.gate",
                timeout=policy.gate_timeout_seconds,
                environment=_gate_environment(),
                node=(
                    evidence_node if isinstance(evidence_node, EvidenceNode) else None
                ),
            )
        node_id = (
            evidence_node.node_id if isinstance(evidence_node, EvidenceNode) else None
        )
        return completed, node_id
    except subprocess.TimeoutExpired as exc:
        raise ProjectReleaseError(
            "release.gate_timeout",
            f"release gate exceeded {policy.gate_timeout_seconds} seconds",
        ) from exc
    except OSError as exc:
        raise ProjectReleaseError(
            "release.gate_unavailable", "release gate command could not be started"
        ) from exc


def _release_gate_failure_detail(
    completed: subprocess.CompletedProcess[str],
) -> str:
    """Summarize a failed gate without dumping its complete transcript."""

    for label, stream in (("stderr", completed.stderr), ("stdout", completed.stdout)):
        collapsed = " ".join((stream or "").split())
        if collapsed:
            return f"last {label}: {collapsed[-_GATE_FAILURE_DETAIL_BYTES:]}"
    return "the gate produced no output"


def _resolve_release_target(root: Path, explicit: str | None) -> str:
    """Resolve the release gate's execution target.

    Precedence: an explicit ``--target`` always wins; otherwise the project's
    declared ``ci_targets`` preference (first entry) is used when present;
    otherwise the release gate falls back to today's existing behavior of
    running directly on the machine that invoked ``litai release check``
    (reported in evidence as ``legacy-local`` to distinguish it from an
    explicitly dispatched private-fleet ``local`` target).
    """

    if explicit is not None:
        if explicit not in RELEASE_TARGETS:
            raise ProjectReleaseError(
                "release.target_invalid", f"unsupported release target: {explicit!r}"
            )
        return explicit
    try:
        project = discover_project(root)
    except (OSError, ValueError):
        project = None
    definition = getattr(project, "definition", None)
    ci_targets = getattr(definition, "ci_targets", ())
    if ci_targets:
        return ci_targets[0].target.value
    return "legacy-local"


def _select_release_workers(
    root: Path,
) -> tuple[tuple[ExecutionWorker, ...], tuple[dict[str, object], ...]]:
    """Select every SSH worker in the fleet the local target can dispatch to.

    Fails closed when the catalog is missing/invalid or declares no eligible
    worker. The declared gate command (``make release-check``) has no
    Windows-native equivalent today, so Windows SSH workers are recognized
    but excluded rather than attempted; each exclusion is returned with an
    explicit reason instead of silently vanishing from the fleet. POSIX
    workers are dispatched to in parallel, not ranked or subsetted, since
    a release gate that only some configured machines proved is incomplete
    evidence.
    """

    try:
        configured = resolve_worker_config_path(project_root=root)
        catalog = load_execution_worker_catalog(configured)
    except (ExecutionDispatchAdapterError, UserAssetPathError) as exc:
        raise ProjectReleaseError(
            "release.target_unconfigured",
            "the local target requires a configured private worker fleet "
            f"({WORKERS_CATALOG_FILE} in the user configuration root): {exc.message}",
        ) from exc
    ssh_workers = tuple(
        worker for worker in catalog.workers if worker.kind is ExecutionWorkerKind.SSH
    )
    if not ssh_workers:
        raise ProjectReleaseError(
            "release.target_unconfigured",
            "the local target requires at least one SSH worker declared in "
            f"{WORKERS_CATALOG_FILE}",
        )
    posix_workers = tuple(
        worker for worker in ssh_workers if worker.requirements.os_family != "windows"
    )
    excluded = tuple(
        {
            "worker_id": worker.worker_id,
            "endpoint": worker.endpoint,
            "reason": "release.windows_gate_unsupported",
        }
        for worker in ssh_workers
        if worker.requirements.os_family == "windows"
    )
    if not posix_workers:
        raise ProjectReleaseError(
            "release.target_unconfigured",
            "the local target requires at least one non-Windows SSH worker "
            f"declared in {WORKERS_CATALOG_FILE}; the declared gate has no "
            "Windows-native equivalent yet",
        )
    return posix_workers, excluded


def _posix_ssh_path(value: str) -> str:
    if value.startswith("~/"):
        return '"$HOME"/' + value.removeprefix("~/")
    return shlex.quote(value)


def _redact_worker_dialog(text: str, endpoint: str) -> str:
    return text.replace(endpoint, "<worker-endpoint>")


def _run_release_gate_on_one_worker(
    root: Path,
    policy: ReleasePolicy,
    snapshot: dict[str, object],
    worker: ExecutionWorker,
    *,
    evidence_run: EvidenceRun | None = None,
    evidence_parent: str | None = None,
) -> dict[str, object]:
    """Run the declared gate on one POSIX worker; raise on any failure.

    Returns a per-worker record (worker id/endpoint, gate evidence, wall-clock
    duration) so a fan-out caller can aggregate several of these without
    re-running the probe/dispatch sequence per worker.
    """

    assert worker.endpoint is not None and worker.workspace is not None
    context = (
        evidence_run.node(
            f"release/target/local/{worker.worker_id}",
            operation="release.target.local.worker",
            parent=evidence_parent,
            host={"kind": "ssh", "worker_id": worker.worker_id},
            pins={
                "worker_id": worker.worker_id,
                "endpoint_digest": _digest_text(worker.endpoint),
                "workspace": worker.workspace,
            },
        )
        if evidence_run is not None
        else None
    )
    with context if context is not None else nullcontext() as evidence_node:
        runner = BoundedSshProcessRunner()
        revision = str(snapshot["head"])
        workspace = _posix_ssh_path(worker.workspace.rstrip("/"))
        probe_timeout = min(_WORKER_PROBE_TIMEOUT_SECONDS, policy.gate_timeout_seconds)
        started = time.monotonic()
        try:
            probe = runner.run(
                ssh_arguments(
                    worker.endpoint,
                    f"git -C {workspace} rev-parse HEAD",
                    probe_timeout,
                    transport=worker.transport,
                ),
                cwd=root,
                timeout_seconds=probe_timeout,
            )
        except SshTransportError as exc:
            if isinstance(evidence_node, EvidenceNode):
                evidence_node.attach_text(
                    "probe-error.txt",
                    _redact_worker_dialog(exc.message, worker.endpoint),
                    role="probe-error",
                )
            raise ProjectReleaseError(
                "release.worker_unreachable",
                f"worker {worker.worker_id!r} is unreachable: {exc.message}",
            ) from exc
        if isinstance(evidence_node, EvidenceNode):
            evidence_node.attach_text(
                "probe-stdout.log",
                _redact_worker_dialog(
                    probe.stdout.decode("utf-8", errors="replace"),
                    worker.endpoint,
                ),
                role="probe-stdout",
            )
            evidence_node.attach_text(
                "probe-stderr.log",
                _redact_worker_dialog(
                    probe.stderr.decode("utf-8", errors="replace"),
                    worker.endpoint,
                ),
                role="probe-stderr",
            )
            evidence_node.add_pins(
                control_envelope_bytes=len(probe.stdout) + len(probe.stderr)
            )
        observed_revision = probe.stdout.decode("utf-8", errors="replace").strip()
        if probe.returncode != 0 or observed_revision != revision:
            raise ProjectReleaseError(
                "release.worker_revision_mismatch",
                f"worker {worker.worker_id!r} workspace is not checked out at "
                f"the prepared revision {revision}",
            )
        overlay = remote_live_gate_overlay(
            try_resolve_live_test_selection(
                project_root=root,
                ignore_environment_pins=True,
                require_opencode=True,
                require_openai_api_key=False,
            )
        )
        gate_command = (
            posix_export_prefix(overlay)
            + " && "
            + f"cd {workspace} && "
            + shlex.join(policy.gate_argv)
        )
        try:
            completed = runner.run(
                ssh_arguments(
                    worker.endpoint,
                    gate_command,
                    policy.gate_timeout_seconds,
                    transport=worker.transport,
                ),
                cwd=root,
                timeout_seconds=policy.gate_timeout_seconds,
            )
        except SshTransportError as exc:
            if isinstance(evidence_node, EvidenceNode):
                evidence_node.attach_text(
                    "gate-error.txt",
                    _redact_worker_dialog(exc.message, worker.endpoint),
                    role="gate-error",
                )
            raise ProjectReleaseError(
                "release.gate_unavailable",
                f"release gate dispatch to worker {worker.worker_id!r} failed: "
                f"{exc.message}",
            ) from exc
        stdout_text = completed.stdout.decode("utf-8", errors="replace")
        stderr_text = completed.stderr.decode("utf-8", errors="replace")
        if isinstance(evidence_node, EvidenceNode):
            evidence_node.attach_text(
                "gate-stdout.log",
                _redact_worker_dialog(stdout_text, worker.endpoint),
                role="stdout",
            )
            evidence_node.attach_text(
                "gate-stderr.log",
                _redact_worker_dialog(stderr_text, worker.endpoint),
                role="stderr",
            )
            evidence_node.add_pins(
                control_envelope_bytes=len(completed.stdout) + len(completed.stderr)
            )
        duration_ms = round((time.monotonic() - started) * 1000)
        if completed.returncode:
            raise ProjectReleaseError(
                "release.gate_failed",
                f"release gate on worker {worker.worker_id!r} failed with exit "
                f"status {completed.returncode}: "
                + _release_gate_failure_detail(
                    subprocess.CompletedProcess(
                        gate_command, completed.returncode, stdout_text, stderr_text
                    )
                ),
            )
        return {
            "worker_id": worker.worker_id,
            "endpoint": worker.endpoint,
            "duration_ms": duration_ms,
            "evidence_node_id": (
                evidence_node.node_id
                if isinstance(evidence_node, EvidenceNode)
                else None
            ),
            "gate": {
                "argv": list(policy.gate_argv),
                "exit_status": completed.returncode,
                "stdout_digest": _digest_text(stdout_text),
                "stderr_digest": _digest_text(stderr_text),
            },
        }


def _run_release_gate_on_workers(
    root: Path,
    policy: ReleasePolicy,
    snapshot: dict[str, object],
    *,
    evidence_run: EvidenceRun | None = None,
    evidence_parent: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Fan the declared gate out to every configured POSIX worker in parallel.

    Every dispatched worker must pass; the gate is only as strong as its
    weakest configured machine. Failures are reported deterministically by
    worker id so a flaky rerun always names the same first offender.
    """

    try:
        workers, excluded = _select_release_workers(root)
    except ProjectReleaseError as exc:
        if evidence_run is not None:
            context = evidence_run.node(
                "release/target/local",
                operation="release.target.local",
                parent=evidence_parent,
                pins={"target": "local"},
            )
            with context as unavailable:
                unavailable.mark_unavailable(exc.message)
        raise
    if evidence_run is not None:
        for item in excluded:
            context = evidence_run.node(
                f"release/target/local/{item['worker_id']}",
                operation="release.target.local.worker",
                parent=evidence_parent,
                host={"kind": "ssh", "worker_id": item["worker_id"]},
                pins={
                    "worker_id": item["worker_id"],
                    "endpoint_digest": _digest_text(str(item["endpoint"])),
                },
            )
            with context as evidence_node:
                evidence_node.skip(str(item["reason"]))
    recorder = PerformanceRecorder(build_root=_release_perf_build_root(root))

    def dispatch(worker: ExecutionWorker) -> dict[str, object]:
        with recorder.span(
            "release.check.worker",
            target_kind="worker",
            target_id=worker.worker_id,
        ):
            return _run_release_gate_on_one_worker(
                root,
                policy,
                snapshot,
                worker,
                evidence_run=evidence_run,
                evidence_parent=evidence_parent,
            )

    outcomes: dict[str, dict[str, object] | ProjectReleaseError] = {}
    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        future_to_worker = {
            executor.submit(dispatch, worker): worker for worker in workers
        }
        for future in as_completed(future_to_worker):
            worker = future_to_worker[future]
            try:
                outcomes[worker.worker_id] = future.result()
            except ProjectReleaseError as exc:
                outcomes[worker.worker_id] = exc
    failures = {
        worker_id: outcome
        for worker_id, outcome in outcomes.items()
        if isinstance(outcome, ProjectReleaseError)
    }
    if failures:
        first_id = min(failures)
        raise failures[first_id]
    per_worker = [outcomes[worker.worker_id] for worker in workers]
    gate = {
        "argv": list(policy.gate_argv),
        "exit_status": 0,
        "worker_count": len(per_worker),
    }
    target: dict[str, object] = {
        "kind": "local",
        "workers": per_worker,
        "excluded_workers": list(excluded),
    }
    return gate, target


def _github_run_matches(
    root: Path, repository: str, branch: str, revision: str, workflow: str = "CI"
) -> list[dict[str, object]]:
    try:
        completed = subprocess.run(
            (
                "gh",
                "run",
                "list",
                "--repo",
                repository,
                "--branch",
                branch,
                "--workflow",
                workflow,
                "--json",
                "databaseId,headSha,status,conclusion,url",
                "--limit",
                "50",
            ),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectReleaseError(
            "release.provider_unavailable",
            "GitHub run polling requires an authenticated gh command",
        ) from exc
    if completed.returncode:
        raise ProjectReleaseError(
            "release.provider_unavailable",
            f"gh run list failed with exit status {completed.returncode}",
        )
    try:
        runs = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProjectReleaseError(
            "release.provider_response_invalid",
            "gh run list did not return valid JSON",
        ) from exc
    if not isinstance(runs, list):
        raise ProjectReleaseError(
            "release.provider_response_invalid",
            "gh run list did not return a JSON array",
        )
    return [
        item
        for item in runs
        if isinstance(item, dict) and item.get("headSha") == revision
    ]


def _run_release_gate_via_github(
    root: Path,
    policy: ReleasePolicy,
    snapshot: dict[str, object],
    *,
    evidence_node: EvidenceNode | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    if policy.provider_kind != "github" or not policy.provider_repository:
        raise ProjectReleaseError(
            "release.target_unconfigured",
            "the github target requires release_policy.provider.kind == "
            '"github" with a configured repository',
        )
    revision = str(snapshot["head"])
    branch = str(snapshot["branch"])
    workflow = getattr(_repository_policy(root), "release_ci_workflow", "CI")
    deadline = time.monotonic() + policy.gate_timeout_seconds
    run: dict[str, object] | None = None
    while True:
        matches = _github_run_matches(
            root, policy.provider_repository, branch, revision, workflow
        )
        completed_matches = [
            item for item in matches if item.get("status") == "completed"
        ]
        if completed_matches:
            run = completed_matches[0]
            break
        if time.monotonic() >= deadline:
            raise ProjectReleaseError(
                "release.gate_timeout",
                f"no completed GitHub Actions run for {revision} within "
                f"{policy.gate_timeout_seconds} seconds",
            )
        time.sleep(
            max(1, min(_GITHUB_POLL_INTERVAL_SECONDS, deadline - time.monotonic()))
        )
    conclusion = run.get("conclusion")
    target: dict[str, object] = {
        "kind": "github",
        "run_id": run.get("databaseId"),
        "run_url": run.get("url"),
        "conclusion": conclusion,
    }
    if conclusion != "success":
        if evidence_node is not None:
            run_id = run.get("databaseId")
            try:
                if run_id is None:
                    raise ValueError("GitHub run omitted databaseId")
                failed_jobs = subprocess.run(
                    ("gh", "run", "view", str(run_id), "--log-failed"),
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=max(1, int(deadline - time.monotonic())),
                )
                if failed_jobs.returncode:
                    raise RuntimeError(
                        (failed_jobs.stderr or failed_jobs.stdout).strip()
                        or f"gh exited with status {failed_jobs.returncode}"
                    )
                output = failed_jobs.stdout
                limit = 256 * 1024
                if len(output.encode("utf-8")) > 2 * limit:
                    encoded = output.encode("utf-8")
                    output = (
                        encoded[:limit].decode("utf-8", errors="replace")
                        + "\n...[failed-job log elided]...\n"
                        + encoded[-limit:].decode("utf-8", errors="replace")
                    )
                evidence_node.attach_text(
                    "failed-jobs.log",
                    output,
                    role="failed-jobs",
                    media_type="text/plain",
                )
            except Exception as exc:
                evidence_node.add_pins(
                    failed_jobs_log_unavailable=type(exc).__name__ + ": " + str(exc)
                )
        raise ProjectReleaseError(
            "release.gate_failed",
            f"GitHub Actions run {run.get('url')} concluded {conclusion!r}",
        )
    gate = {
        "argv": list(policy.gate_argv),
        "exit_status": 0,
        "stdout_digest": _digest_text(json.dumps(run, sort_keys=True)),
        "stderr_digest": _digest_text(""),
    }
    return gate, target


def _release_record_destination(root: Path, output: Path) -> Path:
    destination = output if output.is_absolute() else root / output
    try:
        resolved_root = root.resolve(strict=True)
        resolved_parent = destination.parent.resolve(strict=False)
    except OSError as exc:
        raise ProjectReleaseError(
            "release.record_unavailable", "prepared-record parent is unavailable"
        ) from exc
    inside = (
        resolved_parent == resolved_root or resolved_root in resolved_parent.parents
    )
    if inside:
        relative = destination.relative_to(root)
        if not relative.parts or relative.parts[0] != "_build":
            raise ProjectReleaseError(
                "release.record_overlaps_authority",
                "a project-local prepared record must be written beneath _build",
            )
    if destination.is_symlink():
        raise ProjectReleaseError(
            "release.record_unsafe", "prepared-record destination cannot be a symlink"
        )
    return destination


def _check_release(
    project: Path,
    plan_path: Path,
    *,
    output: Path,
    target: str | None = None,
    evidence_run: EvidenceRun | None = None,
    evidence_node: EvidenceNode | None = None,
    actor: str | None = None,
    break_glass_reason: str | None = None,
) -> dict[str, object]:
    plan = _load_record(plan_path, RELEASE_PLAN_SCHEMA)
    root, policy = load_release_policy(project)
    authorization: dict[str, str | None] | None = None
    if policy.schema == RELEASE_POLICY_SCHEMA:
        _require_pre_release_target(root, policy, str(plan["next_version"]))
        authorization = _authorize(
            root,
            policy,
            actor=actor,
            operation="release check",
            allow_break_glass=plan["transition"] == "patch",
            break_glass_reason=break_glass_reason,
        )
    snapshot = _git_snapshot(root)
    if evidence_node is not None:
        evidence_node.add_pins(
            plan_identity=plan.get("identity"),
            policy_identity=policy.identity,
            base_revision=plan.get("source_revision"),
            prepared_revision=snapshot.get("head"),
            branch=snapshot.get("branch"),
            version=plan.get("next_version"),
            tag=plan.get("tag"),
            provider=plan.get("provider"),
        )
    if not snapshot["clean"]:
        raise ProjectReleaseError(
            "release.check_dirty", "release check requires a clean prepared commit"
        )
    if snapshot["branch"] != _plan_cut_branch(plan):
        raise ProjectReleaseError(
            "release.plan_stale", "prepared release is on a different branch"
        )
    _require_release_line(
        policy,
        branch=str(snapshot["branch"]),
        version=str(plan["next_version"]),
    )
    ancestor = _git(
        root,
        "merge-base",
        "--is-ancestor",
        str(plan["source_revision"]),
        str(snapshot["head"]),
        check=False,
    )
    if ancestor.returncode:
        raise ProjectReleaseError(
            "release.plan_stale", "prepared commit does not descend from the plan"
        )
    changed = frozenset(
        _git(
            root,
            "diff",
            "--name-only",
            f"{plan['source_revision']}..{snapshot['head']}",
        ).stdout.splitlines()
    )
    # The configured test receipt necessarily binds to the exact prepared
    # revision (it hashes the full project authority graph), so refreshing
    # it is only possible *after* prepare's own commit -- exclude it from
    # the scope check rather than forcing every project with a receipt
    # policy into an unsatisfiable prepare-then-freeze sequence.
    project = discover_project(root)
    definition = getattr(project, "definition", None)
    test_receipt_path = getattr(definition, "test_receipt", None)
    reviewed_documentation_path = (
        None
        if policy.documentation_authority_paths
        else _reviewed_documentation_authority_path(root)
    )
    declared = frozenset(
        {
            policy.changelog_path,
            policy.version_authority.path,
            *(item.path for item in policy.version_mirrors),
            *policy.documentation_authority_paths,
            *((reviewed_documentation_path,) if reviewed_documentation_path else ()),
            *((test_receipt_path,) if test_receipt_path else ()),
        }
    )
    recovering_current_version = (
        not changed
        and plan["transition"] == "explicit"
        and plan["current_version"] == plan["next_version"]
        and _changelog_version_heading_count(
            _binding_path(root, policy.changelog_path).read_text(encoding="utf-8"),
            plan["next_version"],
        )
        == 1
    )
    if (not changed and not recovering_current_version) or not changed <= declared:
        raise ProjectReleaseError(
            "release.prepared_scope_invalid",
            "prepared commit must change only declared release authority paths",
        )
    if policy.identity != plan["policy_identity"]:
        raise ProjectReleaseError(
            "release.plan_stale", "release policy changed after planning"
        )
    version = current_release_version(root, policy)
    if version != plan["next_version"]:
        raise ProjectReleaseError(
            "release.not_prepared", "declared versions do not equal the planned version"
        )
    contributions = _revalidate_release_closure(
        root,
        policy,
        plan,
        revision=str(snapshot["head"]),
        require_default_branch_ancestry=True,
    )
    tags = _git(root, "tag", "--list", str(plan["tag"])).stdout.splitlines()
    if tags:
        raise ProjectReleaseError(
            "release.tag_exists", f"release tag already exists: {plan['tag']}"
        )
    resolved_target = _resolve_release_target(root, target)
    if evidence_node is not None:
        evidence_node.add_pins(resolved_target=resolved_target)
    if resolved_target == "gitlab":
        raise ProjectReleaseError(
            "release.target_unsupported",
            "the gitlab release-gate target is not yet supported; use "
            "--target local or --target github",
        )
    if resolved_target == "local":
        gate, target_evidence = _run_release_gate_on_workers(
            root,
            policy,
            snapshot,
            evidence_run=evidence_run,
            evidence_parent=evidence_node.node_id if evidence_node else None,
        )
    elif resolved_target == "github":
        target_context = (
            evidence_run.node(
                "release/target/github",
                operation="release.target.github",
                parent=evidence_node.node_id if evidence_node else None,
                pins={"revision": snapshot["head"], "branch": snapshot["branch"]},
            )
            if evidence_run is not None
            else None
        )
        with (
            target_context if target_context is not None else nullcontext()
        ) as target_node:
            try:
                gate, target_evidence = _run_release_gate_via_github(
                    root, policy, snapshot, evidence_node=target_node
                )
                if isinstance(target_node, EvidenceNode):
                    target_node.add_pins(
                        run_id=target_evidence.get("run_id"),
                        run_url=target_evidence.get("run_url"),
                        conclusion=target_evidence.get("conclusion"),
                    )
            except ProjectReleaseError as exc:
                if isinstance(target_node, EvidenceNode):
                    target_node.add_pins(error_code=exc.code)
                raise
    else:
        # No explicit --target and no declared ci_targets preference: preserve
        # today's existing behavior of running the gate directly on whatever
        # machine invoked `litai release check`.
        if evidence_run is not None and evidence_node is not None:
            for skipped_target, reason in (
                ("local", "not requested; no SSH worker fan-out is needed"),
                ("github", "not requested; no GitHub provider fan-out is needed"),
            ):
                context = evidence_run.node(
                    f"release/target/{skipped_target}",
                    operation=f"release.target.{skipped_target}",
                    parent=evidence_node.node_id,
                    pins={"target": skipped_target},
                )
                with context as skipped_node:
                    skipped_node.skip(reason)
        completed, gate_node_id = _run_release_gate(
            root,
            policy,
            run=evidence_run,
            parent=evidence_node.node_id if evidence_node else None,
        )
        if completed.returncode:
            # The gate is the longest step in a release. Reporting only its
            # exit status forces the operator to re-run a multi-hour command
            # by hand to learn what failed, so carry a bounded tail of what
            # it actually said.
            raise ProjectReleaseError(
                "release.gate_failed",
                f"release gate failed with exit status {completed.returncode}: "
                + _release_gate_failure_detail(completed),
            )
        gate = {
            "argv": list(policy.gate_argv),
            "exit_status": completed.returncode,
            "stdout_digest": _digest_text(completed.stdout),
            "stderr_digest": _digest_text(completed.stderr),
        }
        if isinstance(gate_node_id, str):
            gate["evidence_node_id"] = gate_node_id
            gate["stdout_path"] = f"{gate_node_id}/stdout.log"
            gate["stderr_path"] = f"{gate_node_id}/stderr.log"
        target_evidence = {"kind": "legacy-local"}
    artifacts = _qualify_release_files(root, policy, snapshot, version)
    authenticated_receipt = _require_authenticated_release_receipt(root, policy)
    result: dict[str, object] = {
        "schema": LEGACY_PREPARED_RELEASE_SCHEMA,
        "plan_identity": plan["identity"],
        "policy_identity": policy.identity,
        "base_revision": plan["source_revision"],
        "prepared_revision": snapshot["head"],
        "branch": snapshot["branch"],
        "version": version,
        "tag": plan["tag"],
        "transition": plan["transition"],
        "release_class": plan.get("release_class"),
        "stable_predecessor": plan.get("stable_predecessor"),
        "remote": policy.remote,
        "provider": plan["provider"],
        "gate": gate,
        "target": target_evidence,
        "collateral": plan.get("collateral", []),
        **({"artifacts": artifacts} if artifacts is not None else {}),
        **(
            {"authenticated_receipt_identity": authenticated_receipt}
            if authenticated_receipt is not None
            else {}
        ),
    }
    if contributions is not None:
        result["contributions"] = contributions
    if authorization is not None:
        result["authorization"] = authorization
    if evidence_run is not None and evidence_node is not None:
        result["evidence"] = {
            "run_id": evidence_run.run_id,
            "root": str(evidence_run.root.resolve()),
            "index_path": str(evidence_run.root / "index.json"),
            "node_id": evidence_node.node_id,
        }
        result["schema"] = PREPARED_RELEASE_SCHEMA
    result["identity"] = canonical_identity(result).uri
    destination = _release_record_destination(root, output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        destination,
        json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
    )
    return {**result, "record": str(destination)}


def check_release(
    project: Path,
    plan_path: Path,
    *,
    output: Path,
    target: str | None = None,
    actor: str | None = None,
    break_glass_reason: str | None = None,
) -> dict[str, object]:
    """Check one prepared release while retaining a fail-soft evidence closure."""

    run = attach_run(project)
    if run is None:
        run = open_run(project, operation="release.check")
    if run is not None:
        report_progress(f"Release evidence root: {run.root}")
    state = "failed"
    context = (
        run.node("release", operation="release.check")
        if run is not None
        else nullcontext()
    )
    try:
        with context as evidence_node:
            try:
                result = _check_release(
                    project,
                    plan_path,
                    output=output,
                    target=target,
                    evidence_run=run,
                    evidence_node=(
                        evidence_node
                        if isinstance(evidence_node, EvidenceNode)
                        else None
                    ),
                    actor=actor,
                    break_glass_reason=break_glass_reason,
                )
                state = "passed"
                return result
            except ProjectReleaseError as exc:
                if isinstance(evidence_node, EvidenceNode):
                    evidence_node.add_pins(error_code=exc.code)
                raise
            except BaseException as exc:
                if isinstance(evidence_node, EvidenceNode):
                    evidence_node.add_pins(
                        error_code=f"exception.{type(exc).__name__.casefold()}"
                    )
                raise
    finally:
        if run is not None:
            run.close(state)


def create_release_candidate(
    project: Path,
    *,
    version: str,
    actor: str | None,
    authorize_external_write: bool,
) -> dict[str, object]:
    if not authorize_external_write:
        raise ProjectReleaseError(
            "release.external_authorization_required",
            "rc requires --authorize-external-write",
        )
    root, policy = load_release_policy(project)
    authorization = _authorize(
        root, policy, actor=actor, operation="release candidate publication"
    )
    canonical = _canonical_version(version, policy.version_scheme)
    syntax = (
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
        r"(?:0|[1-9][0-9]*)-rc\.[1-9][0-9]*"
        if policy.version_scheme == "semver"
        else r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
        r"(?:0|[1-9][0-9]*)rc[1-9][0-9]*"
    )
    if not _is_prerelease(canonical, policy.version_scheme) or not re.fullmatch(
        syntax, canonical
    ):
        raise ProjectReleaseError(
            "release.rc_version_invalid",
            "RC version must be canonical x.y.z-rc.N (semver) or x.y.zrcN (pep440)",
        )
    _require_pre_release_target(root, policy, canonical)
    snapshot = _git_snapshot(root)
    if not snapshot["clean"] or snapshot["branch"] != policy.default_branch:
        raise ProjectReleaseError(
            "release.rc_source_invalid",
            "RC tagging requires a clean default-branch checkout",
        )
    if policy.provider_kind != "github" or not policy.provider_repository:
        raise ProjectReleaseError(
            "release.target_unconfigured", "RC tagging requires a GitHub provider"
        )
    matches = _github_run_matches(
        root,
        policy.provider_repository,
        str(snapshot["branch"]),
        str(snapshot["head"]),
        getattr(_repository_policy(root), "release_ci_workflow", "CI"),
    )
    green = next(
        (
            item
            for item in matches
            if item.get("status") == "completed" and item.get("conclusion") == "success"
        ),
        None,
    )
    if green is None:
        raise ProjectReleaseError(
            "release.rc_ci_unavailable",
            "RC tagging requires successful exact-head GitHub CI evidence",
        )
    tag = f"{policy.tag_prefix}{canonical}"
    if _local_tag_revision(root, tag) is not None:
        raise ProjectReleaseError(
            "release.tag_exists", f"release tag already exists: {tag}"
        )
    remote_tag, _annotated = _remote_tag_revision(root, policy.remote, tag)
    if remote_tag is not None:
        raise ProjectReleaseError(
            "release.tag_exists", f"release tag already exists: {tag}"
        )
    _git(
        root,
        "tag",
        "-s" if policy.signed_tag else "-a",
        tag,
        "-m",
        f"Release candidate {tag}",
    )
    _git(root, "push", policy.remote, f"refs/tags/{tag}")
    # Build wheel and create a prerelease GitHub Release with the asset
    wheel_path: Path | None = None
    if policy.provider_kind == "github" and policy.provider_repository:
        wheel_path = _build_wheel(root)
        try:
            create_args = [
                "gh",
                "release",
                "create",
                tag,
                "--repo",
                policy.provider_repository,
                "--title",
                f"Release {tag}",
                "--generate-notes",
                "--prerelease",
            ]
            subprocess.run(
                create_args,
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (
            OSError,
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
        ) as exc:
            raise ProjectReleaseError(
                "release.provider_unavailable",
                f"GitHub Release creation failed: {exc}",
            ) from exc
        if wheel_path is not None:
            _upload_wheel_asset(root, policy.provider_repository, tag, wheel_path)
    return {
        "schema": "literate-ai/release-candidate-receipt@1",
        "version": canonical,
        "tag": tag,
        "revision": snapshot["head"],
        "remote": policy.remote,
        "ci": {
            "run_id": green.get("databaseId"),
            "run_url": green.get("url"),
            "conclusion": "success",
        },
        "authorization": authorization,
        "wheel": str(wheel_path) if wheel_path is not None else None,
    }


def _local_tag_revision(root: Path, tag: str) -> str | None:
    completed = _git(
        root, "rev-parse", "--verify", f"refs/tags/{tag}^{{}}", check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _require_local_tag_shape(root: Path, tag: str, *, signed: bool) -> None:
    object_type = _git(root, "cat-file", "-t", f"refs/tags/{tag}").stdout.strip()
    if object_type != "tag":
        raise ProjectReleaseError(
            "release.local_tag_invalid", f"release tag is not annotated: {tag}"
        )
    if signed:
        verified = _git(root, "tag", "-v", tag, check=False)
        if verified.returncode:
            raise ProjectReleaseError(
                "release.local_tag_invalid",
                f"release tag does not have a locally verifiable signature: {tag}",
            )


def _remote_tag_revision(root: Path, remote: str, tag: str) -> tuple[str | None, bool]:
    completed = _git(
        root,
        "ls-remote",
        "--tags",
        remote,
        f"refs/tags/{tag}",
        f"refs/tags/{tag}^{{}}",
        check=False,
    )
    if completed.returncode:
        raise ProjectReleaseError(
            "release.remote_unavailable", f"cannot inspect remote {remote!r}"
        )
    refs = {
        reference: revision
        for line in completed.stdout.splitlines()
        if "\t" in line
        for revision, reference in (line.split("\t", 1),)
    }
    peeled = refs.get(f"refs/tags/{tag}^{{}}")
    direct = refs.get(f"refs/tags/{tag}")
    return peeled or direct, peeled is not None


def _remote_branch_revision(root: Path, remote: str, branch: str) -> str | None:
    completed = _git(
        root,
        "ls-remote",
        "--heads",
        remote,
        f"refs/heads/{branch}",
        check=False,
    )
    if completed.returncode:
        raise ProjectReleaseError(
            "release.remote_unavailable", f"cannot inspect remote {remote!r}"
        )
    line = completed.stdout.strip()
    return line.split("\t", 1)[0] if "\t" in line else None


def merge_release_pull_request(
    project: Path,
    *,
    number: int,
    actor: str | None,
    authorize_external_write: bool,
) -> dict[str, object]:
    if not authorize_external_write:
        raise ProjectReleaseError(
            "release.external_authorization_required",
            "release-line PR merge requires --authorize-external-write",
        )
    root, policy = load_release_policy(project)
    if policy.provider_kind != "github" or not policy.provider_repository:
        raise ProjectReleaseError(
            "release.target_unconfigured", "release-line PR merge requires GitHub"
        )
    authorization = _authorize(
        root, policy, actor=actor, operation="release-line pull request merge"
    )
    try:
        completed = subprocess.run(
            (
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                policy.provider_repository,
                "--json",
                "number,state,isDraft,baseRefName,headRefOid,body,statusCheckRollup",
            ),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        metadata = json.loads(completed.stdout) if completed.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ProjectReleaseError(
            "release.provider_unavailable",
            "cannot inspect immutable GitHub PR metadata",
        ) from exc
    if not isinstance(metadata, dict):
        raise ProjectReleaseError(
            "release.provider_unavailable",
            "cannot inspect immutable GitHub PR metadata",
        )
    base = metadata.get("baseRefName")
    if not isinstance(base, str) or not _RELEASE_LINE.fullmatch(base):
        raise ProjectReleaseError(
            "release.pr_base_invalid",
            "release merge command only accepts release-line PRs",
        )
    body = metadata.get("body")
    trailer = f"Release-Line: {base}"
    if not isinstance(body, str) or trailer not in body.splitlines():
        raise ProjectReleaseError(
            "release.pr_trailer_invalid",
            f"PR body must contain exact trailer {trailer!r}",
        )
    checks = metadata.get("statusCheckRollup")
    if metadata.get("state") != "OPEN" or metadata.get("isDraft") is not False:
        raise ProjectReleaseError(
            "release.pr_not_mergeable", "PR must be open and non-draft"
        )
    if (
        not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(check, dict)
            or check.get("conclusion") not in {"SUCCESS", "NEUTRAL", "SKIPPED"}
            for check in checks
        )
    ):
        raise ProjectReleaseError(
            "release.pr_checks_not_green", "all release-line PR checks must be green"
        )
    head = metadata.get("headRefOid")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise ProjectReleaseError(
            "release.provider_response_invalid", "PR head is invalid"
        )
    method = getattr(
        getattr(_repository_policy(root), "merge_method", None), "value", "merge"
    )
    merged = subprocess.run(
        (
            "gh",
            "pr",
            "merge",
            str(number),
            "--repo",
            policy.provider_repository,
            f"--{method}",
            "--match-head-commit",
            head,
        ),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if merged.returncode:
        raise ProjectReleaseError(
            "release.pr_merge_failed", "GitHub rejected the guarded release-line merge"
        )
    return {
        "schema": "literate-ai/release-line-merge-receipt@1",
        "pull_request": number,
        "base": base,
        "head_revision": head,
        "merge_method": method,
        "authorization": authorization,
    }


def _changelog_release_notes(text: str, version: str) -> str:
    """Extract just one version's section from a changelog for release notes.

    `prepare_release` inserts each new version's heading directly under the
    permanent `## Unreleased` placeholder (never renaming it), so the raw
    changelog file always starts with "## Unreleased" followed by every past
    version too. Publishing that whole file as GitHub release notes made
    v0.5.0's release page open with the word "Unreleased" instead of its own
    entries -- this isolates one version's own text instead.
    """
    lines = text.splitlines(keepends=True)
    heading = re.compile(rf"^## {re.escape(version)}(?:\s|$)")
    start = next(
        (index for index, line in enumerate(lines) if heading.match(line)), None
    )
    if start is None:
        raise ProjectReleaseError(
            "release.changelog_invalid",
            f"changelog has no heading for version {version}",
        )
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    return "".join(lines[start + 1 : end]).strip("\n") + "\n"


def _qualified_release_files(
    root: Path,
    policy: ReleasePolicy,
    prepared: dict[str, Any],
    *,
    check_bytes: bool = True,
) -> dict[str, Any] | None:
    from literate_ai.release_files import validate_release_files

    if policy.artifact_gate is None:
        if "artifacts" in prepared:
            raise ProjectReleaseError(
                "release.artifacts_invalid",
                "artifact evidence requires its original policy",
            )
        return None
    try:
        return validate_release_files(
            root,
            prepared.get("artifacts"),
            revision=str(prepared["prepared_revision"]),
            version=str(prepared["version"]),
            required_roles=tuple(policy.artifact_gate["required_roles"]),
            check_bytes=check_bytes,
        )
    except (ValueError, OSError) as exc:
        raise ProjectReleaseError("release.artifacts_invalid", str(exc)) from exc


def _qualify_release_files(
    root: Path,
    policy: ReleasePolicy,
    snapshot: dict[str, Any],
    version: str,
) -> dict[str, Any] | None:
    from literate_ai.release_files import release_file_path

    gate = policy.artifact_gate
    if gate is None:
        return None
    try:
        destination = release_file_path(root, gate["manifest"])
        _release_record_destination(root, destination)
        # A successful command must create fresh evidence, never reuse an old pass.
        if destination.exists():
            destination.unlink()
        subprocess.run(
            gate["argv"],
            cwd=root,
            check=True,
            timeout=gate["timeout_seconds"],
        )
        artifacts = _read_json(destination, code="release.artifacts_invalid")
        result = _qualified_release_files(
            root,
            policy,
            {
                "prepared_revision": snapshot["head"],
                "version": version,
                "artifacts": artifacts,
            },
        )
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise ProjectReleaseError(
            "release.artifact_gate_failed", "release artifact qualification failed"
        ) from exc
    current = _git_snapshot(root)
    if (
        not current["clean"]
        or current["head"] != snapshot["head"]
        or current["branch"] != snapshot["branch"]
    ):
        raise ProjectReleaseError(
            "release.prepared_stale", "artifact qualification changed prepared source"
        )
    return result


def _verify_release_file_assets(
    root: Path,
    repository: str,
    tag: str,
    artifacts: dict[str, Any],
) -> None:
    import tempfile

    from literate_ai.release_files import file_identity

    for item in artifacts["files"]:
        name = Path(item["path"]).name
        with tempfile.TemporaryDirectory(prefix="litai-asset-verify-") as directory:
            try:
                subprocess.run(
                    [
                        "gh",
                        "release",
                        "download",
                        tag,
                        "--repo",
                        repository,
                        "--pattern",
                        name,
                        "--dir",
                        directory,
                    ],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    timeout=300,
                )
                downloaded = Path(directory) / name
                if (
                    downloaded.is_symlink()
                    or downloaded.stat().st_size != item["size"]
                    or file_identity(downloaded) != item["identity"]
                ):
                    raise ValueError(
                        "published release file differs from qualified bytes"
                    )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise ProjectReleaseError(
                    "release.published_artifact_mismatch",
                    f"cannot verify qualified asset {name}",
                ) from exc


def _build_wheel(root: Path) -> Path | None:
    """Build a distribution wheel from the clean checkout.

    Returns the local wheel path on success, None if the project is not a
    buildable Python distribution (no ``pyproject.toml``/``setup.py``) — a
    non-Python project or test fixture simply gets no wheel asset rather than
    a failed release.
    """
    import shutil
    import tempfile

    if not ((root / "pyproject.toml").is_file() or (root / "setup.py").is_file()):
        return None

    with tempfile.TemporaryDirectory(prefix="litai-wheel-") as wheel_dir:
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--wheel-dir",
                    wheel_dir,
                    str(root),
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ProjectReleaseError(
                "release.wheel_build_failed",
                f"Distribution wheel build failed: {exc}",
            ) from exc
        wheels = list(Path(wheel_dir).glob("*.whl"))
        if len(wheels) != 1:
            raise ProjectReleaseError(
                "release.wheel_build_failed",
                f"Expected exactly one wheel, found {len(wheels)}",
            )
        wheel = wheels[0]
        final = root / "_build" / wheel.name
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(wheel), str(final))
        return final


def _upload_wheel_asset(root: Path, repository: str, tag: str, wheel: Path) -> None:
    """Upload a wheel as a GitHub Release asset, idempotently.

    ``--clobber`` makes a re-run or resumed publish overwrite an identical
    already-attached asset instead of failing ``release.wheel_upload_failed``
    after the wheel is already present (RELEASE-018-IDEMPOTENT).
    """

    try:
        subprocess.run(
            [
                "gh",
                "release",
                "upload",
                tag,
                str(wheel),
                "--repo",
                repository,
                "--clobber",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise ProjectReleaseError(
            "release.wheel_upload_failed",
            f"GitHub Release asset upload failed: {exc}",
        ) from exc


def _github_release_evidence(
    root: Path, repository: str, tag: str
) -> dict[str, object] | None:
    """Observe the externally meaningful GitHub release, not only its URL."""

    try:
        completed = subprocess.run(
            (
                "gh",
                "release",
                "view",
                tag,
                "--repo",
                repository,
                "--json",
                "url,isDraft,isPrerelease,body,assets",
            ),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectReleaseError(
            "release.provider_unavailable",
            "GitHub publication requires an authenticated gh command",
        ) from exc
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ProjectReleaseError(
            "release.provider_response_invalid",
            "GitHub release response is not valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectReleaseError(
            "release.provider_response_invalid",
            "GitHub release response must be an object",
        )
    url = payload.get("url")
    body = payload.get("body")
    draft = payload.get("isDraft")
    prerelease = payload.get("isPrerelease")
    assets = payload.get("assets")
    if (
        not isinstance(url, str)
        or not url.strip()
        or not isinstance(body, str)
        or not isinstance(draft, bool)
        or not isinstance(prerelease, bool)
        or not isinstance(assets, list)
    ):
        raise ProjectReleaseError(
            "release.provider_response_invalid",
            "GitHub release response omits URL, state, notes, or assets",
        )
    observed_assets: list[dict[str, object]] = []
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise ProjectReleaseError(
                "release.provider_response_invalid",
                f"GitHub release asset {index} must be an object",
            )
        name = asset.get("name")
        size = asset.get("size")
        state = asset.get("state", "uploaded")
        digest = asset.get("digest")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(state, str)
            or (digest is not None and not isinstance(digest, str))
        ):
            raise ProjectReleaseError(
                "release.provider_response_invalid",
                f"GitHub release asset {index} has invalid name, size, state, "
                "or digest",
            )
        observed_assets.append(
            {
                "name": name,
                "size": size,
                "state": state,
                **({"digest": digest} if digest is not None else {}),
            }
        )
    return {
        "url": url.strip(),
        "draft": draft,
        "prerelease": prerelease,
        "notes_present": bool(body.strip()),
        "assets": observed_assets,
    }


def _github_release_url(root: Path, repository: str, tag: str) -> str | None:
    evidence = _github_release_evidence(root, repository, tag)
    return None if evidence is None else str(evidence["url"])


def _expected_wheel_prefix(root: Path, version: str) -> str | None:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file() or pyproject.is_symlink():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectReleaseError(
            "release.package_metadata_invalid",
            "pyproject.toml cannot be read while deriving the required wheel asset",
        ) from exc
    project = data.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    distribution = re.sub(r"[-_.]+", "_", name.strip())
    normalized_version = str(Version(version))
    return f"{distribution}-{normalized_version}-"


def _require_stable_github_release(
    root: Path,
    repository: str,
    tag: str,
    *,
    version: str,
    require_wheel: bool,
) -> dict[str, object]:
    evidence = _github_release_evidence(root, repository, tag)
    if evidence is None:
        raise ProjectReleaseError(
            "release.published_provider_missing",
            f"GitHub release for {tag} is missing; Git publication may be complete",
        )
    if evidence["draft"] or evidence["prerelease"]:
        raise ProjectReleaseError(
            "release.published_provider_unstable",
            f"GitHub release for {tag} is not a stable published release",
        )
    if not evidence["notes_present"]:
        raise ProjectReleaseError(
            "release.published_notes_missing",
            f"GitHub release for {tag} has no release notes",
        )
    wheel_prefix = _expected_wheel_prefix(root, version) if require_wheel else None
    if wheel_prefix is not None:
        assets = evidence["assets"]
        assert isinstance(assets, list)
        wheel_assets = [
            asset
            for asset in assets
            if isinstance(asset, dict)
            and str(asset.get("name", "")).startswith(wheel_prefix)
            and str(asset.get("name", "")).endswith(".whl")
            and asset.get("state") == "uploaded"
            and isinstance(asset.get("size"), int)
            and int(asset["size"]) > 0
        ]
        if not wheel_assets:
            raise ProjectReleaseError(
                "release.published_asset_missing",
                f"GitHub release for {tag} lacks a nonempty uploaded "
                f"{wheel_prefix}*.whl asset",
            )
    return evidence


def _version_at_most(a: str, b: str, *, scheme: str) -> bool:
    parse = SemanticVersion.parse if scheme == "semver" else Version
    return parse(a) <= parse(b)


def _advance_default_branch_after_publish(
    root: Path,
    policy: ReleasePolicy,
    *,
    published_branch: str,
    published_version: str,
) -> dict[str, object] | None:
    """Best-effort: keep the default branch's version ahead of what just shipped.

    Runs automatically at the end of ``publish_release`` whenever the policy
    declares a ``default_branch`` different from the branch that was just
    published -- the exact gap that let this repository's own ``main`` sit
    at an old version through two real releases before this existed (see
    ``advance_default_branch_version``, the manual equivalent). Isolated in
    a throwaway worktree, never touching the caller's own checkout. Only
    acts when the default branch's own version has not already moved past
    what was just published; when it has, there is nothing to fix. Applies
    exactly one patch bump past the published version -- deliberately not a
    judgment call about the next minor/major target, which stays a manual
    decision via ``litai release advance-default-branch``.

    Failures here are reported in the publication receipt but never raise:
    the release already shipped successfully by this point, and a
    version-marker mismatch on a different branch is a different, lower-
    severity class of problem than a failed release.
    """

    if policy.default_branch is None or policy.default_branch == published_branch:
        return None
    try:
        _git(root, "fetch", policy.remote, policy.default_branch)
        remote_ref = f"{policy.remote}/{policy.default_branch}"
        with tempfile.TemporaryDirectory(prefix="litai-advance-default-") as tmp:
            worktree = Path(tmp) / "worktree"
            _git(root, "worktree", "add", "--detach", str(worktree), remote_ref)
            try:
                current_default = current_release_version(worktree, policy)
                if not _version_at_most(
                    current_default, published_version, scheme=policy.version_scheme
                ):
                    return {
                        "branch": policy.default_branch,
                        "action": "skipped",
                        "reason": "already ahead of the published version",
                        "previous_version": current_default,
                    }
                next_version = _next_version(
                    published_version, "patch", None, scheme=policy.version_scheme
                )
                rendered = tuple(
                    _render_binding(
                        worktree,
                        binding,
                        current_default,
                        next_version,
                        version_scheme=policy.version_scheme,
                    )
                    for binding in (policy.version_authority, *policy.version_mirrors)
                )
                _write_transaction(rendered)
                refreshed_documentation = (
                    _refresh_documentation_authority(worktree, policy)
                    if policy.documentation_authority_paths
                    else ()
                )
                changed = sorted(
                    {
                        policy.version_authority.path,
                        *(item.path for item in policy.version_mirrors),
                        *(
                            str(path.relative_to(worktree))
                            for path in refreshed_documentation
                        ),
                    }
                )
                _git_in(worktree, "add", *changed)
                _git_in(
                    worktree,
                    "commit",
                    "-m",
                    f"Advance {policy.default_branch} version to {next_version}\n\n"
                    f"{published_branch} just published {published_version}; "
                    f"{policy.default_branch} was still at {current_default}. "
                    "Automatic post-publish advancement -- see "
                    "litai release advance-default-branch for a deliberate "
                    "minor/major bump instead of this default patch step.",
                )
                _git_in(
                    worktree,
                    "push",
                    policy.remote,
                    f"HEAD:refs/heads/{policy.default_branch}",
                )
                pushed_revision = _git_in(worktree, "rev-parse", "HEAD").stdout.strip()
                return {
                    "branch": policy.default_branch,
                    "action": "advanced",
                    "previous_version": current_default,
                    "next_version": next_version,
                    "revision": pushed_revision,
                }
            finally:
                _git(root, "worktree", "remove", "--force", str(worktree), check=False)
    except ProjectReleaseError as exc:
        return {
            "branch": policy.default_branch,
            "action": "failed",
            "reason": exc.message,
        }


def _require_authenticated_release_receipt(root: Path, policy: ReleasePolicy):
    if policy.authenticated_receipt is None:
        return None
    from literate_ai.adapters.release_evidence import verify_release_current_evidence

    try:
        result = verify_release_current_evidence(root, policy.authenticated_receipt)
        _, fresh = load_release_policy(root)
        if fresh.identity != policy.identity:
            raise ValueError("release policy changed during evidence verification")
        return result
    except (ValueError, TypeError, OSError, RecursionError) as exc:
        raise ProjectReleaseError(
            "release.evidence_invalid",
            "current authenticated receipt verification failed",
        ) from exc


def publish_release(
    project: Path,
    prepared_path: Path,
    *,
    authorize_external_write: bool,
    actor: str | None = None,
    break_glass_reason: str | None = None,
) -> dict[str, object]:
    if not authorize_external_write:
        raise ProjectReleaseError(
            "release.external_authorization_required",
            "publish requires --authorize-external-write",
        )
    prepared = _load_record(
        prepared_path, (PREPARED_RELEASE_SCHEMA, LEGACY_PREPARED_RELEASE_SCHEMA)
    )
    root, policy = load_release_policy(project)
    authorization: dict[str, str | None] | None = None
    if policy.schema == RELEASE_POLICY_SCHEMA:
        _require_pre_release_target(root, policy, str(prepared["version"]))
        prepared_authorization = prepared.get("authorization")
        allow_break_glass = (
            isinstance(prepared_authorization, dict)
            and prepared_authorization.get("mode") == "break-glass"
            and prepared.get("transition") == "patch"
        )
        authorization = _authorize(
            root,
            policy,
            actor=actor,
            operation="release publication",
            allow_break_glass=allow_break_glass,
            break_glass_reason=break_glass_reason,
        )
        if allow_break_glass and (
            prepared_authorization != authorization
            or not _RELEASE_LINE.fullmatch(str(prepared.get("branch", "")))
        ):
            raise ProjectReleaseError(
                "release.authorization_mismatch",
                "break-glass publication must match prepared patch authorization "
                "on an existing release line",
            )
    snapshot = _git_snapshot(root)
    if (
        not snapshot["clean"]
        or snapshot["head"] != prepared["prepared_revision"]
        or snapshot["branch"] != prepared["branch"]
    ):
        raise ProjectReleaseError(
            "release.prepared_stale", "working tree or revision differs from check"
        )
    if policy.identity != prepared["policy_identity"]:
        raise ProjectReleaseError(
            "release.prepared_stale", "release policy differs from check"
        )
    authenticated_receipt = _require_authenticated_release_receipt(root, policy)
    if authenticated_receipt != prepared.get("authenticated_receipt_identity"):
        raise ProjectReleaseError(
            "release.evidence_changed",
            "authenticated current receipt differs from check",
        )
    artifacts = _qualified_release_files(root, policy, prepared)
    contributions = _revalidate_release_closure(
        root,
        policy,
        prepared,
        revision=str(snapshot["head"]),
        require_default_branch_ancestry=True,
    )
    _require_release_line(
        policy,
        branch=str(snapshot["branch"]),
        version=str(prepared["version"]),
    )
    tag = str(prepared["tag"])
    if current_release_version(root, policy) != prepared["version"]:
        raise ProjectReleaseError(
            "release.prepared_stale", "release version differs from checked evidence"
        )
    expected_revision = str(prepared["prepared_revision"])
    remote_branch = _remote_branch_revision(
        root, policy.remote, str(snapshot["branch"])
    )
    if remote_branch is not None and remote_branch not in {
        str(prepared["base_revision"]),
        expected_revision,
    }:
        raise ProjectReleaseError(
            "release.remote_branch_conflict",
            "remote branch changed outside this prepared release; fetch and re-plan",
        )
    remote_tag, remote_annotated = _remote_tag_revision(root, policy.remote, tag)
    if remote_tag is not None and (
        remote_tag != expected_revision or not remote_annotated
    ):
        raise ProjectReleaseError(
            "release.remote_tag_conflict", f"remote tag is incompatible: {tag}"
        )
    local_revision = _local_tag_revision(root, tag)
    if _require_authenticated_release_receipt(root, policy) != authenticated_receipt:
        raise ProjectReleaseError(
            "release.evidence_changed", "authenticated receipt changed before tagging"
        )
    if local_revision is None:
        _git(
            root,
            "tag",
            "-s" if policy.signed_tag else "-a",
            tag,
            "-m",
            f"Release {tag}",
        )
    elif local_revision != expected_revision:
        raise ProjectReleaseError(
            "release.local_tag_conflict", f"local tag selects another revision: {tag}"
        )
    _require_local_tag_shape(root, tag, signed=policy.signed_tag)
    if _require_authenticated_release_receipt(root, policy) != authenticated_receipt:
        raise ProjectReleaseError(
            "release.evidence_changed", "authenticated receipt changed before push"
        )
    try:
        if remote_branch != expected_revision:
            _git(root, "push", policy.remote, f"HEAD:refs/heads/{snapshot['branch']}")
        if remote_tag is None:
            _git(root, "push", policy.remote, f"refs/tags/{tag}")
    except ProjectReleaseError as exc:
        raise ProjectReleaseError(
            "release.partial_git_publication",
            "publication may be partial; inspect the remote branch and tag "
            "before retrying",
        ) from exc
    provider_url: str | None = None
    provider_release: dict[str, object] | None = None
    if policy.provider_kind == "github":
        assert policy.provider_repository is not None
        observed_release = _github_release_evidence(
            root, policy.provider_repository, tag
        )
        provider_url = (
            None if observed_release is None else str(observed_release["url"])
        )
        if observed_release is not None:
            _require_stable_github_release(
                root,
                policy.provider_repository,
                tag,
                version=str(prepared["version"]),
                require_wheel=False,
            )
        changelog = _binding_path(root, policy.changelog_path)
        release_notes = _changelog_release_notes(
            changelog.read_text(encoding="utf-8"), str(prepared["version"])
        )
        try:
            completed = None
            if provider_url is None:
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".md", delete=False
                ) as notes_file:
                    notes_file.write(release_notes)
                    notes_path = notes_file.name
                try:
                    completed = subprocess.run(
                        (
                            "gh",
                            "release",
                            "create",
                            tag,
                            "--repo",
                            policy.provider_repository,
                            "--title",
                            tag,
                            "--notes-file",
                            notes_path,
                            "--verify-tag",
                        ),
                        cwd=root,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                finally:
                    os.unlink(notes_path)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectReleaseError(
                "release.provider_unavailable",
                "GitHub publication requires an authenticated gh command",
            ) from exc
        if completed is not None and completed.returncode:
            raise ProjectReleaseError(
                "release.partial_provider_publication",
                "Git tag was pushed but GitHub release publication failed; "
                "authenticate gh and retry after inspection",
            )
        if completed is not None:
            provider_url = completed.stdout.strip() or None
    # Build and upload wheel after the GitHub Release exists
    wheel_path: Path | None = None
    if policy.provider_kind == "github" and policy.provider_repository:
        if artifacts is not None:
            # Recheck immediately before publication, after any provider calls.
            _qualified_release_files(root, policy, prepared)
            for item in artifacts["files"]:
                path = root / item["path"]
                _upload_wheel_asset(root, policy.provider_repository, tag, path)
                if item["role"] == "wheel":
                    wheel_path = path
            _verify_release_file_assets(
                root, policy.provider_repository, tag, artifacts
            )
        else:
            wheel_path = _build_wheel(root)
            if wheel_path is not None:
                _upload_wheel_asset(root, policy.provider_repository, tag, wheel_path)
        provider_release = _require_stable_github_release(
            root,
            policy.provider_repository,
            tag,
            version=str(prepared["version"]),
            require_wheel=wheel_path is not None,
        )
        provider_url = str(provider_release["url"])
    result: dict[str, object] = {
        "schema": PUBLICATION_RECEIPT_SCHEMA,
        "prepared_release_identity": prepared["identity"],
        "revision": prepared["prepared_revision"],
        "version": prepared["version"],
        "tag": tag,
        "remote": policy.remote,
        "provider": policy.provider_kind,
        "provider_url": provider_url,
        "provider_release": provider_release,
        "default_branch_advance": None,
        "wheel": str(wheel_path) if wheel_path is not None else None,
        "release_class": prepared.get("release_class"),
        "stable_predecessor": prepared.get("stable_predecessor"),
        "collateral": prepared.get("collateral", []),
    }
    if contributions is not None:
        result["contributions"] = contributions
    if authorization is not None:
        result["authorization"] = authorization
    result["identity"] = canonical_identity(result).uri
    return result


def verify_published_release(project: Path, prepared_path: Path) -> dict[str, object]:
    """Prove remote tag, release line, and optional provider match one identity.

    Read-only. Never creates, moves, deletes, or force-pushes a tag.
    """

    prepared = _load_record(
        prepared_path, (PREPARED_RELEASE_SCHEMA, LEGACY_PREPARED_RELEASE_SCHEMA)
    )
    root, policy = load_release_policy(project)
    if policy.identity != prepared["policy_identity"]:
        raise ProjectReleaseError(
            "release.prepared_stale", "release policy differs from check"
        )
    artifacts = _qualified_release_files(root, policy, prepared, check_bytes=False)
    tag = str(prepared["tag"])
    expected = str(prepared["prepared_revision"])
    contributions = _revalidate_release_closure(
        root,
        policy,
        prepared,
        revision=expected,
        require_default_branch_ancestry=False,
    )
    branch = str(prepared["branch"])
    remote_tag, remote_annotated = _remote_tag_revision(root, policy.remote, tag)
    if remote_tag is None:
        raise ProjectReleaseError(
            "release.published_tag_missing",
            f"remote {policy.remote!r} has no tag {tag}",
        )
    if remote_tag != expected or not remote_annotated:
        raise ProjectReleaseError(
            "release.published_tag_mismatch",
            f"remote tag {tag} does not select annotated prepared revision {expected}",
        )
    remote_branch = _remote_branch_revision(root, policy.remote, branch)
    if remote_branch != expected:
        raise ProjectReleaseError(
            "release.published_branch_mismatch",
            f"remote branch {branch} does not select prepared revision {expected}",
        )
    provider_url: str | None = None
    provider_release: dict[str, object] | None = None
    if policy.provider_kind == "github":
        assert policy.provider_repository is not None
        provider_release = _require_stable_github_release(
            root,
            policy.provider_repository,
            tag,
            version=str(prepared["version"]),
            require_wheel=_expected_wheel_prefix(root, str(prepared["version"]))
            is not None,
        )
        provider_url = str(provider_release["url"])
        if artifacts is not None:
            _verify_release_file_assets(
                root, policy.provider_repository, tag, artifacts
            )
    result: dict[str, object] = {
        "schema": PUBLISHED_VERIFICATION_SCHEMA,
        "prepared_release_identity": prepared["identity"],
        "revision": expected,
        "version": prepared["version"],
        "tag": tag,
        "branch": branch,
        "remote": policy.remote,
        "remote_tag_revision": remote_tag,
        "remote_tag_annotated": remote_annotated,
        "remote_branch_revision": remote_branch,
        "provider": policy.provider_kind,
        "provider_url": provider_url,
        "provider_release": provider_release,
        "release_class": prepared.get("release_class"),
        "stable_predecessor": prepared.get("stable_predecessor"),
        "collateral": prepared.get("collateral", []),
    }
    if contributions is not None:
        result["contributions"] = contributions
    result["identity"] = canonical_identity(result).uri
    return result


__all__ = [
    "PREPARED_RELEASE_SCHEMA",
    "LEGACY_PREPARED_RELEASE_SCHEMA",
    "PUBLICATION_RECEIPT_SCHEMA",
    "PUBLISHED_VERIFICATION_SCHEMA",
    "RELEASE_BACKPORT_SCHEMA",
    "RELEASE_BACKPORT_STATUS_SCHEMA",
    "RELEASE_PLAN_SCHEMA",
    "RELEASE_POLICY_FILE",
    "RELEASE_POLICY_SCHEMA",
    "RELEASE_TARGETS",
    "WORKERS_CATALOG_FILE",
    "ProjectReleaseError",
    "ReleasePolicy",
    "VersionBinding",
    "advance_default_branch_version",
    "backport_commits",
    "backport_status",
    "check_release",
    "create_release_candidate",
    "create_release_plan",
    "current_release_version",
    "load_release_policy",
    "merge_release_pull_request",
    "prepare_release",
    "publish_release",
    "release_state",
    "set_release_state",
    "verify_published_release",
]
