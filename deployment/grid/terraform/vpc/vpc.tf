# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# Licensed under the Apache License, Version 2.0 https://aws.amazon.com/apache-2-0/

data "aws_availability_zones" "available" {}

module "vpc" {
  source = "terraform-aws-modules/vpc/aws"
  version = "3.1.0"
  name = "${var.cluster_name}-vpc"
  cidr = "10.0.0.0/16"
  azs = data.aws_availability_zones.available.names
  #private_subnets      = ["10.0.0.0/20","10.0.32.0/20", "10.0.64.0/20"]
  #public_subnets       = ["10.0.130.0/24", "10.0.131.0/24", "10.0.132.0/24"]
  private_subnets = var.private_subnets
  public_subnets = var.public_subnets
  enable_nat_gateway = !var.enable_private_subnet
  single_nat_gateway = !var.enable_private_subnet
  # required for private endpoint
  enable_dns_hostnames = true
  enable_dns_support = true
  tags = {
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }

  public_subnet_tags = {
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
    "kubernetes.io/role/elb"                      = "1"
  }

  private_subnet_tags = {
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
    "kubernetes.io/role/internal-elb"             = "1"
  }
}

module "vpc_endpoints" {
  source = "terraform-aws-modules/vpc/aws//modules/vpc-endpoints"
  vpc_id             = module.vpc.vpc_id
  security_group_ids = [module.vpc.default_security_group_id]
  create = true
  endpoints = {
    sqs = {
      service = "sqs"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }
    s3 = {
      service = "s3"
      service_type    = "Gateway"
      route_table_ids = flatten([module.vpc.intra_route_table_ids, module.vpc.private_route_table_ids, module.vpc.public_route_table_ids])
    }
    dynamodb = {
      service = "dynamodb"
      service_type    = "Gateway"
      route_table_ids = flatten([module.vpc.intra_route_table_ids, module.vpc.private_route_table_ids, module.vpc.public_route_table_ids])
    }
    ec2_autoscaling = {
      service = "autoscaling"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }
    ec2 = {
      service = "ec2"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }
    ecr_dkr = {
      service = "ecr.dkr"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }
    ecr_api = {
      service = "ecr.api"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }
    monitoring = {
      service = "monitoring"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }
    logs = {
      service = "logs"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }

    elasticloadbalancing = {
      service = "elasticloadbalancing"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }

    api_gateway = {
      service = "execute-api"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }

    ssm = {
      service = "ssm"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }

    ssmmessages = {
      service = "ssmmessages"
      private_dns_enabled = var.enable_private_subnet
      subnet_ids = var.enable_private_subnet == true ? module.vpc.private_subnets : []
      security_group_ids = var.enable_private_subnet == true ? [module.vpc.default_security_group_id] : []
    }
  }
}



data "aws_vpc" "selected" {
  id = module.vpc.vpc_id
}

resource "aws_security_group_rule" "https" {
  type              = "ingress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = [data.aws_vpc.selected.cidr_block]
  security_group_id = module.vpc.default_security_group_id
}
