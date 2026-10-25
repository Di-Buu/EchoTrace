import logging
import sys
import time
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.clients.ai import AiConfigurationError, AiResponseError
from app.clients.supabase import SupabaseError
from app.config import get_settings
from app.routers import account, chat, insights, jobs, memories, moments, system, weekly_reports
from app.runtime import source_fingerprint
from app.services.retrieval import UserIsolationError

settings = get_settings()
runtime_fingerprint = source_fingerprint()
logger = logging.getLogger("echotrace.ai")
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(title="EchoTrace API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.app_origin,
        "http://localhost:8081",
        "http://127.0.0.1:8081",
        "http://localhost:19006",
        "http://127.0.0.1:19006",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(moments.router)
app.include_router(chat.router)
app.include_router(memories.router)
app.include_router(insights.router)
app.include_router(weekly_reports.router)
app.include_router(system.router)
app.include_router(account.router)
app.include_router(jobs.router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = str(uuid4())
    request.state.request_id = request_id
    started = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_complete request_id=%s method=%s status=%s elapsed_ms=%s",
        request_id,
        request.method,
        response.status_code,
        round((time.monotonic() - started) * 1000),
    )
    return response


@app.get("/ready")
async def ready() -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.post(
                f"{settings.supabase_rest_url}/rpc/schema_ready",
                headers={"apikey": settings.supabase_anon_key},
                json={},
            )
            if response.is_error or response.json() != "durable-v2":
                raise ValueError("schema_not_ready")
            product = await client.post(
                f"{settings.supabase_rest_url}/rpc/product_schema_ready",
                headers={"apikey": settings.supabase_anon_key},
                json={},
            )
            if product.is_error or product.json() != "product-fixes-v1":
                raise ValueError("product_schema_not_ready")
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return JSONResponse(content={"status": "ready"})


@app.exception_handler(AiConfigurationError)
async def ai_configuration_error(_: Request, exc: AiConfigurationError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(AiResponseError)
async def ai_response_error(_: Request, exc: AiResponseError) -> JSONResponse:
    logger.warning("ai_response_failed code=%s", type(exc).__name__)
    return JSONResponse(status_code=502, content={"detail": "AI 服务暂时不可用，请稍后重试"})


@app.exception_handler(SupabaseError)
async def supabase_error(_: Request, exc: SupabaseError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": "数据服务暂时不可用"})


@app.exception_handler(httpx.RequestError)
async def network_error(_: Request, exc: httpx.RequestError) -> JSONResponse:
    logger.warning("upstream_network_failed code=%s", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "网络服务暂时不可用，可使用原请求标识重试"})


@app.exception_handler(UserIsolationError)
async def isolation_error(_: Request, exc: UserIsolationError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc), "code": "USER_ISOLATION"})


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "echotrace-api",
        "runtime_fingerprint": runtime_fingerprint,
    }
