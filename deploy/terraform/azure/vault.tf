# An existing Vault for secret stores of kind vault (var.vault, docs/config-repo.md §7.4):
# the vault block of values-generated.yaml, and what Vault's admin configures for this
# cluster. Exradius does not install or operate Vault.

locals {
  # Empty without var.vault: the generated values are as without it.
  vault_values = var.vault == null ? {} : {
    vault = merge(
      { address = var.vault.address, authMount = var.vault.auth_mount, role = var.vault.role },
      var.vault.namespace != "" ? { namespace = var.vault.namespace } : {},
      var.vault.audience != "" ? { audience = var.vault.audience } : {},
      # bootstrap.sh makes this ConfigMap from ca_file.
      var.vault.ca_file != "" ? { caBundle = { configMap = "exradius-vault-ca", key = "ca.crt" } } : {},
      var.vault.token_reviewer ? { tokenReviewer = true } : {},
    )
  }
  # bootstrap.sh's field, only with a CA file.
  vault_bootstrap = var.vault == null ? {} : var.vault.ca_file == "" ? {} : { vault_ca_file = abspath(var.vault.ca_file) }
  # The cluster as Vault's Kubernetes auth method reaches it from outside.
  kubernetes_host    = "https://${azurerm_kubernetes_cluster.aks.fqdn}:443"
  kubernetes_ca_cert = try(nonsensitive(base64decode(azurerm_kubernetes_cluster.aks.kube_config[0].cluster_ca_certificate)), "")
}

output "vault_kubernetes_auth" {
  description = "With var.vault: what Vault's admin configures for this cluster (Kubernetes auth). A Vault outside the cluster reviews tokens at kubernetes_host, so its address must be in allowed_cidrs. null without var.vault."
  value = var.vault == null ? null : {
    kubernetes_host    = local.kubernetes_host
    kubernetes_ca_cert = local.kubernetes_ca_cert
    auth_mount         = var.vault.auth_mount
    role               = var.vault.role
    namespace          = "exradius"
    service_accounts   = ["exradius-api", "exradius-worker"]
    audience           = var.vault.audience
    # Vault outside the cluster, with an audience: the reviewer's token, once the chart is installed.
    token_reviewer_jwt = var.vault.token_reviewer ? "kubectl -n exradius get secret exradius-vault-reviewer -o jsonpath='{.data.token}' | base64 -d" : ""
    # One pair per store's mount and path, or a prefix with * (the kind loop's convention below).
    policy   = <<-EOT
      path "secret/data/exradius/*"    { capabilities = ["read"] }
      path "secret/subkeys/exradius/*" { capabilities = ["read"] }
    EOT
    commands = <<-EOT
      %{if var.vault.namespace != ""~}
      export VAULT_NAMESPACE=${var.vault.namespace}
      %{endif~}
      terraform output -json vault_kubernetes_auth | python3 -c 'import json,sys; print(json.load(sys.stdin)["kubernetes_ca_cert"], end="")' > cluster-ca.pem
      terraform output -json vault_kubernetes_auth | python3 -c 'import json,sys; print(json.load(sys.stdin)["policy"], end="")' > exradius-policy.hcl
      %{if var.vault.token_reviewer~}
      kubectl -n exradius get secret exradius-vault-reviewer -o jsonpath='{.data.token}' | base64 -d > reviewer.jwt
      %{endif~}
      vault auth enable -path=${var.vault.auth_mount} kubernetes
      vault write auth/${var.vault.auth_mount}/config kubernetes_host=${local.kubernetes_host} kubernetes_ca_cert=@cluster-ca.pem%{if var.vault.token_reviewer} token_reviewer_jwt=@reviewer.jwt%{endif}
      vault policy write ${var.vault.role} exradius-policy.hcl
      vault write auth/${var.vault.auth_mount}/role/${var.vault.role} bound_service_account_names=exradius-api,exradius-worker bound_service_account_namespaces=exradius%{if var.vault.audience != ""} audience=${var.vault.audience}%{endif} token_policies=${var.vault.role} token_ttl=1h
    EOT
  }
}
