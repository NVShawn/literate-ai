"""Flavor-contributed Standard command-profile authority.

These records describe portable conventions selected by a Flavor.  They do not
contain host paths or observed tool identities; the filesystem projector resolves
those only after the Component lock has selected the exact profile bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, ClassVar

from .._validation import contract_fields, enum_value, fail, string_value
from ..identity import ContentIdentity, contract_identity
from ._common import portable_name

STANDARD_LANGUAGE_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-language-command-profile"
)
STANDARD_BUILD_SYSTEM_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-build-system-command-profile"
)
STANDARD_REPO_MAN_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-repo-man-command-profile"
)
STANDARD_MAKE_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-make-command-profile"
)
STANDARD_CMAKE_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-cmake-command-profile"
)
STANDARD_CARGO_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-cargo-command-profile"
)
STANDARD_MIX_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-mix-command-profile"
)
STANDARD_NPM_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-npm-command-profile"
)
STANDARD_PYTHON_WHEEL_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-python-wheel-command-profile"
)
STANDARD_PLATFORM_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-platform-command-profile"
)
STANDARD_ACCELERATOR_COMMAND_PROFILE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-accelerator-command-profile"
)
STANDARD_COMMAND_PROFILE_CONTENT_KIND = "standard-command-profile"


def _relative_path(value: object, path: str) -> str:
    raw = string_value(value, path)
    candidate = PurePosixPath(raw)
    if (
        candidate.is_absolute()
        or "\\" in raw
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.as_posix() != raw
    ):
        fail(path, "must be a canonical relative POSIX path")
    return raw


def standard_entrypoint_source_path(
    primary_source_entrypoint: str,
    logical_entrypoint_path: str,
    *,
    primary: bool,
) -> str:
    """Project one authored logical entrypoint into generated source custody.

    Component ``entrypoints[].path`` values are portable product-facing paths
    (historically ``run``), while the selected language Flavor owns the generated
    source convention (for example ``source/main.py``).  The primary entrypoint
    therefore retains the Flavor's exact legacy path.  Additional entrypoints use
    their authored logical path beneath ``source/`` and inherit the primary source
    suffix when they do not declare one.  Keeping this projection in the command
    profile contract gives generation and runtime dispatch one deterministic
    authority instead of letting each adapter guess a filesystem path.
    """

    primary_path = PurePosixPath(
        _relative_path(
            primary_source_entrypoint,
            "standard_entrypoint_source_path.primary_source_entrypoint",
        )
    )
    logical = PurePosixPath(
        _relative_path(
            logical_entrypoint_path,
            "standard_entrypoint_source_path.logical_entrypoint_path",
        )
    )
    if primary:
        return primary_path.as_posix()
    if logical.parts[0] != "source":
        logical = PurePosixPath("source") / logical
    if not logical.suffix and primary_path.suffix:
        logical = logical.with_suffix(primary_path.suffix)
    return _relative_path(
        logical.as_posix(), "standard_entrypoint_source_path.projected"
    )


class StandardArtifactLayout(StrEnum):
    FILE = "file"
    TREE = "tree"


class StandardLanguageBuildStrategy(StrEnum):
    ELIXIR_TREE = "elixir-tree"
    PYTHON_TREE = "python-tree"
    JAVASCRIPT_TREE = "javascript-tree"
    TYPESCRIPT_TREE = "typescript-tree"
    RUST_EXECUTABLE = "rust-executable"
    CPP_EXECUTABLE = "cpp-executable"
    SWIFT_EXECUTABLE = "swift-executable"
    GO_EXECUTABLE = "go-executable"
    ZIG_EXECUTABLE = "zig-executable"


class StandardLanguageRuntimeStrategy(StrEnum):
    ELIXIR = "elixir"
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    NATIVE_EXECUTABLE = "native-executable"


@dataclass(frozen=True, slots=True)
class StandardLanguageCommandProfile:
    """Portable source, test, artifact, and tool conventions for one language Flavor."""

    target: str
    toolchain: str
    source_entrypoint: str
    generated_test_entrypoint: str
    artifact_layout: StandardArtifactLayout
    artifact_entrypoint: str
    bazel_output_path: str
    build_strategy: StandardLanguageBuildStrategy
    runtime_strategy: StandardLanguageRuntimeStrategy
    media_type: str
    cpp_library_kind: str | None = None

    SCHEMA: ClassVar[str] = STANDARD_LANGUAGE_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.target, "StandardLanguageCommandProfile.target")
        portable_name(self.toolchain, "StandardLanguageCommandProfile.toolchain")
        _relative_path(
            self.source_entrypoint,
            "StandardLanguageCommandProfile.source_entrypoint",
        )
        _relative_path(
            self.generated_test_entrypoint,
            "StandardLanguageCommandProfile.generated_test_entrypoint",
        )
        if not isinstance(self.artifact_layout, StandardArtifactLayout):
            fail(
                "StandardLanguageCommandProfile.artifact_layout",
                "must be a StandardArtifactLayout",
            )
        _relative_path(
            self.artifact_entrypoint,
            "StandardLanguageCommandProfile.artifact_entrypoint",
        )
        _relative_path(
            self.bazel_output_path,
            "StandardLanguageCommandProfile.bazel_output_path",
        )
        if not isinstance(self.build_strategy, StandardLanguageBuildStrategy):
            fail(
                "StandardLanguageCommandProfile.build_strategy",
                "must be a StandardLanguageBuildStrategy",
            )
        if not isinstance(self.runtime_strategy, StandardLanguageRuntimeStrategy):
            fail(
                "StandardLanguageCommandProfile.runtime_strategy",
                "must be a StandardLanguageRuntimeStrategy",
            )
        string_value(
            self.media_type,
            "StandardLanguageCommandProfile.media_type",
            max_length=255,
        )
        if self.cpp_library_kind is not None:
            string_value(
                self.cpp_library_kind, "StandardLanguageCommandProfile.cpp_library_kind"
            )
            if (
                self.target != "cpp"
                or self.build_strategy
                is not StandardLanguageBuildStrategy.CPP_EXECUTABLE
            ):
                fail(
                    "StandardLanguageCommandProfile.cpp_library_kind",
                    "requires the C++ language profile",
                )
            if self.cpp_library_kind not in {"static", "shared"}:
                fail(
                    "StandardLanguageCommandProfile.cpp_library_kind",
                    "requires static or shared production",
                )
        expected_layout = (
            StandardArtifactLayout.TREE
            if self.build_strategy
            in {
                StandardLanguageBuildStrategy.ELIXIR_TREE,
                StandardLanguageBuildStrategy.PYTHON_TREE,
                StandardLanguageBuildStrategy.JAVASCRIPT_TREE,
                StandardLanguageBuildStrategy.TYPESCRIPT_TREE,
            }
            else StandardArtifactLayout.FILE
        )
        if self.artifact_layout is not expected_layout:
            fail(
                "StandardLanguageCommandProfile.artifact_layout",
                "does not match the selected build strategy",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        value = {
            "schema": self.SCHEMA,
            "target": self.target,
            "toolchain": self.toolchain,
            "source_entrypoint": self.source_entrypoint,
            "generated_test_entrypoint": self.generated_test_entrypoint,
            "artifact_layout": self.artifact_layout.value,
            "artifact_entrypoint": self.artifact_entrypoint,
            "bazel_output_path": self.bazel_output_path,
            "build_strategy": self.build_strategy.value,
            "runtime_strategy": self.runtime_strategy.value,
            "media_type": self.media_type,
        }
        if self.cpp_library_kind is not None:
            value["cpp_library_kind"] = self.cpp_library_kind
        return value

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardLanguageCommandProfile",
    ) -> StandardLanguageCommandProfile:
        names = frozenset(
            {
                "target",
                "toolchain",
                "source_entrypoint",
                "generated_test_entrypoint",
                "artifact_layout",
                "artifact_entrypoint",
                "bazel_output_path",
                "build_strategy",
                "runtime_strategy",
                "media_type",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=names,
            optional=frozenset({"cpp_library_kind"}),
        )
        return cls(
            target=portable_name(data["target"], f"{path}.target"),
            toolchain=portable_name(data["toolchain"], f"{path}.toolchain"),
            source_entrypoint=_relative_path(
                data["source_entrypoint"], f"{path}.source_entrypoint"
            ),
            generated_test_entrypoint=_relative_path(
                data["generated_test_entrypoint"],
                f"{path}.generated_test_entrypoint",
            ),
            artifact_layout=enum_value(
                StandardArtifactLayout,
                data["artifact_layout"],
                f"{path}.artifact_layout",
            ),
            artifact_entrypoint=_relative_path(
                data["artifact_entrypoint"], f"{path}.artifact_entrypoint"
            ),
            bazel_output_path=_relative_path(
                data["bazel_output_path"], f"{path}.bazel_output_path"
            ),
            build_strategy=enum_value(
                StandardLanguageBuildStrategy,
                data["build_strategy"],
                f"{path}.build_strategy",
            ),
            runtime_strategy=enum_value(
                StandardLanguageRuntimeStrategy,
                data["runtime_strategy"],
                f"{path}.runtime_strategy",
            ),
            media_type=string_value(data["media_type"], f"{path}.media_type"),
            cpp_library_kind=(
                string_value(data["cpp_library_kind"], f"{path}.cpp_library_kind")
                if "cpp_library_kind" in data
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardBuildSystemCommandProfile:
    """One Bazel root target selected by the Bazel build-system Flavor."""

    target: str
    toolchain: str
    target_label: str

    SCHEMA: ClassVar[str] = STANDARD_BUILD_SYSTEM_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.target, "StandardBuildSystemCommandProfile.target")
        portable_name(self.toolchain, "StandardBuildSystemCommandProfile.toolchain")
        if self.target != "bazel" or self.toolchain != "bazel":
            fail(
                "StandardBuildSystemCommandProfile.target",
                "the v2 target-label profile is reserved for Bazel",
            )
        raw = string_value(
            self.target_label, "StandardBuildSystemCommandProfile.target_label"
        )
        if not raw.startswith("//:") or len(raw) <= 3 or "/" in raw[3:]:
            fail(
                "StandardBuildSystemCommandProfile.target_label",
                "must be one root-package target label",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target,
            "toolchain": self.toolchain,
            "target_label": self.target_label,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardBuildSystemCommandProfile",
    ) -> StandardBuildSystemCommandProfile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "toolchain", "target_label"}),
        )
        return cls(
            portable_name(data["target"], f"{path}.target"),
            portable_name(data["toolchain"], f"{path}.toolchain"),
            string_value(data["target_label"], f"{path}.target_label"),
        )


@dataclass(frozen=True, slots=True)
class StandardRepoManCommandProfile:
    """Retained repo_man authority selected beside disposable generated source."""

    target: str
    toolchain: str
    entrypoint: str
    build_target: str

    SCHEMA: ClassVar[str] = STANDARD_REPO_MAN_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.target, "StandardRepoManCommandProfile.target")
        portable_name(self.toolchain, "StandardRepoManCommandProfile.toolchain")
        if self.target != "repo-man" or self.toolchain != "repo.sh":
            fail(
                "StandardRepoManCommandProfile.target",
                "the repo_man profile requires the repo-man target and "
                "repo.sh toolchain",
            )
        entrypoint = _relative_path(
            self.entrypoint, "StandardRepoManCommandProfile.entrypoint"
        )
        if PurePosixPath(entrypoint).name not in {"repo.sh", "repo.bat"}:
            fail(
                "StandardRepoManCommandProfile.entrypoint",
                "must name a repo.sh or repo.bat driver",
            )
        portable_name(self.build_target, "StandardRepoManCommandProfile.build_target")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target,
            "toolchain": self.toolchain,
            "entrypoint": self.entrypoint,
            "build_target": self.build_target,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardRepoManCommandProfile",
    ) -> StandardRepoManCommandProfile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "toolchain", "entrypoint", "build_target"}),
        )
        return cls(
            portable_name(data["target"], f"{path}.target"),
            portable_name(data["toolchain"], f"{path}.toolchain"),
            _relative_path(data["entrypoint"], f"{path}.entrypoint"),
            portable_name(data["build_target"], f"{path}.build_target"),
        )


@dataclass(frozen=True, slots=True)
class StandardMakeCommandProfile:
    """Native Makefile and target authority selected by the Make Flavor."""

    target: str
    toolchain: str
    makefile: str
    build_target: str

    SCHEMA: ClassVar[str] = STANDARD_MAKE_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.target, "StandardMakeCommandProfile.target")
        portable_name(self.toolchain, "StandardMakeCommandProfile.toolchain")
        if self.target != "make" or self.toolchain != "make":
            fail(
                "StandardMakeCommandProfile.target",
                "the Make profile requires the make target and toolchain",
            )
        _relative_path(self.makefile, "StandardMakeCommandProfile.makefile")
        portable_name(self.build_target, "StandardMakeCommandProfile.build_target")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target,
            "toolchain": self.toolchain,
            "makefile": self.makefile,
            "build_target": self.build_target,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardMakeCommandProfile",
    ) -> StandardMakeCommandProfile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "toolchain", "makefile", "build_target"}),
        )
        return cls(
            portable_name(data["target"], f"{path}.target"),
            portable_name(data["toolchain"], f"{path}.toolchain"),
            _relative_path(data["makefile"], f"{path}.makefile"),
            portable_name(data["build_target"], f"{path}.build_target"),
        )


@dataclass(frozen=True, slots=True)
class StandardCMakeCommandProfile:
    """Native `CMakeLists.txt` and target authority selected by the CMake Flavor."""

    target: str
    toolchain: str
    cmakelists: str
    build_target: str

    SCHEMA: ClassVar[str] = STANDARD_CMAKE_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.target, "StandardCMakeCommandProfile.target")
        portable_name(self.toolchain, "StandardCMakeCommandProfile.toolchain")
        if self.target != "cmake" or self.toolchain != "cmake":
            fail(
                "StandardCMakeCommandProfile.target",
                "the CMake profile requires the cmake target and toolchain",
            )
        _relative_path(self.cmakelists, "StandardCMakeCommandProfile.cmakelists")
        portable_name(self.build_target, "StandardCMakeCommandProfile.build_target")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target,
            "toolchain": self.toolchain,
            "cmakelists": self.cmakelists,
            "build_target": self.build_target,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardCMakeCommandProfile",
    ) -> StandardCMakeCommandProfile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "toolchain", "cmakelists", "build_target"}),
        )
        return cls(
            portable_name(data["target"], f"{path}.target"),
            portable_name(data["toolchain"], f"{path}.toolchain"),
            _relative_path(data["cmakelists"], f"{path}.cmakelists"),
            portable_name(data["build_target"], f"{path}.build_target"),
        )


@dataclass(frozen=True, slots=True)
class StandardCargoCommandProfile:
    """Locked Cargo manifest and binary target selected by the Cargo Flavor."""

    target: str
    toolchain: str
    manifest: str
    binary: str

    SCHEMA: ClassVar[str] = STANDARD_CARGO_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.target, "StandardCargoCommandProfile.target")
        portable_name(self.toolchain, "StandardCargoCommandProfile.toolchain")
        if self.target != "cargo" or self.toolchain != "cargo":
            fail(
                "StandardCargoCommandProfile.target",
                "the Cargo profile requires the cargo target and toolchain",
            )
        _relative_path(self.manifest, "StandardCargoCommandProfile.manifest")
        if PurePosixPath(self.manifest).name != "Cargo.toml":
            fail("StandardCargoCommandProfile.manifest", "must name Cargo.toml")
        portable_name(self.binary, "StandardCargoCommandProfile.binary")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target,
            "toolchain": self.toolchain,
            "manifest": self.manifest,
            "binary": self.binary,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardCargoCommandProfile"
    ) -> StandardCargoCommandProfile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "toolchain", "manifest", "binary"}),
        )
        return cls(
            portable_name(data["target"], f"{path}.target"),
            portable_name(data["toolchain"], f"{path}.toolchain"),
            _relative_path(data["manifest"], f"{path}.manifest"),
            portable_name(data["binary"], f"{path}.binary"),
        )


@dataclass(frozen=True, slots=True)
class StandardMixCommandProfile:
    """Inert Mix intent and explicitly staged Hex toolchain convention."""

    target: str
    toolchain: str
    manifest: str

    SCHEMA: ClassVar[str] = STANDARD_MIX_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        if self.target != "mix" or self.toolchain != "hex":
            fail(
                "StandardMixCommandProfile.target",
                "requires the mix target and hex toolchain",
            )
        if self.manifest != "source/mix-project.json":
            fail(
                "StandardMixCommandProfile.manifest",
                "requires the canonical declarative Mix intent",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target,
            "toolchain": self.toolchain,
            "manifest": self.manifest,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardMixCommandProfile"
    ) -> StandardMixCommandProfile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "toolchain", "manifest"}),
        )
        return cls(
            string_value(data["target"], f"{path}.target"),
            string_value(data["toolchain"], f"{path}.toolchain"),
            string_value(data["manifest"], f"{path}.manifest"),
        )


@dataclass(frozen=True, slots=True)
class StandardNpmCommandProfile:
    """Locked npm manifest and lockfile selected by the npm packaging Flavor."""

    target: str
    toolchain: str
    manifest: str
    lockfile: str

    SCHEMA: ClassVar[str] = STANDARD_NPM_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.target, "StandardNpmCommandProfile.target")
        portable_name(self.toolchain, "StandardNpmCommandProfile.toolchain")
        if self.target != "npm" or self.toolchain != "npm":
            fail(
                "StandardNpmCommandProfile.target",
                "the npm profile requires the npm target and toolchain",
            )
        manifest = PurePosixPath(
            _relative_path(self.manifest, "StandardNpmCommandProfile.manifest")
        )
        lockfile = PurePosixPath(
            _relative_path(self.lockfile, "StandardNpmCommandProfile.lockfile")
        )
        if manifest.name != "package.json":
            fail("StandardNpmCommandProfile.manifest", "must name package.json")
        if lockfile.name != "package-lock.json":
            fail("StandardNpmCommandProfile.lockfile", "must name package-lock.json")
        if manifest.parent != lockfile.parent:
            fail(
                "StandardNpmCommandProfile.lockfile",
                "must be beside the selected package.json",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target,
            "toolchain": self.toolchain,
            "manifest": self.manifest,
            "lockfile": self.lockfile,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardNpmCommandProfile"
    ) -> StandardNpmCommandProfile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "toolchain", "manifest", "lockfile"}),
        )
        return cls(
            portable_name(data["target"], f"{path}.target"),
            portable_name(data["toolchain"], f"{path}.toolchain"),
            _relative_path(data["manifest"], f"{path}.manifest"),
            _relative_path(data["lockfile"], f"{path}.lockfile"),
        )


@dataclass(frozen=True, slots=True)
class StandardPythonWheelCommandProfile:
    """Binary-only Python dependency authority selected by a packaging Flavor."""

    target: str
    toolchain: str
    manifest: str
    lockfile: str

    SCHEMA: ClassVar[str] = STANDARD_PYTHON_WHEEL_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        if self.target != "pip" or self.toolchain != "python":
            fail(
                "StandardPythonWheelCommandProfile.target",
                "requires the pip packaging target and Python toolchain",
            )
        manifest = PurePosixPath(
            _relative_path(self.manifest, "StandardPythonWheelCommandProfile.manifest")
        )
        lockfile = PurePosixPath(
            _relative_path(self.lockfile, "StandardPythonWheelCommandProfile.lockfile")
        )
        if manifest.name not in {
            "requirements.txt",
            "requirements.in",
            "pyproject.toml",
        }:
            fail(
                "StandardPythonWheelCommandProfile.manifest",
                "must name a requirements manifest or static pyproject.toml",
            )
        if (
            lockfile.name != "python-wheel-lock.json"
            or manifest.parent != lockfile.parent
        ):
            fail(
                "StandardPythonWheelCommandProfile.lockfile",
                "must name python-wheel-lock.json beside the selected manifest",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target,
            "toolchain": self.toolchain,
            "manifest": self.manifest,
            "lockfile": self.lockfile,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardPythonWheelCommandProfile"
    ) -> StandardPythonWheelCommandProfile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "toolchain", "manifest", "lockfile"}),
        )
        return cls(
            portable_name(data["target"], f"{path}.target"),
            portable_name(data["toolchain"], f"{path}.toolchain"),
            _relative_path(data["manifest"], f"{path}.manifest"),
            _relative_path(data["lockfile"], f"{path}.lockfile"),
        )


@dataclass(frozen=True, slots=True)
class StandardPlatformCommandProfile:
    """Artifact naming convention for one selected operating-system Flavor."""

    target: str
    executable_suffix: str

    SCHEMA: ClassVar[str] = STANDARD_PLATFORM_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.target, "StandardPlatformCommandProfile.target")
        suffix = string_value(
            self.executable_suffix,
            "StandardPlatformCommandProfile.executable_suffix",
            nonempty=False,
            max_length=16,
        )
        if suffix not in {"", ".exe"}:
            fail(
                "StandardPlatformCommandProfile.executable_suffix",
                "must be empty or '.exe'",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target,
            "executable_suffix": self.executable_suffix,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardPlatformCommandProfile",
    ) -> StandardPlatformCommandProfile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "executable_suffix"}),
        )
        return cls(
            portable_name(data["target"], f"{path}.target"),
            string_value(
                data["executable_suffix"],
                f"{path}.executable_suffix",
                nonempty=False,
                max_length=16,
            ),
        )


@dataclass(frozen=True, slots=True)
class StandardAcceleratorCommandProfile:
    """Host accelerator tool required by one selected accelerator Flavor."""

    target: str
    toolchain: str
    applies_to_languages: tuple[str, ...]

    SCHEMA: ClassVar[str] = STANDARD_ACCELERATOR_COMMAND_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        portable_name(self.target, "StandardAcceleratorCommandProfile.target")
        portable_name(self.toolchain, "StandardAcceleratorCommandProfile.toolchain")
        if not self.applies_to_languages:
            fail(
                "StandardAcceleratorCommandProfile.applies_to_languages",
                "must contain at least one language target",
            )
        for index, language in enumerate(self.applies_to_languages):
            portable_name(
                language,
                f"StandardAcceleratorCommandProfile.applies_to_languages[{index}]",
            )
        if tuple(sorted(set(self.applies_to_languages))) != self.applies_to_languages:
            fail(
                "StandardAcceleratorCommandProfile.applies_to_languages",
                "must be sorted and unique",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "target": self.target,
            "toolchain": self.toolchain,
            "applies_to_languages": list(self.applies_to_languages),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "StandardAcceleratorCommandProfile"
    ) -> StandardAcceleratorCommandProfile:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"target", "toolchain", "applies_to_languages"}),
        )
        return cls(
            portable_name(data["target"], f"{path}.target"),
            portable_name(data["toolchain"], f"{path}.toolchain"),
            tuple(
                portable_name(item, f"{path}.applies_to_languages[{index}]")
                for index, item in enumerate(data["applies_to_languages"])
            ),
        )


StandardCommandProfile = (
    StandardLanguageCommandProfile
    | StandardBuildSystemCommandProfile
    | StandardRepoManCommandProfile
    | StandardMakeCommandProfile
    | StandardCMakeCommandProfile
    | StandardCargoCommandProfile
    | StandardMixCommandProfile
    | StandardNpmCommandProfile
    | StandardPythonWheelCommandProfile
    | StandardPlatformCommandProfile
    | StandardAcceleratorCommandProfile
)


def parse_standard_command_profile(
    value: Any, *, path: str = "StandardCommandProfile"
) -> StandardCommandProfile:
    if not isinstance(value, dict):
        fail(path, "must be an object")
    schema = value.get("schema")
    parsers = {
        StandardLanguageCommandProfile.SCHEMA: StandardLanguageCommandProfile.from_dict,
        StandardBuildSystemCommandProfile.SCHEMA: (
            StandardBuildSystemCommandProfile.from_dict
        ),
        StandardRepoManCommandProfile.SCHEMA: StandardRepoManCommandProfile.from_dict,
        StandardMakeCommandProfile.SCHEMA: StandardMakeCommandProfile.from_dict,
        StandardCMakeCommandProfile.SCHEMA: StandardCMakeCommandProfile.from_dict,
        StandardCargoCommandProfile.SCHEMA: StandardCargoCommandProfile.from_dict,
        StandardMixCommandProfile.SCHEMA: StandardMixCommandProfile.from_dict,
        StandardNpmCommandProfile.SCHEMA: StandardNpmCommandProfile.from_dict,
        StandardPythonWheelCommandProfile.SCHEMA: (
            StandardPythonWheelCommandProfile.from_dict
        ),
        StandardPlatformCommandProfile.SCHEMA: StandardPlatformCommandProfile.from_dict,
        StandardAcceleratorCommandProfile.SCHEMA: (
            StandardAcceleratorCommandProfile.from_dict
        ),
    }
    try:
        parser = parsers[schema]
    except (KeyError, TypeError):
        fail(f"{path}.schema", "must identify a Standard command-profile schema")
    return parser(value, path=path)


__all__ = [
    "STANDARD_BUILD_SYSTEM_COMMAND_PROFILE_SCHEMA",
    "STANDARD_REPO_MAN_COMMAND_PROFILE_SCHEMA",
    "STANDARD_ACCELERATOR_COMMAND_PROFILE_SCHEMA",
    "STANDARD_CMAKE_COMMAND_PROFILE_SCHEMA",
    "STANDARD_CARGO_COMMAND_PROFILE_SCHEMA",
    "STANDARD_MIX_COMMAND_PROFILE_SCHEMA",
    "STANDARD_COMMAND_PROFILE_CONTENT_KIND",
    "STANDARD_LANGUAGE_COMMAND_PROFILE_SCHEMA",
    "STANDARD_MAKE_COMMAND_PROFILE_SCHEMA",
    "STANDARD_NPM_COMMAND_PROFILE_SCHEMA",
    "STANDARD_PYTHON_WHEEL_COMMAND_PROFILE_SCHEMA",
    "STANDARD_PLATFORM_COMMAND_PROFILE_SCHEMA",
    "StandardArtifactLayout",
    "StandardAcceleratorCommandProfile",
    "StandardBuildSystemCommandProfile",
    "StandardRepoManCommandProfile",
    "StandardCMakeCommandProfile",
    "StandardCargoCommandProfile",
    "StandardMixCommandProfile",
    "StandardCommandProfile",
    "StandardLanguageBuildStrategy",
    "StandardLanguageCommandProfile",
    "StandardLanguageRuntimeStrategy",
    "StandardMakeCommandProfile",
    "StandardNpmCommandProfile",
    "StandardPythonWheelCommandProfile",
    "StandardPlatformCommandProfile",
    "parse_standard_command_profile",
]
