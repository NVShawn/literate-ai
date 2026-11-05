"""Real own-path/Git library inputs, independently authored graph intent.

Provisioning occurs before original verifier custody is issued. Runtime Cargo
remains offline/locked; the local Git source and its actual native cache are
both inside the complete declared verifier roots, without ignored exclusions.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.component_acceptance import load_versioned_library_acceptance
from literate_ai.contracts.rust_library_harness import (
    RustHarnessResolution,
    RustHarnessResolvedDependency,
    RustHarnessResolvedPackage,
)
from tests.support.fixtures_rust_library_harness import resolution


def author_own_dependency_graph(fixture, environment):
    """Provision genuine local sources before capturing the complete oracle.

    Native metadata is deliberately not used to author the expected graph.
    Git's actual committed identity and the provisioned physical checkout
    coordinate are facts about authored inputs, rather than graph observations.
    """
    git = shutil.which("git", path=environment.get("PATH"))
    if git is None:
        raise RuntimeError("actual native Git fixture provisioning required")
    root = fixture.root
    workspace = fixture.authority / "workspace"
    path_source, git_source = workspace / "path-helper", workspace / "git-source"
    for directory, name, value in (
        (path_source, "fixture-path", 20),
        (git_source, "fixture-fixed-git", 22),
    ):
        (directory / "src").mkdir(parents=True)
        (directory / "Cargo.toml").write_text(
            "[package]\nname="
            + json.dumps(name)
            + '\nversion="1.0.0"\nedition="2021"\n',
            encoding="utf-8",
        )
        (directory / "src/lib.rs").write_text(
            f"pub fn value() -> u32 {{ {value} }}\n", encoding="utf-8"
        )
    evidence = root / "own-graph-provisioning"
    evidence.mkdir(mode=0o700)
    values = {
        **environment,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "file",
    }
    for name in tuple(values):
        if name.startswith("GIT_") and name not in {
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_GLOBAL",
            "GIT_TERMINAL_PROMPT",
            "GIT_ALLOW_PROTOCOL",
        }:
            values.pop(name)
    # Keep direct and Cargo CLI Git provisioning synchronous before capture;
    # background auto-maintenance must not mutate a captured .git namespace.
    values.update(
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="maintenance.auto",
        GIT_CONFIG_VALUE_0="false",
        GIT_CONFIG_KEY_1="gc.auto",
        GIT_CONFIG_VALUE_1="0",
    )
    git_command = (git, "-c", "maintenance.auto=false", "-c", "gc.auto=0")
    calls = []

    def invoke(argv, cwd, env):
        result = run_bounded_process(
            argv,
            cwd=cwd,
            environment=env,
            timeout_seconds=180,
            stdout_limit_bytes=1024**2,
            stderr_limit_bytes=1024**2,
            terminate_descendants=True,
            trace=False,
            error_prefix="fixture.rust-own-graph",
        )
        calls.append(tuple(argv))
        label = str(len(calls))
        (evidence / (label + ".stdout")).write_bytes(result.stdout)
        (evidence / (label + ".stderr")).write_bytes(result.stderr)
        (evidence / (label + ".json")).write_text(
            json.dumps(
                {
                    "argv": list(argv),
                    "exit": result.returncode,
                }
            ),
            encoding="utf-8",
        )
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout

    for args in (
        ("init", "--quiet"),
        ("add", "Cargo.toml", "src/lib.rs"),
        (
            "-c",
            "user.name=Rust Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Author exact dependency",
        ),
    ):
        invoke((*git_command, *args), git_source, values)
    commit = (
        invoke((*git_command, "rev-parse", "HEAD"), git_source, values).decode().strip()
    )
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise AssertionError("actual fixed native Git commit required")
    manifest_bytes = invoke(
        (*git_command, "show", commit + ":Cargo.toml"), git_source, values
    )
    original_git_source = invoke(
        (*git_command, "show", commit + ":src/lib.rs"), git_source, values
    )
    library = fixture.export
    with (library / "Cargo.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            '\n[dependencies]\nfixture_path={package="fixture-path",path='
            + json.dumps(str(path_source))
            + '}\nfixture_git={package="fixture-fixed-git",git='
            + json.dumps(git_source.as_uri())
            + ",rev="
            + json.dumps(commit)
            + "}\n"
        )
    (library / "src/lib.rs").write_text(
        '#[cfg(feature="checked")] pub fn answer(input: &str) -> String {\n'
        " let value = fixture_path::value() + fixture_git::value();\n"
        ' format!(r#"{{"answer":{},"echo":{}}}"#, value, input)\n}\n',
        encoding="utf-8",
    )
    # The only Git source is the explicitly authored file:// repository; all
    # registry sources are replaced by the already verified offline vendor.
    # This finite provisioning phase is not verifier execution or admission.
    provision = evidence / "workspace"
    (provision / "src").mkdir(parents=True)
    (provision / ".cargo").mkdir()
    shutil.copy2(workspace / ".cargo/config.toml", provision / ".cargo/config.toml")
    (provision / "Cargo.toml").write_text(
        '[package]\nname="litai-library-acceptance"\nversion="0.0.0"\nedition="2021"\n'
        '[workspace]\n[dependencies]\nqualified_fixture={package="qualified-fixture",path='
        + json.dumps(str(library))
        + ',default-features=false,features=["checked","default"]}\n'
        'serde_json={version="=1.0.151",registry="crates-io",default-features=true,features=[]}\n',
        encoding="utf-8",
    )
    (provision / "src/main.rs").write_bytes(fixture.harness)
    cargo_values = {
        **values,
        "CARGO_HOME": str(fixture.authority / "cargo-home"),
        "CARGO_TARGET_DIR": str(evidence / "target"),
        "CARGO_NET_OFFLINE": "false",
        "CARGO_NET_GIT_FETCH_WITH_CLI": "true",
        "RUSTC": fixture.rustc.command[0],
    }
    invoke((*fixture.cargo.command, "generate-lockfile"), provision, cargo_values)
    cargo_values["CARGO_NET_OFFLINE"] = "true"
    invoke(
        (*fixture.cargo.command, "build", "--offline", "--locked"),
        provision,
        cargo_values,
    )
    fixture.lock = (provision / "Cargo.lock").read_bytes()
    (fixture.authority / "Cargo.lock").write_bytes(fixture.lock)
    checkouts = tuple(
        (fixture.authority / "cargo-home/git/checkouts").glob("*/*/Cargo.toml")
    )
    if len(checkouts) != 1 or checkouts[0].read_bytes() != manifest_bytes:
        raise AssertionError("one exact committed native Git checkout required")
    git_manifest = checkouts[0]
    if (git_manifest.parent / "src/lib.rs").read_bytes() != original_git_source:
        raise AssertionError("native Git checkout differs from committed source")
    fixture.author_oracle()
    base = resolution()
    own_path = RustHarnessResolvedPackage(
        "input:workspace",
        "path-helper/Cargo.toml",
        "fixture-path",
        "1.0.0",
        None,
        (),
        (),
    )
    own_git = RustHarnessResolvedPackage(
        "input:cargo-home",
        git_manifest.relative_to(fixture.authority / "cargo-home").as_posix(),
        "fixture-fixed-git",
        "1.0.0",
        "git+" + git_source.as_uri() + "?rev=" + commit + "#" + commit,
        (),
        (),
    )
    packages = tuple(
        replace(
            row,
            dependencies=(
                RustHarnessResolvedDependency(
                    "fixture_git", own_git.identity, None, None
                ),
                RustHarnessResolvedDependency(
                    "fixture_path", own_path.identity, None, None
                ),
            ),
        )
        if row.identity == base.library
        else row
        for row in base.packages
    )
    expected = RustHarnessResolution(
        base.root,
        base.library,
        tuple(sorted((*packages, own_path, own_git), key=lambda row: row.identity.uri)),
    )
    document = json.loads(fixture.oracle_path.read_text("utf-8"))
    document["rust_harness"]["resolution"] = expected.to_dict()
    fixture.oracle_path.write_text(
        json.dumps(document, sort_keys=True), encoding="utf-8"
    )
    fixture.oracle = load_versioned_library_acceptance(
        fixture.oracle_path, "qualified-fixture"
    )
    (evidence / "intent.json").write_text(
        json.dumps(
            {
                "git_commit": commit,
                "git_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "git_source_sha256": hashlib.sha256(original_git_source).hexdigest(),
                "expected_resolution": expected.to_dict(),
                "expected_graph_from_native_metadata": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path_source, git_manifest.parent
