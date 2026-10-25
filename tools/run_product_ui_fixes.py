"""Local navigation regression; synthetic fixtures only, no model calls."""

import json
import os
import secrets
import subprocess
from uuid import uuid4

import httpx

r = subprocess.run(
    [
        r"D:\DockerDesktop\resources\bin\docker.exe",
        "inspect",
        "echotrace-acceptance-222c498ffb-api-1",
    ],
    capture_output=True,
    check=True,
)
a = dict(s.split("=", 1) for s in json.loads(r.stdout)[0]["Config"]["Env"] if "=" in s)
assert a["SUPABASE_URL"] == "http://host.docker.internal:55321"
email = "navigation-" + uuid4().hex + "@acceptance.local"
password = secrets.token_urlsafe(24)
with httpx.Client(trust_env=False, timeout=10) as c:
    r = c.post(
        "http://127.0.0.1:55321/auth/v1/signup",
        headers={"apikey": a["SUPABASE_ANON_KEY"]},
        json={"email": email, "password": password},
    )
    assert r.status_code == 200
r = subprocess.run(
    ["node", "tools/check_product_fixes.mjs"],
    env={**os.environ, "NAV_EMAIL": email, "NAV_PASSWORD": password},
    check=False,
)
raise SystemExit(r.returncode)
