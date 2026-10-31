terraform {
  required_version = "~> 1.16.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.26"
    }
    tfe = {
      source  = "hashicorp/tfe"
      version = "~> 0.83"
    }
  }

  cloud {
    organization = "FlamaCorp"
    workspaces {
      name = "cloudflare-tokens"
    }
  }
}

# Cloudflare API tokens as code (INFRA-133): every account-owned token of the account lives here, so a scope
# change is a PR whose plan shows the policy diff. Tokens this stack creates have their value written straight to
# the consumer; imported tokens keep the value their consumer already holds.
#
# Auth, env vars on this workspace (docs/configuration-map.md, "HCP Terraform"):
# - CLOUDFLARE_API_TOKEN (sensitive): the bootstrap token, account-owned, Account API Tokens: Edit only, made by
#   hand once. It can mint any token, so it is effectively an account admin: only this workspace holds it, never
#   `cloudflare`, whose PR plans would otherwise run with it. It is not managed here: a workspace never manages
#   its own credential.
# - TFE_TOKEN (sensitive): token of team `cloudflare-tokens`, which can only read runs and read and write the
#   variables of workspace `cloudflare`.
provider "cloudflare" {}

provider "tfe" {
  organization = "FlamaCorp"
}

locals {
  account_id = "fb5eb042529f2621c6de3f34c6801174"

  # Hardcoded: the bootstrap token cannot read zones.
  zones = {
    tbd      = "5afce02a48d33e45b8119b34c88b4a2a" # thebetterdecision.com
    ziftbook = "d548b66e2e1b8793451fd7d9f5099430" # ziftbook.com
    fjdev    = "ff907e2dae27da386ae2f24dd51e76b2" # fjconsulting.dev
  }

  # Permission group ids are global and survive renames (the dashboard's "Single Redirect" is the API's
  # "Dynamic URL Redirects"). A wrong id fails the apply with a 400 before any consumer is touched.
  pg = {
    "DNS Write"                   = "4755a26eedb94da69e1066d98aa820be"
    "Dynamic URL Redirects Write" = "74e1036f577a48528b78d2413b40538d"
    "SSL and Certificates Write"  = "c03055bc037c4ea9afb9a9f104b7b721"
    "Zone Read"                   = "c8fed203ed3043cba015a93ad1616f1f"
    "Zone Settings Write"         = "3030687196b94b638145a3953da2b699"
    "Zone WAF Write"              = "fb6778dc191143babbfaa57993f1d275"
    "Zone Write"                  = "e6d2666161e84845a636613608cee8d5"
  }

  # Looked up by name, not hardcoded: the id of "Access: Apps and Policies Write" is not one this repo has seen
  # (INFRA-117). A rename (no match) or an ambiguous match makes `one()` fail the plan, never the apply.
  access_write_ids = [for g in data.cloudflare_account_api_token_permission_groups_list.access_write.result : g.id
  if g.name == "Access: Apps and Policies Write" && contains(g.scopes, "com.cloudflare.api.account")]

  # Same lookup for "Cloudflare One Connector: cloudflared Write" (INFRA-165): create a tunnel, write its config, read its
  # token. Narrower than the legacy "Cloudflare Tunnel Write", which Cloudflare docs list as equivalent for those calls.
  tunnel_write_ids = [for g in data.cloudflare_account_api_token_permission_groups_list.access_write.result : g.id
  if g.name == "Cloudflare One Connector: cloudflared Write" && contains(g.scopes, "com.cloudflare.api.account")]

  # Same lookup for "Workers R2 Storage Write" (ZIF-133): create R2 buckets and set their CORS, lifecycle and custom
  # domains. No narrower group can create a bucket, and this one also reads, writes and deletes objects in every
  # bucket of the account. The apps use their own bucket-scoped tokens, made by hand per environment.
  r2_write_ids = [for g in data.cloudflare_account_api_token_permission_groups_list.access_write.result : g.id
  if g.name == "Workers R2 Storage Write" && contains(g.scopes, "com.cloudflare.api.account")]

  # The provider stores the API's `resources` bytes as they come back, and jsonencode sorts keys, so a `resources`
  # object with two keys at one level can come back in another order and fail the apply. Keep one key per level:
  # one policy per zone. The provider also matches the API's policies back by effect and permission group set, so
  # two zones with identical group lists could swap resources: keep their lists different (or merge the zones).
  cloudflare_workspace_policies = {
    # Managed zone: settings, records, rate limit (INFRA-123), www redirect rule (INFRA-61).
    tbd = ["Zone Write", "Zone Settings Write", "DNS Write", "SSL and Certificates Write", "Zone WAF Write", "Dynamic URL Redirects Write"]
    # Zone read by a data source; its settings, `dev` record, rate limit and www redirect rule (INFRA-131) are managed.
    ziftbook = ["Zone Read", "Zone Settings Write", "DNS Write", "SSL and Certificates Write", "Zone WAF Write", "Dynamic URL Redirects Write"]
    # Zone read by a data source; only the shared dev Mailgun records `m.` are managed (INFRA-47).
    fjdev = ["Zone Read", "DNS Write"]
  }
}

