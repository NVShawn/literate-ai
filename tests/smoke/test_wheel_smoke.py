"""Unit tests for installed-wheel smoke-test source custody."""

from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path, PurePosixPath

from tools.build_backend.literate_ai_build_backend import distribution_origin_bytes


class CleanWheelSourceProjectionTests(unittest.TestCase):
    def test_every_template_data_file_is_admitted_by_wheel_package_data(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        definition = tomllib.loads(
            (repository / "pyproject.toml").read_text(encoding="utf-8")
        )
        patterns = definition["tool"]["setuptools"]["package-data"][
            "literate_ai.project_template"
        ]
        template = repository / "src/literate_ai/project_template"
        uncovered = []
        for path in sorted(template.rglob("*")):
            if (
                not path.is_file()
                or path.suffix in {".py", ".pyc"}
                or "__pycache__" in path.parts
            ):
                continue
            relative = PurePosixPath(path.relative_to(template).as_posix())
            if not any(relative.match(pattern) for pattern in patterns):
                uncovered.append(relative.as_posix())

        self.assertEqual(uncovered, [])

    def test_distribution_origin_is_exact_and_rejects_credentials(self) -> None:
        document = json.loads(
            distribution_origin_bytes(
                {
                    "LITAI_BUILD_REPOSITORY_URL": (
                        "ssh://git.example.test/operator/literate-ai.git"
                    ),
                    "LITAI_BUILD_GIT_REVISION": "a" * 40,
                }
            )
        )
        self.assertEqual(
            document,
            {
                "schema": "literate-ai/distribution-origin@1",
                "repository_url": ("ssh://git.example.test/operator/literate-ai.git"),
                "git_revision": "a" * 40,
            },
        )
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            distribution_origin_bytes(
                {
                    "LITAI_BUILD_REPOSITORY_URL": (
                        "https://token@example.test/literate-ai.git"
                    ),
                    "LITAI_BUILD_GIT_REVISION": "a" * 40,
                }
            )


if __name__ == "__main__":
    unittest.main()
