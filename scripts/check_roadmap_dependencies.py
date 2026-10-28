"""Read-only roadmap graph gate, including the policy-selected release ledger."""

from __future__ import annotations

import sys
from pathlib import Path

from literate_ai.adapters.roadmap_dependencies import require_project_roadmap_acyclic
from literate_ai.application.roadmap_dependencies import RoadmapDependencyError

if __name__ == "__main__":
    try:
        require_project_roadmap_acyclic(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
    except RoadmapDependencyError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        raise SystemExit(1) from exc
    print("Roadmap completion dependencies: acyclic")
