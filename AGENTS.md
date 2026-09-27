# Repository Rules

- After every `git push`, automatically check Railway deployment status and report the result.
  - Wait 60s after push first (immediate checks always show deploying), then check; if still deploying, wait another 60s and check once more, then report whatever the state is.
  - Check immediately after push, then wait 60s and check again if still deploying
  - Use Railway MCP (get-status / environment-status), not the Railway CLI (uninstalled). Look up this repo's project in `~/GitHub/.railway-projects.json` by directory name - works for any checked-out repo, no local link needed.
- After finishing any long or complicated task, run `~/bin/buzz "<short title>" "<one-line result>"` so the user gets a notification (away-aware: silent if active, sound+buzz if away).

- Machine convention: new repos via `ginit`, clones via `gclone` (both seed the shared AGENTS.md automatically).
