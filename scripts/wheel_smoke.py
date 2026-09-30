"""Build and verify the declared wheel from outside the source checkout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import venv
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from literate_ai.adapters.html_publication import html_output_path, read_output
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.html_observability import HtmlArtifact
from literate_ai.evidence_ledger import (
    EvidenceNode,
    attach_run,
    retained_directory,
)
from literate_ai.release_files import (
    SCHEMA as RELEASE_FILES_SCHEMA,
)
from literate_ai.release_files import (
    file_identity,
    release_file_path,
    validate_release_files,
)
from literate_ai.repository_urls import canonical_repository_origin

REQUIRED_SOURCE_TO_SPECIFICATION_SKILLS = {
    "api-surface",
    "architecture",
    "behavior-state",
    "operations",
    "security",
    "tests",
}

STANDARD_BINDING_PROBE = (
    "import json; "
    "from literate_ai.adapters.standard_lifecycle_binding import "
    "observe_installed_framework_distribution; "
    "from literate_ai.contracts import load_current_standard_lifecycle_policy; "
    "d=observe_installed_framework_distribution(); "
    "p=load_current_standard_lifecycle_policy(); "
    "print(json.dumps({'distribution':d.identity.uri,'policy':p.identity.uri,"
    "'members':len(d.members)},sort_keys=True))"
)

INSTALLED_RUNTIME_RESOURCE_PROBE = """
import json
from importlib.resources import files

package = files("literate_ai.project_template")
required = (
    "workflows/production/staging/dev/workflow.md",
    "routing/production/staging/dev/routing.json",
    "skills/agent/record-user-directed-work/SKILL.md",
    "skills/agent/author-instructional-videos/SKILL.md",
    "flavors/os-windows/standard-command-profile.json",
)
missing = [
    relative for relative in required if not package.joinpath(relative).is_file()
]
guard = files("literate_ai").joinpath("remote_source_guard.py")
if missing or not guard.is_file():
    raise SystemExit(
        "installed wheel lacks runtime resources: "
        + json.dumps({"guard": guard.is_file(), "missing": missing}, sort_keys=True)
    )
print(json.dumps({"guard": True, "project_template": list(required)}, sort_keys=True))
"""

INSTALLED_EVIDENCE_SIGNATURE_PROBE = """
import json
import tempfile
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from literate_ai.security.evidence.github_oidc import (
    GITHUB_OIDC_ISSUER, EvidenceOidcError, GitHubEvidenceIdentityPolicy,
    GitHubEvidenceIdentityRequest, GitHubIssuerKeySet, verify_github_evidence_identity,
    verify_github_run_evidence,
)
from dataclasses import replace
from pathlib import Path
from literate_ai.adapters.evidence_storage import FileSystemEvidenceStore
from literate_ai.application.evidence_resolution import (
    ConfiguredEvidenceResolver, resolve_statement_evidence,
)
from literate_ai.application.evidence_verification import (
    verify_run_evidence_graph, verify_retained_evidence_graph,
)
from literate_ai.security.evidence.retention import SignedEvidenceRetention
from literate_ai.security.evidence.graph import (
    EvidenceRunRequirement, EvidenceVerificationState,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.security.evidence import (
    DerivationRun, EvidenceArtifact, EvidenceLocator, EvidenceNotFoundError,
    EvidenceStorageError,
    EvidenceRunContext, EvidenceStatement, EvidenceSignerRule, EvidenceTrustPolicy,
    EvidenceRevocations, RunEvidenceExpectation, EvidenceTrustError, check_run_evidence,
    DsseEnvelope, DsseError, Ed25519EvidenceSigner, verify_ed25519_envelope,
)

# Deterministic disposable test key; never an operational credential.
signer = Ed25519EvidenceSigner(bytes(range(32)))
media = "application/vnd.in-toto+json"
payload = b"installed wheel signature primitive"
envelope = DsseEnvelope.from_bytes(signer.sign(media, payload).to_bytes())
verified = verify_ed25519_envelope(
    envelope, trusted_public_keys=(signer.public_key,), expected_payload_type=media,
)
assert verified.payload is envelope.payload
assert verified.payload == payload
assert verified.signer_key_identities == (signer.key_identity,)
try:
    verify_ed25519_envelope(
        replace(envelope, payload=b"substituted"),
        trusted_public_keys=(signer.public_key,), expected_payload_type=media,
    )
except DsseError:
    pass
else:
    raise SystemExit("installed signature verifier admitted a substituted payload")
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory).resolve()
    writer = FileSystemEvidenceStore(root, writable=True)
    subject = writer.put_bytes(b"source manifest", media_type="application/json")
    inputs = writer.put_bytes(b"specification", media_type="text/plain")
    journal = writer.put_bytes(b"journal", media_type="application/json")
    context = EvidenceRunContext(
        "wheel-probe", "local-wheel-probe", "0" * 40,
        canonical_identity("workflow"), canonical_identity("target"), 0, 1,
    )
    run = DerivationRun(context, subject,
                        (EvidenceArtifact("spec", inputs),), journal, "passed")
    run_envelope = signer.sign(media, EvidenceStatement(run).to_bytes())
    rule = EvidenceSignerRule(
        signer.public_key, "local:wheel-probe", (context.repository,),
        (context.workflow,), (context.target,), (run.SCHEMA,), (), 0, 100,
    )
    policy = EvidenceTrustPolicy((rule,), 1, 10, 10, 0, 10, 1)
    revocations = EvidenceRevocations(1, 10, (), (), ())
    expectation = RunEvidenceExpectation(
        run.SCHEMA, subject, context.invocation_id, context.repository,
        context.revision, context.workflow, context.target, 0, 2, (),
    )
    checked = check_run_evidence(run_envelope, expectation=expectation,
        policy=policy, revocations=revocations, now=2)
    signed = checked.authenticated
    assert signed.payload is run_envelope.payload
    try:
        check_run_evidence(run_envelope,
            expectation=replace(expectation, revision="f" * 40),
            policy=policy, revocations=revocations, now=2)
    except EvidenceTrustError:
        pass
    else:
        raise SystemExit("installed policy checker admitted the wrong revision")
    resolver = ConfiguredEvidenceResolver({"local": FileSystemEvidenceStore(root)})
    resolved = resolve_statement_evidence(signed.statement, resolver, locators=tuple(
        EvidenceLocator("local", reference, 2)
        for reference in (subject, inputs, journal)
    ))
    assert {item.reference: item.content for item in resolved} == {
        subject: b"source manifest", inputs: b"specification", journal: b"journal",
    }
    envelope_ref = writer.put_bytes(run_envelope.to_bytes(),
        media_type="application/vnd.dsse.envelope.v1+json")
    requirement = EvidenceRunRequirement(envelope_ref, expectation, (), (
        ("input/spec", inputs), ("journal", journal), ("subject", subject),
    ))
    graph_arguments = dict(requirements=(requirement,), resolver=resolver,
        locators=tuple(EvidenceLocator("local", ref, 2)
                       for ref in (envelope_ref, subject, inputs, journal)),
        policy=policy)
    states = iter((EvidenceVerificationState(2, revocations),
                   EvidenceVerificationState(3, revocations)))
    graph = verify_run_evidence_graph(envelope_ref,
        current_state=lambda: next(states), **graph_arguments)
    assert len(graph.runs) == 1 and len(graph.objects) == 4
    assert graph.runs[0][1].checked_at == 3
    assert graph.runs[0][1].authenticated.payload == run_envelope.payload
    states = iter((EvidenceVerificationState(2, revocations),
        EvidenceVerificationState(3, replace(revocations,
            issued_at=3, invocation_ids=(context.invocation_id,)))))
    try:
        verify_run_evidence_graph(envelope_ref,
            current_state=lambda: next(states), **graph_arguments)
    except EvidenceTrustError as error:
        assert error.code == "evidence.trust.invocation-revoked"
    else:
        raise SystemExit("installed graph verifier ignored post-read revocation")
    retention = []
    for ref in (envelope_ref, subject, inputs, journal):
        locator = EvidenceLocator("local", ref, 10)
        retention.append(SignedEvidenceRetention(locator,
            signer.sign(media, EvidenceStatement(locator).to_bytes()).to_bytes()))
    retained_policy = replace(policy, signers=(replace(rule,
        predicate_types=tuple(sorted((run.SCHEMA, EvidenceLocator.SCHEMA))),
        store_ids=("local",)),))
    states = iter((EvidenceVerificationState(2, revocations),
                   EvidenceVerificationState(3, revocations)))
    retained_arguments = dict(requirements=(requirement,), resolver=resolver,
        policy=retained_policy, current_state=lambda: next(states))
    retained = verify_retained_evidence_graph(envelope_ref,
        retention=tuple(retention), **retained_arguments)
    assert len(retained.retention) == 4 and len(retained.graph.objects) == 4
    assert retained.graph.final_state.now == 3
    try:
        verify_retained_evidence_graph(envelope_ref,
            retention=tuple(retention[:-1]), **retained_arguments)
    except EvidenceStorageError as error:
        assert error.code == "evidence.storage.not-found"
    else:
        raise SystemExit("installed graph verifier admitted missing retention proof")
    # Synthetic issuer fixture: packaging qualification, not a live CI attestation.
    issuer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(issuer.public_key(), as_dict=True)
    issuer_jwk.update(kid="fixture", alg="RS256", use="sig")
    keys = GitHubIssuerKeySet(json.dumps({"keys": [issuer_jwk]}).encode(), 1, 10)
    oidc_expectation = replace(expectation,
        repository="https://github.com/example/project")
    oidc_policy = GitHubEvidenceIdentityPolicy(tuple(sorted({
        "sub": "repo:example/project:ref:refs/heads/main",
        "repository": "example/project", "repository_id": "1",
        "repository_owner_id": "2", "workflow_ref": "reviewed-workflow",
        "workflow_sha": "0" * 40, "event_name": "push", "ref": "refs/heads/main",
        "runner_environment": "github-hosted",
    }.items())), context.workflow, (context.target,))
    oidc_run = replace(run, context=replace(context,
        repository=oidc_expectation.repository))
    oidc_envelope = signer.sign(
        media, EvidenceStatement(oidc_run).to_bytes()).to_bytes()
    oidc_ref = writer.put_bytes(oidc_envelope,
        media_type="application/vnd.dsse.envelope.v1+json")
    oidc_request = GitHubEvidenceIdentityRequest(oidc_ref, signer.public_key,
        oidc_expectation, "1", "1", "2", canonical_identity("fixture challenge"))
    oidc_claims = dict(oidc_policy.claims)
    oidc_claims.update(iss=GITHUB_OIDC_ISSUER, aud=oidc_request.audience(oidc_policy),
        sha="0" * 40, run_id="1", run_attempt="1", check_run_id="2",
        jti="fixture-token", iat=1, nbf=1, exp=10)
    token = jwt.encode(oidc_claims, issuer, algorithm="RS256",
        headers={"kid": "fixture"}).encode()
    identity = verify_github_evidence_identity(token, request=oidc_request,
        policy=oidc_policy, key_set=keys, now=2)
    assert identity.request_identity == oidc_request.identity
    live_run = verify_github_run_evidence(token, oidc_envelope,
        request=oidc_request, policy=oidc_policy, key_set=keys, now=2)
    assert live_run.authenticated.statement.predicate == oidc_run
    assert live_run.envelope == oidc_envelope
    try:
        verify_github_evidence_identity(token,
            request=replace(oidc_request, challenge=canonical_identity("different")),
            policy=oidc_policy, key_set=keys, now=2)
    except EvidenceOidcError as error:
        assert error.code == "evidence.oidc.token-invalid"
    else:
        raise SystemExit("installed identity verifier admitted another audience")
    missing = replace(subject, digest="f" * 64)
    try:
        resolver.resolve_many(
            (missing,), locators=(EvidenceLocator("local", missing, 2),),
        )
    except EvidenceNotFoundError:
        pass
    else:
        raise SystemExit("installed evidence resolver admitted a missing blob")
