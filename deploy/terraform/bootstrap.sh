#!/usr/bin/env bash
# After `terraform apply` in deploy/terraform/<cloud>: what a cloud install needs in the
# cluster before `helm install` (docs/kubernetes.md §28). Idempotent: run it again to finish
# a setup that stopped half-way; Secrets that exist are kept.
#
#   deploy/terraform/bootstrap.sh aws|gcp|azure
#
#   1. the kubeconfig context, with the cloud's CLI (signed in by you: aws sso login,
#      gcloud auth login, az login);
#   2. aws: gp3 as the default storage class (EFS's with rwx) and the AWS Load Balancer
#      Controller; gcp and azure: cert-manager and the RabbitMQ Cluster Operator;
#   3. Traefik behind the cloud's load balancer, on the address Terraform reserved, open to
#      allowed_cidrs only;
#   4. the namespace exradius and its Secrets: the secret key and the admin password
#      (generated here, the password printed once), the database URL and, on AWS, the
#      broker's user, from Terraform's outputs. They never touch a file.
#
# Then push the images (terraform output push_command) and install the chart with the
# cloud's values file and values-generated.yaml.
set -euo pipefail

cloud=${1:-}
case "$cloud" in aws | gcp | azure) ;; *) echo "usage: $0 aws|gcp|azure" >&2; exit 2 ;; esac
here=$(cd "$(dirname "$0")" && pwd)
dir="$here/$cloud"
root=$(cd "$here/../.." && pwd)

traefik_chart_version=42.3.1        # Traefik v3.7.17
lb_controller_chart_version=3.6.2   # AWS Load Balancer Controller
cert_manager_version=v1.21.3        # as deploy/kind/dev-up.sh
rabbitmq_operator_version=v2.23.2   # RabbitMQ Cluster Operator, as deploy/kind/dev-up.sh

say() { printf '==> %s\n' "$*"; }
tf() { terraform -chdir="$dir" "$@"; }
# A field of Terraform's bootstrap output.
field() { tf output -json bootstrap | python3 -c 'import json, sys; v = json.load(sys.stdin)[sys.argv[1]]; print(v if isinstance(v, str) else json.dumps(v))' "$1"; }

say "kubeconfig"
eval "$(field kubeconfig)" >/dev/null
context=$(field context)
if [ "$cloud" = gcp ]; then
  # gcloud names the context gke_<project>_<zone>_<name>; the cluster's name is shorter.
  kubectl config rename-context "$context" "$(tf output -raw cluster_name)" >/dev/null 2>&1 || true
  context=$(tf output -raw cluster_name)
fi
kubectl=(kubectl --context "$context")
helm=(helm --kube-context "$context")
"${kubectl[@]}" get nodes -L exradius.dev/pool,kubernetes.io/arch

# Placement of the add-ons: the system pool, tolerating GKE's taint of Arm nodes.
system_placement=(--set-json 'nodeSelector={"exradius.dev/pool":"system"}'
  --set-json 'tolerations=[{"key":"kubernetes.io/arch","operator":"Equal","value":"arm64","effect":"NoSchedule"}]')

case "$cloud" in
aws)
  say "gp3, the default storage class"
  "${kubectl[@]}" apply -f - >/dev/null <<'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
  annotations: {storageclass.kubernetes.io/is-default-class: "true"}
provisioner: ebs.csi.aws.com
parameters: {type: gp3, encrypted: "true"}
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
reclaimPolicy: Delete
EOF
  efs=$(field efs_id)
  if [ -n "$efs" ]; then
    say "efs-sc, for ReadWriteMany project volumes"
    "${kubectl[@]}" apply -f - >/dev/null <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: efs-sc}
provisioner: efs.csi.aws.com
parameters: {provisioningMode: efs-ap, fileSystemId: $efs, directoryPerms: "0770", gid: "1000"}
reclaimPolicy: Delete
EOF
  fi
  say "AWS Load Balancer Controller ($lb_controller_chart_version)"
  "${helm[@]}" upgrade --install aws-load-balancer-controller aws-load-balancer-controller \
    --repo https://aws.github.io/eks-charts --version "$lb_controller_chart_version" \
    --namespace kube-system --set clusterName="$(tf output -raw cluster_name)" \
    --set region="$(tf output -raw region)" --set vpcId="$(field vpc_id)" \
    --set serviceAccount.name=aws-load-balancer-controller "${system_placement[@]}" --wait >/dev/null
  allocations=$(field eip_allocations | python3 -c 'import json, sys; print(",".join(json.load(sys.stdin)))')
  subnets=$(field ingress_subnets | python3 -c 'import json, sys; print(",".join(json.load(sys.stdin)))')
  # A Network Load Balancer on the Elastic IPs, one per public subnet, routing to Traefik's
  # pod addresses; its security group lets allowed_cidrs in.
  lb_settings=(
    --set-string 'service.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-type=external'
    --set-string 'service.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-nlb-target-type=ip'
    --set-string 'service.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-scheme=internet-facing'
    --set-string "service.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-eip-allocations=${allocations//,/\\,}"
    --set-string "service.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-subnets=${subnets//,/\\,}"
    --set-string 'service.annotations.service\.beta\.kubernetes\.io/aws-load-balancer-attributes=load_balancing.cross_zone.enabled=true'
  )
  ;;
