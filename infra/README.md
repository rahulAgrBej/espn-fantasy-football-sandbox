# infra

Templates for the one-time AWS bootstrap. The rendered `*.json` files carry
the real account and repository ids and are gitignored; only the
`*.json.example` placeholders are tracked (`.githooks/pre-commit` enforces
this).

Two different things live here, managed two different ways:

| File | What | How it is applied |
|---|---|---|
| `trust-policy.json.example`, `s3-policy.json.example`, `s3-lifecycle.json.example` | The OIDC role that GitHub Actions assumes, and the bucket it reaches | `sed` the placeholders, then `aws iam` / `aws s3api` by hand |
| `scheduler.yaml` | The EventBridge schedules that dispatch the workflows | `aws cloudformation deploy` |

`scheduler.yaml` is tracked as-is rather than as a `.example`: it carries no
account id, only parameters supplied at deploy time, so the `infra/*.json`
gitignore rule does not apply to it.

## The subject claim is not the format the docs show

`trust-policy.json.example` matches a `sub` of:

```
repo:<OWNER>@<OWNER_ID>/<REPO>@<REPO_ID>:environment:gh_env
```

not the widely documented `repo:<OWNER>/<REPO>:environment:<name>`. This
repository is issued **immutable subject claims**, where GitHub embeds the
numeric owner and repository ids alongside the names. A policy written in
the plain-name form never matches, and the failure reads as
`Not authorized to perform sts:AssumeRoleWithWebIdentity` — which looks
like a missing permission rather than a failed condition.

This is the better of the two forms to pin to: names can be renamed,
transferred, deleted and re-registered, and ids cannot. A policy matching
on names alone would survive the repo being deleted and its name claimed by
someone else; this one would not.

**Do not guess these ids.** Read them from a real token rather than from
the API, so that what the policy matches is what GitHub actually sends:

```yaml
# Temporary workflow, deleted once the values are recorded.
- run: |
    tok=$(curl -sH "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
          "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=sts.amazonaws.com" | jq -r .value)
    payload=$(echo "$tok" | cut -d. -f2)
    pad=$(( (4 - ${#payload} % 4) % 4 )); payload="$payload$(printf '=%.0s' $(seq 1 $pad))"
    echo "$payload" | tr '_-' '/+' | base64 -d | jq '{sub, aud}'
```

Print only `sub`/`aud`. They are identifiers, not secrets; the token is a
credential and must never be echoed.

## Rendering and applying

```bash
export AWS_PROFILE=default
ACCOUNT=<ACCOUNT_ID> BUCKET=espn-ff-data-2026
OWNER=<OWNER> OWNER_ID=<OWNER_ID> REPO=<REPO> REPO_ID=<REPO_ID>

sed -e "s/<ACCOUNT_ID>/$ACCOUNT/g" -e "s/<OWNER_ID>/$OWNER_ID/g" \
    -e "s/<REPO_ID>/$REPO_ID/g"   -e "s/<OWNER>/$OWNER/g" \
    -e "s/<REPO>/$REPO/g" infra/trust-policy.json.example > infra/trust-policy.json
sed "s/<BUCKET>/$BUCKET/g" infra/s3-policy.json.example > infra/s3-policy.json
cp infra/s3-lifecycle.json.example infra/s3-lifecycle.json

aws iam update-assume-role-policy --role-name espn-ff-github-actions \
  --policy-document file://infra/trust-policy.json
```

## The scheduler stack

`scheduler.yaml` holds everything that fires the workflows: 18 EventBridge
schedules, the `ff-dispatch` bus, one rule per workflow, the API destination
and connection that reach GitHub, a dead-letter queue and an alarm. It creates
its own two IAM roles, so `CAPABILITY_NAMED_IAM` is required.

```bash
aws cloudformation deploy \
  --template-file infra/scheduler.yaml \
  --stack-name ff-scheduler \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubToken="$GH_DISPATCH_TOKEN" AlarmEmail=you@example.com
```

That command is the **bootstrap** form, for standing the stack up the first
time. It is actively dangerous for an incremental change (adding a schedule,
retiming one, widening a workflow's `options:`): `GitHubToken` and
`AlarmEmail` have no default, so `aws cloudformation deploy` without
`--parameter-overrides` retains whatever the stack already has for them, but
passing a stale `$GH_DISPATCH_TOKEN` here **updates the
`ff-github-dispatch` connection and breaks every schedule, not just the one
you meant to touch** — `scripts/rotate_dispatch_token.sh` owns that
connection outside CloudFormation by design. For an incremental change, omit
both parameters and preview first:

```bash
aws cloudformation deploy \
  --template-file infra/scheduler.yaml \
  --stack-name ff-scheduler \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-execute-changeset
# then aws cloudformation describe-change-set / execute-change-set --
# see docs/aws-scheduling.md
```

Every schedule defaults to `DISABLED`; the `*ScheduleState` parameters enable
them one workflow at a time. **This stack does not touch the OIDC trust policy
above** — that pins `sub` on the environment name, not the triggering event, so
a dispatched run authenticates to S3 exactly as a scheduled one did.

Rotate the dispatch token with `scripts/rotate_dispatch_token.sh`, not by
redeploying — see `docs/aws-scheduling.md` for why, and for the whole trigger
path. `docs/automation.md` has the rest of the setup and the runbook.