print(json.dumps({"round_trip": True, "tamper_rejected": True,
                  "stored_statement_evidence": True, "missing_blob_refused": True,
                  "policy_checked": True, "wrong_run_refused": True,
                  "graph_verified": True, "mid_read_revocation_refused": True,
                  "retention_checked": True, "missing_retention_refused": True,
                  "oidc_fixture_verified": True, "oidc_audience_refused": True,
                  "oidc_run_possession_verified": True,
                  "signer_key_identity": signer.key_identity}, sort_keys=True))
"""


def _remotes_stripped_parent(repository: Path, destination: Path, revision: str) -> str:
    """Return a local ``--from`` locator that cannot follow ``origin`` to GitHub."""

    run("git", "clone", "--bare", "--no-local", str(repository), str(destination))
    remotes = run("git", "--git-dir", str(destination), "remote").stdout.split()
    for name in remotes:
        run("git", "--git-dir", str(destination), "remote", "remove", name)
    return f"{destination.resolve().as_uri()}#{revision}"


_COLOR_FORCING_ENVIRONMENT_VARIABLES = (
    "FORCE_COLOR",
    "PYTHON_COLORS",
    "CLICOLOR_FORCE",
)


_INTERPRETER_RESOLUTION_ENVIRONMENT_VARIABLES = (
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
)


@contextmanager
def _runtime_directory():
    root = Path(tempfile.mkdtemp(prefix="literate-wheel-smoke-"))
    with retained_directory(
        root,
        node_path="installed/wheel-smoke/runtime",
        operation="installed.wheel-smoke.runtime",
        role="wheel-smoke-workspace",
    ) as retained:
        yield retained


def _subprocess_environment() -> dict[str, str]:
    """A deterministic environment: the invoking shell's color forcing and
    interpreter-resolution variables must not leak into installed-CLI
    subprocesses. ``PYTHONPATH=src`` from a developer session would otherwise
    make ``observe_installed_framework_distribution`` see both the smoke-test
    wheel and the checkout, failing ``standard_binding.distribution_ambiguous``.
    """

    sanitized = {
        key: value
        for key, value in os.environ.items()
        if key not in _COLOR_FORCING_ENVIRONMENT_VARIABLES
        and key not in _INTERPRETER_RESOLUTION_ENVIRONMENT_VARIABLES
    }
    sanitized["NO_COLOR"] = "1"
    return sanitized


def run(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        env=env if env is not None else _subprocess_environment(),
    )
    if completed.returncode != 0:
        command = Path(args[0]).name if args else "subprocess"
        detail = (completed.stderr or completed.stdout).strip()[-4000:]
        raise RuntimeError(
            f"wheel smoke command {command!r} failed with status "
            f"{completed.returncode}: {detail}"
        )
    return completed


def _write_canonical_json(path: Path, value: dict[str, object]) -> None:
    path.write_bytes(
        (
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
    )


def _fixture_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Enable local fixture transport for this Git command only."""

    return run(
        "git",
        "-c",
        "protocol.file.allow=always",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "maintenance.auto=false",
        "-c",
        "gc.auto=0",
        "-c",
        "core.autocrlf=false",
        *args,
        cwd=cwd,
    )


def _installed_cli_result(litai: Path, *args: str, cwd: Path) -> dict[str, object]:
    payload = json.loads(run(str(litai), *args, "--json", cwd=cwd).stdout)
    result = payload.get("result")
    if payload.get("ok") is not True or not isinstance(result, dict):
        raise RuntimeError(f"installed CLI returned an invalid result: {payload!r}")
    return result


