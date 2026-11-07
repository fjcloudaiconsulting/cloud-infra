# DNSSEC and CAA on both app zones (INFRA-231). Token scope: DNS Write, already on both zones.

# Both domains are at Cloudflare Registrar, which publishes the DS at .com by itself once the zone is signed (it can
# take a day or two); there is no registrar step. Until the DS is live the provider may read status "pending" and plan
# "active" again: a no-op PATCH. Back out with status = "disabled" and drop this block only once `dig DS <zone>` is empty
# and the .com DS TTL (a day) has passed: deleting the resource removes the zone's keys at once, and resolvers that
# still hold the DS then fail every name in the zone.
resource "cloudflare_zone_dnssec" "app" {
  for_each = local.app_zones

  zone_id = each.value
  status  = "active"

  lifecycle {
    prevent_destroy = true
  }
}

# CAA: only the CAs Cloudflare issues edge certificates from (Universal SSL, the Workers custom domains on the apexes and
# ziftbook's www, the R2 custom domains img and img-dev, all tunnel hostnames), as listed in Cloudflare's CAA docs.
# Cloudflare adds the same four itself to a Universal SSL zone that has any CAA, but not for dedicated certificates
# (the R2 domains), so they are declared here. No issuewild: `issue` also governs wildcards when none is set.
# The Mailgun tracking hosts email.m.* are CNAMEs to eu.mailgun.org, whose own CAA answers first; these do not apply there.
# No iodef: neither domain has a mailbox. A new provider (a CA, or a host with its own certificate) needs its CA here
# before it can issue, or its issuance and renewals fail.
locals {
  caa_zone_names = { tbd = "thebetterdecision.com", ziftbook = "ziftbook.com" }
  caa_issuers = {
    letsencrypt = "letsencrypt.org"
    google      = "pki.goog; cansignhttpexchanges=yes"
    sslcom      = "ssl.com"
    sectigo     = "sectigo.com"
  }
}

resource "cloudflare_dns_record" "caa" {
  for_each = {
    for pair in setproduct(keys(local.app_zones), keys(local.caa_issuers)) :
    "${pair[0]}/${pair[1]}" => { zone = pair[0], ca = pair[1] }
  }

  zone_id = local.app_zones[each.value.zone]
  name    = local.caa_zone_names[each.value.zone]
  type    = "CAA"
  ttl     = 300
  data = {
    flags = 0
    tag   = "issue"
    value = local.caa_issuers[each.value.ca]
  }
}
