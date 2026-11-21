#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

case "${1:-}" in
  --basic)
    exec uv run --locked pytest -q --ignore=tests/test_journeys.py --ignore=tests/test_inline.py
    ;;
  "")
    exec uv run --locked pytest -q
    ;;
  *)
    printf 'Usage: %s [--basic]\n' "$0" >&2
    exit 2
    ;;
esac