def assert_installed_repository_refresh(litai: Path, root: Path) -> dict[str, object]:
    """Prove reviewed Gitlink refresh and clean-clone reuse via installed surfaces."""

    fixture = root / "repository-refresh"
    fixture.mkdir()
    fixture = fixture.resolve()
    child = fixture / "child"
    child.mkdir()
    _fixture_git("init", "-b", "main", cwd=child)
    _fixture_git("config", "user.name", "Wheel Refresh", cwd=child)
    _fixture_git("config", "user.email", "refresh@example.invalid", cwd=child)
    (child / "source.txt").write_text("first child authority\n", encoding="utf-8")
    _fixture_git("add", "source.txt", cwd=child)
    _fixture_git("commit", "-m", "Publish first child authority", cwd=child)
    first_commit = _fixture_git("rev-parse", "HEAD", cwd=child).stdout.strip()

    child_remote = fixture / "child.git"
    _fixture_git(
        "init", "--bare", "--initial-branch=main", str(child_remote), cwd=fixture
    )
    _fixture_git("remote", "add", "origin", child_remote.resolve().as_uri(), cwd=child)
    _fixture_git("push", "-u", "origin", "main", cwd=child)

    project = fixture / "super"
    project.mkdir()
    _fixture_git("init", "-b", "main", cwd=project)
    _fixture_git("config", "user.name", "Wheel Refresh", cwd=project)
    _fixture_git("config", "user.email", "refresh@example.invalid", cwd=project)
    (project / "README.md").write_text(
        "# Installed refresh fixture\n", encoding="utf-8"
    )
    (project / ".gitmodules").write_text(
        f'[submodule "app"]\n\tpath = app\n\turl = {child_remote.resolve().as_uri()}\n',
        encoding="utf-8",
        newline="\n",
    )
    _fixture_git(
        "clone",
        "--no-local",
        child_remote.resolve().as_uri(),
        str(project / "app"),
        cwd=fixture,
    )
    _fixture_git("config", "user.name", "Wheel Refresh", cwd=project / "app")
    _fixture_git("config", "user.email", "refresh@example.invalid", cwd=project / "app")
    _fixture_git("add", "README.md", ".gitmodules", cwd=project)
    _fixture_git(
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        first_commit,
        "app",
        cwd=project,
    )
    _fixture_git("commit", "-m", "Pin first independent child", cwd=project)

    project_remote = fixture / "super.git"
    _fixture_git(
        "init", "--bare", "--initial-branch=main", str(project_remote), cwd=fixture
    )
    _fixture_git(
        "remote", "add", "origin", project_remote.resolve().as_uri(), cwd=project
    )

    declaration = fixture / "orchestration.json"
    _write_canonical_json(
        declaration,
        {
            "schema": "literate-ai/orchestration@1",
            "relationships": [],
        },
    )
    orchestration = (
        "onboard",
        "orchestrate",
    )
    project_options = (
        "--declaration",
        str(declaration),
        "--project-id",
        "super",
        "--project-version",
        "1.0.0",
    )
    initialization_plan = _installed_cli_result(
        litai,
        *orchestration,
        "plan",
        str(project),
        *project_options,
        cwd=fixture,
    )
    plan_identity = initialization_plan.get("plan_identity")
    planned_orchestration = initialization_plan.get("project", {}).get(
        "repository_orchestration", {}
    )
    if (
        initialization_plan.get("writes") is not False
        or planned_orchestration.get("relationships") != []
        or not isinstance(plan_identity, str)
    ):
        raise RuntimeError("installed orchestration plan lost empty root authority")
    initialization_check = _installed_cli_result(
        litai,
        *orchestration,
        "check",
        str(project),
        *project_options,
        "--expected-plan-identity",
        plan_identity,
        cwd=fixture,
    )
    initialized = _installed_cli_result(
        litai,
        *orchestration,
        "initialize",
        str(project),
        *project_options,
        "--expected-plan-identity",
        plan_identity,
        "--acknowledge",
        cwd=fixture,
    )
    if (
        initialization_check.get("state") != "current"
        or initialized.get("state") != "initialized"
    ):
        raise RuntimeError("installed CLI did not initialize reviewed orchestration")
    _fixture_git("add", "-A", cwd=project)
    _fixture_git("commit", "-m", "Initialize root orchestration authority", cwd=project)

    app = project / "app"
    initial_manifest = json.loads(
        (project / "literate.project.json").read_text(encoding="utf-8")
    )
    initial_manifest_pin = next(
        (
            item.get("commit")
            for item in initial_manifest.get("repository_orchestration", {}).get(
                "repositories", []
            )
            if isinstance(item, dict) and item.get("path") == "app"
        ),
        None,
    )
    initial_index_pin = _fixture_git(
        "ls-files", "--stage", "app", cwd=project
    ).stdout.split()[1]
    if (initial_manifest_pin, initial_index_pin) != (first_commit, first_commit):
        raise RuntimeError("installed initialization did not bind the first exact pin")

    (app / "source.txt").write_text("second child authority\n", encoding="utf-8")
    _fixture_git("add", "source.txt", cwd=app)
    _fixture_git("commit", "-m", "Publish second child authority", cwd=app)
    second_commit = _fixture_git("rev-parse", "HEAD", cwd=app).stdout.strip()
    if second_commit == first_commit:
        raise RuntimeError("child refresh fixture did not publish a second commit")
    _fixture_git("push", "origin", "HEAD:main", cwd=app)

    request = fixture / "refresh.json"
    _write_canonical_json(
        request,
        {
            "schema": "literate-ai/orchestration-refresh-request@1",
            "targets": [{"path": "app", "commit": second_commit}],
        },
    )
    refresh = (*orchestration, "refresh")
    refresh_options = (
        "--request",
        str(request),
        "--repository-fetch-total-seconds",
        "120",
        "--repository-fetch-no-progress-seconds",
        "60",
        "--repository-fetch-connect-seconds",
        "10",
    )
    refresh_plan = _installed_cli_result(
        litai,
        *refresh,
        "plan",
        str(project),
        *refresh_options,
        cwd=fixture,
    )
    refresh_plan_identity = refresh_plan.get("plan_identity")
    if (
        not isinstance(refresh_plan_identity, str)
        or refresh_plan.get("changed") is not True
        or refresh_plan.get("writes") is not False
        or refresh_plan.get("source_writes") is not False
        or refresh_plan.get("target_modes")
        != [{"path": "app", "mode": "root-pin-only"}]
        or refresh_plan.get("child_authority") != "independent"
        or refresh_plan.get("child_acceptance") != "not-qualified"
        or refresh_plan.get("crash_replay") != "not-supported"
    ):
        raise RuntimeError("installed refresh plan made an unsupported child claim")
    refresh_check = _installed_cli_result(
        litai,
        *refresh,
        "check",
        str(project),
        *refresh_options,
        "--expected-plan-identity",
        refresh_plan_identity,
        cwd=fixture,
    )
    refreshed = _installed_cli_result(
        litai,
        *refresh,
        "apply",
        str(project),
        *refresh_options,
        "--expected-plan-identity",
        refresh_plan_identity,
        "--acknowledge",
        cwd=fixture,
    )
    if (
        refresh_check.get("state") != "current"
        or refresh_check.get("source_writes") is not False
        or refresh_check.get("target_modes")
        != [{"path": "app", "mode": "root-pin-only"}]
        or refresh_check.get("child_authority") != "independent"
        or refresh_check.get("child_acceptance") != "not-qualified"
        or refreshed.get("state") != "committed"
        or refreshed.get("transaction_state") != "committed"
        or refreshed.get("changed") is not True
        or refreshed.get("source_writes") is not False
        or refreshed.get("target_modes") != [{"path": "app", "mode": "root-pin-only"}]
        or refreshed.get("child_authority") != "independent"
        or refreshed.get("child_acceptance") != "not-qualified"
        or refreshed.get("crash_replay") != "not-supported"
    ):
        raise RuntimeError(
            "installed refresh result made an unsupported authority claim"
        )

    child_head = _fixture_git("rev-parse", "HEAD", cwd=app).stdout.strip()
    index_pin = _fixture_git("ls-files", "--stage", "app", cwd=project).stdout.split()[
        1
    ]
    manifest = json.loads(
        (project / "literate.project.json").read_text(encoding="utf-8")
    )
    repositories = manifest.get("repository_orchestration", {}).get("repositories", [])
    manifest_pin = next(
        (
            item.get("commit")
            for item in repositories
            if isinstance(item, dict) and item.get("path") == "app"
        ),
        None,
    )
    if (child_head, index_pin, manifest_pin) != (
        second_commit,
        second_commit,
        second_commit,
    ):
        raise RuntimeError(
            "installed refresh did not advance all exact pin authorities"
        )

    review = _installed_cli_result(
        litai,
        "project",
        "documentation-review",
        str(project),
        "--record",
        cwd=fixture,
    )
    locked = _installed_cli_result(litai, "lock", str(project), cwd=fixture)
    if (
        review.get("state") != "current"
        or review.get("recorded") is not True
        or locked.get("repository_lock_updated") is not True
    ):
        raise RuntimeError("installed CLI did not review and lock refreshed authority")

    _fixture_git("add", "-A", cwd=project)
    _fixture_git("commit", "-m", "Commit reviewed child refresh", cwd=project)
    root_commit = _fixture_git("rev-parse", "HEAD", cwd=project).stdout.strip()
    _fixture_git("push", "-u", "origin", "main", cwd=project)

    clean = fixture / "clean"
    _fixture_git(
        "clone",
        "--recurse-submodules",
        project_remote.resolve().as_uri(),
        str(clean),
        cwd=fixture,
    )
    clean_child_head = _fixture_git(
        "rev-parse", "HEAD", cwd=clean / "app"
    ).stdout.strip()
    clean_index_pin = _fixture_git(
        "ls-files", "--stage", "app", cwd=clean
    ).stdout.split()[1]
    clean_manifest = json.loads(
        (clean / "literate.project.json").read_text(encoding="utf-8")
    )
    clean_manifest_pin = next(
        (
            item.get("commit")
            for item in clean_manifest.get("repository_orchestration", {}).get(
                "repositories", []
            )
            if isinstance(item, dict) and item.get("path") == "app"
        ),
        None,
    )
    validation = _installed_cli_result(
        litai, "project", "validate", str(clean), cwd=fixture
    )
    lock_check = _installed_cli_result(
        litai, "lock", str(clean), "--check", cwd=fixture
    )
    clean_plan = _installed_cli_result(litai, "plan", str(clean), cwd=fixture)
    if (
        (clean_child_head, clean_index_pin, clean_manifest_pin)
        != (second_commit, second_commit, second_commit)
        or validation.get("profile") != "canonical"
        or lock_check.get("lock", {}).get("state") != "current"
        or clean_plan.get("writes") is not False
    ):
        raise RuntimeError("clean installed project did not retain the refreshed pin")

    return {
        "child_acceptance": refreshed["child_acceptance"],
        "child_authority": refreshed["child_authority"],
        "child_commit": second_commit,
        "clean_clone": {
            "lock_current": True,
            "plan_read_only": True,
            "validated": True,
        },
        "crash_replay": refreshed["crash_replay"],
        "source_writes": refreshed["source_writes"],
        "target_modes": refreshed["target_modes"],
        "root_commit": root_commit,
    }


