"""Live GitHub OIDC identity checks; no key discovery or receipt authorization."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from literate_ai.contracts._validation import int_value
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

from .checks import check_run_assertion
from .dsse import MAX_ENVELOPE_BYTES, DsseEnvelope
from .records import DSSE_MEDIA_TYPE, EvidenceArtifact
from .statements import AuthenticatedEvidenceStatement, verify_evidence_statement
from .storage import verify_evidence_bytes
from .trust import RunEvidenceExpectation

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_MAX_TOKEN = 32 * 1024
_MAX_JWKS = 64 * 1024
_REQUIRED_SCOPE = frozenset(
    {
        "sub",
        "repository",
        "repository_id",
        "repository_owner_id",
        "workflow_ref",
        "workflow_sha",
        "event_name",
        "ref",
        "runner_environment",
    }
)
_OPTIONAL_SCOPE = frozenset({"environment", "job_workflow_ref", "job_workflow_sha"})


class EvidenceOidcError(ValueError):
    """Sanitized errors never expose bearer tokens, claims or key documents."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value, maximum=2048):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise EvidenceOidcError("evidence.oidc.configuration-invalid")
    return value


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise EvidenceOidcError("evidence.oidc.configuration-invalid")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceOidcError("evidence.oidc.json-invalid")
        result[key] = value
    return result


def _nonfinite(_value):
    raise EvidenceOidcError("evidence.oidc.json-invalid")


def _json(content):
    try:
        result = json.loads(
            content, object_pairs_hook=_object, parse_constant=_nonfinite
        )
    except (ValueError, UnicodeError, RecursionError):
        raise EvidenceOidcError("evidence.oidc.json-invalid") from None
    if not isinstance(result, dict):
        raise EvidenceOidcError("evidence.oidc.json-invalid")
    # JSON parser recursion limits vary across supported interpreters. Enforce the
    # profile's own bound before any JWT/JWK library consumes the decoded object.
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise EvidenceOidcError("evidence.oidc.json-invalid")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return result


