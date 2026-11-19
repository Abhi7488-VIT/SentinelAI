"""Register the testbed shop in SentinelAI and point the testbed Collector at SentinelAI's ingest.

Usage (SentinelAI stack running): make sentinel-connect [REGISTER=1]
  or, from backend/: uv run python -m scripts.connect_testbed [--register]
Credentials: SENTINELAI_EMAIL / SENTINELAI_PASSWORD from the environment or the repo-root .env.

What it does, all through SentinelAI's public API (idempotent; safe to re-run):
1. logs in (or registers with --register), uses the account's first organization;
2. creates or updates project `shop` and its five services from testbed/sentinelai-registry.json
   (descriptions, environment, repo URL, memory limits) and replaces their declared dependencies;
3. rotates one ingest key per service (revokes previous `testbed-collector` keys, creates new);
4. writes SENTINELAI_OTLP_ENDPOINT and SENTINELAI_KEY_* into testbed/.env (gitignored).
Then `make testbed-up` (re)starts the Collector with the SentinelAI overlay.

Keys are written only to testbed/.env and never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY_NAME = "testbed-collector"
DEFAULT_ENDPOINT = "http://sentinelai-ingest:8001/api/v1/otlp"


def env_key(service: str) -> str:
    return "SENTINELAI_KEY_" + service.upper().replace("-", "_")


def _repo_web_url() -> str | None:
    try:
        url = subprocess.run(  # noqa: S603  (fixed argv, no untrusted input)
            ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    if url.startswith("https://") and "@" not in url:
        return url.removesuffix(".git")
    return None


class Api:
    def __init__(self, base: str) -> None:
        self.client = httpx.Client(base_url=base, timeout=15)
        self.headers: dict[str, str] = {}

    def call(self, method: str, path: str, **kw: Any) -> Any:
        resp = self.client.request(method, path, headers=self.headers, **kw)
        if resp.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else None

    def items(self, path: str, **params: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = self.call(
                "GET",
                path,
                params={**params, "limit": 100, **({"cursor": cursor} if cursor else {})},
            )
            out.extend(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                return out


def _login(api: Api, email: str, password: str, register: bool) -> None:
    resp = api.client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if resp.status_code == 401 and register:
        resp = api.client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": password,
                "display_name": "Testbed operator",
                "org_name": "Testbed",
            },
        )
    if resp.status_code >= 400:
        raise SystemExit(f"login failed: {resp.status_code} {resp.text[:200]}")
    api.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _update_env_file(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out = []
    for line in lines:
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    out.extend(f"{k}={v}" for k, v in values.items() if k not in seen)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.chmod(0o600)


def connect(args: argparse.Namespace) -> dict[str, Any]:
    registry = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    api = Api(args.api)
    _login(api, args.email, args.password, args.register)
    orgs = api.items("/api/v1/orgs")
    if not orgs:
        raise SystemExit("the account has no organization")
    org = orgs[0]["id"]
    base = f"/api/v1/orgs/{org}"

    spec = registry["project"]
    projects = {p["slug"]: p for p in api.items(f"{base}/projects")}
    project = projects.get(spec["slug"]) or api.call(
        "POST",
        f"{base}/projects",
        json={"name": spec["name"], "slug": spec["slug"], "description": spec["description"]},
    )
    env = registry["environment"]
    web = _repo_web_url()
    existing = {
        s["name"]: s
        for s in api.items(f"{base}/services", project_id=project["id"], environment=env)
    }
    by_name: dict[str, dict[str, Any]] = {}
    for svc in registry["services"]:
        body = {
            "description": svc["description"],
            "repo_url": f"{web}/tree/main/{svc['repo_path']}" if web else None,
            "deployment": {"platform": "docker-compose", "runtime": "python3.12"},
            "telemetry_config": {"memory_limit_bytes": svc["memory_limit_bytes"]},
        }
        if svc["name"] in existing:
            by_name[svc["name"]] = api.call(
                "PATCH", f"{base}/services/{existing[svc['name']]['id']}", json=body
            )
        else:
            by_name[svc["name"]] = api.call(
                "POST",
                f"{base}/services",
                json={"project_id": project["id"], "name": svc["name"], "environment": env, **body},
            )
    for svc in registry["services"]:
        deps = [
            {"target_service_id": by_name[d]["id"]} if d in by_name else {"target_external": d}
            for d in svc["depends_on"]
        ]
        api.call(
            "PUT",
            f"{base}/services/{by_name[svc['name']]['id']}/dependencies",
            json={"dependencies": deps},
        )

    env_values = {"SENTINELAI_OTLP_ENDPOINT": args.endpoint}
    rotated = 0
    for name, svc in by_name.items():
        keys_path = f"{base}/services/{svc['id']}/api-keys"
        for key in api.items(keys_path):
            if key["name"] == KEY_NAME and key["revoked_at"] is None:
                api.call("POST", f"{keys_path}/{key['id']}/revoke")
                rotated += 1
        created = api.call("POST", keys_path, json={"name": KEY_NAME})
        env_values[env_key(name)] = created["key"]
    _update_env_file(Path(args.testbed_env), env_values)
    return {
        "org_id": org,
        "project_id": project["id"],
        "services": {n: s["id"] for n, s in by_name.items()},
        "keys_created": len(by_name),
        "keys_revoked": rotated,
        "testbed_env": str(args.testbed_env),
        "endpoint": args.endpoint,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--email", default=os.environ.get("SENTINELAI_EMAIL"))
    parser.add_argument("--register", action="store_true", help="create the account if login fails")
    parser.add_argument(
        "--registry", default=str(REPO_ROOT / "testbed" / "sentinelai-registry.json")
    )
    parser.add_argument("--testbed-env", default=str(REPO_ROOT / "testbed" / ".env"))
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    args = parser.parse_args()
    local = {k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if v}
    args.email = args.email or local.get("SENTINELAI_EMAIL")
    args.password = os.environ.get("SENTINELAI_PASSWORD") or local.get("SENTINELAI_PASSWORD")
    if not args.email or not args.password:
        sys.exit("set SENTINELAI_EMAIL and SENTINELAI_PASSWORD (or pass --email)")
    print(json.dumps(connect(args), indent=2))  # noqa: T201  (no secrets in this summary)


if __name__ == "__main__":
    main()
