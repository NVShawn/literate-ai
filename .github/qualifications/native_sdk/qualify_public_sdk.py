"""Exercise an authored retained consumer through the installed public CLI.

No test discovery, binding, transport, producer, command or acceptance mocks.
The observed host compiler is bootstrap tooling, not a source-built compiler SDK.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
)
from literate_ai.adapters.native_sdk_acquisition import NativeSdkProjectAcquisition
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.contracts import (
    ContentIdentity,
    DependencyKind,
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceDependency,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.executable_components.commands import (
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.native_sdks import NativeSdkBuildLayout, NativeSdkBuildRecipe
from literate_ai.contracts.repositories import RepositoryBuildCommand
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptanceCase,
    RetainedProjectAcceptancePolicy,
)
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectProfile,
)
from literate_ai.contracts.retained_project_sdks import (
    RetainedProjectSdkSelectionBinding,
    RetainedProjectSdkSelectionPolicy,
)
from literate_ai.contracts.retained_project_tests import (
    RetainedProjectExpectedCase,
    RetainedProjectTestPolicy,
)
from literate_ai.projects import load_project
from literate_ai.security import AuthorizationRevocationSet, SecurityPolicy

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--project", type=Path, required=True)
p.add_argument("--source-url", required=True)
p.add_argument("--source-commit", required=True)
p.add_argument("--source-license", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
root = a.project.resolve(strict=True)
output = a.output.resolve()
output.mkdir(mode=0o700)
policy_before = json.loads((root / "literate.project.json").read_text())[
    "test_receipt_policy"
]
assert len(policy_before["required_evidence_kinds"]) == 16


def write(path, content):
    path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(0o644)


def checkpoint(stage, **fields):
    (output / "progress.json").write_text(
        json.dumps({"stage": stage, **fields}, indent=2) + "\n"
    )
    print(stage, flush=True)


def cli(name, argv, expected=0, timeout=900):
    checkpoint(name)
    stdout_path, stderr_path = output / (name + ".stdout"), output / (name + ".stderr")
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        result = subprocess.run(
            [sys.executable, "-I", "-m", "literate_ai.cli", "--json", *argv],
            cwd=root,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout,
        )
    response = json.loads(stdout_path.read_bytes() or stderr_path.read_bytes())
    assert result.returncode == expected, (name, result.returncode, response)
    return response["result"] if expected == 0 else response


component = root / "components/vendor-consumer"
component.mkdir()
contract = (
    b"The source SDK exposes vendor_math_scale(double,double) as a C ABI and "
    b"vendor_math.scale(value,factor) as a Python binding. Both return the product.\n"
)
write("components/vendor-consumer/integration.md", contract)
interface = ContentIdentity.parse_uri("sha256:" + hashlib.sha256(contract).hexdigest())
dependency = RepositorySourceDependency(
    "vendor-sdk",
    a.source_url,
    RepositoryRevisionSelector(RepositoryRevisionKind.COMMIT, a.source_commit),
    DependencyKind.BUILD,
)
dep = dependency.to_dict()
dep.pop("schema")
dep["integration_contract"] = {
    "kind": "integration-contract",
    "uri": "integration.md",
    "pin": None,
}
document = {
    "namespace": "qualification",
    "version": "1.0.0",
    "display_name": "Original native SDK consumer",
    "profiles": ["retained-contract"],
    "sample": True,
    "provides": [
        {"name": "qualification.native-consumer", "version": "1.0.0"},
        {"name": "sample.portable-app", "version": "1.0.0"},
    ],
    "requires": [],
    "authoring_inputs": [
        {
            "kind": "specification-to-source-skill",
            "uri": "skills/specification-to-source/"
            "python-portable-application/SKILL.md",
        }
    ],
    "workflow_definition": "workflows/sample-host.md",
    "routing_policy": "routing/sample-host.json",
    "flavor_slots": [
        {
            "slot_id": slot,
            "axis": axis,
            "cardinality": "exactly-one",
            "capability_contract": "sample.portable-app",
        }
        for slot, axis in [
            ("language", "implementation.language-ecosystem"),
            ("os", "platform.os"),
        ]
    ],
    "entrypoints": [],
    "acceptance_contracts": ["integration.md"],
    "source_dependencies": [dep],
}
write(
    "components/vendor-consumer/component.md",
    render_authoring_markdown(
        document,
        "# Original native SDK consumer\n\nRetain and compile src/main.c with the "
        "selected observed bootstrap compiler. Execute the exact source-built SDK "
        "C ABI. Tests must observe actual positive, negative and zero products; "
        "independent acceptance holds separate operands and invalid argument cases. "
        "Documentation must describe that public behavior. This microfixture does "
        "not qualify a source-built compiler or an operating-system release.\n",
    ),
)
layout = NativeSdkBuildLayout(
    "_build/sdk",
    "python",
    LibraryImportSurface(
        "python",
        "vendor_math",
        (LibraryCapabilityImport("vendor.math", interface, "vendor_math", ("scale",)),),
    ),
    (
        "python/vendor_math/lib/vendor_factor.so",
        "python/vendor_math/lib/vendor_math.so",
    ),
    "LICENSE",
    "LICENSE",
    ContentIdentity.parse_uri(
        "sha256:" + hashlib.sha256(a.source_license.read_bytes()).hexdigest()
    ),
    "freebsd",
    "x86_64",
)
recipe = NativeSdkBuildRecipe(
    "vendor-sdk",
    layout,
    ("cmake",),
    (
        RepositoryBuildCommand("configure", ("cmake", "-S", ".", "-B", "_build")),
        RepositoryBuildCommand(
            "build",
            ("cmake", "--build", "_build", "--config", "Release", "--parallel", "2"),
        ),
    ),
)
write(
    "flavors/lang-python/native-sdk-qualification.json",
    canonical_json_bytes(recipe.to_dict()),
)
flavor = root / "flavors/lang-python/flavor.md"
fd, body = parse_authoring_markdown(flavor.read_bytes(), source=str(flavor))
fd["contributions"].append(
    {
        "contribution_id": "native-sdk-qualification",
        "kind": "builder",
        "merge_operator": "exact-singleton",
        "slot": "native-sdk:vendor-sdk",
        "content": {
            "kind": "native-sdk-build-recipe",
            "uri": "native-sdk-qualification.json",
        },
    }
)
flavor.write_bytes(render_authoring_markdown(fd, body))
cli("documentation-review", ["project", "documentation-review", str(root), "--record"])
cli("component-lock", ["lock", str(component), "--target", "host"])
project = load_project(root)
snapshot = FilesystemLockedGenerationAuthorityReader().read(
    component, target_name="host", flavor_roots=project.roots("flavor")
)
if sys.platform.startswith("freebsd"):
    # Diagnostic only: retain the actual tool spelling without replacing observers.
    readelf = shutil.which("readelf")
    if readelf:
        header_result = {"diagnostic_only": True, "host_machine": platform.machine()}
        with (
            (output / "host-elf-header.stdout").open("wb") as stdout,
            (output / "host-elf-header.stderr").open("wb") as stderr,
        ):
            try:
                header = subprocess.run(
                    [readelf, "-h", "/bin/sh"],
                    stdout=stdout,
                    stderr=stderr,
                    timeout=30,
                    check=False,
                )
                header_result["exit_code"] = header.returncode
            except (OSError, subprocess.TimeoutExpired) as error:
                header_result["error"] = type(error).__name__
        (output / "host-elf-header.json").write_text(
            json.dumps(header_result, indent=2) + "\n", encoding="utf-8"
        )
checkpoint("observing-selected-sdk")
acquisition = NativeSdkProjectAcquisition(
    cache_root=output / "selected-sdk",
    source_intelligence_policy=project.definition.source_intelligence,
    security_policy=SecurityPolicy(
        canonical_identity("explicit SDK qualification policy").uri
    ),
    revocations=lambda: AuthorizationRevocationSet(),
    environment=dict(os.environ),
    actor="canonical-sdk-qualification",
    reason="Observe the exact recipe and SDK snapshot for reviewed retained execution",
    host_build_acknowledged=True,
)
owner = acquisition(snapshot)
(binding,) = owner.for_scope(snapshot.authority.lock.root_revision)
built = binding.build
for item in built.product.snapshot.files:
    target = root / "vendor" / item.path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(owner.read_input_blob(binding, item.blob))
    target.chmod(0o755 if item.executable else 0o644)
sdk_policy = RetainedProjectSdkSelectionPolicy(
    (
        RetainedProjectSdkSelectionBinding(
            snapshot.authority.lock.identity,
            built.selection.identity,
            built.product.snapshot.identity,
            "vendor",
        ),
    )
)
write("sdk-policy.json", canonical_json_bytes(sdk_policy.to_dict()))
checkpoint("capturing-observed-bootstrap-tools")
for name, source in [
    ("shell", "/bin/sh"),
    ("cc", "/usr/bin/cc"),
    ("ld.lld", "/usr/bin/ld.lld"),
    ("copy", "/bin/cp"),
]:
    target = root / "tools" / name
    target.parent.mkdir(exist_ok=True)
    shutil.copyfile(Path(source).resolve(strict=True), target)
    target.chmod(0o755)
for path in [
    "/usr/lib/" + n
    for n in [
        "crt1.o",
        "crti.o",
        "crtbegin.o",
        "crtend.o",
        "crtn.o",
        "libgcc.a",
        "libgcc_s.so",
        "libc.so",
        "libcompiler_rt.a",
        "libc_nonshared.a",
        "libssp_nonshared.a",
    ]
] + ["/lib/libc.so.7", "/lib/libgcc_s.so.1"]:
    target = root / "sysroot" / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(path).resolve(strict=True), target)
    target.chmod(0o644)
write(
    "src/main.c",
    """extern double vendor_math_scale(double,double);
