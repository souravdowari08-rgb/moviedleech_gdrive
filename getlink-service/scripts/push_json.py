#!/usr/bin/env python3
"""Push state.json / meta.json to your running GetLink server (no extra packages needed).

    python scripts/push_json.py --state state.json --meta meta.json
    python scripts/push_json.py --server https://api.example.com --admin-key SECRET --state state.json

Defaults come from environment variables GETLINK_SERVER and GETLINK_ADMIN_KEY.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def put(server: str, key: str, name: str, path: str):
    with open(path, "rb") as f:
        body = f.read()
    json.loads(body.decode("utf-8-sig"))  # fail early on invalid JSON
    req = urllib.request.Request(
        f"{server.rstrip('/')}/admin/{name}",
        data=body,
        method="PUT",
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"{name}: OK  {r.read().decode()}")
            return True
    except urllib.error.HTTPError as e:
        print(f"{name}: FAILED {e.code}  {e.read().decode()}", file=sys.stderr)
    except Exception as e:
        print(f"{name}: FAILED {e}", file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.getenv("GETLINK_SERVER", "http://127.0.0.1:8000"))
    ap.add_argument("--admin-key", default=os.getenv("GETLINK_ADMIN_KEY", ""))
    ap.add_argument("--state", help="path to state.json")
    ap.add_argument("--meta", help="path to meta.json")
    a = ap.parse_args()
    if not a.admin_key:
        sys.exit("admin key missing (use --admin-key or GETLINK_ADMIN_KEY)")
    if not (a.state or a.meta):
        sys.exit("nothing to push: pass --state and/or --meta")
    ok = True
    if a.state:
        ok &= put(a.server, a.admin_key, "state", a.state)
    if a.meta:
        ok &= put(a.server, a.admin_key, "meta", a.meta)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
