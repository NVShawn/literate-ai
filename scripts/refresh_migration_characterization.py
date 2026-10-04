#!/usr/bin/env python3
"""Refresh the current migration characterization after an intentional wire change."""

from __future__ import annotations

import json
from pathlib import Path

from literate_ai.contracts import canonical_identity
from tests.support.migration_characterization import (
    CURRENT_FIXTURE_PATH,
    _wire_shape_observation,
)


def main() -> int:
    path = Path(CURRENT_FIXTURE_PATH)
    document = json.loads(path.read_bytes())
    observations = document["observations"]
    observations["wire_shapes"] = _wire_shape_observation()
    document["observations_identity"] = canonical_identity(observations).uri
    path.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
