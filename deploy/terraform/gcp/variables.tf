variable "project" {
  description = "The Google Cloud project (its id), with billing enabled."
  type        = string
}

variable "region" {
  description = "Region of the registry, the bucket, Cloud SQL and the load balancer's address."
  type        = string
  default     = "europe-west4"
}

variable "zone" {
  description = "Zone of the cluster: a zonal cluster's management fee is covered by GKE's free tier (one per billing account)."
  type        = string
  default     = "europe-west4-a"
}

variable "name" {
  description = "Name of the install: the cluster, and the prefix of every resource."
  type        = string
  default     = "exradius"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name))
    error_message = "Lower-case letters, digits and dashes, at most 21 characters."
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
  description = "Lowest GKE version (Regular channel): 1.36 or later, where image volumes and user namespaces are GA."
  type        = string
  default     = "1.36"
}

variable "arm64" {
  description = "Arm (Tau T2A) nodes, the default: images built on an Apple silicon Mac run natively. false: x86 (E2)."
  type        = bool
  default     = true
}

variable "system_machine_type" {
  description = "Machine type of the system pool (the stack and RabbitMQ). Empty: t2a-standard-2, or e2-standard-2 without arm64."
  type        = string
  default     = ""
}

variable "system_nodes" {
  type    = number
  default = 1
}

variable "run_machine_type" {
  description = "Machine type of the run pool. Empty: t2a-standard-4 (4 vCPU, 16 GB), or e2-standard-4 without arm64."
  type        = string
  default     = ""
}

variable "run_nodes" {
  description = "Nodes of the run pool. With more than one, project volumes (ReadWriteOnce) tie a project's runs to one node: see rwx."
  type        = number
  default     = 1
}

variable "node_image_type" {
  description = "Node image: COS_CONTAINERD (GKE's default, needed for GKE Sandbox) or UBUNTU_CONTAINERD."
  type        = string
  default     = "COS_CONTAINERD"
}

variable "gvisor" {
  description = "GKE Sandbox (gVisor) on the run pool: every run pod in the RuntimeClass gvisor. It excludes user namespaces, which the generated values turn off."
  type        = bool
  default     = false
}

variable "db_tier" {
  description = "Cloud SQL machine tier (Enterprise edition; db-f1-micro and db-g1-small are shared-core)."
  type        = string
  default     = "db-f1-micro"
}

variable "rwx" {
  description = "The Filestore CSI driver and project volumes ReadWriteMany on standard-rwx (Filestore Basic HDD, 1 TiB minimum, about $0.28 an hour). NFS has no idmapped mounts, so the generated values turn user namespaces off with it."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Tag of the Exradius images pushed with make -C deploy push."
  type        = string
  default     = "0.2.0"
}
