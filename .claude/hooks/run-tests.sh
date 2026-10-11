#!/bin/bash
# PostToolUse hook: run the test suite after edits to app code or tests.
# Silent on success; on failure prints the tail to stderr and exits 2 so Claude sees it.
f=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')
case "$f" in
  */training_coach/app/*|*/tests/*) ;;
  *) exit 0 ;;
esac
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" || exit 0
out=$(.venv/bin/python -m pytest -q -x -p no:cacheprovider 2>&1)
if [ $? -ne 0 ]; then
  echo "Tests failed after editing $f:" >&2
  echo "$out" | tail -25 >&2
  exit 2
fi
