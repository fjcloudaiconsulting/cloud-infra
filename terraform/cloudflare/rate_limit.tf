# Edge rate limit on the apps' sign-in and token endpoints (INFRA-123) and on Ziftbook's database health probe
# (INFRA-140: unauthenticated, one query per request; its database timeout is app-side). It caps what one address can
# send to them, so a burst gets a 429 from Cloudflare instead of reaching the app. Each app keeps its own per-account
# and per-address limits behind this.
#
# Both zones are on the Free plan, which allows (read 2026-10-04): one rate-limiting rule per zone, the URI path as
# the only request field (no host, no method), counting per client IP and Cloudflare data center (ip.src and
# cf.colo.id, both required), a 10 s period, a 10 s block and the block action, which answers 429. So the rule has no
# host condition and covers every proxied host of the zone at once (TBD app., and dev. once INFRA-67 adds it; Ziftbook
# dev. and any later production host; the TBD landing apex too, which serves none of these paths. TBD www never reaches
# it: its redirect rule runs in an earlier phase). DNS-only records never reach it.
#
# One counter per address for all paths of a zone. 20 requests in 10 s is far above a person signing in (a few
# requests) and above the TBD post-deploy smoke test (one login). GET /api/session on ziftbook.com shares the path of
# the sign-in POST, so reads count too. Paths are matched after the zones' URL normalization (docs/configuration-map.md,
# Cloudflare); a path with an encoded slash also counts, since no app route needs one. Residuals of the plan: the
# counter is per address and per data center, so many addresses (or one IPv6 prefix) get more.
#
# The TBD MCP OAuth token and registration endpoints (INFRA-147, owner ruling 2026-10-09) are listed exactly, so the
# consent endpoints under /api/v1/oauth/authorize are not counted. They are meant for staging only (production
# keeps them off until its security review), but the rule has no host condition. A hosted MCP client calls them from its
# provider's shared egress addresses, so its users share one counter; the app's own per-code, per-client and per-IP limits stay.
#
# Neither zone had an http_ratelimit entrypoint ruleset before this one. A rule added in the dashboard is drift that
# the next apply removes. Back out with enabled = false.
#
# The workspace token needs Zone WAF: Edit on both zones (docs/configuration-map.md, HCP Terraform).
locals {
  auth_rate_limit_paths = {
    tbd = [
      "/api/v1/auth/login",
      "/api/v1/auth/register",
      "/api/v1/auth/forgot-password",
      "/api/v1/auth/reset-password",
      "/api/v1/auth/verify-email",
      "/api/v1/auth/resend-verification-public",
      "/api/v1/auth/mfa/verify",
      "/api/v1/auth/mfa/recovery",
      "/api/v1/auth/mfa/email-code",
      "/api/v1/auth/mfa/email-verify",
      "/api/v1/auth/google/callback",
      "/api/v1/orgs/invitations/preview",
      "/api/v1/orgs/invitations/accept",
      "/api/v1/oauth/token",
      "/api/v1/oauth/register",
    ]
    ziftbook = [
      "/api/session",
      "/api/admin/session",
      "/api/admin/session/second-factor",
      "/api/sign-up",
      "/api/sign-up/complete",
      "/api/password-reset",
      "/api/password-reset/complete",
      "/api/invites/lookup",
      "/api/invites/accept",
      "/api/public/booking-link/session",
      "/api/health/dependencies",
    ]
  }
}

resource "cloudflare_ruleset" "auth_rate_limit" {
  for_each = local.auth_rate_limit_paths

  zone_id = local.app_zones[each.key]
  name    = "default"
  kind    = "zone"
  phase   = "http_ratelimit"

  rules = [{
    ref         = "auth_burst"
    description = "Auth endpoints + Ziftbook DB health probe: 20 requests per 10 s per IP, then 429 (INFRA-123, INFRA-140)"
    expression  = "(http.request.uri.path in {${join(" ", [for p in each.value : jsonencode(p)])}}) or (http.request.uri.path contains \"%2F\") or (http.request.uri.path contains \"%2f\")"
    action      = "block"
    enabled     = true
    ratelimit = {
      characteristics     = ["ip.src", "cf.colo.id"]
      period              = 10
      requests_per_period = 20
      mitigation_timeout  = 10
    }
  }]
}
