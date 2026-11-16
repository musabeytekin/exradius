#!/usr/bin/env bash
# An Exradius project config as Exradius reads it: agents (model, provider, git author,
# tools, the role's opening), connections, repositories, the workstation, services,
# variable names, the agent's hosts and the secret keys, with defaults filled in and
# "not set in the file" where the install decides. Reads the file and git; changes nothing;
# never prints a value.
#
#   describe.sh [--agent SLUG] [config dir]
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
  echo "describe.sh: needs Python 3.8 or later on PATH (python3)" >&2
  exit 2
fi
if ! command -v git >/dev/null 2>&1; then
  echo "describe.sh: needs git on PATH" >&2
  exit 2
fi

exec "$python" -I "$here/exradius_describe.py" "$@"
