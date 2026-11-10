# Exradius on Google Kubernetes Engine (docs/kubernetes.md §16, §28): a VPC, a zonal GKE
# Standard cluster (Dataplane V2, Workload Identity) with a system and a tainted run node
# pool, Artifact Registry (one repository for the Exradius images, one for builds), a Cloud
# Storage bucket, Cloud SQL for PostgreSQL on a private address, the IAM grants of the api,
# the worker and build pods, a static address for the panel's load balancer, and
# values-generated.yaml for helm. RabbitMQ runs in the cluster (bootstrap.sh installs its
# operator).
#
#   terraform init && terraform apply -var project=<id> -var 'allowed_cidrs=["<your address>/32"]'
#   ../bootstrap.sh gcp       # kubeconfig, Traefik, cert-manager, the RabbitMQ operator, Secrets
#   make -C ../.. push ...    # the images (terraform output push_command)
#   helm upgrade --install exradius ../../../charts/exradius -n exradius \
#     -f ../../../charts/exradius/values-gke.yaml -f values-generated.yaml

data "google_project" "this" {}

locals {
  system_type = var.system_machine_type != "" ? var.system_machine_type : (var.arm64 ? "t2a-standard-2" : "e2-standard-2")
  run_type    = var.run_machine_type != "" ? var.run_machine_type : (var.arm64 ? "t2a-standard-4" : "e2-standard-4")
  apis = concat(
    ["compute.googleapis.com", "container.googleapis.com", "artifactregistry.googleapis.com",
    "sqladmin.googleapis.com", "servicenetworking.googleapis.com", "iam.googleapis.com"],
    var.rwx ? ["file.googleapis.com"] : [],
  )
  # Workload Identity Federation for GKE: IAM grants name a Kubernetes ServiceAccount.
  workload_pool = "${var.project}.svc.id.goog"
  principal     = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${local.workload_pool}/subject"
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# -- network -----------------------------------------------------------------------------

resource "google_compute_network" "vpc" {
  name                    = var.name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "nodes" {
  name                     = var.name
  region                   = var.region
  network                  = google_compute_network.vpc.id
  ip_cidr_range            = "10.40.0.0/20"
  private_ip_google_access = true
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.44.0.0/14"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.48.0.0/20"
  }
}

# Cloud SQL's private address comes through private services access.
resource "google_compute_global_address" "private_services" {
  name          = "${var.name}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
  # Deleting the peering fails while Cloud SQL's producer network still holds it; it goes
  # with the network.
  deletion_policy = "ABANDON"
}

# The panel's load balancer (Traefik's Service) takes this address, so publicApiUrl is known
# before anything is installed.
resource "google_compute_address" "ingress" {
  name       = "${var.name}-ingress"
  region     = var.region
  depends_on = [google_project_service.apis]
}

# -- cluster -----------------------------------------------------------------------------

resource "google_service_account" "nodes" {
  account_id   = "${var.name}-nodes"
  display_name = "Exradius GKE nodes"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "nodes" {
  for_each = toset(["roles/logging.logWriter", "roles/monitoring.metricWriter", "roles/monitoring.viewer",
  "roles/stackdriver.resourceMetadata.writer"])
  project = var.project
  role    = each.value
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_container_cluster" "cluster" {
  name                = var.name
  location            = var.zone
  network             = google_compute_network.vpc.id
  subnetwork          = google_compute_subnetwork.nodes.id
  deletion_protection = false

  # No default node pool: the pools are their own resources below (GKE 1.36.4-gke.1469000
  # and later create a cluster without one).
  initial_node_count = 0

  release_channel {
    channel = "REGULAR"
  }
  min_master_version = var.kubernetes_version

  # Dataplane V2 enforces NetworkPolicy (docs/kubernetes.md §11).
  datapath_provider = "ADVANCED_DATAPATH"
  networking_mode   = "VPC_NATIVE"
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  workload_identity_config {
    workload_pool = local.workload_pool
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.allowed_cidrs
      content {
        cidr_block = cidr_blocks.value
      }
    }
  }

  addons_config {
    gcp_filestore_csi_driver_config {
      enabled = var.rwx
    }
  }
}

resource "google_container_node_pool" "system" {
  name       = "system"
  cluster    = google_container_cluster.cluster.id
  node_count = var.system_nodes

  node_config {
    machine_type    = local.system_type
    image_type      = var.node_image_type
    disk_type       = "pd-balanced"
    disk_size_gb    = 50
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = { "exradius.dev/pool" = "system" }
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }
}

resource "google_container_node_pool" "runs" {
  name       = "runs"
  cluster    = google_container_cluster.cluster.id
  node_count = var.run_nodes

  node_config {
    machine_type    = local.run_type
    image_type      = var.node_image_type
    disk_type       = "pd-balanced"
    disk_size_gb    = 100
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    labels          = { "exradius.dev/pool" = "runs" }
    # Only run and build pods (values runs.tolerations), and DaemonSets that tolerate it.
    taint {
      key    = "exradius.dev/runs"
      value  = "true"
      effect = "NO_SCHEDULE"
    }
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
    dynamic "sandbox_config" {
      for_each = var.gvisor ? [1] : []
      content {
        type = "GVISOR"
      }
    }
  }
}
