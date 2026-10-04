"""Real native baseline for the original-source SDK admission fixture."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.native_sdk_custody import (
    capture_native_sdk,
    materialize_native_sdk,
)
from literate_ai.contracts.executable_components.commands import (
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.native_sdks import NativeSdkSnapshot
from literate_ai.storage.cas import FileSystemCAS


class NativeSdkFixtureTests(unittest.TestCase):
    def test_relocated_public_sdk_calls_native_and_missing_library_fails(self) -> None:
        cmake = shutil.which("cmake")
        if cmake is None:
            self.skipTest("native SDK fixture requires CMake and a host C compiler")
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "native_sdk"
        with tempfile.TemporaryDirectory(prefix="native-sdk-fixture-") as directory:
            root = Path(directory).resolve()
            build = root / "build"
            for command in (
                (cmake, "-S", str(fixture), "-B", str(build)),
                (
                    cmake,
                    "--build",
                    str(build),
                    "--config",
                    "Release",
                    "--parallel",
                    "2",
                ),
            ):
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=120
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            producer = FileSystemCAS(root / "producer")
            native_root = build / "sdk"
            libraries = tuple(
                sorted(
                    path.relative_to(native_root).as_posix()
                    for path in (native_root / "python/vendor_math/lib").iterdir()
                    if path.suffix in {".so", ".dylib", ".dll"}
                )
            )
            snapshot = capture_native_sdk(
                native_root,
                store=producer,
                source_lock_identity=canonical_identity("fixture source lock claim"),
                recipe_identity=canonical_identity("fixture recipe claim"),
                target_identity=canonical_identity("fixture target claim"),
                license_identity=canonical_identity("fixture license claim"),
                import_surface=LibraryImportSurface(
                    "python",
                    "vendor_math",
                    (
                        LibraryCapabilityImport(
                            "vendor.math",
                            canonical_identity("fixture public contract"),
                            "vendor_math",
                            ("scale",),
                        ),
                    ),
                ),
                import_root="python",
                native_libraries=libraries,
            )
            received = NativeSdkSnapshot.from_dict(
                json.loads(json.dumps(snapshot.to_dict()))
            )
            self.assertEqual(received.identity, snapshot.identity)
            consumer = FileSystemCAS(root / "consumer")
            for item in snapshot.files:
                self.assertEqual(
                    consumer.put_file(producer.path_for(item.blob)), item.blob
                )
            shutil.rmtree(build)
            shutil.rmtree(producer.root)
            relocated_parent = root / "relocated SDK"
            relocated_parent.mkdir()
            relocated = (
                materialize_native_sdk(
                    received,
                    expected_identity=snapshot.identity,
                    store=consumer,
                    parent=relocated_parent,
                )
                / "python"
            )
            script = (
                "import json,sys; from pathlib import Path; "
                "sys.path.insert(0,sys.argv[1]); import vendor_math; "
                "assert Path(vendor_math.__file__).is_relative_to(Path(sys.argv[1])); "
                "print(json.dumps([vendor_math.scale(1.5,3),"
                "vendor_math.scale(-2.25,2),vendor_math.scale(0.5,0)]))"
            )
            command = (sys.executable, "-I", "-B", "-c", script, str(relocated))
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [4.5, -4.5, 0.0])
            shutil.rmtree(relocated / "vendor_math" / "lib")
            missing_script = (
                "import sys\n"
                "sys.path.insert(0,sys.argv[1])\n"
                "try:\n"
                "    import vendor_math\n"
                "except OSError:\n"
                "    raise SystemExit(17)\n"
                "raise AssertionError('missing native library was accepted')\n"
            )
            missing = subprocess.run(
                (sys.executable, "-I", "-B", "-c", missing_script, str(relocated)),
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(missing.returncode, 17, missing.stderr)
            self.assertEqual(missing.stdout, "")
