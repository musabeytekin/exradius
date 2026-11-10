# Exradius on Amazon EKS (docs/kubernetes.md §16, §28): a VPC, the cluster with a system and
# a tainted run node group, ECR, an S3 bucket, RDS for PostgreSQL, Amazon MQ for RabbitMQ,
# optional EFS, the Pod Identity of the api, the worker and build pods, two Elastic IPs for
# the panel's load balancer, and values-generated.yaml for helm.
#
#   terraform init && terraform apply -var 'allowed_cidrs=["<your address>/32"]'
#   ../bootstrap.sh aws       # kubeconfig, the load balancer controller, Traefik, Secrets
#   make -C ../.. push ...    # the images (terraform output push_command)
#   helm upgrade --install exradius ../../../charts/exradius -n exradius \
#     -f ../../../charts/exradius/values-eks.yaml -f values-generated.yaml
#
# Nodes sit in public subnets with public addresses and no inbound rule from outside, so no
# NAT gateway is paid for; RDS and the broker sit in private subnets.

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

locals {
  azs           = slice(data.aws_availability_zones.available.names, 0, 2)
  system_type   = var.system_instance_type != "" ? var.system_instance_type : (var.arm64 ? "t4g.large" : "t3.large")
  run_type      = var.run_instance_type != "" ? var.run_instance_type : (var.arm64 ? "m7g.xlarge" : "m7i.xlarge")
  ami_type      = var.arm64 ? "AL2023_ARM_64_STANDARD" : "AL2023_x86_64_STANDARD"
  account       = data.aws_caller_identity.current.account_id
  registry      = "${local.account}.dkr.ecr.${var.region}.amazonaws.com"
  stack_images  = ["exradius-backend", "exradius-frontend", "exradius-operator", "exradius-egress", "exradius-builder", "exradius-runtime-claude-code", "exradius-project-default"]
  node_root_gib = 50
}

# -- network -----------------------------------------------------------------------------

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7"

  name = var.name
  cidr = "10.40.0.0/16"
  azs  = local.azs

  public_subnets          = ["10.40.0.0/20", "10.40.16.0/20"]
  map_public_ip_on_launch = true
  # The load balancer controller places internet-facing load balancers here.
  public_subnet_tags = { "kubernetes.io/role/elb" = "1" }

  private_subnets    = ["10.40.128.0/22", "10.40.132.0/22"]
  enable_nat_gateway = false
}

# -- cluster -----------------------------------------------------------------------------

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.29"

  name               = var.name
  kubernetes_version = var.kubernetes_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.public_subnets

  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.allowed_cidrs
  # Whoever runs terraform apply administers the cluster (an EKS access entry).
  enable_cluster_creator_admin_permissions = true

  addons = {
    # NetworkPolicy enforcement (docs/kubernetes.md §11) is the VPC CNI's network policy
    # agent.
    vpc-cni = {
      before_compute       = true
      configuration_values = jsonencode({ enableNetworkPolicy = "true" })
    }
    eks-pod-identity-agent = { before_compute = true }
    kube-proxy             = {}
    coredns                = {}
    aws-ebs-csi-driver = {
      pod_identity_association = [{ role_arn = module.ebs_csi.iam_role_arn, service_account = "ebs-csi-controller-sa" }]
    }
  }

  eks_managed_node_groups = {
    system = {
      ami_type       = local.ami_type
      instance_types = [local.system_type]
      min_size       = var.system_nodes
      max_size       = var.system_nodes
      desired_size   = var.system_nodes
      labels         = { "exradius.dev/pool" = "system" }
      block_device_mappings = {
        root = { device_name = "/dev/xvda", ebs = { volume_size = local.node_root_gib, volume_type = "gp3", delete_on_termination = true } }
      }
    }
    runs = {
      ami_type       = local.ami_type
      instance_types = [local.run_type]
      min_size       = var.run_nodes
      max_size       = var.run_nodes
      desired_size   = var.run_nodes
      labels         = { "exradius.dev/pool" = "runs" }
      # Only run and build pods (values runs.tolerations), and DaemonSets that tolerate it.
      taints = {
        runs = { key = "exradius.dev/runs", value = "true", effect = "NO_SCHEDULE" }
      }
      block_device_mappings = {
        root = { device_name = "/dev/xvda", ebs = { volume_size = 100, volume_type = "gp3", delete_on_termination = true } }
      }
    }
  }
}

# The EBS CSI driver provisions workspaces and project volumes; bootstrap.sh adds gp3 as the
# default storage class (new EKS clusters have none).
module "ebs_csi" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 2.10"

  name                      = "${var.name}-ebs-csi"
  attach_aws_ebs_csi_policy = true
}

module "lb_controller" {
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 2.10"

  name                            = "${var.name}-lb-controller"
  attach_aws_lb_controller_policy = true
  associations = {
    controller = {
      cluster_name    = module.eks.cluster_name
      namespace       = "kube-system"
      service_account = "aws-load-balancer-controller"
    }
  }
}

# The panel's load balancer (Traefik's Service, a Network Load Balancer made by the load
# balancer controller) takes these addresses, one per public subnet, so publicApiUrl is
# known before anything is installed.
resource "aws_eip" "ingress" {
  count  = length(local.azs)
  domain = "vpc"
  tags   = { Name = "${var.name}-ingress-${count.index}" }
}
