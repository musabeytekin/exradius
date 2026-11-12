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
    defaults = {
      id       = "projects/acme-exradius/locations/europe-west4-a/clusters/exradius"
      endpoint = "198.51.100.40"
    }
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

# var.vault (vault.tf). Each run gives vault itself, so a -var 'vault=...' on the command
# line changes none of them.

run "vault_every_field" {
  command = apply
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
      output.vault_kubernetes_auth.kubernetes_host == "https://198.51.100.40" &&
      # master_auth is a computed block the configuration does not set, so a mock cannot fill
      # it: the CA is try()'s "" here, a real cluster's CA in an apply.
      output.vault_kubernetes_auth.kubernetes_ca_cert == "" &&
      output.vault_kubernetes_auth.auth_mount == "clusters/exradius" &&
      output.vault_kubernetes_auth.role == "exradius-prod" &&
      output.vault_kubernetes_auth.namespace == "exradius" &&
      output.vault_kubernetes_auth.service_accounts == ["exradius-api", "exradius-worker"] &&
      output.vault_kubernetes_auth.audience == "vault"
    )
    error_message = "Vault's admin gets the cluster's API address, the mount, role, namespace, ServiceAccounts and audience"
  }
  assert {
    condition     = strcontains(output.vault_kubernetes_auth.token_reviewer_jwt, "get secret exradius-vault-reviewer")
    error_message = "with token_reviewer, how to read the reviewer's token"
  }
  assert {
    condition = (
      strcontains(output.vault_kubernetes_auth.commands, "export VAULT_NAMESPACE=admin/platform\n") &&
      strcontains(output.vault_kubernetes_auth.commands, "vault auth enable -path=clusters/exradius kubernetes\n") &&
      strcontains(output.vault_kubernetes_auth.commands, "vault write auth/clusters/exradius/config kubernetes_host=https://198.51.100.40 kubernetes_ca_cert=@cluster-ca.pem token_reviewer_jwt=@reviewer.jwt\n") &&
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
  variables {
    vault = { address = "vault.example.com:8200" }
  }
  expect_failures = [var.vault]
}

run "vault_refuses_a_missing_ca_file" {
  command = plan
  variables {
    vault = { address = "https://vault.example.com:8200", ca_file = "tests/no-such-ca.pem" }
  }
  expect_failures = [var.vault]
}
