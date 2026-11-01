terraform {
  required_version = "~> 1.17.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.26"
    }
  }

  cloud {
    organization = "FlamaCorp"
    workspaces {
      name = "cloudflare"
    }
  }
}

# Auth: CLOUDFLARE_API_TOKEN, a sensitive env var on the TFC workspace, written by workspace cloudflare-tokens
# (INFRA-133). Its scopes live in terraform/cloudflare-tokens/main.tf, one policy per zone this stack touches
# (thebetterdecision.com, ziftbook.com, fjconsulting.dev) plus Access, Tunnel (cloudflared) and R2 write on the account. A zone missing there
# fails here only at apply; creating a new zone needs account-wide Zone Write, which the token does not have.
provider "cloudflare" {}

variable "account_id" {
  description = "Cloudflare account that owns the zones."
  type        = string
}

# thebetterdecision.com moves here from Route 53 in the old AWS account (INFRA-15).
# Every record below is a 1:1 copy of the Route 53 export taken 2026-10-01 and DNS-only
# (proxied = false), except `app` (INFRA-48) and `dev` (INFRA-67), proxied through the Hetzner tunnel since INFRA-175 and INFRA-173, and `www`, proxied for its redirect (INFRA-61).
# The apex is the Worker `tbd-landing` since INFRA-61: a custom domain attached by hand, whose own read-only
# record is not managed here (docs/configuration-map.md, Worker and Snippet access).
resource "cloudflare_zone" "tbd" {
  account = { id = var.account_id }
  name    = "thebetterdecision.com"
  type    = "full"

  lifecycle {
    prevent_destroy = true
  }
}

locals {
  tbd_records = {
    # www: proxied only so the redirect rule `tbd_redirects` below can answer it; Cloudflare never fetches this target.
    # It points at the apex, an in-zone name, so it never points at a foreign origin; with the rule off, www is not served (a Workers custom domain binds one hostname)
    www = { name = "www.thebetterdecision.com", type = "CNAME", content = "thebetterdecision.com", ttl = 1, proxied = true }

    google_verification = { name = "thebetterdecision.com", type = "TXT", content = "\"google-site-verification=n5V8oSnk53Vi4UraYvoNiWv6FrBVeYkSGDAD9VsMTPY\"", ttl = 60 }

    # TBD production (INFRA-48). On the Hetzner cluster since INFRA-175: the Cloudflare Tunnel (tunnel.tf), which needs
    # the proxied CNAME to <tunnel id>.cfargotunnel.com. Staying a CNAME keeps this an in-place update.
    app = { name = "app.thebetterdecision.com", type = "CNAME", content = "${cloudflare_zero_trust_tunnel_cloudflared.hetzner_k3s.id}.cfargotunnel.com", ttl = 1, proxied = true }
    # TBD staging (INFRA-67), hostname per the 2026-10-03 ruling (staging = dev.<domain>). On the Hetzner cluster since
    # INFRA-173: the Cloudflare Tunnel (tunnel.tf), which needs the proxied CNAME to <tunnel id>.cfargotunnel.com.
    dev = { name = "dev.thebetterdecision.com", type = "CNAME", content = "${cloudflare_zero_trust_tunnel_cloudflared.hetzner_k3s.id}.cfargotunnel.com", ttl = 1, proxied = true }

    # Mailgun sending domain m.thebetterdecision.com (EU).
    mailgun_mx_a     = { name = "m.thebetterdecision.com", type = "MX", content = "mxa.eu.mailgun.org", priority = 10, ttl = 300 }
    mailgun_mx_b     = { name = "m.thebetterdecision.com", type = "MX", content = "mxb.eu.mailgun.org", priority = 10, ttl = 300 }
    mailgun_spf      = { name = "m.thebetterdecision.com", type = "TXT", content = "\"v=spf1 include:mailgun.org ~all\"", ttl = 300 }
    mailgun_dmarc    = { name = "_dmarc.m.thebetterdecision.com", type = "TXT", content = "\"v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:ebe10ff8@dmarc.mailgun.org,mailto:e05f9325@inbox.ondmarc.com; ruf=mailto:ebe10ff8@dmarc.mailgun.org,mailto:e05f9325@inbox.ondmarc.com;\"", ttl = 300 }
    mailgun_dkim     = { name = "email._domainkey.m.thebetterdecision.com", type = "TXT", content = "\"k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDJBSffQ3yl09L0oEmhSxrnVL2YgZ/eUnWT4PEcsW7yzkasaxsNdKC/R9Rud5CyJ0Vc4Q7M7irRRO7N/ChRvO8zFF4jFEJ1pHTtnb7dii1W69ii3tXaaie2OIg3INfs/xPk2IROI1bfWZ61Zr5VU8X+M9qFmP6DG69RjFXLxjvAbwIDAQAB\"", ttl = 300 }
    mailgun_tracking = { name = "email.m.thebetterdecision.com", type = "CNAME", content = "eu.mailgun.org", ttl = 300 }
  }
}

