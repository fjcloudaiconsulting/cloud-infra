"""Fences on how TBD is exposed at the edge: the Hetzner firewall and the Cloudflare tunnel routes.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`. The Terraform is read as text; each
helper fails when the shape it expects is gone, so a refactor updates the fence instead of silently passing it.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TF = ROOT / "terraform"
HCL_STRING = r'"((?:[^"\\]|\\.)*)"'


def hcl_unquote(s):
    return re.sub(r"\\(.)", r"\1", s)


def resource_body(text, rtype, name):
    """The text of `resource "<rtype>" "<name>" { ... }`, by brace matching (strings and comments are simple here)."""
    m = re.search(rf'(?m)^resource "{re.escape(rtype)}" "{re.escape(name)}" \{{', text)
    if not m:
        raise AssertionError(f"resource {rtype}.{name} not found")
    depth, i = 0, m.end() - 1
    in_str = False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[m.end():i]
        i += 1
    raise AssertionError(f"resource {rtype}.{name} is not closed")


def tunnel_rules():
    """The tunnel's ingress rules in order, as dicts of hostname / path (the regex, unescaped) / service."""
    body = resource_body((TF / "cloudflare/tunnel.tf").read_text(), "cloudflare_zero_trust_tunnel_cloudflared_config",
                         "hetzner_k3s")
    rules = []
    for m in re.finditer(r"(?m)^\s*\{ ((?:hostname|path|service) = .*?) \},?\s*$", body):
        rule = {k: hcl_unquote(v) for k, v in re.findall(rf"(hostname|path|service) = {HCL_STRING}", m.group(1))}
        rules.append(rule)
    if not rules or "service" not in rules[-1]:
        raise AssertionError("no tunnel ingress rules found")
    return rules


def route(rules, host, path):
    """The service cloudflared picks: first rule whose hostname equals the host and whose path regex matches."""
    for r in rules:
        if r.get("hostname", host) != host:
            continue
        if "path" in r and not re.search(r["path"], path):
            continue
        return r["service"]
    raise AssertionError("no catch-all rule")


class OriginOnlyThroughTheTunnel(unittest.TestCase):
    """INF-01: the nodes accept no inbound traffic but SSH; the tunnel routes TBD hosts only to TBD Services."""

    def test_hetzner_firewall_admits_ssh_only(self):
        text = "".join(p.read_text() for p in sorted((TF / "hetzner").glob("*.tf")))
        self.assertNotRegex(text, r'resource "hcloud_(load_balancer|floating_ip)')
        fw = re.findall(r'(?m)^resource "hcloud_firewall" "(\w+)"', text)
        self.assertEqual(fw, ["nodes"])
        rules = re.findall(r"rule \{(.*?)\}", resource_body(text, "hcloud_firewall", "nodes"), re.S)
        self.assertEqual(len(rules), 1)
        self.assertRegex(rules[0], r'direction\s*=\s*"in"')
        self.assertRegex(rules[0], r'port\s*=\s*"22"')
        self.assertRegex(rules[0], r"source_ips\s*=\s*var\.ssh_allowed_cidrs")
        servers = re.findall(r'(?m)^resource "hcloud_server" "(\w+)"', text)
        self.assertTrue(servers)
        for name in servers:
            self.assertRegex(resource_body(text, "hcloud_server", name), r"firewall_ids\s*=\s*\[hcloud_firewall\.nodes\.id\]")

    def test_tbd_hosts_reach_tbd_services_only_and_the_rest_is_404(self):
        rules = tunnel_rules()
        self.assertEqual(rules[-1], {"service": "http_status:404"})
        tbd = [r for r in rules if r.get("hostname", "").endswith("thebetterdecision.com")]
        self.assertEqual({r["hostname"] for r in tbd}, {"app.thebetterdecision.com", "dev.thebetterdecision.com"})
        ns = {"app.thebetterdecision.com": "tbd-prod", "dev.thebetterdecision.com": "tbd-staging"}
        for r in tbd:
            self.assertRegex(r["service"], rf"^http://(backend|frontend|mcp)\.{ns[r['hostname']]}\.svc\.cluster\.local:\d+$")
        # An unknown host never falls through to an app.
        self.assertEqual(route(rules, "x.thebetterdecision.com", "/api/v1/auth/login"), "http_status:404")


    def test_database_probes_are_not_routed_on_the_public_hosts(self):
        # INF-09: /ready and /health/dependencies query the database; kubelet and internal probes reach the pod.
        rules = tunnel_rules()
        for host, ns in (("app.thebetterdecision.com", "tbd-prod"), ("dev.thebetterdecision.com", "tbd-staging")):
            backend = f"http://backend.{ns}.svc.cluster.local:8000"
            for path in ("/health", "/api/v1/auth/login"):
                self.assertEqual(route(rules, host, path), backend, (host, path))
            for path in ("/ready", "/ready/", "/health/dependencies", "/health/dependencies/", "/health%2Fdependencies",
                         "/health/", "/healthz"):
                self.assertNotEqual(route(rules, host, path), backend, (host, path))

class BodyLimit(unittest.TestCase):
    """BE-13: Cloudflare refuses a request to a TBD app host whose declared body is over the cap."""

    IMPORT_UPLOAD_CAP = 5 * 1024 * 1024  # tbd backend MAX_UPLOAD_BYTES, the largest file it accepts
    CL = 'http.request.headers["content-length"][0]'

    def rule(self):
        body = resource_body((TF / "cloudflare/body_limit.tf").read_text(), "cloudflare_ruleset", "tbd_body_limit")
        self.assertRegex(body, r"zone_id\s*=\s*cloudflare_zone\.tbd\.id")
        self.assertRegex(body, r'phase\s*=\s*"http_request_firewall_custom"')
        self.assertRegex(body, r'action\s*=\s*"block"')
        self.assertRegex(body, r"enabled\s*=\s*true")
        return hcl_unquote(re.search(rf"expression\s*=\s*{HCL_STRING}", body).group(1))

    def test_expression_covers_both_hosts_and_caps_above_the_largest_upload(self):
        expr = self.rule()
        cl = re.escape(self.CL)
        m = re.fullmatch(
            rf'\(http\.host in \{{"([^"]+)" "([^"]+)"\}}\) and \(len\({cl}\) gt (\d+) or '
            rf'\(len\({cl}\) eq (\d+) and {cl} gt "(\d+)"\)\)', expr)
        self.assertIsNotNone(m, expr)
        hosts, longer, same, cap = {m.group(1), m.group(2)}, int(m.group(3)), int(m.group(4)), m.group(5)
        self.assertEqual(hosts, {"app.thebetterdecision.com", "dev.thebetterdecision.com"})
        # A string compare is a number compare only between strings of one length.
        self.assertEqual(longer, len(cap))
        self.assertEqual(same, len(cap))
        self.assertEqual(int(cap), 16 * 1024 * 1024)
        self.assertGreaterEqual(int(cap), 3 * self.IMPORT_UPLOAD_CAP)

        def blocked(length):
            v = str(length)
            return len(v) > longer or (len(v) == same and v > cap)

        for n in (0, 1, 9, 2_000_000, self.IMPORT_UPLOAD_CAP + 64 * 1024, 9_999_999, int(cap)):
            self.assertFalse(blocked(n), n)
        for n in (int(cap) + 1, 20_000_000, 99_999_999, 100_000_000, 100 * 1024 * 1024):
            self.assertTrue(blocked(n), n)


if __name__ == "__main__":
    unittest.main()
