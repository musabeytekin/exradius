#!/usr/bin/env bash
# HashiCorp Vault for stores of kind vault in the local kind cluster (docs/kubernetes.md §17,
# docs/config-repo.md §7.4). Idempotent; run it again after the Vault pod restarts.
#
# - Vault in dev mode in the namespace vault (deploy/kind/vault.yaml);
# - the Kubernetes auth method against the cluster's own API, reviewing tokens with Vault's
#   ServiceAccount (system:auth-delegator);
# - the policy exradius: read secret/data/exradius/* and secret/subkeys/exradius/*;
# - the role exradius, bound to the ServiceAccounts exradius-api and exradius-worker in the
#   namespace exradius, for tokens with the audience vault (the chart's vault.audience);
# - a sample secret secret/exradius/demo with two fields.
#
# The chart uses it with deploy/kind/values-vault.yaml: `tilt up -- --vault`, or
# `make -C deploy/kind install VAULT=1`.
#
# LOCAL ONLY: dev mode is unsealed, keeps everything in memory, speaks plain HTTP, and its
# root token is "root".
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
context=kind-exradius

say() { printf '==> %s\n' "$*"; }
# The vault CLI inside the Vault pod (VAULT_ADDR and VAULT_TOKEN are set there).
vault() { kubectl --context "$context" -n vault exec deploy/vault -- vault "$@"; }
vault_stdin() { kubectl --context "$context" -n vault exec -i deploy/vault -- vault "$@"; }

say "Vault in dev mode (namespace vault)"
kubectl --context "$context" apply -f "$here/vault.yaml" >/dev/null
kubectl --context "$context" -n vault rollout status deploy/vault --timeout=180s

say "Kubernetes auth method at auth/kubernetes"
if ! vault auth list -format=json | grep -q '"kubernetes/"'; then
  vault auth enable kubernetes >/dev/null
fi
# In its pod, Vault reads its own ServiceAccount token and the cluster's CA for the TokenReviews.
vault write auth/kubernetes/config kubernetes_host=https://kubernetes.default.svc >/dev/null

say "policy exradius"
vault_stdin policy write exradius - >/dev/null <<'EOF'
path "secret/data/exradius/*"    { capabilities = ["read"] }
path "secret/subkeys/exradius/*" { capabilities = ["read"] }
EOF

say "role exradius (exradius-api and exradius-worker in exradius, audience vault)"
vault write auth/kubernetes/role/exradius \
  bound_service_account_names=exradius-api,exradius-worker \
  bound_service_account_namespaces=exradius \
  audience=vault \
  token_policies=exradius \
  token_ttl=1h >/dev/null

say "sample secret secret/exradius/demo"
if ! vault kv get -mount=secret exradius/demo >/dev/null 2>&1; then
  vault kv put -mount=secret exradius/demo DEMO_TOKEN=demo-token-1234 DEMO_PASSWORD=demo-password-5678 >/dev/null
fi
vault kv get -mount=secret -format=json exradius/demo | grep -o '"DEMO_[A-Z]*"' | sort -u | tr '\n' ' '
echo

say "ready: install with deploy/kind/values-vault.yaml, then a store of kind vault at secret, exradius/demo"
