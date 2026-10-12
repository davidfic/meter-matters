#!/usr/bin/env bash
# One-time setup for the build pipeline. Every command that creates or changes
# anything in Google Cloud for the pipeline is in this file, and nowhere else.
#
#   ./deploy/ci-setup.sh            the registry, the bucket, the build account
#                                   and its permissions - enough for deploy.sh
#   ./deploy/ci-setup.sh trigger    connect GitHub and create the push trigger
#   ./deploy/ci-setup.sh status     what exists so far; changes nothing
#   ./deploy/ci-setup.sh cloud-logging
#                                   only if you move build logs out of the
#                                   bucket - see deploy/README.md
#
# Safe to run again at any time: each step looks before it creates, and
# granting a permission that is already granted does nothing.
#
# Names and places come from deploy/ci.conf. The Cloud Run service itself, its
# secrets and its settings are not touched here - those are deploy/cloudrun.sh's.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=deploy/ci.conf
. deploy/ci.conf

g() { gcloud --project "$PROJECT" "$@"; }
say() { printf '\n== %s\n' "$*"; }
exists() { "$@" >/dev/null 2>&1; }

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

member="serviceAccount:$BUILD_ACCOUNT_EMAIL"
repo_link="${GITHUB_REPO##*/}"
repo_link="${repo_link//[^A-Za-z0-9-]/-}"

# A service account that was created a moment ago is not always visible to the
# next command. Try a grant a few times before believing the failure.
grant() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if "$@" >/dev/null; then return 0; fi
    [ "$attempt" = 6 ] || { echo "   not yet - trying again in 5s" >&2; sleep 5; }
  done
  echo "could not grant: $*" >&2
  return 1
}

runtime_account() {
  g run services describe "$SERVICE" --region "$REGION" \
      --format='value(spec.template.spec.serviceAccountName)'
}

cmd_base() {
  say "Project $PROJECT, region $REGION"
  if ! exists g run services describe "$SERVICE" --region "$REGION"; then
    echo "There is no Cloud Run service '$SERVICE' in $REGION. The pipeline deploys to" >&2
    echo "one that exists - create it first with deploy/cloudrun.sh." >&2
    exit 1
  fi

  say "APIs"
  g services enable cloudbuild.googleapis.com artifactregistry.googleapis.com \
      run.googleapis.com iam.googleapis.com storage.googleapis.com

  say "Artifact Registry repository: $REPOSITORY"
  if exists g artifacts repositories describe "$REPOSITORY" --location "$REGION"; then
    echo "   already there"
  else
    g artifacts repositories create "$REPOSITORY" --repository-format=docker \
        --location "$REGION" --description "Images built by the $SERVICE pipeline"
  fi

  # Rolling back needs the old image, so the newest ones are always kept.
  # Only what is both older than 30 days and not among those is removed.
  local policy="$scratch/cleanup-policy.json"
  cat > "$policy" <<EOF
[
  {"name": "keep-newest", "action": {"type": "Keep"},
   "mostRecentVersions": {"keepCount": $KEEP_IMAGES}},
  {"name": "delete-old", "action": {"type": "Delete"},
   "condition": {"tagState": "ANY", "olderThan": "30d"}}
]
EOF
  if ! g artifacts repositories set-cleanup-policies "$REPOSITORY" --location "$REGION" \
      --policy "$policy" --no-dry-run >/dev/null; then
    echo "   could not set the cleanup policy. Nothing depends on it; old images will" >&2
    echo "   simply accumulate until it is set." >&2
  fi

  say "Bucket for build logs and uploaded source: gs://$BUCKET"
  if exists g storage buckets describe "gs://$BUCKET"; then
    echo "   already there"
  else
    g storage buckets create "gs://$BUCKET" --location "$REGION" \
        --uniform-bucket-level-access --public-access-prevention
  fi
  local lifecycle="$scratch/lifecycle.json"
  printf '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": %s}}]}\n' \
      "$BUCKET_KEEP_DAYS" > "$lifecycle"
  g storage buckets update "gs://$BUCKET" --lifecycle-file "$lifecycle" >/dev/null

  say "Build account: $BUILD_ACCOUNT_EMAIL"
  if exists g iam service-accounts describe "$BUILD_ACCOUNT_EMAIL"; then
    echo "   already there"
  else
    g iam service-accounts create "$BUILD_ACCOUNT" \
        --display-name "Builds and deploys $SERVICE" \
        --description "Used by Cloud Build only. Set up by deploy/ci-setup.sh."
  fi

  say "What the build account may do - each on one resource, none on the project"
  echo "   write logs to, and read source from, gs://$BUCKET"
  grant g storage buckets add-iam-policy-binding "gs://$BUCKET" \
      --member "$member" --role roles/storage.admin
  echo "   push images to $REPOSITORY"
  grant g artifacts repositories add-iam-policy-binding "$REPOSITORY" --location "$REGION" \
      --member "$member" --role roles/artifactregistry.writer
  echo "   deploy revisions of $SERVICE"
  grant g run services add-iam-policy-binding "$SERVICE" --region "$REGION" \
      --member "$member" --role roles/run.developer
  local runtime
  runtime="$(runtime_account)"
  echo "   deploy them to run as $runtime"
  grant g iam service-accounts add-iam-policy-binding "$runtime" \
      --member "$member" --role roles/iam.serviceAccountUser

  say "Done"
  echo "Deploy from this directory, today:   ./deploy/deploy.sh"
  echo "Deploy on every push:                 see 'The git path' in deploy/README.md"
}