def assert_installed_operator_golden_paths(
    litai: Path,
    root: Path,
    *,
    parent_source: str,
) -> dict[str, object]:
    """Prove create and adopt through only the release-candidate wheel CLI."""

    created_project = root / "golden-created"
    create_arguments = (
        str(litai),
        "--json",
        "onboard",
        "create",
        str(created_project),
        "--from",
        parent_source,
    )
    create_plan = json.loads(run(*create_arguments, cwd=root).stdout)["result"]
    if create_plan.get("writes") is not False or not isinstance(
        create_plan.get("plan_identity"), str
    ):
        raise RuntimeError("installed onboard create omitted its read-only exact plan")
    create_apply = json.loads(
        run(
            *create_arguments,
            "--apply",
            "--acknowledge",
            "--expect-plan",
            create_plan["plan_identity"],
            cwd=root,
        ).stdout
    )["result"]
    create_status = json.loads(
        run(
            str(litai),
            "--json",
            "status",
            "--project",
            str(created_project),
            cwd=root,
        ).stdout
    )["result"]
    create_validation = json.loads(
        run(
            str(litai),
            "--json",
            "project",
            "validate",
            str(created_project),
            cwd=root,
        ).stdout
    )["result"]
    if (
        create_apply.get("applied") is not True
        or create_status.get("project", {}).get("kind") != "created"
        or create_validation.get("profile") != "canonical"
        or not (created_project / "samples/hello-component/component.md").is_file()
    ):
        raise RuntimeError("installed onboard create did not reach a validated hello")

    adopted_project = root / "golden-adopted"
    adopted_project.mkdir()
    (adopted_project / "app.py").write_text(
        "print('retained application')\n", encoding="utf-8"
    )
    (adopted_project / "Makefile").write_text(
        "all:\n"
        "\tmkdir -p build\n"
        "\tprintf 'artifact\\n' > build/app\n"
        "test:\n"
        "\t@printf 'Ran 1 test\\n'\n"
        "\t@printf 'OK\\n'\n"
        "package:\n"
        "\tmkdir -p dist\n"
        "\tprintf 'package\\n' > dist/app.txt\n",
        encoding="utf-8",
    )
    run("git", "init", "-b", "main", cwd=adopted_project)
    run(
        "git",
        "config",
        "user.email",
        "golden-path@example.invalid",
        cwd=adopted_project,
    )
    run(
        "git",
        "config",
        "user.name",
        "Literate AI Golden Path",
        cwd=adopted_project,
    )
    run("git", "add", ".", cwd=adopted_project)
    run("git", "commit", "-m", "Create brownfield fixture", cwd=adopted_project)
    adopt_arguments = (
        str(litai),
        "--json",
        "onboard",
        "adopt",
        str(adopted_project),
        "--from",
        parent_source,
        "--default-branch",
        "main",
    )
    adopt_plan = json.loads(run(*adopt_arguments, cwd=root).stdout)["result"]
    conversion = adopt_plan.get("conversion", {})
    if (
        adopt_plan.get("writes") is not False
        or adopt_plan.get("landing_stage") != "wrapped"
        or conversion.get("readiness") != "ready"
        or not conversion.get("retained_runners")
        or not isinstance(adopt_plan.get("plan_identity"), str)
    ):
        raise RuntimeError("installed onboard adopt omitted its conversion facts")
    adopt_apply = json.loads(
        run(
            *adopt_arguments,
            "--apply",
            "--acknowledge",
            "--expect-plan",
            adopt_plan["plan_identity"],
            cwd=root,
        ).stdout
    )["result"]
    candidate = root / "retained-candidate.json"
    run(
        str(litai),
        "--json",
        "project",
        "test-receipt",
        "run-retained",
        str(candidate),
        "--project",
        str(adopted_project),
        "--worker-id",
        "local",
        cwd=root,
    )
    run(
        str(litai),
        "--json",
        "project",
        "test-receipt",
        "update",
        str(candidate),
        "--project",
        str(adopted_project),
        cwd=root,
    )
    retained = json.loads(
        run(
            str(litai),
            "--json",
            "project",
            "convert-stage",
            "advance",
            "--to",
            "retained",
            "--project",
            str(adopted_project),
            cwd=root,
        ).stdout
    )["result"]
    adopt_status = json.loads(
        run(
            str(litai),
            "--json",
            "status",
            "--project",
            str(adopted_project),
            cwd=root,
        ).stdout
    )["result"]
    conversion_status = adopt_status.get("project", {}).get("conversion_authority", {})
    if (
        adopt_apply.get("applied") is not True
        or retained.get("stage") != "retained"
        or retained.get("release_authority") != "original-source"
        or conversion_status.get("stage") != "retained"
        or adopt_status.get("project", {}).get("test_receipt", {}).get("state")
        != "current"
    ):
        raise RuntimeError(
            "installed onboard adopt did not reach an explicit retained receipt"
        )
    return {
        "create": {
            "kind": create_status["project"]["kind"],
            "validated": True,
            "hello": True,
        },
        "adopt": {
            "stage": retained["stage"],
            "release_authority": retained["release_authority"],
            "receipt": "current",
        },
        "source": "release-candidate-wheel",
    }


def assert_lifecycle_does_not_require_project_codegraph(
    litai: Path, project: Path
) -> dict[str, object]:
    """Prove installed lifecycle commands do not fail closed on a missing index."""

    index_directory = project / ".codegraph"
    backup = None
    if index_directory.exists():
        backup = project / ".codegraph-wheel-smoke-backup"
        index_directory.rename(backup)
    try:
        missing = subprocess.run(
            (
                str(litai),
                "run",
                "samples/hello-component",
                "--project",
                str(project),
            ),
            cwd=project.parent,
            check=False,
            text=True,
            capture_output=True,
        )
        payload = json.loads(missing.stderr or missing.stdout)
        error_code = payload.get("error", {}).get("code")
        if missing.returncode == 0 or str(error_code).startswith(
            "project.source_intelligence_"
        ):
            raise RuntimeError(
                "installed run failed closed on missing optional CodeGraph"
            )
        if error_code != "artifact_export.not_built":
            raise RuntimeError(
                "installed run did not continue to artifact lookup without CodeGraph"
            )
    finally:
        if backup is not None:
            if index_directory.exists():
                if index_directory.is_dir():
                    shutil.rmtree(index_directory)
                else:
                    index_directory.unlink()
            backup.rename(index_directory)
    return {"continued_without_index": "run"}


def bootstrap_real_worker_runtime(
    root: Path, wheel: Path, distribution_identity: str
) -> tuple[Path | None, dict[str, object] | None]:
    """Install the real wheel as a Standard worker does."""

    if sys.platform not in {"linux", "win32"}:
        return None, None
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    try:
        from literate_ai.remote_worker_bootstrap import (
            bootstrap_worker_wheelhouse,
            export_worker_wheelhouse,
        )
    finally:
        del sys.path[0]
    wheelhouse = root / "worker-wheelhouse"
    manifest, closure_identity = export_worker_wheelhouse(
        wheel,
        wheelhouse,
        framework_distribution_identity=distribution_identity,
    )
    environment = root / "worker-runtime"
    launcher, installed = bootstrap_worker_wheelhouse(
        wheelhouse,
        manifest,
        expected_closure_identity=closure_identity,
        expected_distribution_identity=distribution_identity,
        install_root=environment,
    )
    scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    observed = json.loads(
        run(str(python), "-c", STANDARD_BINDING_PROBE, cwd=root).stdout
    )
    if observed["distribution"] != distribution_identity:
        raise RuntimeError("worker-equivalent install changed Standard distribution")
    return launcher, {
        "capability": None,
        "capability_identity": None,
        "dependency_closure_identity": closure_identity,
        "installed_distributions": installed,
        "standard_binding": observed,
    }


def bootstrap_uv_tool_worker_runtime(
    root: Path, wheel: Path, distribution_identity: str
) -> dict[str, object] | None:
    """Prove a normal uv tool install can export and bootstrap its exact wheel."""

    uv = shutil.which("uv")
    if uv is None:
        return None
    tool_directory = root / "uv-tools"
    binary_directory = root / "uv-bin"
    cache_directory = root / "uv-cache"
    environment = {
        **_subprocess_environment(),
        "UV_TOOL_DIR": str(tool_directory),
        "UV_TOOL_BIN_DIR": str(binary_directory),
        "UV_CACHE_DIR": str(cache_directory),
    }
    run(
        uv,
        "tool",
        "install",
        "--force",
        "--python",
        sys.executable,
        str(wheel),
        cwd=root,
        env=environment,
    )
    litai = binary_directory / ("litai.exe" if sys.platform == "win32" else "litai")
    candidates = tuple(
        path
        for path in tool_directory.iterdir()
        if path.is_dir() and (path / "pyvenv.cfg").is_file()
    )
    if len(candidates) != 1 or not litai.is_file():
        raise RuntimeError("uv tool install did not create one isolated litai runtime")
    scripts = candidates[0] / ("Scripts" if sys.platform == "win32" else "bin")
    python = scripts / ("python.exe" if sys.platform == "win32" else "python")
    observed = json.loads(
        run(str(python), "-c", STANDARD_BINDING_PROBE, cwd=root, env=environment).stdout
    )
    if observed["distribution"] != distribution_identity:
        raise RuntimeError(
            "uv and pip installations changed the exact framework distribution identity"
        )

    wheelhouse = root / "uv-worker-wheelhouse"
    exported = json.loads(
        run(
            str(litai),
            "worker",
            "wheelhouse",
            "export",
            "--framework-wheel",
            str(wheel),
            "--distribution-identity",
            distribution_identity,
            "--output",
            str(wheelhouse),
            cwd=root,
            env=environment,
        ).stdout
    )
    result = exported.get("result", {})
    manifest = Path(str(result.get("manifest", "")))
    closure_identity = str(result.get("dependency_closure_identity", ""))
    if not exported.get("ok") or not manifest.is_file():
        raise RuntimeError("uv-installed litai did not export a worker wheelhouse")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    try:
        from literate_ai.remote_worker_bootstrap import bootstrap_worker_wheelhouse
    finally:
        del sys.path[0]
    launcher, installed = bootstrap_worker_wheelhouse(
        wheelhouse,
        manifest,
        expected_closure_identity=closure_identity,
        expected_distribution_identity=distribution_identity,
        install_root=root / "uv-worker-runtime",
    )
    return {
        "dependency_closure_identity": closure_identity,
        "installed_distributions": installed,
        "launcher": str(launcher),
        "standard_binding": observed,
    }


def assert_standard_project_version_check(
    litai: Path, project: Path
) -> dict[str, object]:
    """Prove version inspection resolves Standard bindings and rejects forged pins."""

    exact = json.loads(
        run(str(litai), "version", "check", "--project", str(project)).stdout
    )
    exact_project = exact.get("result", {}).get("project", {})
    exact_driver = exact_project.get("lifecycle_driver", {})
    exact_derived = exact_project.get("derived_identities", {})
    if (
        not exact.get("ok")
        or not exact.get("result", {}).get("ok")
        or exact_driver.get("binding") != "standard"
        or exact_derived.get("driver_identity_matches") is not True
        or exact_derived.get("driver_resolution_error") is not None
    ):
        raise RuntimeError(
            "installed version check cannot resolve its initialized Standard project"
        )

    manifest_path = project / "literate.project.json"
    original = manifest_path.read_bytes()
    manifest = json.loads(original)
    lifecycle_driver = manifest.get("lifecycle_driver", {})
    distribution_identity = lifecycle_driver.get("framework_distribution_identity", {})
    if not isinstance(distribution_identity, dict):
        raise RuntimeError("initialized Standard binding has no distribution identity")
    distribution_identity["digest"] = "0" * 64
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        forged = subprocess.run(
            (str(litai), "version", "check", "--project", str(project)),
            cwd=project.parent,
            check=False,
            text=True,
            capture_output=True,
        )
        forged_payload = json.loads(forged.stdout or forged.stderr)
        forged_project = forged_payload.get("result", {}).get("project", {})
        forged_derived = forged_project.get("derived_identities", {})
        forged_error = forged_derived.get("driver_resolution_error", {})
        if (
            forged.returncode != 1
            or not forged_payload.get("ok")
            or forged_payload.get("result", {}).get("ok") is not False
            or forged_derived.get("driver_identity_matches") is not False
            or forged_error.get("code") != "standard_binding.distribution_mismatch"
        ):
            raise RuntimeError(
                "installed version check did not reject a forged Standard binding"
            )
    finally:
        manifest_path.write_bytes(original)

    return {
        "binding": exact_driver["binding"],
        "exact": True,
        "forged_error": forged_error["code"],
    }


