# Request body cap on the TBD app hosts (BE-13). cloudflared has no body limit of its own and nothing sits between it
# and the app, so the cap is a zone custom rule: a request to app. or dev. that declares a Content-Length over 16 MiB
# gets Cloudflare's block page (403) and never reaches the cluster. It covers every path of those hosts, the API and
# the frontend alike. The apex (Worker) and www (redirect) are not matched.
#
# 16 MiB: three times the largest file the backend accepts (the 5 MiB import upload, tbd MAX_UPLOAD_BYTES), and above
# the largest import confirm (at most 10,000 rows that echo that file's content). The Free plan's rules language reads
# a header as a string, so the rule compares the digit count first and only then the 8-digit value: between strings of
# one length, the string order is the number order.
#
# A body without a declared length is refused too (EDGE-02), by a second rule on app. and dev.: any request with a
# Transfer-Encoding header (an HTTP/1.1 body without a length is chunked), and a POST, PUT or PATCH with no
# Content-Length at all (an HTTP/2 body may go without one). The second clause names its methods because browsers send
# Content-Length: 0 on an empty POST or PUT but nothing on an empty PATCH or DELETE: TBD's frontend sends DELETE without
# a body, so DELETE (like GET, HEAD and OPTIONS) is not in it, and PATCH is, because the frontend sends every PATCH with
# a JSON body. TBD's clients declare a length: the
# frontend sends JSON or FormData, browsers send CSP reports with one, and Mailgun's webhook POSTs are expected to
# (check Security Events after a change). The one exception is exactly /mcp on dev., the MCP server (staging only,
# INFRA-147, tunnel.tf): a third-party MCP client may send its body without a length, and that server reads it through
# its own 64 KiB stream cap. The exemption needs the raw path to be /mcp as well as the normalized one, so it covers
# only the request line an MCP client sends, which tunnel.tf routes to that server; a path that only normalizes to /mcp
# (a dot segment, an encoded letter) is not exempt. http.request.headers.names keeps the case the client sent (HTTP/1.1
# clients write Content-Length), so the rule lowers each name before comparing; any() over no matching name is false, so
# a request without the header is matched. Its own rule, so Security Events name the case and enabled = false backs it
# out alone.
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
    }, {
    ref         = "tbd_body_length_required"
    description = "TBD app hosts: block a chunked request, or a POST, PUT or PATCH without a Content-Length, except /mcp on dev. (EDGE-02)"
    expression  = "(http.host in {\"app.thebetterdecision.com\" \"dev.thebetterdecision.com\"}) and (any(lower(http.request.headers.names[*])[*] eq \"transfer-encoding\") or (http.request.method in {\"PATCH\" \"POST\" \"PUT\"} and not any(lower(http.request.headers.names[*])[*] eq \"content-length\"))) and not (http.host eq \"dev.thebetterdecision.com\" and http.request.uri.path eq \"/mcp\" and raw.http.request.uri.path eq \"/mcp\")"
    action      = "block"
    enabled     = true
  }]
}
