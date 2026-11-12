# terraform test: a plan and an apply of this module against mocked providers, so the
# expressions, counts and the generated values are checked without an Azure subscription.

mock_provider "azurerm" {
  mock_resource "azurerm_resource_group" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/exradius" }
  }
  mock_resource "azurerm_subnet" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/exradius/providers/Microsoft.Network/virtualNetworks/exradius/subnets/nodes" }
  }
  mock_resource "azurerm_virtual_network" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/exradius/providers/Microsoft.Network/virtualNetworks/exradius" }
  }
  mock_resource "azurerm_private_dns_zone" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/exradius/providers/Microsoft.Network/privateDnsZones/exradius.postgres.database.azure.com" }
  }
  mock_resource "azurerm_public_ip" {
    defaults = { ip_address = "203.0.113.30" }
  }
  mock_resource "azurerm_container_registry" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/exradius/providers/Microsoft.ContainerRegistry/registries/exradiusabc123" }
  }
  mock_resource "azurerm_storage_account" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/exradius/providers/Microsoft.Storage/storageAccounts/exradiusabc123" }
  }
  mock_resource "azurerm_storage_container" {
    defaults = { id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/exradius/providers/Microsoft.Storage/storageAccounts/exradiusabc123/blobServices/default/containers/exradius" }
  }
  mock_resource "azurerm_postgresql_flexible_server" {
    defaults = {
      id   = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/exradius/providers/Microsoft.DBforPostgreSQL/flexibleServers/exradius-abc123"
      fqdn = "exradius-abc123.postgres.database.azure.com"
    }
  }
  mock_resource "azurerm_user_assigned_identity" {
    defaults = {
      id           = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/exradius/providers/Microsoft.ManagedIdentity/userAssignedIdentities/exradius-app"
      client_id    = "55555555-5555-5555-5555-555555555555"
      principal_id = "66666666-6666-6666-6666-666666666666"
    }
  }
}

mock_provider "local" {}

override_resource {
  target = azurerm_kubernetes_cluster.aks
  values = {
    id              = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/exradius/providers/Microsoft.ContainerService/managedClusters/exradius"
    oidc_issuer_url = "https://westeurope.oic.prod-aks.azure.com/tenant/issuer/"
    identity        = { type = "SystemAssigned", principal_id = "11111111-1111-1111-1111-111111111111", tenant_id = "22222222-2222-2222-2222-222222222222", identity_ids = [] }
    fqdn            = "exradius-abc123.hcp.westeurope.azmk8s.io"
    kube_config     = [{ cluster_ca_certificate = "Y2E=", host = "https://exradius-abc123.hcp.westeurope.azmk8s.io:443", client_certificate = "", client_key = "", username = "", password = "" }]
  }
}

# Mocks leave the cluster's computed kubelet_identity block empty, so the two AcrPull grants
# to the kubelet are left out; everything else (and what it depends on) is planned.

override_resource {
  target = random_string.suffix
  values = { result = "abc123" }
}

variables {
  subscription_id = "00000000-0000-0000-0000-000000000000"
  allowed_cidrs   = ["198.51.100.7/32"]
}

run "defaults" {
  command = apply
  plan_options {
    target = [
      local_file.values, azurerm_kubernetes_cluster_node_pool.runs, azurerm_role_assignment.cluster_network,
      azurerm_federated_identity_credential.sa, azurerm_role_assignment.app_blobs, azurerm_role_assignment.app_pull,
      azurerm_role_assignment.build_push, azurerm_postgresql_flexible_server_database.exradius,
    ]
  }

  assert {
    condition     = output.public_api_url == "http://203.0.113.30"
    error_message = "publicApiUrl is the reserved address"
  }
  assert {
    condition     = azurerm_container_registry.stack.name == "exradiusabc123" && azurerm_container_registry.builds.name == "exradiusabc123builds"
    error_message = "two registries, globally unique names"
  }
  assert {
    condition     = local.values.identity.app.serviceAccountAnnotations["azure.workload.identity/client-id"] == "55555555-5555-5555-5555-555555555555"
    error_message = "the app identity's client id annotates the ServiceAccounts"
  }
  assert {
    condition     = azurerm_federated_identity_credential.sa["build"].subject == "system:serviceaccount:exradius-builds:exradius-build" && length(azurerm_federated_identity_credential.sa) == 3
    error_message = "a federated credential per ServiceAccount"
  }
  assert {
    condition     = azurerm_kubernetes_cluster.aks.network_profile[0].network_policy == "cilium" && azurerm_kubernetes_cluster_node_pool.runs.vm_size == "Standard_D4ps_v5"
    error_message = "Cilium NetworkPolicy, Arm run nodes"
  }
  assert {
    condition     = !contains(keys(local.values), "runs") && local.values.objectStore.azure.account == "exradiusabc123"
    error_message = "the storage account; no run overrides by default"
  }
}