def assert_installed_graph_exports(
    litai: Path,
    project: Path,
    *,
    expected_graph: dict[str, object] | None = None,
) -> dict[str, object]:
    """Prove every portable graph view comes from one installed-wheel graph."""

    expected_markers = {
        "json": '"schema":"literate-ai/authority-graph@2"',
        "text": "project ",
        "mermaid": "flowchart LR",
        "dot": "digraph literate_ai {",
        "svg": '<svg xmlns="http://www.w3.org/2000/svg"',
    }
    canonical_graph: dict[str, object] | None = None
    counts: dict[str, int] | None = None
    for format_name, marker in expected_markers.items():
        payload = json.loads(
            run(
                str(litai),
                "graph",
                "--project",
                str(project),
                "--format",
                format_name,
                cwd=project.parent,
            ).stdout
        )
        result = payload.get("result", {})
        graph = result.get("graph")
        rendered = result.get("rendered")
        if (
            not payload.get("ok")
            or result.get("schema") != "literate-ai/authority-graph@2"
            or not isinstance(graph, dict)
            or not isinstance(rendered, str)
            or marker not in rendered
        ):
            raise RuntimeError(
                f"installed wheel produced an invalid {format_name} graph export"
            )
        if canonical_graph is None:
            canonical_graph = graph
            if expected_graph is not None and graph != expected_graph:
                raise RuntimeError(
                    "installed graph export differs from project validation authority"
                )
            counts = {
                "nodes": len(graph.get("nodes", [])),
                "edges": len(graph.get("edges", [])),
            }
        elif graph != canonical_graph:
            raise RuntimeError(
                f"installed {format_name} graph does not match the canonical graph"
            )
    assert counts is not None
    return {"formats": sorted(expected_markers), **counts}


