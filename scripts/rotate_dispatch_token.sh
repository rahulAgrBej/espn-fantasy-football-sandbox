#!/usr/bin/env bash
# Rotate the GitHub PAT that AWS uses to dispatch the collection workflows,
# and prove the new one works end to end.
#
# The token lives in the EventBridge connection `ff-github-dispatch`, which
# stores it in Secrets Manager. It is rotated with `aws events
# update-connection` rather than by redeploying infra/scheduler.yaml: a stack
# update would put a live credential through CloudFormation parameters on
# every rotation. The cost is that the stack shows permanent drift on that one
# property, which is deliberate -- see docs/aws-scheduling.md.
#
# Fine-grained PATs expire within 366 days, so unlike the ESPN cookies this
# credential dies on a known date. It can still die early (revoked, repo
# access changed), and nothing fires when it does -- so the backstop is the
# ff-dispatch-failed CloudWatch alarm, which trips because GitHub answers an
# expired token with 403 and EventBridge does not retry 403.
#
# Usage:
#   ./scripts/rotate_dispatch_token.sh                      # prompts
#   GH_DISPATCH_TOKEN=... ./scripts/rotate_dispatch_token.sh --no-prompt
#
# Minting the replacement:
#   GitHub > Settings > Developer settings > Personal access tokens >
#   Fine-grained tokens > Generate new token
#     Resource owner ....... rahulAgrBej
#     Repository access .... Only select repositories > this repo
#     Permissions .......... Actions: Read and write   (and nothing else --
#                            NOT Contents, so a leaked token cannot push code)

set -euo pipefail

REPO="${REPO:-rahulAgrBej/espn-fantasy-football-sandbox}"
CONNECTION="${CONNECTION:-ff-github-dispatch}"
BUS="${BUS:-ff-dispatch}"
WORKFLOW="${WORKFLOW:-health.yml}"

die() { printf '\n%s\n' "$*" >&2; exit 1; }

command -v aws >/dev/null || die "aws is not installed."
command -v gh  >/dev/null || die "gh is not installed -- see https://cli.github.com"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated -- run: gh auth login"

if [ "${1:-}" != "--no-prompt" ]; then
    printf 'New fine-grained PAT (input hidden): '; read -rs GH_DISPATCH_TOKEN; printf '\n'
fi

[ -n "${GH_DISPATCH_TOKEN:-}" ] || die "GH_DISPATCH_TOKEN is empty."

# Check the token against GitHub before handing it to AWS. A token that is
# valid but under-scoped fails identically to an expired one once it is behind
# EventBridge -- a 403 in a DLQ message -- so find out here instead, where the
# response body actually says what is wrong.
echo
echo "Checking the token against $REPO ..."
code="$(curl -sS -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $GH_DISPATCH_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        "https://api.github.com/repos/$REPO/actions/workflows")"

case "$code" in
    200) echo "  token can read Actions on $REPO." ;;
    401) die "401 -- the token is invalid or already expired." ;;
    403) die "403 -- the token is valid but lacks Actions access to $REPO.
Check: Repository access includes this repo, and Permissions > Actions is
set to 'Read and write'." ;;
    404) die "404 -- the token cannot see $REPO at all. Its repository access
probably does not include this repo." ;;
    *)   die "Unexpected HTTP $code from GitHub." ;;
esac

# Never interpolate the token into the command line: argv is visible to every
# process on the box via `ps`. Build the request in a 0600 temp file instead
# and remove it on any exit, including a failure partway through.
echo
echo "Updating EventBridge connection $CONNECTION ..."
payload="$(mktemp)"
chmod 600 "$payload"
trap 'rm -f "$payload"' EXIT

CONNECTION="$CONNECTION" GH_DISPATCH_TOKEN="$GH_DISPATCH_TOKEN" python3 -c '
import json, os, sys
json.dump({
    "Name": os.environ["CONNECTION"],
    "AuthorizationType": "API_KEY",
    "AuthParameters": {
        "ApiKeyAuthParameters": {
            "ApiKeyName": "Authorization",
            "ApiKeyValue": "Bearer " + os.environ["GH_DISPATCH_TOKEN"].strip(),
        }
    },
}, sys.stdout)
' > "$payload"

aws events update-connection --cli-input-json "file://$payload" >/dev/null
rm -f "$payload"

# The connection goes AUTHORIZING while EventBridge rewrites the secret, and
# an invocation during that window fails. Wait it out rather than racing it.
echo "  waiting for the connection to leave AUTHORIZING ..."
for _ in $(seq 1 30); do
    state="$(aws events describe-connection --name "$CONNECTION" \
             --query ConnectionState --output text)"
    [ "$state" = "AUTHORIZED" ] && break
    sleep 2
done
[ "${state:-}" = "AUTHORIZED" ] || die "Connection stuck in state '${state:-unknown}'."
echo "  $CONNECTION is AUTHORIZED."

# Validate through the whole path -- bus, rule, transformer, destination,
# auth -- rather than trusting that a token good against api.github.com is
# also good once EventBridge is holding it. health.yml is the safe probe: no
# AWS role, no S3, no credits.
echo
echo "Dispatching $WORKFLOW through the $BUS bus ..."
before="$(gh run list --workflow="$WORKFLOW" --limit 1 --repo "$REPO" \
          --json databaseId --jq '.[0].databaseId // 0')"

aws events put-events --entries "$(printf '[{"EventBusName":"%s","Source":"ff.scheduler","DetailType":"dispatch.health","Detail":"{}"}]' "$BUS")" \
    --query 'FailedEntryCount' --output text | grep -qx 0 \
    || die "put-events was rejected -- the bus did not accept the event."

# EventBridge delivers in about a second, but GitHub needs a moment before the
# run is queryable.
run_id=""
for _ in $(seq 1 20); do
    sleep 3
    run_id="$(gh run list --workflow="$WORKFLOW" --limit 1 --repo "$REPO" \
              --json databaseId --jq '.[0].databaseId // 0')"
    [ "$run_id" != "$before" ] && [ "$run_id" != "0" ] && break
    run_id=""
done

if [ -z "$run_id" ]; then
    echo
    echo "No new $WORKFLOW run appeared within 60s." >&2
    echo "The token was accepted by GitHub directly, so the failure is in the" >&2
    echo "AWS path. Check the dead-letter queue for the rejected event:" >&2
    echo "  aws sqs receive-message --queue-url \$(aws cloudformation describe-stacks \\" >&2
    echo "    --stack-name ff-scheduler --query \\" >&2
    echo "    'Stacks[0].Outputs[?OutputKey==\`DlqUrl\`].OutputValue' --output text)" >&2
    exit 1
fi

echo "  run $run_id dispatched -- waiting ..."
if gh run watch "$run_id" --repo "$REPO" --interval 10 --exit-status >/dev/null 2>&1; then
    echo
    echo "Token rotated. AWS can dispatch $REPO workflows."
    echo
    echo "Record the new expiry date in docs/aws-scheduling.md."
else
    echo
    echo "The dispatch worked but $WORKFLOW itself failed -- run $run_id." >&2
    echo "That is not the token: AWS reached GitHub and GitHub started the run." >&2
    echo "Most likely the ESPN cookies have expired." >&2
    echo "  gh run view $run_id --log-failed --repo $REPO" >&2
    exit 1
fi
