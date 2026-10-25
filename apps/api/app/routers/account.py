from fastapi import APIRouter

from app.dependencies import CurrentUser, Database

router = APIRouter(prefix="/account", tags=["account"])


@router.delete("/data")
async def delete_personal_data(user: CurrentUser, db: Database) -> dict:
    """Delete the signed-in user's product data while keeping the Auth account."""
    return (await db.rpc("clear_personal_data", user.access_token, {}))[0]
