"""Tiered release qualification: cover N platforms x M actions left to right.

A release declares the platforms it must provide and the actions (code
generation, build, test and other lifecycle gates) that must pass on each.
Every platform x action cell is covered by the leftmost tier that can run it:

1. ``local``: the host running the coding CLI covers its own platform;
2. ``worker``: one configured ``workers.json`` worker per remaining platform;
3. ``ci``: an exact-revision CI run covers its declared platforms.

CI is required only when a cell is left that no left tier can cover, or when
the policy marks CI mandatory. A failing action is never retried on another
tier: failure is evidence, and only an unavailable runner falls through.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from literate_ai.contracts import canonical_identity

QUALIFICATION_RECORD_SCHEMA = "literate-ai/release-qualification@1"
TIERS = ("local", "worker", "ci")
_OS_FAMILIES = ("linux", "macos", "windows")
_PLATFORM = re.compile(r"^(linux|macos|windows)(?:-([a-z0-9_]{1,32}))?$")
_ACTION = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class ReleaseQualificationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _fail(path: str, message: str) -> None:
    raise ReleaseQualificationError(
        "release.qualification_invalid", f"{path}: {message}"
    )


def _platforms(value: object, path: str, *, minimum: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) < minimum
        or len(value) > 64
        or any(
            not isinstance(item, str) or not _PLATFORM.fullmatch(item) for item in value
        )
        or len(set(value)) != len(value)
    ):
        _fail(
            path,
            "platforms must be unique os_family or os_family-cpu_architecture "
            "values (linux, macos, windows)",
        )
    return tuple(value)


@dataclass(frozen=True, slots=True)
class QualificationAction:
    """One required action; ``argv`` runs on Linux and macOS.

    Windows runners have no POSIX shell, so an action runs there only through
    its own ``windows_argv``; without one, Windows is left to CI.
    """

    name: str
    argv: tuple[str, ...]
    windows_argv: tuple[str, ...] | None = None

    def argv_for(self, os_family: str | None) -> tuple[str, ...] | None:
        if os_family == "windows":
            return self.windows_argv
        return self.argv if os_family in {"linux", "macos"} else None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            **(
                {"windows_argv": list(self.windows_argv)}
                if self.windows_argv is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class ReleaseQualificationPolicy:
    """The release's required platforms (N) and actions (M), and its CI rule."""

    platforms: tuple[str, ...]
    actions: tuple[QualificationAction, ...]
    ci_mandatory: bool
    ci_platforms: tuple[str, ...]

    @classmethod
    def from_dict(
        cls, value: object, *, gate_argv: Sequence[str]
    ) -> ReleaseQualificationPolicy:
        if not isinstance(value, dict) or not {"platforms"} <= set(value) <= {
            "platforms",
            "actions",
            "ci",
        }:
            _fail("qualification", "requires platforms, optional actions and ci")
        platforms = _platforms(value["platforms"], "qualification.platforms", minimum=1)
        raw_actions = value.get("actions")
        if raw_actions is None:
            # The declared release gate is the single action by default.
            actions = (QualificationAction("release-gate", tuple(gate_argv)),)
        else:
            if not isinstance(raw_actions, list) or not 1 <= len(raw_actions) <= 64:
                _fail("qualification.actions", "must list 1 to 64 actions")
            parsed = []
            for index, item in enumerate(raw_actions):
                path = f"qualification.actions[{index}]"
                if not isinstance(item, dict) or not {"name", "argv"} <= set(item) <= {
                    "name",
                    "argv",
                    "windows_argv",
                }:
                    _fail(path, "requires name and argv, and optional windows_argv")
                if not isinstance(item["name"], str) or not _ACTION.fullmatch(
                    item["name"]
                ):
                    _fail(f"{path}.name", "must be a lowercase action name")
                vectors = {}
                for key in ("argv", "windows_argv"):
                    argv = item.get(key)
                    if argv is None and key == "windows_argv":
                        continue
                    if (
                        not isinstance(argv, list)
                        or not argv
                        or any(not isinstance(part, str) or not part for part in argv)
                    ):
                        _fail(f"{path}.{key}", "must be a non-empty argument vector")
                    vectors[key] = tuple(argv)
                parsed.append(
                    QualificationAction(
                        item["name"], vectors["argv"], vectors.get("windows_argv")
                    )
                )
            if len({item.name for item in parsed}) != len(parsed):
                _fail("qualification.actions", "action names must be unique")
            actions = tuple(parsed)
        ci = value.get("ci", {})
        if not isinstance(ci, dict) or not set(ci) <= {"mandatory", "platforms"}:
            _fail("qualification.ci", "allows only mandatory and platforms")
        mandatory = ci.get("mandatory", False)
        if type(mandatory) is not bool:
            _fail("qualification.ci.mandatory", "must be a boolean")
        ci_platforms = _platforms(
            ci.get("platforms", []), "qualification.ci.platforms", minimum=0
        )
        if mandatory and not ci_platforms:
            _fail("qualification.ci.platforms", "mandatory CI must cover a platform")
        return cls(platforms, actions, mandatory, ci_platforms)

    def to_dict(self) -> dict[str, object]:
        return {
            "platforms": list(self.platforms),
            "actions": [item.to_dict() for item in self.actions],
            "ci": {
                "mandatory": self.ci_mandatory,
                "platforms": list(self.ci_platforms),
            },
        }

    @property
    def identity(self) -> str:
        return canonical_identity(
            {"schema": "literate-ai/release-qualification-policy@1", **self.to_dict()}
        ).uri


