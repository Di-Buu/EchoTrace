"""Start the product against local Supabase; no cloud database or automatic migration."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))
from app.config import Settings

DOCKER = r"D:\DockerDesktop\resources\bin\docker.exe"
WORKSPACE = ROOT / ".acceptance/echotrace-acceptance-222c498ffb"


def main(args):
    env = {**os.environ, "PATH": str(Path(DOCKER).parent) + ";" + os.environ["PATH"]}
    hidden = []
    log = ROOT / ".acceptance/local-quality-start.log"
    log.parent.mkdir(exist_ok=True)

    def command(command_args, *, cwd=ROOT):
        result = subprocess.run(
            command_args, cwd=cwd, env=env, capture_output=True, check=False
        )
        text = result.stdout.decode("utf-8", errors="replace") + result.stderr.decode(
            "utf-8", errors="replace"
        )
        if result.returncode:
            for value in hidden:
                if value:
                    text = text.replace(value, "[redacted]")
            log.write_text(text, encoding="utf-8")
            raise RuntimeError(
                "启动步骤失败；已脱敏详情见 .acceptance/local-quality-start.log"
            )
        return result.stdout.decode("utf-8", errors="replace")

    command([DOCKER, "info", "--format", "{{.ServerVersion}}"])
    if not (WORKSPACE / "acceptance.json").exists():
        raise RuntimeError(
            "找不到已验收的隔离数据库目录，请按 UPGRADE.md 初始化；不会自动连接云数据库"
        )
    marker = json.loads((WORKSPACE / "acceptance.json").read_text(encoding="utf-8-sig"))
    if marker.get("local_only") is not True:
        raise RuntimeError("拒绝使用没有本地隔离标记的环境")
    # Starting the existing project preserves its volume; it does not reset or migrate.
    status_args = [
        "cmd.exe",
        "/d",
        "/c",
        "npm.cmd",
        "exec",
        "--offline",
        "--package=supabase@2.122.0",
        "--",
        "supabase",
        "status",
        "-o",
        "json",
        "--workdir",
        str(WORKSPACE),
    ]
    probe = subprocess.run(status_args, env=env, capture_output=True, check=False)
    if probe.returncode:
        command([sys.executable, "tools/run_local_supabase.py", str(WORKSPACE)])
        probe = subprocess.run(status_args, env=env, capture_output=True, check=False)
    if probe.returncode:
        raise RuntimeError("本地 Supabase 尚未就绪；不输出认证配置")
    local = json.loads(probe.stdout)
    if local["API_URL"] != "http://127.0.0.1:55321":
        raise RuntimeError("拒绝连接非本机数据库")
    hidden.extend((local["ANON_KEY"], local["SERVICE_ROLE_KEY"]))
    with httpx.Client(timeout=10, trust_env=False) as client:
        ready = client.post(
            local["API_URL"] + "/rest/v1/rpc/schema_ready",
            headers={"apikey": local["ANON_KEY"]},
            json={},
        )
        if ready.status_code != 200 or ready.json() != "durable-v2":
            raise RuntimeError(
                "数据库迁移未就绪；请先单独应用本地迁移，不会在启动时自动修改结构"
            )
        product = client.post(
            local["API_URL"] + "/rest/v1/rpc/product_schema_ready",
            headers={"apikey": local["ANON_KEY"]},
            json={},
        )
        if product.status_code != 200 or product.json() != "product-fixes-v1":
            raise RuntimeError("本地产品修复迁移尚未应用；请先按修复记录迁移，不会自动改库")
    model = Settings(_env_file=ROOT / "apps/api/.env")
    hidden.append(model.dashscope_api_key)
    if not args.mock and not (
        model.dashscope_api_key and model.chat_model and model.embedding_model
    ):
        raise RuntimeError(
            "请在 apps/api/.env 填好百炼模型配置；无需改其中的云数据库配置，本启动器会使用本地库"
        )
    if args.check_only:
        print("Docker、本地 Auth/Data API 和数据库结构已就绪；未启动收费 Worker。")
        print("模式：", "模拟模型" if args.mock else "真实百炼；使用时会消耗额度")
        return
    # Do not interrupt an acceptance run or silently replace the current real worker.
    active = command(
        [
            DOCKER,
            "ps",
            "--filter",
            "name=echotrace-real-acceptance-worker-1",
            "--format",
            "{{.Names}}",
        ]
    )
    if active.strip():
        raise RuntimeError("真实模型验收尚在运行，请等它结束后再启动日常使用环境")
    env.update(
        SUPABASE_URL="http://host.docker.internal:55321",
        SUPABASE_ANON_KEY=local["ANON_KEY"],
        SUPABASE_SERVICE_ROLE_KEY=local["SERVICE_ROLE_KEY"],
        ECHOTRACE_IMAGE="echotrace:acceptance",
        ECHOTRACE_BIND_PORT="18000",
        APP_ORIGIN=f"http://127.0.0.1:{args.port}",
        DASHSCOPE_BASE_URL=model.dashscope_base_url,
        DASHSCOPE_API_KEY=model.dashscope_api_key,
        CHAT_MODEL=model.chat_model,
        EMBEDDING_MODEL=model.embedding_model,
        EMBEDDING_DIMENSION=str(model.embedding_dimension),
        AGENT_MAX_TOKENS=str(args.token_budget),
        ANALYSIS_STRATEGY="dynamic",
        RETRIEVAL_FUSION="rrf",
        TASK_TIMEOUT_SECONDS="600",
    )
    source_hash = hashlib.sha256(
        b"".join(p.read_bytes() for p in sorted((ROOT / "apps/api/app").rglob("*.py")))
    ).hexdigest()
    image_probe = subprocess.run(
        [
            DOCKER,
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            "echotrace:acceptance",
            "-c",
            "import hashlib;from pathlib import Path;print(hashlib.sha256(b''.join(p.read_bytes() for p in sorted(Path('/app/app').rglob('*.py')))).hexdigest())",
        ],
        capture_output=True,
        check=False,
    )
    if image_probe.returncode or image_probe.stdout.decode().strip() != source_hash:
        print("正在构建与当前源码一致的本地镜像。", flush=True)
        command([DOCKER, "build", "--tag", "echotrace:acceptance", "."])
    build = ROOT / ".acceptance/web-quality"
    build_env = {
        **os.environ,
        "EXPO_NO_DOTENV": "1",
        "CI": "1",
        "EXPO_PUBLIC_API_URL": "http://127.0.0.1:18000",
        "EXPO_PUBLIC_SUPABASE_URL": local["API_URL"],
        "EXPO_PUBLIC_SUPABASE_ANON_KEY": local["ANON_KEY"],
    }
    print("正在准备只连接本机的网页。", flush=True)
    result = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "node_modules\\.bin\\expo.cmd",
            "export",
            "--platform",
            "web",
            "--output-dir",
            str(build),
        ],
        cwd=ROOT / "apps/mobile",
        env=build_env,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "网页构建失败，请先在 apps/mobile 安装依赖并运行 typecheck；未启动模型 Worker"
        )
    for path in build.rglob("*"):
        if path.suffix in {".js", ".html", ".json"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(
                value and len(value) > 10 and value in text
                for value in (local["SERVICE_ROLE_KEY"], model.dashscope_api_key)
            ):
                raise RuntimeError("网页产物检查失败：检测到不应公开的凭据，停止启动")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if (
                not Path(self.translate_path(self.path)).is_file()
                and not Path(self.path).suffix
            ):
                self.path = "/index.html"
            super().do_GET()

    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), partial(Handler, directory=str(build))
    )
    compose = [DOCKER, "compose", "-p", marker["project_id"], "-f", "compose.yaml"]
    if args.mock:
        compose += ["-f", "compose.smoke.yaml"]
    try:
        command(compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "120"])
        print(f"已启动：http://127.0.0.1:{args.port}/", flush=True)
        print(
            "模型："
            + ("本地模拟，仅验证界面" if args.mock else "真实百炼，实际使用会消耗额度"),
            flush=True,
        )
        print(
            "使用本地账号注册/登录；原云端账号不会自动迁入。Ctrl+C停止应用服务，数据库保留。",
            flush=True,
        )
        if args.smoke:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            with httpx.Client(trust_env=False, timeout=5) as client:
                response = client.get("http://127.0.0.1:18000/ready")
                if response.status_code != 200:
                    raise RuntimeError("API尚未就绪")
                page = client.get(f"http://127.0.0.1:{args.port}/")
                if page.status_code != 200:
                    raise RuntimeError("本地网页尚未就绪")
                scripts = re.findall(r'<script[^>]+src="([^"]+)"', page.text)
                if (
                    not scripts
                    or client.get(
                        f"http://127.0.0.1:{args.port}/" + scripts[0].lstrip("/")
                    ).status_code
                    != 200
                ):
                    raise RuntimeError("网页脚本尚未就绪")
            server.shutdown()
            thread.join(timeout=5)
            print(
                "启动冒烟通过，正在关闭应用服务；没有创建用户或模型任务。", flush=True
            )
            return
        server.serve_forever()
    except KeyboardInterrupt:
        print("正在停止API和Worker，数据库与记录继续保留。", flush=True)
    finally:
        server.server_close()
        command(compose + ["stop", "--timeout", "30", "api", "worker"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true", help="只用模拟模型验证界面")
    parser.add_argument(
        "--check-only", action="store_true", help="检查环境，不启动Worker或模型任务"
    )
    parser.add_argument(
        "--smoke", action="store_true", help="启动就绪后退出；通常配合--mock"
    )
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument(
        "--token-budget",
        type=int,
        choices=range(1000, 200001),
        default=80000,
        metavar="1000..200000",
    )
    main(parser.parse_args())
