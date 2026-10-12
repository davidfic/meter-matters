#!/usr/bin/env bash
# Deploy what is in this directory. One command, and no git hosting needed:
# the same pipeline a push would start, fed from here instead.
#
#   ./deploy/deploy.sh             test, build, push, deploy
#   ./deploy/deploy.sh --dry-run   show what would be sent, and send nothing
#
# What is sent is what .gcloudignore allows - the app, its tests, the invented
# fixtures, the pipeline's own steps - and that list is checked here a second
# time, against rules written in this file, before anything leaves. None of
# your bills, exports or secrets can be in it.
#
# Needs ./deploy/ci-setup.sh to have been run once.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=deploy/ci.conf
. deploy/ci.conf

dry_run=0
case "${1:-}" in
  "") ;;
  --dry-run) dry_run=1 ;;
  *) sed -n '2,/^[^#]/{/^#/p;}' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

g() { gcloud --project "$PROJECT" "$@"; }

# ---- what would be sent -----------------------------------------------------
files="$(gcloud meta list-files-for-upload . 2>/dev/null)"
if [ -z "$files" ]; then
  echo "gcloud listed nothing to upload - is .gcloudignore intact?" >&2
  exit 1
fi
# Everything sent must be one of these. It repeats .gcloudignore on purpose:
# a slip in that file then stops here instead of becoming an upload.
allowed='^(Dockerfile|\.dockerignore|requirements\.txt|[^/]+\.py|test-report\.json|templates/.+|static/.+|fixtures/synthetic/.+|deploy/ci/.+)$'
strangers="$(printf '%s\n' "$files" | grep -Ev "$allowed" || true)"
if [ -n "$strangers" ]; then
  echo "refusing to upload. These are not part of the app, its tests or the pipeline:" >&2
  printf '%s\n' "$strangers" | sed 's/^/    /' >&2
  echo "Fix .gcloudignore (it is an allow-list), or if one of these really should" >&2
  echo "travel, add it there and to 'allowed' in $0." >&2
  exit 1
fi
for needed in Dockerfile .dockerignore requirements.txt deploy/ci/test.sh; do
  printf '%s\n' "$files" | grep -qxF "$needed" || {
    echo "the upload would be missing $needed - is .gcloudignore intact?" >&2
    exit 1
  }
done

# ---- what to call the image --------------------------------------------------
# The commit, when there is one and it is exactly what is being sent. Anything
# else says so in its name, so a tag never claims to be a commit it is not.
stamp="$(date -u +%Y%m%d-%H%M%S)"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && git rev-parse HEAD >/dev/null 2>&1; then
  commit="$(git rev-parse HEAD)"
  if [ -z "$(git status --porcelain)" ]; then
    tag="$commit"
  else
    tag="${commit:0:12}-uncommitted-$stamp"
  fi
else
  tag="manual-$stamp"
fi

echo "project   $PROJECT ($REGION)"
echo "service   $SERVICE"
echo "image     $IMAGE_URI:$tag"
echo "sending   $(printf '%s\n' "$files" | wc -l) files"

if [ "$dry_run" = 1 ]; then
  printf '%s\n' "$files" | sort | sed 's/^/    /'
  echo "dry run: nothing was sent."
  exit 0
fi

if ! g iam service-accounts describe "$BUILD_ACCOUNT_EMAIL" >/dev/null 2>&1; then
  echo "The build account doesn't exist yet. Run ./deploy/ci-setup.sh once, then this." >&2
  exit 1
fi

# ---- send it -------------------------------------------------------------------
# Waits for the build and shows its log as it runs. A failed test, a refused
# image or a failed deploy ends it, and this command fails with it.
g builds submit . \
    --config cloudbuild.yaml \
    --region "$REGION" \
    --service-account "projects/$PROJECT/serviceAccounts/$BUILD_ACCOUNT_EMAIL" \
    --gcs-source-staging-dir "gs://$BUCKET/source" \
    --substitutions "_TAG=$tag,_REGION=$REGION,_SERVICE=$SERVICE,_REPOSITORY=$REPOSITORY,_IMAGE=$IMAGE"

echo
echo "Deployed. $(g run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
