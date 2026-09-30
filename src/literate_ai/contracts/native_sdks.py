"""Original-source SDK byte custody; snapshots do not grant execution admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    fields,
    list_value,
    parse_tuple,
    string_tuple,
    string_value,
)
from .blobs import BlobRef
from .executable_components.commands import LibraryImportSurface
from .identity import ContentIdentity, canonical_identity, contract_identity
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths
from .repositories import RepositoryBuildCommand, _dependency_id


def native_sdk_consumer_build_identity(
    locked_authority: ContentIdentity, inputs: tuple[ContentIdentity, ...]
) -> ContentIdentity:
    """Bind consumer build authority to its exact selected SDK inputs."""
    if not inputs:
        return locked_authority
    return canonical_identity(
        {
            "schema": "literate-ai/native-sdk-consumer-build-authority@1",
            "locked_build_authority": locked_authority.to_dict(),
            "native_sdk_input_identities": [item.to_dict() for item in inputs],
        }
    )


@dataclass(frozen=True, slots=True)
class NativeSdkFile:
    path: str
    blob: BlobRef
    executable: bool = False

    def __post_init__(self) -> None:
        canonical_relative_posix_path(self.path, label="NativeSdkFile.path")
        if not isinstance(self.blob, BlobRef):
            raise ValueError("SDK file requires an immutable blob")
        if not isinstance(self.executable, bool):
            raise ValueError("SDK file executable mode must be a boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "blob": self.blob.to_dict(),
            "executable": self.executable,
        }

    @classmethod
    def from_dict(cls, value: Any) -> NativeSdkFile:
        data = fields(
            value, path=cls.__name__, required=frozenset({"path", "blob", "executable"})
        )
        return cls(
            string_value(data["path"], "NativeSdkFile.path"),
            BlobRef.from_dict(data["blob"]),
            bool_value(data["executable"], "NativeSdkFile.executable"),
        )


@dataclass(frozen=True, slots=True)
class NativeSdkSnapshot:
    """Bind copied SDK bytes to declared inputs, without authenticating those inputs.

    The future admission verifier must resolve and verify each input identity and
    observe the actual target/ABI and runtime closure before consumer execution.
    Neither construction nor materialization is an admission or acceptance receipt.
    """

    source_lock_identity: ContentIdentity
    recipe_identity: ContentIdentity
    target_identity: ContentIdentity
    license_identity: ContentIdentity
    import_surface: LibraryImportSurface
    import_root: str
    native_libraries: tuple[str, ...]
    files: tuple[NativeSdkFile, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:native-sdk-snapshot"

    def __post_init__(self) -> None:
        for name in (
            "source_lock_identity",
            "recipe_identity",
            "target_identity",
            "license_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                raise ValueError(f"NativeSdkSnapshot.{name} must be a content identity")
        if not isinstance(self.import_surface, LibraryImportSurface):
            raise ValueError("SDK requires a typed public import surface")
        import_root = canonical_relative_posix_path(
            self.import_root, label="NativeSdkSnapshot.import_root"
        )
        if (
            not isinstance(self.files, tuple)
            or not self.files
            or len(self.files) > 16384
        ):
            raise ValueError("SDK requires between 1 and 16384 immutable files")
        if any(not isinstance(item, NativeSdkFile) for item in self.files):
            raise ValueError("SDK files must be typed")
        names = tuple(item.path for item in self.files)
        canonical_relative_posix_paths(names, label="NativeSdkSnapshot.files")
        if names != tuple(sorted(names)):
            raise ValueError("SDK files must be canonically ordered")
        if not any(
            import_root in canonical_relative_posix_path(name, label="SDK file").parents
            for name in names
        ):
            raise ValueError("SDK import root contains no files")
        if (
            not isinstance(self.native_libraries, tuple)
            or not 1 <= len(self.native_libraries) <= 16384
        ):
            raise ValueError("SDK requires declared native library files")
        canonical_relative_posix_paths(
            self.native_libraries, label="NativeSdkSnapshot.native_libraries"
        )
        if self.native_libraries != tuple(sorted(self.native_libraries)):
            raise ValueError("SDK native libraries must be canonically ordered")
        if not set(self.native_libraries).issubset(names):
            raise ValueError("SDK native library is absent from its exact file tree")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "source_lock_identity": self.source_lock_identity.to_dict(),
            "recipe_identity": self.recipe_identity.to_dict(),
            "target_identity": self.target_identity.to_dict(),
            "license_identity": self.license_identity.to_dict(),
            "import_surface": self.import_surface.to_dict(),
            "import_root": self.import_root,
            "native_libraries": list(self.native_libraries),
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_dict(cls, value: Any) -> NativeSdkSnapshot:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "source_lock_identity",
                    "recipe_identity",
                    "target_identity",
                    "license_identity",
                    "import_surface",
                    "import_root",
                    "native_libraries",
                    "files",
                }
            ),
        )
        return cls(
            ContentIdentity.from_dict(data["source_lock_identity"]),
            ContentIdentity.from_dict(data["recipe_identity"]),
            ContentIdentity.from_dict(data["target_identity"]),
            ContentIdentity.from_dict(data["license_identity"]),
            LibraryImportSurface.from_dict(data["import_surface"]),
            string_value(data["import_root"], "NativeSdkSnapshot.import_root"),
            tuple(
                string_value(item, "SDK native library")
                for item in list_value(data["native_libraries"], "SDK native libraries")
            ),
            tuple(
                NativeSdkFile.from_dict(item)
                for item in list_value(data["files"], "SDK files")
            ),
        )


@dataclass(frozen=True, slots=True)
class NativeSdkBuildLayout:
    """Portable SDK output layout and exact license/import/target authority."""

    sdk_root: str
    import_root: str
    import_surface: LibraryImportSurface
    native_libraries: tuple[str, ...]
    source_license: str
    output_license: str
    license_identity: ContentIdentity
    operating_system: str
    architecture: str

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:native-sdk-build-layout"

    def __post_init__(self) -> None:
        if self.operating_system not in {
            "linux",
            "macos",
            "windows",
        } or self.architecture not in {"x86_64", "arm64"}:
            raise ValueError("SDK layout requires a supported exact OS/architecture")
        for name in ("sdk_root", "import_root", "source_license", "output_license"):
            canonical_relative_posix_path(getattr(self, name), label=f"SDK {name}")
        if not isinstance(self.import_surface, LibraryImportSurface):
            raise TypeError("SDK layout requires a public import surface")
        if not isinstance(self.license_identity, ContentIdentity):
            raise TypeError("SDK layout requires an exact license identity")
        if (
            not isinstance(self.native_libraries, tuple)
            or not 1 <= len(self.native_libraries) <= 16384
        ):
            raise ValueError("SDK layout requires native library paths")
        canonical_relative_posix_paths(self.native_libraries, label="SDK libraries")
        if self.native_libraries != tuple(sorted(self.native_libraries)):
            raise ValueError("SDK libraries must be canonically ordered")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "sdk_root": self.sdk_root,
            "import_root": self.import_root,
            "import_surface": self.import_surface.to_dict(),
            "native_libraries": list(self.native_libraries),
            "source_license": self.source_license,
            "output_license": self.output_license,
            "license_identity": self.license_identity.to_dict(),
            "operating_system": self.operating_system,
            "architecture": self.architecture,
        }

    @classmethod
    def from_dict(cls, value: Any) -> NativeSdkBuildLayout:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "sdk_root",
                    "import_root",
                    "import_surface",
                    "native_libraries",
                    "source_license",
                    "output_license",
                    "license_identity",
                    "operating_system",
                    "architecture",
                }
            ),
        )
        return cls(
            string_value(data["sdk_root"], "SDK sdk_root"),
            string_value(data["import_root"], "SDK import_root"),
            LibraryImportSurface.from_dict(data["import_surface"]),
            string_tuple(data["native_libraries"], "SDK native_libraries"),
            string_value(data["source_license"], "SDK source_license"),
            string_value(data["output_license"], "SDK output_license"),
            ContentIdentity.from_dict(data["license_identity"]),
            string_value(data["operating_system"], "SDK operating_system"),
            string_value(data["architecture"], "SDK architecture"),
        )


NATIVE_SDK_BUILD_RECIPE_CONTENT_KIND = "native-sdk-build-recipe"


@dataclass(frozen=True, slots=True)
class NativeSdkBuildRecipe:
    """Authored original-source commands selected through a Flavor contribution."""

    dependency_id: str
    layout: NativeSdkBuildLayout
    tools: tuple[str, ...]
    commands: tuple[RepositoryBuildCommand, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:native-sdk-build-recipe"

    def __post_init__(self) -> None:
        _dependency_id(self.dependency_id, "NativeSdkBuildRecipe.dependency_id")
        if not isinstance(self.layout, NativeSdkBuildLayout):
            raise ValueError("SDK recipe requires a typed layout")
        if not isinstance(self.tools, tuple) or not 1 <= len(self.tools) <= 64:
            raise ValueError("SDK recipe requires 1 to 64 named tools")
        for name in self.tools:
            _dependency_id(name, "NativeSdkBuildRecipe.tools")
        if self.tools != tuple(sorted(set(self.tools))):
            raise ValueError("SDK recipe tools must be unique and canonically ordered")
        if not isinstance(self.commands, tuple) or not 1 <= len(self.commands) <= 64:
            raise ValueError("SDK recipe requires 1 to 64 commands")
        if any(not isinstance(item, RepositoryBuildCommand) for item in self.commands):
            raise ValueError("SDK recipe commands must be typed")
        if len({item.step_id for item in self.commands}) != len(self.commands):
            raise ValueError("SDK recipe command step IDs must be unique")
        if any(item.argv[0] not in self.tools for item in self.commands):
            raise ValueError("SDK recipe commands require a declared named tool")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "dependency_id": self.dependency_id,
            "layout": self.layout.to_dict(),
            "tools": list(self.tools),
            "commands": [item.to_dict() for item in self.commands],
        }

    @classmethod
    def from_dict(cls, value: Any) -> NativeSdkBuildRecipe:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"dependency_id", "layout", "tools", "commands"}),
        )
        return cls(
            string_value(data["dependency_id"], "SDK dependency_id"),
            NativeSdkBuildLayout.from_dict(data["layout"]),
            string_tuple(data["tools"], "SDK tools"),
            parse_tuple(
                data["commands"], "SDK commands", RepositoryBuildCommand.from_dict
            ),
        )
