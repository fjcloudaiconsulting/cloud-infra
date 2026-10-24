"""Fences on the Cloudflare edge rules in terraform/cloudflare: the TBD body rules, the auth rate limit, the Access
bypasses on TBD staging.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`. The Terraform is read as text with the
helpers of test_tbd_edge.py; each expression is matched whole, so the model next to it is the rule Cloudflare runs,
and a change to the rule has to update the model and its cases here.
"""
import re
import unittest

from test_tbd_edge import HCL_STRING, TF, hcl_unquote, resource_body, route, tunnel_rules

TBD_HOSTS = {"app.thebetterdecision.com", "dev.thebetterdecision.com"}


def list_objects(body, attr):
    """The text of each `{ ... }` element of `<attr> = [ ... ]` in a resource body, by brace matching."""
    m = re.search(rf"(?m)^\s*{re.escape(attr)}\s*=\s*\[", body)
    if not m:
        raise AssertionError(f"{attr} = [ not found")
    objs, depth, start, in_str, i = [], 0, None, False, m.end()
    while i < len(body):
        c = body[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                start = i + 1
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                objs.append(body[start:i])
        elif c == "]" and depth == 0:
            return objs
        i += 1
    raise AssertionError(f"{attr} = [ is not closed")


def ruleset_rules(path, name):
    """The rules of a cloudflare_ruleset by ref: {ref: {attr: value}} for its string and boolean attributes."""
    rules = {}
    for obj in list_objects(resource_body((TF / path).read_text(), "cloudflare_ruleset", name), "rules"):
        attrs = {k: hcl_unquote(v) for k, v in re.findall(rf"(?m)^\s*(\w+)\s*=\s*{HCL_STRING}\s*$", obj)}
        attrs.update(re.findall(r"(?m)^\s*(enabled)\s*=\s*(true|false)\s*$", obj))
        rules[attrs["ref"]] = attrs
    return rules


def normalized(raw):
    """Cloudflare's URL normalization of a path, as far as these tests need it: backslashes to slashes, repeated
    slashes merged, percent-encoded unreserved characters decoded, dot segments removed (RFC 3986 5.2.4)."""
    def unreserved(m):
        c = chr(int(m.group(1), 16))
        return c if re.fullmatch(r"[A-Za-z0-9._~-]", c) else "%" + m.group(1).upper()

    path = re.sub(r"%([0-9A-Fa-f]{2})", unreserved, re.sub(r"/{2,}", "/", raw.replace("\\", "/")))
    out, segs = [], path.split("/")[1:]
    for i, seg in enumerate(segs):
        if seg == "..":
            out = out[:-1]
        elif seg != ".":
            out.append(seg)
        if seg in (".", "..") and i == len(segs) - 1:
            out.append("")
    return "/" + "/".join(out)


class BodyLengthRequired(unittest.TestCase):
    """EDGE-02: a body on a TBD app host must declare its length; the declared-length cap (BE-13) stays as it is.

    Not matched, by design: a DELETE, GET, HEAD or OPTIONS over HTTP/2 that carries a body and no Content-Length.
    HTTP/2 has no Transfer-Encoding, and the browser sends an empty DELETE with no length, so the edge cannot tell
    those apart from an ordinary request; the app's own body limits apply there.
    """

    CL = 'http.request.headers["content-length"][0]'
    NAMES = r"lower\(http\.request\.headers\.names\[\*\]\)\[\*\]"

    def models(self):
        rules = ruleset_rules("cloudflare/body_limit.tf", "tbd_body_limit")
        self.assertEqual(list(rules), ["tbd_body_limit", "tbd_body_length_required"])
        for r in rules.values():
            self.assertEqual((r["action"], r["enabled"]), ("block", "true"), r["ref"])

        cl = re.escape(self.CL)
        cap = re.fullmatch(
            rf'\(http\.host in \{{"([^"]+)" "([^"]+)"\}}\) and \(len\({cl}\) gt (\d+) or '
            rf'\(len\({cl}\) eq (\d+) and {cl} gt "(\d+)"\)\)', rules["tbd_body_limit"]["expression"])
        self.assertIsNotNone(cap, rules["tbd_body_limit"]["expression"])
        cap_hosts = {cap.group(1), cap.group(2)}
        longer, same, limit = int(cap.group(3)), int(cap.group(4)), cap.group(5)

        req = re.fullmatch(
            rf'\(http\.host in \{{"([^"]+)" "([^"]+)"\}}\) and '
            rf'\(any\({self.NAMES} eq "transfer-encoding"\) or '
            rf'\(http\.request\.method in \{{((?:"[A-Z]+" ?)+)\}} and '
            rf'not any\({self.NAMES} eq "content-length"\)\)\) and '
            rf'not \(http\.host eq "([^"]+)" and http\.request\.uri\.path eq "([^"]+)" and '
            rf'raw\.http\.request\.uri\.path eq "([^"]+)"\)',
            rules["tbd_body_length_required"]["expression"])
        self.assertIsNotNone(req, rules["tbd_body_length_required"]["expression"])
        req_hosts = {req.group(1), req.group(2)}
        methods = set(re.findall(r'"([A-Z]+)"', req.group(3)))
        exempt = (req.group(4), req.group(5), req.group(6))

        def declared(headers):
            # http.request.headers: keys lowercased, every value of a repeated header; [0] is the first.
            values = [v for k, v in headers if k.lower() == "content-length"]
            return values[0] if values else None

        def over_cap(host, headers):
            v = declared(headers)
            return host in cap_hosts and v is not None and (len(v) > longer or (len(v) == same and v > limit))

        def no_length(host, method, raw, headers):
            # http.request.headers.names: the names as the client sent them; any() of no match is false.
            names = [k.lower() for k, _ in headers]
            return (host in req_hosts
                    and ("transfer-encoding" in names or (method in methods and "content-length" not in names))
                    and not (host == exempt[0] and normalized(raw) == exempt[1] and raw == exempt[2]))

        def blocked(host, method, raw, headers=()):
            return over_cap(host, headers) or no_length(host, method, raw, headers)

        return cap_hosts, req_hosts, methods, exempt, blocked

    def test_bodies_without_a_length_are_blocked_and_ordinary_requests_are_not(self):
        cap_hosts, req_hosts, methods, exempt, blocked = self.models()
        self.assertEqual(req_hosts, TBD_HOSTS)
        self.assertEqual(req_hosts, cap_hosts)
        # Browsers send Content-Length: 0 on an empty POST or PUT, none on an empty PATCH or DELETE; tbd's frontend
        # sends every PATCH with a JSON body and many DELETEs without one.
        self.assertEqual(methods, {"POST", "PUT", "PATCH"})
        chunked = [("transfer-encoding", "chunked")]
        for host in sorted(TBD_HOSTS):
            for path in ("/api/v1/security/csp-report", "/api/v1/webhooks/mailgun", "/api/v1/auth/login", "/"):
                # A chunked HTTP/1.1 body, whatever the method, and however the client writes the header.
                for method in ("POST", "PUT", "PATCH", "DELETE", "GET", "OPTIONS"):
                    self.assertTrue(blocked(host, method, path, chunked), (host, method, path))
                self.assertTrue(blocked(host, "POST", path, [("Transfer-Encoding", "chunked")]), (host, path))
                # An HTTP/2 write with no Content-Length.
                for method in ("POST", "PUT", "PATCH"):
                    self.assertTrue(blocked(host, method, path), (host, method, path))
            # Reads and an empty DELETE carry no length and are not matched (see the class docstring).
            for method in ("GET", "HEAD", "OPTIONS", "DELETE"):
                for path in ("/", "/api/v1/auth/status", "/api/v1/tags/7", "/health"):
                    self.assertFalse(blocked(host, method, path), (host, method, path))
            self.assertFalse(blocked(host, "GET", "/", [("TE", "trailers")]), host)
            # Normal writes: a JSON body, a browser's empty POST, an HTTP/1.1 client's capitalised header, a large
            # import under the cap, a DELETE with a body.
            for method, headers in (("POST", [("content-type", "application/json"), ("content-length", "57")]),
                                    ("POST", [("content-length", "0")]),
                                    ("PUT", [("content-length", "0")]),
                                    ("PATCH", [("Content-Type", "application/json"), ("Content-Length", "512")]),
                                    ("POST", [("CONTENT-LENGTH", "5308416")]),
                                    ("DELETE", [("content-length", "40")])):
                self.assertFalse(blocked(host, method, "/api/v1/import/confirm", headers), (host, method, headers))
            # A declared length over the cap is still blocked, whatever the method.
            for method in ("POST", "GET"):
                self.assertTrue(blocked(host, method, "/api/v1/import/upload", [("content-length", "16777217")]))
        # Other hosts of the zone are not matched.
        for host in ("thebetterdecision.com", "www.thebetterdecision.com", "x.thebetterdecision.com"):
            self.assertFalse(blocked(host, "POST", "/api/v1/security/csp-report", chunked), host)

    def test_only_the_mcp_server_request_line_is_exempt(self):
        _, _, _, exempt, blocked = self.models()
        host, path, raw = exempt
        self.assertEqual(exempt, ("dev.thebetterdecision.com", "/mcp", "/mcp"))
        # The exemption is the MCP server's tunnel route (it caps its own body), not a path that reaches the backend.
        self.assertEqual(route(tunnel_rules(), host, raw), "http://mcp.tbd-staging.svc.cluster.local:8001")
        for headers in ((), [("transfer-encoding", "chunked")]):
            self.assertFalse(blocked(host, "POST", raw, headers), headers)
        self.assertTrue(blocked(host, "POST", raw, [("content-length", "99999999")]))
        # Paths that normalize to /mcp but are not the request line an MCP client sends.
        for variant in ("/./mcp", "/x/../mcp", "//mcp", "/%6dcp", "/%6Dcp", "/m%63p", "\\mcp"):
            self.assertEqual(normalized(variant), path, variant)
            self.assertTrue(blocked(host, "POST", variant), variant)
        for h, p in (("app.thebetterdecision.com", "/mcp"), (host, "/mcp/"), (host, "/MCP"), (host, "/mcp/x"),
                     (host, "/api/v1/oauth/token")):
            self.assertTrue(blocked(h, "POST", p), (h, p))


class AuthRateLimitPaths(unittest.TestCase):
    """EDGE-05: the TBD sign-in and token paths share the zone's edge counter; chatty paths stay out of it."""

    EXPRESSION_MAX = 4096  # Cloudflare's limit on a rule expression, in characters

    def path_lists(self):
        text = (TF / "cloudflare/rate_limit.tf").read_text()
        m = re.search(r"(?ms)^\s*auth_rate_limit_paths\s*=\s*\{(.*?)^\s*\}\s*$", text)
        self.assertIsNotNone(m, "local.auth_rate_limit_paths not found")
        lists = {k: [hcl_unquote(s) for s in re.findall(HCL_STRING, v)]
                 for k, v in re.findall(r"(?ms)^\s*(\w+)\s*=\s*\[(.*?)\]", m.group(1))}
        self.assertEqual(set(lists), {"tbd", "ziftbook"})
        return text, lists

    def test_tbd_sign_in_and_token_paths_are_counted(self):
        _, lists = self.path_lists()
        tbd = lists["tbd"]
        for path in ("/api/v1/auth/login", "/api/v1/auth/verify", "/api/v1/auth/google/callback",
                     "/api/v1/auth/sso-stepup/callback", "/api/v1/oauth/token", "/api/v1/oauth/register"):
            self.assertIn(path, tbd)
        # One person reaches 20 in 10 s on these (tabs refreshing, typing in sign-up, a burst of CSP reports), and the
        # counter is the one sign-in uses. The OAuth consent endpoints are not sign-in either.
        for path in ("/api/v1/auth/refresh", "/api/v1/auth/check-username", "/api/v1/security/csp-report",
                     "/api/v1/oauth/authorize", "/api/v1/oauth/authorize/context"):
            self.assertNotIn(path, tbd)

    def test_lists_are_exact_paths_without_duplicates_and_fit_the_expression_limit(self):
        text, lists = self.path_lists()
        # The template holds an interpolation with quotes of its own: take the line's outer quotes.
        body = resource_body(text, "cloudflare_ruleset", "auth_rate_limit")
        expr = hcl_unquote(re.search(r'(?m)^\s*expression\s*=\s*"(.*)"\s*$', body).group(1))
        joined = '${join(" ", [for p in each.value : jsonencode(p)])}'
        self.assertIn(joined, expr)
        for zone, paths in lists.items():
            self.assertEqual(len(paths), len(set(paths)), zone)
            for p in paths:
                self.assertRegex(p, r"^/[a-z0-9/._-]+$", (zone, p))
            rendered = expr.replace(joined, " ".join(f'"{p}"' for p in paths))
            self.assertLessEqual(len(rendered), self.EXPRESSION_MAX, zone)


class StagingAccessBypass(unittest.TestCase):
    """EDGE-06: on TBD staging, Access bypasses exactly what an MCP client calls without a browser."""

    def bypass_paths(self, name):
        text = (TF / "cloudflare/access.tf").read_text()
        body = resource_body(text, "cloudflare_zero_trust_access_application", name)
        m = re.search(r"for_each\s*=\s*toset\(\[(.*?)\]\)", body, re.S)
        self.assertIsNotNone(m, f"{name}: for_each = toset([...]) not found")
        self.assertRegex(body, r'domain\s*=\s*"\$\{local\.access_host\}\$\{each\.key\}"')
        return [hcl_unquote(s) for s in re.findall(HCL_STRING, m.group(1))]

    def test_mcp_bypass_is_the_exact_headless_endpoints(self):
        mcp = self.bypass_paths("tbd_staging_mcp")
        self.assertEqual(len(mcp), len(set(mcp)))
        self.assertEqual(set(mcp), {
            "/mcp",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/api/v1/oauth/register",
            "/api/v1/oauth/token",
        })
        for p in mcp:
            self.assertNotIn("*", p)

    def test_consent_and_session_paths_stay_behind_access(self):
        paths = self.bypass_paths("tbd_staging_mcp") + self.bypass_paths("tbd_staging_smoke")

        def bypassed(path):
            # An application path covers its subpaths, and a trailing * all of them. A plain string prefix is wider
            # than that, so a path this finds not bypassed is not bypassed by Access either.
            return any(path.startswith(p.rstrip("*")) for p in paths)

        for path in ("/mcp", "/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-authorization-server",
                     "/api/v1/oauth/register", "/api/v1/oauth/token", "/health", "/robots.txt"):
            self.assertTrue(bypassed(path), path)
        for path in ("/api/v1/oauth/authorize", "/api/v1/oauth/authorize/context", "/api/v1/oauth/",
                     "/oauth/authorize", "/api/v1/auth/status", "/api/v1/auth/me", "/api/v1/auth/login", "/",
                     "/_next/static/x.js"):
            self.assertFalse(bypassed(path), path)


if __name__ == "__main__":
    unittest.main()
