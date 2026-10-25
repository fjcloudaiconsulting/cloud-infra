terraform {
  required_version = "~> 1.16.0"

  # ⚠⚠ THIS IS A DIFFERENT AWS ACCOUNT FROM terraform/tbd-apex/.
  # apex (the public landing site) lives in the operator's older account; this
  # workspace targets the company account 884686184019, which was empty at
  # genesis. Do not copy an account id between the two.
  #
  # State + plan/apply run in Terraform Cloud (FlamaCorp/tbd-backups),
  # VCS-driven against cloud-infra, working directory terraform/tbd-backups/,
  # trigger patterns terraform/tbd-backups/** and aws/bootstrap/tfc-backups-*. Speculative plans on PR; merges
  # to main create runs awaiting manual Confirm & Apply. Auto-apply is off,
  # matching FlamaCorp/tbd and FlamaCorp/tbd-apex.
  #
  # ⚠ WHY A SEPARATE WORKSPACE (TBD-400): the AWS provider validates credentials at configure time, so an AWS auth
  # problem fails the whole run of any workspace that holds it. The backup chain therefore stays apart from the
  # workspaces whose repair path must not depend on it. (The DigitalOcean workspace it was separated from was retired, INFRA-49.)
  #
  # ⚠ RENAMING THIS WORKSPACE IS NOT JUST AN EDIT HERE. The name is also the
  # AWS trust boundary: it appears in the `app.terraform.io:sub` condition of
  # aws/bootstrap/tfc-backups-trust.json, which is managed BY this
  # workspace. Renaming without widening first denies
  # AssumeRoleWithWebIdentity, and the workspace then cannot apply its own fix
  # (that is TBD-372, which cost an out-of-band trust-policy edit on
  # 2026-08-11). Procedure: ADD A SECOND STATEMENT naming the new workspace,
  # apply, rename, update var.tfc_workspace_name, apply, then delete the old
  # statement. Do not widen by globbing. NEVER rename first.
  # .github/scripts/check-tbd-backups-fences.py fences the two against each other
  # at PR time, which is what prevents the event rather than easing recovery.
  cloud {
    organization = "FlamaCorp"
    workspaces {
      name = "tbd-backups"
    }
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.67"
    }
  }
}
