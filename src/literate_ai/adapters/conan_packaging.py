"""Native Conan package construction over exact materialized package plans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.adapters.packaging import (
    logical_package_files,
    materialized_package_input_bytes,
    package_result_for,
    validate_materialized_package_root,
)
from literate_ai.application.packaging import PackagingError, verify_package_result
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.executable_components.packages import (
    PackagedFile,
    PackageFileKind,
    PackageKind,
    PackagePlan,
    PackageResult,
)
from literate_ai.contracts.identity import canonical_identity

_CONAN_COORDINATE = re.compile(r"^[a-z0-9][a-z0-9_+.-]*$")
_OUTPUT_LIMIT = 1024 * 1024
_CONAN_METADATA = frozenset({"conaninfo.txt", "conanmanifest.txt"})


def _conan_library_names(paths: Sequence[str]) -> tuple[str, ...]:
    """Project declared link filenames into Conan's logical library names."""

    names: list[str] = []
    for path in paths:
        name = PurePosixPath(path).name
        folded = name.casefold()
        if folded.endswith(".lib"):
            logical = name[:-4]
        elif name.startswith("lib") and folded.endswith(".a"):
            logical = name[3:-2]
        elif name.startswith("lib") and folded.endswith(".dylib"):
            logical = name[3:-6]
        elif name.startswith("lib") and ".so" in folded:
            logical = name[3 : folded.index(".so")]
        else:
            raise PackagingError(
                f"Conan cannot project declared link library name: {path}"
            )
        if not logical or _CONAN_COORDINATE.fullmatch(logical) is None:
            raise PackagingError(f"Conan link library name is not portable: {path}")
        names.append(logical)
    if len(set(names)) != len(names):
        raise PackagingError("Conan link library names must be unique")
    return tuple(names)


def _restored_reference(catalog: object, coordinate: str) -> str:
    """Resolve exactly one binary, including both revisions, in the fresh cache."""

    def only(value: object, label: str) -> tuple[str, dict]:
        if not isinstance(value, dict) or len(value) != 1:
            raise PackagingError(f"Conan verification requires one exact {label}")
        key, child = next(iter(value.items()))
        if not isinstance(key, str) or not isinstance(child, dict):
            raise PackagingError(f"Conan verification has invalid {label}")
        return key, child

    scope, cache = only(catalog, "cache")
    name, recipe = only(cache, "coordinate")
    if scope != "Local Cache" or name != coordinate:
        raise PackagingError("Conan verification restored another package")
    revision, recipe = only(recipe.get("revisions"), "recipe revision")
    package_id, binary = only(recipe.get("packages"), "binary package")
    package_revision, _ = only(binary.get("revisions"), "package revision")
    if any(
        re.fullmatch(r"[0-9a-f]{32,64}", part) is None
        for part in (revision, package_id, package_revision)
    ):
        raise PackagingError("Conan verification has invalid revision identifiers")
    return f"{coordinate}#{revision}:{package_id}#{package_revision}"


def _require_resolved_reference(graph: object, coordinate: str, expected: str) -> None:
    """Require a consumer graph to select one exact restored Conan binary."""

    if not isinstance(graph, dict):
        raise PackagingError("Conan consumer graph is invalid")
    graph_value = graph.get("graph")
    nodes = graph_value.get("nodes") if isinstance(graph_value, dict) else None
    if not isinstance(nodes, dict):
        raise PackagingError("Conan consumer graph has no nodes")
    selected = []
    for node in nodes.values():
        if not isinstance(node, dict):
            raise PackagingError("Conan consumer graph has an invalid node")
        reference = node.get("ref")
        if not isinstance(reference, str) or reference.split("#", 1)[0] != coordinate:
            continue
        package_id = node.get("package_id")
        package_revision = node.get("prev")
        if (
            re.fullmatch(r"[0-9a-f]{32,64}", reference.rpartition("#")[2]) is None
            or not isinstance(package_id, str)
            or re.fullmatch(r"[0-9a-f]{32,64}", package_id) is None
            or not isinstance(package_revision, str)
            or re.fullmatch(r"[0-9a-f]{32,64}", package_revision) is None
        ):
            raise PackagingError(
                "Conan consumer graph has an invalid package reference"
            )
        selected.append(f"{reference}:{package_id}#{package_revision}")
    if selected != [expected]:
        raise PackagingError("Conan consumer resolved another package revision")


