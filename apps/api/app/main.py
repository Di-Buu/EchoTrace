import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.clients.ai import AiConfigurationError, AiResponseError
from app.clients.supabase import SupabaseError
from app.config import get_settings
from app.routers import chat, insights, memories, moments, system
from app.services.retrieval import UserIsolationError

settings = get_settings()
logger = logging.getLogger("echotrace.ai")
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
app.include_router(system.router)


@app.exception_handler(AiConfigurationError)
async def ai_configuration_error(_: Request, exc: AiConfigurationError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(AiResponseError)
async def ai_response_error(_: Request, exc: AiResponseError) -> JSONResponse:
    logger.warning("AI response error: %s", exc)
    return JSONResponse(status_code=502, content={"detail": "AI 服务暂时不可用，请稍后重试"})


@app.exception_handler(SupabaseError)
async def supabase_error(_: Request, exc: SupabaseError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": "数据服务暂时不可用"})


@app.exception_handler(UserIsolationError)
async def isolation_error(_: Request, exc: UserIsolationError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc), "code": "USER_ISOLATION"})


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "echotrace-api"}
