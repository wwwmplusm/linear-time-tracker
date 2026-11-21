#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
service=${1:?Compose service required}
origin=${2:?Public HTTPS origin required}
exec 9>.deploy.lock
flock -w 300 9
stage=$(mktemp -d .release.XXXXXXXX)
trap 'rm -rf -- "$stage"' EXIT

python3 -c '
import sys
import tarfile
from pathlib import PurePosixPath

root_files = {".dockerignore", "Dockerfile", "pyproject.toml", "uv.lock"}
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
    for member in archive:
        fields = PurePosixPath(member.name).parts
        if not fields or ".." in fields or not (
            (len(fields) == 1 and fields[0] in root_files and member.isfile())
            or (fields[0] == "tracker" and (member.isdir() or member.isfile()))
        ):
            raise ValueError("Unexpected file in release archive")
        archive.extract(member, sys.argv[1], filter="data")
' "$stage"

image=$(docker compose config --images "$service")
container=$(docker compose ps -q "$service")
previous=
if [[ -n "$container" ]]; then
  previous=$(docker inspect --format '{{.Image}}' "$container")
fi

candidate="$image:candidate"
docker build --tag "$candidate" "$stage"
docker tag "$candidate" "$image"

activate() {
  docker compose up -d --no-deps --no-build --pull never --force-recreate "$service"
}
healthy() {
  local status
  status=$(curl --silent --show-error --retry 8 --retry-delay 2 --connect-timeout 5 \
    --max-time 10 --output /dev/null --write-out '%{http_code}' "$origin/healthz") &&
    [[ "$status" == 204 ]]
}

if ! activate || ! healthy; then
  printf 'Release failed its health check.\n' >&2
  if [[ -n "$previous" ]]; then
    docker tag "$previous" "$image"
    activate
    printf 'Previous image restored.\n' >&2
  fi
  exit 1
fi

rsync -a --delete -- "$stage/tracker/" tracker/
install -m 644 -- "$stage/.dockerignore" "$stage/Dockerfile" "$stage/pyproject.toml" "$stage/uv.lock" .
docker image rm "$candidate" >/dev/null
printf 'Healthy release at %s\n' "$origin"
