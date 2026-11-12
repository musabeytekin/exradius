variable "subscription_id" {
  description = "The Azure subscription (az account show --query id -o tsv)."
  type        = string
}

variable "location" {
  description = "Azure region."
  type        = string
  default     = "westeurope"
}

variable "name" {
  description = "Name of the install: the cluster, the resource group, and the prefix of every resource."
  type        = string
  default     = "exradius"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,14}$", var.name))
    error_message = "Lower-case letters, digits and dashes, at most 15 characters (storage account names are short)."
  }
}

variable "allowed_cidrs" {
  description = "Addresses that may reach the cluster's API and the panel's load balancer: your own (curl -4 ifconfig.me)/32."
  type        = list(string)
  validation {
    condition     = length(var.allowed_cidrs) > 0 && !contains(var.allowed_cidrs, "0.0.0.0/0")
    error_message = "Name at least one address, and not 0.0.0.0/0."
  }
}

variable "kubernetes_version" {
  description = "AKS version: 1.36 or later (image volumes and user namespaces are GA there)."
  type        = string
  default     = "1.36"
}

variable "arm64" {
  description = "Arm (Dpsv5, Ampere Altra) nodes, the default: images built on an Apple silicon Mac run natively. false: x86 (Dsv5)."
  type        = bool
  default     = true
}

variable "system_vm_size" {
  description = "VM size of the system pool. Empty: Standard_D2ps_v5, or Standard_D2s_v5 without arm64."
  type        = string
  default     = ""
}

variable "system_nodes" {
  type    = number
  default = 1
}

variable "run_vm_size" {
  description = "VM size of the run pool. Empty: Standard_D4ps_v5 (4 vCPU, 16 GiB), or Standard_D4s_v5 without arm64."
  type        = string
  default     = ""
}

variable "run_nodes" {
  description = "Nodes of the run pool. With more than one, project volumes (ReadWriteOnce) tie a project's runs to one node: see rwx."
  type        = number
  default     = 1
}

variable "db_sku" {
  description = "Azure Database for PostgreSQL flexible server SKU."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "rwx" {
  description = "Project volumes ReadWriteMany on AKS's azurefile-csi class (SMB). No idmapped mounts there, so the generated values turn user namespaces off with it."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Tag of the Exradius images pushed with make -C deploy push."
  type        = string
  default     = "0.2.0"
}

variable "vault" {
  description = <<-EOT
    An existing HashiCorp Vault for secret stores of kind vault (docs/config-repo.md §7.4).
    Exradius does not install or operate Vault: this points the api and the worker at one the
    organisation runs, inside the cluster or outside it. null (the default): no Vault, and
    values-generated.yaml is as without it.
    address: https://vault.example.com:8200. auth_mount and role: where Vault's Kubernetes
    auth method for this cluster is enabled, and the role the api and the worker sign in as.
    namespace: a Vault Enterprise or HCP Vault namespace. audience: set it (vault is
    recommended) and the role's audience to the same, so the pods send a projected token that
    only Vault accepts; empty sends their own ServiceAccount token, which the Kubernetes API
    accepts too. ca_file: a PEM file with the CA of a private Vault's certificate, relative to
    this directory or absolute; bootstrap.sh makes the ConfigMap exradius-vault-ca from it.
    token_reviewer: true when Vault runs outside the cluster and the role has an audience:
    the chart then makes a ServiceAccount exradius-vault-reviewer allowed to review tokens,
    whose token Vault uses for that (output vault_kubernetes_auth says how to read it).
  EOT
  type = object({
    address        = string
    auth_mount     = optional(string, "kubernetes")
    role           = optional(string, "exradius")
    namespace      = optional(string, "")
    audience       = optional(string, "")
    ca_file        = optional(string, "")
    token_reviewer = optional(bool, false)
  })
  default = null
  validation {
    condition = var.vault == null ? true : (
      can(regex("^https?://[A-Za-z0-9.-]+(:[0-9]+)?/?$", var.vault.address)) &&
      can(regex("^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$", var.vault.auth_mount)) &&
      can(regex("^[A-Za-z0-9_.@-]+$", var.vault.role)) &&
      can(regex("^[A-Za-z0-9_./-]*$", var.vault.namespace)) &&
      can(regex("^[A-Za-z0-9_.:/-]*$", var.vault.audience))
    )
    error_message = "address is http(s)://host[:port]; auth_mount, role, namespace and audience are plain names or paths."
  }
  validation {
    condition     = var.vault == null ? true : (var.vault.ca_file == "" || fileexists(var.vault.ca_file))
    error_message = "ca_file names no file (relative to this directory, or absolute)."
  }
}
