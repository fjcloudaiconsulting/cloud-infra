terraform {
  required_version = "~> 1.17.0"

  required_providers {
    netbird = {
      source  = "netbirdio/netbird"
      version = "~> 0.0.10"
    }
  }

  # State + plan/apply run in HCP Terraform (FlamaCorp/netbird), VCS-driven against cloud-infra, working
  # directory terraform/netbird/. Speculative plans on PR; merges to main await Confirm & Apply.
  # The provider reads NB_PAT (a NetBird service user's access token) from the workspace environment.
  cloud {
    organization = "FlamaCorp"
    workspaces {
      name = "netbird"
    }
  }
}
