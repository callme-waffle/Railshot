#!/bin/sh
set -eu
case "${1:-}" in
  *Username*) printf '%s\n' x-access-token ;;
  *Password*) printf '%s\n' "${GITHUB_TOKEN:?Git credentials are not configured}" ;;
  *) exit 1 ;;
esac
