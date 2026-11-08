"""Prepare the dev Home Assistant from docker-compose.dev.yml (standard library only).

    docker compose -f docker-compose.dev.yml up -d --build loop homeassistant
    python tools/ha-dev/setup.py

Steps, each skipped when already done:
  1. onboarding: creates the dev user (login screen: pick it, no password on this machine)
  2. adds the Shabbat Elevator Tracker integration for the loop tracker, floor 3
  3. writes the "Shabbat Elevator" dashboard with this device's entity IDs

The dev user's password only matters for API access; set HA_DEV_PASSWORD to choose it.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOMAIN = "shabbat_elevator_tracker"
TEMPLATE = ROOT / "tools" / "ha-dev" / "shabbat-elevator-dashboard.yaml"
DASHBOARD = ROOT / "data" / "ha-dev" / "dashboards" / "shabbat-elevator.yaml"
TEMPLATE_PREFIX = "example_building"


class HA:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.client_id = self.base + "/"
        self.token: str | None = None

    def call(self, method: str, path: str, body=None, form: bool = False, auth: bool = True):
        headers = {}
        data = None
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
        return json.loads(raw) if "json" in ctype else raw.decode()

    def wait_ready(self, timeout: float = 300) -> None:
        end = time.time() + timeout
        while True:
            try:
                self.call("GET", "/manifest.json", auth=False)
                return
            except (urllib.error.URLError, ConnectionError, OSError):
                if time.time() > end:
                    sys.exit(f"Home Assistant did not answer at {self.base}")
                time.sleep(3)

    def _token_from_code(self, code: str) -> None:
        tok = self.call("POST", "/auth/token", {"grant_type": "authorization_code", "code": code,
                                                "client_id": self.client_id}, form=True, auth=False)
        self.token = tok["access_token"]

    def onboard_or_login(self, username: str, password: str) -> None:
        try:
            steps = {s["step"]: s["done"] for s in self.call("GET", "/api/onboarding", auth=False)}
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            steps = {}  # onboarding finished: Home Assistant no longer serves it
        if not steps.get("user", True):
            print("onboarding: creating the dev user")
            r = self.call("POST", "/api/onboarding/users", {
                "client_id": self.client_id, "name": "Dev", "username": username,
                "password": password, "language": "en"}, auth=False)
            self._token_from_code(r["auth_code"])
        else:
            flow = self.call("POST", "/auth/login_flow", {
                "client_id": self.client_id, "handler": ["homeassistant", None],
                "redirect_uri": self.client_id}, auth=False)
            r = self.call("POST", f"/auth/login_flow/{flow['flow_id']}", {
                "client_id": self.client_id, "username": username, "password": password}, auth=False)
            if r.get("type") != "create_entry":
                sys.exit("login failed: set HA_DEV_PASSWORD to the dev user's password")
            self._token_from_code(r["result"])
        for step, body in (("core_config", {}), ("analytics", {}),
                           ("integration", {"client_id": self.client_id, "redirect_uri": self.client_id})):
            if not steps.get(step, True):
                self.call("POST", f"/api/onboarding/{step}", body)
                print(f"onboarding: {step} done")

    def flow(self, start_path: str, start_body: dict, *steps: dict) -> dict:
        r = self.call("POST", start_path, start_body)
        for user_input in steps:
            if r.get("type") != "form":
                break
            r = self.call("POST", f"{start_path}/{r['flow_id']}", user_input)
        return r

    def ensure_integration(self, url: str, floor: str) -> None:
        entries = self.call("GET", f"/api/config/config_entries/entry?domain={DOMAIN}")
        if entries:
            print(f"integration: already set up ({entries[0]['title']}); change the floor under Configure")
            return
        r = self.flow("/api/config/config_entries/flow", {"handler": DOMAIN, "show_advanced_options": False},
                      {"url": url}, {"home_floor": floor})
        if r.get("type") != "create_entry":
            sys.exit(f"integration flow did not finish: {json.dumps(r)}")
        print(f"integration: added ({r['title']}), floor {floor}")

    def entity_prefix(self, timeout: float = 60) -> str:
        end = time.time() + timeout
        while True:
            ids = self.call("POST", "/api/template",
                            {"template": f"{{{{ integration_entities('{DOMAIN}') | join(',') }}}}"}).split(",")
            for e in ids:
                if e.startswith("sensor.") and e.endswith("_next_arrival"):
                    return e[len("sensor."):-len("_next_arrival")]
            if time.time() > end:
                sys.exit("no next-arrival sensor found")
            time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ha", default="http://localhost:8123")
    ap.add_argument("--tracker", default="http://loop:8080", help="tracker URL as seen from Home Assistant")
    ap.add_argument("--floor", default="3")
    args = ap.parse_args()

    ha = HA(args.ha)
    ha.wait_ready()
    ha.onboard_or_login("dev", os.environ.get("HA_DEV_PASSWORD", "dev-only-password"))
    ha.ensure_integration(args.tracker, args.floor)
    prefix = ha.entity_prefix()
    DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD.write_text(TEMPLATE.read_text(encoding="utf-8").replace(TEMPLATE_PREFIX, prefix), encoding="utf-8")
    print(f"dashboard: written for entities '{prefix}_*'")
    print(f"open {args.ha}/shabbat-elevator/elevator")


if __name__ == "__main__":
    main()
