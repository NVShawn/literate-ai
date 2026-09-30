"""Provenance DAG contracts for cross-project catalog composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._validation import object_value, optional_string, string_value

CATALOG_IMPORTS_SCHEMA = "literate-ai/catalog-imports@3"


@dataclass(frozen=True)
class TransitiveAncestor:
    """One node in the upstream provenance chain of an imported item."""

    SCHEMA = "literate-ai/catalog-import-ancestor@1"

    project_id: str
    project_identity: str  # "sha256:<hex>" URI
    ref: str  # "local:<path>" or "git:<url>@<commit>"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "project_id": self.project_id,
            "project_identity": self.project_identity,
            "ref": self.ref,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "TransitiveAncestor"
    ) -> TransitiveAncestor:
        data = object_value(value, path)
        return cls(
            project_id=string_value(data["project_id"], f"{path}.project_id"),
            project_identity=string_value(
                data["project_identity"], f"{path}.project_identity"
            ),
            ref=string_value(data["ref"], f"{path}.ref"),
        )


@dataclass(frozen=True)
class CatalogImportFile:
    """Content-addressed record of one file in a catalog import."""

    path: str
    identity: str  # "sha256:<hex>"

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "identity": self.identity}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CatalogImportFile"
    ) -> CatalogImportFile:
        data = object_value(value, path)
        return cls(
            path=string_value(data["path"], f"{path}.path"),
            identity=string_value(data["identity"], f"{path}.identity"),
        )


@dataclass(frozen=True)
class CatalogImportSource:
    """Identifies the project an item was copied from, with its full ancestor chain."""

    SCHEMA = "literate-ai/catalog-import-source@1"

    project_id: str
    project_identity: str  # content hash of source literate.project.json
    ref: str  # "local:<abs-path>" or "git:<url>@<commit>"
    transitive_ancestors: tuple[TransitiveAncestor, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "project_id": self.project_id,
            "project_identity": self.project_identity,
            "ref": self.ref,
            "transitive_ancestors": [a.to_dict() for a in self.transitive_ancestors],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CatalogImportSource"
    ) -> CatalogImportSource:
        data = object_value(value, path)
        ancestors_raw = data.get("transitive_ancestors", [])
        if not isinstance(ancestors_raw, list):
            ancestors_raw = []
        return cls(
            project_id=string_value(data["project_id"], f"{path}.project_id"),
            project_identity=string_value(
                data["project_identity"], f"{path}.project_identity"
            ),
            ref=string_value(data["ref"], f"{path}.ref"),
            transitive_ancestors=tuple(
                TransitiveAncestor.from_dict(
                    a, path=f"{path}.transitive_ancestors[{i}]"
                )
                for i, a in enumerate(ancestors_raw)
            ),
        )

    def all_project_ids(self) -> frozenset[str]:
        """Return this source's project_id plus all transitive ancestor ids."""
        return frozenset(
            {self.project_id} | {a.project_id for a in self.transitive_ancestors}
        )


@dataclass(frozen=True)
class CatalogImport:
    """One imported catalog item with its provenance."""

    SCHEMA = "literate-ai/catalog-import@2"

    kind: str  # "flavor", "skill", "component", "workflow", or "routing"
    # Bare name, optionally beneath a catalog directory for skills.
    name: str
    source: CatalogImportSource
    files: tuple[CatalogImportFile, ...]
    copied_at: str  # ISO-8601

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "name": self.name,
            "source": self.source.to_dict(),
            "files": [f.to_dict() for f in self.files],
            "copied_at": self.copied_at,
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "CatalogImport") -> CatalogImport:
        data = object_value(value, path)
        files_raw = data.get("files", [])
        return cls(
            kind=string_value(data["kind"], f"{path}.kind"),
            name=string_value(data["name"], f"{path}.name"),
            source=CatalogImportSource.from_dict(data["source"], path=f"{path}.source"),
            files=tuple(
                CatalogImportFile.from_dict(f, path=f"{path}.files[{i}]")
                for i, f in enumerate(files_raw if isinstance(files_raw, list) else [])
            ),
            copied_at=string_value(data.get("copied_at", ""), f"{path}.copied_at"),
        )


