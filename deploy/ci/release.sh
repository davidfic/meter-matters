#!/usr/bin/env bash
# Pipeline step: put the new image on the service that is already running.
#
# Only the image changes. No environment variable, secret, memory, CPU,
# scaling or access flag is passed, and Cloud Run carries every setting a
# command does not mention over to the new revision - so what
# deploy/cloudrun.sh configured stays exactly as it left it.
set -euo pipefail

: "${PROJECT:?}" "${REGION:?}" "${SERVICE:?}"
: "${IMAGE:?set IMAGE to the repository path, without a tag}"
tag="${TAG:-${COMMIT_SHA:-}}"
[ -n "$tag" ] || { echo "no tag: neither TAG nor COMMIT_SHA is set" >&2; exit 1; }

g() { gcloud --project "$PROJECT" "$@"; }

# `run deploy` would create a service that doesn't exist - one with no
# database and no secrets. This pipeline only ever updates.
if ! g run services describe "$SERVICE" --region "$REGION" \
    --format='value(metadata.name)' >/dev/null; then
  echo "there is no service '$SERVICE' in $REGION to deploy to (or this account" >&2
  echo "may not see it). Create it with deploy/cloudrun.sh, then run this again." >&2
  exit 1
fi

g run deploy "$SERVICE" --region "$REGION" --image "$IMAGE:$tag" --quiet

# After a rollback, traffic is pinned to the revision that was rolled back to,
# and a new revision would be created but never served. A deploy that passed
# its tests is meant to be live, so say so.
g run services update-traffic "$SERVICE" --region "$REGION" --to-latest --quiet

url="$(g run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
revision="$(g run services describe "$SERVICE" --region "$REGION" \
    --format='value(status.latestReadyRevisionName)')"

# /health, which asks the app whether it can reach its database. Not
# /healthz: Cloud Run keeps that path for itself and answers 404 before the
# app is asked.
# curl, not python3: the cloud-sdk:slim image this step runs in has no Python.
health="$url/health"
last="no answer"
for _ in 1 2 3 4 5 6; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$health")" || code=""
    if [ "$code" = 200 ]; then
        echo "$health answered 200"
        last=""
        break
    fi
    last="${code:+HTTP $code}"
    last="${last:-no answer}"
    sleep 5
done
if [ -n "$last" ]; then
    echo "$health is not answering: $last" >&2
    echo "The new revision is serving. To go back, see 'Rolling back' in deploy/README.md." >&2
    exit 1
fi

echo "deployed $IMAGE:$tag as $revision"
echo "$url"
