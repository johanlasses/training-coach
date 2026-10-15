#!/bin/bash
# PostToolUse hook: enforce CLAUDE.md rules on the file just edited.
# Templates: every URL needs {{ base }} (ingress), and no inline grid-template-columns (breaks mobile media queries).
# Python: ruff, bugs-only rule set from pyproject.toml. Report only, never auto-fix. Unused imports/vars are skipped here (often mid-edit); CI checks them.
f=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')
[ -f "$f" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
case "$f" in
  */training_coach/app/templates/*.html)
    bad=$(grep -nE '(href|action|src|formaction)="/([^/]|")|style="[^"]*grid-template-columns' "$f")
    if [ -n "$bad" ]; then
      echo "$f breaks a template rule (URLs need {{ base }} for ingress; grid layouts belong in app.css):" >&2
      echo "$bad" >&2
      exit 2
    fi ;;
  *.py)
    out=$(.venv/bin/ruff check --quiet --ignore F401,F841 "$f" 2>&1)
    if [ $? -ne 0 ]; then
      echo "ruff found problems in $f:" >&2
      echo "$out" | tail -25 >&2
      exit 2
    fi ;;
esac
exit 0
