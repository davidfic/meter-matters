#!/usr/bin/env bash
# Pipeline step: a throwaway Postgres for test_postgres.py to run against.
#
# It sits on the build's own Docker network, keeps its data in memory, and is
# gone when the build machine is. The password is not a secret - nothing
# outside the build can reach the container, and there is nothing in it.
set -euo pipefail

: "${PG_NAME:=elec-ci-postgres}"
: "${PG_NETWORK:=cloudbuild}"      # the network Cloud Build runs every step on
: "${PG_IMAGE:=postgres:16}"
: "${PG_PASSWORD:=ci-only-not-a-secret}"
: "${PG_WAIT:=90}"                 # seconds

docker rm --force --volumes "$PG_NAME" >/dev/null 2>&1 || true
docker run --detach --name "$PG_NAME" --network "$PG_NETWORK" \
    --env POSTGRES_PASSWORD="$PG_PASSWORD" \
    --tmpfs /var/lib/postgresql/data \
    "$PG_IMAGE" >/dev/null

# Asked over TCP, not the socket: while the image sets itself up it runs a
# temporary server that answers on the socket only, then restarts. Ready on
# TCP means ready for the tests.
for _ in $(seq 1 "$PG_WAIT"); do
  if docker exec "$PG_NAME" pg_isready --host 127.0.0.1 --username postgres --quiet; then
    echo "postgres is ready at $PG_NAME:5432"
    exit 0
  fi
  sleep 1
done

echo "postgres did not come up within ${PG_WAIT}s:" >&2
docker logs --tail 50 "$PG_NAME" >&2 || true
exit 1
