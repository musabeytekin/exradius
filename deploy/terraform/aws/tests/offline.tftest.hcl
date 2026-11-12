# terraform test: a plan and an apply of this module against mocked providers, so the
# expressions, counts and the generated values are checked without an AWS account.

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-central-1a", "eu-central-1b", "eu-central-1c"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012", arn = "arn:aws:iam::123456789012:user/installer" }
  }
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::123456789012:user/installer" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_eks_addon_version" {
    defaults = { version = "v1.20.0-eksbuild.1" }
  }
  mock_resource "aws_launch_template" {
    defaults = { id = "lt-0123456789abcdef0", arn = "arn:aws:ec2:eu-central-1:123456789012:launch-template/lt-0123456789abcdef0", latest_version = 1 }
  }
  mock_resource "aws_mq_broker" {
    defaults = {
      instances = [{ console_url = "", ip_address = "", endpoints = ["amqps://b-1234.mq.eu-central-1.amazonaws.com:5671"] }]
    }
  }
  mock_resource "aws_eip" {
    defaults = { public_ip = "203.0.113.10", allocation_id = "eipalloc-0123" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/mock" }
  }
  mock_resource "aws_eks_cluster" {
    defaults = {
      arn                   = "arn:aws:eks:eu-central-1:123456789012:cluster/exradius"
      endpoint              = "https://ABC.gr7.eu-central-1.eks.amazonaws.com"
      identity              = [{ oidc = [{ issuer = "https://oidc.eks.eu-central-1.amazonaws.com/id/ABC" }] }]
      certificate_authority = [{ data = "Y2E=" }]
    }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::exradius-123456789012-eu-central-1" }
  }
  mock_resource "aws_db_instance" {
    defaults = { address = "exradius.abc.eu-central-1.rds.amazonaws.com" }
  }
}

mock_provider "tls" {}
mock_provider "time" {}
mock_provider "null" {}
mock_provider "cloudinit" {}
mock_provider "local" {}

variables {
  allowed_cidrs = ["198.51.100.7/32"]
}

run "defaults" {
  command = apply

  assert {
    condition     = output.public_api_url == "http://203.0.113.10"
    error_message = "publicApiUrl is the first Elastic IP"
  }
  assert {
    condition     = local.values.broker.url == "amqps://b-1234.mq.eu-central-1.amazonaws.com:5671/" && local.values.broker.publicHost == "b-1234.mq.eu-central-1.amazonaws.com"
    error_message = "the broker URL and host come from Amazon MQ's endpoint"
  }
  assert {
    condition     = local.values.images.registry == "123456789012.dkr.ecr.eu-central-1.amazonaws.com" && local.values.builds.registry == local.values.images.registry
    error_message = "ECR is the registry of the images and of builds"
  }
  assert {
    condition     = !contains(keys(local.values), "runs")
    error_message = "without rwx the run settings stay values-eks.yaml's"
  }
  assert {
    condition     = length(aws_ecr_repository.stack) == 7 && length(aws_eip.ingress) == 2
    error_message = "seven image repositories and an address per subnet"
  }
  assert {
    condition     = aws_mq_broker.mq.publicly_accessible == false && length(aws_security_group.mq) == 1
    error_message = "a private broker behind its security group by default"
  }
  assert {
    condition     = strcontains(output.push_command, "PLATFORM=linux/arm64")
    error_message = "Graviton by default"
  }
}

run "rwx_public_broker_x86" {
  command = apply
  variables {
    rwx       = true
    mq_public = true
    arm64     = false
  }

  assert {
    condition     = local.values.runs.projectVolume.accessMode == "ReadWriteMany" && local.values.runs.userNamespaces == false
    error_message = "EFS project volumes, without user namespaces"
  }
  assert {
    condition     = length(aws_efs_mount_target.projects) == 2 && length(aws_security_group.mq) == 0
    error_message = "a mount target per subnet; a public broker has no security group"
  }
  assert {
    condition     = strcontains(output.push_command, "PLATFORM=linux/amd64")
    error_message = "x86_64 images"
  }
}

run "refuses_an_open_cluster" {
  command = plan
  variables {
    allowed_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.allowed_cidrs]
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
      output.vault_kubernetes_auth.kubernetes_host == "https://ABC.gr7.eu-central-1.eks.amazonaws.com" &&
      output.vault_kubernetes_auth.kubernetes_ca_cert == "ca" &&
      output.vault_kubernetes_auth.auth_mount == "clusters/exradius" &&
      output.vault_kubernetes_auth.role == "exradius-prod" &&
      output.vault_kubernetes_auth.namespace == "exradius" &&
      output.vault_kubernetes_auth.service_accounts == ["exradius-api", "exradius-worker"] &&
      output.vault_kubernetes_auth.audience == "vault"
    )
    error_message = "Vault's admin gets the cluster's API endpoint and decoded CA, the mount, role, namespace, ServiceAccounts and audience"
  }
  assert {
    condition     = strcontains(output.vault_kubernetes_auth.token_reviewer_jwt, "get secret exradius-vault-reviewer")
    error_message = "with token_reviewer, how to read the reviewer's token"
  }
  assert {
    condition = (
      strcontains(output.vault_kubernetes_auth.commands, "export VAULT_NAMESPACE=admin/platform\n") &&
      strcontains(output.vault_kubernetes_auth.commands, "vault auth enable -path=clusters/exradius kubernetes\n") &&
      strcontains(output.vault_kubernetes_auth.commands, "vault write auth/clusters/exradius/config kubernetes_host=https://ABC.gr7.eu-central-1.eks.amazonaws.com kubernetes_ca_cert=@cluster-ca.pem token_reviewer_jwt=@reviewer.jwt\n") &&
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
