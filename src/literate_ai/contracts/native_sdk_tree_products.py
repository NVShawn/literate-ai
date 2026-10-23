"""Explicit tree-aware SDK authority; decoded claims do not admit a producer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, ClassVar

from ._validation import (
    bool_value,
    contract_fields,
    list_value,
    string_tuple,
    string_value,
)
from .executable_components.commands import LibraryImportSurface
from .identity import ContentIdentity, contract_identity
from .native_sdk_trees import NativeSdkTree
from .native_sdks import NativeSdkFile
from .paths import canonical_relative_posix_path, canonical_relative_posix_paths
from .repositories import RepositoryBuildCommand, _dependency_id
from .retained_project import RetainedProjectLimits

NATIVE_SDK_TREE_BUILD_RECIPE_CONTENT_KIND = "native-sdk-tree-build-recipe"


def _library_paths(value: tuple[str, ...], *, case_sensitive: bool) -> None:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 16384:
        raise ValueError("SDK requires 1 to 16384 declared native libraries")
    for path in value:
        canonical_relative_posix_path(path, label="SDK native library")
    if not case_sensitive:
        canonical_relative_posix_paths(value, label="SDK native libraries")
    if value != tuple(sorted(set(value))):
        raise ValueError("SDK native libraries must be unique and canonically ordered")


@dataclass(frozen=True, slots=True)
class NativeSdkTreeSnapshot:
    """Bind complete alias topology to claimed SDK inputs, without admission."""

    source_lock_identity: ContentIdentity
    recipe_identity: ContentIdentity
    target_identity: ContentIdentity
    license_identity: ContentIdentity
    import_surface: LibraryImportSurface
    import_root: str
    native_libraries: tuple[str, ...]
    tree: NativeSdkTree

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:native-sdk-tree-snapshot@1"

    def __post_init__(self) -> None:
        for name in (
            "source_lock_identity",
            "recipe_identity",
            "target_identity",
            "license_identity",
        ):
            if not isinstance(getattr(self, name), ContentIdentity):
                raise TypeError("SDK tree snapshot requires exact input identities")
        if not isinstance(self.import_surface, LibraryImportSurface) or not isinstance(
            self.tree, NativeSdkTree
        ):
            raise TypeError("SDK tree snapshot requires typed public imports and tree")
        canonical_relative_posix_path(self.import_root, label="SDK tree import root")
        resolved = PurePosixPath(self.tree.resolve(self.import_root))
        if str(resolved) not in {".", *self.tree.directories} or not any(
            resolved in PurePosixPath(item.path).parents for item in self.tree.files
        ):
            raise ValueError(
                "SDK tree import root must resolve to a nonempty directory"
            )
        _library_paths(self.native_libraries, case_sensitive=self.tree.case_sensitive)
        files = {item.path for item in self.files}
        if any(
            path not in files for path in self.tree.resolve_all(self.native_libraries)
        ):
            raise ValueError(
                "SDK native library must resolve to a physical regular file"
            )

    @property
    def files(self) -> tuple[NativeSdkFile, ...]:
        return self.tree.files

    def file_at(self, path: str) -> NativeSdkFile:
        """Join one logical library/license path to its exact physical file blob."""
        resolved = self.tree.resolve(path)
        for item in self.tree.files:
            if item.path == resolved:
                return item
        raise ValueError("SDK declared file does not resolve to a regular member")

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
            "tree": self.tree.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> NativeSdkTreeSnapshot:
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
                    "tree",
                }
            ),
        )
        return cls(
            ContentIdentity.from_dict(data["source_lock_identity"]),
            ContentIdentity.from_dict(data["recipe_identity"]),
            ContentIdentity.from_dict(data["target_identity"]),
            ContentIdentity.from_dict(data["license_identity"]),
            LibraryImportSurface.from_dict(data["import_surface"]),
            string_value(data["import_root"], "SDK import root"),
            string_tuple(data["native_libraries"], "SDK native libraries"),
            NativeSdkTree.from_dict(data["tree"]),
        )


@dataclass(frozen=True, slots=True)
class NativeSdkTreeBuildLayout:
    """Bind explicit tree limits and naming semantics into planned SDK output."""

    sdk_root: str
    import_root: str
    import_surface: LibraryImportSurface
    native_libraries: tuple[str, ...]
    source_license: str
    output_license: str
    license_identity: ContentIdentity
    operating_system: str
    architecture: str
    limits: RetainedProjectLimits
    case_sensitive: bool = False

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:native-sdk-tree-build-layout@1"

    def __post_init__(self) -> None:
        if self.operating_system not in {
            "freebsd",
            "linux",
            "macos",
            "windows",
        } or self.architecture not in {"x86_64", "arm64"}:
            raise ValueError("SDK layout requires a supported exact OS/architecture")
        for name in ("sdk_root", "import_root", "source_license", "output_license"):
            canonical_relative_posix_path(getattr(self, name), label=f"SDK {name}")
        if not isinstance(self.import_surface, LibraryImportSurface) or not isinstance(
            self.license_identity, ContentIdentity
        ):
            raise TypeError("SDK layout requires typed import and license authority")
        if type(self.case_sensitive) is not bool or not isinstance(
            self.limits, RetainedProjectLimits
        ):
            raise TypeError(
                "SDK layout requires explicit naming semantics and typed bounds"
            )
        _library_paths(self.native_libraries, case_sensitive=self.case_sensitive)

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
            "limits": self.limits.to_dict(),
            "case_sensitive": self.case_sensitive,
        }

    @classmethod
    def from_dict(cls, value: Any) -> NativeSdkTreeBuildLayout:
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
                    "limits",
                    "case_sensitive",
                }
            ),
        )
        return cls(
            string_value(data["sdk_root"], "SDK root"),
            string_value(data["import_root"], "SDK import root"),
            LibraryImportSurface.from_dict(data["import_surface"]),
            string_tuple(data["native_libraries"], "SDK native libraries"),
            string_value(data["source_license"], "SDK source license"),
            string_value(data["output_license"], "SDK output license"),
            ContentIdentity.from_dict(data["license_identity"]),
            string_value(data["operating_system"], "SDK OS"),
            string_value(data["architecture"], "SDK architecture"),
            RetainedProjectLimits.from_dict(data["limits"]),
            bool_value(data["case_sensitive"], "SDK case sensitivity"),
        )


@dataclass(frozen=True, slots=True)
class NativeSdkTreeBuildRecipe:
    """Explicit authored tree producer recipe; construction grants no execution."""

    dependency_id: str
    layout: NativeSdkTreeBuildLayout
    tools: tuple[str, ...]
    commands: tuple[RepositoryBuildCommand, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:native-sdk-tree-build-recipe@1"

    def __post_init__(self) -> None:
        _dependency_id(self.dependency_id, "SDK tree recipe dependency")
        if not isinstance(self.layout, NativeSdkTreeBuildLayout):
            raise TypeError("SDK tree recipe requires an explicit tree layout")
        if not isinstance(self.tools, tuple) or not 1 <= len(self.tools) <= 64:
            raise ValueError("SDK tree recipe requires 1 to 64 named tools")
        for name in self.tools:
            _dependency_id(name, "SDK tree recipe tool")
        if self.tools != tuple(sorted(set(self.tools))):
            raise ValueError("SDK tree recipe tools must be unique and ordered")
        if not isinstance(self.commands, tuple) or not 1 <= len(self.commands) <= 64:
            raise ValueError("SDK tree recipe requires 1 to 64 commands")
        if any(
            not isinstance(command, RepositoryBuildCommand) for command in self.commands
        ):
            raise TypeError("SDK tree recipe commands must be typed")
        if len({command.step_id for command in self.commands}) != len(self.commands):
            raise ValueError("SDK tree recipe step IDs must be unique")
        if any(command.argv[0] not in self.tools for command in self.commands):
            raise ValueError("SDK tree recipe commands require declared tools")

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "dependency_id": self.dependency_id,
            "layout": self.layout.to_dict(),
            "tools": list(self.tools),
            "commands": [command.to_dict() for command in self.commands],
        }

    @classmethod
    def from_dict(cls, value: Any) -> NativeSdkTreeBuildRecipe:
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset({"dependency_id", "layout", "tools", "commands"}),
        )
        return cls(
            string_value(data["dependency_id"], "SDK dependency"),
            NativeSdkTreeBuildLayout.from_dict(data["layout"]),
            string_tuple(data["tools"], "SDK tools"),
            tuple(
                RepositoryBuildCommand.from_dict(item)
                for item in list_value(data["commands"], "SDK commands")
            ),
        )
