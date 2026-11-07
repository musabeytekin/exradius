#!/usr/bin/env bash
# Local check of an Exradius project config, the way the admin panel's Verify runs it:
# validate exradius.yaml, build the images, start the services, run the setup commands in
# the workstation under the run's conditions (uid 1000 named runner with home /home/runner,
# read-only root filesystem, no capabilities, no-new-privileges, resources.memory as the
# memory limit, workstation.cache mounted from .exradius-cache/ next to exradius.yaml),
# then clean up everything but the cache.
# The container flags mirror backend/exradius/worker/containers.py in the Exradius
# repository; exradius_check.py holds them.
#
#   check.sh [options] [config dir]        check.sh --help for the options
#
# Needs the Docker CLI with a running daemon, git, and Python 3.8+ (standard library only).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
    python="$candidate"
    break
  fi
done
if [ -z "$python" ]; then
  echo "check.sh: needs Python 3.8 or later on PATH (python3)" >&2
  exit 2
fi
if ! command -v git >/dev/null 2>&1; then
  echo "check.sh: needs git on PATH" >&2
  exit 2
fi
validate_only=0
for arg in "$@"; do
  case "$arg" in --validate|-h|--help) validate_only=1 ;; esac
done
if [ "$validate_only" = 0 ] && ! command -v docker >/dev/null 2>&1; then
  echo "check.sh: needs the Docker CLI on PATH (and a running Docker daemon)" >&2
  exit 2
fi

exec "$python" -I "$here/exradius_check.py" "$@"
