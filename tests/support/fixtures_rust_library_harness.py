"""Real native Rust verifier inputs, with independently authored graph intent.

The fixture's oracle is intent read from actual files. It does not substitute a
project admission, tool observation, native result, or historical execution grant.
Public Standard lifecycle qualification remains a separate journey.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.component_acceptance import load_versioned_library_acceptance
from literate_ai.adapters.retained_cargo_input_owners_v2 import (
    RetainedCargoInputRootV2,
    capture_retained_cargo_input_owners_v2,
)
from literate_ai.contracts import (
    LibraryCapabilityImport,
    LibraryImportSurface,
    canonical_identity,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.retained_cargo_custody import (
    RetainedCargoCustodyPolicy,
    RetainedCargoInputBudget,
    RetainedCargoOwnerBudget,
)
from literate_ai.contracts.rust_library_harness import (
    RustHarnessInputSources,
    RustHarnessLockfile,
    RustHarnessRegistryDependency,
    RustHarnessResolution,
    RustHarnessResolvedDependency,
    RustHarnessResolvedPackage,
    RustLibraryHarnessDependencies,
)
from tests.support.fixtures_rust_harness_vendor import prepare_vendor

REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
VENDOR_SCOPE = "external:VERIFIER_VENDOR"
CASE_INPUT = {
    "label": "serde fixture",
    "values": [1, True, None],
    "nested": {"x": [2, 3]},
}

HARNESS = b"""fn main() {
 let args: Vec<String> = std::env::args().collect();
 let cases: serde_json::Value = serde_json::from_str(&args[3]).unwrap();
 let rows: Vec<_> = cases.as_array().unwrap().iter().map(|case| {
  let input = serde_json::to_string(&case["arguments"][0]).unwrap();
  let actual = qualified_fixture::answer(&input);
  let result: serde_json::Value = serde_json::from_str(&actual).unwrap();
  serde_json::json!({"case_id":case["case_id"],
   "capability":case["capability"],"result":result})
 }).collect();
 println!("{}", serde_json::json!({
  "schema":"literate-ai/library-acceptance-results@1","cases":rows}));
}
"""


def prepare_native_verifier_class(cls):
    """Provision a genuine direct-native class seed once, before oracle capture."""
    from literate_ai.adapters.builders.cargo import discover_cargo_toolchain
    from literate_ai.adapters.builders.rust import discover_rust_toolchain
    from literate_ai.adapters.lifecycle.standard_local import (
        LocalComponentToolBinding,
    )
    from tests.support.fixtures_rust_harness_vendor import CRATES
    from tests.support.fixtures_rust_library_harness import prepare_seed

    if not shutil.which("cargo") or not shutil.which("rustc"):
        raise unittest.SkipTest("native Cargo and rustc are unavailable")
    candidates = tuple((Path.home() / ".cargo/registry/cache").glob("*"))
    archives = next(
        (
            root
            for root in candidates
            if all((root / (name + ".crate")).is_file() for name in CRATES)
        ),
        None,
    )
    if archives is None:
        raise unittest.SkipTest("exact real upstream Rust fixture archives unavailable")
    cls.temporary = tempfile.TemporaryDirectory(prefix="native Rust verifier seed ")
    cls.addClassCleanup(cls.temporary.cleanup)
    cls.seed = Path(cls.temporary.name).resolve()
    cls.environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("CARGO_", "RUST"))
    }
    # This named direct-native profile is selected and measured BEFORE its
    # oracle is authored. It does not silently replace a pinned shim route.
    # Ordinary rustup execution under isolated HOME remains unqualified.
    observed_rust = discover_rust_toolchain(cls.environment)
    native_rust = Path(observed_rust.compiler_executable)
    native_cargo = native_rust.with_name("cargo.exe" if os.name == "nt" else "cargo")
    if not native_cargo.is_file():
        raise unittest.SkipTest("direct native Cargo/Rust fixture profile unavailable")
    cls.cargo = LocalComponentToolBinding.from_observed_toolchain(
        discover_cargo_toolchain(cls.environment, pinned_command=(str(native_cargo),))
    )
    cls.rustc = LocalComponentToolBinding.from_observed_toolchain(
        discover_rust_toolchain(
            {
                **cls.environment,
                "RUSTC": (
                    subprocess.list2cmdline([str(native_rust)])
                    if os.name == "nt"
                    else shlex.quote(str(native_rust))
                ),
            }
        )
    )
    prepare_seed(cls.seed, archives, cls.cargo, cls.environment)
    # Later native checks and public rebuilds share the prepared fixture home;
    # retain the operator HOME and exact native tool bindings unchanged.
    cls.environment["CARGO_HOME"] = str(cls.seed / "cargo-home")


def prepare_seed(root, archive_directory, cargo, environment):
    """Provision once before complete capture, including native Cargo bookkeeping."""
    prepare_vendor(root / "vendor", archive_directory)
    (root / "cargo-home").mkdir()
    library = root / "library"
    (library / "src").mkdir(parents=True)
    (library / "Cargo.toml").write_text(
        '[package]\nname="qualified-fixture"\nversion="1.0.0"\nedition="2021"\n'
        '[features]\ndefault=["checked"]\nchecked=[]\n',
        encoding="utf-8",
    )
    (library / "src/lib.rs").write_text(
        '#[cfg(feature="checked")] pub fn answer(input: &str) -> String {\n'
        ' format!(r#"{{"answer":42,"echo":{}}}"#, input)\n}\n',
        encoding="utf-8",
    )
    workspace = root / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / ".cargo").mkdir()
    (workspace / ".cargo/config.toml").write_text(
        '[source.crates-io]\nreplace-with="fixture-vendor"\n'
        "[source.fixture-vendor]\ndirectory="
        + json.dumps(str(root / "vendor"))
        + "\n[net]\noffline=true\n",
        encoding="utf-8",
    )
    (workspace / "Cargo.toml").write_text(
        '[package]\nname="litai-library-acceptance"\nversion="0.0.0"\nedition="2021"\n'
        '[workspace]\n[dependencies]\nqualified_fixture={package="qualified-fixture",path='
        + json.dumps(str(library))
        + ',default-features=false,features=["checked","default"]}\n'
        'serde_json={version="=1.0.151",registry="crates-io",default-features=true,features=[]}\n',
        encoding="utf-8",
    )
    (workspace / "src/main.rs").write_bytes(HARNESS)
    values = {
        **environment,
        "CARGO_HOME": str(root / "cargo-home"),
        "CARGO_NET_OFFLINE": "true",
        "CARGO_TARGET_DIR": str(root / "target"),
    }
    for phase, args in (
        ("lock", ("generate-lockfile", "--offline")),
        ("build", ("build", "--offline", "--locked")),
    ):
        result = run_bounded_process(
            (*cargo.command, *args),
            cwd=workspace,
            environment=values,
            timeout_seconds=180,
            stdout_limit_bytes=1024**2,
            stderr_limit_bytes=1024**2,
            terminate_descendants=True,
            trace=False,
            error_prefix="fixture.rust_" + phase,
        )
        (root / (phase + ".stdout")).write_bytes(result.stdout)
        (root / (phase + ".stderr")).write_bytes(result.stderr)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))


# Authored expected resolution for the pinned real crates. Keep all Cargo
# metadata edges, including cfg(any()) rows, rather than pretending that only
# compiled runtime crates constitute the complete locked dependency graph.
GRAPH = {
    "itoa": ("1.0.18", (), ()),
    "memchr": ("2.8.0", ("alloc", "std"), ()),
    "proc-macro2": (
        "1.0.106",
        ("proc-macro",),
        (("unicode_ident", "unicode-ident", None),),
    ),
    "quote": ("1.0.45", ("proc-macro",), (("proc_macro2", "proc-macro2", None),)),
    "serde": ("1.0.228", (), (("serde_core", "serde_core", None),)),
    "serde_core": (
        "1.0.228",
        ("result", "std"),
        (("serde_derive", "serde_derive", "cfg(any())"),),
    ),
    "serde_derive": (
        "1.0.228",
        ("default",),
        (
            ("proc_macro2", "proc-macro2", None),
            ("quote", "quote", None),
            ("syn", "syn", None),
        ),
    ),
    "serde_json": (
        "1.0.151",
        ("default", "std"),
        (
            ("itoa", "itoa", None),
            ("memchr", "memchr", None),
            ("serde", "serde", "cfg(any())"),
            ("serde_core", "serde_core", None),
            ("zmij", "zmij", None),
        ),
    ),
    "syn": (
        "2.0.117",
        ("clone-impls", "derive", "parsing", "printing", "proc-macro"),
        (
            ("proc_macro2", "proc-macro2", None),
            ("quote", "quote", None),
            ("unicode_ident", "unicode-ident", None),
        ),
    ),
    "unicode-ident": ("1.0.24", (), ()),
    "zmij": ("1.0.21", (), ()),
}


def digest(content):
    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(content).hexdigest())


def resolution(*, library_manifest="Cargo.toml"):
    packages = {
        name: RustHarnessResolvedPackage(
            "input:" + VENDOR_SCOPE,
            f"{name}-{version}/Cargo.toml",
            name,
            version,
            REGISTRY,
            features,
            (),
        )
        for name, (version, features, _edges) in GRAPH.items()
    }
    packages["library"] = RustHarnessResolvedPackage(
        "library",
        library_manifest,
        "qualified-fixture",
        "1.0.0",
        None,
        ("checked", "default"),
        (),
    )
    packages["harness"] = RustHarnessResolvedPackage(
        "harness",
        "Cargo.toml",
        "litai-library-acceptance",
        "0.0.0",
        None,
        (),
        (),
    )
    for name, (_version, _features, edges) in GRAPH.items():
        packages[name] = replace(
            packages[name],
            dependencies=tuple(
                sorted(
                    (
                        RustHarnessResolvedDependency(
                            alias, packages[provider].identity, None, target
                        )
                        for alias, provider, target in edges
                    ),
                    key=lambda edge: edge.key,
                )
            ),
        )
    packages["harness"] = replace(
        packages["harness"],
        dependencies=(
            RustHarnessResolvedDependency(
                "qualified_fixture", packages["library"].identity, None, None
            ),
            RustHarnessResolvedDependency(
                "serde_json", packages["serde_json"].identity, None, None
            ),
        ),
    )
    return RustHarnessResolution(
        packages["harness"].identity,
        packages["library"].identity,
        tuple(sorted(packages.values(), key=lambda row: row.identity.uri)),
    )


class NativeRustVerifierFixture:
    """Copy a once-provisioned native seed, then author its exact current oracle."""

    def __init__(self, root: Path, seed: Path, cargo, rustc):
        self.root = root
        self.authority = root / "verification"
        self.authority.mkdir()
        self.export = root / "library"
        shutil.copytree(seed / "library", self.export)
        for name in ("vendor", "cargo-home"):
            shutil.copytree(seed / name, self.authority / name)
        workspace = self.authority / "workspace"
        (workspace / ".cargo").mkdir(parents=True)
        (workspace / ".cargo/config.toml").write_text(
            '[source.crates-io]\nreplace-with="fixture-vendor"\n'
            "[source.fixture-vendor]\ndirectory="
            + json.dumps(str(self.authority / "vendor"))
            + "\n[net]\noffline=true\n",
            encoding="utf-8",
        )
        self.harness = (seed / "workspace/src/main.rs").read_bytes()
        (self.authority / "harness.rs").write_bytes(self.harness)
        self.lock = (seed / "workspace/Cargo.lock").read_bytes()
        (self.authority / "Cargo.lock").write_bytes(self.lock)
        self.cargo, self.rustc = cargo, rustc
        interface = canonical_identity(
            {"fixture-public-interface": "answer:json-text->json-text"}
        )
        self.surface = LibraryImportSurface(
            "rust",
            "qualified_fixture",
            (
                LibraryCapabilityImport(
                    "qualified.answer", interface, "qualified_fixture", ("answer",)
                ),
            ),
        )
        self.interface = interface
        self.author_oracle()

    def author_oracle(
        self, *, specification_identity=None, library_manifest="Cargo.toml"
    ):
        roots = (
            ("cargo-home", "cargo-home"),
            (VENDOR_SCOPE, "vendor"),
            ("workspace", "workspace"),
        )
        budget = RetainedCargoInputBudget(4096, 8192, 2 * 1024**2, 32 * 1024**2)
        policy = RetainedCargoCustodyPolicy(
            tuple(RetainedCargoOwnerBudget(scope, budget) for scope, _ in roots),
            RetainedCargoInputBudget(12288, 24576, 2 * 1024**2, 96 * 1024**2),
        )
        owners = capture_retained_cargo_input_owners_v2(
            tuple(
                RetainedCargoInputRootV2(scope, self.authority / path)
                for scope, path in roots
            ),
            policy=policy,
            maximum_metadata_bytes=96 * 1024**2,
        )
        declaration = RustLibraryHarnessDependencies(
            (
                RustHarnessRegistryDependency(
                    "serde_json",
                    "serde_json",
                    "1.0.151",
                    "crates-io",
                    REGISTRY,
                    True,
                    (),
                ),
            ),
            RustHarnessLockfile("Cargo.lock", digest(self.lock), len(self.lock)),
            RustHarnessInputSources(roots, policy, owners.identity, 96 * 1024**2),
            resolution(library_manifest=library_manifest),
            self.cargo.toolchain_identity,
            self.rustc.toolchain_identity,
            180,
            1024**2,
            1024**2,
        )
        document = {
            "schema": "literate-ai/library-acceptance-oracle@2",
            "component": "qualified-fixture",
            "specification_set_identity": (
                specification_identity
                or canonical_identity({"native-fixture": "answer42"})
            ).uri,
            "public_interface_identities": [self.interface.uri],
            "import_surface_identity": self.surface.identity.uri,
            "language": "rust",
            "harness": "harness.rs",
            "harness_identity": digest(self.harness).uri,
            "cases": [
                {
                    "case_id": "actual",
                    "capability": self.surface.capabilities[0].capability,
                    "arguments": [CASE_INPUT],
                    "expected_result": {"answer": 42, "echo": CASE_INPUT},
                }
            ],
            "rust_harness": declaration.to_dict(),
        }
        self.oracle_path = self.authority / "qualified-fixture.json"
        self.oracle_path.write_text(
            json.dumps(document, sort_keys=True), encoding="utf-8"
        )
        self.oracle = load_versioned_library_acceptance(
            self.oracle_path, "qualified-fixture"
        )
