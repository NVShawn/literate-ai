"""Owned isolated-process driver for one exact pip wheel installation profile.

Executed as a script by python_install.py, never as installed application code.
The caller has verified every input wheel and entry-point declaration first.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import sys
from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    mode, installer, request_path, output = sys.argv[1:]
    if mode not in {"project", "install"}:
        raise ValueError("Unsupported installer driver mode")
    sys.dont_write_bytecode = True
    sys.path.insert(0, installer)
    from pip._internal.models.scheme import Scheme
    from pip._internal.operations.install.wheel import (
        PipScriptMaker,
        fix_script,
        get_console_script_specs,
        install_wheel,
    )

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    output_root = Path(output)
    results = {}
    for package in request:
        name, wheel_path = package["name"], package["wheel"]
        root = output_root / name if mode == "project" else output_root
        scheme = {
            "purelib": "site",
            "platlib": "site",
            "scripts": "scripts",
            "headers": "headers/" + name,
            "data": "data",
        }
        console, gui = package["console"], package["gui"]
        skipped = []
        replaced = {}
        installed = []
        with ZipFile(wheel_path) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                path = entry.filename
                parts = path.split("/")
                destination = "site/" + path
                if parts[0].endswith(".data"):
                    destination = scheme[parts[1]] + "/" + "/".join(parts[2:])
                if (
                    len(parts) > 2
                    and parts[0].endswith(".data")
                    and parts[1] == "scripts"
                ):
                    basename = parts[-1]
                    match = basename
                    for suffix in ("-script.py", ".exe", ".pya"):
                        if basename.lower().endswith(suffix):
                            match = basename[: -len(suffix)]
                            break
                    if match in console or match in gui:
                        skipped.append(path)
                        continue
                    with archive.open(entry) as stream:
                        first = stream.readline(1024 * 1024)
                        if first.startswith(b"#!python"):
                            replaced[path] = destination
                            if mode == "project":
                                target = root / destination
                                target.parent.mkdir(parents=True, exist_ok=True)
                                with target.open("wb") as target_stream:
                                    target_stream.write(first)
                                    while block := stream.read(1024 * 1024):
                                        target_stream.write(block)
                                fix_script(str(target))
                installed.append(destination)
        generated = []
        if mode == "project":
            (root / "scripts").mkdir(parents=True, exist_ok=True)
            maker = PipScriptMaker(None, str(root / "scripts"))
            maker.executable = sys.executable
            maker.clobber = True
            maker.variants = {""}
            maker.set_mode = True
            generated.extend(maker.make_multiple(get_console_script_specs(console)))
            generated.extend(
                maker.make_multiple(
                    [f"{key} = {value}" for key, value in gui.items()], {"gui": True}
                )
            )
            generated = [Path(path).relative_to(root).as_posix() for path in generated]
        else:
            install_wheel(
                name,
                wheel_path,
                Scheme(**{key: str(root / value) for key, value in scheme.items()}),
                req_description=name,
                pycompile=False,
                warn_script_location=False,
                direct_url=None,
                requested=True,
                script_executable=sys.executable,
            )
            # The projection is a prior process result bound into the request.
            generated = package["generated"]
            # Normalize RECORD from the fixed profile's exact ownership map. pip
            # otherwise retains stale rows for suppressed setuptools wrappers.
            record = next(
                path for path in installed if path.endswith(".dist-info/RECORD")
            )
            metadata = record.rsplit("/", 1)[0]
            installed.extend(
                [metadata + "/INSTALLER", metadata + "/REQUESTED", *generated]
            )
            with (root / record).open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                for path in sorted(set(installed)):
                    relative = os.path.relpath(root / path, root / "site").replace(
                        os.sep, "/"
                    )
                    if path == record:
                        writer.writerow([relative, "", ""])
                        continue
                    digest = hashlib.sha256()
                    size = 0
                    with (root / path).open("rb") as payload:
                        while block := payload.read(1024 * 1024):
                            size += len(block)
                            digest.update(block)
                    writer.writerow(
                        [
                            relative,
                            "sha256="
                            + base64.urlsafe_b64encode(digest.digest())
                            .rstrip(b"=")
                            .decode(),
                            size,
                        ]
                    )
        results[name] = {
            "scheme": scheme,
            "replaced": replaced,
            "skipped": sorted(skipped),
            "generated": sorted(generated),
        }
    print(json.dumps(results, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
