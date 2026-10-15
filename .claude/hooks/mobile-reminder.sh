#!/bin/bash
# Stop hook: if templates or CSS were edited this session after the last mobile_check.py run, make Claude run check-mobile.
in=$(cat)
[ "$(echo "$in" | jq -r '.stop_hook_active')" = "true" ] && exit 0
t=$(echo "$in" | jq -r '.transcript_path // empty')
[ -f "$t" ] || exit 0
last_edit=$(grep -nE '"type":"tool_use".*"name":"(Edit|Write|MultiEdit)".*"file_path":"[^"]*training_coach/app/(templates|static)/' "$t" | tail -1 | cut -d: -f1)
[ -n "$last_edit" ] || exit 0
last_check=$(grep -nE '"type":"tool_use".*"command":"[^"]*mobile_check\.py' "$t" | tail -1 | cut -d: -f1)
if [ -z "$last_check" ] || [ "$last_edit" -gt "$last_check" ]; then
  jq -n '{decision:"block", reason:"Templates or CSS changed since the last mobile check. Run the check-mobile skill (tools/mobile_check.py) before finishing."}'
fi
exit 0
