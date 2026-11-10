# What the chart's values point at: Artifact Registry, the bucket, Cloud SQL, and the IAM
# grants of the nodes, the api, the worker and build pods.

# -- registry ----------------------------------------------------------------------------

# The Exradius images (make -C deploy push REGISTRY=<region>-docker.pkg.dev/<project>/exradius).
resource "google_artifact_registry_repository" "stack" {
  location      = var.region
  repository_id = "${var.name}-images"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

# Images built from projects' Dockerfiles (builds.registry), apart, so build pods may push
# there and nowhere else.
resource "google_artifact_registry_repository" "builds" {
  location      = var.region
  repository_id = "${var.name}-builds"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

resource "google_artifact_registry_repository_iam_member" "nodes" {
  for_each   = { stack = google_artifact_registry_repository.stack.name, builds = google_artifact_registry_repository.builds.name }
  location   = var.region
  repository = each.value
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.nodes.email}"
}

# -- object store ------------------------------------------------------------------------

resource "google_storage_bucket" "objects" {
  name                        = "${var.name}-${var.project}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
}

# -- PostgreSQL --------------------------------------------------------------------------

resource "random_password" "db" {
  length  = 32
  special = false
}

# A deleted instance's name is reserved for a week: a suffix lets a destroyed install be
# made again at once.
resource "random_id" "db" {
  byte_length = 3
}

resource "google_sql_database_instance" "db" {
  name                = "${var.name}-${random_id.db.hex}"
  database_version    = "POSTGRES_17"
  region              = var.region
  deletion_protection = false

  settings {
    # PostgreSQL 16 and later default to Enterprise Plus, which has no shared-core tiers.
    edition   = "ENTERPRISE"
    tier      = var.db_tier
    disk_type = "PD_SSD"
    disk_size = 10
    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.vpc.id
      ssl_mode        = "ENCRYPTED_ONLY"
    }
    backup_configuration {
      enabled = false
    }
  }
  depends_on = [google_service_networking_connection.private_services]
}

resource "google_sql_database" "exradius" {
  name     = "exradius"
  instance = google_sql_database_instance.db.name
}

resource "google_sql_user" "exradius" {
  name     = "exradius"
  instance = google_sql_database_instance.db.name
  password = random_password.db.result
}

# -- Workload Identity -------------------------------------------------------------------

# The api and the worker: objects in the bucket; the worker also reads built images.
resource "google_storage_bucket_iam_member" "app" {
  for_each = toset(["exradius-api", "exradius-worker"])
  bucket   = google_storage_bucket.objects.name
  role     = "roles/storage.objectUser"
  member   = "${local.principal}/ns/exradius/sa/${each.value}"
}

resource "google_artifact_registry_repository_iam_member" "worker" {
  location   = var.region
  repository = google_artifact_registry_repository.builds.name
  role       = "roles/artifactregistry.reader"
  member     = "${local.principal}/ns/exradius/sa/exradius-worker"
}

# Build pods: push to the builds repository, nothing else.
resource "google_artifact_registry_repository_iam_member" "build" {
  location   = var.region
  repository = google_artifact_registry_repository.builds.name
  role       = "roles/artifactregistry.writer"
  member     = "${local.principal}/ns/exradius-builds/sa/exradius-build"
}
