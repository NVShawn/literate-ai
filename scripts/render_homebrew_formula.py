"""Render the Homebrew formula for installing literate-ai itself."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from literate_ai.version import DISTRIBUTION_VERSION

_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

FORMULA_TEMPLATE = """class LiterateAi < Formula
  include Language::Python::Virtualenv

  LITERATE_AI_VERSION = "{version}"

  desc "Specification-led software lifecycle control plane"
  homepage "https://github.com/{repository}"
  license "Apache-2.0"
  version LITERATE_AI_VERSION

  # Replace url/sha256 with the published sdist when the matching tag exists.
  url "https://github.com/{repository}/archive/refs/tags/v#{{LITERATE_AI_VERSION}}.tar.gz"
  sha256 "{sha256}"

  depends_on "python@3.13"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/litai --version")
  end
end
"""


def _release_repository() -> str:
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "literate.release.json").read_text(
            encoding="utf-8"
        )
    )
    repository = policy.get("provider", {}).get("repository")
    if (
        not isinstance(repository, str)
        or _GITHUB_REPOSITORY.fullmatch(repository) is None
    ):
        raise ValueError("release provider repository must be OWNER/REPOSITORY")
    return repository


def render_homebrew_formula(
    *,
    version: str = DISTRIBUTION_VERSION,
    sha256: str | None = None,
    repository: str | None = None,
) -> str:
    digest = sha256 or ("0" * 64)
    selected_repository = repository or _release_repository()
    return FORMULA_TEMPLATE.format(
        version=version,
        sha256=digest,
        repository=selected_repository,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sha256")
    arguments = parser.parse_args(argv)
    text = render_homebrew_formula(sha256=arguments.sha256)
    if arguments.output is None:
        print(text, end="")
    else:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