def satisfies(
    platform: str, os_family: str | None, cpu_architecture: str | None
) -> bool:
    """Whether a runner with these proven facts provides one required platform.

    A platform naming an architecture is satisfied only by a runner whose
    architecture is known; an unknown architecture never proves one.
    """

    match = _PLATFORM.fullmatch(platform)
    if match is None or os_family not in _OS_FAMILIES:
        return False
    required_os, required_architecture = match.groups()
    return required_os == os_family and (
        required_architecture is None or required_architecture == cpu_architecture
    )


@dataclass(frozen=True, slots=True)
class QualificationRunner:
    """A worker that may cover a platform, in deterministic preference order."""

    worker_id: str
    os_family: str | None
    cpu_architecture: str | None


@dataclass(frozen=True, slots=True)
class QualificationPlan:
    local: tuple[str, ...]
    workers: Mapping[str, tuple[str, ...]]
    ci: tuple[str, ...]
    ci_required: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "local": list(self.local),
            "workers": {
                key: list(value) for key, value in sorted(self.workers.items())
            },
            "ci": list(self.ci),
            "ci_required": self.ci_required,
        }


def plan_qualification(
    policy: ReleaseQualificationPolicy,
    *,
    host: tuple[str | None, str | None] | None,
    workers: Sequence[QualificationRunner],
    unavailable_workers: frozenset[str] = frozenset(),
) -> QualificationPlan:
    """Assign every platform to its leftmost available tier.

    ``workers`` maps each still-uncovered platform to every eligible worker in
    order, so an unreachable first choice can fall through to the next. CI is
    required when a platform has no left tier or the policy mandates CI, and
    planning fails closed when CI cannot cover what remains.
    """

    def runnable(os_family: str | None) -> bool:
        return all(action.argv_for(os_family) for action in policy.actions)

    local = tuple(
        platform
        for platform in policy.platforms
        if host is not None and runnable(host[0]) and satisfies(platform, *host)
    )
    candidates = {}
    for platform in policy.platforms:
        if platform in local:
            continue
        eligible = tuple(
            runner.worker_id
            for runner in sorted(workers, key=lambda item: item.worker_id)
            if runner.worker_id not in unavailable_workers
            and runnable(runner.os_family)
            and satisfies(platform, runner.os_family, runner.cpu_architecture)
        )
        if eligible:
            candidates[platform] = eligible
    remaining = tuple(
        platform
        for platform in policy.platforms
        if platform not in local and platform not in candidates
    )
    uncoverable = [item for item in remaining if item not in policy.ci_platforms]
    if uncoverable:
        raise ReleaseQualificationError(
            "release.qualification_incomplete",
            "no local host, configured worker or declared CI platform can provide "
            + ", ".join(uncoverable),
        )
    ci_required = bool(remaining) or policy.ci_mandatory
    return QualificationPlan(
        local,
        candidates,
        remaining if not policy.ci_mandatory else policy.ci_platforms,
        ci_required,
    )


def coverage_cells(
    policy: ReleaseQualificationPolicy,
    covered: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    """Return one cell per platform x action, failing on any uncovered cell."""

    cells = []
    for platform in policy.platforms:
        source = covered.get(platform)
        if source is None or source.get("tier") not in TIERS:
            raise ReleaseQualificationError(
                "release.qualification_incomplete",
                f"platform {platform!r} has no passing qualification",
            )
        for action in policy.actions:
            cells.append({"platform": platform, "action": action.name, **source})
    return cells


def require_qualification_record(
    record: object,
    policy: ReleaseQualificationPolicy,
    *,
    revision: str,
) -> dict[str, object]:
    """Accept a recorded qualification only for this policy and exact revision."""

    if (
        not isinstance(record, dict)
        or record.get("schema") != QUALIFICATION_RECORD_SCHEMA
        or record.get("revision") != revision
        or record.get("policy_identity") != policy.identity
    ):
        raise ReleaseQualificationError(
            "release.qualification_stale",
            "qualification record is not for this policy and exact revision",
        )
    identity = record.get("identity")
    body = {key: value for key, value in record.items() if key != "identity"}
    if identity != canonical_identity(body).uri:
        raise ReleaseQualificationError(
            "release.qualification_invalid", "qualification record identity mismatch"
        )
    cells = record.get("coverage")
    expected = {
        (platform, action.name)
        for platform in policy.platforms
        for action in policy.actions
    }
    if (
        not isinstance(cells, list)
        or {
            (item.get("platform"), item.get("action"))
            for item in cells
            if isinstance(item, dict) and item.get("tier") in TIERS
        }
        != expected
        or len(cells) != len(expected)
    ):
        raise ReleaseQualificationError(
            "release.qualification_incomplete",
            "qualification record does not cover every platform and action",
        )
    if policy.ci_mandatory and record.get("ci") is None:
        raise ReleaseQualificationError(
            "release.qualification_incomplete",
            "the release policy makes CI mandatory",
        )
    return record
