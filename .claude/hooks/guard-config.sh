#!/bin/bash
# PreToolUse hook (Edit|Write): settings live in the DB, so the add-on config.yaml must not grow options:/schema:.
in=$(cat)
case "$(echo "$in" | jq -r '.tool_input.file_path // empty')" in */training_coach/config.yaml) ;; *) exit 0 ;; esac
if echo "$in" | jq -r '.tool_input.new_string // .tool_input.content // empty' | grep -qE '^(options|schema):'; then
  echo "Blocked: settings live in the DB (app/config.py Config + apply_form), not in add-on options. See CLAUDE.md." >&2
  exit 2
fi
exit 0