resource "cloudflare_dns_record" "tbd" {
  for_each = local.tbd_records

  zone_id  = cloudflare_zone.tbd.id
  name     = each.value.name
  type     = each.value.type
  content  = each.value.content
  priority = try(each.value.priority, null)
  ttl      = each.value.ttl
  proxied  = try(each.value.proxied, false)
}

# www -> apex, 301, path and query kept (INFRA-61; CloudFront's redirect kept the path but dropped the query). A zone rule rather than Worker code: the
# Worker would have to run first on every request (run_worker_first) to see the host, and every asset hit would then
# count against the Free plan's Worker requests. Free allows 10 such rules per zone. A rule added in the dashboard is
# drift that the next apply removes. The workspace token needs Zone > Single Redirect > Edit on this zone.
resource "cloudflare_ruleset" "tbd_redirects" {
  zone_id = cloudflare_zone.tbd.id
  name    = "default"
  kind    = "zone"
  phase   = "http_request_dynamic_redirect"

  rules = [{
    ref         = "www_to_apex"
    description = "www to the apex, 301, keeps path and query (INFRA-61)"
    expression  = "(http.host eq \"www.thebetterdecision.com\")"
    action      = "redirect"
    enabled     = true
    action_parameters = {
      from_value = {
        status_code           = 301
        preserve_query_string = true
        target_url = {
          expression = "concat(\"https://thebetterdecision.com\", http.request.uri.path)"
        }
      }
    }
  }]
}

# www.ziftbook.com -> apex, 301, path and query kept (INFRA-131), the same pattern as tbd_redirects. www stays the
# `ziftbook-landing` Worker's custom domain (attached by hand): zone redirect rules run before the Worker, so the rule
# answers first and the Worker needs neither a www branch nor run_worker_first on every path. The zone is read, not
# managed, and has no entrypoint ruleset in this phase yet, so this creates it. The workspace token needs Zone >
# Single Redirect > Edit on this zone (cloudflare-tokens): apply that workspace first.
resource "cloudflare_ruleset" "ziftbook_redirects" {
  zone_id = data.cloudflare_zone.ziftbook.id
  name    = "default"
  kind    = "zone"
  phase   = "http_request_dynamic_redirect"

  rules = [{
    ref         = "www_to_apex"
    description = "www to the apex, 301, keeps path and query (INFRA-131)"
    expression  = "(http.host eq \"www.ziftbook.com\")"
    action      = "redirect"
    enabled     = true
    action_parameters = {
      from_value = {
        status_code           = 301
        preserve_query_string = true
        target_url = {
          expression = "concat(\"https://ziftbook.com\", http.request.uri.path)"
        }
      }
    }
  }]
}

# Baseline security for zones that serve apps (INFRA-14). Zone settings act only on proxied
# hostnames: ziftbook.com (dev, app, apex and www), thebetterdecision.com (`app` and `dev` since
# INFRA-48 and INFRA-67, apex and www since INFRA-61).
# ziftbook.com's zone is read here, not managed.
data "cloudflare_zone" "ziftbook" {
  filter = { name = "ziftbook.com", account = { id = var.account_id } }
}

locals {
  app_zones = {
    tbd      = cloudflare_zone.tbd.id
    ziftbook = data.cloudflare_zone.ziftbook.id
  }
  app_zone_settings = {
    min_tls_version  = "1.2"
    always_use_https = "on"
    # HSTS without includeSubDomains or preload, so a host that cannot do HTTPS stays reachable
    # and backing out is max_age = 0 (browsers keep the old max_age until they revisit).
    # One year (INFRA-75): the zone header overrides the landing Workers' own (ziftbook's smoke expects it). On the TBD
    # apex it replaces the two years with includeSubDomains and preload that CloudFront sent (INFRA-61).
    security_header = {
      strict_transport_security = {
        enabled            = true
        max_age            = 31536000
        include_subdomains = false
        preload            = false
        nosniff            = false
      }
    }
  }
}

