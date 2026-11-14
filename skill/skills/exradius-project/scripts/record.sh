#!/usr/bin/env bash
# Write check/reviewed.json next to exradius.yaml: the commit of each repository the config
# was checked against, which the exradius-update skill compares from next time.
#
#   record.sh [--repo NAME=PATH ...] [config dir]
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$here/../../exradius-update/scripts/changes.sh" --record "$@"
