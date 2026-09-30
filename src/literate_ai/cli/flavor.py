"""litai flavor subcommands: add a Flavor to an existing project."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from literate_ai.adapters.flavor_add import FlavorAddError, add_flavor_to_project

from .errors import CliFailure


def flavor_add_from_args(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return add_flavor_to_project(
            Path(args.project),
            args.selector,
            set_default=not getattr(args, "no_default", False),
        )
    except FlavorAddError as exc:
        raise CliFailure(exc.code, exc.message) from exc


def add_flavor_parser(commands: argparse._SubParsersAction) -> None:
    flavor = commands.add_parser(
        "flavor",
        help="add a Flavor to an existing project without re-running init",
    )
    flavor_commands = flavor.add_subparsers(
        dest="flavor_command", required=True, parser_class=type(flavor)
    )
    add = flavor_commands.add_parser(
        "add",
        help="copy one shipped Flavor into the project and select it",
    )
    add.add_argument("selector", help="Flavor name, directory, or flavor:// coordinate")
    add.add_argument("--project", default=".", help="canonical project root")
    add.add_argument(
        "--no-default",
        action="store_true",
        default=False,
        help="install the Flavor files without adding it to default_flavor_selectors "
        "(use for polyglot projects where per-Component flavor_slots select it)",
    )


__all__ = ["add_flavor_parser", "flavor_add_from_args"]
