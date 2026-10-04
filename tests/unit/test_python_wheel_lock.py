from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import unittest
import zipfile

from packaging.markers import default_environment

from literate_ai.adapters.dependencies.acquisition import (
    _generated_lock_projection,
)
from literate_ai.adapters.dependencies.python_lock import (
    SCHEMA,
    parse_python_wheel_lock,
    verify_locked_wheel,
)
from literate_ai.adapters.dependencies.types import DependencyObservationError


def wheel_record(name="example", dependencies=()):
    metadata = (
        f"Metadata-Version: 2.3\nName: {name}\nVersion: 1.0\nRequires-Python: >=3.11\n"
        + "".join(
            f"Requires-Dist: {requirement}\n" for requirement in sorted(dependencies)
        )
        + "\n"
    )
    stream = io.BytesIO()
    files = {
        f"{name}-1.0.dist-info/METADATA": metadata.encode(),
        f"{name}/__init__.py": b"VALUE = 1\n",
        f"{name}-1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"
        ),
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for path, data in sorted(files.items()):
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest())
            .rstrip(b"=")
            .decode()
        )
        writer.writerow([path, "sha256=" + digest, len(data)])
    writer.writerow([f"{name}-1.0.dist-info/RECORD", "", ""])
    files[f"{name}-1.0.dist-info/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(stream, "w") as archive:
        for path, data in sorted(files.items()):
            archive.writestr(zipfile.ZipInfo(path), data)
    content = stream.getvalue()
    return {
        "name": name,
        "version": "1.0",
        "filename": f"{name}-1.0-py3-none-any.whl",
        "sha256": hashlib.sha256(content).hexdigest(),
        "requires_python": ">=3.11",
        "requires_dist": sorted(dependencies),
    }, content


def document():
    example, _ = wheel_record(dependencies=("helper>=1",))
    helper, _ = wheel_record("helper")
    return {
        "schema": SCHEMA,
        "environment": default_environment(),
        "tags": ["py3-none-any"],
        "requirements": ["example==1.0"],
        "packages": [example, helper],
    }


class PythonWheelLockTests(unittest.TestCase):
    def parse(self, value):
        return parse_python_wheel_lock(json.dumps(value))

    def test_hash_drift(self):
        lock = self.parse(document())
        with self.assertRaises(DependencyObservationError) as caught:
            verify_locked_wheel(lock.packages[0], io.BytesIO(b"changed bytes"))
        self.assertEqual(
            caught.exception.code, "dependencies.python-wheel-hash-mismatch"
        )

    def test_parser_alone_cannot_bypass_standard_admission(self):
        with self.assertRaises(DependencyObservationError) as caught:
            _generated_lock_projection(
                {
                    "requirements.txt": "example==1.0\n",
                    "python-wheel-lock.json": json.dumps(document()),
                }
            )
        self.assertEqual(
            caught.exception.code, "dependencies.python-acquisition-evidence-missing"
        )


if __name__ == "__main__":
    unittest.main()