extern double strtod(const char *,char **);
extern int printf(const char *,...);
int main(int argc,char **argv) {
 if(argc!=3) return 2;
 char *a,*b; double x=strtod(argv[1],&a),y=strtod(argv[2],&b);
 if(!*argv[1]||!*argv[2]||*a||*b) return 2;
 return printf("%.1f\\n",vendor_math_scale(x,y))<0 ? 3 : 0;
}
""",
)
write(
    "build.sh",
    """set -eu
inputs=$1; objects=$2; outputs=$3
"$inputs/tools/cc" -no-pie -nostdinc --sysroot="$inputs/sysroot" \
 -B"$inputs/tools" -fuse-ld=lld \
 "$inputs/src/main.c" "$inputs/vendor/python/vendor_math/lib/vendor_math.so" \
 -o "$outputs/app"
mkdir "$outputs/lib"
"$inputs/tools/copy" "$inputs/vendor/python/vendor_math/lib/vendor_math.so" \
 "$outputs/lib/vendor_math.so"
"$inputs/tools/copy" "$inputs/vendor/python/vendor_math/lib/vendor_factor.so" \
 "$outputs/lib/vendor_factor.so"
""",
)
# mkdir is a shell builtin only on some shells; capture its exact executable too.
shutil.copyfile(Path("/bin/mkdir").resolve(strict=True), root / "tools/mkdir")
(root / "tools/mkdir").chmod(0o755)
write(
    "build.sh",
    (root / "build.sh")
    .read_text()
    .replace('mkdir "$outputs/lib"', '"$inputs/tools/mkdir" "$outputs/lib"'),
)
write(
    "tests/check.sh",
    """set -eu
