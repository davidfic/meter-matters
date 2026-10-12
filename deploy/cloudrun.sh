#!/usr/bin/env bash
# Deploy to Cloud Run, with Supabase as the database.
#
#   PROJECT=my-project REGION=us-east1 ./deploy/cloudrun.sh secrets   # once
#   PROJECT=my-project REGION=us-east1 ./deploy/cloudrun.sh deploy
#   PROJECT=my-project REGION=us-east1 ./deploy/cloudrun.sh schedule  # once, after deploy
#
# Secrets are typed at a hidden prompt and go straight to Secret Manager. They
# never appear in this file, the command line, or the shell history.
set -euo pipefail

: "${PROJECT:?set PROJECT to your Google Cloud project id}"
: "${REGION:=us-central1}"
: "${SERVICE:=elec-bill}"
# Cloud Scheduler is offered in us-central1; this stays settable for regions where it is not.
# It only makes an HTTPS request, so any nearby region does.
: "${SCHEDULER_REGION:=$REGION}"
cd "$(dirname "$0")/.."

g() { gcloud --project "$PROJECT" "$@"; }

# name -> what it is. Optional ones can be skipped by pressing Enter.
REQUIRED=(elec-database-url elec-secret-key elec-task-token)
OPTIONAL=(elec-token-key elec-google-client-secret elec-inbound-secret
          elec-mailgun-signing-key elec-smtp-pass)

has_secret() { g secrets describe "$1" >/dev/null 2>&1; }

put_secret() {
  local name="$1" value="$2"
  if has_secret "$name"; then
    printf '%s' "$value" | g secrets versions add "$name" --data-file=- >/dev/null
  else
    printf '%s' "$value" | g secrets create "$name" --replication-policy=automatic \
        --data-file=- >/dev/null
  fi
  echo "  stored $name"
}

cmd_secrets() {
  g services enable run.googleapis.com secretmanager.googleapis.com \
      cloudbuild.googleapis.com artifactregistry.googleapis.com cloudscheduler.googleapis.com

  echo "Supabase connection string - Project Settings > Database > Transaction pooler (port 6543):"
  echo "(typing is hidden - paste it and press Enter)"
  read -rs value; echo
  put_secret elec-database-url "$value"

  # Generated here rather than typed: nobody needs to know these.
  has_secret elec-secret-key || put_secret elec-secret-key \
      "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  has_secret elec-task-token || put_secret elec-task-token \
      "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

  for name in "${OPTIONAL[@]}"; do
    echo "$name (hidden; Enter to skip):"
    read -rs value; echo
    [ -n "$value" ] && put_secret "$name" "$value"
  done

  # The service runs as the default compute account unless you say otherwise.
  local account
  account="$(g projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
  for name in "${REQUIRED[@]}" "${OPTIONAL[@]}"; do
    has_secret "$name" && g secrets add-iam-policy-binding "$name" \
        --member="serviceAccount:$account" --role=roles/secretmanager.secretAccessor >/dev/null
  done
  echo "done. Next: $0 deploy"
}

run_tests() {
  # The tests, on a throwaway Postgres where Docker allows it: the app runs on
  # Postgres in production, and those tests are skipped without a database.
  # Never the real Supabase: they empty the tables they use. The container lives
  # in memory, on localhost only, and is removed whatever happens.
  local py=python3; [ -x .venv/bin/python ] && py=.venv/bin/python
  if ! { command -v docker >/dev/null && docker info >/dev/null 2>&1; }; then
    echo "(no Docker here: the Postgres tests will be skipped)"
    "$py" testreport.py --label "$(date -u +%Y-%m-%d-%H%M)" \
        || echo "(tests did not all pass - the report will say so)"
    return
  fi
  local name="elec-test-pg-$$" port
  docker run -d --rm --name "$name" -e POSTGRES_PASSWORD=test-only --tmpfs /var/lib/postgresql/data \
      -p 127.0.0.1::5432 postgres:16 >/dev/null
  trap 'docker rm -f "'"$name"'" >/dev/null 2>&1 || true' RETURN
  port="$(docker port "$name" 5432/tcp | head -1 | sed 's/.*://')"
  for _ in $(seq 1 60); do
    docker exec "$name" pg_isready --host 127.0.0.1 --username postgres --quiet 2>/dev/null && break
    sleep 1
  done
  ELEC_TEST_DATABASE_URL="postgresql://postgres:test-only@127.0.0.1:$port/postgres" \
      "$py" testreport.py --label "$(date -u +%Y-%m-%d-%H%M)" \
      || echo "(tests did not all pass - the report will say so)"
}

cmd_deploy() {
  # Run the tests here and ship the results with the code they describe. It is
  # a report, not a gate: a failing suite is deployed, and the /test-suite page
  # says so.
  run_tests
  local plain="ELEC_TRUSTED_PROXIES=1,WEB_CONCURRENCY=1"
  if [ -f deploy/cloudrun.env ]; then
    plain="$plain,$(grep -Ev '^\s*(#|$)' deploy/cloudrun.env | paste -sd, -)"
  fi

  local secrets="" name var
  for name in "${REQUIRED[@]}" "${OPTIONAL[@]}"; do
    has_secret "$name" || continue
    var="$(echo "$name" | tr 'a-z-' 'A-Z_')"
    secrets="${secrets:+$secrets,}$var=$name:latest"
  done

  g run deploy "$SERVICE" --source . --region "$REGION" \
      --allow-unauthenticated \
      --memory 512Mi --cpu 1 --concurrency 8 \
      --min-instances 0 --max-instances 3 \
      --set-env-vars "$plain" \
      --set-secrets "$secrets"

  local url
  url="$(g run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
  echo
  echo "Deployed: $url"
  if ! grep -qs '^ELEC_BASE_URL=' deploy/cloudrun.env; then
    echo "Now put  ELEC_BASE_URL=$url  in deploy/cloudrun.env and deploy once more -"
    echo "sign-in cookies and Google's redirect both depend on it."
  fi
}

cmd_schedule() {
  local url token
  url="$(g run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
  token="$(g secrets versions access latest --secret elec-task-token)"
  # Cloud Run only runs while serving a request, so the daily check is a
  # request: Cloud Scheduler makes it.
  g scheduler jobs delete "$SERVICE-check" --location "$SCHEDULER_REGION" --quiet 2>/dev/null || true
  g scheduler jobs create http "$SERVICE-check" --location "$SCHEDULER_REGION" \
      --schedule "0 9 * * *" --time-zone "America/New_York" \
      --uri "$url/api/tasks/check" --http-method POST \
      --headers "Authorization=Bearer $token" \
      --attempt-deadline 300s
  echo "Scheduled daily at 09:00. Run it now:"
  echo "  gcloud --project $PROJECT scheduler jobs run $SERVICE-check --location $SCHEDULER_REGION"
}

case "${1:-}" in
  secrets)  cmd_secrets ;;
  deploy)   cmd_deploy ;;
  schedule) cmd_schedule ;;
  *) sed -n '2,9p' "$0"; exit 1 ;;
esac
