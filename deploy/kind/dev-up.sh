#!/usr/bin/env bash
# A local kind cluster for Exradius (docs/kubernetes.md §17). Idempotent: run it again to
# finish a setup that stopped half-way; nothing that exists is replaced.
#
#   1. the registry container kind-registry (localhost:5001 from this machine,
#      kind-registry:5000 from the node);
#   2. the cluster exradius, with the node's containerd pointed at the registry and the build
#      pods' seccomp profile (deploy/seccomp/buildkit.json) in the kubelet's directory;
#   3. CloudNativePG, cert-manager (the RabbitMQ operator's webhooks need it) and the
#      RabbitMQ Cluster Operator;
#   4. the namespace exradius and the Secrets the chart reads: the secret key, the admin
#      password (printed when it is created) and the SeaweedFS S3 credentials.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
cluster=exradius
context=kind-$cluster
registry=kind-registry
registry_image=registry:2.8.3
cnpg_chart_version=0.29.1          # CloudNativePG 1.30.1
cert_manager_version=v1.21.3
rabbitmq_operator_version=v2.23.2  # RabbitMQ Cluster Operator
kubectl=(kubectl --context "$context")

say() { printf '==> %s\n' "$*"; }

say "registry $registry on 127.0.0.1:5001"
if [ "$(docker inspect -f '{{.State.Running}}' "$registry" 2>/dev/null || true)" != true ]; then
  if docker inspect "$registry" >/dev/null 2>&1; then
    docker start "$registry" >/dev/null
  else
    docker run -d --restart=always -p 127.0.0.1:5001:5000 --network bridge --name "$registry" "$registry_image" >/dev/null
  fi
fi

say "cluster $cluster"
if ! kind get clusters 2>/dev/null | grep -qx "$cluster"; then
  kind create cluster --config "$here/cluster.yaml"
fi
kubectl config use-context "$context" >/dev/null

say "registry hosts on the node"
for node in $(kind get nodes --name "$cluster"); do
  for host in localhost:5001 kind-registry:5000; do
    docker exec "$node" mkdir -p "/etc/containerd/certs.d/$host"
    printf '[host."http://%s:5000"]\n' "$registry" | docker exec -i "$node" cp /dev/stdin "/etc/containerd/certs.d/$host/hosts.toml"
  done
done
say "build pods' seccomp profile on the node"
make -s -C "$here" dev-seccomp >/dev/null
if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$registry")" = null ]; then
  docker network connect kind "$registry"
fi
"${kubectl[@]}" apply -f "$here/registry-hosting.yaml" >/dev/null

say "CloudNativePG (chart $cnpg_chart_version)"
helm upgrade --install cnpg cloudnative-pg --repo https://cloudnative-pg.github.io/charts \
  --version "$cnpg_chart_version" --kube-context "$context" \
  --namespace cnpg-system --create-namespace --wait >/dev/null

say "cert-manager ($cert_manager_version)"
helm upgrade --install cert-manager cert-manager --repo https://charts.jetstack.io \
  --version "$cert_manager_version" --kube-context "$context" \
  --namespace cert-manager --create-namespace --set crds.enabled=true --wait >/dev/null

say "RabbitMQ Cluster Operator ($rabbitmq_operator_version)"
"${kubectl[@]}" apply -f "https://github.com/rabbitmq/cluster-operator/releases/download/$rabbitmq_operator_version/cluster-operator.yml" >/dev/null
"${kubectl[@]}" -n rabbitmq-system rollout status deployment/rabbitmq-cluster-operator --timeout=300s >/dev/null

say "namespace exradius and its Secrets"
"${kubectl[@]}" apply -f "$here/namespace.yaml" >/dev/null
secret_exists() { "${kubectl[@]}" -n exradius get secret "$1" >/dev/null 2>&1; }
if ! secret_exists exradius-secret-key; then
  # A Fernet key: 32 random bytes, URL-safe base64.
  "${kubectl[@]}" -n exradius create secret generic exradius-secret-key \
    --from-literal=secret-key="$(openssl rand -base64 32 | tr '+/' '-_')" >/dev/null
fi
if ! secret_exists exradius-admin; then
  password=$(openssl rand -hex 12)
  "${kubectl[@]}" -n exradius create secret generic exradius-admin --from-literal=password="$password" >/dev/null
  printf '\nPanel login: admin / %s\n' "$password"
  printf 'Read it again with:\n  kubectl --context %s -n exradius get secret exradius-admin -o jsonpath='"'"'{.data.password}'"'"' | base64 -d\n\n' "$context"
fi
if ! secret_exists exradius-objectstore; then
  "${kubectl[@]}" -n exradius create secret generic exradius-objectstore \
    --from-literal=accessKey=exradius --from-literal=secretKey="$(openssl rand -hex 20)" >/dev/null
fi

say "ready: tilt up (or make -C deploy/kind images install)"
