from fastapi import APIRouter, Header

from app.dependencies import CurrentUser, Database
from app.domain import ProductEvent
from app.services.telemetry import Telemetry

router = APIRouter(tags=["system"])


@router.post("/events")
async def event(
    payload: ProductEvent,
    user: CurrentUser,
    db: Database,
    client_version: str | None = Header(default=None, alias="X-Client-Version"),
) -> dict:
    await Telemetry(db, user.access_token, user.id).product_event(
        event_name=payload.event_name,
        properties=payload.properties,
        session_id=payload.session_id,
        request_id=payload.request_id,
        client_version=client_version,
    )
    return {"ok": True}
