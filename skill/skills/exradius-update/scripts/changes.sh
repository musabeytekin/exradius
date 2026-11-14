#!/usr/bin/env bash
# What changed in a project's code since its Exradius config was last compared with it:
# for each repository of exradius.yaml with a local checkout, the changes between the commit
# in check/reviewed.json (estimated from the config's last commit when the file is absent)
# and the head of the config's branch, limited to what can make the config stale and grouped
# by the part of the config it touches. --record writes check/reviewed.json. Reads git only;
# prints names and versions, never the content of an env file or a registry configuration.
#
#   changes.sh [options] [config dir]        changes.sh --help for the options
#
# Needs git and Python 3.8+ (standard library only). No Docker.
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
  echo "changes.sh: needs Python 3.8 or later on PATH (python3)" >&2
  exit 2
fi
if ! command -v git >/dev/null 2>&1; then
  echo "changes.sh: needs git on PATH" >&2
  exit 2
fi

exec "$python" -I "$here/exradius_changes.py" "$@"
