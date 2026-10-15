#!/bin/bash
# PreToolUse hook (Bash): keep the personal GitHub credential one-off and keep the ABB token off this repo.
# See CLAUDE.md "Push with the personal GitHub account" and the release-addon skill.
cmd=$(jq -r '.tool_input.command // empty')
block() { echo "Blocked: $1" >&2; exit 2; }
if echo "$cmd" | grep -qE 'git( +-C +[^ ]+)? +config\b[^;&|]*credential'; then
  block "don't persist a git credential helper. Pass it inline with 'git -c credential.helper=...' (see /release-addon)."
fi
if echo "$cmd" | grep -qE 'git-credentials|credential-store|gh +auth +setup-git'; then
  block "don't store GitHub credentials on disk. Use the one-off helper from /release-addon."
fi
if echo "$cmd" | grep -qE 'git +remote +(add|set-url)[^;&|]*://[^/ ]*@'; then
  block "no credentials in the remote URL."
fi
if echo "$cmd" | grep -qE '(^|[;&|] *)git( +-c +[^ ]+| +-C +[^ ]+)* +push\b' && ! echo "$cmd" | grep -q 'credential.helper='; then
  block "plain 'git push' uses the ABB GITHUB_TOKEN and gets a 403. Use the push command from /release-addon."
fi
exit 0
