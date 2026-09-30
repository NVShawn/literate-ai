"""Project package-specific rustc-env output from a checked Cargo JSON stream."""

import os
from pathlib import Path


def cargo_build_environments(records, metadata, *, workspace):
    """Return copied package overlays; refuse ambiguous host/target output.

    The caller first validates the complete compilation stream and workspace graph.
    Values are process inputs, not evidence that paths named by them are in custody.
    """
    packages = {package["id"]: package for package in metadata["packages"]}
    observed = {}
    total = 0
    count = 0
    for record in records:
        if record.get("reason") != "build-script-executed":
            continue
        count += 1
        package_id = record.get("package_id")
        pairs = record.get("env")
        if (
            count > 4096
            or not isinstance(package_id, str)
            or package_id not in packages
            or not isinstance(pairs, list)
            or len(pairs) > 256
        ):
            raise ValueError("retained.tests.build-environment-invalid")
        values = {}
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("retained.tests.build-environment-invalid")
            key, value = pair
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 128
                or not isinstance(value, str)
                or len(value) > 65536
                or "=" in key
                or "\x00" in key + value
            ):
                raise ValueError("retained.tests.build-environment-invalid")
            key = key.upper() if os.name == "nt" else key
            if key.casefold() in {name.casefold() for name in values}:
                raise ValueError("retained.tests.build-environment-ambiguous")
            total += len(key.encode("utf-8")) + len(value.encode("utf-8"))
            if total > 1024 * 1024:
                raise ValueError("retained.tests.build-environment-limit")
            values[key] = value
        overlay = tuple(sorted(values.items()))
        if package_id in observed and observed[package_id] != overlay:
            raise ValueError("retained.tests.build-environment-ambiguous")
        observed[package_id] = overlay
    result = {}
    for package_id, overlay in observed.items():
        package = packages[package_id]
        root = Path(package["manifest_path"]).parent
        if package["source"] is None and root.is_relative_to(workspace):
            if dict(overlay).get("CARGO_MANIFEST_DIR", str(root)) != str(root):
                raise ValueError("retained.tests.manifest-environment-override")
            result[root.relative_to(workspace).as_posix()] = overlay
    return result
