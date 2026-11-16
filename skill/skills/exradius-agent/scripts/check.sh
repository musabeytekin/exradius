#!/usr/bin/env bash
# The local check of the exradius-project skill, under this skill's own path.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$here/../../exradius-project/scripts/check.sh" "$@"