run "rwx_x86" {
  command = apply
  plan_options {
    target = [
      local_file.values, azurerm_kubernetes_cluster_node_pool.runs, azurerm_role_assignment.cluster_network,
      azurerm_federated_identity_credential.sa, azurerm_role_assignment.app_blobs, azurerm_role_assignment.app_pull,
      azurerm_role_assignment.build_push, azurerm_postgresql_flexible_server_database.exradius,
    ]
  }
  variables {
    rwx   = true
    arm64 = false
  }

  assert {
    condition     = local.values.runs.projectVolume.storageClass == "azurefile-csi" && local.values.runs.userNamespaces == false
    error_message = "Azure Files project volumes turn user namespaces off"
  }
  assert {
    condition     = azurerm_kubernetes_cluster.aks.default_node_pool[0].vm_size == "Standard_D2s_v5"
    error_message = "x86 system nodes"
  }
}

# var.vault (vault.tf). Each run gives vault itself, so a -var 'vault=...' on the command
# line changes none of them.

run "vault_every_field" {
  command = apply
  plan_options {
    target = [
      local_file.values, azurerm_kubernetes_cluster_node_pool.runs, azurerm_role_assignment.cluster_network,
      azurerm_federated_identity_credential.sa, azurerm_role_assignment.app_blobs, azurerm_role_assignment.app_pull,
      azurerm_role_assignment.build_push, azurerm_postgresql_flexible_server_database.exradius,
    ]
  }
  variables {
    vault = {
      address        = "https://vault.example.com:8200"
      auth_mount     = "clusters/exradius"
      role           = "exradius-prod"
      namespace      = "admin/platform"
      audience       = "vault"
      ca_file        = "tests/vault-ca.pem"
      token_reviewer = true
    }
  }

  assert {
    condition = yamldecode(local_file.values.content).vault == {
      address       = "https://vault.example.com:8200"
      authMount     = "clusters/exradius"
      role          = "exradius-prod"
      namespace     = "admin/platform"
      audience      = "vault"
      caBundle      = { configMap = "exradius-vault-ca", key = "ca.crt" }
      tokenReviewer = true
    }
    error_message = "values-generated.yaml carries the chart's vault block, every field"
  }
  assert {
    condition     = output.bootstrap.vault_ca_file == abspath("tests/vault-ca.pem")
    error_message = "bootstrap.sh gets the CA file's absolute path for the ConfigMap exradius-vault-ca"
  }
  assert {
    condition = (
      output.vault_kubernetes_auth.kubernetes_host == "https://exradius-abc123.hcp.westeurope.azmk8s.io:443" &&
      output.vault_kubernetes_auth.kubernetes_ca_cert == "ca" &&
      output.vault_kubernetes_auth.auth_mount == "clusters/exradius" &&
      output.vault_kubernetes_auth.role == "exradius-prod" &&
      output.vault_kubernetes_auth.namespace == "exradius" &&
      output.vault_kubernetes_auth.service_accounts == ["exradius-api", "exradius-worker"] &&
      output.vault_kubernetes_auth.audience == "vault"
    )
    error_message = "Vault's admin gets the cluster's API address and decoded CA, the mount, role, namespace, ServiceAccounts and audience"
  }
  assert {
    condition     = strcontains(output.vault_kubernetes_auth.token_reviewer_jwt, "get secret exradius-vault-reviewer")
    error_message = "with token_reviewer, how to read the reviewer's token"
  }
  assert {
    condition = (
      strcontains(output.vault_kubernetes_auth.commands, "export VAULT_NAMESPACE=admin/platform\n") &&
      strcontains(output.vault_kubernetes_auth.commands, "vault auth enable -path=clusters/exradius kubernetes\n") &&
      strcontains(output.vault_kubernetes_auth.commands, "vault write auth/clusters/exradius/config kubernetes_host=https://exradius-abc123.hcp.westeurope.azmk8s.io:443 kubernetes_ca_cert=@cluster-ca.pem token_reviewer_jwt=@reviewer.jwt\n") &&
      strcontains(output.vault_kubernetes_auth.commands, "vault write auth/clusters/exradius/role/exradius-prod bound_service_account_names=exradius-api,exradius-worker bound_service_account_namespaces=exradius audience=vault token_policies=exradius-prod token_ttl=1h\n")
    )
    error_message = "the vault commands use the namespace, mount, role, audience and the reviewer's token"
  }
  assert {
    condition     = alltrue([for line in split("\n", output.vault_kubernetes_auth.commands) : trimspace(line) == line])
    error_message = "the commands are flush left, one per line"
  }
}