# Removing an entry only drops it from state (the provider's Delete is a no-op), so back out by
# applying the old value ("off", max_age = 0), never by deleting it.
resource "cloudflare_zone_setting" "app" {
  for_each = {
    for pair in setproduct(keys(local.app_zones), keys(local.app_zone_settings)) :
    "${pair[0]}/${pair[1]}" => { zone = pair[0], setting = pair[1] }
  }

  zone_id    = local.app_zones[each.value.zone]
  setting_id = each.value.setting
  value      = local.app_zone_settings[each.value.setting]
}

# The TBD zone runs in Full (strict) (INFRA-25). A DNS-only record never reaches this setting. The zone's SSL/TLS
# mode is already Custom (ssl_automatic_mode = custom, read 2026-10-02), so this value does not drift.
# ziftbook.com moved to strict with its first proxied hostname on the node (INFRA-47).
resource "cloudflare_zone_setting" "tbd_ssl" {
  zone_id    = cloudflare_zone.tbd.id
  setting_id = "ssl"
  value      = "strict"
}

# Ziftbook staging (INFRA-47), hostname per the 2026-10-03 ruling (staging = dev.<domain>). On the Hetzner cluster
# since INFRA-173: the Cloudflare Tunnel (tunnel.tf), which needs the proxied CNAME to <tunnel id>.cfargotunnel.com.
resource "cloudflare_dns_record" "ziftbook_dev" {
  zone_id = data.cloudflare_zone.ziftbook.id
  name    = "dev.ziftbook.com"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.hetzner_k3s.id}.cfargotunnel.com"
  ttl     = 1 # automatic, required for proxied records
  proxied = true
}

# Ziftbook production (INFRA-82). On the Hetzner cluster since INFRA-181, through the Cloudflare Tunnel like dev.
resource "cloudflare_dns_record" "ziftbook_app" {
  zone_id = data.cloudflare_zone.ziftbook.id
  name    = "app.ziftbook.com"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.hetzner_k3s.id}.cfargotunnel.com"
  ttl     = 1 # automatic, required for proxied records
  proxied = true
}

# Ziftbook trusts cf-connecting-ip; overwrite_header would replace it with a pseudo IPv4 (ZIF-219). Read "off" 2026-10-24.
resource "cloudflare_zone_setting" "ziftbook_pseudo_ipv4" {
  zone_id    = data.cloudflare_zone.ziftbook.id
  setting_id = "pseudo_ipv4"
  value      = "off"
}

resource "cloudflare_zone_setting" "ziftbook_ssl_mode" {
  zone_id    = data.cloudflare_zone.ziftbook.id
  setting_id = "ssl_automatic_mode"
  value      = "custom"
}

resource "cloudflare_zone_setting" "ziftbook_ssl" {
  zone_id    = data.cloudflare_zone.ziftbook.id
  setting_id = "ssl"
  value      = "strict"

  depends_on = [cloudflare_zone_setting.ziftbook_ssl_mode]
}

# Shared Mailgun sending domain for every dev/staging environment (INFRA-47, owner ruling 2026-10-03):
# m.fjconsulting.dev, EU region. Only production gets per-app m.<appdomain> domains. The zone is
# read, not managed: its other records (Google mail, site) live outside Terraform. The record set
# mirrors TBD's m.thebetterdecision.com above (MX, SPF, DKIM, DMARC with Mailgun reporting, tracking).
# Mailgun's Cloudflare auto-setup created all but the MX on 2026-10-03; they were adopted with import
# blocks, removed once applied. A second dev app only needs a new Mailgun sending key, no change
# here. The DKIM value is a public key, split in two strings (TXT 255-char limit).
data "cloudflare_zone" "fjdev" {
  filter = { name = "fjconsulting.dev", account = { id = var.account_id } }
}

