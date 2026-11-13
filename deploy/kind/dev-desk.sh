#!/usr/bin/env bash
# Exradius Desk's dev identity provider in the local kind cluster
# (bridges/desk/docs/design.md §14). Idempotent.
#
# - Keycloak in dev mode in the namespace keycloak (deploy/kind/keycloak.yaml) with the realm
#   exradius: the client exradius-desk, the groups team-a and team-b, and the people ada, ben
#   (team-a) and cem (team-b), each with their name as password;
# - the Secret exradius-desk-oidc in exradius with the client's secret, which
#   charts/exradius/values-local.yaml names.
#
# Then `tilt up` (Desk is on in values-local.yaml) and, once the api answers on
# localhost:8100, `make -C deploy/kind desk-token`.
#
# LOCAL ONLY: dev mode, plain HTTP, fixed passwords and the client secret desk-local-secret.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
context=${CONTEXT:-kind-exradius}
kubectl=(kubectl --context "$context")

say() { printf '==> %s\n' "$*"; }

say "Keycloak in dev mode (namespace keycloak)"
"${kubectl[@]}" apply -f "$here/keycloak.yaml" >/dev/null
# A changed realm file is imported only by a new pod (--import-realm skips existing realms,
# and dev mode keeps its database in the pod).
"${kubectl[@]}" -n keycloak rollout restart deploy/keycloak >/dev/null
"${kubectl[@]}" -n keycloak rollout status deploy/keycloak --timeout=300s

say "Secret exradius-desk-oidc in exradius"
"${kubectl[@]}" apply -f "$here/namespace.yaml" >/dev/null
"${kubectl[@]}" -n exradius create secret generic exradius-desk-oidc \
  --from-literal=client-secret=desk-local-secret --dry-run=client -o yaml | "${kubectl[@]}" apply -f - >/dev/null

cat <<'TEXT'

ready. Keycloak: http://localhost:8180 (Tilt forwards it; admin admin/admin)
People: ada/ada and ben/ben in team-a, cem/cem in team-b.
A project shows in Desk with bridges/desk.yaml in its config repository:  groups: [team-a]
Next: tilt up, then make -C deploy/kind desk-token
TEXT