run "vault_address_only" {
  command = apply
  plan_options {
    target = [
      local_file.values, azurerm_kubernetes_cluster_node_pool.runs, azurerm_role_assignment.cluster_network,
      azurerm_federated_identity_credential.sa, azurerm_role_assignment.app_blobs, azurerm_role_assignment.app_pull,
      azurerm_role_assignment.build_push, azurerm_postgresql_flexible_server_database.exradius,
    ]
  }
  variables {
    vault = { address = "https://vault.example.com:8200" }
  }

  assert {
    condition = yamldecode(local_file.values.content).vault == {
      address   = "https://vault.example.com:8200"
      authMount = "kubernetes"
      role      = "exradius"
    }
    error_message = "the defaults; no namespace, audience, caBundle or tokenReviewer, so the chart's defaults hold"
  }
  assert {
    condition     = !contains(keys(output.bootstrap), "vault_ca_file")
    error_message = "no CA file for bootstrap.sh"
  }
  assert {
    condition = (
      output.vault_kubernetes_auth.audience == "" && output.vault_kubernetes_auth.token_reviewer_jwt == "" &&
      !strcontains(output.vault_kubernetes_auth.commands, "VAULT_NAMESPACE") &&
      !strcontains(output.vault_kubernetes_auth.commands, "reviewer") &&
      !strcontains(output.vault_kubernetes_auth.commands, "audience=") &&
      startswith(output.vault_kubernetes_auth.commands, "terraform output -json vault_kubernetes_auth")
    )
    error_message = "no audience, reviewer or namespace in the commands"
  }
  assert {
    condition     = alltrue([for line in split("\n", output.vault_kubernetes_auth.commands) : trimspace(line) == line])
    error_message = "the commands are flush left, one per line"
  }
}

run "no_vault" {
  command = apply
  plan_options {
    target = [
      local_file.values, azurerm_kubernetes_cluster_node_pool.runs, azurerm_role_assignment.cluster_network,
      azurerm_federated_identity_credential.sa, azurerm_role_assignment.app_blobs, azurerm_role_assignment.app_pull,
      azurerm_role_assignment.build_push, azurerm_postgresql_flexible_server_database.exradius,
    ]
  }
  variables {
    vault = null
  }

  assert {
    condition     = !contains(keys(yamldecode(local_file.values.content)), "vault") && !contains(keys(local.values), "vault")
    error_message = "without var.vault the generated values have no vault block"
  }
  assert {
    condition     = output.vault_kubernetes_auth == null && !contains(keys(output.bootstrap), "vault_ca_file")
    error_message = "without var.vault, vault_kubernetes_auth is null and bootstrap.sh gets no CA file"
  }
}

run "vault_refuses_a_bad_address" {
  command = plan
  plan_options {
    target = [local_file.values]
  }
  variables {
    vault = { address = "vault.example.com:8200" }
  }
  expect_failures = [var.vault]
}

run "vault_refuses_a_missing_ca_file" {
  command = plan
  plan_options {
    target = [local_file.values]
  }
  variables {
    vault = { address = "https://vault.example.com:8200", ca_file = "tests/no-such-ca.pem" }
  }
  expect_failures = [var.vault]
}
