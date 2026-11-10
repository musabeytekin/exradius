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