# Unfiltered: the API's `name` filter wants a URL-encoded value, so the exact match happens in access_write_ids.
data "cloudflare_account_api_token_permission_groups_list" "access_write" {
  account_id = local.account_id
}

# Token of the `cloudflare` workspace. It replaces the hand-made token `cloudflare-tfc` (left out of the imports and
# deleted by hand once `cloudflare` plans clean with this one) with the same scopes,
# narrowed from "all zones" to the three zones Terraform uses. A new zone needs a policy here first (creating a zone
# needs account-wide Zone Write, which this token no longer has).
resource "cloudflare_account_token" "cloudflare_workspace" {
  account_id = local.account_id
  name       = "tf: cloudflare workspace"

  policies = concat(
    [for zone, groups in local.cloudflare_workspace_policies : {
      effect            = "allow"
      resources         = jsonencode({ "com.cloudflare.api.account.zone.${local.zones[zone]}" = "*" })
      permission_groups = [for g in groups : { id = local.pg[g] }]
    }],
    [{
      # Cloudflare Access application and policies gating dev.thebetterdecision.com (INFRA-117). Account scope: reusable
      # policies are account objects. Needs Zero Trust enabled on the account first (owner step, docs/runbooks.md).
      effect            = "allow"
      resources         = jsonencode({ "com.cloudflare.api.account.${local.account_id}" = "*" })
      permission_groups = [{ id = one(local.access_write_ids) }]
      }, {
      # Cloudflare Tunnels managed by the `cloudflare` workspace (INFRA-165, Hetzner migration INFRA-164). Account scope.
      effect            = "allow"
      resources         = jsonencode({ "com.cloudflare.api.account.${local.account_id}" = "*" })
      permission_groups = [{ id = one(local.tunnel_write_ids) }]
      }, {
      # R2 buckets for Ziftbook service photos (ZIF-133), managed by the `cloudflare` workspace. Account scope.
      effect            = "allow"
      resources         = jsonencode({ "com.cloudflare.api.account.${local.account_id}" = "*" })
      permission_groups = [{ id = one(local.r2_write_ids) }]
    }],
  )

  # A replace (rotation) creates the new token and rewrites the variable before the old token is deleted.
  lifecycle {
    create_before_destroy = true
  }
}

data "tfe_workspace" "cloudflare" {
  name = "cloudflare"
}

# Adopts the variable the owner created by hand; the first apply overwrites its value.
import {
  to = tfe_variable.cloudflare_workspace_token
  id = "FlamaCorp/cloudflare/var-HBAjSYdmBUbyJFeR"
}

resource "tfe_variable" "cloudflare_workspace_token" {
  workspace_id = data.tfe_workspace.cloudflare.id
  category     = "env"
  key          = "CLOUDFLARE_API_TOKEN"
  value        = cloudflare_account_token.cloudflare_workspace.value
  sensitive    = true
  description  = "Managed by workspace cloudflare-tokens (INFRA-133)"
}

# Every other account-owned token, adopted as it is: imported-tokens.json is the API's own view of each token
# (`resources` kept as the exact string the API returns), written by the bootstrap script. Their values stay with
# their consumers; a replace would mint a value nothing receives, so wire a consumer here before rotating one.
locals {
  imported = jsondecode(file("${path.module}/imported-tokens.json"))
}

import {
  for_each = local.imported
  to       = cloudflare_account_token.imported[each.key]
  id       = "${local.account_id}/${each.value.id}"
}

resource "cloudflare_account_token" "imported" {
  for_each = local.imported

  account_id = local.account_id
  name       = each.value.name
  expires_on = try(each.value.expires_on, null)
  not_before = try(each.value.not_before, null)
  condition  = try(each.value.condition, null)

  policies = [for p in each.value.policies : {
    effect            = p.effect
    resources         = p.resources
    permission_groups = [for g in p.permission_groups : { id = g.id }]
  }]

  # These belong to other consumers (another project's CI among them): a dropped or renamed entry, or a replace,
  # would delete a token whose value nothing here can redeliver. To stop managing one: `moved` it to a standalone
  # resource, then `removed` that with destroy = false (docs/runbooks.md, "Cloudflare API tokens").
  lifecycle {
    prevent_destroy = true
  }
}
