"""Exercise the deterministic test provider in-process, no socket or model."""

import importlib.util
import io
import json
from pathlib import Path

path = Path(__file__).with_name("mock_provider.py")
spec = importlib.util.spec_from_file_location("mock_provider", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
handler = object.__new__(module.Handler)
handler.path = "/v1/embeddings"
raw = json.dumps({"input": "合成测试"}).encode()
handler.headers = {"Content-Length": str(len(raw))}
handler.rfile = io.BytesIO(raw)
results = []
handler.send_json = results.append
handler.do_POST()
assert len(results[0]["data"][0]["embedding"]) == 1024
print("Mock provider embedding contract passed (not Docker/AI validation).")
