# What the chart's values point at: the registries, the storage account, PostgreSQL, and
# the Workload Identity of the api, the worker and build pods.

# -- registries --------------------------------------------------------------------------

# The Exradius images (make -C deploy push REGISTRY=<name>.azurecr.io).
resource "azurerm_container_registry" "stack" {
  name                = local.compact
  location            = var.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

# Images built from projects' Dockerfiles (builds.registry): a registry of its own, so build
# pods may push there and nowhere else.
resource "azurerm_container_registry" "builds" {
  name                = "${local.compact}builds"
  location            = var.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

resource "azurerm_role_assignment" "kubelet_pull" {
  for_each             = { stack = azurerm_container_registry.stack.id, builds = azurerm_container_registry.builds.id }
  scope                = each.value
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
}

# -- object store ------------------------------------------------------------------------

resource "azurerm_storage_account" "objects" {
  name                            = local.compact
  location                        = var.location
  resource_group_name             = azurerm_resource_group.rg.name
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  tags                            = local.tags
}

resource "azurerm_storage_container" "objects" {
  name               = "exradius"
  storage_account_id = azurerm_storage_account.objects.id
}

# -- PostgreSQL --------------------------------------------------------------------------

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "azurerm_postgresql_flexible_server" "db" {
  name                          = "${var.name}-${random_string.suffix.result}"
  location                      = var.location
  resource_group_name           = azurerm_resource_group.rg.name
  version                       = "17"
  sku_name                      = var.db_sku
  storage_mb                    = 32768
  backup_retention_days         = 7
  delegated_subnet_id           = azurerm_subnet.postgres.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false
  administrator_login           = "exradius"
  administrator_password        = random_password.db.result
  tags                          = local.tags
  depends_on                    = [azurerm_private_dns_zone_virtual_network_link.postgres]

  lifecycle {
    # Azure places a server in a zone of its choosing; moving it later is not wanted.
    ignore_changes = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "exradius" {
  name      = "exradius"
  server_id = azurerm_postgresql_flexible_server.db.id
}

# -- Workload Identity -------------------------------------------------------------------

resource "azurerm_user_assigned_identity" "app" {
  name                = "${var.name}-app"
  location            = var.location
  resource_group_name = azurerm_resource_group.rg.name
  tags                = local.tags
}

resource "azurerm_user_assigned_identity" "build" {
  name                = "${var.name}-build"
  location            = var.location
  resource_group_name = azurerm_resource_group.rg.name
  tags                = local.tags
}

locals {
  federated = {
    api    = { identity = azurerm_user_assigned_identity.app.id, subject = "system:serviceaccount:exradius:exradius-api" }
    worker = { identity = azurerm_user_assigned_identity.app.id, subject = "system:serviceaccount:exradius:exradius-worker" }
    build  = { identity = azurerm_user_assigned_identity.build.id, subject = "system:serviceaccount:exradius-builds:exradius-build" }
  }
}

resource "azurerm_federated_identity_credential" "sa" {
  for_each                  = local.federated
  name                      = each.key
  user_assigned_identity_id = each.value.identity
  issuer                    = azurerm_kubernetes_cluster.aks.oidc_issuer_url
  subject                   = each.value.subject
  audience                  = ["api://AzureADTokenExchange"]
}

# The api and the worker: blobs in the container; the worker also reads built images.
resource "azurerm_role_assignment" "app_blobs" {
  scope                = azurerm_storage_container.objects.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "app_pull" {
  scope                = azurerm_container_registry.builds.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# Build pods: push to the builds registry, nothing else.
resource "azurerm_role_assignment" "build_push" {
  scope                = azurerm_container_registry.builds.id
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.build.principal_id
}
