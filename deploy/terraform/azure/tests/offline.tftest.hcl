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
