# Request body cap on the TBD app hosts (BE-13). cloudflared has no body limit of its own and nothing sits between it
# and the app, so the cap is a zone custom rule: a request to app. or dev. that declares a Content-Length over 16 MiB
# gets Cloudflare's block page (403) and never reaches the cluster. It covers every path of those hosts, the API and
# the frontend alike. The apex (Worker) and www (redirect) are not matched.
#
# 16 MiB: three times the largest file the backend accepts (the 5 MiB import upload, tbd MAX_UPLOAD_BYTES), and above
# the largest import confirm (at most 10,000 rows that echo that file's content). The Free plan's rules language reads
# a header as a string, so the rule compares the digit count first and only then the 8-digit value: between strings of
# one length, the string order is the number order. A body sent without a Content-Length is not counted here; the
# backend's own per-route caps apply to it.
#
# A rule added in the dashboard is drift that the next apply removes; if the zone already has dashboard custom rules,
# the create fails until that ruleset is imported. Back out with enabled = false. The workspace token's Zone WAF Write
# on this zone (cloudflare-tokens) covers this phase, as for the rate limit.
resource "cloudflare_ruleset" "tbd_body_limit" {
  zone_id = cloudflare_zone.tbd.id
  name    = "default"
  kind    = "zone"
  phase   = "http_request_firewall_custom"

  rules = [{
    ref         = "tbd_body_limit"
    description = "TBD app hosts: block a declared request body over 16 MiB (BE-13)"
    expression  = "(http.host in {\"app.thebetterdecision.com\" \"dev.thebetterdecision.com\"}) and (len(http.request.headers[\"content-length\"][0]) gt 8 or (len(http.request.headers[\"content-length\"][0]) eq 8 and http.request.headers[\"content-length\"][0] gt \"16777216\"))"
    action      = "block"
    enabled     = true
  }]
}
