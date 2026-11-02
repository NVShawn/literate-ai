"""Native tool observations may describe plans; parsing is never a tool probe."""

import re
from pathlib import Path

from literate_ai.adapters.freebsd_packaging import _ABI, FreeBsdPkgToolBinding
from literate_ai.adapters.native_tar_archives import NativeTarToolBinding
from literate_ai.application.packaging import PackagingError

TOOLS_SCHEMA = "literate-ai/retained-unit-native-tool-observations@1"


class _PlanningPkgTool(FreeBsdPkgToolBinding):
    def require_unchanged(self):
        raise PackagingError("Parsed pkg observations require fresh native discovery")


class _PlanningTarTool(NativeTarToolBinding):
    def require_current(self):
        raise PackagingError("Parsed tar observations require fresh native discovery")


def retained_unit_tool_observations(pkg_tool, tar_tool):
    if not isinstance(pkg_tool, FreeBsdPkgToolBinding) or not isinstance(
        tar_tool, NativeTarToolBinding
    ):
        raise TypeError("retained.unit.typed-native-tool-observations-required")
    return {
        "schema": TOOLS_SCHEMA,
        "pkg": pkg_tool.to_dict(),
        "tar": tar_tool.to_dict(),
    }


def parse_retained_unit_tool_observations(value):
    """Use intentionally unexecutable paths until actual native discovery occurs."""
    if type(value) is not dict or set(value) != {"schema", "pkg", "tar"}:
        raise ValueError("retained.unit.closed-tool-observations-required")
    pkg, tar = value["pkg"], value["tar"]
    if (
        value["schema"] != TOOLS_SCHEMA
        or type(pkg) is not dict
        or set(pkg)
        != {
            "schema",
            "executable_sha256",
            "version",
            "abi",
            "architecture",
            "configuration",
            "plugins",
        }
        or type(tar) is not dict
        or set(tar) != {"schema", "executable_sha256", "version"}
        or pkg["schema"] != "literate-ai/freebsd-pkg-tool@1"
        or tar["schema"] != "literate-ai/native-libarchive-tar-tool@1"
        or pkg["configuration"] != "empty"
        or pkg["plugins"] is not False
    ):
        raise ValueError("retained.unit.exact-tool-observation-fields-required")
    for observation in (pkg, tar):
        if (
            not isinstance(observation["executable_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", observation["executable_sha256"]) is None
            or not isinstance(observation["version"], str)
            or not 1 <= len(observation["version"].encode("utf-8")) <= 4096
            or any(ord(character) < 32 for character in observation["version"])
        ):
            raise ValueError("retained.unit.bounded-tool-observation-required")
    if (
        len(pkg["version"]) > 128
        or not isinstance(pkg["abi"], str)
        or _ABI.fullmatch(pkg["abi"]) is None
        or not isinstance(pkg["architecture"], str)
        or re.fullmatch(r"[A-Za-z0-9:._-]{1,96}", pkg["architecture"]) is None
        or not tar["version"].startswith("bsdtar ")
        or re.search(r"\blibarchive [0-9][0-9A-Za-z.+_-]*\b", tar["version"]) is None
    ):
        raise ValueError("retained.unit.native-tool-observation-invalid")
    return (
        _PlanningPkgTool(
            Path("unexecuted-native-tools/pkg-static"),
            pkg["executable_sha256"],
            pkg["version"],
            pkg["abi"],
            pkg["architecture"],
        ),
        _PlanningTarTool(
            Path("unexecuted-native-tools/bsdtar"),
            tar["executable_sha256"],
            tar["version"],
        ),
    )
