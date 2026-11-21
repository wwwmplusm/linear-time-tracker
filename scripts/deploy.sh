#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

: "${VPS_SSH_HOST:?Set VPS_SSH_HOST to user@hostname}"
: "${VPS_HEALTH_URL:?Set VPS_HEALTH_URL to the public HTTPS origin}"
deploy_path=${VPS_DEPLOY_PATH:-/opt/linear-time-tracker}
service=${VPS_TRACKER_SERVICE:-linear-time-tracker}
if [[ ! "$deploy_path" =~ ^/[a-zA-Z0-9_./-]+$ ||
      ! "$service" =~ ^[a-zA-Z0-9_-]+$ ||
      ! "$VPS_HEALTH_URL" =~ ^https://[a-zA-Z0-9.-]+(:[0-9]+)?$ ]]; then
  printf 'Invalid deploy path, service name or HTTPS origin.\n' >&2
  exit 2
fi

ssh_options=(-o BatchMode=yes -o StrictHostKeyChecking=yes)
if [[ -n "${VPS_SSH_KEY_FILE:-}" ]]; then
  ssh_options+=(-o IdentitiesOnly=yes -i "$VPS_SSH_KEY_FILE")
fi

source_paths=(.dockerignore Dockerfile pyproject.toml uv.lock tracker)
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then
  remote_head=$(git ls-remote origin refs/heads/main)
  if [[ "$remote_head" != "$GITHUB_SHA"$'\trefs/heads/main' ]]; then
    printf 'Main advanced during tests. Skipping this deploy.\n'
    exit 0
  fi
  git archive --format=tar.gz "$GITHUB_SHA" -- "${source_paths[@]}" |
    ssh "${ssh_options[@]}" -- "$VPS_SSH_HOST" "$deploy_path/deploy-release $service $VPS_HEALTH_URL"
else
  ./scripts/test.sh --basic
  git ls-files -z --cached --others --exclude-standard --deduplicate -- "${source_paths[@]}" |
    tar --null -czf - -T - |
    ssh "${ssh_options[@]}" -- "$VPS_SSH_HOST" "$deploy_path/deploy-release $service $VPS_HEALTH_URL"
fi
