# infra

Templates for the one-time AWS bootstrap. The rendered `*.json` files carry
the real account and repository ids and are gitignored; only the
`*.json.example` placeholders are tracked (`.githooks/pre-commit` enforces
this).

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

See `docs/automation.md` for the rest of the setup and the runbook.
