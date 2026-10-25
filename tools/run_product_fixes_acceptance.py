"""Local HTTP product-fix acceptance. Synthetic users; mock model only."""

import json
import os
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx

root = Path.cwd()
docker = r"D:\DockerDesktop\resources\bin\docker.exe"
name = "echotrace-product-fixes-api"
checks = []
tokens = []


def command(args, env=None):
    r = subprocess.run([docker, *args], capture_output=True, env=env, check=False)
    if r.returncode:
        raise RuntimeError("Container command failed; private output suppressed")
    return r.stdout


def check(label, value):
    checks.append({"check": label, "passed": bool(value)})
    (root / ".acceptance/product-http-checks.json").write_text(
        json.dumps(checks, indent=2), encoding="utf-8"
    )
    print(("PASS " if value else "FAIL ") + label, flush=True)
    assert value, label


api = json.loads(command(["inspect", "echotrace-acceptance-222c498ffb-api-1"]))[0]
a = dict(x.split("=", 1) for x in api["Config"]["Env"] if "=" in x)
assert a["SUPABASE_URL"] == "http://host.docker.internal:55321"
worker = json.loads(command(["inspect", "echotrace-acceptance-222c498ffb-worker-1"]))[0]
assert not worker["State"]["Running"], (
    "Pause Worker first; synthetic jobs must not call real models"
)
network = next(iter(api["NetworkSettings"]["Networks"]))
command(["start", "echotrace-acceptance-222c498ffb-model-1"])
env = {
    **os.environ,
    "SUPABASE_URL": a["SUPABASE_URL"],
    "SUPABASE_ANON_KEY": a["SUPABASE_ANON_KEY"],
    "DASHSCOPE_API_KEY": "synthetic",
    "DASHSCOPE_BASE_URL": "http://model:8089/v1",
    "CHAT_MODEL": "mock",
    "EMBEDDING_MODEL": "mock",
    "APP_ORIGIN": "http://127.0.0.1:8081",
}
args = [
    "run",
    "--rm",
    "-d",
    "--name",
    name,
    "--network",
    network,
    "-p",
    "127.0.0.1:18002:8000",
]
for key in [
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_BASE_URL",
    "CHAT_MODEL",
    "EMBEDDING_MODEL",
    "APP_ORIGIN",
]:
    args += ["-e", key]
args += ["echotrace:acceptance"]
command(args, env)
base = "http://127.0.0.1:18002"
auth = "http://127.0.0.1:55321"
key = a["SUPABASE_ANON_KEY"]


def control(c, **values):
    c.post("http://127.0.0.1:8089/__control", json=values).raise_for_status()


def user(c):
    r = c.post(
        auth + "/auth/v1/signup",
        headers={"apikey": key},
        json={
            "email": "http-product-" + uuid4().hex + "@acceptance.local",
            "password": secrets.token_urlsafe(24),
        },
    )
    r.raise_for_status()
    data = r.json()
    token = data["access_token"]
    tokens.append(token)
    return data["user"]["id"], {"Authorization": "Bearer " + token, "apikey": key}