outputs=$1
positive=$("$outputs/app" 1.5 3)
negative=$("$outputs/app" -2 4)
zero=$("$outputs/app" 0 8)
test "$positive" = 4.5
test "$negative" = -8.0
test "$zero" = 0.0
"""
    'printf \'%s\\n\' \'{"schema":"literate-ai/retained-project-test-report@1",'
    '"action":"tests","cases":[{"id":"negative","outcome":"passed",'
    '"reason":null},{"id":"positive","outcome":"passed","reason":null},'
    '{"id":"zero","outcome":"passed","reason":null}]}\' > "$outputs/tests.json"\n',
)
write(
    "manual-input.md",
    "# Native SDK consumer\n\nRun `app VALUE FACTOR` to print their product to "
    "one decimal place. Invalid operands exit 2. The selected vendor SDK is "
    "source-built; host compiler bootstrap provenance is observed, not "
    "source-built SDK admission.\n",
)
test_policy = RetainedProjectTestPolicy(
    project.definition.test_receipt_policy.identity,
    tuple(
        RetainedProjectExpectedCase(name, "tests")
        for name in ["negative", "positive", "zero"]
    ),
    (("tests", "tests.json"),),
)
write("retained-tests.json", canonical_json_bytes(test_policy.to_dict()))
locks = (snapshot.authority.lock.identity,)
accept = RetainedProjectAcceptancePolicy(
    locks,
    (
        RetainedProjectAcceptanceCase(
            "withheld-product", "accept", "app", ("2", "5"), b"", 0, b"10.0\n", b"", 10
        ),
        RetainedProjectAcceptanceCase(
            "invalid-operands", "accept", "app", ("bad", "5"), b"", 2, b"", b"", 10
        ),
    ),
)
write(
    "verification/retained-project-acceptance.json",
    canonical_json_bytes(accept.to_dict()),
)
roles = {
    "src": "source",
    "tools": "toolchain",
    "sysroot": "toolchain",
    "vendor": "sdk",
    "sdk-policy.json": "sdk-policy",
    "build.sh": "build-definition",
    "profile.json": "build-definition",
    "tests": "test-definition",
    "manual-input.md": "docs-definition",
    "retained-tests.json": "test-policy",
    "literate.release.json": "release-policy",
}
limits = RetainedProjectLimits(2000, 512 * 1024 * 1024, 2 * 1024 * 1024 * 1024)
write("profile.json", "{}\n")
manifest = discover_retained_project_inputs(root, roles, limits)
tools = tuple(
    (
        name,
        "tools/" + name,
        ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256((root / "tools" / name).read_bytes()).hexdigest()
        ),
    )
    for name in ["shell", "cc", "ld.lld", "copy", "mkdir"]
)
action = RetainedProjectAction
profile = RetainedProjectProfile(
    "host",
    ExecutionWorker("local", ExecutionWorkerKind.LOCAL).identity,
    manifest.role_identity({"sdk", "toolchain"}),
    manifest.role_identity({"dependency"}),
    ContentIdentity.parse_uri(
        "sha256:"
        + hashlib.sha256((root / "retained-tests.json").read_bytes()).hexdigest()
    ),
    ContentIdentity.parse_uri(
        "sha256:"
        + hashlib.sha256((root / "literate.release.json").read_bytes()).hexdigest()
    ),
    tools,
    (
        action(
            "build",
            "build",
            "shell",
            ("{input_root}/build.sh", "{input_root}", "{object_root}", "{output_root}"),
            (),
            ("native-consumer", "sdk-runtime"),
            ("app", "lib"),
            120,
        ),
        action(
            "tests",
            "test",
            "shell",
            ("{input_root}/tests/check.sh", "{output_root}"),
            ("build",),
            ("positive", "negative", "zero"),
            ("tests.json",),
            60,
        ),
        action(
            "docs",
            "docs",
            "copy",
            ("{input_root}/manual-input.md", "{output_root}/manual.txt"),
            ("build",),
            ("consumer-manual",),
            ("manual.txt",),
            30,
        ),
        action(
            "accept",
            "accept",
            "shell",
            ("-c", 'test -s "$1/manual.txt"', "accept", "{output_root}"),
            ("tests", "docs"),
            ("withheld-product", "invalid-operands"),
            (),
            30,
        ),
    ),
    ("app", "lib", "manual.txt"),
    ContentIdentity.parse_uri(
        "sha256:" + hashlib.sha256(canonical_json_bytes(accept.to_dict())).hexdigest()
    ),
    runtime_library_paths=("vendor/python/vendor_math/lib",),
)
write("profile.json", canonical_json_bytes(profile.to_dict()))
manifest = discover_retained_project_inputs(root, roles, limits)
write("manifest.json", canonical_json_bytes(manifest.to_dict()))
assert (
    json.loads((root / "literate.project.json").read_text())["test_receipt_policy"]
    == policy_before
)
cli(
    "final-documentation-review",
    ["project", "documentation-review", str(root), "--record"],
)
cli("final-validate", ["project", "validate", str(root)])
checkpoint("committing-disposable-original-authority")
for args in [
    ("init", "-q"),
    ("add", "."),
    ("commit", "-qm", "Authored original SDK consumer qualification"),
]:
    subprocess.run(
        [
            "git",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=SDK qualification",
            "-c",
            "user.email=sdk@example.test",
            *args,
        ],
        cwd=root,
        check=True,
        timeout=180,
    )
adopt = cli("original-plan", ["project", "adopt-original", "--project", str(root)])
cli(
    "original-apply",
    [
        "project",
        "adopt-original",
        "--project",
        str(root),
        "--apply",
        "--acknowledge",
        "--expected-plan-identity",
        adopt["plan_identity"],
    ],
)
arguments = [
    "build",
    "--project",
    str(root),
    "--target",
    "host",
    "--retained-project",
    "manifest.json",
    "--retained-project-profile",
    "profile.json",
    "--obj-dir",
    str(output / "objects"),
    "--build-dir",
    str(output / "products"),
]
plan = cli("retained-plan", [*arguments, "--retained-project-plan"], timeout=1800)
negative = cli("missing-authorization", arguments, expected=2)
assert negative["error"]["code"] == "rebuild.host_execution_not_acknowledged"
negative = cli(
    "missing-retained-authorization", [*arguments, "--allow-host-execution"], expected=2
)
assert negative["error"]["code"] == "retained_project.admission_invalid"
assert (
    "Authorize the exact current retained-project plan identity"
    in negative["error"]["message"]
)
result = cli(
    "authorized-execution",
    [
        *arguments,
        "--authorize-retained-project",
        plan["authorization_identity"],
        "--allow-host-execution",
    ],
    timeout=3600,
)
assert result["admitted"] is True
assert result["retained_receipt"]["release_authorized"] is False
assert (
    json.loads((root / "literate.project.json").read_text())["test_receipt_policy"]
    == policy_before
)
assert not (root / "verification/current.json").exists()
# Prove that this execution genuinely needed the reviewed loader projection.
# The control uses the sealed accepted app without ambient loader/import keys.
app = Path(result["workspace"]) / "outputs" / "app"
assert app.is_file() and not app.is_symlink()
assert app.resolve().is_relative_to((output / "objects").resolve())
clean_environment = {
    k: v
    for k, v in os.environ.items()
    if not (k.startswith(("LD_", "DYLD_")) or k in ("PYTHONHOME", "PYTHONPATH"))
}
control = subprocess.run(
    [str(app), "2", "5"], env=clean_environment, capture_output=True, timeout=20
)
(output / "without-loader.stdout").write_bytes(control.stdout)
(output / "without-loader.stderr").write_bytes(control.stderr)
assert control.returncode != 0 and control.stdout != b"10.0\n", (
    "app did not require captured loader context"
)
checkpoint(
    "complete-local-runtime",
    retained_receipt=result["retained_receipt_identity"],
    canonical_installed_cli_qualified=True,
    consumer_rpath=False,
    without_loader_exit_code=control.returncode,
    reviewed_loader_required=True,
    compiler_sdk_admitted=False,
    product_receipt=False,
    iso_created=False,
    release_acceptance=False,
)
