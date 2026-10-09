"""Declarative Mix project intent; admission never evaluates mix.exs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .identity import ContentIdentity, canonical_identity

MIX_PROJECT_SCHEMA = "literate-ai/mix-project@1"
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SUFFIX = r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
_VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)" + _SUFFIX
_REQUIREMENT_VERSION = r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,2}" + _SUFFIX
_TERM = rf"(?:~>|>=|<=|>|<|==|!=)?\s*{_REQUIREMENT_VERSION}"
_REQUIREMENT = re.compile(rf"{_TERM}(?:\s+(?:and|or)\s+{_TERM})*\Z")


@dataclass(frozen=True, slots=True)
class MixDependencyIntent:
    """One named public Hex requirement, without executable source authority."""

    name: str
    requirement: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise ValueError("Mix dependency requires one canonical application name")
        if (
            not isinstance(self.requirement, str)
            or len(self.requirement) > 256
            or self.requirement != self.requirement.strip()
            or _REQUIREMENT.fullmatch(self.requirement) is None
        ):
            raise ValueError("Mix dependency requires a bounded semantic version range")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "requirement": self.requirement}


@dataclass(frozen=True, slots=True)
class MixProjectIntent:
    """Data used to synthesize a native project only after build authorization."""

    app: str
    version: str
    description: str
    licenses: tuple[str, ...]
    dependencies: tuple[MixDependencyIntent, ...] = ()
    links: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.app, str) or _NAME.fullmatch(self.app) is None:
            raise ValueError("Mix project requires a canonical application name")
        if (
            not isinstance(self.version, str)
            or re.fullmatch(_VERSION, self.version) is None
        ):
            raise ValueError("Mix project requires one exact semantic version")
        if (
            not isinstance(self.description, str)
            or not 1 <= len(self.description) <= 256
            or any(ord(char) < 32 for char in self.description)
            or "#{" in self.description
        ):
            raise ValueError("Mix project requires a bounded literal description")
        if (
            not isinstance(self.licenses, tuple)
            or not 1 <= len(self.licenses) <= 16
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,127}", value)
                for value in self.licenses
            )
            or len(set(self.licenses)) != len(self.licenses)
        ):
            raise ValueError(
                "Mix project requires distinct literal license identifiers"
            )
        if (
            not isinstance(self.dependencies, tuple)
            or len(self.dependencies) > 512
            or any(
                not isinstance(value, MixDependencyIntent)
                for value in self.dependencies
            )
            or len({value.name for value in self.dependencies})
            != len(self.dependencies)
            or any(value.name == self.app for value in self.dependencies)
        ):
            raise ValueError("Mix project requires distinct external Hex requirements")

        if not isinstance(self.links, tuple) or not 1 <= len(self.links) <= 16:
            raise ValueError("Mix package requires bounded metadata links")
        labels = set()
        for item in self.links:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("Mix package link must be one label and URL")
            label, url = item
            if (
                not isinstance(label, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,63}", label)
                or label in labels
                or not isinstance(url, str)
                or not 1 <= len(url) <= 2048
                or "#{" in url
                or any(char.isspace() or ord(char) < 32 for char in url)
            ):
                raise ValueError("Mix package metadata link is invalid")
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(
                    "Mix package link requires an HTTP URL without credentials"
                )
            labels.add(label)

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": MIX_PROJECT_SCHEMA,
            "app": self.app,
            "version": self.version,
            "description": self.description,
            "licenses": list(self.licenses),
            "dependencies": [value.to_dict() for value in self.dependencies],
            "links": dict(self.links),
        }

    @classmethod
    def from_bytes(cls, content: bytes) -> MixProjectIntent:
        if not isinstance(content, bytes) or len(content) > 64 * 1024:
            raise ValueError("Mix project intent must be bounded UTF-8 JSON")

        def pairs(values):
            result = {}
            for name, value in values:
                if name in result:
                    raise ValueError("Mix project intent contains a duplicate key")
                result[name] = value
            return result

        try:
            value = json.loads(content, object_pairs_hook=pairs)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Mix project intent is invalid UTF-8 JSON") from exc
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema",
                "app",
                "version",
                "description",
                "licenses",
                "dependencies",
                "links",
            }
            or value["schema"] != MIX_PROJECT_SCHEMA
            or not isinstance(value["licenses"], list)
            or not isinstance(value["dependencies"], list)
            or not isinstance(value["links"], dict)
        ):
            raise ValueError("Mix project intent has an invalid authority shape")
        dependencies = []
        for dependency in value["dependencies"]:
            if not isinstance(dependency, dict) or set(dependency) != {
                "name",
                "requirement",
            }:
                raise ValueError(
                    "Mix dependency accepts only public Hex requirement intent"
                )
            dependencies.append(MixDependencyIntent(**dependency))
        return cls(
            value["app"],
            value["version"],
            value["description"],
            tuple(value["licenses"]),
            tuple(dependencies),
            tuple(value["links"].items()),
        )

    def native_project(
        self, *, package_files: tuple[str, ...], provider_ebins: tuple[str, ...] = ()
    ) -> bytes:
        """Produce fixed project code from checked literals, never raw source text."""
        if (
            not isinstance(package_files, tuple)
            or not package_files
            or any(
                not isinstance(path, str)
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
                or path.startswith("/")
                or any(part in {"", ".", ".."} for part in path.split("/"))
                for path in package_files
            )
        ):
            raise ValueError(
                "Mix package files must be canonical relative literal paths"
            )

        # JSON string syntax is valid for these Elixir literals. Reject interpolation
        # above so metadata cannot introduce executable expressions into the project.
        def literal(value):
            return json.dumps(value, ensure_ascii=False)

        # The trusted producer supplies retained native paths. They are not
        # authored Mix intent or acquired dependency declarations. Preserve Mix's
        # normal code-path pruning and restore only these exact provider paths
        # through a framework compiler immediately before Elixir compilation.
        from pathlib import Path

        if not isinstance(provider_ebins, tuple) or any(
            not isinstance(path, str)
            or not Path(path).is_absolute()
            or ".." in Path(path).parts
            or "#{" in path
            or any(ord(char) < 32 for char in path)
            for path in provider_ebins
        ):
            raise ValueError("Mix provider paths require absolute literal selections")
        provider_compiler = ""
        compiler_config = ""
        if provider_ebins:
            paths = ", ".join(map(literal, provider_ebins))
            compiler_config = (
                "     compilers: [:erlang, :litai_providers, :elixir, :app],\n"
            )
            provider_compiler = (
                "defmodule Mix.Tasks.Compile.LitaiProviders do\n"
                "  use Mix.Task.Compiler\n"
                "  def run(_) do\n"
                f"    Enum.each([{paths}], &Code.prepend_path/1)\n"
                "    {:ok, []}\n"
                "  end\n"
                "end\n"
            )

        deps = ", ".join(
            "{:" + dep.name + ", " + literal(dep.requirement) + "}"
            for dep in self.dependencies
        )
        files = ", ".join(map(literal, package_files))
        licenses = ", ".join(map(literal, self.licenses))
        links = ", ".join(
            literal(label) + " => " + literal(url) for label, url in self.links
        )
        return (
            "defmodule LitaiFrameworkMixProject do\n"
            "  use Mix.Project\n"
            "  def project do\n"
            f"    [app: :{self.app}, version: {literal(self.version)},\n"
            f"     description: {literal(self.description)},\n"
            '     elixirc_paths: ["."],\n'
            f"{compiler_config}"
            f"     deps: [{deps}],\n"
            f"     package: [files: [{files}], licenses: [{licenses}], "
            f"links: %{{{links}}}]]\n"
            "  end\n"
            "end\n"
            f"{provider_compiler}"
        ).encode()