try:
    with httpx.Client(trust_env=False, timeout=30) as c:
        for _ in range(40):
            try:
                if c.get(base + "/ready").status_code == 200:
                    break
            except httpx.RequestError:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError("Test API not ready")
        control(c, fail_next=0, delay_seconds=0, chat_usage_tokens=10, bad_chat=False)
        uid, h = user(c)
        other, oh = user(c)
        body = {"content": "合成聊天重试测试", "input_type": "text"}
        headers = {**h, "Idempotency-Key": "failure-retry"}
        control(c, fail_next=3, fail_status=503)
        r = c.post(base + "/chat", headers=headers, json=body)
        check("model_failure_returns_502", r.status_code == 502)
        r = c.post(base + "/chat", headers=headers, json=body)
        check("same_chat_retry_succeeds", r.status_code == 200)
        result = r.json()
        tid = result["thread_id"]
        mid = result["moment_id"]
        msgs = c.get(base + f"/threads/{tid}/messages", headers=h).json()
        check(
            "retry_has_one_user_and_one_assistant",
            sum(x["role"] == "user" for x in msgs) == 1
            and sum(x["role"] == "assistant" for x in msgs) == 1,
        )
        before = c.get("http://127.0.0.1:8089/__stats").json()["requests"]
        replay = c.post(base + "/chat", headers=headers, json=body)
        after = c.get("http://127.0.0.1:8089/__stats").json()["requests"]
        check(
            "successful_replay_uses_cache_without_model",
            replay.json() == result and after == before,
        )
        check(
            "chat_key_conflict_409",
            c.post(
                base + "/chat", headers=headers, json={"content": "另一句"}
            ).status_code
            == 409,
        )
        check(
            "other_user_cannot_read_thread",
            c.get(base + f"/threads/{tid}/messages", headers=oh).status_code == 404,
        )
        check(
            "other_user_cannot_delete_moment",
            c.delete(base + f"/moments/{mid}", headers=oh).status_code == 404,
        )
        # Cursor ties: all 51 records share one timestamp, no embedding jobs created.
        rows = [
            {
                "id": str(uuid4()),
                "user_id": other,
                "content": "合成分页 " + str(i),
                "mode": "capture",
                "memory_enabled": False,
                "created_at": "2026-10-01T01:00:00Z",
                "event_at": "2026-09-01T12:00:00+08:00",
                "event_time_source": "user_confirmed",
            }
            for i in range(51)
        ]
        c.post(auth + "/rest/v1/moments", headers=oh, json=rows).raise_for_status()
        first = c.get(base + "/moments?limit=50", headers=oh).json()
        last = first[-1]
        second = c.get(
            base + "/moments",
            headers=oh,
            params={
                "limit": 50,
                "before_at": last["created_at"],
                "before_id": last["id"],
            },
        ).json()
        check(
            "keyset_pagination_51_tied_timestamps",
            len(first) == 50
            and len(second) == 1
            and len({x["id"] for x in first + second}) == 51,
        )
        target = first[0]
        edited = c.patch(
            base + "/moments/" + target["id"],
            headers=oh,
            json={"content": "合成修改后原文"},
        )
        check(
            "content_edit_keeps_memory_and_event_date",
            edited.status_code == 200
            and edited.json()["memory_enabled"] is False
            and edited.json()["event_at"] is not None,
        )
        toggled = c.patch(
            base + f"/moments/{target['id']}/memory?enabled=true", headers=oh
        )
        check(
            "permission_toggle_works",
            toggled.status_code == 200 and toggled.json()["memory_enabled"] is True,
        )
        check(
            "single_record_delete",
            c.delete(base + "/moments/" + target["id"], headers=oh).status_code == 200
            and c.get(base + "/moments/" + target["id"], headers=oh).status_code == 404,
        )
        control(c, delay_seconds=1)

        def send(_):
            with httpx.Client(trust_env=False, timeout=20) as client:
                return client.post(
                    base + "/chat",
                    headers={**h, "Idempotency-Key": "parallel"},
                    json={"content": "合成并发消息"},
                ).status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(send, range(4)))
        check(
            "concurrent_chat_does_not_duplicate",
            statuses.count(200) == 1 and all(x in [200, 409] for x in statuses),
        )
        # Clear while a mock model response is still in flight, never a paid call.
        control(c, delay_seconds=2)

        def delayed():
            with httpx.Client(trust_env=False, timeout=20) as client:
                return client.post(
                    base + "/chat",
                    headers={**h, "Idempotency-Key": "clear-race"},
                    json={"content": "合成清空竞争"},
                ).status_code

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(delayed)
            for _ in range(50):
                data = c.get(base + "/moments?limit=100", headers=h).json()
                if any(x["content"] == "合成清空竞争" for x in data):
                    break
                time.sleep(0.05)
            cleared = c.delete(base + "/account/data", headers=h)
            status = future.result(timeout=15)
        check(
            "clear_during_chat_prevents_late_reply",
            cleared.status_code == 200
            and status == 409
            and c.get(base + "/moments", headers=h).json() == [],
        )
        check(
            "clear_preserves_other_user_records",
            len(c.get(base + "/moments?limit=100", headers=oh).json()) == 50,
        )
        check(
            "deleted_thread_is_unavailable",
            c.get(base + f"/threads/{tid}/messages", headers=h).status_code == 404,
        )
finally:
    with httpx.Client(trust_env=False, timeout=10) as c:
        try:
            control(c, fail_next=0, delay_seconds=0)
        except httpx.HTTPError:
            pass
        for token in tokens:
            try:
                c.delete(
                    base + "/account/data", headers={"Authorization": "Bearer " + token}
                ).raise_for_status()
            except httpx.HTTPError:
                print("Synthetic account cleanup needs review; no real data touched")
    command(["stop", "--timeout", "20", name])
