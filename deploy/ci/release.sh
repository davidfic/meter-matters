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
python3 - "$url/health" <<'EOF'
import sys
import time
import urllib.error
import urllib.request

url, last = sys.argv[1], "no answer"
for attempt in range(6):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            if response.status == 200:
                print(f"{url} answered 200")
                sys.exit(0)
            last = f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        last = f"HTTP {exc.code}"
    except OSError as exc:
        last = str(exc)
    time.sleep(5)
print(f"{url} is not answering: {last}", file=sys.stderr)
print("The new revision is serving. To go back, see 'Rolling back' in deploy/README.md.",
      file=sys.stderr)
sys.exit(1)
EOF

echo "deployed $IMAGE:$tag as $revision"
echo "$url"
