# values-generated.yaml: the account's part of the chart's values, given after
# charts/exradius/values-eks.yaml. No secret is in it: bootstrap.sh makes the Secrets.

locals {
  public_api_url = "http://${aws_eip.ingress[0].public_ip}"
  mq_endpoint    = aws_mq_broker.mq.instances[0].endpoints[0]
  mq_host        = regex("^amqps://([^:/]+)", local.mq_endpoint)[0]
  values = merge(
    {
      publicApiUrl = local.public_api_url
      images       = { registry = local.registry, tag = var.image_tag }
      broker       = { url = "${local.mq_endpoint}/", publicHost = local.mq_host }
      objectStore  = { bucket = aws_s3_bucket.objects.bucket, region = var.region }
      builds       = { registry = local.registry }
    },
    var.rwx ? {
      runs = {
        projectVolume = { accessMode = "ReadWriteMany", storageClass = "efs-sc" }
        # NFS has no idmapped mounts: user-namespaced pods cannot mount it.
        userNamespaces = false
      }
    } : {},
  )
}

resource "local_file" "values" {
  filename        = "${path.module}/values-generated.yaml"
  file_permission = "0644"
  content         = "# Written by deploy/terraform/aws (terraform apply). Give it after charts/exradius/values-eks.yaml.\n${yamlencode(local.values)}"
}

output "public_api_url" {
  description = "The panel and the API, once Traefik and the chart are installed."
  value       = local.public_api_url
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "region" {
  value = var.region
}

output "registry" {
  value = local.registry
}

output "push_command" {
  description = "Signs Docker in to ECR and pushes every Exradius image."
  value       = "aws ecr get-login-password --region ${var.region} | docker login --username AWS --password-stdin ${local.registry} && make -C deploy push REGISTRY=${local.registry} TAG=${var.image_tag} PLATFORM=${var.arm64 ? "linux/arm64" : "linux/amd64"}"
}

output "bootstrap" {
  description = "What deploy/terraform/bootstrap.sh needs."
  value = {
    cloud            = "aws"
    kubeconfig       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name} --alias ${var.name}"
    context          = var.name
    vpc_id           = module.vpc.vpc_id
    allowed_cidrs    = var.allowed_cidrs
    eip_allocations  = aws_eip.ingress[*].allocation_id
    ingress_subnets  = module.vpc.public_subnets
    efs_id           = var.rwx ? aws_efs_file_system.projects[0].id : ""
    postgres_secrets = true
    broker_secret    = true
  }
}

output "postgres_uri" {
  description = "The database URL for the Secret exradius-postgres."
  value       = "postgresql://exradius:${random_password.db.result}@${aws_db_instance.db.address}:5432/exradius?ssl=require"
  sensitive   = true
}

output "broker_password" {
  description = "Amazon MQ's password for the user exradius (the Secret exradius-broker)."
  value       = random_password.mq.result
  sensitive   = true
}

output "cleanup_command" {
  description = "Deletes the ECR repositories builds made on push (not in Terraform's state); run before terraform destroy."
  value       = "aws ecr describe-repositories --region ${var.region} --query 'repositories[?starts_with(repositoryName, `exradius/`)].repositoryName' --output text | tr '\\t' '\\n' | xargs -r -n1 aws ecr delete-repository --region ${var.region} --force --repository-name"
}
