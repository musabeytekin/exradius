# values-generated.yaml: the project's part of the chart's values, given after
# charts/exradius/values-gke.yaml. No secret is in it: bootstrap.sh makes the Secrets.

locals {
  registry_host  = "${var.region}-docker.pkg.dev"
  images         = "${local.registry_host}/${var.project}/${google_artifact_registry_repository.stack.repository_id}"
  builds         = "${local.registry_host}/${var.project}/${google_artifact_registry_repository.builds.repository_id}"
  public_api_url = "http://${google_compute_address.ingress.address}"
  # NFS has no idmapped mounts, and gVisor cannot run a user-namespaced pod: either turns
  # user namespaces off.
  runs = merge(
    var.rwx ? { projectVolume = { accessMode = "ReadWriteMany", storageClass = "standard-rwx" } } : {},
    var.gvisor ? { runtimeClassName = "gvisor" } : {},
    var.rwx || var.gvisor ? { userNamespaces = false } : {},
  )
  values = merge(
    {
      publicApiUrl = local.public_api_url
      images       = { registry = local.images, tag = var.image_tag }
      objectStore  = { bucket = google_storage_bucket.objects.name }
      builds       = { registry = local.builds }
    },
    length(local.runs) > 0 ? { runs = local.runs } : {},
    local.vault_values,
  )
}

resource "local_file" "values" {
  filename        = "${path.module}/values-generated.yaml"
  file_permission = "0644"
  content         = "# Written by deploy/terraform/gcp (terraform apply). Give it after charts/exradius/values-gke.yaml.\n${yamlencode(local.values)}"
}

output "public_api_url" {
  description = "The panel and the API, once Traefik and the chart are installed."
  value       = local.public_api_url
}

output "cluster_name" {
  value = google_container_cluster.cluster.name
}

output "registry" {
  value = local.images
}

output "push_command" {
  description = "Signs Docker in to Artifact Registry and pushes every Exradius image."
  value       = "gcloud auth configure-docker ${local.registry_host} --quiet && make -C deploy push REGISTRY=${local.images} TAG=${var.image_tag} PLATFORM=${var.arm64 ? "linux/arm64" : "linux/amd64"}"
}

output "bootstrap" {
  description = "What deploy/terraform/bootstrap.sh needs."
  value = merge({
    cloud         = "gcp"
    kubeconfig    = "gcloud container clusters get-credentials ${google_container_cluster.cluster.name} --zone ${var.zone} --project ${var.project}"
    context       = "gke_${var.project}_${var.zone}_${google_container_cluster.cluster.name}"
    allowed_cidrs = var.allowed_cidrs
    ingress_ip    = google_compute_address.ingress.address
    arm64         = var.arm64
  }, local.vault_bootstrap)
}

output "postgres_uri" {
  description = "The database URL for the Secret exradius-postgres."
  value       = "postgresql://exradius:${random_password.db.result}@${google_sql_database_instance.db.private_ip_address}:5432/exradius?ssl=require"
  sensitive   = true
}
