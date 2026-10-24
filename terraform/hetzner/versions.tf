terraform {
  required_version = "~> 1.17.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.70"
    }
  }

  # State + plan/apply run in HCP Terraform (FlamaCorp/hetzner), VCS-driven against cloud-infra, working
  # directory terraform/hetzner/. Speculative plans on PR; merges to main await Confirm & Apply.
  # The provider reads HCLOUD_TOKEN from the workspace environment.
  cloud {
    organization = "FlamaCorp"
    workspaces {
      name = "hetzner"
    }
  }
}
