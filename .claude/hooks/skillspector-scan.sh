#!/bin/sh
set -eu

if ! command -v jq >/dev/null 2>&1; then
    echo "SkillSpector hook: jq is required to inspect the edited path" >&2
    exit 127
fi

if ! file_path=$(jq -er '.tool_input.file_path // empty'); then
    echo "SkillSpector hook: tool input did not contain a file path" >&2
    exit 2
fi

case "$file_path" in
    *SKILL.md) ;;
    *) exit 0 ;;
esac

if ! command -v skillspector >/dev/null 2>&1; then
    echo "SkillSpector hook: skillspector is required for SKILL.md edits" >&2
    exit 127
fi

skillspector scan "$file_path"
