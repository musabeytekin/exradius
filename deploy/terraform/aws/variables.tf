variable "region" {
  description = "AWS region."
  type        = string
  default     = "eu-central-1"
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
  description = "Addresses that may reach the cluster's API and the panel's load balancer: your own (curl -4 ifconfig.me)/32. Nothing else is let in."
  type        = list(string)
  validation {
    condition     = length(var.allowed_cidrs) > 0 && !contains(var.allowed_cidrs, "0.0.0.0/0")
    error_message = "Name at least one address, and not 0.0.0.0/0."
  }
}

variable "kubernetes_version" {
  description = "EKS version: 1.36 or later (image volumes and user namespaces are GA there)."
  type        = string
  default     = "1.37"
}

variable "arm64" {
  description = "Graviton (arm64) nodes, the default: images built on an Apple silicon Mac run natively. false: x86_64."
  type        = bool
  default     = true
}

variable "system_instance_type" {
  description = "Instance type of the system node group (the stack: api, worker, panel, operator, egress, Traefik). Empty: t4g.large, or t3.large without arm64."
  type        = string
  default     = ""
}

variable "system_nodes" {
  description = "Nodes of the system group. Two put the egress replicas on two nodes."
  type        = number
  default     = 1
}

variable "run_instance_type" {
  description = "Instance type of the run node group (run and build pods). Empty: m7g.xlarge (4 vCPU, 16 GiB), or m7i.xlarge without arm64."
  type        = string
  default     = ""
}

variable "run_nodes" {
  description = "Nodes of the run group. With more than one, project volumes (ReadWriteOnce) tie a project's runs to one node: see rwx."
  type        = number
  default     = 1
}

variable "db_instance_class" {
  description = "RDS for PostgreSQL instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "mq_instance_type" {
  description = "Amazon MQ for RabbitMQ instance type (RabbitMQ 4 runs on mq.m7g only)."
  type        = string
  default     = "mq.m7g.medium"
}

variable "mq_public" {
  description = "Make the broker reachable from the internet (AMQPS with its user and password; public brokers take no security group), for bridges and the CLI outside the VPC. Off: only the cluster reaches it."
  type        = bool
  default     = false
}

variable "rwx" {
  description = "An EFS file system and the efs-sc storage class, and project volumes ReadWriteMany on it, so runs of one project share their mirrors and caches across run nodes. NFS has no idmapped mounts, so the generated values turn user namespaces off with it."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Tag of the Exradius images pushed with make -C deploy push."
  type        = string
  default     = "0.2.0"
}
