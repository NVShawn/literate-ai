from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
import unittest
import zipfile
from unittest.mock import patch

from packaging.markers import default_environment

from literate_ai.adapters.dependencies.acquisition import (
    _generated_lock_projection,
    _python_wheel_lock_projection,
    _require_lock_graph_covered,
)
from literate_ai.adapters.dependencies.python_lock import (
    SCHEMA,
    parse_python_wheel_lock,
    verify_locked_wheel,
    verify_python_wheel_lock,
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

    def test_exact_graph_and_owned_wheel_metadata(self):
        lock = self.parse(document())
        self.assertEqual(lock.edges, (("@root", "example"), ("example", "helper")))
        _, content = wheel_record(dependencies=("helper>=1",))
        verify_locked_wheel(lock.packages[0], io.BytesIO(content))
        lock.require_target(default_environment(), ("py3-none-any",))

    def test_missing_transitive_package(self):
        value = document()
        value["packages"].pop()
        with self.assertRaises(DependencyObservationError):
            self.parse(value)

    def test_complete_acquired_inventory(self):
        value = document()
        lock = self.parse(value)
        _, example = wheel_record(dependencies=("helper>=1",))
        _, helper = wheel_record("helper")
        wheels = {"example": io.BytesIO(example), "helper": io.BytesIO(helper)}
        verify_python_wheel_lock(
            lock, wheels, environment=default_environment(), tags=("py3-none-any",)
        )
        for inventory in (
            {"example": wheels["example"]},
            {**wheels, "extra": io.BytesIO()},
        ):
            with self.assertRaises(DependencyObservationError) as caught:
                verify_python_wheel_lock(
                    lock,
                    inventory,
                    environment=default_environment(),
                    tags=("py3-none-any",),
                )
            self.assertEqual(
                caught.exception.code, "dependencies.python-wheel-inventory-mismatch"
            )

    def test_base_dependencies_remain_active_with_extras(self):
        value = document()
        value["requirements"] = ["example[feature]==1.0"]
        value["packages"][0]["requires_dist"] = ['helper>=1; extra != "feature"']
        self.assertIn(("example", "helper"), self.parse(value).edges)

    def test_unreachable_package(self):
        value = document()
        value["requirements"] = ["helper==1.0"]
        with self.assertRaises(DependencyObservationError):
            self.parse(value)

    def test_marker_selection(self):
        value = document()
        value["packages"][0]["requires_dist"] = [
            'helper>=1; sys_platform == "unsupported"'
        ]
        value["packages"].pop()
        self.assertEqual(self.parse(value).edges, (("@root", "example"),))

    def test_extras_selection(self):
        value = document()
        value["requirements"] = ["example[feature]==1.0"]
        value["packages"][0]["requires_dist"] = ['helper>=1; extra == "feature"']
        self.assertIn(("example", "helper"), self.parse(value).edges)

    def test_cycle_terminates_and_preserves_edges(self):
        value = document()
        value["packages"][1]["requires_dist"] = ["example==1.0"]
        self.assertEqual(len(self.parse(value).edges), 3)

    def test_missing_extra_edge_is_not_ignored(self):
        value = document()
        value["requirements"] = ["example[feature]==1.0"]
        value["packages"][0]["requires_dist"] = ['missing; extra == "feature"']
        with self.assertRaises(DependencyObservationError):
            self.parse(value)

    def test_metadata_dependency_omission_fails_against_wheel(self):
        value = document()
        value["packages"][0]["requires_dist"] = []
        value["packages"].pop()
        lock = self.parse(value)
        _, content = wheel_record(dependencies=("helper>=1",))
        with self.assertRaises(DependencyObservationError) as caught:
            verify_locked_wheel(lock.packages[0], io.BytesIO(content))
        self.assertEqual(
            caught.exception.code, "dependencies.python-wheel-metadata-mismatch"
        )

    def test_hash_drift(self):
        lock = self.parse(document())
        with self.assertRaises(DependencyObservationError) as caught:
            verify_locked_wheel(lock.packages[0], io.BytesIO(b"changed bytes"))
        self.assertEqual(
            caught.exception.code, "dependencies.python-wheel-hash-mismatch"
        )

    def test_archive_bound_and_closed_stream_fail_as_dependency_errors(self):
        lock = self.parse(document())
        _, content = wheel_record(dependencies=("helper>=1",))
        with (
            patch("literate_ai.adapters.dependencies.python_lock._MAX_WHEEL_BYTES", 8),
            self.assertRaises(DependencyObservationError),
        ):
            verify_locked_wheel(lock.packages[0], io.BytesIO(content))
        closed = io.BytesIO(content)
        closed.close()
        with self.assertRaises(DependencyObservationError) as caught:
            verify_locked_wheel(lock.packages[0], closed)
        self.assertEqual(caught.exception.code, "dependencies.python-wheel-invalid")

    def test_metadata_must_be_at_matching_top_level_directory(self):
        for member in (
            "other-1.0.dist-info/METADATA",
            "example-2.0.dist-info/METADATA",
            "nested/example-1.0.dist-info/METADATA",
        ):
            with self.subTest(member=member):
                value = document()
                _, content = wheel_record(dependencies=("helper>=1",))
                replacement = io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(content)) as old:
                    with zipfile.ZipFile(replacement, "w") as new:
                        for entry in old.infolist():
                            name = (
                                member
                                if entry.filename.endswith("/METADATA")
                                else entry.filename
                            )
                            new.writestr(zipfile.ZipInfo(name), old.read(entry))
                value["packages"][0]["sha256"] = hashlib.sha256(
                    replacement.getvalue()
                ).hexdigest()
                lock = self.parse(value)
                with self.assertRaises(DependencyObservationError):
                    verify_locked_wheel(lock.packages[0], replacement)

    def test_sbom_projection_requires_every_package_and_edge(self):
        projection = _python_wheel_lock_projection(self.parse(document()))
        refs = {
            "python-wheel-lock:example": "pkg:pypi/example@1.0",
            "python-wheel-lock:helper": "pkg:pypi/helper@1.0",
        }
        edges = {
            ("application", refs["python-wheel-lock:example"]),
            (refs["python-wheel-lock:example"], refs["python-wheel-lock:helper"]),
        }
        _require_lock_graph_covered(
            projection,
            package_ref_by_lock_key=refs,
            root_ref="application",
            source_edges=edges,
        )
        for incomplete in (set(), {next(iter(edges))}):
            with self.assertRaises(DependencyObservationError) as caught:
                _require_lock_graph_covered(
                    projection,
                    package_ref_by_lock_key=refs,
                    root_ref="application",
                    source_edges=incomplete,
                )
            self.assertEqual(caught.exception.code, "dependencies.lock-edge-missing")
        with self.assertRaises(DependencyObservationError) as caught:
            _require_lock_graph_covered(
                projection,
                package_ref_by_lock_key={},
                root_ref="application",
                source_edges=edges,
            )
        self.assertEqual(caught.exception.code, "dependencies.lock-inventory-missing")

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

    def test_independent_target_observation_required(self):
        lock = self.parse(document())
        environment = default_environment()
        environment["platform_machine"] = "another-machine"
        with self.assertRaises(DependencyObservationError) as caught:
            lock.require_target(environment, ("py3-none-any",))
        self.assertEqual(
            caught.exception.code, "dependencies.python-lock-target-mismatch"
        )
        with self.assertRaises(DependencyObservationError) as caught:
            lock.require_target(default_environment(), ("cp311-cp311-win_amd64",))
        self.assertEqual(
            caught.exception.code, "dependencies.python-lock-target-mismatch"
        )

    def test_duplicate_json_key_rejected(self):
        with self.assertRaises(DependencyObservationError):
            parse_python_wheel_lock('{"schema":"a","schema":"b"}')

    def test_invalid_authorities_fail_closed(self):
        mutations = (
            lambda d: d.update(extra=True),
            lambda d: d.update(schema="unknown"),
            lambda d: d.update(
                requirements=["example @ https://example.invalid/pkg.whl"]
            ),
            lambda d: d["packages"][0].update(sha256="guess"),
            lambda d: d["packages"][0].update(filename="../example.whl"),
            lambda d: d["packages"][0].update(filename="other-1.0-py3-none-any.whl"),
            lambda d: d["packages"][0].update(filename="example-2.0-py3-none-any.whl"),
            lambda d: d["packages"][0].update(
                filename="example-1.0-cp310-cp310-win_amd64.whl"
            ),
            lambda d: d["packages"][0].update(requires_python=">=99"),
            lambda d: d["packages"][0].update(requires_dist=["helper>9"]),
            lambda d: d["packages"].append(copy.deepcopy(d["packages"][0])),
            lambda d: d["environment"].update(python_version="1.0"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                value = document()
                mutation(value)
                with self.assertRaises(DependencyObservationError):
                    self.parse(value)


if __name__ == "__main__":
    unittest.main()