def _verify_restored_payload(result: PackageResult, root: Path) -> None:
    expected = {item.path: item for item in result.files}
    if any(path.split("/", 1)[0].casefold() in _CONAN_METADATA for path in expected):
        raise PackagingError("Conan payload collides with native package metadata")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise PackagingError(
                "Conan payload must contain only regular files and directories"
            )
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if not _CONAN_METADATA <= actual:
        raise PackagingError("Conan native package metadata is missing")
    covered = set()
    for relative, item in expected.items():
        path = root.joinpath(*Path(relative).parts)
        content = materialized_package_input_bytes(path)
        if (
            len(content) != item.blob.size
            or hashlib.sha256(content).hexdigest() != item.blob.digest
        ):
            raise PackagingError(
                f"Conan payload differs from package result: {relative}"
            )
        if path.is_file():
            if os.name != "nt" and bool(path.stat().st_mode & 0o111) != item.executable:
                raise PackagingError(
                    f"Conan payload executable mode differs: {relative}"
                )
            covered.add(relative)
        else:
            covered.update(
                child.relative_to(root).as_posix()
                for child in path.rglob("*")
                if child.is_file()
            )
    if actual - _CONAN_METADATA != covered:
        raise PackagingError("Conan payload contains undeclared files")


def _executable_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ConanToolBinding:
    command: tuple[str, ...]
    executable: Path
    executable_identity: str
    version: str

    @classmethod
    def discover(cls, command: Sequence[str] = ("conan",)) -> ConanToolBinding:
        values = tuple(command)
        if not values or any(not isinstance(item, str) or not item for item in values):
            raise PackagingError("Conan command must be a non-empty argv sequence")
        selected = shutil.which(values[0])
        if selected is None:
            raise PackagingError(
                "Conan is unavailable; install and authenticate the selected package "
                "tool before native package construction"
            )
        executable = Path(selected).resolve(strict=True)
        resolved = (str(executable), *values[1:])
        completed = run_with_tree_kill(
            [*resolved, "--version"],
            text=True,
            timeout=15,
        )
        output = (completed.stdout + completed.stderr).strip()
        if (
            completed.returncode != 0
            or not output
            or len(output.encode()) > _OUTPUT_LIMIT
        ):
            raise PackagingError("Conan version probe failed")
        return cls(resolved, executable, _executable_digest(executable), output)

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/conan-tool-binding@1",
                "executable_identity": self.executable_identity,
                "version": self.version,
                "arguments": list(self.command[1:]),
            }
        )

    def require_unchanged(self) -> None:
        try:
            current = Path(self.command[0]).resolve(strict=True)
        except OSError as exc:
            raise PackagingError("Conan executable became unavailable") from exc
        if (
            current != self.executable
            or _executable_digest(current) != self.executable_identity
        ):
            raise PackagingError("Conan executable changed during package construction")


