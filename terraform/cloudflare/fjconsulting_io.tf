# fjconsulting.io (INFRA-275): the company's Google Workspace domain. The zone was added by hand and is read here, not
# managed. Only the mail and verification records below are managed: the apex and www belong to the website (INFRA-279).
# Every record is DNS-only: a proxied CNAME hides its target, which breaks DKIM and domain verification.
data "cloudflare_zone" "fjio" {
  filter = { name = "fjconsulting.io", account = { id = var.account_id } }
}

locals {
  fjio_records = {
    mx                         = { type = "MX", name = "fjconsulting.io", content = "SMTP.GOOGLE.COM", priority = 1, ttl = 60 }
    spf                        = { type = "TXT", name = "fjconsulting.io", content = "\"v=spf1 include:_spf.google.com ~all\"", ttl = 1 }
    google_site_verification   = { type = "TXT", name = "fjconsulting.io", content = "\"google-site-verification=25eXDAqS1_llO0tWTXlAQ7uxZJVSyciKMGhfYJezUMc\"", ttl = 60 }
    google_domain_verification = { type = "CNAME", name = "e7tkea56bfk4.fjconsulting.io", content = "gv-kjhyvamujwy5bu.dv.googlehosted.com", ttl = 1 }
    # A public key, split in two strings (TXT 255-char limit).
    google_dkim                   = { type = "TXT", name = "google._domainkey.fjconsulting.io", content = "\"v=DKIM1;k=rsa;p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxWFAectVpd5K/ChglfrLp4EE39rw0Z1t5o/MLJ1Ni44xy8gyVf3B8N1R+QuGex6HMPmJ2Rg0AwPpfXKS361zY+qXv8Qw3uN6t5eftwepdIfqtl2IA+QEQEayP8OTCyggVMgBdkF5rZecgrqKJhoLBFy14ZPVQWODRHgaI5FXUmD75JE7BlyvPegMRqtp76tnJVB\" \"hL7xSiTVggdKgxj9jotuwQ+fpO2fQHV1NKJ2dOnu86JsaXkmJ8k1L505vBfHxjTNjvbpBzIF4e0gcn/Vl47qKnXyWHSM7LEmXzHifl2lgQ0t2OqMNzNsDv3Y0lp1Efv4UEic/u7lR9tYXt0iazQIDAQAB\"", ttl = 60 }
    atlassian_domain_verification = { type = "TXT", name = "fjconsulting.io", content = "\"atlassian-domain-verification=vKARPekk/dAODqJV9uj4o2tX78Tlm/Rm8HQmyHz9j6ewFbWHZpdroICWQItvM8Vi\"", ttl = 60 }
    atlassian_dkim_470be0         = { type = "CNAME", name = "atlassian-470be0._domainkey.fjconsulting.io", content = "atlassian-470be0.dkim.atlassian.net", ttl = 60 }
    atlassian_dkim_d4397b         = { type = "CNAME", name = "atlassian-d4397b._domainkey.fjconsulting.io", content = "atlassian-d4397b.dkim.atlassian.net", ttl = 60 }
    atlassian_bounces             = { type = "CNAME", name = "atlassian-bounces.fjconsulting.io", content = "bounces.mail-us.atlassian.net", ttl = 3600 }
  }
}

resource "cloudflare_dns_record" "fjio" {
  for_each = local.fjio_records

  zone_id  = data.cloudflare_zone.fjio.id
  name     = each.value.name
  type     = each.value.type
  content  = each.value.content
  priority = try(each.value.priority, null)
  ttl      = each.value.ttl
  proxied  = false
}

# Adopts the records made by hand. Remove these blocks once applied.
import {
  to = cloudflare_dns_record.fjio["mx"]
  id = "f3fb27219825edb004ee62cba0de45aa/2e23e74ca1635f7640101190f7e09f03"
}

import {
  to = cloudflare_dns_record.fjio["spf"]
  id = "f3fb27219825edb004ee62cba0de45aa/819c1a4e61c80bc1f8d5e0a167bb9567"
}

import {
  to = cloudflare_dns_record.fjio["google_site_verification"]
  id = "f3fb27219825edb004ee62cba0de45aa/79c76e070f1be70aceb13649a03d9385"
}

import {
  to = cloudflare_dns_record.fjio["google_domain_verification"]
  id = "f3fb27219825edb004ee62cba0de45aa/499fc4223ae6b009798eadc21758264e"
}

import {
  to = cloudflare_dns_record.fjio["google_dkim"]
  id = "f3fb27219825edb004ee62cba0de45aa/587e1ac77cb669f192232dd4db2d94e5"
}

import {
  to = cloudflare_dns_record.fjio["atlassian_domain_verification"]
  id = "f3fb27219825edb004ee62cba0de45aa/818ac70a004a86f947be9d2ec67787d7"
}
