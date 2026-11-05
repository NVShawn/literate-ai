"""Real original-source project; optional paired source-distribution observations.

Installed journeys omit both observations and use the installed defaults. No
fixture object supplies admission, completed-run observations or policy checks.
"""

import hashlib
import json
import os
import shutil
from dataclasses import replace
from types import SimpleNamespace

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.component_lock_application import (
    current_project_component_locks,
)
from literate_ai.adapters.project_initialization import (
    FilesystemProjectInitializationAdapter,
    host_platform_selector,
)
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_receipt_promotion import (
    retained_project_runner_identity,
)
from literate_ai.contracts import (
    ContentIdentity,
    ProjectInitializationOrigin,
    ProjectTestReceiptPolicy,
    RepositoryParentSelection,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptanceCase,
    RetainedProjectAcceptancePolicy,
)
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectProfile,
)
from literate_ai.contracts.retained_project_tests import (
    RetainedProjectExpectedCase,
    RetainedProjectTestPolicy,
)
from literate_ai.projects import load_project

KINDS = (
    "acceptance-result",
    "build-result",
    "resolved-sbom",
    "source-sbom",
    "test-report",
    "test-runner",
    "workspace-admission",
)
NATIVE_SOURCE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
static int copy(const char *a,const char *b) {
 FILE *in=fopen(a,"rb"),*out=fopen(b,"wb"); if(!in||!out) return 4;
 int c; while((c=fgetc(in))!=EOF) if(fputc(c,out)==EOF) return 5;
 if(ferror(in)||fclose(in)||fclose(out)) return 6; return chmod(b,0755)?7:0;
}
static int same(const char *a,const char *b) {
 FILE *x=fopen(a,"rb"),*y=fopen(b,"rb"); if(!x||!y) return 0;
 int c,d; do { c=fgetc(x); d=fgetc(y); if(c!=d)return 0; } while(c!=EOF);
 int ok=!ferror(x)&&!ferror(y); fclose(x);fclose(y);return ok;
}
int main(int n,char **v) {
 if(n==2&&!strcmp(v[1],"echo")) {
  int c; while((c=getchar())!=EOF) if(putchar(c)==EOF)return 9;
  return ferror(stdin)?10:0;
 }
 if(n==3&&!strcmp(v[1],"export")) return copy(v[0],v[2]);
 if(n==4&&!strcmp(v[1],"test")) {
  if(!same(v[0],v[2]))return 11;
  FILE *f=fopen(v[3],"wb"); if(!f)return 12;
  fputs("{\"schema\":\"literate-ai/retained-project-test-report@1\","
        "\"action\":\"tests\",\"cases\":[{\"id\":\"original-suite\","
        "\"outcome\":\"passed\",\"reason\":null}]}",f);
  return fclose(f)?13:0;
 }
 if(n==3&&!strcmp(v[1],"docs")) {
  FILE *f=fopen(v[2],"wb");if(!f)return 14;
  fputs("Original native suite and independent byte-stream oracle.\n",f);
  return fclose(f)?15:0;
 }
 if(n==2&&!strcmp(v[1],"empty"))return 0;
 return 16;
}
"""


def _identity(raw):
    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(raw).hexdigest())


def _invoke(invoke, arguments):
    status, result = invoke(arguments)
    if status:
        raise AssertionError((arguments, status, result))
    return result["result"]


def author_public_retained_project(root, invoke, *, driver=None, receipt=None):
    """Author intent first; only the actual public run issues retained evidence."""
    if (driver is None) != (receipt is None):
        raise TypeError("source framework observations must be paired")
    observations = {}
    if driver is not None:
        observations = {
            "standard_binding_provider": lambda: (driver, receipt),
            "initialization_origin_provider": lambda: ProjectInitializationOrigin(
                "https://github.com/jordanhubbard/literate-ai.git",
                "a" * 40,
                "literate-ai",
                "1.4.0",
            ),
        }
    project = root / "project"
    FilesystemProjectInitializationAdapter(**observations).initialize(
        project,
        flavor_selectors=("+python", host_platform_selector()),
        parent_selection=RepositoryParentSelection.root(),
        bootstrap_tools=False,
        default_branch="main",
    )
    for component_root in load_project(project).definition.component_roots:
        for component in sorted((project / component_root).rglob("component.md")):
            _invoke(invoke, ["lock", str(component.parent)])
    locks = current_project_component_locks(load_project(project))
    for name in (
        "original",
        "tools",
        "build",
        "tests",
        "verification",
        "retained-docs",
    ):
        (project / name).mkdir(exist_ok=True)
    (project / "retained-docs/commands.md").write_text(
        "# Original documentation command\n\n"
        "The docs stage MUST invoke the sealed original runner as `docs` with "
        "the declared docs.txt output path. It MUST write the UTF-8 line "
        "`Original native suite and independent byte-stream oracle.` followed "
        "by one LF. This original command definition is retained input.\n"
    )
    source = project / "original/runner.c"
    source.write_text(NATIVE_SOURCE)
    tool = project / "tools/runner"
    compiler = shutil.which("cc")
    if compiler is None or os.name == "nt":
        raise RuntimeError("real native POSIX C compiler required")
    result = run_bounded_process(
        [compiler, "-O0", str(source), "-o", str(tool)],
        cwd=project,
        environment=dict(os.environ),
        timeout_seconds=60,
        stdout_limit_bytes=65536,
        stderr_limit_bytes=65536,
        terminate_descendants=True,
        trace=False,
        error_prefix="promotion.fixture.compile",
    )
    (root / "native-compile.stdout").write_bytes(result.stdout)
    (root / "native-compile.stderr").write_bytes(result.stderr)
    if result.returncode:
        raise AssertionError((result.returncode, result.stderr))
    tool.chmod(0o755)
    (project / "tests/suite.md").write_text(
        "# Original native suite\n\nThe test MUST compare the sealed application "
        "byte for byte with the actual compiled original runner. Independent "
        "acceptance MUST echo the exact withheld input and emit no stderr.\n"
    )
    policy = ProjectTestReceiptPolicy(
        "original-native-suite",
        "1.0.0",
        canonical_identity("unbound runner intent"),
        KINDS,
        1,
    )
    tests = RetainedProjectTestPolicy(
        policy.identity,
        (RetainedProjectExpectedCase("original-suite", "tests"),),
        (("tests", "test-result.json"),),
    )
    test_path = project / "test-policy.json"
    test_path.write_bytes(canonical_json_bytes(tests.to_dict()))
    acceptance = RetainedProjectAcceptancePolicy(
        tuple(lock.identity for lock in locks),
        (
            RetainedProjectAcceptanceCase(
                "exact-original-stream",
                "accept",
                "app.bin",
                ("echo",),
                b"withheld\x00original\n",
                0,
                b"withheld\x00original\n",
                b"",
                10,
            ),
        ),
    )
    acceptance_raw = canonical_json_bytes(acceptance.to_dict())
    (project / "verification/retained-project-acceptance.json").write_bytes(
        acceptance_raw
    )
    actions = (
        RetainedProjectAction(
            "build",
            "build",
            "runner",
            ("export", "{output_root}/app.bin"),
            (),
            ("native-product",),
            ("app.bin",),
            20,
        ),
        RetainedProjectAction(
            "tests",
            "test",
            "runner",
            ("test", "{output_root}/app.bin", "{output_root}/test-result.json"),
            ("build",),
            ("original-suite",),
            ("test-result.json",),
            20,
        ),
        RetainedProjectAction(
            "docs",
            "docs",
            "runner",
            ("docs", "{output_root}/docs.txt"),
            ("build",),
            ("original-docs",),
            ("docs.txt",),
            20,
        ),
        RetainedProjectAction(
            "accept",
            "accept",
            "runner",
            ("empty",),
            ("tests", "docs"),
            ("exact-original-stream",),
            (),
            20,
        ),
    )
    roots = {
        "original": "source",
        "tools": "toolchain",
        "build": "build-definition",
        "tests": "test-definition",
        "retained-docs": "docs-definition",
        "test-policy.json": "test-policy",
        "literate.release.json": "release-policy",
    }
    limits = RetainedProjectLimits(100, 8 * 1024**2, 16 * 1024**2)
    profile_path = project / "build/profile.json"
    profile_path.write_text("{}")
    manifest = discover_retained_project_inputs(
        project,
        roots,
        limits,
        executable_paths=("tools/runner",),
    )
    profile = RetainedProjectProfile(
        "host",
        ExecutionWorker("local", ExecutionWorkerKind.LOCAL).identity,
        manifest.role_identity({"sdk", "toolchain"}),
        manifest.role_identity({"dependency"}),
        _identity(test_path.read_bytes()),
        _identity((project / "literate.release.json").read_bytes()),
        (("runner", "tools/runner", _identity(tool.read_bytes())),),
        actions,
        ("app.bin", "docs.txt"),
        _identity(acceptance_raw),
    )
    runner = retained_project_runner_identity(
        SimpleNamespace(profile=profile), manifest, tests
    )
    policy = replace(policy, runner_identity=runner)
    tests = replace(tests, project_policy_identity=policy.identity)
    test_path.write_bytes(canonical_json_bytes(tests.to_dict()))
    profile = replace(profile, test_policy_identity=_identity(test_path.read_bytes()))
    profile_path.write_bytes(canonical_json_bytes(profile.to_dict()))
    manifest = discover_retained_project_inputs(
        project,
        roots,
        limits,
        executable_paths=("tools/runner",),
    )
    if (
        retained_project_runner_identity(
            SimpleNamespace(profile=profile), manifest, tests
        )
        != runner
    ):
        raise AssertionError("only the project-policy cycle was omitted")
    (project / "manifest.json").write_bytes(canonical_json_bytes(manifest.to_dict()))
    configuration = json.loads((project / "literate.project.json").read_bytes())
    configuration["test_receipt_policy"] = policy.to_dict()
    (project / "literate.project.json").write_bytes(canonical_json_bytes(configuration))
    _invoke(invoke, ["project", "documentation-review", str(project), "--record"])
    for argv in (
        ["init", "-q", "--initial-branch=main"],
        ["add", "--all"],
        [
            "-c",
            "user.name=Native Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--quiet",
            "-m",
            "Reviewed original suite",
        ],
    ):
        result = run_bounded_process(
            [
                "git",
                "-c",
                "maintenance.auto=false",
                "-c",
                "gc.auto=0",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-C",
                str(project),
                *argv,
            ],
            cwd=project,
            environment=dict(os.environ),
            timeout_seconds=30,
            stdout_limit_bytes=65536,
            stderr_limit_bytes=65536,
            terminate_descendants=True,
            trace=False,
            error_prefix="promotion.fixture.git",
        )
        if result.returncode:
            raise AssertionError((result.returncode, result.stderr))
    adoption = _invoke(invoke, ["project", "adopt-original", "--project", str(project)])
    _invoke(
        invoke,
        [
            "project",
            "adopt-original",
            "--project",
            str(project),
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            adoption["plan_identity"],
        ],
    )
    return project, policy


def forge_public_run_evidence(store, retained_receipt, promotion_reference, *, kind):
    """Derive hostile coherent CAS facts from an ACTUAL run, never a live owner.

    Every affected JSON reference and manifest hash is recomputed. This bypasses
    a simple digest-corruption refusal so the public verifier must check the
    actual report/oracle semantics. Original blobs are never overwritten.
    """
    from literate_ai.contracts import BlobRef

    receipt_raw = canonical_json_bytes(retained_receipt)
    receipt_ref = BlobRef(
        hashlib.sha256(receipt_raw).hexdigest(),
        len(receipt_raw),
        media_type="application/json",
    )
    if store.get_bytes(receipt_ref) != receipt_raw:
        raise AssertionError("only the actual retained receipt may seed a forgery")
    references = [BlobRef.from_dict(row) for row in retained_receipt["records"]]
    root = json.loads(store.get_bytes(promotion_reference))
    run_evidence = json.loads(
        store.get_bytes(BlobRef.from_dict(root["accepted_run_evidence"]))
    )
    references.extend(
        BlobRef.from_dict(row)
        for row in run_evidence["command_output_manifests"]
        if row is not None
    )
    for name in ("input-manifest", "output-manifest", "payload-manifest"):
        document = json.loads(
            store.get_bytes(BlobRef.from_dict(retained_receipt["roots"][name]))
        )
        references.extend(
            BlobRef(row["sha256"], row["size"]) for row in document["members"]
        )
    references = list({row.identity: row for row in references}.values())
    if len(references) > 1024 or sum(row.size for row in references) > 32 * 1024**2:
        raise AssertionError("bounded small native forgery fixture required")
    by_identity = {row.identity: row for row in references}

    def read(reference):
        return json.loads(store.get_bytes(reference))

    completed = read(BlobRef.from_dict(retained_receipt["roots"]["completed-run"]))
    if kind == "report":
        observation = read(by_identity[completed["test_observations"][0]])
        target = by_identity[observation["blob_identity"]]
        actual_report = read(target)
        actual_report["cases"][0]["outcome"] = "failed"
        raw = canonical_json_bytes(actual_report)
    elif kind == "nonobject-record":
        target = by_identity[completed["command_observations"][0]]
        raw = canonical_json_bytes([])
    elif kind in {"stdout", "stderr"}:
        observation = read(by_identity[completed["acceptance_observations"][0]])
        target = by_identity[observation[kind + "_identity"]]
        raw = b"same exit code, wrong actual " + kind.encode()
    else:
        raise AssertionError("closed forgery scenario required")
    replacement = store.put_bytes(raw, media_type=target.media_type)
    replacements = {target.identity: replacement}
    documents = {}
    for reference in references:
        if reference.identity == target.identity:
            continue
        try:
            value = read(reference)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, (dict, list)):
            documents[reference.identity] = (reference, value)

    def transform(value):
        if isinstance(value, dict):
            if set(value) == {"algorithm", "digest", "size", "media_type"}:
                reference = BlobRef.from_dict(value)
                replacement = replacements.get(reference.identity)
                if replacement is not None:
                    return replacement.to_dict()
            result = {key: transform(item) for key, item in value.items()}
            if isinstance(value.get("sha256"), str) and "size" in value:
                replacement = replacements.get("sha256:" + value["sha256"])
                if replacement is not None:
                    result["size"] = replacement.size
            return result
        if isinstance(value, list):
            return [transform(item) for item in value]
        if isinstance(value, str):
            replacement = replacements.get(value)
            if replacement is not None:
                return replacement.identity
            replacement = replacements.get("sha256:" + value)
            if replacement is not None:
                return replacement.digest
        return value

    for _ in range(64):
        changed = False
        for identity, (reference, value) in documents.items():
            altered = transform(value)
            if altered == value:
                continue
            updated = store.put_bytes(
                canonical_json_bytes(altered),
                media_type=reference.media_type,
            )
            if replacements.get(identity) != updated:
                replacements[identity] = updated
                changed = True
        if not changed:
            break
    else:
        raise AssertionError("native metadata dependency graph did not converge")
    forged_receipt = transform(retained_receipt)
    forged_receipt["records"] = sorted(
        {row["digest"]: row for row in forged_receipt["records"]}.values(),
        key=lambda row: row["digest"],
    )
    new_receipt = store.put_bytes(
        canonical_json_bytes(forged_receipt),
        media_type="application/json",
    )
    replacements[receipt_ref.identity] = new_receipt
    root = read(promotion_reference)
    for name in ("planning_evidence", "accepted_run_evidence"):
        reference = BlobRef.from_dict(root[name])
        replacements[reference.identity] = store.put_bytes(
            canonical_json_bytes(transform(read(reference))),
            media_type=reference.media_type,
        )
    reference = BlobRef.from_dict(root["workspace_admission"])
    replacements[reference.identity] = store.put_bytes(
        canonical_json_bytes(transform(read(reference))),
        media_type=reference.media_type,
    )
    new_bundle = store.put_bytes(
        canonical_json_bytes(transform(root)),
        media_type="application/json",
    )
    return new_receipt, new_bundle


def refresh_public_retained_manifest(project):
    """Re-author the fixture's exact declared inputs after an intentional edit."""
    manifest = discover_retained_project_inputs(
        project,
        {
            "original": "source",
            "tools": "toolchain",
            "build": "build-definition",
            "tests": "test-definition",
            "retained-docs": "docs-definition",
            "test-policy.json": "test-policy",
            "literate.release.json": "release-policy",
        },
        RetainedProjectLimits(100, 8 * 1024**2, 16 * 1024**2),
        executable_paths=("tools/runner",),
    )
    (project / "manifest.json").write_bytes(canonical_json_bytes(manifest.to_dict()))