class ConanPackageAdapter:
    """Create one real Conan binary and portable cache archive without publication."""

    def __init__(self, name: str, version: str, tool: ConanToolBinding):
        if not _CONAN_COORDINATE.fullmatch(name):
            raise ValueError("Conan package name is invalid")
        if not _CONAN_COORDINATE.fullmatch(version):
            raise ValueError("Conan package version is invalid")
        if not isinstance(tool, ConanToolBinding):
            raise TypeError("Conan tool binding must be typed")
        self.name = name
        self.version = version
        self.tool = tool
        self._created: dict[str, bytes] = {}

    @property
    def packager_identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/conan-packager@1",
                "name": self.name,
                "version": self.version,
                "tool_identity": self.tool.identity.uri,
                "operation": "export-pkg+cache-save",
            }
        )

    def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        conan_home: Path,
        timeout: int = 180,
    ) -> subprocess.CompletedProcess[str]:
        self.tool.require_unchanged()
        environment = dict(os.environ)
        environment["CONAN_HOME"] = str(conan_home)
        environment["CONAN_NON_INTERACTIVE"] = "1"
        completed = run_with_tree_kill(
            [*self.tool.command, *arguments],
            cwd=cwd,
            env=environment,
            text=True,
            timeout=timeout,
        )
        self.tool.require_unchanged()
        output_size = len(completed.stdout.encode()) + len(completed.stderr.encode())
        if output_size > _OUTPUT_LIMIT:
            raise PackagingError("Conan command output exceeded its bounded limit")
        if completed.returncode != 0:
            detail = (completed.stdout + completed.stderr)[-4000:]
            raise PackagingError(f"Conan command failed: {detail}")
        return completed

    def package(
        self,
        plan: PackagePlan,
        *,
        materialized_root: Path,
        object_root: Path,
    ) -> PackageResult:
        if plan.package_kind is not PackageKind.ARCHIVE:
            raise PackagingError("Conan adapter requires the archive package kind")
        if plan.packager_identity != self.packager_identity:
            raise PackagingError("Conan metadata differs from exact packager authority")
        if any(
            item.path.split("/", 1)[0].casefold() in _CONAN_METADATA
            for item in plan.inputs
        ):
            raise PackagingError("Conan payload collides with native package metadata")
        root, materialized = validate_materialized_package_root(plan, materialized_root)
        output_root = Path(object_root)
        if output_root.is_symlink():
            raise PackagingError("Conan object root cannot be a symbolic link")
        output_root.mkdir(parents=True, exist_ok=True)
        output_root = output_root.resolve(strict=True)
        with tempfile.TemporaryDirectory(
            prefix="conan-package-", dir=output_root
        ) as temporary:
            return self._package_in_staging(
                plan,
                root=root,
                materialized=materialized,
                output_root=output_root,
                staging=Path(temporary),
            )

    def _package_in_staging(
        self,
        plan: PackagePlan,
        *,
        root: Path,
        materialized: dict[str, Path],
        output_root: Path,
        staging: Path,
    ) -> PackageResult:
        conan_home = staging / "conan-home"
        recipe_root = staging / "recipe"
        payload = recipe_root / "payload"
        recipe_root.mkdir()
        shutil.copytree(root, payload)
        executable_paths = {item.path for item in plan.entrypoints}
        for item in plan.inputs:
            path = payload.joinpath(*Path(item.path).parts)
            if path.is_file():
                path.chmod(0o755 if item.path in executable_paths else 0o644)
        layout = plan.native_library_layout
        native_root = plan.native_library_root
        if (layout is None) != (native_root is None):
            raise PackagingError("Conan native library authority is incomplete")
        package_type = "application"
        settings = ("os", "arch")
        package_info = ""
        if layout is not None:
            assert native_root is not None
            package_type = (
                "static-library" if layout.kind == "static" else "shared-library"
            )
            settings = ("os", "arch", "compiler", "build_type")
            # CppLibraryLayout defines public includes relative to its single
            # ``include/`` root. Advertising each header's parent would turn
            # ``sample/api.hpp`` into ``sample/sample/api.hpp`` for consumers.
            includedirs = ((PurePosixPath(native_root) / "include").as_posix(),)
            libdirs = tuple(
                sorted(
                    {
                        (
                            PurePosixPath(native_root) / PurePosixPath(path).parent
                        ).as_posix()
                        for path in layout.link_files
                    }
                )
            )
            bindirs = tuple(
                sorted(
                    {
                        (
                            PurePosixPath(native_root) / PurePosixPath(path).parent
                        ).as_posix()
                        for path in layout.runtime_files
                    }
                )
            )
            libraries = _conan_library_names(layout.link_files)
            package_info = (
                "\n    def package_info(self):\n"
                f"        self.cpp_info.includedirs = {list(includedirs)!r}\n"
                f"        self.cpp_info.libdirs = {list(libdirs)!r}\n"
                f"        self.cpp_info.bindirs = {list(bindirs)!r}\n"
                f"        self.cpp_info.libs = {list(libraries)!r}\n"
            )
        recipe = (
            "from conan import ConanFile\n"
            "from conan.tools.files import copy\n"
            "import os\n\n"
            "class LiterateAiPackage(ConanFile):\n"
            f"    name = {self.name!r}\n"
            f"    version = {self.version!r}\n"
            f"    package_type = {package_type!r}\n"
            f"    settings = {settings!r}\n"
            "    exports_sources = 'payload/*'\n\n"
            "    def package(self):\n"
            "        copy(self, '*', src=os.path.join(self.source_folder, 'payload'), "
            "dst=self.package_folder)\n" + package_info
        )
        (recipe_root / "conanfile.py").write_bytes(recipe.encode())
        profile = conan_home / "profiles" / "default"
        if not profile.exists():
            self._run(
                ("profile", "detect", "--force"),
                cwd=recipe_root,
                conan_home=conan_home,
            )
        export_record = staging / "export.json"
        self._run(
            (
                "export-pkg",
                ".",
                "--name",
                self.name,
                "--version",
                self.version,
                "--no-remote",
                "--test-folder=",
                "--format=json",
                f"--out-file={export_record}",
            ),
            cwd=recipe_root,
            conan_home=conan_home,
        )
        if not export_record.is_file():
            raise PackagingError("Conan did not emit its package creation record")
        try:
            record = json.loads(export_record.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PackagingError("Conan package creation record is invalid") from exc
        if self.name not in json.dumps(record, sort_keys=True):
            raise PackagingError("Conan package creation record names another package")
        archive = output_root / (
            f"{self.name}-{self.version}-{plan.target_identity.digest[:16]}.conan.tgz"
        )
        self._run(
            (
                "cache",
                "save",
                f"{self.name}/{self.version}:*",
                f"--file={archive}",
            ),
            cwd=recipe_root,
            conan_home=conan_home,
        )
        if archive.is_symlink() or not archive.is_file():
            raise PackagingError("Conan did not emit a portable package archive")
        content = archive.read_bytes()
        if not content:
            raise PackagingError("Conan emitted an empty package archive")
        blob = BlobRef(
            hashlib.sha256(content).hexdigest(),
            len(content),
            media_type="application/gzip",
        )
        self._created[blob.identity] = content
        artifact = PackagedFile(
            archive.name,
            "conan-cache-archive",
            PackageFileKind.PACKAGE_OUTPUT,
            plan.identity,
            plan.target_identity,
            blob,
            False,
        )
        files = logical_package_files(plan)
        result = package_result_for(plan, files=files, artifacts=(artifact,))

        def read_blob(reference: BlobRef) -> bytes:
            if reference == blob:
                return content
            item = next(
                (candidate for candidate in plan.inputs if candidate.blob == reference),
                None,
            )
            if item is None:
                raise PackagingError("Conan verification requested an unknown blob")
            return materialized_package_input_bytes(materialized[item.path])

        verify_package_result(plan, result, read_blob=read_blob)
        self.verify_bytes(result, content, object_root=output_root)
        return result

    @contextmanager
    def restore_verified(
        self, result: PackageResult, content: bytes, *, object_root: Path
    ) -> Iterator[Path]:
        """Yield one payload-verified package root in a newly isolated cache."""

        if result.packager_identity != self.packager_identity:
            raise PackagingError("Conan result differs from exact packager authority")
        if len(result.artifacts) != 1:
            raise PackagingError("Conan result must contain one native package")
        artifact = result.artifacts[0]
        if (
            not isinstance(content, bytes)
            or len(content) != artifact.blob.size
            or hashlib.sha256(content).hexdigest() != artifact.blob.digest
        ):
            raise PackagingError("Conan bytes differ from the package result")
        output_root = Path(object_root).resolve(strict=True)
        verification = Path(tempfile.mkdtemp(prefix="conan-verify-", dir=output_root))
        try:
            archive = verification / artifact.path
            archive.write_bytes(content)
            conan_home = verification / "conan-home"
            self._run(
                ("cache", "restore", str(archive)),
                cwd=verification,
                conan_home=conan_home,
            )
            listed = self._run(
                ("list", f"{self.name}/{self.version}#*:*#*", "--format=json"),
                cwd=verification,
                conan_home=conan_home,
            )
            try:
                catalog = json.loads(listed.stdout)
            except json.JSONDecodeError as exc:
                raise PackagingError("Conan verification catalog is invalid") from exc
            reference = _restored_reference(catalog, f"{self.name}/{self.version}")
            located = self._run(
                ("cache", "path", reference), cwd=verification, conan_home=conan_home
            )
            supplied = Path(located.stdout.strip())
            try:
                relative = supplied.relative_to(conan_home)
                current = conan_home
                for part in relative.parts:
                    current = current / part
                    if part in {".", ".."} or current.is_symlink():
                        raise ValueError("unsafe cache path")
                package_root = supplied.resolve(strict=True)
                package_root.relative_to(conan_home.resolve(strict=True))
                if not package_root.is_dir():
                    raise ValueError("package root is not a directory")
            except (OSError, ValueError) as exc:
                raise PackagingError(
                    "Conan package path escaped its isolated cache"
                ) from exc
            self._run(
                ("cache", "check-integrity", reference),
                cwd=verification,
                conan_home=conan_home,
            )
            _verify_restored_payload(result, package_root)
            yield package_root
        finally:
            shutil.rmtree(verification, ignore_errors=True)

    def verify_bytes(
        self, result: PackageResult, content: bytes, *, object_root: Path
    ) -> None:
        with self.restore_verified(result, content, object_root=object_root):
            pass

    def read_created_blob(self, reference: BlobRef) -> bytes:
        try:
            return self._created[reference.identity]
        except KeyError as exc:
            raise PackagingError("Conan blob was not created by this adapter") from exc


__all__ = ["ConanPackageAdapter", "ConanToolBinding"]