gcp | azure)
  say "cert-manager ($cert_manager_version)"
  "${helm[@]}" upgrade --install cert-manager cert-manager --repo https://charts.jetstack.io \
    --version "$cert_manager_version" --namespace cert-manager --create-namespace --set crds.enabled=true \
    "${system_placement[@]}" \
    --set-json 'webhook.nodeSelector={"exradius.dev/pool":"system"}' \
    --set-json 'webhook.tolerations=[{"key":"kubernetes.io/arch","operator":"Equal","value":"arm64","effect":"NoSchedule"}]' \
    --set-json 'cainjector.nodeSelector={"exradius.dev/pool":"system"}' \
    --set-json 'cainjector.tolerations=[{"key":"kubernetes.io/arch","operator":"Equal","value":"arm64","effect":"NoSchedule"}]' \
    --set-json 'startupapicheck.tolerations=[{"key":"kubernetes.io/arch","operator":"Equal","value":"arm64","effect":"NoSchedule"}]' \
    --wait >/dev/null
  say "RabbitMQ Cluster Operator ($rabbitmq_operator_version)"
  "${kubectl[@]}" apply -f "https://github.com/rabbitmq/cluster-operator/releases/download/$rabbitmq_operator_version/cluster-operator.yml" >/dev/null
  "${kubectl[@]}" -n rabbitmq-system patch deployment rabbitmq-cluster-operator --type merge -p \
    '{"spec":{"template":{"spec":{"nodeSelector":{"exradius.dev/pool":"system"},"tolerations":[{"key":"kubernetes.io/arch","operator":"Equal","value":"arm64","effect":"NoSchedule"}]}}}}' >/dev/null
  "${kubectl[@]}" -n rabbitmq-system rollout status deployment/rabbitmq-cluster-operator --timeout=300s >/dev/null
  ip=$(field ingress_ip)
  if [ "$cloud" = gcp ]; then
    lb_settings=(--set "service.spec.loadBalancerIP=$ip")
  else
    # The address lives in the install's resource group, where the cluster may use it.
    lb_settings=(
      --set-string "service.annotations.service\.beta\.kubernetes\.io/azure-load-balancer-resource-group=$(field resource_group)"
      --set-string "service.annotations.service\.beta\.kubernetes\.io/azure-pip-name=$(field ingress_ip_name)"
    )
  fi
  ;;
esac

say "Traefik ($traefik_chart_version)"
sources=$(field allowed_cidrs)
"${helm[@]}" upgrade --install traefik traefik --repo https://traefik.github.io/charts \
  --version "$traefik_chart_version" --namespace traefik --create-namespace \
  -f "$here/traefik.yaml" "${system_placement[@]}" "${lb_settings[@]}" \
  --set-json "service.spec.loadBalancerSourceRanges=$sources" --wait >/dev/null
for _ in $(seq 60); do
  address=$("${kubectl[@]}" -n traefik get service traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')
  [ -n "$address" ] && break
  sleep 5
done
echo "    the load balancer: ${address:-not ready yet} (publicApiUrl $(tf output -raw public_api_url))"

say "namespace exradius and its Secrets"
"${kubectl[@]}" apply -f "$root/deploy/kind/namespace.yaml" >/dev/null
secret_exists() { "${kubectl[@]}" -n exradius get secret "$1" >/dev/null 2>&1; }
if ! secret_exists exradius-secret-key; then
  "${kubectl[@]}" -n exradius create secret generic exradius-secret-key \
    --from-literal=secret-key="$(openssl rand -base64 32 | tr '+/' '-_')" >/dev/null
fi
if ! secret_exists exradius-admin; then
  password=$(openssl rand -hex 12)
  "${kubectl[@]}" -n exradius create secret generic exradius-admin --from-literal=password="$password" >/dev/null
  printf '\nPanel login: admin / %s\n' "$password"
  printf 'Read it again with:\n  kubectl --context %s -n exradius get secret exradius-admin -o jsonpath='"'"'{.data.password}'"'"' | base64 -d\n\n' "$context"
fi
if ! secret_exists exradius-postgres; then
  tf output -raw postgres_uri | "${kubectl[@]}" -n exradius create secret generic exradius-postgres \
    --from-file=uri=/dev/stdin >/dev/null
fi
if [ "$cloud" = aws ] && ! secret_exists exradius-broker; then
  tf output -raw broker_password | "${kubectl[@]}" -n exradius create secret generic exradius-broker \
    --from-literal=username=exradius --from-file=password=/dev/stdin >/dev/null
fi

say "ready. Push the images, then install the chart:"
echo "    $(tf output -raw push_command)"
case "$cloud" in aws) values=eks ;; gcp) values=gke ;; azure) values=aks ;; esac
echo "    helm upgrade --install exradius charts/exradius --kube-context $context --namespace exradius \\"
echo "      -f charts/exradius/values-$values.yaml -f deploy/terraform/$cloud/values-generated.yaml"
