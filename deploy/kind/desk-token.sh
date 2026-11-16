#!/usr/bin/env bash
# An integration token for the bridge desk, created through the local API as the admin and
# stored in the Secret exradius-desk, then Desk restarted (bridges/desk/docs/design.md §14).
# Needs Tilt's port-forward of the api on localhost:8100. Run it again to replace the token.
set -euo pipefail

context=${CONTEXT:-kind-exradius}
api=${API:-http://localhost:8100}
kubectl=(kubectl --context "$context")
jar=$(mktemp)
trap 'rm -f "$jar"' EXIT

say() { printf '==> %s\n' "$*"; }

password=$("${kubectl[@]}" -n exradius get secret exradius-admin -o jsonpath='{.data.password}' | base64 -d)
say "signing in to $api as admin"
# The password goes on stdin, not on curl's command line.
PASSWORD="$password" python3 -c 'import json, os; print(json.dumps({"username": "admin", "password": os.environ["PASSWORD"]}))' |
  curl -sf -c "$jar" -H 'Content-Type: application/json' --data @- "$api/api/auth/login" >/dev/null

name="desk-local-$(date +%Y%m%d%H%M%S)"
say "integration token $name for the bridge desk"
token=$(curl -sf -b "$jar" -H 'Content-Type: application/json' \
  -d "{\"bridge\": \"desk\", \"name\": \"$name\"}" "$api/api/integration-tokens" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')

say "Secret exradius-desk, and Desk restarted"
"${kubectl[@]}" -n exradius create secret generic exradius-desk --from-literal=integration-token="$token" \
  --dry-run=client -o yaml | "${kubectl[@]}" apply -f - >/dev/null
"${kubectl[@]}" -n exradius rollout restart deploy/exradius-desk >/dev/null
"${kubectl[@]}" -n exradius rollout status deploy/exradius-desk --timeout=180s
say "ready: http://localhost:3200 (older desk-local tokens can be revoked in the panel)"