locals {
  fjdev_mail_records = {
    mx_a     = { type = "MX", name = "m.fjconsulting.dev", content = "mxa.eu.mailgun.org", priority = 10 }
    mx_b     = { type = "MX", name = "m.fjconsulting.dev", content = "mxb.eu.mailgun.org", priority = 10 }
    spf      = { type = "TXT", name = "m.fjconsulting.dev", content = "\"v=spf1 include:mailgun.org ~all\"" }
    dmarc    = { type = "TXT", name = "_dmarc.m.fjconsulting.dev", content = "\"v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:ebe10ff8@dmarc.mailgun.org,mailto:e05f9325@inbox.ondmarc.com; ruf=mailto:ebe10ff8@dmarc.mailgun.org,mailto:e05f9325@inbox.ondmarc.com;\"" }
    dkim     = { type = "TXT", name = "mta._domainkey.m.fjconsulting.dev", content = "\"k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApsCPgZz8EKnXqDFkGDP2gBc1qxfcyT3zD18o+I7HaGzq4zV4Ib76F6vOFPJjrxwBya74NdLXp+vClblG75xFcVAMkg17RPVu0kIJ+9SNWox2H2cVs1P4s2WL3a22uVoZnod/ORjtpYUT99xKbsjYM89i7byAJiwHG5TDtevNxtjws1ZgwVJcCNbJg58LEq9VxpLIR+26ft\" \"r7Y2z5mSNQuZ0erEDYGpuLaVehCIX81XqqpPpl51VDpE+/ZYZ1UINGzc1BxeEKu4w/HT1dloRwx9ebkwnjA3IK0r1tfhffO6uaxjKc0CCpCZKnCuvuhwiF4cKemLiF/SMBP7joNZPH7QIDAQAB\"" }
    tracking = { type = "CNAME", name = "email.m.fjconsulting.dev", content = "eu.mailgun.org" }
  }
}

resource "cloudflare_dns_record" "fjdev_mail" {
  for_each = local.fjdev_mail_records

  zone_id  = data.cloudflare_zone.fjdev.id
  name     = each.value.name
  type     = each.value.type
  content  = each.value.content
  priority = try(each.value.priority, null)
  ttl      = 300
  proxied  = false
}

# Ziftbook production's Mailgun sending domain (INFRA-139): m.ziftbook.com, EU region, created in Mailgun 2026-10-06.
# Same record set as m.thebetterdecision.com and m.fjconsulting.dev above; values from Mailgun's domain record list.
# The DKIM value is a 1024-bit public key, one string. Verify in Mailgun after the apply.
locals {
  ziftbook_mail_records = {
    mx_a     = { type = "MX", name = "m.ziftbook.com", content = "mxa.eu.mailgun.org", priority = 10 }
    mx_b     = { type = "MX", name = "m.ziftbook.com", content = "mxb.eu.mailgun.org", priority = 10 }
    spf      = { type = "TXT", name = "m.ziftbook.com", content = "\"v=spf1 include:mailgun.org ~all\"" }
    dmarc    = { type = "TXT", name = "_dmarc.m.ziftbook.com", content = "\"v=DMARC1; p=none; pct=100; fo=1; ri=3600; rua=mailto:ebe10ff8@dmarc.mailgun.org,mailto:e05f9325@inbox.ondmarc.com; ruf=mailto:ebe10ff8@dmarc.mailgun.org,mailto:e05f9325@inbox.ondmarc.com;\"" }
    dkim     = { type = "TXT", name = "email._domainkey.m.ziftbook.com", content = "\"k=rsa; p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC1X2888qHqW4D/8t8+ye5xRPQJoFrq02xHeV3XAg5jMUH69ZIYFoFomXtAMYOdO17uXdTb4cXvWI9VpN1DLREJvHgVPrf3QvVC9BRaLWum5hemIsuLZLIoe0WC/nyyHwB92n0r1jDV49fh+ZdDQiFZdTxVTJpagi3bI6jp+lyF7wIDAQAB\"" }
    tracking = { type = "CNAME", name = "email.m.ziftbook.com", content = "eu.mailgun.org" }
  }
}

resource "cloudflare_dns_record" "ziftbook_mail" {
  for_each = local.ziftbook_mail_records

  zone_id  = data.cloudflare_zone.ziftbook.id
  name     = each.value.name
  type     = each.value.type
  content  = each.value.content
  priority = try(each.value.priority, null)
  ttl      = 300
  proxied  = false
}

output "tbd_name_servers" {
  description = "The zone's name servers (the domain is at Cloudflare Registrar since 2026-10-06)."
  value       = cloudflare_zone.tbd.name_servers
}

output "tbd_zone_status" {
  description = "pending until the registrar NS switch, then active."
  value       = cloudflare_zone.tbd.status
}