def _base64(value):
    if (
        not isinstance(value, bytes)
        or not value
        or not re.fullmatch(rb"[A-Za-z0-9_-]+", value)
    ):
        raise EvidenceOidcError("evidence.oidc.encoding-invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + b"=" * (-len(value) % 4))
    except ValueError:
        raise EvidenceOidcError("evidence.oidc.encoding-invalid") from None
    if base64.urlsafe_b64encode(decoded).rstrip(b"=") != value:
        raise EvidenceOidcError("evidence.oidc.encoding-invalid")
    return decoded


@dataclass(frozen=True, slots=True)
class GitHubEvidenceIdentityPolicy:
    """Independent exact provider scope; no branch, event or subject wildcards."""

    claims: tuple[tuple[str, str], ...]
    workflow: ContentIdentity
    targets: tuple[ContentIdentity, ...]
    maximum_token_lifetime_seconds: int = 600
    maximum_token_age_seconds: int = 600
    maximum_key_set_age_seconds: int = 3600
    maximum_clock_skew_seconds: int = 0
    maximum_run_age_seconds: int = 600
    maximum_run_duration_seconds: int = 86400

    def __post_init__(self):
        if (
            not isinstance(self.claims, tuple)
            or not 9 <= len(self.claims) <= 12
            or any(
                not isinstance(item, tuple) or len(item) != 2 for item in self.claims
            )
        ):
            raise EvidenceOidcError("evidence.oidc.configuration-invalid")
        for key, value in self.claims:
            _text(key, 64)
            _text(value)
        names = tuple(key for key, _ in self.claims)
        if (
            names != tuple(sorted(set(names)))
            or not _REQUIRED_SCOPE <= set(names) <= _REQUIRED_SCOPE | _OPTIONAL_SCOPE
        ):
            raise EvidenceOidcError("evidence.oidc.configuration-invalid")
        values = dict(self.claims)
        for name in ("repository_id", "repository_owner_id"):
            _number(values[name])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", values["repository"]):
            raise EvidenceOidcError("evidence.oidc.configuration-invalid")
        if ("job_workflow_ref" in values) != ("job_workflow_sha" in values):
            raise EvidenceOidcError("evidence.oidc.configuration-invalid")
        for name in ("workflow_sha", "job_workflow_sha"):
            if name in values and not re.fullmatch(r"[0-9a-f]{40}", values[name]):
                raise EvidenceOidcError("evidence.oidc.configuration-invalid")
        if values["runner_environment"] not in ("github-hosted", "self-hosted"):
            raise EvidenceOidcError("evidence.oidc.configuration-invalid")
        if (
            not isinstance(self.workflow, ContentIdentity)
            or not isinstance(self.targets, tuple)
            or not 1 <= len(self.targets) <= 1024
            or any(not isinstance(item, ContentIdentity) for item in self.targets)
        ):
            raise EvidenceOidcError("evidence.oidc.configuration-invalid")
        targets = tuple(item.uri for item in self.targets)
        if targets != tuple(sorted(set(targets))):
            raise EvidenceOidcError("evidence.oidc.configuration-invalid")
        for value in (
            self.maximum_token_lifetime_seconds,
            self.maximum_token_age_seconds,
            self.maximum_key_set_age_seconds,
            self.maximum_run_age_seconds,
            self.maximum_run_duration_seconds,
        ):
            int_value(value, "evidence.oidc.interval", minimum=1, maximum=86400)
        int_value(
            self.maximum_clock_skew_seconds, "evidence.oidc.clock_skew", maximum=60
        )

    @property
    def identity(self):
        return canonical_identity(
            {
                "issuer": GITHUB_OIDC_ISSUER,
                "claims": dict(self.claims),
                "workflow": self.workflow.to_dict(),
                "targets": [t.to_dict() for t in self.targets],
                "maximum_token_lifetime_seconds": self.maximum_token_lifetime_seconds,
                "maximum_token_age_seconds": self.maximum_token_age_seconds,
                "maximum_key_set_age_seconds": self.maximum_key_set_age_seconds,
                "maximum_clock_skew_seconds": self.maximum_clock_skew_seconds,
                "maximum_run_age_seconds": self.maximum_run_age_seconds,
                "maximum_run_duration_seconds": self.maximum_run_duration_seconds,
            }
        )


@dataclass(frozen=True, slots=True)
class GitHubEvidenceIdentityRequest:
    evidence: BlobRef
    signer_public_key: bytes
    expectation: RunEvidenceExpectation
    run_id: str
    run_attempt: str
    check_run_id: str
    challenge: ContentIdentity

    def __post_init__(self):
        EvidenceArtifact("evidence", self.evidence)
        if (
            self.evidence.media_type != DSSE_MEDIA_TYPE
            or not isinstance(self.signer_public_key, bytes)
            or len(self.signer_public_key) != 32
            or not isinstance(self.expectation, RunEvidenceExpectation)
            or not isinstance(self.challenge, ContentIdentity)
        ):
            raise EvidenceOidcError("evidence.oidc.request-invalid")
        for value in (self.run_id, self.run_attempt, self.check_run_id):
            _number(value)

    @property
    def identity(self):
        return canonical_identity(
            {
                "evidence": self.evidence.to_dict(),
                "signer_public_key": self.signer_public_key.hex(),
                "expectation": self.expectation.to_dict(),
                "run_id": self.run_id,
                "run_attempt": self.run_attempt,
                "check_run_id": self.check_run_id,
                "challenge": self.challenge.to_dict(),
            }
        )

    def audience(self, policy):
        return (
            "urn:literate-ai:evidence-oidc:"
            + canonical_identity(
                {
                    "request": self.identity.to_dict(),
                    "policy": policy.identity.to_dict(),
                }
            ).digest
        )


@dataclass(frozen=True, slots=True)
class GitHubIssuerKeySet:
    """JWKS obtained through trusted issuer transport, never supplied by the token."""

    document: bytes = field(repr=False)
    fetched_at: int
    expires_at: int

    def __post_init__(self):
        if (
            not isinstance(self.document, bytes)
            or not 1 <= len(self.document) <= _MAX_JWKS
        ):
            raise EvidenceOidcError("evidence.oidc.keys-invalid")
        int_value(self.fetched_at, "evidence.oidc.keys_fetched_at")
        int_value(
            self.expires_at,
            "evidence.oidc.keys_expires_at",
            minimum=self.fetched_at + 1,
        )

    @property
    def identity(self):
        return canonical_identity(
            {
                "issuer": GITHUB_OIDC_ISSUER,
                "document": hashlib.sha256(self.document).hexdigest(),
                "fetched_at": self.fetched_at,
                "expires_at": self.expires_at,
            }
        )


def _issuer_key(key_set, kid):
    document = _json(key_set.document)
    keys = document.get("keys")
    if not isinstance(keys, list) or not 1 <= len(keys) <= 32:
        raise EvidenceOidcError("evidence.oidc.keys-invalid")
    indexed = {}
    for key in keys:
        if not isinstance(key, dict):
            raise EvidenceOidcError("evidence.oidc.keys-invalid")
        identifier = key.get("kid")
        _text(identifier, 256)
        if identifier in indexed:
            raise EvidenceOidcError("evidence.oidc.keys-ambiguous")
        indexed[identifier] = key
    key = indexed.get(kid)
    if key is None:
        raise EvidenceOidcError("evidence.oidc.key-unknown")
    if (
        key.get("kty") != "RSA"
        or key.get("alg", "RS256") != "RS256"
        or key.get("use", "sig") != "sig"
        or key.get("key_ops", ["verify"]) != ["verify"]
        or set(key) & {"d", "p", "q", "dp", "dq", "qi", "oth"}
    ):
        raise EvidenceOidcError("evidence.oidc.key-invalid")
    try:
        modulus = _base64(_text(key.get("n"), 1366).encode("ascii"))
        exponent = _base64(_text(key.get("e"), 6).encode("ascii"))
        if not 256 <= len(modulus) <= 1024 or not 1 <= len(exponent) <= 4:
            raise EvidenceOidcError("evidence.oidc.key-invalid")
        public_key = jwt.PyJWK.from_dict(key, algorithm="RS256").key
        if (
            not isinstance(public_key, RSAPublicKey)
            or not 2048 <= public_key.key_size <= 8192
        ):
            raise EvidenceOidcError("evidence.oidc.key-invalid")
    except (ValueError, UnicodeError, jwt.PyJWTError):
        raise EvidenceOidcError("evidence.oidc.key-invalid") from None
    return public_key


@dataclass(frozen=True, slots=True)
class AuthenticatedGitHubEvidenceIdentity:
    """Live identity binding only; possession, historical trust and admission follow."""

    request_identity: ContentIdentity
    policy_identity: ContentIdentity
    key_set_identity: ContentIdentity
    token_digest: ContentIdentity
    checked_at: int
    issued_at: int
    expires_at: int


def verify_github_evidence_identity(
    token: bytes,
    *,
    request: GitHubEvidenceIdentityRequest,
    policy: GitHubEvidenceIdentityPolicy,
    key_set: GitHubIssuerKeySet,
    now: int,
) -> AuthenticatedGitHubEvidenceIdentity:
    if (
        not isinstance(request, GitHubEvidenceIdentityRequest)
        or not isinstance(policy, GitHubEvidenceIdentityPolicy)
        or not isinstance(key_set, GitHubIssuerKeySet)
    ):
        raise EvidenceOidcError("evidence.oidc.configuration-invalid")
    int_value(now, "evidence.oidc.now")
    if (
        not key_set.fetched_at <= now < key_set.expires_at
        or now - key_set.fetched_at > policy.maximum_key_set_age_seconds
    ):
        raise EvidenceOidcError("evidence.oidc.keys-stale")
    scoped = dict(policy.claims)
    if (
        request.expectation.repository != "https://github.com/" + scoped["repository"]
        or request.expectation.workflow != policy.workflow
        or request.expectation.target not in policy.targets
    ):
        raise EvidenceOidcError("evidence.oidc.request-scope-mismatch")
    if not isinstance(token, bytes) or not 1 <= len(token) <= _MAX_TOKEN:
        raise EvidenceOidcError("evidence.oidc.token-invalid")
    parts = token.split(b".")
    if len(parts) != 3:
        raise EvidenceOidcError("evidence.oidc.token-invalid")
    header = _json(_base64(parts[0]))
    _json(
        _base64(parts[1])
    )  # Reject duplicate/deep/nonfinite JSON before library decode.
    signature = _base64(parts[2])
    if (
        set(header) - {"alg", "kid", "typ", "x5t"}
        or header.get("alg") != "RS256"
        or header.get("typ") != "JWT"
        or not 256 <= len(signature) <= 1024
    ):
        raise EvidenceOidcError("evidence.oidc.header-invalid")
    kid = _text(header.get("kid"), 256)
    key = _issuer_key(key_set, kid)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=GITHUB_OIDC_ISSUER,
            audience=request.audience(policy),
            options={
                "strict_aud": True,
                "verify_signature": True,
                "verify_iss": True,
                "verify_aud": True,
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
                "require": ["iss", "aud", "sub", "jti", "exp", "iat", "nbf"],
            },
        )
    except jwt.PyJWTError:
        raise EvidenceOidcError("evidence.oidc.token-invalid") from None
    for name in ("iat", "nbf", "exp"):
        if type(claims[name]) is not int or not 0 <= claims[name] <= 2**63 - 1:
            raise EvidenceOidcError("evidence.oidc.time-invalid")
    issued, starts, expires = (claims[name] for name in ("iat", "nbf", "exp"))
    if (
        not issued <= starts < expires
        or expires <= now
        or max(issued, starts) > now + policy.maximum_clock_skew_seconds
        or expires - issued > policy.maximum_token_lifetime_seconds
        or now - issued > policy.maximum_token_age_seconds
    ):
        raise EvidenceOidcError("evidence.oidc.time-invalid")
    expected = {
        **scoped,
        "sha": request.expectation.revision,
        "run_id": request.run_id,
        "run_attempt": request.run_attempt,
        "check_run_id": request.check_run_id,
    }
    if any(claims.get(name) != value for name, value in expected.items()) or any(
        name in claims and name not in scoped for name in _OPTIONAL_SCOPE
    ):
        raise EvidenceOidcError("evidence.oidc.claims-mismatch")
    _text(claims["jti"], 256)
    return AuthenticatedGitHubEvidenceIdentity(
        request.identity,
        policy.identity,
        key_set.identity,
        ContentIdentity.parse_uri("sha256:" + hashlib.sha256(token).hexdigest()),
        now,
        issued,
        expires,
    )


