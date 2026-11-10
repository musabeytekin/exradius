# What the chart's values point at: ECR, the bucket, RDS, Amazon MQ, optional EFS, and the
# Pod Identity of the api, the worker and build pods.

# -- registry ----------------------------------------------------------------------------

# The Exradius images (make -C deploy push REGISTRY=<registry>).
resource "aws_ecr_repository" "stack" {
  for_each             = toset(local.stack_images)
  name                 = each.value
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

# Images built from projects' Dockerfiles go to exradius/<project>/<name>; ECR creates each
# repository on its first push.
resource "aws_ecr_repository_creation_template" "builds" {
  prefix               = "exradius"
  description          = "Exradius builds: exradius/<project>/<name>"
  applied_for          = ["CREATE_ON_PUSH"]
  image_tag_mutability = "MUTABLE"
}

# -- object store ------------------------------------------------------------------------

resource "aws_s3_bucket" "objects" {
  bucket        = "${var.name}-${local.account}-${var.region}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "objects" {
  bucket                  = aws_s3_bucket.objects.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -- PostgreSQL --------------------------------------------------------------------------

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "db" {
  name       = var.name
  subnet_ids = module.vpc.private_subnets
}

locals {
  # Pods take their node's security groups (the VPC CNI without security groups for pods).
  node_security_groups = [module.eks.node_security_group_id, module.eks.cluster_primary_security_group_id]
}

resource "aws_security_group" "db" {
  name   = "${var.name}-postgres"
  vpc_id = module.vpc.vpc_id
  ingress {
    description     = "PostgreSQL from the cluster nodes and pods"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = local.node_security_groups
  }
}

resource "aws_db_instance" "db" {
  identifier              = var.name
  engine                  = "postgres"
  engine_version          = "17"
  instance_class          = var.db_instance_class
  allocated_storage       = 20
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "exradius"
  username                = "exradius"
  password                = random_password.db.result
  db_subnet_group_name    = aws_db_subnet_group.db.name
  vpc_security_group_ids  = [aws_security_group.db.id]
  publicly_accessible     = false
  backup_retention_period = 1
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true
}

# -- RabbitMQ ----------------------------------------------------------------------------

resource "random_password" "mq" {
  length  = 32
  special = false
}

resource "aws_security_group" "mq" {
  count  = var.mq_public ? 0 : 1
  name   = "${var.name}-rabbitmq"
  vpc_id = module.vpc.vpc_id
  ingress {
    description     = "AMQPS from the cluster nodes and pods"
    from_port       = 5671
    to_port         = 5671
    protocol        = "tcp"
    security_groups = local.node_security_groups
  }
}

resource "aws_mq_broker" "mq" {
  broker_name                = var.name
  engine_type                = "RabbitMQ"
  engine_version             = "4.2"
  host_instance_type         = var.mq_instance_type
  deployment_mode            = "SINGLE_INSTANCE"
  publicly_accessible        = var.mq_public
  subnet_ids                 = var.mq_public ? null : [module.vpc.private_subnets[0]]
  security_groups            = var.mq_public ? null : [aws_security_group.mq[0].id]
  auto_minor_version_upgrade = true
  apply_immediately          = true
  user {
    username = "exradius"
    password = random_password.mq.result
  }
}

# -- optional RWX storage ----------------------------------------------------------------

resource "aws_efs_file_system" "projects" {
  count     = var.rwx ? 1 : 0
  encrypted = true
  tags      = { Name = "${var.name}-projects" }
}

resource "aws_security_group" "efs" {
  count  = var.rwx ? 1 : 0
  name   = "${var.name}-efs"
  vpc_id = module.vpc.vpc_id
  ingress {
    description     = "NFS from the cluster nodes"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = local.node_security_groups
  }
}

resource "aws_efs_mount_target" "projects" {
  count           = var.rwx ? length(local.azs) : 0
  file_system_id  = aws_efs_file_system.projects[0].id
  subnet_id       = module.vpc.public_subnets[count.index]
  security_groups = [aws_security_group.efs[0].id]
}

module "efs_csi" {
  count   = var.rwx ? 1 : 0
  source  = "terraform-aws-modules/eks-pod-identity/aws"
  version = "~> 2.10"

  name                      = "${var.name}-efs-csi"
  attach_aws_efs_csi_policy = true
}

resource "aws_eks_addon" "efs_csi" {
  count        = var.rwx ? 1 : 0
  cluster_name = module.eks.cluster_name
  addon_name   = "aws-efs-csi-driver"
  pod_identity_association {
    role_arn        = module.efs_csi[0].iam_role_arn
    service_account = "efs-csi-controller-sa"
  }
  depends_on = [module.eks]
}

# -- Pod Identity ------------------------------------------------------------------------

data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

locals {
  builds_repositories = "arn:aws:ecr:${var.region}:${local.account}:repository/exradius/*"
}

# The api and the worker: the bucket; the worker also reads built images' configs.
resource "aws_iam_role" "app" {
  name               = "${var.name}-app"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

data "aws_iam_policy_document" "app" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.objects.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.objects.arn}/*"]
  }
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [local.builds_repositories]
  }
}

resource "aws_iam_role_policy" "app" {
  role   = aws_iam_role.app.id
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_eks_pod_identity_association" "app" {
  for_each        = toset(["exradius-api", "exradius-worker"])
  cluster_name    = module.eks.cluster_name
  namespace       = "exradius"
  service_account = each.value
  role_arn        = aws_iam_role.app.arn
}

# Build pods: push to exradius/* (and create those repositories on the first push), nothing
# else; never the Exradius images or the bucket.
resource "aws_iam_role" "build" {
  name               = "${var.name}-build"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

data "aws_iam_policy_document" "build" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions = [
      "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
      "ecr:CreateRepository",
    ]
    resources = [local.builds_repositories]
  }
}

resource "aws_iam_role_policy" "build" {
  role   = aws_iam_role.build.id
  policy = data.aws_iam_policy_document.build.json
}

resource "aws_eks_pod_identity_association" "build" {
  cluster_name    = module.eks.cluster_name
  namespace       = "exradius-builds"
  service_account = "exradius-build"
  role_arn        = aws_iam_role.build.arn
}
