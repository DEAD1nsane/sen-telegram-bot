# Repository Rules

- After every `git push`, automatically check Railway deployment status and report the result.
  - Check immediately after push, then wait 60s and check again if still deploying
  - Use Railway MCP (get-status / environment-status), not the Railway CLI (uninstalled). Look up this repo's project in `~/GitHub/.railway-projects.json` by directory name - works for any checked-out repo, no local link needed.
