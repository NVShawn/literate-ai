"""Package attribution and strict bounds for Cargo build-script environments."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.retained_cargo_build_environment import (
    cargo_build_environments,
)


class CargoBuildEnvironmentTests(unittest.TestCase):
    def test_tool_binding_accepts_empty_values_but_rejects_invalid_process_input(self):
        binding = LocalComponentToolBinding(
            sys.executable, environment=(("EMPTY", ""),)
        )
        self.assertEqual(binding.environment, (("EMPTY", ""),))
        for pair in (("BAD=NAME", "v"), ("NUL", "\x00"), ("N\x00UL", "v")):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                LocalComponentToolBinding(sys.executable, environment=(pair,))

    def setUp(self):
        self.workspace = Path.cwd() / "workspace"
        self.metadata = {
            "packages": [
                {
                    "id": name,
                    "manifest_path": str(self.workspace / name / "Cargo.toml"),
                    "source": None,
                }
                for name in ("one", "two")
            ]
        }
        self.records = [
            {
                "reason": "build-script-executed",
                "package_id": "one",
                "env": [["VALUE", "one=value"], ["EMPTY", ""]],
            },
            {
                "reason": "build-script-executed",
                "package_id": "two",
                "env": [["VALUE", "two"]],
            },
        ]

    def project(self, records=None):
        return cargo_build_environments(
            self.records if records is None else records,
            self.metadata,
            workspace=self.workspace,
        )

    def test_package_values_do_not_leak_and_are_copied(self):
        observed = self.project()
        self.records[0]["env"][0][1] = "changed"
        self.assertEqual(dict(observed["one"]), {"VALUE": "one=value", "EMPTY": ""})
        self.assertEqual(dict(observed["two"]), {"VALUE": "two"})

    def test_unknown_package_or_malformed_values_refuse(self):
        for change in (
            {"package_id": "foreign"},
            {"package_id": []},
            {"env": None},
            {"env": [["X"]]},
            {"env": [["X", 1]]},
            {"env": [["", "v"]]},
            {"env": [["X=Y", "v"]]},
            {"env": [["X", "\x00"]]},
            {"env": [["X", "a" * 65537]]},
            {"env": [["X" * 129, "v"]]},
        ):
            with self.subTest(change=list(change)):
                records = copy.deepcopy(self.records)
                records[0].update(change)
                with self.assertRaises(ValueError):
                    self.project(records)

    def test_duplicate_keys_and_conflicting_build_contexts_refuse(self):
        records = copy.deepcopy(self.records)
        records[0]["env"].append(["VALUE", "other"])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.project(records)
        records = copy.deepcopy(self.records)
        records.append({**records[0], "env": [["VALUE", "other-context"]]})
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.project(records)
        self.assertEqual(self.project(self.records + self.records), self.project())

    def test_windows_key_aliases_refuse(self):
        records = copy.deepcopy(self.records)
        records[0]["env"].append(["value", "other"])
        with patch(
            "literate_ai.adapters.retained_cargo_build_environment.os.name", "nt"
        ):
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                self.project(records)

    def test_manifest_directory_cannot_be_overridden(self):
        records = copy.deepcopy(self.records)
        records[0]["env"] = [["CARGO_MANIFEST_DIR", str(self.workspace / "two")]]
        with self.assertRaisesRegex(ValueError, "manifest-environment-override"):
            self.project(records)

    def test_global_and_per_record_limits(self):
        record = copy.deepcopy(self.records[0])
        record["env"] = [[f"K{i}", ""] for i in range(257)]
        with self.assertRaises(ValueError):
            self.project([record])
        record["env"] = [[f"K{i}", "x" * 65536] for i in range(16)]
        with self.assertRaisesRegex(ValueError, "limit"):
            self.project([record])
        record["env"] = []
        with self.assertRaises(ValueError):
            self.project([record] * 4097)
