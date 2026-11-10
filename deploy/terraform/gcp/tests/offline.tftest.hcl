# terraform test: a plan and an apply of this module against mocked providers, so the
# expressions, counts and the generated values are checked without a Google Cloud project.

mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "123456789012" }
  }
  mock_resource "google_compute_address" {
    defaults = { address = "203.0.113.20" }
  }
  mock_resource "google_compute_network" {
    defaults = { id = "projects/acme-exradius/global/networks/exradius" }
  }
  mock_resource "google_service_account" {
    defaults = { email = "exradius-nodes@acme-exradius.iam.gserviceaccount.com" }
  }
  mock_resource "google_artifact_registry_repository" {
    defaults = { name = "repo" }
  }
  mock_resource "google_sql_database_instance" {
    defaults = { private_ip_address = "10.50.0.3" }
  }
  mock_resource "google_container_cluster" {
    defaults = { id = "projects/acme-exradius/locations/europe-west4-a/clusters/exradius" }
  }
}

mock_provider "local" {}

variables {
  project       = "acme-exradius"
  allowed_cidrs = ["198.51.100.7/32"]
}

run "defaults" {
  command = apply

  assert {
    condition     = output.public_api_url == "http://203.0.113.20"
    error_message = "publicApiUrl is the reserved address"
  }
  assert {
    condition     = local.values.images.registry == "europe-west4-docker.pkg.dev/acme-exradius/exradius-images" && local.values.builds.registry == "europe-west4-docker.pkg.dev/acme-exradius/exradius-builds"
    error_message = "the images and builds repositories"
  }
  assert {
    condition     = local.values.objectStore.bucket == "exradius-acme-exradius" && !contains(keys(local.values), "runs")
    error_message = "the bucket; no run overrides by default"
  }
  assert {
    condition     = google_artifact_registry_repository_iam_member.build.member == "principal://iam.googleapis.com/projects/123456789012/locations/global/workloadIdentityPools/acme-exradius.svc.id.goog/subject/ns/exradius-builds/sa/exradius-build"
    error_message = "build pods are granted by their ServiceAccount"
  }
  assert {
    condition     = google_container_node_pool.runs.node_config[0].machine_type == "t2a-standard-4" && google_container_cluster.cluster.datapath_provider == "ADVANCED_DATAPATH"
    error_message = "Arm run nodes, Dataplane V2"
  }
  assert {
    condition     = google_sql_database_instance.db.settings[0].edition == "ENTERPRISE" && google_sql_database_instance.db.settings[0].ip_configuration[0].ipv4_enabled == false
    error_message = "Enterprise edition, private address only"
  }
}

run "gvisor_and_rwx" {
  command = apply
  variables {
    gvisor = true
    rwx    = true
    arm64  = false
  }

  assert {
    condition     = local.values.runs.runtimeClassName == "gvisor" && local.values.runs.userNamespaces == false && local.values.runs.projectVolume.storageClass == "standard-rwx"
    error_message = "GKE Sandbox and Filestore turn user namespaces off"
  }
  assert {
    condition     = google_container_node_pool.runs.node_config[0].sandbox_config[0].type == "GVISOR" && contains(keys(google_project_service.apis), "file.googleapis.com")
    error_message = "the sandbox on the run pool, the Filestore API"
  }
  assert {
    condition     = strcontains(output.push_command, "PLATFORM=linux/amd64")
    error_message = "x86 images"
  }
}
