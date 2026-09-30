#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(git -C "$source_dir" rev-parse --show-toplevel)"
obj_dir="${OBJ_DIR:-$repo_dir/_build}"
check_only=0
if [[ "${1:-}" == "--check" ]]; then
  check_only=1
fi

python_bin=""
for candidate in \
  "$obj_dir/doc-toolchain/bin/python3" \
  "$obj_dir/doc-toolchain/bin/python" \
  "$obj_dir/doc-toolchain/Scripts/python.exe"
do
  if [[ -x "$candidate" ]]; then
    python_bin="$candidate"
    break
  fi
done
if [[ -z "$python_bin" && -z "${OBJ_DIR:-}" && -x "$repo_dir/.venv/bin/python3" ]]; then
  python_bin="$repo_dir/.venv/bin/python3"
fi
if [[ -z "$python_bin" ]]; then
  python_bin="python3"
fi

if ! "$python_bin" -c "import pptx, docx, PIL, lxml" >/dev/null 2>&1; then
  echo "Pinned authoring toolchain is not importable for ${python_bin}." >&2
  echo "Run \`make doc-toolchain-bootstrap\` to install python-pptx, python-docx, lxml, and Pillow into ignored OBJ_DIR." >&2
  echo "Refusing to pip-install from regenerate_python.sh without that explicit Makefile authorization." >&2
  exit 2
fi

if [[ "$check_only" -eq 1 ]]; then
  printf '%s\n' "$python_bin"
  exit 0
fi

build_dir="$obj_dir/literate-ai-manager-overview"
mkdir -p "$build_dir"

(
  cd "$source_dir"
  LITERATE_AI_DECK_SOURCE="$source_dir" \
  LITERATE_AI_REPO="$repo_dir" \
  OBJ_DIR="$obj_dir" \
  "$python_bin" build_deck.py
  LITERATE_AI_DECK_SOURCE="$source_dir" \
  LITERATE_AI_REPO="$repo_dir" \
  OBJ_DIR="$obj_dir" \
  "$python_bin" build_narrative.py
  if [[ "${LITERATE_AI_DECK_SKIP_RENDER:-}" != "1" ]]; then
    LITERATE_AI_DECK_SOURCE="$source_dir" \
    LITERATE_AI_REPO="$repo_dir" \
    OBJ_DIR="$obj_dir" \
    "$python_bin" render_slides.py
  fi
)

pptx="${LITERATE_AI_DECK_OUTPUT:-$source_dir/literate-ai-manager-and-engineering-overview.pptx}"
docx="${LITERATE_AI_NARRATIVE_OUTPUT:-$source_dir/literate-ai-manager-and-engineering-overview.docx}"
manifest="$build_dir/capability-manifest.json"
echo "PPTX: $pptx"
echo "DOCX: $docx"

acceptance="$build_dir/acceptance.json"
"$python_bin" "$repo_dir/scripts/verify_document_pair.py" \
  --manifest "$manifest" \
  --component "$repo_dir/components/literate-ai-overview/component.md" \
  --json "$acceptance"
echo "Acceptance report: $acceptance"
