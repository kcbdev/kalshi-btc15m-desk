"""Provision the BTC15M desk on Coolify and deploy it.

Idempotent: re-running finds the existing project/app/storage/env instead of
creating duplicates.

  python scripts/coolify_deploy.py status
  python scripts/coolify_deploy.py create
  python scripts/coolify_deploy.py storage
  python scripts/coolify_deploy.py deploy
  python scripts/coolify_deploy.py wait
"""
import json
import os
import sys
import time

import requests

TOKEN = os.environ.get("COOLIFY_ACCESS_TOKEN", "")
BASE = os.environ.get("COOLIFY_BASE", "https://coolify.kcb.ma/api/v1")
HDR = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json", "Content-Type": "application/json"}

PROJECT_NAME = "Kalshi"
SERVER_UUID = os.environ.get("COOLIFY_SERVER_UUID", "e4cowswcks844wow04c084wg")
REPO = "https://github.com/kcbdev/kalshi-btc15m-desk"
BRANCH = "main"
APP_NAME = "kalshi-btc15m-desk"
APP_DESCRIPTION = "BTC15M multi-agent paper-trading desk (simulation only, no real orders)"
FQDN = "https://kalshi.kcb.ma"
PORT = "8000"
STATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "coolify.json")


def call(method, path, payload=None, ok=(200, 201)):
    r = requests.request(method, BASE + path, headers=HDR, json=payload, timeout=60)
    if r.status_code not in ok:
        raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}


def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2)


def find_project():
    for p in call("GET", "/projects"):
        if p["name"] == PROJECT_NAME:
            return p
    return None


def find_app():
    for a in call("GET", "/applications"):
        if a["name"] == APP_NAME:
            return a
    return None


def cmd_status():
    st = load_state()
    print(json.dumps(st, indent=2))
    app = find_app()
    if app:
        d = call("GET", f"/applications/{app['uuid']}")
        print(json.dumps({k: d.get(k) for k in
                          ("uuid", "name", "fqdn", "status", "build_pack", "ports_exposes",
                           "git_repository", "git_branch", "project_uuid")}, indent=2))
    else:
        print("app not found")


def cmd_create():
    st = load_state()
    proj = find_project()
    if proj:
        print("project exists:", proj["uuid"])
    else:
        proj = call("POST", "/projects", {"name": PROJECT_NAME,
                                          "description": "Kalshi BTC15M paper-trading desk"})
        print("project created:", proj.get("uuid"))
    st["project_uuid"] = proj["uuid"]

    app = find_app()
    if app:
        print("app exists:", app["uuid"])
    else:
        payload = {
            "project_uuid": proj["uuid"],
            "server_uuid": SERVER_UUID,
            "environment_name": "production",
            "git_repository": REPO,
            "git_branch": BRANCH,
            "build_pack": "dockerfile",
            "dockerfile_location": "/Dockerfile",
            "base_directory": "/",
            "ports_exposes": PORT,
            "domains": FQDN,
            "name": APP_NAME,
            "description": APP_DESCRIPTION,
            "instant_deploy": False,
        }
        app = call("POST", "/applications/public", payload)
        print("app created:", app.get("uuid"), "| status:", app.get("status"))
    st["app_uuid"] = app["uuid"]
    save_state(st)

    # make sure the domain is set even on an existing app (API 4.6 calls it "domains")
    for body in ({"domains": FQDN, "ports_exposes": PORT}, {"fqdn": FQDN}):
        try:
            call("PATCH", f"/applications/{app['uuid']}", body, ok=(200, 201, 204))
            print("domain ensured:", FQDN)
            break
        except SystemExit as exc:
            print("patch rejected:", str(exc)[:200])


def cmd_storage():
    st = load_state()
    app_uuid = st.get("app_uuid") or find_app()["uuid"]
    cur = call("GET", f"/applications/{app_uuid}/storages")
    mounts = [s["mount_path"] for s in cur.get("persistent_storages", [])]
    if "/app/data" in mounts:
        print("storage exists:", mounts)
        return
    # Coolify 4.6 requires a storage type; "volume" = docker named volume
    body = {
        "type": "volume",
        "name": "btc15m-data",
        "mount_path": "/app/data",
    }
    r = call("POST", f"/applications/{app_uuid}/storages", body, ok=(200, 201))
    print("storage created:", json.dumps(r)[:400])


def cmd_deploy():
    st = load_state()
    app_uuid = st.get("app_uuid") or find_app()["uuid"]
    # Coolify 4.6: deploy is a POST with the uuid(s) in the body
    r = call("POST", "/deploy", {"uuid": app_uuid, "force": True}, ok=(200, 201))
    print("deploy triggered:", json.dumps(r)[:400])
    st["last_deploy"] = time.time()
    save_state(st)


def cmd_wait(timeout=900):
    st = load_state()
    app_uuid = st.get("app_uuid") or find_app()["uuid"]
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        deps = call("GET", "/deployments")
        mine = [d for d in deps if d.get("application_uuid") == app_uuid or
                d.get("resource_uuid") == app_uuid or app_uuid in json.dumps(d)]
        if mine:
            d = sorted(mine, key=lambda x: x.get("created_at") or "", reverse=True)[0]
            status = d.get("status")
            if status != last:
                print(f"deployment {d.get('deployment_uuid') or d.get('uuid')}: {status}")
                last = status
            if status in ("finished", "failed", "cancelled"):
                print(json.dumps({k: d.get(k) for k in ("status", "commit", "finished_at", "deployment_uuid")},
                                 indent=2, default=str))
                if status == "finished":
                    app = call("GET", f"/applications/{app_uuid}")
                    print("app status:", app.get("status"))
                return 0 if status == "finished" else 1
        time.sleep(10)
    print("timed out waiting for deployment")
    return 1


def main():
    if not TOKEN:
        print("COOLIFY_ACCESS_TOKEN missing")
        return 1
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    fn = {"status": cmd_status, "create": cmd_create, "storage": cmd_storage,
          "deploy": cmd_deploy, "wait": cmd_wait}.get(cmd)
    if not fn:
        print("unknown command", cmd)
        return 1
    return fn() or 0


if __name__ == "__main__":
    sys.exit(main())
