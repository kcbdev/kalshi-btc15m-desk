"""Small Coolify API helper (read-only inspection for now).

Token comes from the COOLIFY_ACCESS_TOKEN environment variable.
Usage: python scripts/coolify_api.py projects|servers|apps|app <uuid>|storages <uuid>
"""
import json
import os
import sys

import requests

TOKEN = os.environ.get("COOLIFY_ACCESS_TOKEN", "")
BASE = os.environ.get("COOLIFY_BASE", "https://coolify.kcb.ma/api/v1")
HDR = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}


def get(path, **kw):
    r = requests.get(BASE + path, headers=HDR, timeout=40, **kw)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return r.text


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "projects"
    if cmd == "projects":
        for p in get("/projects"):
            print(p["name"], p["uuid"])
    elif cmd == "servers":
        for s in get("/servers"):
            print(s["name"], s["uuid"], s.get("ip"))
    elif cmd == "apps":
        for a in get("/applications"):
            print(f"{a['name'][:34]:36s} {a.get('build_pack'):12s} {a.get('fqdn') or '-':42s} {a['uuid']}")
    elif cmd == "app":
        d = get(f"/applications/{sys.argv[2]}")
        keep = ["uuid", "name", "fqdn", "build_pack", "ports_exposes", "git_repository", "git_branch",
                "dockerfile_location", "base_directory", "project_uuid", "destination", "status",
                "custom_docker_run_options", "settings"]
        for k in keep:
            if k in d:
                print(k, "=", json.dumps(d[k])[:300])
    elif cmd == "storages":
        print(json.dumps(get(f"/applications/{sys.argv[2]}/storages"), indent=1)[:1200])
    elif cmd == "envs":
        for e in get(f"/applications/{sys.argv[2]}/envs"):
            print(e.get("key"), "=", (e.get("value") or "")[:40], "| build:", e.get("is_buildtime"),
                  "| preview:", e.get("is_preview"))
    else:
        print("unknown command", cmd)


if __name__ == "__main__":
    main()
