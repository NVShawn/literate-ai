"""Strict human-authored Flavor Markdown and normalized runtime projection."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from literate_ai.contracts import (
    Capability,
    CapabilityRequirement,
    ContentIdentity,
    ContentReference,
    FlavorCoordinate,
    FlavorCoRequisiteGroup,
    FlavorDefinition,
    HashAlgorithm,
    ProviderOverrideDeclaration,
    TargetConstraint,
)
from literate_ai.contracts._validation import fields, string_tuple, string_value
from literate_ai.contracts.authoring_markdown import parse_authoring_markdown
from literate_ai.contracts.flavors import ContributionReference

FLAVOR_MARKDOWN_SCHEMA = "literate-ai/flavor-markdown@1"


class FlavorMarkdownError(ValueError):
    """Flavor authoring is malformed or cannot be resolved exactly."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class FlavorContentSelector:
    kind: str
    uri: str


@dataclass(frozen=True, slots=True)
class AuthoredFlavorContribution:
    contribution_id: str
    kind: str
    merge_operator: str
    slot: str
    content: FlavorContentSelector


@dataclass(frozen=True, slots=True)
class FlavorMarkdownAuthoring:
    namespace: str
    name: str
    version: str
    display_name: str
    primary_axis: str
    target: str
    secondary_constraints: tuple[dict[str, Any], ...]
    applicable_capabilities: tuple[str, ...]
    provides: tuple[dict[str, Any], ...]
    requires: tuple[dict[str, Any], ...]
    specification_roots: tuple[str, ...]
    authoring_inputs: tuple[FlavorContentSelector, ...]
    contributions: tuple[AuthoredFlavorContribution, ...]
    conflicts: tuple[str, ...]
    co_requisites: tuple[str, ...]
    co_requisite_groups: tuple[dict[str, Any], ...]
    order_before: tuple[str, ...]
    order_after: tuple[str, ...]
    description: str
    provider_overrides: tuple[dict[str, Any], ...] = ()

    def resolve(self, read_reference: Callable[[str], bytes]) -> FlavorDefinition:
        """Resolve authored paths into the exact v2 machine contract."""

        def reference(kind: str, uri: str) -> ContentReference:
            try:
                content = read_reference(uri)
            except (OSError, ValueError) as exc:
                raise FlavorMarkdownError(
                    "flavor_markdown.reference_unavailable",
                    f"Flavor reference is unavailable: {uri}",
                ) from exc
            return ContentReference(
                kind,
                uri,
                ContentIdentity(
                    HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()
                ),
            )

        provides = tuple(
            Capability.from_dict(
                {
                    "schema": Capability.SCHEMA,
                    "name": item["name"],
                    "version": item["version"],
                    "contract": item["contract"],
                },
                path=f"flavor.md.provides[{index}]",
            )
            for index, item in enumerate(self.provides)
        )
        requirements = tuple(
            CapabilityRequirement.from_dict(
                {"schema": CapabilityRequirement.SCHEMA, **item},
                path=f"flavor.md.requires[{index}]",
            )
            for index, item in enumerate(self.requires)
        )
        return FlavorDefinition(
            coordinate=FlavorCoordinate(self.namespace, self.name),
            version=self.version,
            display_name=self.display_name,
            primary_axis=self._primary_axis(),
            secondary_constraints=tuple(
                TargetConstraint.from_dict(
                    item, path=f"flavor.md.secondary_constraints[{index}]"
                )
                for index, item in enumerate(self.secondary_constraints)
            ),
            applicable_capabilities=self.applicable_capabilities,
            provides=provides,
            requires=requirements,
            specification_fragments=tuple(
                reference("specification", uri) for uri in self.specification_roots
            ),
            authoring_inputs=tuple(
                reference(item.kind, item.uri) for item in self.authoring_inputs
            ),
            contributions=tuple(
                ContributionReference(
                    item.contribution_id,
                    self._contribution_kind(item.kind),
                    self._merge_operator(item.merge_operator),
                    item.slot,
                    reference(item.content.kind, item.content.uri),
                )
                for item in self.contributions
            ),
            conflicts=self.conflicts,
            co_requisites=self.co_requisites,
            order_before=self.order_before,
            order_after=self.order_after,
            supported_targets=(self.target,),
            co_requisite_groups=tuple(
                FlavorCoRequisiteGroup.from_dict(
                    item, path=f"flavor.md.co_requisite_groups[{index}]"
                )
                for index, item in enumerate(self.co_requisite_groups)
            ),
            provider_overrides=tuple(
                ProviderOverrideDeclaration.from_dict(
                    {
                        "schema": ProviderOverrideDeclaration.SCHEMA,
                        **item,
                    },
                    path=f"flavor.md.provider_overrides[{index}]",
                )
                for index, item in enumerate(self.provider_overrides)
            ),
        )

    @staticmethod
    def _enum(enum_type: type[Any], value: str, label: str) -> Any:
        try:
            return enum_type(value)
        except ValueError as exc:
            raise FlavorMarkdownError(
                "flavor_markdown.value_invalid", f"invalid {label}: {value!r}"
            ) from exc

    def _primary_axis(self) -> Any:
        from literate_ai.contracts import FlavorAxis

        return self._enum(FlavorAxis, self.primary_axis, "primary_axis")

    @staticmethod
    def _contribution_kind(value: str) -> Any:
        from literate_ai.contracts import ContributionKind

        return FlavorMarkdownAuthoring._enum(
            ContributionKind, value, "contribution kind"
        )

    @staticmethod
    def _merge_operator(value: str) -> Any:
        from literate_ai.contracts import MergeOperator

        return FlavorMarkdownAuthoring._enum(
            MergeOperator, value, "contribution merge operator"
        )


