"""Real local-registry crates for native ADR0056 declaration boundaries.

These are explicitly authored test crates, not an upstream registry assertion.
Every crate has actual archive/index/vendor bytes and is compiled by native
offline Cargo. Expected resolution is authored from these source declarations.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from dataclasses import replace

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.component_acceptance import load_versioned_library_acceptance
from literate_ai.adapters.retained_cargo_input_owners_v2 import (
    RetainedCargoInputRootV2,
    capture_retained_cargo_input_owners_v2,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.retained_cargo_custody import (
    RetainedCargoCustodyPolicy,
    RetainedCargoInputBudget,
    RetainedCargoOwnerBudget,
)
from literate_ai.contracts.rust_library_harness import (
    MAX_DIRECT_DEPENDENCIES,
    MAX_SELECTED_FEATURES,
    RustHarnessLockfile,
    RustHarnessRegistryDependency,
    RustHarnessResolution,
    RustHarnessResolvedDependency,
    RustHarnessResolvedPackage,
)
from tests.support.fixtures_rust_library_harness import (
    HARNESS,
    VENDOR_SCOPE,
    resolution,
)


def author_native_boundary_graph(fixture, environment):
    """Author genuine exact-128/exact-4096 inputs; no graph copied from metadata."""
    if MAX_DIRECT_DEPENDENCIES != 128 or MAX_SELECTED_FEATURES != 4096:
        raise AssertionError("reviewed ADR0056 boundary authority changed")
    document = json.loads(fixture.oracle_path.read_text("utf-8"))
    original = fixture.oracle.rust_harness
    library_manifest = next(
        row.manifest
        for row in original.resolution.packages
        if row.identity == original.resolution.library
    )
    base = resolution(library_manifest=library_manifest)
    # One additional actual crate also provisions the causal 129th declaration.
    count = MAX_DIRECT_DEPENDENCIES
    selected_count = MAX_SELECTED_FEATURES - sum(
        len(row.features) for row in base.packages
    )
    if not 0 < selected_count <= MAX_SELECTED_FEATURES:
        raise AssertionError("actual complete feature boundary unavailable")
    workspace = fixture.authority / "workspace"
    index = workspace / "boundary-index"
    archives = workspace / "boundary-archives"
    index.mkdir()
    archives.mkdir()
    index_url = index.as_uri()
    registry_source = "registry+" + index_url
    (index / "config.json").write_text(
        json.dumps(
            {
                "dl": archives.as_uri() + "/{crate}-{version}.crate",
            }
        ),
        encoding="utf-8",
    )
    dependencies, packages, calls = [], [], []
    for number in range(count):
        name, alias = f"fixture-boundary-{number:03}", f"fixture_bound_{number:03}"
        all_features = (
            tuple(f"chosen_{n:04}" for n in range(MAX_SELECTED_FEATURES))
            if number == 0
            else ()
        )
        features = all_features[:selected_count] if number == 0 else ()
        content = (
            "[package]\nname="
            + json.dumps(name)
            + '\nversion="1.0.0"\nedition="2021"\n'
            + (
                "[features]\n" + "".join(feature + "=[]\n" for feature in all_features)
                if all_features
                else ""
            )
        ).encode()
        members = {
            "Cargo.toml": content,
            "src/lib.rs": f"pub fn value() -> u64 {{ {number} }}\n".encode(),
        }
        directory = fixture.authority / "vendor" / (name + "-1.0.0")
        (directory / "src").mkdir(parents=True)
        for relative, payload in members.items():
            (directory / relative).write_bytes(payload)
        archive = archives / (name + "-1.0.0.crate")
        with tarfile.open(archive, "w:gz") as output:
            for relative, payload in sorted(members.items()):
                member = tarfile.TarInfo(name + "-1.0.0/" + relative)
                member.size, member.mode, member.mtime = len(payload), 0o644, 0
                output.addfile(member, io.BytesIO(payload))
        raw_archive = archive.read_bytes()
        if len(raw_archive) > 128 * 1024:
            raise AssertionError("bounded actual fixture archive required")
        checksum = hashlib.sha256(raw_archive).hexdigest()
        (directory / ".cargo-checksum.json").write_text(
            json.dumps(
                {
                    "package": checksum,
                    "files": {
                        path: hashlib.sha256(value).hexdigest()
                        for path, value in members.items()
                    },
                }
            ),
            encoding="utf-8",
        )
        registry_entry = index / name[:2] / name[2:4] / name
        registry_entry.parent.mkdir(parents=True, exist_ok=True)
        registry_entry.write_text(
            json.dumps(
                {
                    "name": name,
                    "vers": "1.0.0",
                    "deps": [],
                    "cksum": checksum,
                    "features": {feature: [] for feature in all_features},
                    "yanked": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        dependencies.append(
            RustHarnessRegistryDependency(
                alias,
                name,
                "1.0.0",
                "fixture-boundary",
                registry_source,
                False,
                features,
            )
        )
        packages.append(
            RustHarnessResolvedPackage(
                "input:" + VENDOR_SCOPE,
                name + "-1.0.0/Cargo.toml",
                name,
                "1.0.0",
                registry_source,
                features,
                (),
            )
        )
        if number < MAX_DIRECT_DEPENDENCIES - 1:
            calls.append(alias + "::value()")
    fixture.harness = HARNESS.replace(
        b"fn main() {",
        (
            "fn main() {\n let dependency_sum: u64 = "
            + " + ".join(calls)
            + f";\n assert_eq!(dependency_sum, {sum(range(count - 1))});"
        ).encode(),
        1,
    )
    (fixture.authority / "harness.rs").write_bytes(fixture.harness)
    config = workspace / ".cargo/config.toml"
    with config.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n[registries.fixture-boundary]\nindex="
            + json.dumps(index_url)
            + "\n[source.fixture-boundary]\nregistry="
            + json.dumps(index_url)
            + '\nreplace-with="fixture-vendor"\n'
        )
    values = {
        **environment,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "file",
    }
    for key in tuple(values):
        if key.startswith("GIT_") and key not in {
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_GLOBAL",
            "GIT_TERMINAL_PROMPT",
            "GIT_ALLOW_PROTOCOL",
        }:
            values.pop(key)
    values.update(
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="maintenance.auto",
        GIT_CONFIG_VALUE_0="false",
        GIT_CONFIG_KEY_1="gc.auto",
        GIT_CONFIG_VALUE_1="0",
    )
    git = shutil.which("git", path=values.get("PATH"))
    if git is None:
        raise RuntimeError("real native local registry Git required")
    evidence = fixture.root / "boundary-provisioning"
    evidence.mkdir()
    sequence = []

    def run(argv, cwd, env):
        result = run_bounded_process(
            argv,
            cwd=cwd,
            environment=env,
            timeout_seconds=300,
            stdout_limit_bytes=1024**2,
            stderr_limit_bytes=1024**2,
            terminate_descendants=True,
            trace=False,
            error_prefix="fixture.rust-boundary-provision",
        )
        sequence.append(tuple(argv))
        for suffix, raw in (("stdout", result.stdout), ("stderr", result.stderr)):
            (evidence / f"{len(sequence)}.{suffix}").write_bytes(raw)
        (evidence / f"{len(sequence)}.json").write_text(
            json.dumps({"argv": list(argv), "exit": result.returncode}),
            encoding="utf-8",
        )
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))

    for args in (
        ("init", "--quiet"),
        ("add", "."),
        (
            "-c",
            "user.name=Boundary Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Author actual registry boundary crates",
        ),
    ):
        run(
            (git, "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args),
            index,
            values,
        )
    # Only fixture-owned local-index/vendor sources exist. All Cargo commands,
    # including provisioning, are explicitly offline and never download crates.
    provision = evidence / "workspace"
    (provision / "src").mkdir(parents=True)
    (provision / ".cargo").mkdir()
    shutil.copy2(config, provision / ".cargo/config.toml")
    manifest = (
        '[package]\nname="litai-library-acceptance"\nversion="0.0.0"\nedition="2021"\n'
        '[workspace]\n[dependencies]\nqualified_fixture={package="qualified-fixture",path='
        + json.dumps(str(fixture.export))
        + ',default-features=false,features=["checked","default"]}\n'
        'serde_json={version="=1.0.151",registry="crates-io",default-features=true,features=[]}\n'
    )

    def dependency_line(dependency):
        return (
            dependency.alias
            + "={package="
            + json.dumps(dependency.package)
            + ',version="=1.0.0",registry="fixture-boundary",'
            + "default-features=false,features="
            + json.dumps(list(dependency.features))
            + "}\n"
        )

    for dependency in dependencies[:-1]:
        manifest += dependency_line(dependency)
    (provision / "Cargo.toml").write_text(manifest, encoding="utf-8")
    (provision / "src/main.rs").write_bytes(fixture.harness)
    values.update(
        CARGO_HOME=str(fixture.authority / "cargo-home"),
        CARGO_TARGET_DIR=str(evidence / "target"),
        CARGO_NET_OFFLINE="true",
        RUSTC=fixture.rustc.command[0],
    )
    for args in (
        ("generate-lockfile", "--offline"),
        ("build", "--offline", "--locked"),
    ):
        run((*fixture.cargo.command, *args), provision, values)
    fixture.lock = (provision / "Cargo.lock").read_bytes()
    (fixture.authority / "Cargo.lock").write_bytes(fixture.lock)
    (provision / "Cargo.toml").write_text(
        manifest + dependency_line(dependencies[-1]), encoding="utf-8"
    )
    run((*fixture.cargo.command, "generate-lockfile", "--offline"), provision, values)
    over_lock = (provision / "Cargo.lock").read_bytes()
    (fixture.authority / "Cargo129.lock").write_bytes(over_lock)
    all_packages = (*base.packages, *packages[:-1])
    root_edges = tuple(
        sorted(
            (
                *next(
                    row for row in base.packages if row.identity == base.root
                ).dependencies,
                *(
                    RustHarnessResolvedDependency(
                        dep.alias, package.identity, None, None
                    )
                    for dep, package in zip(
                        dependencies[:-1], packages[:-1], strict=True
                    )
                ),
            ),
            key=lambda row: row.key,
        )
    )
    all_packages = tuple(
        replace(row, dependencies=root_edges) if row.identity == base.root else row
        for row in all_packages
    )
    graph = RustHarnessResolution(
        base.root,
        base.library,
        tuple(sorted(all_packages, key=lambda row: row.identity.uri)),
    )
    over_edges = tuple(
        sorted(
            (
                *root_edges,
                RustHarnessResolvedDependency(
                    dependencies[-1].alias, packages[-1].identity, None, None
                ),
            ),
            key=lambda row: row.key,
        )
    )
    over_graph = RustHarnessResolution(
        base.root,
        base.library,
        tuple(
            sorted(
                (
                    *(
                        replace(row, dependencies=over_edges)
                        if row.identity == base.root
                        else row
                        for row in graph.packages
                    ),
                    packages[-1],
                ),
                key=lambda row: row.identity.uri,
            )
        ),
    )
    roots = tuple(
        RetainedCargoInputRootV2(scope, fixture.authority / path)
        for scope, path in original.inputs.roots
    )
    owners = capture_retained_cargo_input_owners_v2(
        roots,
        policy=original.inputs.custody,
        maximum_metadata_bytes=original.inputs.maximum_metadata_bytes,
    )
    declaration = replace(
        original,
        dependencies=tuple(
            sorted(
                (*dependencies[:-1], *original.dependencies), key=lambda row: row.alias
            )
        ),
        lockfile=RustHarnessLockfile(
            original.lockfile.file,
            ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(fixture.lock).hexdigest()
            ),
            len(fixture.lock),
        ),
        inputs=replace(original.inputs, owners_identity=owners.identity),
        resolution=graph,
        timeout_seconds=300,
    )
    document["harness_identity"] = (
        "sha256:" + hashlib.sha256(fixture.harness).hexdigest()
    )
    document["rust_harness"] = declaration.to_dict()
    fixture.oracle_path.write_text(
        json.dumps(document, sort_keys=True), encoding="utf-8"
    )
    fixture.oracle = load_versioned_library_acceptance(
        fixture.oracle_path, "qualified-fixture"
    )
    return {
        "direct_dependencies": len(declaration.dependencies),
        "resolved_selected_features": sum(len(row.features) for row in graph.packages),
        "fixture_registry": index_url,
        "native_graph_used_to_author_expectations": False,
        "over_count_dependency": dependencies[-1].to_dict(),
        "over_count_package": packages[-1].to_dict(),
        "over_count_resolution": over_graph.to_dict(),
        "over_count_lockfile": RustHarnessLockfile(
            "Cargo129.lock",
            ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(over_lock).hexdigest()
            ),
            len(over_lock),
        ).to_dict(),
    }


def exact_source_policy(fixture):
    """Derive actual byte/count caps from canonical complete capture, not a walk."""
    inputs = fixture.oracle.rust_harness.inputs
    roots = tuple(
        RetainedCargoInputRootV2(scope, fixture.authority / path)
        for scope, path in inputs.roots
    )
    owners = capture_retained_cargo_input_owners_v2(
        roots,
        policy=inputs.custody,
        maximum_metadata_bytes=inputs.maximum_metadata_bytes,
    )
    rows = []
    for scope, tree in owners.trees:
        rows.append(
            RetainedCargoOwnerBudget(
                scope,
                RetainedCargoInputBudget(
                    tree.file_count,
                    tree.entry_count,
                    max(blob.size for _, blob, _ in tree.files),
                    tree.payload_bytes,
                ),
            )
        )
    return roots, RetainedCargoCustodyPolicy(
        tuple(rows),
        RetainedCargoInputBudget(
            sum(row.budget.maximum_files for row in rows),
            sum(row.budget.maximum_entries for row in rows),
            max(row.budget.maximum_file_bytes for row in rows),
            sum(row.budget.maximum_total_bytes for row in rows),
        ),
    )


def bind_exact_native_input_boundaries(fixture):
    """Admit actual complete source bytes and discover its finite metadata edge.

    Every probe uses the canonical real filesystem owner. Only its exact named
    metadata-limit refusal is a rejected budget; any currentness/IO failure is
    preserved as a failing control, never converted into a boundary result.
    """
    roots, policy = exact_source_policy(fixture)
    original = fixture.oracle.rust_harness
    high = original.inputs.maximum_metadata_bytes
    low = len(roots) * 16 * 1024**2 - 1
    probes = []

    def admits(amount):
        try:
            owner = capture_retained_cargo_input_owners_v2(
                roots, policy=policy, maximum_metadata_bytes=amount
            )
        except ValueError as exc:
            if str(exc) != "retained.cargo.v2-input-metadata-limit":
                raise
            probes.append({"bytes": amount, "admitted": False})
            return False
        owner.require_unchanged()
        probes.append({"bytes": amount, "admitted": True})
        return True

    if not admits(high):
        raise AssertionError("bounded original metadata allowance must admit fixture")
    for _ in range(28):
        if high - low <= 1:
            break
        middle = (low + high) // 2
        if admits(middle):
            high = middle
        else:
            low = middle
    if high - low != 1 or not admits(high) or admits(high - 1):
        raise AssertionError("actual one-byte metadata boundary unavailable")
    owners = capture_retained_cargo_input_owners_v2(
        roots, policy=policy, maximum_metadata_bytes=high
    )
    inputs = replace(
        original.inputs,
        custody=policy,
        maximum_metadata_bytes=high,
        owners_identity=owners.identity,
    )
    declaration = replace(original, inputs=inputs)
    document = json.loads(fixture.oracle_path.read_text("utf-8"))
    document["rust_harness"] = declaration.to_dict()
    fixture.oracle_path.write_text(
        json.dumps(document, sort_keys=True), encoding="utf-8"
    )
    fixture.oracle = load_versioned_library_acceptance(
        fixture.oracle_path, "qualified-fixture"
    )
    (fixture.root / "boundary-provisioning/exact-input-budgets.json").write_text(
        json.dumps(
            {
                "policy": policy.to_dict(),
                "minimum_admitted_metadata_bytes": high,
                "metadata_probes": probes,
                "scope": "Actual complete source capture; no mirrored estimator",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return roots, policy, high


def public_boundary_refusals(fixture, facts):
    """Author invalid operator intents for the real public rebuild boundary.

    No parser outcome or native result is simulated here. Count-overflow inputs
    have real provisioned source/lock/graph intent; policy caps and registry drift
    remain the actual production readers and native resolver's decision.
    """
    original = json.loads(fixture.oracle_path.read_text("utf-8"))
    cases = []

    def copy():
        return json.loads(json.dumps(original))

    document = copy()
    rust = document["rust_harness"]
    rust["dependencies"] = sorted(
        [*rust["dependencies"], facts["over_count_dependency"]],
        key=lambda row: row["alias"],
    )
    rust["resolution"] = facts["over_count_resolution"]
    rust["lockfile"] = facts["over_count_lockfile"]
    cases.append(
        ("129-direct", document, "component_acceptance.rust_oracle_invalid", None)
    )
    document = copy()
    rust = document["rust_harness"]
    selected = next(
        row for row in rust["dependencies"] if row["alias"] == "fixture_bound_000"
    )
    next_feature = f"chosen_{len(selected['features']):04}"
    selected["features"].append(next_feature)
    next(
        row
        for row in rust["resolution"]["packages"]
        if row["name"] == selected["package"]
    )["features"].append(next_feature)
    cases.append(
        (
            "4097-resolved-features",
            document,
            "component_acceptance.rust_oracle_invalid",
            None,
        )
    )
    for field in ("maximum_files", "maximum_entries", "maximum_total_bytes"):
        document = copy()
        document["rust_harness"]["inputs"]["custody"]["aggregate"][field] -= 1
        cases.append(
            (
                field + "-one-short",
                document,
                "cli.invalid_input",
                "retained.cargo.v2-input-aggregate-limit",
            )
        )
    document = copy()
    owner = max(
        document["rust_harness"]["inputs"]["custody"]["owners"],
        key=lambda row: row["budget"]["maximum_file_bytes"],
    )
    owner["budget"]["maximum_file_bytes"] -= 1
    cases.append(
        (
            "file-bytes-one-short",
            document,
            "cli.invalid_input",
            "retained.cargo.v2-input-file-limit",
        )
    )
    document = copy()
    document["rust_harness"]["inputs"]["maximum_metadata_bytes"] -= 1
    cases.append(
        (
            "metadata-one-short",
            document,
            "cli.invalid_input",
            "retained.cargo.v2-input-metadata-limit",
        )
    )
    document = copy()
    document["rust_harness"]["stdout_limit_bytes"] = 1
    cases.append(
        (
            "native-output-limit",
            document,
            "component_acceptance.rust_metadata_before_output_limit",
            None,
        )
    )
    document = copy()
    document["rust_harness"]["dependencies"][0]["alias"] = "qualified_fixture"
    document["rust_harness"]["dependencies"].sort(key=lambda row: row["alias"])
    cases.append(
        ("alias-collision", document, "cli.invalid_input", "dependency alias collides")
    )
    document = copy()
    rust = document["rust_harness"]
    serde = next(
        row for row in rust["resolution"]["packages"] if row["name"] == "serde_json"
    )
    before = RustHarnessResolvedPackage.from_dict(serde).identity
    serde["source"] = "registry+" + facts["fixture_registry"]
    after = RustHarnessResolvedPackage.from_dict(serde).identity
    next(row for row in rust["dependencies"] if row["alias"] == "serde_json")[
        "registry_source"
    ] = serde["source"]
    packages = []
    for row in rust["resolution"]["packages"]:
        for edge in row["dependencies"]:
            if ContentIdentity.from_dict(edge["provider"]) == before:
                edge["provider"] = after.to_dict()
        packages.append(RustHarnessResolvedPackage.from_dict(row))
    rust["resolution"]["packages"] = [
        row.to_dict() for row in sorted(packages, key=lambda row: row.identity.uri)
    ]
    cases.append(
        (
            "registry-identity-drift",
            document,
            "component_acceptance.rust_harness_resolution_drift",
            None,
        )
    )
    return tuple(cases)