@dataclass(frozen=True, slots=True)
class AuthenticatedGitHubRunEvidence:
    """Live provider identity and signed run; closure and admission remain separate."""

    identity_check: AuthenticatedGitHubEvidenceIdentity
    authenticated: AuthenticatedEvidenceStatement
    envelope: bytes = field(repr=False)

    @property
    def identity(self):
        check = self.identity_check
        return canonical_identity(
            {
                "request": check.request_identity.to_dict(),
                "policy": check.policy_identity.to_dict(),
                "keys": check.key_set_identity.to_dict(),
                "token": check.token_digest.to_dict(),
                "checked_at": check.checked_at,
                "issued_at": check.issued_at,
                "expires_at": check.expires_at,
                "envelope": hashlib.sha256(self.envelope).hexdigest(),
            }
        )


def verify_github_run_evidence(
    token: bytes,
    envelope: bytes,
    *,
    request: GitHubEvidenceIdentityRequest,
    policy: GitHubEvidenceIdentityPolicy,
    key_set: GitHubIssuerKeySet,
    now: int,
) -> AuthenticatedGitHubRunEvidence:
    """Authenticate live CI identity and prove possession for its exact DSSE bytes.

    This does not issue durable signing authority, validate graph closure, consult
    revocations or consume a challenge. Those remain trusted admission duties.
    """

    if not isinstance(envelope, bytes) or not 1 <= len(envelope) <= MAX_ENVELOPE_BYTES:
        raise EvidenceOidcError("evidence.oidc.envelope-invalid")
    identity = verify_github_evidence_identity(
        token,
        request=request,
        policy=policy,
        key_set=key_set,
        now=now,
    )
    verify_evidence_bytes(request.evidence, envelope)
    authenticated = verify_evidence_statement(
        DsseEnvelope.from_bytes(envelope),
        trusted_public_keys=(request.signer_public_key,),
    )
    check_run_assertion(
        authenticated,
        expectation=request.expectation,
        now=now,
        maximum_clock_skew_seconds=policy.maximum_clock_skew_seconds,
        maximum_run_age_seconds=policy.maximum_run_age_seconds,
        maximum_run_duration_seconds=policy.maximum_run_duration_seconds,
    )
    return AuthenticatedGitHubRunEvidence(identity, authenticated, envelope)
