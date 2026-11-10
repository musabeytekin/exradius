# values-generated.yaml: the subscription's part of the chart's values, given after
# charts/exradius/values-aks.yaml. No secret is in it: bootstrap.sh makes the Secrets.

locals {
  public_api_url = "http://${azurerm_public_ip.ingress.ip_address}"
  values = merge(
    {
      publicApiUrl = local.public_api_url
      images       = { registry = azurerm_container_registry.stack.login_server, tag = var.image_tag }
      builds       = { registry = azurerm_container_registry.builds.login_server }
      objectStore = {
        bucket = azurerm_storage_container.objects.name
        azure  = { account = azurerm_storage_account.objects.name }
      }
      identity = {
        app   = { serviceAccountAnnotations = { "azure.workload.identity/client-id" = azurerm_user_assigned_identity.app.client_id } }
        build = { serviceAccountAnnotations = { "azure.workload.identity/client-id" = azurerm_user_assigned_identity.build.client_id } }
      }
    },
    var.rwx ? {
      runs = {
        projectVolume = { accessMode = "ReadWriteMany", storageClass = "azurefile-csi" }
        # SMB has no idmapped mounts: user-namespaced pods cannot mount it.
        userNamespaces = false
      }
    } : {},
  )
}

resource "local_file" "values" {
  filename        = "${path.module}/values-generated.yaml"
  file_permission = "0644"
  content         = "# Written by deploy/terraform/azure (terraform apply). Give it after charts/exradius/values-aks.yaml.\n${yamlencode(local.values)}"
}

output "public_api_url" {
  description = "The panel and the API, once Traefik and the chart are installed."
  value       = local.public_api_url
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.aks.name
}

output "registry" {
  value = azurerm_container_registry.stack.login_server
}

output "push_command" {
  description = "Signs Docker in to the registry and pushes every Exradius image."
  value       = "az acr login --name ${azurerm_container_registry.stack.name} && make -C deploy push REGISTRY=${azurerm_container_registry.stack.login_server} TAG=${var.image_tag} PLATFORM=${var.arm64 ? "linux/arm64" : "linux/amd64"}"
}

output "bootstrap" {
  description = "What deploy/terraform/bootstrap.sh needs."
  value = {
    cloud           = "azure"
    kubeconfig      = "az aks get-credentials --resource-group ${azurerm_resource_group.rg.name} --name ${azurerm_kubernetes_cluster.aks.name} --overwrite-existing"
    context         = azurerm_kubernetes_cluster.aks.name
    allowed_cidrs   = var.allowed_cidrs
    ingress_ip      = azurerm_public_ip.ingress.ip_address
    ingress_ip_name = azurerm_public_ip.ingress.name
    resource_group  = azurerm_resource_group.rg.name
  }
}

output "postgres_uri" {
  description = "The database URL for the Secret exradius-postgres."
  value       = "postgresql://exradius:${random_password.db.result}@${azurerm_postgresql_flexible_server.db.fqdn}:5432/exradius?ssl=require"
  sensitive   = true
}
