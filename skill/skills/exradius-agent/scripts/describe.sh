#!/usr/bin/env bash
# The exradius-info skill's description of a config, under this skill's own path.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$here/../../exradius-info/scripts/describe.sh" "$@"
