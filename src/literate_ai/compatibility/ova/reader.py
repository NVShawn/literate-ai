"""Retained, read-only compatibility readers for legacy OVA artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .models import (
    CompatibilityDiagnostic,
    DiagnosticSeverity,
    LifecycleState,
    OvaCompatibilityResult,
    freeze,
)
from .yaml_subset import YamlSubsetError, load_yaml_subset


class _Diagnostics:
    def __init__(self, source_path: str) -> None:
        self.source_path = source_path
        self.items: list[CompatibilityDiagnostic] = []
        self.drifted = False
        self.interrupted = False

    def add(
        self,
        code: str,
        message: str,
        value_path: str = "$",
        *,
        severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
        drifted: bool = False,
        interrupted: bool = False,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.items.append(
            CompatibilityDiagnostic(
                code=code,
                message=message,
                source_path=self.source_path,
                value_path=value_path,
                severity=severity,
                line=line,
                column=column,
            )
        )
        self.drifted = self.drifted or drifted
        self.interrupted = self.interrupted or interrupted

    def state(self) -> LifecycleState:
        if any(item.severity is DiagnosticSeverity.ERROR for item in self.items):
            return LifecycleState.INVALID
        if self.drifted:
            return LifecycleState.DRIFTED
        if self.interrupted:
            return LifecycleState.INTERRUPTED
        return LifecycleState.VALID


class OvaCompatibilityReader:
    """Read supported OVA data without importing OVA or changing source bytes."""

    def read_component(self, path: str | Path) -> OvaCompatibilityResult:
        return self._read_mapping(
            path, "component", self._validate_component, yaml=True
        )

    def read_settings(self, path: str | Path) -> OvaCompatibilityResult:
        """Read the persisted model/group catalog or a runtime settings snapshot."""

        value, diagnostics, digest = self._load(path, yaml=Path(path).suffix != ".json")
        if value is None and diagnostics.items:
            return self._result("settings", diagnostics, digest, value)
        if not isinstance(value, Mapping):
            return self._result("settings", diagnostics, digest, value)
        if "endpoints" in value or "groups" in value:
            kind = "settings:model-portfolio"
            self._validate_model_settings(value, diagnostics)
        else:
            kind = "settings:runtime"
            self._validate_runtime_settings(value, diagnostics)
        return self._result(kind, diagnostics, digest, value)

    def read_cache(self, path: str | Path) -> OvaCompatibilityResult:
        return self._read_mapping(path, "cache", self._validate_cache)

    def read_source_package(self, path: str | Path) -> OvaCompatibilityResult:
        return self._read_mapping(
            path,
            "package:source",
            lambda value, diagnostics: self._validate_source_package(
                value, diagnostics, "$"
            ),
        )

    def read_object_package(self, path: str | Path) -> OvaCompatibilityResult:
        return self._read_mapping(
            path,
            "package:object",
            lambda value, diagnostics: self._validate_object_package(
                value, diagnostics, "$"
            ),
        )

    def read_provenance(self, path: str | Path) -> OvaCompatibilityResult:
        return self._read_mapping(
            path, "provenance:generation", self._validate_provenance
        )

    def read_publication_settings(self, path: str | Path) -> OvaCompatibilityResult:
        return self._read_mapping(
            path,
            "publication:settings",
            self._validate_publication_settings,
        )

    def read_publications(self, path: str | Path) -> OvaCompatibilityResult:
        value, diagnostics, digest = self._load(path, yaml=False)
        if value is None and diagnostics.items:
            return self._result("publication:records", diagnostics, digest, value)
        if not self._sequence(value, diagnostics, "$", nonempty=False):
            return self._result("publication:records", diagnostics, digest, value)
        for index, item in enumerate(value):
            pointer = f"$[{index}]"
            if self._mapping(item, diagnostics, pointer):
                self._validate_publication(item, diagnostics, pointer)
        return self._result("publication:records", diagnostics, digest, value)

    def _read_mapping(
        self,
        path: str | Path,
        kind: str,
        validator: Callable[[Mapping[str, Any], _Diagnostics], None],
        *,
        yaml: bool = False,
    ) -> OvaCompatibilityResult:
        value, diagnostics, digest = self._load(path, yaml=yaml)
        if value is None and diagnostics.items:
            return self._result(kind, diagnostics, digest, value)
        if self._mapping(value, diagnostics, "$"):
            validator(value, diagnostics)
        return self._result(kind, diagnostics, digest, value)

    @staticmethod
    def _result(
        kind: str,
        diagnostics: _Diagnostics,
        digest: str | None,
        value: Any,
    ) -> OvaCompatibilityResult:
        if isinstance(value, Mapping):
            frozen = freeze(value)
            assert isinstance(frozen, Mapping)
            records = (frozen,)
            shape = "mapping"
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            records_list = []
            for item in value:
                if isinstance(item, Mapping):
                    frozen = freeze(item)
                    assert isinstance(frozen, Mapping)
                    records_list.append(frozen)
            records = tuple(records_list)
            shape = "sequence"
        else:
            records = ()
            shape = "unreadable"
        return OvaCompatibilityResult(
            kind=kind,
            source_path=diagnostics.source_path,
            source_digest=digest,
            root_shape=shape,
            records=records,
            diagnostics=tuple(diagnostics.items),
            state=diagnostics.state(),
        )

    @staticmethod
    def _load(
        path: str | Path,
        *,
        yaml: bool,
    ) -> tuple[Any, _Diagnostics, str | None]:
        configured = Path(path).expanduser()
        source_path = (
            str(configured.resolve()) if configured.exists() else str(configured)
        )
        diagnostics = _Diagnostics(source_path)
        if configured.is_symlink():
            diagnostics.add(
                "ova.source.symlink", "OVA artifact must not be a symbolic link"
            )
            return None, diagnostics, None
        try:
            content = configured.read_bytes()
        except OSError as exc:
            diagnostics.add(
                "ova.source.unreadable", f"OVA artifact cannot be read: {exc}"
            )
            return None, diagnostics, None
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            diagnostics.add(
                "ova.source.not_utf8",
                "OVA artifact must be UTF-8",
                line=exc.start + 1,
            )
            return None, diagnostics, digest
        try:
            value = load_yaml_subset(text) if yaml else json.loads(text)
        except YamlSubsetError as exc:
            diagnostics.add(
                "ova.yaml.invalid",
                str(exc),
                line=exc.line,
                column=exc.column,
            )
            return None, diagnostics, digest
        except json.JSONDecodeError as exc:
            interrupted = not text.rstrip().endswith(("}", "]"))
            diagnostics.add(
                "ova.json.interrupted" if interrupted else "ova.json.invalid",
                "OVA JSON is incomplete" if interrupted else "OVA JSON is invalid",
                interrupted=interrupted,
                severity=(
                    DiagnosticSeverity.WARNING
                    if interrupted
                    else DiagnosticSeverity.ERROR
                ),
                line=exc.lineno,
                column=exc.colno,
            )
            return None, diagnostics, digest
        return value, diagnostics, digest

    def _validate_component(
        self, value: Mapping[str, Any], diagnostics: _Diagnostics
    ) -> None:
        self._schema(value, diagnostics, (2, 3))
        self._unknown(
            value,
            {
                "schema_version",
                "component",
                "openspec",
                "spec_files",
                "skills",
                "model_selector",
                "requirements",
                "flavor_slots",
                "sample",
                "acceptance",
                "entrypoints",
            },
            diagnostics,
            "$",
        )
        component = value.get("component")
        if self._mapping(component, diagnostics, "$.component"):
            self._required(component, {"id", "name"}, diagnostics, "$.component")
            self._unknown(
                component,
                {"id", "name", "kind", "version", "summary", "provides"},
                diagnostics,
                "$.component",
            )
            self._text(component.get("id"), diagnostics, "$.component.id")
            self._text(component.get("name"), diagnostics, "$.component.name")
            if component.get("kind", "application") not in {"application", "component"}:
                diagnostics.add(
                    "ova.component.kind",
                    "unsupported component kind",
                    "$.component.kind",
                )
        openspec = value.get("openspec")
        if self._mapping(openspec, diagnostics, "$.openspec"):
            self._required(openspec, {"capabilities"}, diagnostics, "$.openspec")
            self._unknown(
                openspec,
                {"schema_name", "root", "capabilities", "active_change"},
                diagnostics,
                "$.openspec",
            )
            self._sequence(
                openspec.get("capabilities"), diagnostics, "$.openspec.capabilities"
            )
            root = openspec.get("root", "openspec")
            self._path(root, diagnostics, "$.openspec.root")
        for field in ("spec_files", "skills", "requirements"):
            self._sequence(value.get(field), diagnostics, f"$.{field}")
        for index, path in enumerate(value.get("spec_files", [])):
            self._path(path, diagnostics, f"$.spec_files[{index}]")
        for index, skill in enumerate(value.get("skills", [])):
            pointer = f"$.skills[{index}]"
            if self._mapping(skill, diagnostics, pointer):
                self._required(
                    skill,
                    {"skill_id", "source", "content_digest"}
                    | ({"version"} if value.get("schema_version") == 3 else set()),
                    diagnostics,
                    pointer,
                )
                self._unknown(
                    skill,
                    {"skill_id", "source", "content_digest"}
                    | ({"version"} if value.get("schema_version") == 3 else set()),
                    diagnostics,
                    pointer,
                )
                self._path(skill.get("source"), diagnostics, f"{pointer}.source")
        selector = value.get("model_selector")
        if self._mapping(selector, diagnostics, "$.model_selector"):
            self._required(selector, {"stages"}, diagnostics, "$.model_selector")
            self._unknown(
                selector,
                {"strategy", "stages", "allow_fallback"}
                | ({"version"} if value.get("schema_version") == 3 else set()),
                diagnostics,
                "$.model_selector",
            )
            stages = selector.get("stages")
            if self._sequence(stages, diagnostics, "$.model_selector.stages"):
                declared = {
                    item.get("stage") for item in stages if isinstance(item, Mapping)
                }
                missing = {
                    "api_contracts",
                    "file_plan",
                    "generate",
                    "repair",
                } - declared
                if missing:
                    diagnostics.add(
                        "ova.component.model_stages_missing",
                        f"missing required model stages: {', '.join(sorted(missing))}",
                        "$.model_selector.stages",
                    )
        entrypoints = value.get("entrypoints", {})
        if self._mapping(entrypoints, diagnostics, "$.entrypoints"):
            for name, path in entrypoints.items():
                self._path(path, diagnostics, f"$.entrypoints.{name}")

    def _validate_model_settings(
        self, value: Mapping[str, Any], diagnostics: _Diagnostics
    ) -> None:
        self._schema(value, diagnostics, 1)
        self._required(value, {"endpoints", "groups"}, diagnostics, "$")
        self._unknown(
            value, {"schema_version", "endpoints", "groups"}, diagnostics, "$"
        )
        endpoints = value.get("endpoints")
        groups = value.get("groups")
        endpoint_ids: set[str] = set()
        if self._mapping(endpoints, diagnostics, "$.endpoints"):
            endpoint_ids = set(endpoints)
            for key, endpoint in endpoints.items():
                pointer = f"$.endpoints.{key}"
                if not self._mapping(endpoint, diagnostics, pointer):
                    continue
                self._required(
                    endpoint,
                    {
                        "endpoint_id",
                        "provider",
                        "model_id",
                        "base_url",
                        "capabilities",
                        "context_tokens",
                    },
                    diagnostics,
                    pointer,
                )
                self._unknown(
                    endpoint,
                    {
                        "endpoint_id",
                        "provider",
                        "model_id",
                        "base_url",
                        "credential_env",
                        "capabilities",
                        "context_tokens",
                        "cost_class",
                        "locality",
                        "enabled",
                    },
                    diagnostics,
                    pointer,
                )
                if endpoint.get("endpoint_id") != key:
                    self._drift(
                        diagnostics,
                        f"{pointer}.endpoint_id",
                        "endpoint key does not match endpoint_id",
                    )
        if self._mapping(groups, diagnostics, "$.groups"):
            for key, group in groups.items():
                pointer = f"$.groups.{key}"
                if not self._mapping(group, diagnostics, pointer):
                    continue
                self._required(
                    group, {"group_id", "endpoint_ids"}, diagnostics, pointer
                )
                self._unknown(
                    group,
                    {"group_id", "version", "description", "endpoint_ids", "routing"},
                    diagnostics,
                    pointer,
                )
                if group.get("group_id") != key:
                    self._drift(
                        diagnostics,
                        f"{pointer}.group_id",
                        "group key does not match group_id",
                    )
                ids = group.get("endpoint_ids")
                if self._sequence(ids, diagnostics, f"{pointer}.endpoint_ids"):
                    missing = {
                        item for item in ids if isinstance(item, str)
                    } - endpoint_ids
                    if missing:
                        self._drift(
                            diagnostics,
                            f"{pointer}.endpoint_ids",
                            "model group refers to unknown endpoints: "
                            + ", ".join(sorted(missing)),
                        )

    def _validate_runtime_settings(
        self, value: Mapping[str, Any], diagnostics: _Diagnostics
    ) -> None:
        value = {
            key: item
            for key, item in value.items()
            if key not in {"codegraph_binary", "codegraph_timeout_seconds"}
        }
        known = {
            "host",
            "port",
            "ova_home",
            "ova_env_file",
            "runtime_mode",
            "sidecar_host",
            "sidecar_port",
            "sidecar_timeout_seconds",
            "native_operation_timeout_seconds",
            "webrtc_signal_port",
            "webrtc_public_host",
            "stream_fps",
            "cuda_device",
            "start_sidecar",
            "catalog_path",
            "project_id",
            "workspace_id",
            "model_catalog",
            "runtime_id",
            "session_id",
            "nvidia_api_base_url",
            "nvidia_model",
            "nvidia_models",
            "nvidia_timeout_seconds",
        }
        self._unknown(value, known, diagnostics, "$")
        for field in (
            "host",
            "ova_home",
            "runtime_mode",
            "project_id",
            "workspace_id",
        ):
            if field in value:
                self._text(value[field], diagnostics, f"$.{field}")
        if "nvidia_api_key" in value:
            diagnostics.add(
                "ova.settings.persisted_secret",
                "runtime credentials must remain process-only",
                "$.nvidia_api_key",
            )

    def _validate_cache(
        self, value: Mapping[str, Any], diagnostics: _Diagnostics
    ) -> None:
        self._schema(value, diagnostics, 1)
        self._required(
            value,
            {"component_id", "source_packages", "object_packages"},
            diagnostics,
            "$",
        )
        self._unknown(
            value,
            {
                "schema_version",
                "component_id",
                "source_packages",
                "object_packages",
                "latest_source_package_id",
                "latest_object_package_id",
            },
            diagnostics,
            "$",
        )
        component_id = value.get("component_id")
        sources = value.get("source_packages")
        objects = value.get("object_packages")
        if self._mapping(sources, diagnostics, "$.source_packages"):
            for package_id, package in sources.items():
                pointer = f"$.source_packages.{package_id}"
                if self._mapping(package, diagnostics, pointer):
                    self._validate_source_package(package, diagnostics, pointer)
                    if (
                        package.get("package_id") != package_id
                        or package.get("component_id") != component_id
                    ):
                        self._drift(
                            diagnostics,
                            pointer,
                            "source package key or component ownership drifted",
                        )
        if self._mapping(objects, diagnostics, "$.object_packages"):
            for package_id, package in objects.items():
                pointer = f"$.object_packages.{package_id}"
                if self._mapping(package, diagnostics, pointer):
                    self._validate_object_package(package, diagnostics, pointer)
                    if (
                        package.get("package_id") != package_id
                        or package.get("component_id") != component_id
                    ):
                        self._drift(
                            diagnostics,
                            pointer,
                            "object package key or component ownership drifted",
                        )
                    if (
                        isinstance(sources, Mapping)
                        and package.get("source_package_id") not in sources
                    ):
                        self._drift(
                            diagnostics,
                            f"{pointer}.source_package_id",
                            "object package source is absent",
                        )
        latest_source = value.get("latest_source_package_id")
        latest_object = value.get("latest_object_package_id")
        if (
            latest_source is not None
            and isinstance(sources, Mapping)
            and latest_source not in sources
        ):
            self._drift(
                diagnostics,
                "$.latest_source_package_id",
                "latest source package is absent",
            )
        if (
            latest_object is not None
            and isinstance(objects, Mapping)
            and latest_object not in objects
        ):
            self._drift(
                diagnostics,
                "$.latest_object_package_id",
                "latest object package is absent",
            )

    def _validate_source_package(
        self, value: Mapping[str, Any], diagnostics: _Diagnostics, pointer: str
    ) -> None:
        required = {
            "package_id",
            "component_id",
            "source_identity",
            "files",
            "dependencies",
            "spec_files",
            "skill_ids",
            "model_selector_digest",
            "content_digest",
            "created_at",
        }
        self._required(value, required, diagnostics, pointer)
        self._unknown(value, required, diagnostics, pointer)
        for field in ("files", "spec_files"):
            items = value.get(field)
            if self._sequence(items, diagnostics, f"{pointer}.{field}"):
                for index, path in enumerate(items):
                    self._path(path, diagnostics, f"{pointer}.{field}[{index}]")

    def _validate_object_package(
        self, value: Mapping[str, Any], diagnostics: _Diagnostics, pointer: str
    ) -> None:
        required = {
            "package_id",
            "component_id",
            "source_package_id",
            "artifacts",
            "dependency_packages",
            "dependency_components",
            "depending_components",
            "content_digest",
            "built_at",
        }
        self._required(value, required, diagnostics, pointer)
        self._unknown(value, required, diagnostics, pointer)
        artifacts = value.get("artifacts")
        if self._sequence(artifacts, diagnostics, f"{pointer}.artifacts"):
            for index, artifact in enumerate(artifacts):
                artifact_pointer = f"{pointer}.artifacts[{index}]"
                if self._mapping(artifact, diagnostics, artifact_pointer):
                    self._required(
                        artifact,
                        {"path", "kind", "content_digest"},
                        diagnostics,
                        artifact_pointer,
                    )
                    self._unknown(
                        artifact,
                        {"path", "kind", "content_digest"},
                        diagnostics,
                        artifact_pointer,
                    )
                    self._path(
                        artifact.get("path"), diagnostics, f"{artifact_pointer}.path"
                    )
        packages = value.get("dependency_packages")
        components = value.get("dependency_components")
        if (
            isinstance(packages, Sequence)
            and not isinstance(packages, str)
            and isinstance(components, Mapping)
        ):
            if set(item for item in packages if isinstance(item, str)) != set(
                components.values()
            ):
                self._drift(
                    diagnostics, pointer, "object dependency package views disagree"
                )

    def _validate_provenance(
        self, value: Mapping[str, Any], diagnostics: _Diagnostics
    ) -> None:
        self._schema(value, diagnostics, 1)
        required = {
            "schema_version",
            "transaction_id",
            "parent_transaction_id",
            "model_selections",
            "prompt_template_digest",
            "dependency_lock_digest",
            "vocabulary_digest",
            "specification_digest",
            "spec_artifacts",
            "queries",
            "evidence",
            "files",
            "validation",
            "repair_attempts",
            "accepted_at",
        }
        self._required(value, required, diagnostics, "$")
        self._unknown(value, required, diagnostics, "$")
        self._sequence(value.get("model_selections"), diagnostics, "$.model_selections")
        artifacts = value.get("spec_artifacts")
        if self._sequence(artifacts, diagnostics, "$.spec_artifacts"):
            for index, artifact in enumerate(artifacts):
                pointer = f"$.spec_artifacts[{index}]"
                if self._mapping(artifact, diagnostics, pointer):
                    self._path(artifact.get("path"), diagnostics, f"{pointer}.path")
        files = value.get("files")
        if self._sequence(files, diagnostics, "$.files", nonempty=False):
            for index, item in enumerate(files):
                pointer = f"$.files[{index}]"
                if self._mapping(item, diagnostics, pointer):
                    self._path(item.get("path"), diagnostics, f"{pointer}.path")
        for field in ("queries", "evidence", "validation"):
            self._sequence(value.get(field), diagnostics, f"$.{field}", nonempty=False)

    def _validate_publication_settings(
        self, value: Mapping[str, Any], diagnostics: _Diagnostics
    ) -> None:
        self._schema(value, diagnostics, 1)
        self._required(value, {"auto_publish", "targets"}, diagnostics, "$")
        self._unknown(
            value, {"schema_version", "auto_publish", "targets"}, diagnostics, "$"
        )
        targets = value.get("targets")
        if self._mapping(targets, diagnostics, "$.targets"):
            for target_id, target in targets.items():
                pointer = f"$.targets.{target_id}"
                if not self._mapping(target, diagnostics, pointer):
                    continue
                self._required(
                    target,
                    {"target_id", "transport", "location", "enabled"},
                    diagnostics,
                    pointer,
                )
                self._unknown(
                    target,
                    {"target_id", "transport", "location", "enabled"},
                    diagnostics,
                    pointer,
                )
                if target.get("target_id") != target_id:
                    self._drift(
                        diagnostics,
                        f"{pointer}.target_id",
                        "publication target key drifted",
                    )

    def _validate_publication(
        self, value: Mapping[str, Any], diagnostics: _Diagnostics, pointer: str
    ) -> None:
        fields = {
            "publication_id",
            "component_id",
            "source_package_id",
            "object_package_id",
            "target_id",
            "state",
            "published_digest",
            "error",
            "created_at",
            "updated_at",
        }
        self._required(value, fields, diagnostics, pointer)
        self._unknown(value, fields, diagnostics, pointer)
        state = value.get("state")
        allowed = {"configured", "queued", "publishing", "published", "failed"}
        if state not in allowed:
            diagnostics.add(
                "ova.publication.state",
                "unsupported publication state",
                f"{pointer}.state",
            )
        elif state in {"queued", "publishing"}:
            diagnostics.add(
                "ova.publication.interrupted",
                f"publication remains in transient state {state!r}",
                f"{pointer}.state",
                severity=DiagnosticSeverity.WARNING,
                interrupted=True,
            )
        elif state == "published" and not value.get("published_digest"):
            self._drift(
                diagnostics,
                f"{pointer}.published_digest",
                "published record has no digest",
            )
        elif state == "failed" and not value.get("error"):
            self._drift(
                diagnostics, f"{pointer}.error", "failed publication has no error"
            )

    @staticmethod
    def _mapping(value: Any, diagnostics: _Diagnostics, pointer: str) -> bool:
        if not isinstance(value, Mapping):
            diagnostics.add(
                "ova.schema.mapping_required", "expected a mapping", pointer
            )
            return False
        return True

    @staticmethod
    def _sequence(
        value: Any,
        diagnostics: _Diagnostics,
        pointer: str,
        *,
        nonempty: bool = True,
    ) -> bool:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            diagnostics.add(
                "ova.schema.sequence_required", "expected a sequence", pointer
            )
            return False
        if nonempty and not value:
            diagnostics.add(
                "ova.schema.sequence_empty", "sequence must not be empty", pointer
            )
            return False
        return True

    @staticmethod
    def _text(value: Any, diagnostics: _Diagnostics, pointer: str) -> bool:
        if not isinstance(value, str) or not value.strip():
            diagnostics.add(
                "ova.schema.text_required", "expected non-empty text", pointer
            )
            return False
        return True

    @classmethod
    def _path(cls, value: Any, diagnostics: _Diagnostics, pointer: str) -> bool:
        if not cls._text(value, diagnostics, pointer):
            return False
        assert isinstance(value, str)
        path = PurePosixPath(value.strip())
        if (
            "\\" in value
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            diagnostics.add(
                "ova.schema.path_invalid",
                "expected a normalized project-relative POSIX path",
                pointer,
            )
            return False
        return True

    @staticmethod
    def _required(
        value: Mapping[str, Any],
        fields: set[str],
        diagnostics: _Diagnostics,
        pointer: str,
    ) -> None:
        for field in sorted(fields - value.keys()):
            diagnostics.add(
                "ova.schema.field_missing",
                f"required field {field!r} is absent",
                f"{pointer}.{field}",
            )

    @staticmethod
    def _unknown(
        value: Mapping[str, Any],
        fields: set[str],
        diagnostics: _Diagnostics,
        pointer: str,
    ) -> None:
        for field in value.keys() - fields:
            diagnostics.add(
                "ova.compatibility.unknown_field",
                f"field {field!r} is not known to the Phase-1 reader and was preserved",
                f"{pointer}.{field}",
                severity=DiagnosticSeverity.INFO,
            )

    @staticmethod
    def _schema(
        value: Mapping[str, Any],
        diagnostics: _Diagnostics,
        expected: int | tuple[int, ...],
    ) -> None:
        accepted = (expected,) if isinstance(expected, int) else expected
        if value.get("schema_version") not in accepted:
            display = " or ".join(str(item) for item in accepted)
            diagnostics.add(
                "ova.schema.version",
                f"expected schema_version {display}",
                "$.schema_version",
            )

    @staticmethod
    def _drift(diagnostics: _Diagnostics, pointer: str, message: str) -> None:
        diagnostics.add(
            "ova.lifecycle.drift",
            message,
            pointer,
            severity=DiagnosticSeverity.WARNING,
            drifted=True,
        )


__all__ = ["OvaCompatibilityReader"]
