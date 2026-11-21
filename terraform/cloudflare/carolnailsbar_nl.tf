# carolnailsbar.nl: the zone was added by hand, without a record scan, and is read here, not managed. Every record
# below is a 1:1 copy of the previous DNS host, so the nameserver switch changes nothing. The apex A records and www
# serve the old site until the launch moves the apex to the Worker `carol-nails-bar` (attached by hand) and www to a
# redirect; that change removes them here first. Every record is DNS-only, as at the previous host.
data "cloudflare_zone" "cnb" {
  filter = { name = "carolnailsbar.nl", account = { id = var.account_id } }
}

locals {
  cnb_records = {
    apex_a_1                 = { type = "A", name = "carolnailsbar.nl", content = "76.223.105.230" }
    apex_a_2                 = { type = "A", name = "carolnailsbar.nl", content = "13.248.243.5" }
    www                      = { type = "CNAME", name = "www.carolnailsbar.nl", content = "carolnailsbar.nl" }
    mx_aspmx                 = { type = "MX", name = "carolnailsbar.nl", content = "aspmx.l.google.com", priority = 1 }
    mx_alt1                  = { type = "MX", name = "carolnailsbar.nl", content = "alt1.aspmx.l.google.com", priority = 5 }
    mx_alt2                  = { type = "MX", name = "carolnailsbar.nl", content = "alt2.aspmx.l.google.com", priority = 5 }
    mx_alt3                  = { type = "MX", name = "carolnailsbar.nl", content = "alt3.aspmx.l.google.com", priority = 10 }
    mx_alt4                  = { type = "MX", name = "carolnailsbar.nl", content = "alt4.aspmx.l.google.com", priority = 10 }
    spf                      = { type = "TXT", name = "carolnailsbar.nl", content = "\"v=spf1 include:spf.em.secureserver.net include:dc-aa8e722993._spfm.carolnailsbar.nl ~all\"" }
    spf_google               = { type = "TXT", name = "dc-aa8e722993._spfm.carolnailsbar.nl", content = "\"v=spf1 include:_spf.google.com ~all\"" }
    dmarc                    = { type = "TXT", name = "_dmarc.carolnailsbar.nl", content = "\"v=DMARC1; p=none; rua=mailto:dmarc_rua@onsecureserver.net\"" }
    mta_sts_apex             = { type = "TXT", name = "carolnailsbar.nl", content = "\"v=STSv1;id=1776432944477;\"" }
    google_site_verification = { type = "TXT", name = "carolnailsbar.nl", content = "\"google-site-verification=3yryvuHiZYQ67WtjG0XMrqgrxd0lyD5xcoZABVANypA\"" }
    # A public key, split in two strings (TXT 255-char limit), as published before the move.
    google_dkim = { type = "TXT", name = "google._domainkey.carolnailsbar.nl", content = "\"v=DKIM1;k=rsa;p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApqke7ypPIJdEMGvLx36wvUnWMxJ/T8evpU1CilRA0BScBCl+g6J4LJxPdC2q9SuX1TkqHbPzdUVXoo6y0fKcqHwcydN258ZDNjtYfZFUAMVjvcKLA4L/M8/nXGVV24LbUEEjTyAlKF9P1Ww40XUH4i34ixjl7g2O68c7xpP2SBZrx6R26WZiVbEjX4oCujYy4tY\" \"UAjQdvNdC7Dx6tu522HayanvbgE/E6jmAUMrnmm/lVOs3v6zBbUBRc9VkfYfk8inrt8OhSDP5xQ1hoSlx3emV2LaQoubkQ5wbQjtPB4ikjikE3IfZ9yFC1HymB4DU8xAbneLsxl/XQsGOzaU+ZwIDAQAB\"" }
  }
}

resource "cloudflare_dns_record" "cnb" {
  for_each = local.cnb_records

  zone_id  = data.cloudflare_zone.cnb.id
  name     = each.value.name
  type     = each.value.type
  content  = each.value.content
  priority = try(each.value.priority, null)
  ttl      = 1
  proxied  = false
}
