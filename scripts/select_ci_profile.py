"""Select explicit PR feedback or full qualification, never impact-test selection."""

from __future__ import annotations

import json
import os
from pathlib import Path


def select_matrix(event: str, base_ref: str) -> dict:
    rows = json.loads(
        (Path(__file__).resolve().parents[1] / ".github/ci-matrix.json").read_text()
    )
    ordinary_pr = event == "pull_request" and not base_ref.startswith("release/")
    return {"include": [row for row in rows if not ordinary_pr or row["pr"]]}


if __name__ == "__main__":
    matrix = select_matrix(os.environ["CI_EVENT"], os.environ.get("CI_BASE_REF", ""))
    print("matrix=" + json.dumps(matrix, separators=(",", ":")))
