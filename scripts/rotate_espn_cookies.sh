#!/usr/bin/env bash
# Rotate the ESPN session cookies and prove they work.
#
# GitHub has no trigger that fires when a secret changes -- there is no such
# event in Actions at all -- so "validate on rotation" has to be something
# the rotation itself does. This script is that: set both secrets, then
# immediately run the workflow that depends on them and wait for the verdict.
#
# Rotating is only half the problem. Cookies also expire *between* rotations,
# silently, and no amount of triggering-on-change would catch that -- nothing
# changes when a cookie dies. That gap is covered by .github/workflows/health.yml,
# which probes on a schedule.
#
# Usage:
#   ./scripts/rotate_espn_cookies.sh                 # prompts for both values
#   ESPN_S2=... SWID='{...}' ./scripts/rotate_espn_cookies.sh --no-prompt
#
# Get the values from a logged-in browser:
#   DevTools > Application > Cookies > fantasy.espn.com
#   espn_s2  -- copy url-encoded, exactly as shown
#   SWID     -- include the surrounding braces

set -euo pipefail

REPO="${REPO:-rahulAgrBej/espn-fantasy-football-sandbox}"
ENVIRONMENT="${ENVIRONMENT:-gh_env}"
WORKFLOW="${WORKFLOW:-espn.yml}"

die() { printf '\n%s\n' "$*" >&2; exit 1; }

command -v gh >/dev/null || die "gh is not installed -- see https://cli.github.com"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated -- run: gh auth login"

# Read the values. -s keeps them off the terminal and out of shell history;
# they are credentials, and this is the one moment they pass through a human's
# hands.
if [ "${1:-}" != "--no-prompt" ]; then
    printf 'espn_s2 (input hidden): '; read -rs ESPN_S2; printf '\n'
    printf 'SWID, braces included (input hidden): '; read -rs SWID; printf '\n'
fi

[ -n "${ESPN_S2:-}" ] || die "ESPN_S2 is empty."
[ -n "${SWID:-}" ]    || die "SWID is empty."

# SWID without braces is the single most common way this goes wrong, and it
# fails later as an opaque 401 rather than as anything that names the cause.
case "$SWID" in
    \{*\}) ;;
    *) die "SWID must include the surrounding braces, e.g. {XXXXXXXX-XXXX-...}." ;;
esac

echo
echo "Updating $ENVIRONMENT secrets on $REPO ..."
printf '%s' "$ESPN_S2" | gh secret set ESPN_S2 --env "$ENVIRONMENT" --repo "$REPO"
printf '%s' "$SWID"    | gh secret set SWID    --env "$ENVIRONMENT" --repo "$REPO"
echo "  ESPN_S2, SWID updated."

# Validate against the real workflow rather than trusting that the paste was
# clean. A cookie that was copied truncated looks identical to a good one
# until something actually authenticates with it.
echo
echo "Validating with $WORKFLOW ..."
gh workflow run "$WORKFLOW" --ref main --repo "$REPO"

# The run needs a moment to be queryable before its id exists.
sleep 8
run_id="$(gh run list --workflow="$WORKFLOW" --limit 1 --repo "$REPO" \
          --json databaseId --jq '.[0].databaseId')"
[ -n "$run_id" ] || die "Could not find the dispatched run. Check: gh run list --repo $REPO"

echo "  run $run_id -- waiting ..."
if gh run watch "$run_id" --repo "$REPO" --interval 10 --exit-status >/dev/null 2>&1; then
    echo
    echo "Cookies are valid and $WORKFLOW succeeded."
else
    echo
    echo "$WORKFLOW FAILED with the new cookies." >&2
    echo "  gh run view $run_id --log-failed --repo $REPO" >&2
    echo >&2
    echo "Exit 3 from probe means the cookies are still bad -- most often a" >&2
    echo "truncated espn_s2, or SWID pasted without its braces." >&2
    exit 1
fi
