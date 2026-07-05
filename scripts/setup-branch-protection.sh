#!/usr/bin/env bash
#
# Require the CI test jobs to pass before anything merges to main.
#
# THIS IS A MANUAL, ONE-TIME OPS STEP — it changes GitHub repo settings and needs
# repo-admin rights + an authenticated `gh`. It is intentionally NOT run by any
# workflow or automation. Run it yourself when you're ready:
#
#     bash scripts/setup-branch-protection.sh                 # main on the current repo
#     bash scripts/setup-branch-protection.sh OWNER/REPO main
#
# It makes both test jobs ("test" = JS unit/e2e, "Pipeline tests (pytest)" = the
# signalpipe suite) required status checks, with "strict" (branch must be up to
# date) enabled. Adjust the JSON below to taste.
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
BRANCH="${2:-main}"

echo "Setting branch protection on ${REPO}@${BRANCH} ..."

gh api -X PUT "repos/${REPO}/branches/${BRANCH}/protection" --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "checks": [
      { "context": "test" },
      { "context": "Pipeline tests (pytest)" }
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON

echo "Done. Required checks on ${REPO}@${BRANCH}: 'test' + 'Pipeline tests (pytest)'."