_FIELDS = frozenset(
    {
        "schema",
        "namespace",
        "name",
        "version",
        "display_name",
        "primary_axis",
        "target",
        "secondary_constraints",
        "applicable_capabilities",
        "provides",
        "requires",
        "specification_roots",
        "authoring_inputs",
        "contributions",
        "conflicts",
        "co_requisites",
        "order_before",
        "order_after",
    }
)
_OPTIONAL_FIELDS = frozenset({"co_requisite_groups", "provider_overrides"})


def parse_flavor_markdown(content: bytes, *, source: str) -> FlavorMarkdownAuthoring:
    try:
        raw, description = parse_authoring_markdown(content, source=source)
        data = fields(raw, path=source, required=_FIELDS, optional=_OPTIONAL_FIELDS)
        if data["schema"] != FLAVOR_MARKDOWN_SCHEMA:
            raise FlavorMarkdownError(
                "flavor_markdown.schema_unsupported",
                f"schema must be {FLAVOR_MARKDOWN_SCHEMA!r}",
            )
        return FlavorMarkdownAuthoring(
            namespace=string_value(data["namespace"], f"{source}.namespace"),
            name=string_value(data["name"], f"{source}.name"),
            version=string_value(data["version"], f"{source}.version"),
            display_name=string_value(data["display_name"], f"{source}.display_name"),
            primary_axis=string_value(data["primary_axis"], f"{source}.primary_axis"),
            target=string_value(data["target"], f"{source}.target"),
            secondary_constraints=_mapping_tuple(
                data["secondary_constraints"], f"{source}.secondary_constraints"
            ),
            applicable_capabilities=string_tuple(
                data["applicable_capabilities"],
                f"{source}.applicable_capabilities",
            ),
            provides=_mapping_tuple(data["provides"], f"{source}.provides"),
            requires=_mapping_tuple(data["requires"], f"{source}.requires"),
            specification_roots=string_tuple(
                data["specification_roots"], f"{source}.specification_roots"
            ),
            authoring_inputs=tuple(
                _selector(item, f"{source}.authoring_inputs[{index}]")
                for index, item in enumerate(
                    _sequence(data["authoring_inputs"], source)
                )
            ),
            contributions=tuple(
                _contribution(item, f"{source}.contributions[{index}]")
                for index, item in enumerate(_sequence(data["contributions"], source))
            ),
            conflicts=string_tuple(data["conflicts"], f"{source}.conflicts"),
            co_requisites=string_tuple(
                data["co_requisites"], f"{source}.co_requisites"
            ),
            co_requisite_groups=(
                _mapping_tuple(
                    data["co_requisite_groups"], f"{source}.co_requisite_groups"
                )
                if "co_requisite_groups" in data
                else ()
            ),
            order_before=string_tuple(data["order_before"], f"{source}.order_before"),
            order_after=string_tuple(data["order_after"], f"{source}.order_after"),
            description=description,
            provider_overrides=(
                _mapping_tuple(
                    data["provider_overrides"], f"{source}.provider_overrides"
                )
                if "provider_overrides" in data
                else ()
            ),
        )
    except FlavorMarkdownError:
        raise
    except (TypeError, ValueError) as exc:
        raise FlavorMarkdownError(
            "flavor_markdown.invalid", f"invalid Flavor Markdown: {source}"
        ) from exc


def _sequence(value: object, path: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise FlavorMarkdownError("flavor_markdown.invalid", f"{path} must be a list")
    return tuple(value)


def _mapping_tuple(value: object, path: str) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(_sequence(value, path)):
        if not isinstance(item, dict) or any(not isinstance(key, str) for key in item):
            raise FlavorMarkdownError(
                "flavor_markdown.invalid", f"{path}[{index}] must be a mapping"
            )
        result.append(item)
    return tuple(result)


def _selector(value: object, path: str) -> FlavorContentSelector:
    data = fields(value, path=path, required=frozenset({"kind", "uri"}))
    return FlavorContentSelector(
        string_value(data["kind"], f"{path}.kind"),
        string_value(data["uri"], f"{path}.uri"),
    )


def _contribution(value: object, path: str) -> AuthoredFlavorContribution:
    data = fields(
        value,
        path=path,
        required=frozenset(
            {"contribution_id", "kind", "merge_operator", "slot", "content"}
        ),
    )
    return AuthoredFlavorContribution(
        contribution_id=string_value(
            data["contribution_id"], f"{path}.contribution_id"
        ),
        kind=string_value(data["kind"], f"{path}.kind"),
        merge_operator=string_value(data["merge_operator"], f"{path}.merge_operator"),
        slot=string_value(data["slot"], f"{path}.slot"),
        content=_selector(data["content"], f"{path}.content"),
    )


__all__ = [
    "FLAVOR_MARKDOWN_SCHEMA",
    "FlavorMarkdownAuthoring",
    "FlavorMarkdownError",
    "parse_flavor_markdown",
]