cmd_trigger() {
  if ! exists g iam service-accounts describe "$BUILD_ACCOUNT_EMAIL"; then
    echo "Run ./deploy/ci-setup.sh first - the build account doesn't exist yet." >&2
    exit 1
  fi

  say "APIs"
  g services enable cloudbuild.googleapis.com secretmanager.googleapis.com

  # Cloud Build keeps the GitHub token it is given in Secret Manager, and its
  # own service agent is what stores and reads it. This is Google's agent, not
  # the build account: nothing in a build can act as it.
  say "Letting Cloud Build keep its GitHub token in Secret Manager"
  local number agent
  number="$(g projects describe "$PROJECT" --format='value(projectNumber)')"
  agent="service-${number}@gcp-sa-cloudbuild.iam.gserviceaccount.com"
  grant g projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$agent" \
      --role roles/secretmanager.admin --condition=None

  say "Connection to GitHub: $CONNECTION"
  if ! exists g builds connections describe "$CONNECTION" --region "$REGION"; then
    g builds connections create github "$CONNECTION" --region "$REGION"
  fi
  local stage
  stage="$(g builds connections describe "$CONNECTION" --region "$REGION" \
      --format='value(installationState.stage)')"
  if [ "$stage" != "COMPLETE" ]; then
    echo
    echo "GitHub has to be told to allow this, in a browser. Cloud Build says:"
    g builds connections describe "$CONNECTION" --region "$REGION" \
        --format='value(installationState.message)' | sed 's/^/   /'
    echo
    echo "Open this link, signed in to GitHub as the owner of $GITHUB_REPO:"
    g builds connections describe "$CONNECTION" --region "$REGION" \
        --format='value(installationState.actionUri)' | sed 's/^/   /'
    echo
    echo "When it asks where to install, choose 'Only select repositories' and pick"
    echo "${GITHUB_REPO##*/}. Then run this again:   ./deploy/ci-setup.sh trigger"
    exit 3
  fi
  echo "   connected"

  say "Repository: $GITHUB_REPO"
  if exists g builds repositories describe "$repo_link" \
      --connection "$CONNECTION" --region "$REGION"; then
    echo "   already linked"
  else
    g builds repositories create "$repo_link" --connection "$CONNECTION" --region "$REGION" \
        --remote-uri "https://github.com/${GITHUB_REPO}.git"
  fi

  say "Trigger: $TRIGGER (a push to $BRANCH)"
  if exists g builds triggers describe "$TRIGGER" --region "$REGION"; then
    echo "   already there"
  else
    g builds triggers create github --name "$TRIGGER" --region "$REGION" \
        --repository "projects/$PROJECT/locations/$REGION/connections/$CONNECTION/repositories/$repo_link" \
        --branch-pattern "^${BRANCH}\$" \
        --build-config cloudbuild.yaml \
        --service-account "projects/$PROJECT/serviceAccounts/$BUILD_ACCOUNT_EMAIL" \
        --description "Test, build and deploy $SERVICE on every push to $BRANCH"
  fi

  say "Done"
  echo "From now on:   git push origin $BRANCH"
  echo "Or, without pushing anything new:"
  echo "   gcloud --project $PROJECT builds triggers run $TRIGGER --region $REGION --branch $BRANCH"
}

cmd_status() {
  local runtime
  show() {   # show <label> <command ...>
    local label="$1"; shift
    if exists "$@"; then printf '   %-44s yes\n' "$label"; else printf '   %-44s no\n' "$label"; fi
  }
  say "Project $PROJECT, region $REGION"
  show "Cloud Run service $SERVICE" g run services describe "$SERVICE" --region "$REGION"
  show "registry $REPOSITORY" g artifacts repositories describe "$REPOSITORY" --location "$REGION"
  show "bucket gs://$BUCKET" g storage buckets describe "gs://$BUCKET"
  show "build account $BUILD_ACCOUNT" g iam service-accounts describe "$BUILD_ACCOUNT_EMAIL"
  show "GitHub connection $CONNECTION" g builds connections describe "$CONNECTION" --region "$REGION"
  show "repository link $repo_link" g builds repositories describe "$repo_link" \
      --connection "$CONNECTION" --region "$REGION"
  show "trigger $TRIGGER" g builds triggers describe "$TRIGGER" --region "$REGION"
  if runtime="$(runtime_account 2>/dev/null)" && [ -n "$runtime" ]; then
    echo "   the service runs as $runtime"
  fi
}

cmd_cloud_logging() {
  # Not part of the normal setup. Build logs go to the bucket, which needs no
  # role on the project. Cloud Logging is the alternative, and writing to it
  # is a project-wide role - the only one the build account would hold.
  say "Letting the build account write to Cloud Logging"
  grant g projects add-iam-policy-binding "$PROJECT" --member "$member" \
      --role roles/logging.logWriter --condition=None
  echo "Now, in cloudbuild.yaml, remove the logsBucket line and set"
  echo "   options: { logging: CLOUD_LOGGING_ONLY }"
}

case "${1:-base}" in
  base)          cmd_base ;;
  trigger)       cmd_trigger ;;
  status)        cmd_status ;;
  cloud-logging) cmd_cloud_logging ;;
  *) sed -n '2,/^[^#]/{/^#/p;}' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