def project_clean_source(repository: Path, destination: Path) -> None:
    """Project tracked and non-ignored worktree files into a fresh build context."""

    listed = run(
        "git",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        cwd=repository,
    ).stdout
    paths = tuple(item for item in listed.split("\0") if item)
    if not paths:
        raise RuntimeError("wheel source projection is empty")
    destination.mkdir()
    for relative in paths:
        logical = PurePosixPath(relative)
        if (
            logical.is_absolute()
            or not logical.parts
            or any(part in {"", ".", ".."} for part in logical.parts)
            or logical.as_posix() != relative
        ):
            raise RuntimeError(
                f"wheel source projection contains an invalid path: {relative}"
            )
        source = repository.joinpath(*logical.parts)
        target = destination.joinpath(*logical.parts)
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(
                f"wheel source projection contains an unsafe file: {relative}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def add_selected_flavor_packaging_provider(project: Path) -> None:
    """Add a portable Component reached only through the selected Python Flavor."""

    provider = project / "components" / "wheel-package-publisher"
    provider.mkdir()
    (provider / "component.md").write_text(
        """---
namespace: example
version: 1.0.0
display_name: Wheel smoke package publisher
profiles: ["portable"]
sample: false
provides:
  - name: packaging.wheel-smoke-publisher
    version: 1.0.0
requires: []
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/portable-application/SKILL.md
workflow_definition: workflows/production/staging/dev/workflow.md
routing_policy: routing/production/staging/dev/routing.json
flavor_slots: []
entrypoints:
  - name: publish
    kind: packaging
    path: publish
acceptance_contracts: []
source_dependencies: []
---
# Wheel smoke package publisher

Provide a deterministic packaging-phase publisher for installed-wheel qualification.
""",
        encoding="utf-8",
        newline="\n",
    )

    flavor = project / "flavors" / "lang-python" / "flavor.md"
    current = flavor.read_text(encoding="utf-8")
    required = "requires: []"
    if current.count(required) != 1:
        raise RuntimeError("installed Python Flavor has an unexpected requires field")
    flavor.write_text(
        current.replace(
            required,
            """requires:
  - requirement_id: publish-wheel-smoke-package
    capability: packaging.wheel-smoke-publisher
    version_range: ">=1,<2"
    dependency_kind: packaging
    optional: false
    constraints: []""",
        ),
        encoding="utf-8",
        newline="\n",
    )


def _retain_qualified_html(
    repository: Path, project: Path, artifact_record: dict[str, object]
) -> dict[str, str]:
    """Preserve verified HTML/record bytes before the smoke workspace is pruned.

    These are rendering evidence, not a release manifest or browser acceptance.
    Copy a bounded verified snapshot rather than reopening the source for copying.
    """
    # Canonicalize the harness-owned roots (for example macOS /var -> /private/var).
    # Output/source children still pass the no-indirection checks below.
    repository = repository.resolve(strict=True)
    project = project.resolve(strict=True)
    artifact = HtmlArtifact.from_dict(artifact_record)
    source = read_output(html_output_path(project, artifact.artifact_path))
    if source is None or not artifact.matches_bytes(source.content):
        raise RuntimeError("HTML changed during qualification")
    output = release_file_path(repository, "_build/ci-html")
    output.mkdir(parents=True, exist_ok=True)
    retained = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    destination = release_file_path(retained, artifact.artifact_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(source.content)
        stream.flush()
        os.fsync(stream.fileno())
    copied = read_output(destination)
    if copied is None or not artifact.matches_bytes(copied.content):
        raise RuntimeError("HTML changed during retention")
    record = release_file_path(retained, "artifact.json")
    content = (json.dumps(artifact.to_dict(), sort_keys=True) + "\n").encode()
    with record.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    recorded = read_output(record)
    if recorded is None or recorded.content != content:
        raise RuntimeError("HTML artifact record changed during retention")
    return {
        "html": destination.relative_to(repository).as_posix(),
        "record": record.relative_to(repository).as_posix(),
    }


def _retain_qualified_wheel(
    repository: Path,
    wheel: Path,
    *,
    revision: str,
    version: str,
    expected_identity: str,
) -> Path:
    """Keep the tested bytes; write a portable manifest only after validation."""

    if file_identity(wheel) != expected_identity:
        raise RuntimeError("wheel changed during qualification")
    output = release_file_path(repository, "_build/ci-wheels")
    output.mkdir(parents=True, exist_ok=True)
    # Separate runs never replace the custody or evidence of an earlier run.
    retained = Path(tempfile.mkdtemp(prefix="run-", dir=output))
    destination = release_file_path(retained, wheel.name)
    shutil.copy2(wheel, destination)
    value = {
        "schema": RELEASE_FILES_SCHEMA,
        "revision": revision,
        "version": version,
        "files": [
            {
                "role": "wheel",
                "path": destination.name,
                "size": wheel.stat().st_size,
                "identity": expected_identity,
            }
        ],
    }
    value["identity"] = canonical_identity(value).uri
    validate_release_files(
        retained,
        value,
        revision=revision,
        version=version,
        required_roles=("wheel",),
    )
    manifest = release_file_path(retained, "manifest.json")
    temporary = release_file_path(retained, "manifest.tmp")
    temporary.write_bytes((json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
    temporary.replace(manifest)
    return manifest


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel", type=Path, help="qualify these already-built wheel bytes"
    )
    arguments = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    configuration = tomllib.loads(
        (repository / "pyproject.toml").read_text(encoding="utf-8")
    )
    if configuration["project"].get("dynamic") != ["version"] or configuration["tool"][
        "setuptools"
    ]["dynamic"]["version"] != {"attr": "literate_ai.version.DISTRIBUTION_VERSION"}:
        raise RuntimeError("wheel metadata does not use the single version authority")
    sys.path.insert(0, str(repository / "src"))
    try:
        from literate_ai.version import DISTRIBUTION_VERSION
    finally:
        del sys.path[0]
    declared_version = DISTRIBUTION_VERSION
    if run(
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        cwd=repository,
    ).stdout:
        raise RuntimeError(
            "installed-wheel qualification requires a clean exact Git revision"
        )
    repository_url = canonical_repository_origin(
        run("git", "remote", "get-url", "origin", cwd=repository).stdout.strip()
    )
    git_revision = run("git", "rev-parse", "HEAD", cwd=repository).stdout.strip()
    build_environment = {
        **os.environ,
        "LITAI_BUILD_REPOSITORY_URL": repository_url,
        "LITAI_BUILD_GIT_REVISION": git_revision,
    }
    with _runtime_directory() as directory:
        root = Path(directory)
        parent_source = _remotes_stripped_parent(
            repository, root / "parent.git", git_revision
        )
        source = root / "source"
        project_clean_source(repository, source)
        wheels = root / "wheels"
        wheels.mkdir()
        if arguments.wheel is None:
            run(
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--wheel-dir",
                str(wheels),
                str(source),
                cwd=root,
                env=build_environment,
            )
        else:
            supplied = arguments.wheel.resolve(strict=True)
            if (
                not supplied.name.startswith(f"literate_ai-{declared_version}-")
                or supplied.suffix != ".whl"
            ):
                raise RuntimeError(
                    "supplied wheel does not select the declared release"
                )
            shutil.copy2(supplied, wheels / supplied.name)
        wheel_files = tuple(wheels.glob("literate_ai-*.whl"))
        if len(wheel_files) != 1:
            raise RuntimeError("wheel build did not produce exactly one distribution")
        tested_wheel_identity = file_identity(wheel_files[0])
        environment = root / "environment"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
        python = scripts / ("python.exe" if sys.platform == "win32" else "python")
        litai = scripts / ("litai.exe" if sys.platform == "win32" else "litai")
        legacy = scripts / ("literate.exe" if sys.platform == "win32" else "literate")
        run(
            str(python),
            "-m",
            "pip",
            "install",
            str(wheel_files[0]),
            cwd=root,
        )
        installed_runtime_resources = json.loads(
            run(
                str(python),
                "-c",
                INSTALLED_RUNTIME_RESOURCE_PROBE,
                cwd=root,
            ).stdout
        )
        installed_evidence_signatures = json.loads(
            run(str(python), "-c", INSTALLED_EVIDENCE_SIGNATURE_PROBE, cwd=root).stdout
        )
        checkpoint_help = run(
            str(python),
            "-m",
            "literate_ai.test_checkpointing",
            "--help",
            cwd=root,
        ).stdout
        if "python" not in checkpoint_help or "gates" not in checkpoint_help:
            raise RuntimeError("installed wheel omitted the checkpoint runner contract")
        standard_binding = json.loads(
            run(str(python), "-c", STANDARD_BINDING_PROBE, cwd=root).stdout
        )
        source_admission_lifecycle = json.loads(
            run(
                str(python),
                str(repository / "scripts" / "installed_source_admission_smoke.py"),
                str(root / "source-admission-lifecycle"),
                standard_binding["distribution"],
                cwd=root,
            ).stdout
        )
        worker_litai, worker_runtime = bootstrap_real_worker_runtime(
            root, wheel_files[0], standard_binding["distribution"]
        )
        uv_tool_worker_runtime = bootstrap_uv_tool_worker_runtime(
            root, wheel_files[0], standard_binding["distribution"]
        )
        second_environment = root / "environment-two"
        venv.EnvBuilder(with_pip=True).create(second_environment)
        second_scripts = second_environment / (
            "Scripts" if sys.platform == "win32" else "bin"
        )
        second_python = second_scripts / (
            "python.exe" if sys.platform == "win32" else "python"
        )
        run(
            str(second_python),
            "-m",
            "pip",
            "install",
            str(wheel_files[0]),
            cwd=root,
        )
        second_standard_binding = json.loads(
            run(str(second_python), "-c", STANDARD_BINDING_PROBE, cwd=root).stdout
        )
        if second_standard_binding != standard_binding:
            raise RuntimeError(
                "Standard distribution or policy identity changed between "
                "clean installs"
            )
        repeated_source = root / "source-repeated"
        project_clean_source(repository, repeated_source)
        repeated_wheels = root / "wheels-repeated"
        repeated_wheels.mkdir()
        run(
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(repeated_wheels),
            str(repeated_source),
            cwd=root,
            env=build_environment,
        )
        repeated_wheel_files = tuple(repeated_wheels.glob("literate_ai-*.whl"))
        if len(repeated_wheel_files) != 1:
            raise RuntimeError(
                "repeated wheel build did not produce exactly one distribution"
            )
        repeated_environment = root / "environment-repeated-build"
        venv.EnvBuilder(with_pip=True).create(repeated_environment)
        repeated_scripts = repeated_environment / (
            "Scripts" if sys.platform == "win32" else "bin"
        )
        repeated_python = repeated_scripts / (
            "python.exe" if sys.platform == "win32" else "python"
        )
        run(
            str(repeated_python),
            "-m",
            "pip",
            "install",
            str(repeated_wheel_files[0]),
            cwd=root,
        )
        repeated_standard_binding = json.loads(
            run(str(repeated_python), "-c", STANDARD_BINDING_PROBE, cwd=root).stdout
        )
        if repeated_standard_binding != standard_binding:
            raise RuntimeError(
                "Standard distribution or policy identity changed between clean "
                "builds of the same exact revision and origin"
            )
        resolved_binding = run(
            str(second_python),
            "-c",
            (
                "import sys; "
                "from literate_ai.adapters.standard_lifecycle_binding import "
                "resolve_standard_project_lifecycle_driver; "
                "from literate_ai.contracts import (ContentIdentity, "
                "StandardProjectLifecycleDriver); "
                "d=StandardProjectLifecycleDriver(ContentIdentity.parse_uri(sys.argv[1]),"
                "ContentIdentity.parse_uri(sys.argv[2])); "
                "r=resolve_standard_project_lifecycle_driver(d); "
                "r.require_unchanged(); print(r.trust_binding.identity.uri)"
            ),
            standard_binding["distribution"],
            standard_binding["policy"],
            cwd=root,
        ).stdout.strip()
        if not resolved_binding.startswith("sha256:"):
            raise RuntimeError("clean wheel cannot resolve its exact Standard binding")
        tamper_code = run(
            str(second_python),
            "-c",
            (
                "import sys; from pathlib import Path; "
                "from importlib.resources import files; "
                "from literate_ai.adapters.standard_lifecycle_binding import "
                "(StandardLifecycleBindingError,"
                "resolve_standard_project_lifecycle_driver); "
                "from literate_ai.contracts import ("
                "CURRENT_STANDARD_LIFECYCLE_POLICY_RESOURCE,ContentIdentity,"
                "StandardProjectLifecycleDriver); "
                "d=StandardProjectLifecycleDriver(ContentIdentity.parse_uri(sys.argv[1]),"
                "ContentIdentity.parse_uri(sys.argv[2])); "
                "r=resolve_standard_project_lifecycle_driver(d); "
                "p=Path(str(files('literate_ai.standard_policies').joinpath("
                "CURRENT_STANDARD_LIFECYCLE_POLICY_RESOURCE))); b=p.read_bytes(); "
                "p.write_bytes(b+b'\\n'); "
                "\ntry:\n r.require_unchanged()\n"
                "except StandardLifecycleBindingError as e:\n print(e.code)\n"
                "else:\n raise SystemExit('tampered Standard policy was accepted')\n"
                "finally:\n p.write_bytes(b)"
            ),
            standard_binding["distribution"],
            standard_binding["policy"],
            cwd=root,
        ).stdout.strip()
        if tamper_code != "standard_binding.changed":
            raise RuntimeError(
                "Standard binding drift guard did not reject byte tampering"
            )
        if not litai.is_file() or legacy.exists():
            raise RuntimeError("installed wheel must expose only the litai CLI")
        help_text = run(str(litai), "--help", cwd=root).stdout
        if not help_text.startswith("usage: litai ") or "usage: literate " in help_text:
            raise RuntimeError("installed CLI help does not identify litai")
        command_help = run(str(litai), "help", cwd=root).stdout
        init_help = run(str(litai), "init", "help", cwd=root).stdout
        nested_help = run(str(litai), "project", "validate", "help", cwd=root).stdout
        if (
            not command_help.startswith("usage: litai ")
            or not init_help.startswith("usage: litai init")
            or not nested_help.startswith("usage: litai project validate")
        ):
            raise RuntimeError("installed CLI does not expose command-path help")
        cli_version = run(str(litai), "--version", cwd=root).stdout.strip()
        if cli_version != f"litai {declared_version}":
            raise RuntimeError("installed CLI version differs from its authority")
        version = run(
            str(python),
            "-c",
            "import literate_ai; print(literate_ai.__version__)",
            cwd=root,
        ).stdout.strip()
        if version != declared_version:
            raise RuntimeError(
                f"installed wheel reported unexpected version {version!r}"
            )
        operator_golden_paths = assert_installed_operator_golden_paths(
            litai, root, parent_source=parent_source
        )
        repository_refresh = assert_installed_repository_refresh(litai, root)
        html_observability = json.loads(
            run(
                str(python),
                "-I",
                str(repository / "scripts" / "installed_html_smoke.py"),
                str(litai),
                str(root / "golden-created"),
                standard_binding["distribution"],
                "--parent-repository",
                str(root / "parent.git"),
                cwd=root,
            ).stdout
        )
        sbom_profile = run(
            str(python),
            "-c",
            (
                "from literate_ai.adapters.dependencies import build_cyclonedx_bom; "
                "from literate_ai.contracts import (ContentIdentity, "
                "CycloneDxLifecycle, CycloneDxManagedComponent, "
                "CycloneDxManagedGraph, ManagedComponentKind, component_bom_ref); "
                "i=ContentIdentity.parse_uri('sha256:'+'b'*64); "
                "r=component_bom_ref(i); "
                "g=CycloneDxManagedGraph(r,(CycloneDxManagedComponent(r,"
                "ManagedComponentKind.ROOT,i,'component://wheel/smoke',"
                "'1.0.0',()),),(),ContentIdentity.parse_uri('sha256:'+'c'*64)); "
                "b,e=build_cyclonedx_bom(lifecycle=CycloneDxLifecycle.SOURCE,"
                "managed_graph=g); print(e.lifecycle.value)"
            ),
            cwd=root,
        ).stdout.strip()
        if sbom_profile != "pre-build":
            raise RuntimeError("installed wheel cannot validate CycloneDX 1.7 SBOMs")
        replication_schema = run(
            str(python),
            "-c",
            (
                "from literate_ai.self_hosting import "
                "SNAPSHOT_REPLICATION_REPORT_SCHEMA; "
                "print(SNAPSHOT_REPLICATION_REPORT_SCHEMA)"
            ),
            cwd=root,
        ).stdout.strip()
        if replication_schema != "literate-ai/snapshot-replication@1":
            raise RuntimeError(
                "installed wheel lacks the snapshot-replication conformance runtime"
            )
        version_contract = run(
            str(python),
            "-c",
            (
                "from literate_ai.contracts import (ComponentCoordinate, "
                "ComponentRevisionRef, ContentIdentity, SemanticVersion); "
                "ref=ComponentRevisionRef(ComponentCoordinate('wheel','smoke'), "
                "'1.2.3-rc.1', ContentIdentity.parse_uri('sha256:'+'a'*64)); "
                "assert ComponentRevisionRef.from_dict(ref.to_dict()) == ref; "
                "print(SemanticVersion.parse(ref.version))"
            ),
            cwd=root,
        ).stdout.strip()
        if version_contract != "1.2.3-rc.1":
            raise RuntimeError("installed wheel lacks exact version contracts")
        schema_catalog = json.loads(
            run(
                str(python),
                "-c",
                (
                    "import json; "
                    "from literate_ai.schema_catalog import schema_path; "
                    "print(schema_path('index.json').read_text(encoding='utf-8'))"
                ),
                cwd=root,
            ).stdout
        )
        if schema_catalog.get("catalog_id") != "urn:literate-ai:schema-catalog:v1":
            raise RuntimeError("installed wheel lacks the public JSON Schema catalog")
        schema_versions = json.loads(
            run(
                str(python),
                "-c",
                (
                    "import json; "
                    "from literate_ai.schema_catalog import (verify_published_schemas, "
                    "verify_schema_catalog); "
                    "print(json.dumps({'v1': verify_schema_catalog('v1'), "
                    "'v2': verify_schema_catalog('v2'), "
                    "'published_v1': verify_published_schemas()}, sort_keys=True))"
                ),
                cwd=root,
            ).stdout
        )
        if (
            schema_versions["published_v1"]
            != {
                "algorithm": "sha256",
                "file_count": 17,
                "release": "0.1.1",
                "source_commit": "465260cb450dc1c386e257f93ff0277a28f8666b",
                "source_tag": "v0.1.1",
            }
            or schema_versions["v2"].get("release") != declared_version
        ):
            raise RuntimeError(
                "installed wheel does not preserve both versioned schema catalogs"
            )
        version_check = json.loads(
            run(str(litai), "version", "check", "--no-project", cwd=root).stdout
        )
        if (
            not version_check["ok"]
            or not version_check["result"]["ok"]
            or version_check["result"]["distribution"]["authority_version"]
            != declared_version
        ):
            raise RuntimeError("installed CLI version check failed")
        output = run(str(litai), "spec", "skills", cwd=root).stdout
        payload = json.loads(output)
        installed = {str(item["skill_id"]) for item in payload["result"]["skills"]}
        if not REQUIRED_SOURCE_TO_SPECIFICATION_SKILLS.issubset(installed):
            raise RuntimeError(
                "installed wheel lacks required source-to-specification skills: "
                f"required {sorted(REQUIRED_SOURCE_TO_SPECIFICATION_SKILLS)}, "
                f"got {sorted(installed)}"
            )
        project = root / "initialized-project"
        initialized = json.loads(
            run(
                str(litai),
                "init",
                str(project),
                "--from",
                parent_source,
                cwd=root,
            ).stdout
        )
        initialization = initialized.get("result", {})
        if (
            not initialized["ok"]
            or initialization.get("schema") != "literate-ai/project-initialization@6"
            or initialization.get("standard_binding") != "configured"
            or not isinstance(initialization.get("initial_lock"), dict)
            or not (project / "SKILL.md").is_file()
        ):
            raise RuntimeError("installed wheel cannot initialize agent onboarding")
        initialization_origin = json.loads(
            (project / ".literate/initialization-origin.json").read_text(
                encoding="utf-8"
            )
        )
        if (
            initialization_origin.get("repository_url") != repository_url
            or initialization_origin.get("git_revision") != git_revision
        ):
            raise RuntimeError("installed wheel lost its exact Git build origin")
        manifest = json.loads(
            (project / "literate.project.json").read_text(encoding="utf-8")
        )
        host_flavor = (
            "windows"
            if sys.platform == "win32"
            else "macos"
            if sys.platform == "darwin"
            else "linux"
        )
        host_selector = f"+flavor://literate-ai/os-{host_flavor}"
        expected_default_selectors = [
            "+flavor://literate-ai/lang-python",
            "+flavor://literate-ai/build-make",
            "+flavor://literate-ai/package-pip",
            host_selector,
        ]
        selected_directories = (
            "lang-python",
            "build-make",
            "package-pip",
            f"os-{host_flavor}",
        )
        starter_assets = (
            "CHANGELOG.md",
            "literate.release.json",
            "docs/roadmap/active-work.md",
            "docs/user/test-matrix.md",
            "literate.test.example.json",
            "literate.workers.example.json",
            "samples/hello-component/component.md",
            "samples/hello-component/component.lock.json",
            "skills/specification-to-source/portable-application/SKILL.md",
            "skills/specification-to-source/python-portable-application/SKILL.md",
            "skills/specification-to-source/python-repository-layout/SKILL.md",
            "skills/specification-to-source/repository-layout/SKILL.md",
            "skills/specification-to-source/make-build-system/SKILL.md",
            "skills/agent/record-user-directed-work/SKILL.md",
            "skills/agent/release-project/SKILL.md",
            "flavors/build-make/standard-command-profile.json",
            *(f"flavors/{item}/flavor.md" for item in selected_directories),
            *(f"flavors/{item}/openspec/spec.md" for item in selected_directories),
        )
        if manifest.get(
            "default_flavor_selectors"
        ) != expected_default_selectors or not all(
            (project / relative).is_file() for relative in starter_assets
        ):
            raise RuntimeError(
                "installed wheel lacks its complete portable starter authority"
            )
        scoped = root / "scoped-shape"
        scoped_init = json.loads(
            run(
                str(litai),
                "init",
                str(scoped),
                "--empty",
                "--from",
                parent_source,
                "--flavor",
                "python",
                "--flavor",
                "macos",
                cwd=root,
            ).stdout
        )
        if not scoped_init.get("ok"):
            raise RuntimeError(
                "installed wheel cannot initialize an explicit Flavor shape"
            )
        if (
            not (scoped / "flavors" / "lang-python" / "flavor.md").is_file()
            or not (scoped / "flavors" / "os-macos" / "flavor.md").is_file()
        ):
            raise RuntimeError(
                "explicit python+macos init omitted a declared Flavor directory"
            )
        scoped_manifest = json.loads(
            (scoped / "literate.project.json").read_text(encoding="utf-8")
        )
        if scoped_manifest.get("default_flavor_selectors") != [
            "+flavor://literate-ai/lang-python",
            "+flavor://literate-ai/os-macos",
            "+flavor://literate-ai/build-make",
            "+flavor://literate-ai/package-pip",
        ]:
            raise RuntimeError(
                "explicit python+macos init selected an unexpected Flavor shape"
            )
        lifecycle_driver = manifest.get("lifecycle_driver")
        receipt_policy = manifest.get("test_receipt_policy")
        if (
            not isinstance(lifecycle_driver, dict)
            or lifecycle_driver.get("binding") != "standard"
            or lifecycle_driver.get("framework_distribution_identity", {}).get("digest")
            != standard_binding["distribution"].removeprefix("sha256:")
            or lifecycle_driver.get("policy_identity", {}).get("digest")
            != standard_binding["policy"].removeprefix("sha256:")
            or not isinstance(receipt_policy, dict)
        ):
            raise RuntimeError("initialized project lacks its exact Standard binding")
        standard_project_version_check = assert_standard_project_version_check(
            litai, project
        )
        source_intelligence = manifest.get("source_intelligence")
        if source_intelligence != {
            "schema": ("urn:literate-ai:schema:v1:project-source-intelligence-policy"),
            "provider_id": "none",
            "command": None,
            "minimum_version": None,
            "artifact_path": None,
            "stages": {
                "project-maintenance": "off",
                "source-generation": "off",
                "cache-consumption": "off",
                "source-to-specification": "off",
                "repository-source-admission": "off",
                "structural-review": "off",
            },
            "artifact_publication": "metadata-only",
        }:
            raise RuntimeError(
                "installed wheel does not default source intelligence to none"
            )
        validated = json.loads(
            run(str(litai), "project", "validate", str(project), cwd=root).stdout
        )
        if not validated["ok"] or validated["result"]["profile"] != "canonical":
            raise RuntimeError("installed wheel cannot validate its project template")
        graph_exports = assert_installed_graph_exports(
            litai,
            project,
            expected_graph=validated["result"]["authority_graph"],
        )
        lifecycle_codegraph_evidence = (
            assert_lifecycle_does_not_require_project_codegraph(
                worker_litai or litai, project
            )
        )
        update = json.loads(run(str(litai), "update", str(project), cwd=root).stdout)
        update_result = update.get("result", {})
        framework_update = update_result.get("framework", {})
        lineage_update = update_result.get("repository_lineage", {})
        upstream_origin = framework_update.get("upstream_origin", {})
        if (
            not update.get("ok")
            or update_result.get("schema") != "literate-ai/composite-project-update@1"
            or lineage_update.get("schema")
            != "urn:literate-ai:schema:v1:repository-lineage-update-plan"
            or upstream_origin.get("repository_url") != repository_url
            or upstream_origin.get("git_revision") != git_revision
        ):
            raise RuntimeError(
                "installed wheel cannot plan against its exact initialization origin: "
                f"{update_result!r}"
            )
        reparent = json.loads(
            run(
                str(litai),
                "reparent",
                parent_source,
                "--project",
                str(project),
                cwd=root,
            ).stdout
        )
        if not reparent.get("ok") or reparent.get("result", {}).get("changed"):
            raise RuntimeError(
                "installed wheel cannot plan an identical repository parent as a no-op"
            )
        after_plans = json.loads(
            run(
                str(litai),
                "graph",
                "--project",
                str(project),
                "--format",
                "json",
                cwd=root,
            ).stdout
        )
        if (
            after_plans.get("result", {}).get("graph")
            != validated["result"]["authority_graph"]
        ):
            raise RuntimeError(
                "update or reparent planning changed effective project authority"
            )
        resolved_flavors = {item["id"] for item in validated["result"]["flavors"]}
        resolved_skills = {
            item["skill_id"]
            for item in validated["result"]["specification_to_source_skills"]
        }
        resolved_components = {
            item["coordinate"] for item in validated["result"]["components"]
        }
        selected_flavors = {"lang-python", "build-make", f"os-{host_flavor}"}
        if (
            not selected_flavors.issubset(resolved_flavors)
            or not resolved_skills
            or "component://samples/hello-component" not in resolved_components
        ):
            raise RuntimeError(
                "installed wheel cannot resolve its starter authority: "
                f"selected flavors {sorted(selected_flavors)!r}, "
                f"resolved flavors {sorted(resolved_flavors)!r}, "
                f"skills {sorted(resolved_skills)!r}, "
                f"components {sorted(resolved_components)!r}"
            )
        locked = json.loads(
            run(
                str(litai),
                "lock",
                "samples/hello-component",
                "--check",
                cwd=project,
            ).stdout
        )
        if not locked["ok"] or not locked["result"]["current"]:
            raise RuntimeError("installed starter lock is not current")

        add_selected_flavor_packaging_provider(project)
        provider_lock = json.loads(
            run(
                str(litai),
                "lock",
                "components/wheel-package-publisher",
                cwd=project,
            ).stdout
        )
        selected_lock = json.loads(
            run(str(litai), "lock", "samples/hello-component", cwd=project).stdout
        )
        if not provider_lock.get("ok") or not selected_lock.get("ok"):
            raise RuntimeError(
                "installed wheel cannot lock a selected-Flavor packaging provider"
            )
        run(
            str(python),
            "-c",
            (
                "import sys; "
                "from pathlib import Path; "
                "from literate_ai.adapters.project_initialization import "
                "record_project_authority_review; "
                "record_project_authority_review(Path(sys.argv[1]))"
            ),
            str(project),
            cwd=root,
        )
        selected_validation = json.loads(
            run(str(litai), "project", "validate", str(project), cwd=root).stdout
        )
        if (
            not selected_validation.get("ok")
            or selected_validation.get("result", {}).get("profile") != "canonical"
        ):
            raise RuntimeError(
                "installed wheel cannot validate the selected-Flavor provider project"
            )
        starter_lock = json.loads(
            (project / "samples" / "hello-component" / "component.lock.json").read_text(
                encoding="utf-8"
            )
        )
        root_node = next(
            (
                node
                for node in starter_lock["nodes"]
                if node["revision"]["coordinate"]["name"] == "hello-component"
            ),
            None,
        )
        provider_node = next(
            (
                node
                for node in starter_lock["nodes"]
                if node["revision"]["coordinate"]["name"] == "wheel-package-publisher"
            ),
            None,
        )
        flavor_requirement = (
            None
            if root_node is None
            else next(
                (
                    item
                    for item in root_node["revision"].get("flavor_requirements", [])
                    if item["requirement"]["requirement_id"]
                    == "publish-wheel-smoke-package"
                ),
                None,
            )
        )
        packaging_edge = next(
            (
                edge
                for edge in starter_lock["edges"]
                if edge["requirement_id"] == "publish-wheel-smoke-package"
            ),
            None,
        )
        flavor_name = (
            None
            if flavor_requirement is None
            else flavor_requirement["flavor_revision"]["definition"]["coordinate"][
                "name"
            ]
        )
        if (
            root_node is None
            or provider_node is None
            or flavor_requirement is None
            or flavor_name != "lang-python"
            or packaging_edge is None
            or packaging_edge["kind"] != "packaging"
            or packaging_edge["consumer_revision"] != starter_lock["root_revision"]
            or packaging_edge["provider_revision"] == starter_lock["root_revision"]
        ):
            raise RuntimeError(
                "installed wheel did not preserve the exact selected-Flavor "
                "packaging requirement and edge: "
                f"root_present={root_node is not None}, "
                f"provider_present={provider_node is not None}, "
                f"flavor={flavor_name!r}, edge={packaging_edge!r}, "
                f"root_revision={starter_lock['root_revision']!r}"
            )
        starter_lock_flavors = [
            selected["value"]
            for slot in root_node["target_flavor_selection"]["slots"]
            for selected in slot["selected"]
        ]
        expected_lock_flavors = [
            "make",
            "python",
            host_flavor,
            "pip",
        ]
        if starter_lock_flavors != expected_lock_flavors:
            raise RuntimeError(
                "installed starter lock does not select the complete default build, "
                "language, host, and package Flavors"
            )
        planned = json.loads(
            run(str(litai), "plan", "samples/hello-component", cwd=project).stdout
        )
        if (
            not planned["ok"]
            or not planned["result"]["standard_component_execution"]["generation_plans"]
        ):
            raise RuntimeError("installed starter Component cannot be planned")
        if (
            run("git", "rev-parse", "HEAD", cwd=repository).stdout.strip()
            != git_revision
            or run(
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                cwd=repository,
            ).stdout
        ):
            raise RuntimeError(
                "repository changed during installed-wheel qualification"
            )
        html_observability["retained"] = _retain_qualified_html(
            repository, root / "golden-created", html_observability["artifact"]
        )
        html_observability["update"]["retained"] = _retain_qualified_html(
            repository,
            root / "golden-created",
            html_observability["update"]["artifact"],
        )
        html_observability["version_health"]["retained"] = _retain_qualified_html(
            repository,
            root / "golden-created",
            html_observability["version_health"]["artifact"],
        )
        html_observability["lock_health"]["retained"] = _retain_qualified_html(
            repository,
            root / "golden-created",
            html_observability["lock_health"]["artifact"],
        )
        for state in html_observability["lock_health"]["states"]:
            state["retained"] = _retain_qualified_html(
                repository, root / "golden-created", state["artifact"]
            )
        for report in (
            html_observability["verification_health"],
            html_observability["verification_health"]["failed"],
        ):
            report["retained"] = _retain_qualified_html(
                repository, root / "golden-created", report["artifact"]
            )
        retained_manifest = _retain_qualified_wheel(
            repository,
            wheel_files[0],
            revision=git_revision,
            version=declared_version,
            expected_identity=tested_wheel_identity,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "version": version,
                    "wheel": wheel_files[0].name,
                    "wheel_identity": tested_wheel_identity,
                    "retained_manifest": retained_manifest.relative_to(
                        repository
                    ).as_posix(),
                    "operator_golden_paths": operator_golden_paths,
                    "repository_refresh": repository_refresh,
                    "html_observability": html_observability,
                    "skills": sorted(installed),
                    "snapshot_replication_schema": replication_schema,
                    "version_contract": version_contract,
                    "sbom_profile": sbom_profile,
                    "schema_catalog": schema_catalog["catalog_id"],
                    "published_schemas": schema_versions["published_v1"],
                    "runtime_resources": installed_runtime_resources,
                    "evidence_signature_primitives": installed_evidence_signatures,
                    "schema_catalog_versions": sorted(
                        key for key in schema_versions if key.startswith("v")
                    ),
                    "version_check": version_check["result"]["ok"],
                    "project_profile": validated["result"]["profile"],
                    "project_graph_exports": graph_exports,
                    "project_default_flavors": manifest["default_flavor_selectors"],
                    "project_source_intelligence": source_intelligence,
                    "project_codegraph_lifecycle": lifecycle_codegraph_evidence,
                    "worker_runtime": worker_runtime,
                    "uv_tool_worker_runtime": uv_tool_worker_runtime,
                    "project_update_origin": upstream_origin,
                    "project_reparent_noop": True,
                    "project_standard_binding": lifecycle_driver,
                    "project_standard_version_check": standard_project_version_check,
                    "starter_lock_current": locked["result"]["current"],
                    "starter_lock_flavors": starter_lock_flavors,
                    "selected_flavor_packaging_provider": {
                        "capability": packaging_edge["capability"],
                        "flavor": "python",
                        "provider": "component://example/wheel-package-publisher",
                        "requirement": packaging_edge["requirement_id"],
                    },
                    "starter_recipe_identity": planned["result"]["recipe_identity"],
                    "source_admission_lifecycle": source_admission_lifecycle,
                    "standard_binding": standard_binding,
                    "standard_binding_tamper_code": tamper_code,
                },
                sort_keys=True,
            )
        )
    return 0


def main() -> int:
    run = attach_run()
    if run is None:
        return _main()
    context = run.node(
        "installed/wheel-smoke",
        operation="installed.wheel-smoke",
        parent=os.environ.get("LITAI_EVIDENCE_PARENT"),
    )
    with context as node:
        status = _main()
        if status and isinstance(node, EvidenceNode):
            node.fail(f"wheel smoke exited with status {status}")
        return status


if __name__ == "__main__":
    raise SystemExit(main())
