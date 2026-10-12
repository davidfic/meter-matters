#!/usr/bin/env bash
# Rehearse the pipeline on this machine. Nothing is uploaded, pushed or deployed.
#
#   ./deploy/ci-local.sh                 the files deploy.sh would upload
#   ./deploy/ci-local.sh --git           the files git would commit
#   ./deploy/ci-local.sh --files-from F  the files listed in F, one per line
#   ./deploy/ci-local.sh --no-postgres   skip the database (the Postgres tests skip)
#
# It copies those files - and only those, so none of your own bills - into a
# scratch Docker volume, then runs the pipeline's own steps from deploy/ci/ in
# the same images Cloud Build uses: a throwaway Postgres, the test suite, the
# image build and the check of what the image holds. Everything it creates is
# removed again when it finishes, however it finishes.
#
# What it cannot rehearse is the part that needs Google: the push to Artifact
# Registry and the deploy to Cloud Run.
set -euo pipefail
cd "$(dirname "$0")/.."

mode=upload list="" postgres=1
while [ $# -gt 0 ]; do
  case "$1" in
    --git)         mode=git ;;
    --files-from)  mode=list; list="${2:?--files-from needs a file}"; shift ;;
    --no-postgres) postgres=0 ;;
    -h|--help)     sed -n '2,/^[^#]/{/^#/p;}' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

BUILDER=gcr.io/cloud-builders/docker
PYTHON="$(sed -n 's/^FROM[[:space:]]\{1,\}\([^[:space:]]\{1,\}\).*/\1/p' Dockerfile | head -1)"
: "${PYTHON:?could not read the base image from the Dockerfile}"
NETWORK=cloudbuild                  # the name Cloud Build gives its own
PG_NAME=elec-ci-postgres
PG_PASSWORD=ci-only-not-a-secret
volume="elec-ci-local-$$"
image="elec-bill-ci-local"
tag="rehearsal-$$"
files="$(mktemp)"
made_network=0

cleanup() {
  status=$?
  trap - EXIT INT TERM
  echo
  echo "--- cleaning up"
  docker rm --force --volumes "$PG_NAME" >/dev/null 2>&1 || true
  docker image rm --force "$image:$tag" >/dev/null 2>&1 || true
  docker volume rm --force "$volume" >/dev/null 2>&1 || true
  [ "$made_network" = 1 ] && docker network rm "$NETWORK" >/dev/null 2>&1 || true
  rm -f "$files"
  if [ "$status" = 0 ]; then
    echo "REHEARSAL PASSED"
  else
    echo "REHEARSAL FAILED (exit $status)"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "--- choosing files ($mode)"
case "$mode" in
  upload) gcloud meta list-files-for-upload . 2>/dev/null > "$files" ;;
  git)    git ls-files --cached --others --exclude-standard > "$files" ;;
  list)   grep -v '^[[:space:]]*$' "$list" > "$files" ;;
esac
# Deleted-but-still-tracked files and the like: only what is really there.
present="$(mktemp)"
while IFS= read -r path; do [ -f "$path" ] && printf '%s\n' "$path"; done < "$files" > "$present"
mv "$present" "$files"
[ -s "$files" ] || { echo "no files to test" >&2; exit 1; }
echo "$(wc -l < "$files") files"

step() {   # step <name> <image> [docker run options ...] -- <command ...>
  local name="$1" from="$2"; shift 2
  local options=()
  while [ "$1" != "--" ]; do options+=("$1"); shift; done
  shift
  echo
  echo "--- step: $name ($from)"
  docker run --rm --network "$NETWORK" --volume "$volume:/workspace" --workdir /workspace \
      "${options[@]}" "$from" "$@"
}
socket=(--volume /var/run/docker.sock:/var/run/docker.sock)

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  docker network create "$NETWORK" >/dev/null
  made_network=1
fi
docker volume create "$volume" >/dev/null
tar --create --files-from "$files" \
  | docker run --rm --interactive --volume "$volume:/workspace" "$PYTHON" \
      tar --extract --directory /workspace

database=()
if [ "$postgres" = 1 ]; then
  step postgres "$BUILDER" "${socket[@]}" --entrypoint bash \
      --env PG_PASSWORD="$PG_PASSWORD" --env PG_NAME="$PG_NAME" -- deploy/ci/postgres.sh
  database=(--env "ELEC_TEST_DATABASE_URL=postgresql://postgres:$PG_PASSWORD@$PG_NAME:5432/postgres")
fi

step test "$PYTHON" "${database[@]}" --entrypoint bash -- deploy/ci/test.sh

step build "$BUILDER" "${socket[@]}" --entrypoint bash \
    --env IMAGE="$image" --env TAG="$tag" -- deploy/ci/image.sh build
