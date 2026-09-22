from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from app.clients.supabase import SupabaseClient
from app.config import Settings, get_settings
from app.domain import UserContext


def get_db(settings: Annotated[Settings, Depends(get_settings)]) -> SupabaseClient:
    return SupabaseClient(settings)


async def get_current_user(
    db: Annotated[SupabaseClient, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> UserContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    access_token = authorization.split(" ", 1)[1].strip()
    try:
        profile = await db.resolve_user(access_token)
        return UserContext(id=profile["id"], email=profile.get("email"), access_token=access_token)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效") from exc


CurrentUser = Annotated[UserContext, Depends(get_current_user)]
Database = Annotated[SupabaseClient, Depends(get_db)]
