#!/usr/bin/env bash
# Pipeline steps: build the image, look inside it, and only then push it.
#
#     image.sh build    build IMAGE:<tag>, then refuse it if it holds anything
#                       but the app
#     image.sh push     push what `build` made
#
# IMAGE is the repository path without a tag. The tag is the commit being
# built (COMMIT_SHA, which a trigger provides) unless TAG says otherwise, as it
# does for a build submitted by hand.
set -euo pipefail
cd "$(dirname "$0")/../.."

: "${IMAGE:?set IMAGE to the repository path, without a tag}"
tag="${TAG:-${COMMIT_SHA:-}}"
if [ -z "$tag" ]; then
  echo "no tag: neither TAG nor COMMIT_SHA is set" >&2
  exit 1
fi
# What a Docker tag may be, so a stray character fails here and not at the push.
if ! [[ "$tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "not usable as an image tag: $tag" >&2
  exit 1
fi
image="$IMAGE:$tag"

build() {
  docker build --tag "$image" .

  # An allow-list, like .dockerignore and .gcloudignore: the image may hold
  # the app's own modules, its templates, its static files and its
  # requirements. Anything else - a test, a fixture, a bill, a .env - stops
  # the build here, before the image leaves this machine.
  local unexpected
  unexpected="$(docker run --rm --network none --entrypoint find "$image" \
      /app -mindepth 1 ! -type d \
      ! -path '/app/templates/*' \
      ! -path '/app/static/*' \
      ! -path '/app/requirements.txt' \
      ! \( -path '/app/*.py' ! -path '/app/*/*' \
           ! -name 'test_*.py' ! -name 'testing.py' ! -name 'portal.py' \) )"
  if [ -n "$unexpected" ]; then
    echo "refusing this image - it holds files that are not the app:" >&2
    echo "$unexpected" | sed 's/^/    /' >&2
    echo "Dockerfile and .dockerignore decide what goes in." >&2
    exit 1
  fi
  echo "built $image"
  docker run --rm --network none --entrypoint find "$image" /app -mindepth 1 ! -type d \
      | sort | sed 's/^/    /'
}

push() {
  docker push "$image"
  echo "pushed $image"
}

case "${1:-}" in
  build) build ;;
  push)  push ;;
  *) echo "usage: $0 build|push" >&2; exit 2 ;;
esac
