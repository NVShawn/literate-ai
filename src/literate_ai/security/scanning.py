"""Deterministic source-module scanning and authorization revocation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, ClassVar

from .policy import (
    AUTHORIZATION_REVOCATION_SET_SCHEMA,
    SECURITY_SCAN_REPORT_SCHEMA,
    AuthorizationError,
    BuildAuthorization,
    BuildRequest,
    FindingSeverity,
    ObservationExecutionAuthorization,
    ObservationRequest,
    SecurityFinding,
    _exact_fields,
    _require_digest,
    _require_text,
    _require_unique_digests,
    _string,
    _tuple,
    normalize_security_document,
)


def _digest(value: object) -> str:
    data = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


@dataclass(frozen=True, slots=True)
class SourceModule:
    path: str
    source_digest: str
    content: bytes

    def __post_init__(self) -> None:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("source module path must be normalized and relative")
        actual = f"sha256:{hashlib.sha256(self.content).hexdigest()}"
        if self.source_digest != actual:
            raise ValueError("source module digest does not match its content")

    @classmethod
    def create(cls, path: str, content: bytes) -> SourceModule:
        return cls(path, f"sha256:{hashlib.sha256(content).hexdigest()}", content)


@dataclass(frozen=True, slots=True)
class ScanRule:
    rule_id: str
    category: str
    severity: FindingSeverity
    pattern: str
    message: str
    suffixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.rule_id or not self.category or not self.message:
            raise ValueError("scan rule identity and message cannot be empty")
        re.compile(self.pattern)


@dataclass(frozen=True, slots=True)
class SecurityScanReport:
    SCHEMA: ClassVar[str] = SECURITY_SCAN_REPORT_SCHEMA

    scanner_identity: str
    module_digests: tuple[str, ...]
    rule_set_digest: str
    findings: tuple[SecurityFinding, ...]

    def __post_init__(self) -> None:
        _require_text(self.scanner_identity, "scanner_identity")
        _require_unique_digests(self.module_digests, "module_digests")
        _require_digest(self.rule_set_digest, "rule_set_digest")
        if not isinstance(self.findings, tuple):
            raise ValueError("findings must be a tuple")
        finding_ids = tuple(item.finding_id for item in self.findings)
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("findings must have unique identities")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "scanner_identity": self.scanner_identity,
            "module_digests": list(self.module_digests),
            "rule_set_digest": self.rule_set_digest,
            "findings": [item.to_dict() for item in self.findings],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SecurityScanReport:
        value = normalize_security_document(value, expected_schema=cls.SCHEMA)
        _exact_fields(
            value,
            frozenset(
                {
                    "schema",
                    "scanner_identity",
                    "module_digests",
                    "rule_set_digest",
                    "findings",
                }
            ),
            "security scan report",
        )
        findings = _tuple(value["findings"], "findings")
        if any(not isinstance(item, Mapping) for item in findings):
            raise ValueError("findings must be objects")
        return cls(
            scanner_identity=_string(value["scanner_identity"], "scanner_identity"),
            module_digests=tuple(
                _string(item, "module_digests")
                for item in _tuple(value["module_digests"], "module_digests")
            ),
            rule_set_digest=_string(value["rule_set_digest"], "rule_set_digest"),
            findings=tuple(SecurityFinding.from_dict(item) for item in findings),
        )


class RuleBasedSourceScanner:
    """Scan text as inert bytes; source is never imported or executed."""

    def __init__(self, scanner_identity: str, rules: tuple[ScanRule, ...]) -> None:
        if not scanner_identity or not rules:
            raise ValueError("scanner requires identity and at least one rule")
        if len({rule.rule_id for rule in rules}) != len(rules):
            raise ValueError("scanner rule IDs must be unique")
        self.scanner_identity = scanner_identity
        self.rules = rules
        self.rule_set_digest = _digest([asdict(rule) for rule in rules])

    def scan(self, modules: tuple[SourceModule, ...]) -> SecurityScanReport:
        findings: list[SecurityFinding] = []
        for module in sorted(modules, key=lambda item: item.path):
            text = module.content.decode("utf-8", errors="replace")
            suffix = PurePosixPath(module.path).suffix.lower()
            for rule in self.rules:
                if rule.suffixes and suffix not in rule.suffixes:
                    continue
                for match_index, _match in enumerate(
                    re.finditer(rule.pattern, text, re.MULTILINE)
                ):
                    identity = _digest(
                        {
                            "scanner": self.scanner_identity,
                            "rules": self.rule_set_digest,
                            "module": module.source_digest,
                            "rule": rule.rule_id,
                            "match": match_index,
                        }
                    )
                    findings.append(
                        SecurityFinding(
                            finding_id=f"finding:{identity.removeprefix('sha256:')[:32]}",
                            source_digest=module.source_digest,
                            category=rule.category,
                            severity=rule.severity,
                            scanner_identity=self.scanner_identity,
                            message=f"{module.path}: {rule.message}",
                        )
                    )
        return SecurityScanReport(
            scanner_identity=self.scanner_identity,
            module_digests=tuple(sorted({module.source_digest for module in modules})),
            rule_set_digest=self.rule_set_digest,
            findings=tuple(findings),
        )


def baseline_python_rules() -> tuple[ScanRule, ...]:
    """Conservative starter policy; consumers may supply richer scanners."""

    return (
        ScanRule(
            "python.dynamic-eval",
            "dynamic-code-execution",
            FindingSeverity.HIGH,
            r"\b(?:eval|exec)\s*\(",
            "dynamic code execution requires review",
            (".py",),
        ),
        ScanRule(
            "python.shell-process",
            "process-execution",
            FindingSeverity.HIGH,
            r"\b(?:os\.system|subprocess\.(?:Popen|run|call))\s*\(",
            "process execution requires review",
            (".py",),
        ),
        ScanRule(
            "python.unsafe-deserialization",
            "unsafe-deserialization",
            FindingSeverity.CRITICAL,
            r"\b(?:pickle\.loads?|yaml\.load)\s*\(",
            "unsafe deserialization is blocked by the baseline policy",
            (".py",),
        ),
    )


def baseline_cpp_rules() -> tuple[ScanRule, ...]:
    """Conservative process and unsafe-memory rules for generated C++ source."""

    suffixes = (".cpp", ".cc", ".cu", ".cxx", ".h", ".hh", ".hpp", ".hxx")
    return (
        ScanRule(
            "cpp.shell-process",
            "process-execution",
            FindingSeverity.HIGH,
            r"\b(?:std::)?system\s*\(|\b(?:popen|_popen)\s*\(",
            "shell process execution requires review",
            suffixes,
        ),
        ScanRule(
            "cpp.unsafe-copy",
            "unsafe-memory-operation",
            FindingSeverity.HIGH,
            r"\b(?:strcpy|strcat|gets)\s*\(",
            "unbounded memory operation requires review",
            suffixes,
        ),
    )


def baseline_rust_rules() -> tuple[ScanRule, ...]:
    """Conservative process and unsafe-code rules for generated Rust source."""

    return (
        ScanRule(
            "rust.process-command",
            "process-execution",
            FindingSeverity.HIGH,
            r"\b(?:std::process::)?Command\s*::\s*new\s*\(",
            "process execution requires review",
            (".rs",),
        ),
        ScanRule(
            "rust.unsafe-block",
            "unsafe-memory-operation",
            FindingSeverity.HIGH,
            r"\bunsafe\s*\{",
            "unsafe Rust requires review",
            (".rs",),
        ),
    )


def baseline_javascript_rules() -> tuple[ScanRule, ...]:
    """Conservative dynamic-code and process rules for generated JavaScript."""

    suffixes = (".cjs", ".js", ".mjs")
    return (
        ScanRule(
            "javascript.dynamic-eval",
            "dynamic-code-execution",
            FindingSeverity.HIGH,
            r"\b(?:eval|Function)\s*\(",
            "dynamic code execution requires review",
            suffixes,
        ),
        ScanRule(
            "javascript.child-process",
            "process-execution",
            FindingSeverity.HIGH,
            r"(?:node:)?child_process|\b(?:spawn|spawnSync|exec|execFile)\s*\(",
            "child process execution requires review",
            suffixes,
        ),
    )


def baseline_swift_rules() -> tuple[ScanRule, ...]:
    """Conservative process and unsafe-interop rules for generated Swift."""

    return (
        ScanRule(
            "swift.process",
            "process-execution",
            FindingSeverity.HIGH,
            r"\bProcess\s*\(",
            "process execution requires review",
            (".swift",),
        ),
        ScanRule(
            "swift.unsafe-bitcast",
            "unsafe-memory-operation",
            FindingSeverity.HIGH,
            r"\bunsafeBitCast\s*\(",
            "unsafe memory reinterpretation requires review",
            (".swift",),
        ),
    )


@dataclass(frozen=True, slots=True)
class AuthorizationRevocation:
    authorization_id: str
    actor: str
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.authorization_id, "authorization_id")
        _require_text(self.actor, "actor")
        _require_text(self.reason, "reason")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AuthorizationRevocation:
        _exact_fields(
            value,
            frozenset({"authorization_id", "actor", "reason"}),
            "authorization revocation",
        )
        return cls(
            authorization_id=_string(value["authorization_id"], "authorization_id"),
            actor=_string(value["actor"], "actor"),
            reason=_string(value["reason"], "reason"),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationRevocationSet:
    SCHEMA: ClassVar[str] = AUTHORIZATION_REVOCATION_SET_SCHEMA

    entries: tuple[AuthorizationRevocation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise ValueError("revocation entries must be a tuple")
        if len({entry.authorization_id for entry in self.entries}) != len(self.entries):
            raise ValueError("an authorization can only be revoked once")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "entries": [item.to_dict() for item in self.entries],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AuthorizationRevocationSet:
        value = normalize_security_document(value, expected_schema=cls.SCHEMA)
        _exact_fields(
            value, frozenset({"schema", "entries"}), "authorization revocation set"
        )
        entries = _tuple(value["entries"], "entries")
        if any(not isinstance(item, Mapping) for item in entries):
            raise ValueError("revocation entries must be objects")
        return cls(tuple(AuthorizationRevocation.from_dict(item) for item in entries))

    def revoke(
        self, authorization_id: str, *, actor: str, reason: str
    ) -> AuthorizationRevocationSet:
        if any(item.authorization_id == authorization_id for item in self.entries):
            return self
        return AuthorizationRevocationSet(
            self.entries + (AuthorizationRevocation(authorization_id, actor, reason),)
        )

    def require_build_valid(
        self,
        authorization: BuildAuthorization,
        request: BuildRequest,
        *,
        now: datetime,
    ) -> None:
        if any(
            item.authorization_id == authorization.authorization_id
            for item in self.entries
        ):
            raise AuthorizationError("security.authorization_revoked")
        authorization.require_valid(request, now=now)

    def require_observation_valid(
        self,
        authorization: ObservationExecutionAuthorization,
        request: ObservationRequest,
        *,
        now: datetime,
    ) -> None:
        if any(
            item.authorization_id == authorization.authorization_id
            for item in self.entries
        ):
            raise AuthorizationError("security.observation_authorization_revoked")
        authorization.require_valid(request, now=now)


__all__ = [
    "AuthorizationRevocation",
    "AuthorizationRevocationSet",
    "RuleBasedSourceScanner",
    "ScanRule",
    "SecurityScanReport",
    "SourceModule",
    "baseline_cpp_rules",
    "baseline_javascript_rules",
    "baseline_python_rules",
    "baseline_rust_rules",
    "baseline_swift_rules",
]
