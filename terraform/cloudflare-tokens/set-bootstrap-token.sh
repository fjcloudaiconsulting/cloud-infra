#!/usr/bin/env bash
# Reads the Cloudflare bootstrap token (account-owned, Account API Tokens: Edit) on stdin, checks it, and stores it
# as the sensitive env var CLOUDFLARE_API_TOKEN of HCP Terraform workspace FlamaCorp/cloudflare-tokens (INFRA-133).
# It also writes ~/Downloads/INFRA-133-imported-tokens.json: every other account-owned token, except those named
# "tf: ...", as the API describes it (id, name, policies, conditions; never a value), in the shape of
# imported-tokens.json. Prints nothing secret. Needs the owner's HCP Terraform login (~/.terraform.d).
# Usage, from the repo root: pbpaste | bash terraform/cloudflare-tokens/set-bootstrap-token.sh; pbcopy </dev/null
set -euo pipefail
CF="$(cat)"; CF="${CF//[$'\r\n ']/}"
[ -n "$CF" ] || { echo "empty token on stdin"; exit 1; }
export CF
python3 - <<'PY'
import json, os, re, sys, urllib.request, urllib.error
ACC = "fb5eb042529f2621c6de3f34c6801174"
# Must match local.pg in terraform/cloudflare-tokens/main.tf.
PG = {"4755a26eedb94da69e1066d98aa820be": "DNS Write",
      "c03055bc037c4ea9afb9a9f104b7b721": "SSL and Certificates Write", "c8fed203ed3043cba015a93ad1616f1f": "Zone Read",
      "3030687196b94b638145a3953da2b699": "Zone Settings Write", "fb6778dc191143babbfaa57993f1d275": "Zone WAF Write",
      "e6d2666161e84845a636613608cee8d5": "Zone Write", "74e1036f577a48528b78d2413b40538d": "Dynamic URL Redirects Write"}
cf = os.environ["CF"]
tfc = json.load(open(os.path.expanduser("~/.terraform.d/credentials.tfrc.json")))["credentials"]["app.terraform.io"]["token"]

def call(url, tok, method="GET", body=None, ctype="application/json"):
    req = urllib.request.Request(url, method=method, data=body and json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None

# The workspace by name, before anything else: a failed lookup stops the run with nothing stored.
st, d = call("https://app.terraform.io/api/v2/organizations/FlamaCorp/workspaces/cloudflare-tokens", tfc,
             ctype="application/vnd.api+json")
if st != 200:
    sys.exit("HCP Terraform: cannot read workspace cloudflare-tokens (HTTP %s); nothing stored." % st)
WS = d["data"]["id"]

api = "https://api.cloudflare.com/client/v4/accounts/" + ACC
st, d = call(api + "/tokens/verify", cf)
if st != 200 or not d.get("success"):
    sys.exit("Cloudflare says this is not a valid account-owned token of the account; nothing stored.")
me = d["result"]["id"]
st, d = call(api + "/tokens/permission_groups?per_page=1000", cf)
if st != 200:
    sys.exit("The token cannot list permission groups: it lacks Account API Tokens; nothing stored.")
live = {g["id"]: g["name"] for g in d["result"]}
bad = {i: n for i, n in PG.items() if live.get(i) != n}
print("Permission group ids:", "OK" if not bad else "MISMATCH %s" % bad)

st, d = call(api + "/tokens?per_page=50", cf)
if st != 200:
    sys.exit("The token cannot list account tokens; nothing stored.")
if d.get("result_info", {}).get("total_count", 0) > len(d["result"]):
    sys.exit("More than 50 account tokens: page through them before importing; nothing stored.")
out, skipped = {}, []
for t in d["result"]:
    # Expired or exposed-and-revoked tokens drop out of the provider's state on read: an import of one fails the plan.
    if t["id"] == me or t["name"].startswith("tf: ") or t.get("status") in ("expired", "revoked (exposed)"):
        skipped.append("%s (%s)" % (t["name"], t.get("status")))
        continue
    # The order the provider keeps after an import (sortPolicies in its custom.go): groups by id, policies by
    # (effect, resources). Any other order plans an update of the imported token. resources is the exact compact
    # string the API returns: the provider compares it byte for byte.
    policies = [{"effect": p["effect"], "resources": json.dumps(p["resources"], separators=(",", ":")),
                 "permission_groups": sorted(({"id": g["id"], "name": g.get("name", "")} for g in p["permission_groups"]),
                                             key=lambda g: g["id"])}
                for p in t["policies"]]
    entry = {"id": t["id"], "name": t["name"], "policies": sorted(policies, key=lambda p: (p["effect"], p["resources"]))}
    for k in ("expires_on", "not_before"):
        if t.get(k):
            entry[k] = t[k]
    if t.get("condition"):
        entry["condition"] = t["condition"]
    if t.get("status") != "active":
        entry["status_note"] = t.get("status")
    key = re.sub(r"[^a-z0-9]+", "_", t["name"].lower()).strip("_")
    out[key if key not in out else key + "_" + t["id"][:6]] = entry
path = os.path.expanduser("~/Downloads/INFRA-133-imported-tokens.json")
with open(path, "w") as f:
    json.dump(out, f, indent=2, sort_keys=True)
    f.write("\n")
print("Wrote %d tokens to import (%s) to %s; left out: %s" % (len(out), ", ".join(e["name"] for e in out.values()), path, skipped))

tfapi = "https://app.terraform.io/api/v2/workspaces/" + WS + "/vars"
st, d = call(tfapi, tfc, ctype="application/vnd.api+json")
existing = [v["id"] for v in (d or {}).get("data", []) if v["attributes"]["key"] == "CLOUDFLARE_API_TOKEN"]
attrs = {"key": "CLOUDFLARE_API_TOKEN", "value": cf, "category": "env", "sensitive": True,
         "description": "Bootstrap token, Account API Tokens: Edit, hand-made (INFRA-133)"}
if existing:
    st, _ = call(tfapi + "/" + existing[0], tfc, "PATCH", {"data": {"id": existing[0], "type": "vars", "attributes": attrs}}, "application/vnd.api+json")
else:
    st, _ = call(tfapi, tfc, "POST", {"data": {"type": "vars", "attributes": attrs}}, "application/vnd.api+json")
print("HCP Terraform:", "stored" if st in (200, 201) else "FAILED, HTTP %s" % st)
sys.exit(0 if st in (200, 201) else 1)
PY
unset CF
