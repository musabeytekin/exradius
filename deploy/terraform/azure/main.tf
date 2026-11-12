# Exradius on Azure Kubernetes Service (docs/kubernetes.md §16, §28): a resource group and
# VNet, an AKS cluster (Free tier, Azure CNI overlay powered by Cilium, Workload Identity)
# with a system and a tainted run node pool, two container registries (the Exradius images,
# and builds), a storage account, Azure Database for PostgreSQL on a private address, the
# federated identities of the api, the worker and build pods, a static address for the
# panel's load balancer, and values-generated.yaml for helm. RabbitMQ runs in the cluster
# (bootstrap.sh installs its operator).
#
#   terraform init && terraform apply -var subscription_id=<id> -var 'allowed_cidrs=["<your address>/32"]'
#     an existing Vault (vault.tf): add -var 'vault={address="https://vault.example.com:8200", audience="vault"}'
#   ../bootstrap.sh azure     # kubeconfig, Traefik, cert-manager, the RabbitMQ operator, Secrets
#   make -C ../.. push ...    # the images (terraform output push_command)
#   helm upgrade --install exradius ../../../charts/exradius -n exradius \
#     -f ../../../charts/exradius/values-aks.yaml -f values-generated.yaml

locals {
  system_size = var.system_vm_size != "" ? var.system_vm_size : (var.arm64 ? "Standard_D2ps_v5" : "Standard_D2s_v5")
  run_size    = var.run_vm_size != "" ? var.run_vm_size : (var.arm64 ? "Standard_D4ps_v5" : "Standard_D4s_v5")
  # Globally unique, letters and digits only.
  compact = "${replace(var.name, "-", "")}${random_string.suffix.result}"
  tags    = { "exradius.dev-install" = var.name }
}

resource "random_string" "suffix" {
  length  = 6
  special = false
  upper   = false
}

resource "azurerm_resource_group" "rg" {
  name     = var.name
  location = var.location
  tags     = local.tags
}

# -- network -----------------------------------------------------------------------------

resource "azurerm_virtual_network" "vnet" {
  name                = var.name
  location            = var.location
  resource_group_name = azurerm_resource_group.rg.name
  address_space       = ["10.40.0.0/16"]
  tags                = local.tags
}

resource "azurerm_subnet" "nodes" {
  name                 = "nodes"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.40.0.0/20"]
}

# PostgreSQL's private access: a delegated subnet and its private DNS zone.
resource "azurerm_subnet" "postgres" {
  name                 = "postgres"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.40.16.0/24"]
  delegation {
    name = "postgres"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_private_dns_zone" "postgres" {
  name                = "${var.name}.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.rg.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                = "postgres"
  private_dns_zone_id = azurerm_private_dns_zone.postgres.id
  virtual_network_id  = azurerm_virtual_network.vnet.id
}

# The panel's load balancer (Traefik's Service) takes this address, so publicApiUrl is known
# before anything is installed.
resource "azurerm_public_ip" "ingress" {
  name                = "${var.name}-ingress"
  location            = var.location
  resource_group_name = azurerm_resource_group.rg.name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.tags
}

# -- cluster -----------------------------------------------------------------------------

resource "azurerm_kubernetes_cluster" "aks" {
  name                = var.name
  location            = var.location
  resource_group_name = azurerm_resource_group.rg.name
  dns_prefix          = var.name
  kubernetes_version  = var.kubernetes_version
  sku_tier            = "Free"
  tags                = local.tags

  oidc_issuer_enabled       = true
  workload_identity_enabled = true

  api_server_access_profile {
    authorized_ip_ranges = var.allowed_cidrs
  }

  default_node_pool {
    name            = "system"
    vm_size         = local.system_size
    node_count      = var.system_nodes
    vnet_subnet_id  = azurerm_subnet.nodes.id
    os_sku          = "Ubuntu2404"
    os_disk_size_gb = 64
    node_labels     = { "exradius.dev/pool" = "system" }
    upgrade_settings {
      max_surge = "10%"
    }
  }

  identity {
    type = "SystemAssigned"
  }

  # The node pools here, not node auto-provisioning.
  node_provisioning_profile {
    mode = "Manual"
  }

  # Cilium enforces NetworkPolicy (docs/kubernetes.md §11).
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_data_plane  = "cilium"
    network_policy      = "cilium"
    load_balancer_sku   = "standard"
    pod_cidr            = "10.244.0.0/16"
    service_cidr        = "10.41.0.0/16"
    dns_service_ip      = "10.41.0.10"
  }
}

resource "azurerm_kubernetes_cluster_node_pool" "runs" {
  name                  = "runs"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.aks.id
  vm_size               = local.run_size
  node_count            = var.run_nodes
  vnet_subnet_id        = azurerm_subnet.nodes.id
  os_sku                = "Ubuntu2404"
  os_disk_size_gb       = 128
  node_labels           = { "exradius.dev/pool" = "runs" }
  # Only run and build pods (values runs.tolerations), and DaemonSets that tolerate it.
  node_taints = ["exradius.dev/runs=true:NoSchedule"]
  tags        = local.tags
  upgrade_settings {
    max_surge = "10%"
  }
}

# The cluster places its load balancer on the reserved address in this resource group.
resource "azurerm_role_assignment" "cluster_network" {
  scope                = azurerm_resource_group.rg.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_kubernetes_cluster.aks.identity[0].principal_id
}
