terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.28 added ECR repository creation templates that create on push.
      version = "~> 6.28"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { "exradius.dev/install" = var.name }
  }
}
