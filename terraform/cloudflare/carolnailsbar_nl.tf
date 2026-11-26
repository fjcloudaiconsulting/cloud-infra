# carolnailsbar.nl: the zone was added by hand and is read here. At launch, the owner attaches the apex as a
# custom domain on the Worker `carol-nails-bar`. The www hostname redirects to the apex at the zone edge.
# Mail, verification and email-marketing records are a 1:1 copy of the previous DNS host's export and remain DNS-only.
data "cloudflare_zone" "cnb" {
  filter = { name = "carolnailsbar.nl", account = { id = var.account_id } }
}

locals {
  cnb_records = {
    # The redirect rule only answers proxied traffic.
    www                      = { type = "CNAME", name = "www.carolnailsbar.nl", content = "carolnailsbar.nl", proxied = true }
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
    # The previous host's email-marketing service (it is also in the SPF include): its bounce and DKIM records.
    em_bounces_cloud  = { type = "CNAME", name = "bounces.cloud.em.carolnailsbar.nl", content = "cbounces.cloud.em.secureserver.net" }
    em_bounces_cloud2 = { type = "CNAME", name = "bounces.cloud2.em.carolnailsbar.nl", content = "cbounces.cloud2.em.secureserver.net" }
    em_dkim_cloud     = { type = "CNAME", name = "sable.cloud._domainkey.carolnailsbar.nl", content = "dkim.cloud.em.secureserver.net" }
    em_dkim_cloud2    = { type = "CNAME", name = "sable.cloud2._domainkey.carolnailsbar.nl", content = "dkim.cloud2.em.secureserver.net" }
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
  proxied  = try(each.value.proxied, false)
}

resource "cloudflare_ruleset" "cnb_redirects" {
  zone_id = data.cloudflare_zone.cnb.id
  name    = "default"
  kind    = "zone"
  phase   = "http_request_dynamic_redirect"

  # The legacy inventory lists lowercase paths. Match them exactly since lower() support in Free Single Redirects is unverified.
  rules = [{
    ref         = "about_to_studio"
    description = "Legacy About paths to the English studio section (INFRA-286)"
    expression  = "(http.host in {\"carolnailsbar.nl\" \"www.carolnailsbar.nl\"} and http.request.uri.path in {\"/about\" \"/about/\"} and http.request.uri.query eq \"\")"
    action      = "redirect"
    enabled     = true
    action_parameters = {
      from_value = {
        status_code           = 301
        preserve_query_string = false
        target_url = {
          value = "https://carolnailsbar.nl/en/#studio"
        }
      }
    }
    }, {
    ref         = "about_to_studio_query"
    description = "Legacy About paths with the query before the studio fragment (INFRA-286)"
    expression  = "(http.host in {\"carolnailsbar.nl\" \"www.carolnailsbar.nl\"} and http.request.uri.path in {\"/about\" \"/about/\"} and http.request.uri.query ne \"\")"
    action      = "redirect"
    enabled     = true
    action_parameters = {
      from_value = {
        status_code           = 301
        preserve_query_string = false
        target_url = {
          expression = "concat(\"https://carolnailsbar.nl/en/?\", http.request.uri.query, \"#studio\")"
        }
      }
    }
    }, {
    ref         = "www_to_apex"
    description = "www to the apex, 301, keeps path and query (INFRA-286)"
    expression  = "(http.host eq \"www.carolnailsbar.nl\")"
    action      = "redirect"
    enabled     = true
    action_parameters = {
      from_value = {
        status_code           = 301
        preserve_query_string = true
        target_url = {
          expression = "concat(\"https://carolnailsbar.nl\", http.request.uri.path)"
        }
      }
    }
  }]
}
