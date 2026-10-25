"""Local HTTP must work even when a developer's environment proxy is stopped."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.clients.ai import BailianClient
from app.clients.supabase import SupabaseClient
from app.config import Settings


@pytest.fixture
def local_endpoint(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.respond({"id": "synthetic-user"})

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path.endswith("product_schema_ready"):
                self.respond("product-fixes-v1")
            elif self.path.endswith("schema_ready"):
                self.respond("durable-v2")
            else:
                self.respond({"choices": [{"message": {"content": "synthetic reply"}}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


@pytest.mark.asyncio
async def test_bailian_does_not_depend_on_environment_proxy(local_endpoint):
    ai = BailianClient(Settings(
        _env_file=None,
        dashscope_api_key="synthetic",
        chat_model="synthetic",
        dashscope_base_url=local_endpoint,
    ))
    reply, _ = await ai.chat(system="synthetic", messages=[])
    assert reply == "synthetic reply"


@pytest.mark.asyncio
async def test_auth_and_worker_adapter_ignore_stopped_proxy(local_endpoint):
    db = SupabaseClient(Settings(
        _env_file=None, supabase_url=local_endpoint, supabase_anon_key="synthetic"
    ))
    try:
        assert await db.resolve_user("synthetic") == {"id": "synthetic-user"}
    finally:
        await db.aclose()


@pytest.mark.asyncio
async def test_ready_does_not_depend_on_environment_proxy(local_endpoint, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "settings", Settings(
        _env_file=None, supabase_url=local_endpoint, supabase_anon_key="synthetic"
    ))
    assert (await main.ready()).status_code == 200