@dataclass(frozen=True)
class CatalogInheritanceDecision:
    """One effective, shadowed, or explicitly withheld upstream candidate."""

    SCHEMA = "literate-ai/catalog-inheritance-decision@1"
    DISPOSITIONS = frozenset({"effective", "shadowed", "withheld"})

    kind: str
    name: str
    source: CatalogImportSource
    files: tuple[CatalogImportFile, ...]
    disposition: str
    selected_source_project_id: str | None = None

    def __post_init__(self) -> None:
        if self.disposition not in self.DISPOSITIONS:
            raise ValueError(
                f"unknown catalog inheritance disposition: {self.disposition}"
            )
        if self.disposition == "shadowed" and self.selected_source_project_id is None:
            raise ValueError(
                "shadowed catalog candidates require their selected source"
            )
        if (
            self.disposition != "shadowed"
            and self.selected_source_project_id is not None
        ):
            raise ValueError("only shadowed catalog candidates name a selected source")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "name": self.name,
            "source": self.source.to_dict(),
            "files": [item.to_dict() for item in self.files],
            "disposition": self.disposition,
        }
        if self.selected_source_project_id is not None:
            value["selected_source_project_id"] = self.selected_source_project_id
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CatalogInheritanceDecision"
    ) -> CatalogInheritanceDecision:
        data = object_value(value, path)
        files_raw = data.get("files", [])
        return cls(
            kind=string_value(data["kind"], f"{path}.kind"),
            name=string_value(data["name"], f"{path}.name"),
            source=CatalogImportSource.from_dict(data["source"], path=f"{path}.source"),
            files=tuple(
                CatalogImportFile.from_dict(item, path=f"{path}.files[{index}]")
                for index, item in enumerate(
                    files_raw if isinstance(files_raw, list) else []
                )
            ),
            disposition=string_value(data["disposition"], f"{path}.disposition"),
            selected_source_project_id=optional_string(
                data.get("selected_source_project_id"),
                f"{path}.selected_source_project_id",
            ),
        )


@dataclass(frozen=True)
class CatalogImportsFile:
    """The full .literate/imports.json provenance file."""

    SCHEMA = CATALOG_IMPORTS_SCHEMA
    PATH = ".literate/imports.json"

    imports: tuple[CatalogImport, ...]
    decisions: tuple[CatalogInheritanceDecision, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "imports": [i.to_dict() for i in self.imports],
            "decisions": [item.to_dict() for item in self.decisions],
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "CatalogImportsFile"
    ) -> CatalogImportsFile:
        data = object_value(value, path)
        imports_raw = data.get("imports", [])
        decisions_raw = data.get("decisions", [])
        return cls(
            imports=tuple(
                CatalogImport.from_dict(imp, path=f"{path}.imports[{i}]")
                for i, imp in enumerate(
                    imports_raw if isinstance(imports_raw, list) else []
                )
            ),
            decisions=tuple(
                CatalogInheritanceDecision.from_dict(
                    item, path=f"{path}.decisions[{index}]"
                )
                for index, item in enumerate(
                    decisions_raw if isinstance(decisions_raw, list) else []
                )
            ),
        )

    @classmethod
    def load(cls, project_root: Path) -> CatalogImportsFile:
        """Read imports.json, returning empty if absent."""
        path = project_root / cls.PATH
        if not path.exists():
            return cls(imports=())
        import json

        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, project_root: Path) -> None:
        import json

        path = project_root / self.PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def all_source_project_ids(self) -> frozenset[str]:
        """All project IDs this project has imported from (direct + transitive)."""
        ids: set[str] = set()
        for imp in self.imports:
            ids |= imp.source.all_project_ids()
        return frozenset(ids)

    def with_import(self, new_import: CatalogImport) -> CatalogImportsFile:
        """Replace the matching catalog item with ``new_import``."""
        kept = tuple(
            i
            for i in self.imports
            if not (i.kind == new_import.kind and i.name == new_import.name)
        )
        return CatalogImportsFile(
            imports=kept + (new_import,), decisions=self.decisions
        )


__all__ = [
    "CATALOG_IMPORTS_SCHEMA",
    "CatalogImport",
    "CatalogImportFile",
    "CatalogInheritanceDecision",
    "CatalogImportSource",
    "CatalogImportsFile",
    "TransitiveAncestor",
]
